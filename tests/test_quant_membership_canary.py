import json
import os
from datetime import UTC, date, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


def test_fixed_composition_sic_path_starts_before_io_and_reads_back(tmp_path):
    from tokmo.composition import execute_official_membership_sic_capture
    from tokmo.facts.official_membership_live import LiveOutcomeV1
    from tokmo.records.official_membership_campaign_store import (
        OfficialMembershipCampaignStore,
    )
    from tokmo.records.official_membership_capture_store import (
        OfficialMembershipCaptureStore,
    )

    campaign = OfficialMembershipCampaignStore(
        tmp_path / "campaign" / "ledger.sqlite3",
        clock=lambda: datetime(2026, 8, 13, tzinfo=UTC),
    )
    capture = OfficialMembershipCaptureStore(
        tmp_path / "official-membership" / "documentation-captures" / "capture.sqlite3"
    )
    body = b"<table><tr><td>3571</td></tr></table>"

    class FakeTransport:
        def fetch(self, identity, *, sec_user_agent):
            assert campaign.usage() == 1
            return LiveOutcomeV1(
                True, "OK", body, datetime(2026, 8, 13, tzinfo=UTC), 200
            )

    receipt = execute_official_membership_sic_capture(
        campaign_store=campaign,
        capture_store=capture,
        transport=FakeTransport(),
        sec_user_agent="fixture agent contact@example.test",
    )
    assert campaign.usage() == 1
    assert campaign.read(1).status == "SUCCEEDED"
    assert capture.read(receipt.capture_id).body == body


def test_fixed_tools_perform_offline_preflight_without_transport(monkeypatch, capsys):
    root = Path(__file__).parents[1]
    for name, expected in (
        ("run_sec_sic_capture", (2,)),
        ("run_quant_membership_canary", (0, 2)),
    ):
        spec = spec_from_file_location(name, root / "tools" / f"{name}.py")
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.main(["--mode", "offline-preflight"]) in expected
        assert not any(
            token in capsys.readouterr().err for token in ("https://", "CIK", "/Users/")
        )


def test_exact_cohort_composition_seam_exists_and_is_fixed():
    from tokmo.composition import execute_official_membership_canary

    assert callable(execute_official_membership_canary)


def test_exact_cohort_fake_path_uses_seven_starts_and_canonical_readback(tmp_path):
    from tokmo.composition import execute_official_membership_canary
    from tokmo.facts.official_membership_live import EndpointKindV1, LiveOutcomeV1
    from tokmo.records.official_membership_campaign_store import (
        OfficialMembershipCampaignStore,
    )
    from tokmo.records.official_membership_observation_store import (
        OfficialMembershipObservationStore,
    )
    from tokmo.values.current_holdings import (
        AccountHoldingsV1,
        AccountScopeIdentityV1,
        CurrentHoldingsV1,
        HoldingItemV1,
        canonical_holdings_bytes,
    )
    from tokmo.values.official_membership_observation import OfficialSicSnapshotV1

    captured = datetime(2026, 8, 13, 1, 20, tzinfo=UTC)
    symbols = ("AAA", "BBB", "CCC", "DDD")
    scope = "toss-acct-" + "1" * 24
    holdings = CurrentHoldingsV1(
        1,
        (
            AccountHoldingsV1(
                scope,
                1,
                tuple(HoldingItemV1(s, "1", "1", "USD", "US", None) for s in symbols),
            ),
        ),
        True,
        captured,
        {scope: AccountScopeIdentityV1(scope, "personal", 1, None)},
    )
    holdings_bytes = canonical_holdings_bytes(holdings)
    campaign = OfficialMembershipCampaignStore(
        tmp_path / "campaign" / "ledger.sqlite3",
        clock=lambda: datetime(2026, 8, 13, 1, 25, tzinfo=UTC),
    )
    observations = OfficialMembershipObservationStore(
        tmp_path / "official-membership" / "observations.sqlite3",
        stamp_factory=lambda: datetime(2026, 8, 13, 1, 31, tzinfo=UTC),
    )
    listed = (
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
        + "\n".join(f"{s}|{s} Common Stock|Q|N|N|100|N|N" for s in symbols[:2])
        + "\nFile Creation Time: 081320260125|||||||\n"
    ).encode()
    other = (
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
        + "\n".join(f"{s}|{s} Common Stock|N|{s}|N|100|N|{s}" for s in symbols[2:])
        + "\nFile Creation Time: 081320260125|||||||\n"
    ).encode()
    registry = json.dumps(
        {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [
                [index + 1, s, s, "Nasdaq" if index < 2 else "NYSE"]
                for index, s in enumerate(symbols)
            ],
        }
    ).encode()
    bodies = {
        EndpointKindV1.NASDAQ_LISTED: listed,
        EndpointKindV1.OTHER_LISTED: other,
        EndpointKindV1.SEC_REGISTRY: registry,
    }

    class FakeTransport:
        def fetch(self, identity, *, sec_user_agent):
            if identity.endpoint_kind is EndpointKindV1.SEC_SUBMISSIONS:
                index = int(identity.cik)
                symbol = symbols[index - 1]
                body = json.dumps(
                    {
                        "cik": index,
                        "sic": "3571",
                        "tickers": [symbol],
                        "exchanges": ["Nasdaq" if index < 3 else "NYSE"],
                    }
                ).encode()
            else:
                body = bodies[identity.endpoint_kind]
            return LiveOutcomeV1(
                True, "OK", body, datetime(2026, 8, 13, 1, 26, tzinfo=UTC), 200
            )

    reads = 0

    def holdings_reader():
        nonlocal reads
        reads += 1
        return holdings_bytes, holdings

    snapshot = OfficialSicSnapshotV1(
        "sec-sic-html-v1", "RECONSTRUCTED_CONTRACT", ("3571",), "fixture-sec-sic"
    )
    receipt, materials, proofs = execute_official_membership_canary(
        campaign_store=campaign,
        observation_store=observations,
        transport=FakeTransport(),
        holdings_reader=holdings_reader,
        sic_snapshot=snapshot,
        sec_user_agent="fixture contact@example.test",
        clock=lambda: datetime(2026, 8, 13, 1, 30, tzinfo=UTC),
        acquisition_id="acq-fixed-offline-1",
    )
    assert campaign.usage() == 7 and reads == 2
    assert receipt.status == "SAVED"
    assert tuple(item.symbol for item in materials) == symbols
    assert len(proofs) == 4


def test_holdings_mutation_aborts_before_canonical_append(monkeypatch):
    import inspect

    from tokmo import composition

    source = inspect.getsource(composition.execute_official_membership_canary)
    assert source.index("second_holdings_bytes, _ = holdings_reader()") < source.index(
        "observation_store.append_success("
    )
    assert 'raise ValueError("holdings_mutated")' in source


def _private_file(path, body):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    path.write_bytes(body)
    os.chmod(path, 0o600)


def test_public_cli_fixed_preflight_validates_complete_gate_without_starts(
    tmp_path, monkeypatch
):
    from tokmo.records.current_holdings_store import CurrentHoldingsStore
    from tokmo.records.official_membership_campaign_store import (
        OfficialMembershipCampaignStore,
    )
    from tokmo.records.official_membership_observation_store import (
        OfficialMembershipObservationStore,
    )
    from tokmo.values.current_holdings import (
        AccountHoldingsV1,
        AccountScopeIdentityV1,
        CurrentHoldingsV1,
        HoldingItemV1,
    )
    from tokmo.values.official_membership_observation import OfficialSicSnapshotV1

    root = Path(__file__).parents[1]
    spec = spec_from_file_location(
        "fixed_canary_cli", root / "tools" / "run_quant_membership_canary.py"
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    now = datetime(2026, 8, 13, 1, 25, tzinfo=UTC)
    paths = module.FixedProductionPaths.for_test(tmp_path)
    _private_file(paths.sec_config, b"SEC_USER_AGENT=fixture contact@example.test\n")
    scope = "toss-acct-" + "2" * 24
    holdings = CurrentHoldingsV1(
        1,
        (
            AccountHoldingsV1(
                scope,
                1,
                tuple(
                    HoldingItemV1(symbol, "1", "1", "USD", "US", None)
                    for symbol in ("AAA", "BBB", "CCC", "DDD")
                ),
            ),
        ),
        True,
        now,
        {scope: AccountScopeIdentityV1(scope, "personal", 1, None)},
    )
    CurrentHoldingsStore(paths.holdings, clock=lambda: now).replace(
        holdings, identity_fingerprints={scope: "3" * 64}
    )
    campaign = OfficialMembershipCampaignStore(paths.campaign, clock=lambda: now)
    observations = OfficialMembershipObservationStore(
        paths.observations, stamp_factory=lambda: now
    )
    context = module._offline_preflight_for_test(
        paths=paths,
        now=now,
        sic_snapshot=OfficialSicSnapshotV1(
            "sec-sic-html-v1", "OFFICIAL_CAPTURE", ("3571",), "fixture"
        ),
        campaign_store=campaign,
        observation_store=observations,
    )
    assert context.expected_count == 4
    assert context.remaining_capacity == 32
    assert campaign.usage() == 0


def test_public_cli_has_no_path_store_transport_or_identity_overrides():
    root = Path(__file__).parents[1]
    source = (root / "tools" / "run_quant_membership_canary.py").read_text()
    for forbidden in (
        "--campaign",
        "--store",
        "--root",
        "--config",
        "--url",
        "--symbol",
        "--cik",
        "--user-agent",
        "--transport",
    ):
        assert forbidden not in source
    assert "FixedProductionPaths.production()" in source
    production_body = source.split("def _production_preflight", 1)[1].split(
        "def _failure", 1
    )[0]
    assert "_campaign_store_for_test" not in production_body
    assert "_offline_preflight_for_test" not in production_body


def test_canary_production_holdings_path_matches_active_cadence_contract():
    root = Path(__file__).parents[1]
    cadence = (root / "src/tokmo/programs/production_cadence.py").read_text()
    assert 'root / "stores" / "current_holdings.json"' in cadence
    spec = spec_from_file_location(
        "canary_canonical_path", root / "tools/run_quant_membership_canary.py"
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    paths = module.FixedProductionPaths.production()
    assert paths.holdings.relative_to(Path.home() / ".local/share/tokmo-v3") == Path(
        "stores/current_holdings.json"
    )
    assert (
        "toss/current_holdings.json"
        not in (root / "tools/run_quant_membership_canary.py").read_text()
    )


def test_fixed_campaign_path_rejects_reset_alias_and_preserves_32_ceiling(tmp_path):
    root = Path(__file__).parents[1]
    spec = spec_from_file_location(
        "fixed_canary_paths", root / "tools" / "run_quant_membership_canary.py"
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    paths = module.FixedProductionPaths.for_test(tmp_path)
    paths.campaign.parent.mkdir(parents=True, mode=0o700)
    os.chmod(paths.campaign.parent, 0o700)
    store = module._campaign_store_for_test(
        paths, now=lambda: datetime(2026, 8, 13, tzinfo=UTC)
    )
    for index in range(32):
        store.reserve("SEC_SIC")
    reopened = module._campaign_store_for_test(
        paths, now=lambda: datetime(2026, 8, 13, tzinfo=UTC)
    )
    assert reopened.usage() == 32
    try:
        reopened.reserve("SEC_SIC")
    except ValueError as error:
        assert str(error) == "campaign_ceiling"
    else:
        raise AssertionError("33rd start admitted")
    alias = paths.campaign.with_name("reset.sqlite3")
    try:
        module._campaign_store_for_test(
            paths, now=lambda: datetime.now(UTC), candidate=alias
        )
    except ValueError as error:
        assert str(error) == "campaign_path"
    else:
        raise AssertionError("alternate campaign path admitted")


def test_fixed_campaign_factory_rejects_hardlinked_ledger_before_sqlite_io(tmp_path):
    root = Path(__file__).parents[1]
    spec = spec_from_file_location(
        "fixed_canary_hardlink", root / "tools" / "run_quant_membership_canary.py"
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    paths = module.FixedProductionPaths.for_test(tmp_path)
    paths.campaign.parent.mkdir(parents=True, mode=0o700)
    paths.campaign.write_bytes(b"not-opened-as-sqlite")
    os.chmod(paths.campaign, 0o600)
    os.link(paths.campaign, paths.campaign.with_name("alias.sqlite3"))
    try:
        module._campaign_store_for_test(paths, now=lambda: datetime.now(UTC))
    except ValueError as error:
        assert str(error) == "campaign_path"
    else:
        raise AssertionError("hardlinked campaign ledger admitted")


def test_sic_public_preflight_uses_shared_fixed_ledger_and_zero_starts(tmp_path):
    from tokmo.records.official_membership_campaign_store import (
        OfficialMembershipCampaignStore,
    )
    from tokmo.records.official_membership_capture_store import (
        OfficialMembershipCaptureStore,
    )

    root = Path(__file__).parents[1]
    spec = spec_from_file_location(
        "sic_fixed_preflight", root / "tools/run_sec_sic_capture.py"
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    paths = module.FixedSicPaths.for_test(tmp_path)
    _private_file(paths.sec_config, b"SEC_USER_AGENT=fixture contact@example.test\n")
    paths.campaign.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(paths.campaign.parent, 0o700)
    paths.capture.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(paths.capture.parent, 0o700)
    campaign = OfficialMembershipCampaignStore(
        paths.campaign, clock=lambda: datetime(2026, 8, 13, tzinfo=UTC)
    )
    capture = OfficialMembershipCaptureStore(paths.capture)
    result = module._offline_preflight_for_test(
        paths=paths, campaign_store=campaign, capture_store=capture
    )
    assert result.remaining_capacity == 32
    assert campaign.usage() == 0 and capture.count() == 0


def test_sic_public_cli_has_no_dependency_overrides_and_rejects_path_aliases(tmp_path):
    root = Path(__file__).parents[1]
    tool = root / "tools/run_sec_sic_capture.py"
    source = tool.read_text()
    for forbidden in (
        "--campaign",
        "--store",
        "--root",
        "--config",
        "--cap",
        "--user-agent",
        "--transport",
        "--url",
    ):
        assert forbidden not in source
    spec = spec_from_file_location("sic_fixed_paths", tool)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    paths = module.FixedSicPaths.for_test(tmp_path)
    paths.campaign.parent.mkdir(parents=True, mode=0o700)
    os.chmod(paths.campaign.parent, 0o700)
    alias = paths.campaign.with_name("reset-campaign.sqlite3")
    try:
        module._campaign_store_for_test(paths, candidate=alias)
    except ValueError as error:
        assert str(error) == "campaign_path"
    else:
        raise AssertionError("alternate SIC campaign admitted")


def test_sic_preflight_refuses_capacity_and_prior_capture_without_new_start(tmp_path):
    from tokmo.records.official_membership_campaign_store import (
        OfficialMembershipCampaignStore,
    )
    from tokmo.records.official_membership_capture_store import (
        OfficialMembershipCaptureStore,
    )

    root = Path(__file__).parents[1]
    spec = spec_from_file_location(
        "sic_conflict", root / "tools/run_sec_sic_capture.py"
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    paths = module.FixedSicPaths.for_test(tmp_path)
    _private_file(paths.sec_config, b"SEC_USER_AGENT=fixture contact@example.test\n")
    paths.campaign.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.capture.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    campaign = OfficialMembershipCampaignStore(
        paths.campaign, clock=lambda: datetime(2026, 8, 13, tzinfo=UTC)
    )
    capture = OfficialMembershipCaptureStore(paths.capture)
    capture.append_success(
        body=b"official", captured_at=datetime(2026, 8, 13, tzinfo=UTC)
    )
    try:
        module._offline_preflight_for_test(
            paths=paths, campaign_store=campaign, capture_store=capture
        )
    except ValueError as error:
        assert str(error) == "capture_conflict"
    else:
        raise AssertionError("prior SIC capture admitted")
    assert campaign.usage() == 0


def test_sic_preflight_rejects_exhausted_shared_ceiling_and_unsafe_ledger(tmp_path):
    from tokmo.records.official_membership_campaign_store import (
        OfficialMembershipCampaignStore,
    )
    from tokmo.records.official_membership_capture_store import (
        OfficialMembershipCaptureStore,
    )

    root = Path(__file__).parents[1]
    spec = spec_from_file_location("sic_ceiling", root / "tools/run_sec_sic_capture.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    paths = module.FixedSicPaths.for_test(tmp_path)
    _private_file(paths.sec_config, b"SEC_USER_AGENT=fixture contact@example.test\n")
    paths.campaign.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.capture.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    campaign = OfficialMembershipCampaignStore(
        paths.campaign, clock=lambda: datetime(2026, 8, 13, tzinfo=UTC)
    )
    for _ in range(32):
        campaign.reserve("SEC_SIC")
    try:
        module._offline_preflight_for_test(
            paths=paths,
            campaign_store=campaign,
            capture_store=OfficialMembershipCaptureStore(paths.capture),
        )
    except ValueError as error:
        assert str(error) == "campaign_capacity"
    else:
        raise AssertionError("exhausted campaign admitted")
    assert campaign.usage() == 32
    hard = paths.campaign.with_name("hard.sqlite3")
    os.link(paths.campaign, hard)
    try:
        module._campaign_store_for_test(paths)
    except ValueError as error:
        assert str(error) == "campaign_path"
    else:
        raise AssertionError("hardlinked canonical campaign admitted")


def test_sic_and_canary_production_campaign_paths_are_exactly_shared():
    root = Path(__file__).parents[1]
    modules = []
    for name in ("run_sec_sic_capture", "run_quant_membership_canary"):
        spec = spec_from_file_location(name + "_shared", root / "tools" / f"{name}.py")
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)
    assert (
        modules[0].FixedSicPaths.production().campaign
        == modules[1].FixedProductionPaths.production().campaign
    )


def test_sic_gate_distinguishes_missing_parent_from_unsafe_parent(tmp_path):
    root = Path(__file__).parents[1]
    spec = spec_from_file_location(
        "sic_typed_paths", root / "tools/run_sec_sic_capture.py"
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    missing = tmp_path / "missing"
    try:
        module._private_dir(missing)
    except module.SicGateError as error:
        assert error.code == "REQUIRED_DIRECTORY_MISSING"
    else:
        raise AssertionError("missing directory admitted")
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    os.chmod(unsafe, 0o755)
    try:
        module._private_dir(unsafe)
    except module.SicGateError as error:
        assert error.code == "PRIVATE_PATH_INVALID"
    else:
        raise AssertionError("unsafe directory admitted")


def test_sic_armed_fake_execution_preflights_reserves_once_and_reads_back(tmp_path):
    from tokmo.facts.official_membership_live import LiveOutcomeV1
    from tokmo.records.official_membership_campaign_store import (
        OfficialMembershipCampaignStore,
    )
    from tokmo.records.official_membership_capture_store import (
        OfficialMembershipCaptureStore,
    )

    root = Path(__file__).parents[1]
    spec = spec_from_file_location(
        "sic_armed_fake", root / "tools/run_sec_sic_capture.py"
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    paths = module.FixedSicPaths.for_test(tmp_path)
    _private_file(paths.sec_config, b"SEC_USER_AGENT=fixture contact@example.test\n")
    paths.campaign.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.capture.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    campaign = OfficialMembershipCampaignStore(
        paths.campaign, clock=lambda: datetime(2026, 8, 13, tzinfo=UTC)
    )
    capture = OfficialMembershipCaptureStore(paths.capture)
    calls = []

    class FakeTransport:
        def fetch(self, identity, *, sec_user_agent):
            calls.append(identity)
            assert campaign.usage() == 1
            return LiveOutcomeV1(
                True, "OK", b"official", datetime(2026, 8, 13, tzinfo=UTC), 200
            )

    receipt = module._execute_armed_for_test(
        paths=paths,
        campaign_store=campaign,
        capture_store=capture,
        transport=FakeTransport(),
    )
    assert len(calls) == 1 and campaign.usage() == 1
    assert campaign.read(1).status == "SUCCEEDED"
    assert capture.read(receipt.capture_id) == receipt


def test_sic_armed_failure_consumes_one_start_without_retry(tmp_path):
    from tokmo.facts.official_membership_live import LiveOutcomeV1
    from tokmo.records.official_membership_campaign_store import (
        OfficialMembershipCampaignStore,
    )
    from tokmo.records.official_membership_capture_store import (
        OfficialMembershipCaptureStore,
    )

    root = Path(__file__).parents[1]
    spec = spec_from_file_location(
        "sic_armed_failure", root / "tools/run_sec_sic_capture.py"
    )
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    paths = module.FixedSicPaths.for_test(tmp_path)
    _private_file(paths.sec_config, b"SEC_USER_AGENT=fixture contact@example.test\n")
    paths.campaign.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.capture.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    campaign = OfficialMembershipCampaignStore(
        paths.campaign, clock=lambda: datetime(2026, 8, 13, tzinfo=UTC)
    )
    capture = OfficialMembershipCaptureStore(paths.capture)
    calls = 0

    class FakeTransport:
        def fetch(self, identity, *, sec_user_agent):
            nonlocal calls
            calls += 1
            return LiveOutcomeV1(False, "TRANSPORT_FAILURE")

    try:
        module._execute_armed_for_test(
            paths=paths,
            campaign_store=campaign,
            capture_store=capture,
            transport=FakeTransport(),
        )
    except ValueError as error:
        assert str(error) == "sic_capture_failed:TRANSPORT_FAILURE"
    else:
        raise AssertionError("failed source accepted")
    assert calls == 1 and campaign.usage() == 1
    assert campaign.read(1).status == "FAILED" and capture.count() == 0


@pytest.mark.parametrize(
    ("error", "expected"),
    (
        (ValueError("sic_capture_failed:HTTP_403"), "HTTP_403"),
        (ValueError("sic_capture_failed:CONTENT_LENGTH"), "CONTENT_LENGTH"),
        (ValueError("HTTP_403"), "UNKNOWN"),
        (OSError("BODY_TOO_LARGE"), "UNKNOWN"),
        (ValueError("sic_capture_failed:HTTP_１２３"), "UNKNOWN"),
        (ValueError("sic_capture_failed:HTTP_999999"), "UNKNOWN"),
        (ValueError("sic_capture_failed:HTTP_403\nsecret"), "UNKNOWN"),
    ),
)
def test_sic_cli_failure_taxonomy_is_prefix_anchored_and_ascii_bounded(error, expected):
    root = Path(__file__).parents[1]
    spec = spec_from_file_location(
        "sic_failure_taxonomy", root / "tools/run_sec_sic_capture.py"
    )
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module._safe_capture_failure_code(error) == expected


def test_sic_main_contains_separately_armed_production_execution_graph():
    root = Path(__file__).parents[1]
    source = (root / "tools/run_sec_sic_capture.py").read_text()
    assert 'parser.add_argument("--armed"' in source
    main_body = source.split("def main", 1)[1]
    assert "_production_preflight()" in main_body
    assert "OfficialMembershipLiveTransport" in main_body
    assert "_execute_armed(" in main_body


# --- exact-cohort armed gate -------------------------------------------------

CANARY_TOOL = Path(__file__).parents[1] / "tools/run_quant_membership_canary.py"
_CAPTURED_AT = datetime(2026, 8, 13, 1, 20, tzinfo=UTC)
_PREFLIGHT_AT = datetime(2026, 8, 13, 1, 25, tzinfo=UTC)
_OBSERVED_AT = datetime(2026, 8, 13, 1, 26, tzinfo=UTC)
_EVALUATED_AT = datetime(2026, 8, 13, 1, 30, tzinfo=UTC)
_MARKET_DATE = date(2026, 8, 12)
_FIXTURE_AGENT = "fixture contact@example.test"


def _canary_module(name):
    spec = spec_from_file_location(name, CANARY_TOOL)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _official_snapshot():
    from tokmo.values.official_membership_observation import OfficialSicSnapshotV1

    return OfficialSicSnapshotV1(
        "sec-sic-html-v1", "OFFICIAL_CAPTURE", ("3571",), "sec-sic-official-capture-v1"
    )


def _armed_fixture(
    module,
    tmp_path,
    symbols,
    *,
    campaign_seed=0,
    sec_config=b"SEC_USER_AGENT=fixture contact@example.test\n",
):
    from tokmo.records.current_holdings_store import CurrentHoldingsStore
    from tokmo.records.official_membership_campaign_store import (
        OfficialMembershipCampaignStore,
    )
    from tokmo.records.official_membership_observation_store import (
        OfficialMembershipObservationStore,
    )
    from tokmo.values.current_holdings import (
        AccountHoldingsV1,
        AccountScopeIdentityV1,
        CurrentHoldingsV1,
        HoldingItemV1,
    )

    paths = module.FixedProductionPaths.for_test(tmp_path)
    _private_file(paths.sec_config, sec_config)
    scope = "toss-acct-" + "4" * 24
    holdings = CurrentHoldingsV1(
        1,
        (
            AccountHoldingsV1(
                scope,
                1,
                tuple(
                    HoldingItemV1(symbol, "1", "1", "USD", "US", None)
                    for symbol in symbols
                ),
            ),
        ),
        True,
        _CAPTURED_AT,
        {scope: AccountScopeIdentityV1(scope, "personal", 1, None)},
    )
    CurrentHoldingsStore(paths.holdings, clock=lambda: _CAPTURED_AT).replace(
        holdings, identity_fingerprints={scope: "5" * 64}
    )
    campaign = OfficialMembershipCampaignStore(
        paths.campaign, clock=lambda: _PREFLIGHT_AT
    )
    for _ in range(campaign_seed):
        campaign.reserve("SEC_SIC")
    observations = OfficialMembershipObservationStore(
        paths.observations, stamp_factory=lambda: _PREFLIGHT_AT
    )
    return paths, campaign, observations


def _fake_membership_transport(symbols, *, calls, failure_at=None):
    from tokmo.facts.official_membership_live import EndpointKindV1, LiveOutcomeV1

    listed = (
        "Symbol|Security Name|Market Category|Test Issue|Financial Status"
        "|Round Lot Size|ETF|NextShares\n"
        + "\n".join(f"{s}|{s} Common Stock|Q|N|N|100|N|N" for s in symbols[:2])
        + "\nFile Creation Time: 081320260125|||||||\n"
    ).encode()
    other = (
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size"
        "|Test Issue|NASDAQ Symbol\n"
        + "\n".join(f"{s}|{s} Common Stock|N|{s}|N|100|N|{s}" for s in symbols[2:])
        + "\nFile Creation Time: 081320260125|||||||\n"
    ).encode()
    registry = json.dumps(
        {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [
                [index + 1, s, s, "Nasdaq" if index < 2 else "NYSE"]
                for index, s in enumerate(symbols)
            ],
        }
    ).encode()
    bodies = {
        EndpointKindV1.NASDAQ_LISTED: listed,
        EndpointKindV1.OTHER_LISTED: other,
        EndpointKindV1.SEC_REGISTRY: registry,
    }

    class FakeTransport:
        def fetch(self, identity, *, sec_user_agent):
            calls.append((identity.endpoint_kind, identity.cik, sec_user_agent))
            if failure_at is not None and len(calls) == failure_at:
                return LiveOutcomeV1(False, "TRANSPORT_FAILURE")
            if identity.endpoint_kind is EndpointKindV1.SEC_SUBMISSIONS:
                index = int(identity.cik)
                symbol = symbols[index - 1]
                body = json.dumps(
                    {
                        "cik": index,
                        "sic": "3571",
                        "tickers": [symbol],
                        "exchanges": ["Nasdaq" if index < 3 else "NYSE"],
                    }
                ).encode()
            else:
                body = bodies[identity.endpoint_kind]
            return LiveOutcomeV1(True, "OK", body, _OBSERVED_AT, 200)

    return FakeTransport()


def test_canary_armed_fake_run_uses_exact_seven_starts_and_canonical_readback(tmp_path):
    from tokmo.facts.official_membership_live import EndpointKindV1

    module = _canary_module("canary_armed_fake")
    symbols = ("AAA", "BBB", "CCC", "DDD")
    paths, campaign, observations = _armed_fixture(
        module, tmp_path, symbols, campaign_seed=3
    )
    calls = []
    receipt, materials, proofs = module._execute_armed_for_test(
        paths=paths,
        now=_PREFLIGHT_AT,
        sic_snapshot=_official_snapshot(),
        campaign_store=campaign,
        observation_store=observations,
        transport=_fake_membership_transport(symbols, calls=calls),
        clock=lambda: _EVALUATED_AT,
    )
    assert [kind for kind, _, _ in calls] == [
        EndpointKindV1.NASDAQ_LISTED,
        EndpointKindV1.OTHER_LISTED,
        EndpointKindV1.SEC_REGISTRY,
        EndpointKindV1.SEC_SUBMISSIONS,
        EndpointKindV1.SEC_SUBMISSIONS,
        EndpointKindV1.SEC_SUBMISSIONS,
        EndpointKindV1.SEC_SUBMISSIONS,
    ]
    assert all(agent == _FIXTURE_AGENT for _, _, agent in calls)
    assert campaign.usage() == 10
    assert [campaign.read(ordinal).status for ordinal in range(4, 11)] == [
        "SUCCEEDED"
    ] * 7
    assert receipt.status == "SAVED"
    assert tuple(item.symbol for item in materials) == symbols
    assert len(proofs) == 4
    acquisition_id = module._fixed_acquisition_id(_MARKET_DATE)
    assert observations.read_acquisition(acquisition_id) is not None
    assert observations.read_receipt(acquisition_id) is not None
    assert len(observations.read_materials(acquisition_id)) == 4


def test_canary_armed_admits_last_whole_plan_and_lands_exactly_on_the_ceiling(tmp_path):
    from tokmo.records.official_membership_campaign_store import CAMPAIGN_CEILING

    module = _canary_module("canary_armed_last_plan")
    symbols = ("AAA", "BBB", "CCC", "DDD")
    paths, campaign, observations = _armed_fixture(
        module, tmp_path, symbols, campaign_seed=CAMPAIGN_CEILING - 7
    )
    assert campaign.usage() == 25
    calls = []
    receipt, materials, proofs = module._execute_armed_for_test(
        paths=paths,
        now=_PREFLIGHT_AT,
        sic_snapshot=_official_snapshot(),
        campaign_store=campaign,
        observation_store=observations,
        transport=_fake_membership_transport(symbols, calls=calls),
        clock=lambda: _EVALUATED_AT,
    )
    assert len(calls) == 7
    assert campaign.usage() == CAMPAIGN_CEILING == 32
    assert receipt.status == "SAVED"
    assert len(materials) == 4 and len(proofs) == 4
    with pytest.raises(ValueError) as ceiling:
        campaign.reserve("SEC_SUBMISSIONS")
    assert str(ceiling.value) == "campaign_ceiling"
    assert campaign.usage() == 32


def test_canary_consumed_starts_sanitizes_an_unreadable_durable_ledger():
    import sqlite3

    module = _canary_module("canary_ledger_unreadable")

    class RaisingLedger:
        def __init__(self, error):
            self._error = error

        def usage(self):
            raise self._error

    for error in (
        sqlite3.DatabaseError("file is not a database: /Users/private/ledger"),
        sqlite3.OperationalError("disk I/O error"),
        sqlite3.NotSupportedError("boom"),
        RuntimeError("unexpected"),
        OSError("EIO"),
        ValueError("private_path"),
    ):
        assert module._consumed_starts(RaisingLedger(error), 3) == 7


def test_canary_consumed_starts_reports_the_bounded_truthful_delta():
    module = _canary_module("canary_ledger_delta")

    class FixedLedger:
        def __init__(self, used):
            self._used = used

        def usage(self):
            return self._used

    assert module._consumed_starts(FixedLedger(3), 3) == 0
    assert module._consumed_starts(FixedLedger(4), 3) == 1
    assert module._consumed_starts(FixedLedger(10), 3) == 7
    assert module._consumed_starts(FixedLedger(2), 3) == 0
    assert module._consumed_starts(FixedLedger(99), 3) == 7


def test_canary_armed_inherits_cumulative_ledger_and_needs_whole_seven(tmp_path):
    module = _canary_module("canary_armed_capacity")
    symbols = ("AAA", "BBB", "CCC", "DDD")
    paths, campaign, observations = _armed_fixture(
        module, tmp_path, symbols, campaign_seed=26
    )
    calls = []
    with pytest.raises(ValueError) as failure:
        module._execute_armed_for_test(
            paths=paths,
            now=_PREFLIGHT_AT,
            sic_snapshot=_official_snapshot(),
            campaign_store=campaign,
            observation_store=observations,
            transport=_fake_membership_transport(symbols, calls=calls),
            clock=lambda: _EVALUATED_AT,
        )
    assert str(failure.value) == "campaign_capacity"
    assert calls == [] and campaign.usage() == 26


@pytest.mark.parametrize(
    ("symbols", "expected"),
    (
        (("AAA", "BBB", "CCC"), "cohort_size"),
        (("AAA", "BBB", "CCC", "DDD", "EEE"), "cohort_size"),
    ),
)
def test_canary_preflight_stops_with_zero_starts_when_cohort_is_not_four(
    tmp_path, symbols, expected
):
    module = _canary_module("canary_cohort_" + str(len(symbols)))
    paths, campaign, observations = _armed_fixture(module, tmp_path, symbols)
    calls = []
    with pytest.raises(ValueError) as failure:
        module._execute_armed_for_test(
            paths=paths,
            now=_PREFLIGHT_AT,
            sic_snapshot=_official_snapshot(),
            campaign_store=campaign,
            observation_store=observations,
            transport=_fake_membership_transport(
                ("AAA", "BBB", "CCC", "DDD"), calls=calls
            ),
            clock=lambda: _EVALUATED_AT,
        )
    assert str(failure.value) == expected
    assert calls == [] and campaign.usage() == 0
    assert (
        observations.read_acquisition(module._fixed_acquisition_id(_MARKET_DATE))
        is None
    )


def test_canary_preflight_stops_with_zero_starts_on_mutated_holdings_bytes(tmp_path):
    module = _canary_module("canary_holdings_mutation")
    symbols = ("AAA", "BBB", "CCC", "DDD")
    paths, campaign, observations = _armed_fixture(module, tmp_path, symbols)
    paths.holdings.write_bytes(paths.holdings.read_bytes() + b"\n")
    os.chmod(paths.holdings, 0o600)
    calls = []
    with pytest.raises(ValueError) as failure:
        module._execute_armed_for_test(
            paths=paths,
            now=_PREFLIGHT_AT,
            sic_snapshot=_official_snapshot(),
            campaign_store=campaign,
            observation_store=observations,
            transport=_fake_membership_transport(symbols, calls=calls),
            clock=lambda: _EVALUATED_AT,
        )
    assert str(failure.value) == "holdings"
    assert calls == [] and campaign.usage() == 0


def test_canary_preflight_stops_with_zero_starts_on_non_canonical_holdings(tmp_path):
    module = _canary_module("canary_holdings_canonical")
    symbols = ("AAA", "BBB", "CCC", "DDD")
    paths, campaign, observations = _armed_fixture(module, tmp_path, symbols)
    decoded = json.loads(paths.holdings.read_bytes())
    reserialized = json.dumps(decoded, indent=2).encode()
    assert reserialized != paths.holdings.read_bytes()
    paths.holdings.write_bytes(reserialized)
    os.chmod(paths.holdings, 0o600)
    calls = []
    with pytest.raises(ValueError) as failure:
        module._execute_armed_for_test(
            paths=paths,
            now=_PREFLIGHT_AT,
            sic_snapshot=_official_snapshot(),
            campaign_store=campaign,
            observation_store=observations,
            transport=_fake_membership_transport(symbols, calls=calls),
            clock=lambda: _EVALUATED_AT,
        )
    assert str(failure.value) == "holdings"
    assert module._safe_canary_failure_code(failure.value) == "HOLDINGS"
    assert calls == [] and campaign.usage() == 0


def test_canary_preflight_stops_with_zero_starts_on_acquisition_conflict(tmp_path):
    module = _canary_module("canary_conflict_gate")
    symbols = ("AAA", "BBB", "CCC", "DDD")
    paths, campaign, observations = _armed_fixture(module, tmp_path, symbols)
    calls = []
    module._execute_armed_for_test(
        paths=paths,
        now=_PREFLIGHT_AT,
        sic_snapshot=_official_snapshot(),
        campaign_store=campaign,
        observation_store=observations,
        transport=_fake_membership_transport(symbols, calls=calls),
        clock=lambda: _EVALUATED_AT,
    )
    assert campaign.usage() == 7
    with pytest.raises(ValueError) as failure:
        module._execute_armed_for_test(
            paths=paths,
            now=_PREFLIGHT_AT,
            sic_snapshot=_official_snapshot(),
            campaign_store=campaign,
            observation_store=observations,
            transport=_fake_membership_transport(symbols, calls=calls),
            clock=lambda: _EVALUATED_AT,
        )
    assert str(failure.value) == "acquisition_conflict"
    assert len(calls) == 7 and campaign.usage() == 7


def test_canary_preflight_stops_with_zero_starts_on_untyped_sic_resource(tmp_path):
    module = _canary_module("canary_sic_type_gate")
    symbols = ("AAA", "BBB", "CCC", "DDD")
    paths, campaign, observations = _armed_fixture(module, tmp_path, symbols)
    calls = []
    with pytest.raises(ValueError) as failure:
        module._execute_armed_for_test(
            paths=paths,
            now=_PREFLIGHT_AT,
            sic_snapshot=("3571",),
            campaign_store=campaign,
            observation_store=observations,
            transport=_fake_membership_transport(symbols, calls=calls),
            clock=lambda: _EVALUATED_AT,
        )
    assert str(failure.value) == "sic_resource"
    assert calls == [] and campaign.usage() == 0


def test_canary_preflight_rejects_typed_snapshot_that_is_not_official_capture(tmp_path):
    from tokmo.values.official_membership_observation import OfficialSicSnapshotV1

    module = _canary_module("canary_sic_class_gate")
    symbols = ("AAA", "BBB", "CCC", "DDD")
    paths, campaign, observations = _armed_fixture(module, tmp_path, symbols)
    snapshot = OfficialSicSnapshotV1(
        "sec-sic-html-v1", "RECONSTRUCTED_CONTRACT", ("3571",), "fixture-sec-sic"
    )
    assert isinstance(snapshot, OfficialSicSnapshotV1)
    assert snapshot.classification != module.OFFICIAL_CAPTURE_CLASSIFICATION
    calls = []
    with pytest.raises(ValueError) as failure:
        module._execute_armed_for_test(
            paths=paths,
            now=_PREFLIGHT_AT,
            sic_snapshot=snapshot,
            campaign_store=campaign,
            observation_store=observations,
            transport=_fake_membership_transport(symbols, calls=calls),
            clock=lambda: _EVALUATED_AT,
        )
    assert str(failure.value) == "sic_resource"
    assert module._safe_canary_failure_code(failure.value) == "SIC_RESOURCE"
    assert calls == [] and campaign.usage() == 0
    assert (
        observations.read_acquisition(module._fixed_acquisition_id(_MARKET_DATE))
        is None
    )


def test_canary_holdings_gate_precedes_any_credential_value_read(tmp_path):
    module = _canary_module("canary_credential_order")
    paths, campaign, observations = _armed_fixture(
        module, tmp_path, ("AAA", "BBB", "CCC"), sec_config=b"SEC_USER_AGENT=changeme\n"
    )
    calls = []
    with pytest.raises(ValueError) as failure:
        module._execute_armed_for_test(
            paths=paths,
            now=_PREFLIGHT_AT,
            sic_snapshot=_official_snapshot(),
            campaign_store=campaign,
            observation_store=observations,
            transport=_fake_membership_transport(
                ("AAA", "BBB", "CCC", "DDD"), calls=calls
            ),
            clock=lambda: _EVALUATED_AT,
        )
    assert str(failure.value) == "cohort_size"
    assert calls == [] and campaign.usage() == 0


def test_canary_source_failure_consumes_one_start_and_writes_zero_materials(tmp_path):
    module = _canary_module("canary_source_failure")
    symbols = ("AAA", "BBB", "CCC", "DDD")
    paths, campaign, observations = _armed_fixture(
        module, tmp_path, symbols, campaign_seed=3
    )
    calls = []
    with pytest.raises(ValueError) as failure:
        module._execute_armed_for_test(
            paths=paths,
            now=_PREFLIGHT_AT,
            sic_snapshot=_official_snapshot(),
            campaign_store=campaign,
            observation_store=observations,
            transport=_fake_membership_transport(symbols, calls=calls, failure_at=1),
            clock=lambda: _EVALUATED_AT,
        )
    assert str(failure.value) == "source_failure"
    assert len(calls) == 1 and campaign.usage() == 4
    assert campaign.read(4).status == "FAILED"
    assert module._consumed_starts(campaign, 3) == 1
    assert module._safe_canary_failure_code(failure.value) == "SOURCE_FAILURE"
    acquisition_id = module._fixed_acquisition_id(_MARKET_DATE)
    assert observations.read_materials(acquisition_id) == ()


@pytest.mark.parametrize(
    ("error", "expected"),
    (
        (ValueError("source_failure"), "SOURCE_FAILURE"),
        (ValueError("canary_parse"), "CANARY_PARSE"),
        (ValueError("canary_assembly"), "CANARY_ASSEMBLY"),
        (ValueError("canary_readback"), "CANARY_READBACK"),
        (ValueError("holdings_mutated"), "HOLDINGS_MUTATED"),
        (ValueError("cohort_size"), "COHORT_SIZE"),
        (ValueError("campaign_capacity"), "CAMPAIGN_CAPACITY"),
        (ValueError("acquisition_conflict"), "ACQUISITION_CONFLICT"),
        (ValueError("source_failure\nsecret"), "UNKNOWN"),
        (ValueError("source_failure:/Users/private"), "UNKNOWN"),
        (ValueError("SOURCE_FAILURE"), "UNKNOWN"),
        (ValueError("ｃohort_size"), "UNKNOWN"),
        (OSError("source_failure"), "UNKNOWN"),
        (RuntimeError("boom"), "UNKNOWN"),
    ),
)
def test_canary_failure_taxonomy_is_exact_match_and_ascii_bounded(error, expected):
    module = _canary_module("canary_taxonomy")
    assert module._safe_canary_failure_code(error) == expected


def test_canary_failure_output_is_one_sanitized_bounded_json_line(capsys):
    module = _canary_module("canary_failure_output")
    assert (
        module._failure(
            "CANARY_FAILED_SOURCE_FAILURE", campaign_starts=1, source_starts=1
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload == {
        "campaign_starts": 1,
        "code": "CANARY_FAILED_SOURCE_FAILURE",
        "source_starts": 1,
    }
    assert lines[0].isascii()


def test_canary_public_modes_preserve_default_refusal_and_separate_arming(
    monkeypatch, capsys
):
    module = _canary_module("canary_modes")

    def unreachable(*args, **kwargs):
        raise AssertionError("live provider constructed")

    monkeypatch.setattr(module, "OfficialMembershipLiveTransport", unreachable)
    monkeypatch.setattr(module, "_execute_armed", unreachable)
    assert module.main([]) in (0, 2)
    assert module.main(["--mode", "offline-preflight"]) in (0, 2)
    for argv in (
        ["--mode", "offline-preflight", "--armed"],
        ["--mode", "bounded-live-canary"],
    ):
        with pytest.raises(SystemExit) as exit_info:
            module.main(argv)
        assert exit_info.value.code == 2
    capsys.readouterr()

    def refuse(now):
        raise ValueError("private_path")

    monkeypatch.setattr(module, "_production_preflight", refuse)
    assert module.main(["--mode", "bounded-live-canary", "--armed"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err.strip()) == {
        "campaign_starts": 0,
        "code": "PREFLIGHT_REFUSED",
        "source_starts": 0,
    }


def _arm_hermetic_main(
    module, monkeypatch, tmp_path, symbols, *, failure_at=None, campaign_seed=3
):
    """Point the fixed production graph at a temporary owner-only layout."""
    import socket

    paths, campaign, _ = _armed_fixture(
        module, tmp_path, symbols, campaign_seed=campaign_seed
    )
    for directory in (tmp_path, paths.holdings.parent, paths.campaign.parent):
        os.chmod(directory, 0o700)
    monkeypatch.setattr(
        module.FixedProductionPaths,
        "production",
        classmethod(lambda cls: cls.for_test(tmp_path)),
    )
    monkeypatch.setattr(module, "load_production_sic_snapshot", _official_snapshot)
    monkeypatch.setattr(module, "_utc_now", lambda: _EVALUATED_AT)
    calls = []
    transport = _fake_membership_transport(symbols, calls=calls, failure_at=failure_at)
    monkeypatch.setattr(
        module, "OfficialMembershipLiveTransport", lambda *, clock: transport
    )

    def blocked(*args, **kwargs):
        raise AssertionError("socket opened by a hermetic canary test")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    return paths, campaign, calls


def _stored_materials(paths, acquisition_id):
    from tokmo.records.official_membership_observation_store import (
        OfficialMembershipObservationStore,
    )

    return OfficialMembershipObservationStore(paths.observations).read_materials(
        acquisition_id
    )


def test_canary_public_main_armed_success_is_silent_and_uses_exact_seven_starts(
    tmp_path, monkeypatch, capsys
):
    module = _canary_module("canary_main_armed_success")
    symbols = ("AAA", "BBB", "CCC", "DDD")
    paths, campaign, calls = _arm_hermetic_main(module, monkeypatch, tmp_path, symbols)
    assert campaign.usage() == 3
    exit_code = module.main(["--mode", "bounded-live-canary", "--armed"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "" and captured.err == ""
    assert len(calls) == 7
    assert campaign.usage() == 10
    assert [campaign.read(ordinal).status for ordinal in range(4, 11)] == [
        "SUCCEEDED"
    ] * 7
    acquisition_id = module._fixed_acquisition_id(_MARKET_DATE)
    materials = _stored_materials(paths, acquisition_id)
    assert tuple(item.symbol for item in materials) == symbols


def test_canary_public_main_armed_first_source_failure_stays_bounded_and_sanitized(
    tmp_path, monkeypatch, capsys
):
    module = _canary_module("canary_main_armed_failure")
    symbols = ("AAA", "BBB", "CCC", "DDD")
    paths, campaign, calls = _arm_hermetic_main(
        module, monkeypatch, tmp_path, symbols, failure_at=1
    )
    exit_code = module.main(["--mode", "bounded-live-canary", "--armed"])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 1 and lines[0].isascii()
    payload = json.loads(lines[0])
    assert payload["code"].startswith("CANARY_FAILED_")
    assert payload == {
        "campaign_starts": 1,
        "code": "CANARY_FAILED_SOURCE_FAILURE",
        "source_starts": 1,
    }
    assert len(calls) == 1
    assert campaign.usage() == 4
    assert campaign.read(4).status == "FAILED"
    acquisition_id = module._fixed_acquisition_id(_MARKET_DATE)
    assert _stored_materials(paths, acquisition_id) == ()
    for token in (
        "https://",
        "/Users/",
        "CIK",
        "sec.env",
        "nasdaq",
        str(tmp_path),
        *symbols,
    ):
        assert token not in captured.err


def test_canary_public_main_wires_only_the_fixed_production_graph():
    source = CANARY_TOOL.read_text()
    main_body = source.split("def main", 1)[1]
    assert '"--armed"' in main_body and 'action="store_true"' in main_body
    assert "_production_preflight(" in main_body
    assert "OfficialMembershipLiveTransport" in main_body
    assert "_execute_armed(" in main_body
    assert "_for_test" not in main_body
    armed_body = source.split("def _execute_armed(", 1)[1].split(
        "def _execute_armed_for_test", 1
    )[0]
    assert "_evaluate_preflight(" in armed_body
    assert "execute_official_membership_canary(" in armed_body


def test_canary_tool_has_no_consumer_recurring_or_delivery_reach():
    source = CANARY_TOOL.read_text()
    for forbidden in (
        "quant_multifactor",
        "assemble_quant_multifactor_input",
        "execute_holdings_route",
        "toss",
        "Toss",
        "telegram",
        "Telegram",
        "cron",
        "launchd",
        "schedule",
        "notify",
        "deliver",
        "report",
        "urlopen(",
        "requests",
        "--armed-recurring",
        "sleep",
    ):
        assert forbidden not in source
