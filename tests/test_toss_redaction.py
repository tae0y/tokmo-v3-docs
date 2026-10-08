"""Toss holdings redaction, private-artifact, and human-output suite.

Sentinel credentials and sentinel account sequences are driven through a full
simulated campaign; afterwards no private artifact may contain any of them.
The human-facing surfaces carry aggregates and provider field names only.
"""

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tokmo.composition import (
    PROJECT_ROOT, TOSS_ENV_NAME, TOSS_STATE_NAME, TOSS_STORE_NAME,
    LiveRunError, main, run_toss, setup_toss, toss_report,
)
from tokmo.facts.toss import (
    TOSS_CAMPAIGN_CODES, TossAccessToken, TossAccountList, _TossTransportSuccess,
    build_auth_request, build_holdings_request,
)
from tokmo.observability import TracerLogger
from tokmo.programs.toss_holdings import (
    aggregate, diff_latest, field_presence, format_masked_result, format_report,
)
from tokmo.records.fact_store import FactStore, StoreCorruptError, as_record
from tokmo.runtime.config import ConfigError, TossConfig, load_toss_config
from tokmo.values import TOSS_SOURCE, TossAccountHoldingsFact, TossHoldingFact

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
CLIENT_ID = "SENTINEL-CLIENT-ID-0001"
CLIENT_SECRET = "SENTINEL-CLIENT-SECRET-0001"
TOKEN_VALUE = "SENTINEL-ACCESS-TOKEN-0001"
SEQ_ONE = "SENTINEL-SEQ-0001"
SEQ_TWO = "SENTINEL-SEQ-0002"
SENTINELS = (CLIENT_ID, CLIENT_SECRET, TOKEN_VALUE, SEQ_ONE, SEQ_TWO)

TOKEN_BODY = json.dumps({"access_token": TOKEN_VALUE, "expires_in": 3600,
                         "token_type": "Bearer"}).encode()
ACCOUNTS_BODY = json.dumps({"result": [{"accountSeq": SEQ_ONE},
                                       {"accountSeq": SEQ_TWO}]}).encode()
HOLDINGS_BODY = (FIXTURES / "toss_holdings_success.json").read_bytes()


# --- fixtures may never carry a real account sequence or token ---------------

def test_every_fixture_account_sequence_and_token_is_marked_synthetic():
    checked = 0
    for path in sorted(FIXTURES.glob("toss_*.json")):
        payload = json.loads(path.read_text())
        for value in _values_for(payload, ("accountSeq", "access_token")):
            assert value.startswith("SYNTHETIC-"), (path.name, value)
            checked += 1
    assert checked >= 3


def _values_for(node, keys):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys:
                yield str(value)
            yield from _values_for(value, keys)
    elif isinstance(node, list):
        for item in node:
            yield from _values_for(item, keys)


# --- representations ---------------------------------------------------------

def test_no_credential_carrier_renders_its_secret():
    config = TossConfig(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    auth = build_auth_request(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    token = TossAccessToken(expires_in=3600, token_type="Bearer", value=TOKEN_VALUE)
    data = build_holdings_request(token=TOKEN_VALUE, account_seq=SEQ_ONE)
    accounts = TossAccountList((SEQ_ONE, SEQ_TWO))
    body = _TossTransportSuccess(body=HOLDINGS_BODY)

    rendered = " ".join(repr(item) + str(item)
                        for item in (config, auth, token, data, accounts, body))

    for sentinel in SENTINELS:
        assert sentinel not in rendered
    assert "SYN001" not in rendered


def test_a_holding_never_renders_a_position():
    fact = capture_fact(1)

    rendered = repr(fact) + str(fact)

    for value in ("SYN001", "10", "71000", "SYNTHETIC-A"):
        assert value not in rendered
    assert "averagePurchasePrice" in rendered


# --- the event vocabulary survives the logger's marker filter ----------------

def test_every_campaign_outcome_can_be_logged_without_tripping_the_filter(tmp_path):
    from tokmo.composition import log_reason

    logger = TracerLogger(tmp_path, clock=lambda: NOW)
    for code in sorted(TOSS_CAMPAIGN_CODES):
        logger.info(run_id="toss-run", stage="capture", action="SAVE",
                    reason=log_reason(code), source=TOSS_SOURCE, decision_id=None)

    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(lines) == len(TOSS_CAMPAIGN_CODES)


# --- a full simulated campaign leaves no sentinel behind ---------------------

class Clock:
    def __init__(self, start=NOW):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


class Transport:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)

    def __call__(self, _request):
        return self.outcomes.pop(0)


def project_root_of(home):
    """Every test drives a throwaway project root, so the real repository
    credential file is never created, read, or otherwise touched."""
    root = home / "project"
    root.mkdir(exist_ok=True)
    return root


def run_sentinel_campaign(home, clock=None):
    project_root = project_root_of(home)
    setup_toss(home=home, project_root=project_root)
    config = project_root / TOSS_ENV_NAME
    config.write_text(f"TOSS_CLIENT_ID={CLIENT_ID}\nTOSS_CLIENT_SECRET={CLIENT_SECRET}\n")
    os.chmod(config, 0o600)
    clock = clock or Clock()
    ids = iter([f"record-{index}" for index in range(20)])
    return run_toss(
        home=home, project_root=project_root,
        auth_transport=Transport(_TossTransportSuccess(body=TOKEN_BODY)),
        data_transport=Transport(_TossTransportSuccess(body=ACCOUNTS_BODY),
                                 _TossTransportSuccess(body=HOLDINGS_BODY),
                                 _TossTransportSuccess(body=HOLDINGS_BODY)),
        clock=clock, sleeper=clock.sleep, id_factory=ids.__next__,
        run_id="toss-run",
    )


def test_no_private_artifact_contains_a_credential_token_or_account_sequence(tmp_path):
    result = run_sentinel_campaign(tmp_path)

    artifacts = [path for path in tmp_path.rglob("*")
                 if path.is_file() and path.name != TOSS_ENV_NAME]
    hits = {path.name: sum(path.read_text().count(s) for s in SENTINELS)
            for path in artifacts}

    assert result.code == "success"
    assert {TOSS_STATE_NAME, TOSS_STORE_NAME} <= set(hits)
    assert hits == dict.fromkeys(hits, 0)


def test_the_snapshot_store_holds_positions_but_no_account_identity(tmp_path):
    run_sentinel_campaign(tmp_path)
    leaf = tmp_path / ".local" / "share" / "tokmo-v3" / "toss"

    records = json.loads((leaf / TOSS_STORE_NAME).read_text())["records"]

    assert {record["account_ordinal"] for record in records} == {1, 2}
    assert records[0]["holdings"][0]["symbol"] == "SYN001"
    assert all("accountSeq" not in json.dumps(record) for record in records)


def test_the_store_rejects_a_record_carrying_an_account_identifier(tmp_path):
    leaf = tmp_path / "leaf"
    leaf.mkdir(mode=0o700)
    store = FactStore(leaf / TOSS_STORE_NAME, clock=lambda: NOW,
                      id_factory=lambda: "record-0")
    store.save_batch([capture_fact(1)], policy_version="toss1-v1")
    payload = json.loads(store.path.read_text())
    payload["records"][0]["account_seq"] = SEQ_ONE
    store.path.write_text(json.dumps(payload))

    with pytest.raises(StoreCorruptError):
        store.records()


# --- project-local credential file -------------------------------------------

def test_the_credential_file_is_project_local_and_never_home_or_ambient():
    """The user-approved exception: credentials live beside the sources at a
    deterministic project root; runtime artifacts still live under `home`."""
    assert TOSS_ENV_NAME == ".env.toss"
    assert PROJECT_ROOT == Path(__file__).resolve().parents[1]


def test_no_source_module_names_a_home_level_toss_credential_file():
    src_root = Path(__file__).parents[1] / "src" / "tokmo"
    for path in sorted(src_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text()
        assert "toss.env" not in text, path.name
        assert ".config/tokmo-v3" not in text, path.name


def test_both_toss_entry_points_demand_an_explicit_project_root(tmp_path):
    """No implicit default reaches the real repository secret: a caller that
    forgets the argument fails loudly instead of touching `PROJECT_ROOT`."""
    clock = Clock()

    with pytest.raises(TypeError):
        setup_toss(home=tmp_path)
    with pytest.raises(TypeError):
        run_toss(home=tmp_path, auth_transport=Transport(),
                 data_transport=Transport(), clock=clock, sleeper=clock.sleep,
                 id_factory=lambda: "record-0", run_id="toss-run")


def test_setup_writes_only_an_empty_owner_only_credential_template(tmp_path):
    project_root = project_root_of(tmp_path)

    setup_toss(home=tmp_path, project_root=project_root)

    env_path = project_root / TOSS_ENV_NAME
    assert env_path.read_text() == "TOSS_CLIENT_ID=\nTOSS_CLIENT_SECRET=\n"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    with pytest.raises(ConfigError) as error:
        load_toss_config(env_path)
    assert error.value.reason == "config_invalid"


def test_setup_creates_no_home_config_directory_or_legacy_credential_file(tmp_path):
    setup_toss(home=tmp_path, project_root=project_root_of(tmp_path))

    assert not (tmp_path / ".config").exists()
    assert [path.name for path in tmp_path.rglob("toss.env")] == []
    assert (tmp_path / ".local" / "share" / "tokmo-v3" / "toss").is_dir()


def test_setup_never_overwrites_credentials_that_are_already_present(tmp_path):
    project_root = project_root_of(tmp_path)
    setup_toss(home=tmp_path, project_root=project_root)
    env_path = project_root / TOSS_ENV_NAME
    env_path.write_text(f"TOSS_CLIENT_ID={CLIENT_ID}\nTOSS_CLIENT_SECRET={CLIENT_SECRET}\n")

    setup_toss(home=tmp_path, project_root=project_root)

    assert load_toss_config(env_path).client_id == CLIENT_ID


@pytest.mark.parametrize("text,reason", [
    ("TOSS_CLIENT_ID=x\n", "config_missing"),
    ("TOSS_CLIENT_SECRET=y\n", "config_missing"),
    ("TOSS_CLIENT_ID=x\nTOSS_CLIENT_SECRET=\n", "config_invalid"),
    ("TOSS_CLIENT_ID=replace-me\nTOSS_CLIENT_SECRET=y\n", "config_invalid"),
    ("TOSS_CLIENT_ID=x\nTOSS_CLIENT_ID=x\nTOSS_CLIENT_SECRET=y\n", "config_invalid"),
])
def test_the_credential_loader_is_fail_closed(tmp_path, text, reason):
    path = tmp_path / TOSS_ENV_NAME
    path.write_text(text)

    with pytest.raises(ConfigError) as error:
        load_toss_config(path)

    assert error.value.reason == reason


def refused_code(home, project_root):
    """Drive a campaign that must stop before any request is started."""
    clock = Clock()
    with pytest.raises(LiveRunError) as error:
        run_toss(home=home, project_root=project_root,
                 auth_transport=Transport(), data_transport=Transport(),
                 clock=clock, sleeper=clock.sleep, id_factory=lambda: "record-0",
                 run_id="toss-run")
    return error.value.code


def credentials_at(path):
    path.write_text(f"TOSS_CLIENT_ID={CLIENT_ID}\nTOSS_CLIENT_SECRET={CLIENT_SECRET}\n")
    os.chmod(path, 0o600)
    return path


def test_a_world_readable_credential_file_stops_the_campaign(tmp_path):
    project_root = project_root_of(tmp_path)
    setup_toss(home=tmp_path, project_root=project_root)
    os.chmod(credentials_at(project_root / TOSS_ENV_NAME), 0o644)

    assert refused_code(tmp_path, project_root) == "private_state_invalid"


def test_a_symlinked_credential_file_is_neither_replaced_nor_followed(tmp_path):
    project_root = project_root_of(tmp_path)
    setup_toss(home=tmp_path, project_root=project_root)
    env_path = project_root / TOSS_ENV_NAME
    env_path.unlink()
    elsewhere = credentials_at(tmp_path / "elsewhere.env")
    env_path.symlink_to(elsewhere)

    setup_toss(home=tmp_path, project_root=project_root)

    assert env_path.is_symlink()
    assert CLIENT_ID in elsewhere.read_text()
    assert refused_code(tmp_path, project_root) == "private_state_invalid"


def test_a_legacy_home_credential_file_is_never_created_or_read(tmp_path):
    """The retired `~/.config/tokmo-v3/toss.env` location is inert: valid
    credentials sitting there cannot start a campaign."""
    project_root = project_root_of(tmp_path)
    setup_toss(home=tmp_path, project_root=project_root)
    (project_root / TOSS_ENV_NAME).unlink()
    legacy_dir = tmp_path / ".config" / "tokmo-v3"
    legacy_dir.mkdir(mode=0o700, parents=True)
    legacy = credentials_at(legacy_dir / "toss.env")

    assert refused_code(tmp_path, project_root) == "private_state_invalid"
    assert CLIENT_ID in legacy.read_text()  # left exactly as the user had it


def test_ambient_environment_credentials_are_ignored(tmp_path, monkeypatch):
    """Nothing falls back to the process environment — and therefore not to
    a login shell's exports either."""
    project_root = project_root_of(tmp_path)
    setup_toss(home=tmp_path, project_root=project_root)
    (project_root / TOSS_ENV_NAME).unlink()
    monkeypatch.setenv("TOSS_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("TOSS_CLIENT_SECRET", CLIENT_SECRET)

    assert refused_code(tmp_path, project_root) == "private_state_invalid"


def test_an_ambient_environment_never_overrides_the_project_file(tmp_path,
                                                                 monkeypatch):
    project_root = project_root_of(tmp_path)
    setup_toss(home=tmp_path, project_root=project_root)
    monkeypatch.setenv("TOSS_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("TOSS_CLIENT_SECRET", CLIENT_SECRET)

    # The file still holds the empty template, so the campaign stops there.
    assert refused_code(tmp_path, project_root) == "private_state_invalid"


# --- human-facing output is aggregate-only -----------------------------------

def capture_fact(ordinal, *, captured_at=NOW, count=2, quantity="10"):
    return TossAccountHoldingsFact(
        source=TOSS_SOURCE, captured_at=captured_at, account_ordinal=ordinal,
        account_count=count,
        holdings=(TossHoldingFact(
            symbol="SYN001", quantity=quantity, avg_price="71000",
            avg_price_field="averagePurchasePrice", currency="KRW",
            market_raw="KR", market_field="market", name="SYNTHETIC-A",
            asset_type="equity"),))


def test_the_aggregate_counts_never_include_a_position():
    facts = (capture_fact(1), capture_fact(2))

    counts = aggregate(facts)
    presence = field_presence(facts)

    assert counts == {"accounts": 2, "holdings_items": 2,
                      "currencies_observed": 1, "markets_observed": 1}
    assert presence == {"avg_price_fields": ["averagePurchasePrice"],
                        "market_fields": ["market"], "item_counts": [1, 1]}


def test_the_masked_result_line_carries_no_symbol_quantity_or_price(tmp_path):
    result = run_sentinel_campaign(tmp_path)

    line = format_masked_result(result)

    assert "accounts=2" in line and "saved=2" in line
    assert "avg_price_field=averagePurchasePrice" in line
    for value in ("SYN001", "71000", "120.75", *SENTINELS):
        assert value not in line


def test_the_report_states_observational_counts_and_claims_no_causality(tmp_path):
    later = NOW + timedelta(days=1)
    records = records_for((capture_fact(1), capture_fact(2)))
    records += records_for((capture_fact(1, captured_at=later, quantity="4"),
                            capture_fact(2, captured_at=later, quantity="16")))

    diff = diff_latest(records)
    line = format_report(diff)

    assert diff["counts"] == {"changed": 2, "added": 0, "removed": 0,
                              "current_items": 2, "previous_items": 2}
    assert diff["changed"][0] == {
        "account_ordinal": 1, "symbol": "SYN001",
        "quantity_from": "10", "quantity_to": "4",
        "avg_price_from": "71000", "avg_price_to": "71000"}
    assert diff["account_count_changed"] is False
    assert set(diff) == {"current_captured_at", "previous_captured_at",
                         "account_count_changed", "changed", "added", "removed",
                         "counts"}
    assert "changed=2" in line and "added=0" in line
    for value in ("SYN001", "71000", "4", "16"):
        assert value not in line.split("captured_at=")[0]


def test_a_first_capture_has_nothing_to_compare_against():
    diff = diff_latest(records_for((capture_fact(1),)))

    assert diff["previous_captured_at"] is None
    assert diff["counts"]["previous_items"] == 0
    assert diff["counts"]["added"] == 0


def test_an_account_count_change_is_surfaced_not_silently_diffed():
    later = NOW + timedelta(days=1)
    records = records_for((capture_fact(1, count=1),))
    records += records_for((capture_fact(1, captured_at=later),
                            capture_fact(2, captured_at=later)))

    diff = diff_latest(records)

    assert diff["account_count_changed"] is True


def records_for(facts):
    return [as_record(fact, f"record-{fact.account_ordinal}", NOW, "toss1-v1")
            for fact in facts]


def test_the_report_command_prints_counts_only(tmp_path, capsys):
    run_sentinel_campaign(tmp_path)

    toss_report(home=tmp_path)

    printed = capsys.readouterr().out
    assert "changed=" in printed and "accounts=" in printed
    for value in ("SYN001", "71000", *SENTINELS):
        assert value not in printed


def test_the_detailed_comparison_is_written_only_inside_the_private_leaf(tmp_path):
    run_sentinel_campaign(tmp_path)
    leaf = tmp_path / ".local" / "share" / "tokmo-v3" / "toss"

    diff_path = leaf / "latest_diff.json"
    assert stat.S_IMODE(diff_path.stat().st_mode) == 0o600
    assert set(json.loads(diff_path.read_text())) == {
        "current_captured_at", "previous_captured_at", "account_count_changed",
        "changed", "added", "removed", "counts"}
    assert [path.name for path in tmp_path.rglob("latest_diff.json")] == [
        "latest_diff.json"]


# --- the command line never carries a credential -----------------------------

def test_no_toss_verb_accepts_a_credential_argument(tmp_path, capsys):
    project_root = project_root_of(tmp_path)
    where = ["--home", str(tmp_path), "--project-root", str(project_root)]

    assert main(["toss-setup", *where]) == 0
    assert main(["toss-holdings", CLIENT_SECRET, *where]) == 64

    printed = capsys.readouterr()
    assert CLIENT_SECRET not in printed.out
    assert "toss-setup" in printed.err and "--project-root" in printed.err


def test_the_cli_writes_the_template_into_the_given_project_root_only(tmp_path):
    project_root = project_root_of(tmp_path)

    assert main(["toss-setup", "--project-root", str(project_root),
                 "--home", str(tmp_path)]) == 0

    assert (project_root / TOSS_ENV_NAME).read_text() == (
        "TOSS_CLIENT_ID=\nTOSS_CLIENT_SECRET=\n")
    assert not (tmp_path / ".config").exists()
