import json
import stat
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tokmo.composition import run_fixture
from tokmo.facts.eia import HttpResponse, get_wti, parse_wti_response
from tokmo.observability import TracerLogger
from tokmo.programs.tracer import run_tracer
from tokmo.records.decision_journal import DecisionJournal
from tokmo.records.fact_store import FactStore
from tokmo.runtime.config import ConfigError, load_config
from tokmo.values import SaveStatus, UnavailableFact, WtiFact

NOW = datetime(2026, 7, 27, 1, 2, 3, tzinfo=UTC)
BODY = b'{"response":{"data":[{"period":"2026-07-24","series":"RWTC","value":"78.420","units":"$/BBL"}]}}'
FIXTURE = Path(__file__).parent / "fixtures" / "eia_wti_success.json"
POLICY = "tracer1-v1"

RECORD_FIELDS = {
    "record_id",
    "source",
    "series",
    "period",
    "value",
    "units",
    "observed_at",
    "stored_at",
    "policy_version",
}
DECISION_FIELDS = {
    "decision_id",
    "run_id",
    "source_ref",
    "action",
    "reason",
    "observed_at",
    "decided_at",
    "policy_version",
}


def fact(value="78.420"):
    return WtiFact(
        "eia_wti_rwtc", "RWTC", date(2026, 7, 24), Decimal(value), "$/BBL", NOW
    )


def make_store(root, record_id="rec-1"):
    return FactStore(
        root / "facts.json", clock=lambda: NOW, id_factory=lambda: record_id
    )


def test_env_canonical_and_export_forms_load_equally(tmp_path):
    canonical = tmp_path / "a.env"
    exported = tmp_path / "b.env"
    canonical.write_text("MODE=tracer\nEIA_API_KEY=fixture-key\n")
    exported.write_text("export MODE=tracer\nexport EIA_API_KEY=fixture-key\n")
    assert load_config(canonical) == load_config(exported)


@pytest.mark.parametrize(
    "content,reason",
    [
        (None, "config_missing"),
        ("MODE=tracer\nEIA_API_KEY=replace-me\n", "config_invalid"),
        ("MODE=tracer\nMODE=tracer\nEIA_API_KEY=x\n", "config_invalid"),
        ("MODE=live\nEIA_API_KEY=x\n", "config_invalid"),
        ("MODE tracer\nEIA_API_KEY=x\n", "config_invalid"),
    ],
)
def test_env_errors_are_typed_and_do_not_expose_values(tmp_path, content, reason):
    path = tmp_path / "x.env"
    if content is not None:
        path.write_text(content)

    with pytest.raises(ConfigError) as caught:
        load_config(path)

    assert caught.value.reason == reason
    assert "replace-me" not in str(caught.value)


@pytest.mark.parametrize("raw", ["78.420", 78.42])
def test_eia_success_accepts_string_and_json_number(raw):
    row = {"period": "2026-07-24", "series": "RWTC", "value": raw, "units": "$/BBL"}
    body = json.dumps({"response": {"data": [row]}}).encode()

    result = parse_wti_response(body, observed_at=NOW)

    assert result == fact("78.420" if isinstance(raw, str) else "78.42")


@pytest.mark.parametrize(
    "body,reason",
    [
        (b'{"response":{"data":[]}}', "no_published_data"),
        (
            b'{"response":{"data":[{"period":"2026-07-24","series":"BAD","value":"1","units":"$/BBL"}]}}',
            "contract_mismatch",
        ),
        (b"{bad", "parse_error"),
    ],
)
def test_eia_bad_fixtures_return_typed_unavailable(body, reason):
    result = parse_wti_response(body, observed_at=NOW)
    assert isinstance(result, UnavailableFact)
    assert result.reason == reason


@pytest.mark.parametrize(
    "transport,reason,retry",
    [
        (
            lambda _request: (_ for _ in ()).throw(TimeoutError()),
            "transport_error",
            None,
        ),
        (
            lambda _request: (_ for _ in ()).throw(ConnectionError()),
            "transport_error",
            None,
        ),
        (
            lambda _request: HttpResponse(429, b"", {"Retry-After": "17"}),
            "rate_limited",
            17,
        ),
    ],
)
def test_transport_failures_map_to_typed_unavailable(transport, reason, retry):
    result = get_wti(api_key="fixture-key", transport=transport, clock=lambda: NOW)
    assert isinstance(result, UnavailableFact)
    assert (result.reason, result.retry_after) == (reason, retry)


def test_success_is_atomically_saved_and_journaled(tmp_path):
    result = run_tracer(
        fact(),
        store=make_store(tmp_path),
        journal=DecisionJournal(tmp_path / "decisions.jsonl"),
        clock=lambda: NOW,
        id_factory=lambda: "dec-1",
        run_id="run-1",
    )

    assert result.save_result.status is SaveStatus.SAVED
    record = json.loads((tmp_path / "facts.json").read_text())["records"][0]
    assert set(record) == RECORD_FIELDS
    assert record["value"] == "78.42"
    assert record["units"] == "$/BBL"
    assert record["policy_version"] == POLICY

    decision = json.loads((tmp_path / "decisions.jsonl").read_text())
    assert set(decision) == DECISION_FIELDS
    assert decision["action"] == "SAVE"
    assert decision["reason"] == "saved"


def test_unavailable_skips_store_and_journals_reason(tmp_path):
    result = run_tracer(
        UnavailableFact("eia_wti_rwtc", "no_published_data", NOW),
        store=make_store(tmp_path),
        journal=DecisionJournal(tmp_path / "decisions.jsonl"),
        clock=lambda: NOW,
        id_factory=lambda: "dec-1",
        run_id="run-1",
    )

    assert result.save_result is None
    assert not (tmp_path / "facts.json").exists()
    decision = json.loads((tmp_path / "decisions.jsonl").read_text())
    assert decision["action"] == "SKIP"
    assert decision["reason"] == "SKIP:no_published_data"


def test_duplicate_and_conflict_are_deterministic_and_preserve_original(tmp_path):
    store = make_store(tmp_path)

    first = store.save(fact(), policy_version=POLICY)
    duplicate = store.save(fact(), policy_version=POLICY)
    conflict = store.save(fact("99"), policy_version=POLICY)

    assert (first.status, duplicate.status, conflict.status) == (
        SaveStatus.SAVED,
        SaveStatus.DUPLICATE,
        SaveStatus.CONFLICT,
    )
    assert conflict.reason == "identity_conflict"
    assert (
        json.loads((tmp_path / "facts.json").read_text())["records"][0]["value"]
        == "78.42"
    )


def test_corrupt_store_fails_closed_and_writes_diagnostic(tmp_path):
    path = tmp_path / "facts.json"
    path.write_text("bad")

    result = make_store(tmp_path).save(fact(), policy_version=POLICY)

    assert result.status is SaveStatus.FAILED
    assert result.reason == "store_corrupt"
    assert (tmp_path / "facts.json.diagnostic.json").exists()
    assert path.read_text() == "bad"


def test_restart_recovers_records(tmp_path):
    make_store(tmp_path, record_id="rec-1").save(fact(), policy_version=POLICY)
    reopened = make_store(tmp_path, record_id="unused")
    assert reopened.records()[0]["record_id"] == "rec-1"


def test_replay_with_same_injections_returns_same_action_reason(tmp_path):
    def execute(root):
        parsed = parse_wti_response(BODY, observed_at=NOW)
        return run_tracer(
            parsed,
            store=make_store(root),
            journal=DecisionJournal(root / "decisions.jsonl"),
            clock=lambda: NOW,
            id_factory=lambda: "dec-1",
            run_id="run-1",
        ).decision

    first = execute(tmp_path / "a")
    replay = execute(tmp_path / "b")
    assert (first.action, first.reason) == (replay.action, replay.reason)


def test_logger_flushes_debug_on_error_and_rca_and_all_files_are_private(tmp_path):
    logger = TracerLogger(tmp_path, clock=lambda: NOW, capacity=2)
    logger.debug(
        run_id="run",
        stage="parse",
        action="BEGIN",
        reason="sanitized_fixture",
        source="eia_wti_rwtc",
        decision_id=None,
    )
    logger.error(
        run_id="run",
        stage="parse",
        action="FAIL",
        reason="parse_error",
        source="eia_wti_rwtc",
        decision_id=None,
    )
    logger.debug(
        run_id="run",
        stage="retry",
        action="AGAIN",
        reason="sanitized_fixture",
        source="eia_wti_rwtc",
        decision_id=None,
    )
    logger.flush_rca("run")

    for path in tmp_path.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert "BEGIN" in (tmp_path / "incident.jsonl").read_text()
    rca = (tmp_path / "rca.jsonl").read_text()
    # The ERROR flush drained the buffer, so RCA carries only post-error DEBUG.
    assert "AGAIN" in rca
    assert "BEGIN" not in rca


def test_fixture_composition_runs_end_to_end_and_replays(tmp_path):
    env = tmp_path / "tracer.env"
    env.write_text("MODE=tracer\nEIA_API_KEY=fixture-key\n")

    def execute(output_dir):
        return run_fixture(
            env_path=env,
            fixture_path=FIXTURE,
            output_dir=output_dir,
            clock=lambda: NOW,
            id_factory=iter(["rec", "dec"]).__next__,
            run_id="run",
        )

    first = execute(tmp_path / "first")
    replay = execute(tmp_path / "replay")

    assert (first.decision.action, first.decision.reason) == ("SAVE", "saved")
    assert (replay.decision.action, replay.decision.reason) == ("SAVE", "saved")
    assert (tmp_path / "first" / "events.jsonl").exists()


def test_artifacts_do_not_contain_secret_url_or_legacy_path(tmp_path):
    secret = "fixture-key"
    env = tmp_path / "tracer.env"
    env.write_text(f"MODE=tracer\nEIA_API_KEY={secret}\n")
    output_dir = tmp_path / "out"

    run_fixture(
        env_path=env,
        fixture_path=FIXTURE,
        output_dir=output_dir,
        clock=lambda: NOW,
        id_factory=iter(["rec", "dec"]).__next__,
        run_id="run",
    )

    artifacts = [path for path in output_dir.rglob("*") if path.is_file()]
    assert artifacts
    combined = "\n".join(path.read_text() for path in artifacts)
    for banned in (secret, "https://", "api.eia.gov", "api_key", "stock-monitor"):
        assert banned not in combined, banned
