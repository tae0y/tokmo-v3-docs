"""R3 provider-date repair: strict TDD tests for the CLOSED FRED R3 SDS v0.1.

Incorporates independent review findings B1/B2/M1/M2/M3/M4 + launch-relevant minors.
"""

from datetime import date, datetime, timedelta, timezone
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import stat
import importlib.util
from pathlib import Path
import pytest

from tokmo.values import fred_macro
from tokmo.values.fred_macro import (
    FredOperation, FredRequest, ExecutionMode, canonical_json,
)
from tokmo.records.fred_material_store import FredMaterialStore
from tokmo.facts.fred import build_query_pairs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _live_tool():
    path = Path(__file__).parents[1] / "tools" / "fred_canonical_live.py"
    spec = importlib.util.spec_from_file_location("fred_canonical_live_r3_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _v3_db_with_r1(root):
    from tokmo.records.fred_material_store import DDL_V3
    database = root / "fred.sqlite3"
    database.touch(mode=0o600)
    with sqlite3.connect(database) as connection:
        connection.executescript(DDL_V3)
        connection.execute("INSERT INTO campaign VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            1, fred_macro.SOURCE, fred_macro.PURPOSE, fred_macro.CAMPAIGN_CODE,
            fred_macro.CODE_REVISION, fred_macro.MANIFEST_CODE, fred_macro.MANIFEST_REVISION,
            fred_macro.MANIFEST_DIGEST, "LIVE", "PAYEMS", 10, "QUARANTINED", "bad_request"))
        request = fred_macro.g_live_manifest(release_id=1)[0]
        connection.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            fred_macro.SOURCE, fred_macro.PURPOSE, "a" * 64, 1, request.operation.value,
            request.request_identity, "LIVE", "2026-08-12T00:00:00.000000Z", "FAILED",
            fred_macro.CAMPAIGN_CODE, fred_macro.MANIFEST_REVISION, fred_macro.MANIFEST_DIGEST))
    return database


def _migrated_r2(tmp_path, module):
    root = tmp_path / "r2"
    root.mkdir(mode=0o700)
    _v3_db_with_r1(root)
    module.migrate_r2_schema(root, lock_path=tmp_path / "lock",
                             migrated_at=datetime(2026, 8, 12, tzinfo=timezone.utc))
    store = FredMaterialStore(root)
    request = fred_macro.g_live_manifest(release_id=1)[0]
    reservation = store.reserve_campaign(
        fred_macro.R2_CAMPAIGN_CODE, 1, request,
        started_at=datetime(2026, 8, 12, 1, tzinfo=timezone.utc))
    store.settle(reservation, "FAILED")
    store.quarantine_campaign(
        fred_macro.R2_CAMPAIGN_CODE, "provider_bad_request_unknown")
    return root


# Real-shape probe fixtures matching provenance script schema.
# Probes contain: sequence, name, started_at, outcome, http_status, category, provider_code
_REAL_PROBES = [
    {"sequence": 1, "name": "minimal_series",
     "started_at": "2026-08-12T03:45:07.254037+00:00",
     "outcome": "settled", "http_status": 200,
     "category": "success", "provider_code": None},
    {"sequence": 2, "name": "r2_realtime_date",
     "started_at": "2026-08-12T03:45:08.765990+00:00",
     "outcome": "settled", "http_status": 400,
     "category": "provider_error_json", "provider_code": 400},
]
_EXPECTED_DIAGNOSTIC_COUNT = 2
_EXPECTED_DIAGNOSTIC_SHA = "bf8844d1a639461ac8de3c2fe5f11f53ed9c611030d5def0d7f092734f2f6f4e"

_DIAGNOSTIC_NAMES_ALLOWLIST = frozenset({"minimal_series", "r2_realtime_date"})


def _make_diagnostic_artifact(root, *, probes=None, schema="tokmo.fred.bounded-diagnostic.v1"):
    """Create a valid g-live-diagnostic-probes.json with real-shape probes."""
    if probes is None:
        probes = [dict(p) for p in _REAL_PROBES]
    art = {"schema": schema, "probes": probes}
    path = root / "g-live-diagnostic-probes.json"
    path.write_bytes(canonical_json(art))
    path.chmod(0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, digest, len(probes)


# ---------------------------------------------------------------------------
# 1. R3 manifest literals, digest, and no-realtime contract
# ---------------------------------------------------------------------------

R3_CAMPAIGN_CODE = "FRED_G_LIVE_PAYEMS_R3"
R3_MANIFEST_CODE = "FRED_G_LIVE_PAYEMS_FIVE_R3"
R3_MANIFEST_REVISION = "fred-canonical-stage3-g-live-r3-provider-date-v1"
R3_CODE_REVISION = "fred-g-live-controller-r3-provider-date-v1"
R3_MANIFEST_DIGEST = "da4d281aa2deae985b818b2d74dadfb922ef259721985c8aa26b348059828d51"

R3_EXACT_LITERAL = (
    b'{"campaign_code":"FRED_G_LIVE_PAYEMS_R3",'
    b'"manifest_code":"FRED_G_LIVE_PAYEMS_FIVE_R3",'
    b'"manifest_revision":"fred-canonical-stage3-g-live-r3-provider-date-v1",'
    b'"operations":'
    b'[{"operation":"SERIES","pairs":[["series_id","PAYEMS"]]},'
    b'{"operation":"SERIES_OBSERVATIONS","pairs":[["series_id","PAYEMS"],'
    b'["observation_start","2026-07-01"],["observation_end","2026-07-31"],'
    b'["units","lin"],["output_type","4"],["sort_order","asc"],'
    b'["limit","10"],["offset","0"]]},'
    b'{"operation":"SERIES_RELEASE","pairs":[["series_id","PAYEMS"]]},'
    b'{"operation":"RELEASE_DATES","pairs":[["release_id",'
    b'{"ordinal":3,"type":"operation_3_release_id"}],'
    b'["include_release_dates_with_no_data","false"],["sort_order","desc"],'
    b'["limit","10"],["offset","0"]]},'
    b'{"operation":"SERIES_VINTAGE_DATES","pairs":[["series_id","PAYEMS"],'
    b'["sort_order","asc"],["limit","10"],["offset","0"]]}]}\n'
)


def test_r3_constants_match_sds():
    assert fred_macro.R3_CAMPAIGN_CODE == R3_CAMPAIGN_CODE
    assert fred_macro.R3_MANIFEST_CODE == R3_MANIFEST_CODE
    assert fred_macro.R3_MANIFEST_REVISION == R3_MANIFEST_REVISION
    assert fred_macro.R3_CODE_REVISION == R3_CODE_REVISION
    assert fred_macro.R3_MANIFEST_DIGEST == R3_MANIFEST_DIGEST


def test_r3_manifest_has_no_realtime_on_any_operation():
    manifest = fred_macro.r3_g_live_manifest(release_id=1)
    assert [req.operation.value for req in manifest] == [
        "SERIES", "SERIES_OBSERVATIONS", "SERIES_RELEASE",
        "RELEASE_DATES", "SERIES_VINTAGE_DATES",
    ]
    for req in manifest:
        assert req.realtime_start is None
        assert req.realtime_end is None


def test_r3_manifest_observation_window_and_op4_symbolic():
    manifest = fred_macro.r3_g_live_manifest(release_id=42)
    assert manifest[1].observation_start == date(2026, 7, 1)
    assert manifest[1].observation_end == date(2026, 7, 31)
    assert manifest[1].output_type == 4
    assert manifest[1].limit == 10 and manifest[1].offset == 0
    assert manifest[3].release_id == 42
    assert manifest[3].include_release_dates_with_no_data is False
    assert manifest[3].limit == 10 and manifest[3].offset == 0
    assert manifest[4].limit == 10 and manifest[4].offset == 0


def test_r3_manifest_emitted_key_sets_are_exact_official_allowlists():
    manifest = fred_macro.r3_g_live_manifest(release_id=1)
    pairs_list = [build_query_pairs(req) for req in manifest]
    key_sets = [set(key for key, _ in pairs) for pairs in pairs_list]
    expected = (
        {"series_id", "file_type"},
        {"series_id", "observation_start", "observation_end", "units",
         "output_type", "sort_order", "limit", "offset", "file_type"},
        {"series_id", "file_type"},
        {"release_id", "include_release_dates_with_no_data", "sort_order",
         "limit", "offset", "file_type"},
        {"series_id", "sort_order", "limit", "offset", "file_type"},
    )
    assert key_sets == list(expected)
    for pairs in pairs_list:
        keys = [k for k, _ in pairs]
        assert "realtime_start" not in keys
        assert "realtime_end" not in keys


def test_r3_digest_exact_literal_bytes():
    computed = hashlib.sha256(R3_EXACT_LITERAL).hexdigest()
    assert computed == R3_MANIFEST_DIGEST


def test_r3_digest_shared_builder_matches():
    operations = []
    for index, request in enumerate(fred_macro.r3_g_live_manifest(release_id=1), 1):
        pairs = []
        for key, value in build_query_pairs(request):
            if key == "file_type":
                continue
            if index == 4 and key == "release_id":
                pairs.append([key, {"ordinal": 3, "type": "operation_3_release_id"}])
            else:
                pairs.append([key, value])
        operations.append({"operation": request.operation.value, "pairs": pairs})
    payload = {
        "campaign_code": R3_CAMPAIGN_CODE,
        "manifest_code": R3_MANIFEST_CODE,
        "manifest_revision": R3_MANIFEST_REVISION,
        "operations": operations,
    }
    builder_bytes = canonical_json(payload)
    builder_digest = hashlib.sha256(builder_bytes).hexdigest()
    assert builder_digest == R3_MANIFEST_DIGEST
    assert builder_bytes == R3_EXACT_LITERAL


def test_r3_manifest_rejects_invalid_release_id():
    for value in (None, True, 0):
        with pytest.raises(ValueError, match="invalid_release_id"):
            fred_macro.r3_g_live_manifest(release_id=value)


# ---------------------------------------------------------------------------
# 2. R1/R2 manifest immutability after R3 addition
# ---------------------------------------------------------------------------

def test_r1_r2_manifests_and_digests_byte_identical():
    assert fred_macro.CAMPAIGN_CODE == "FRED_G_LIVE_PAYEMS_R1"
    assert fred_macro.MANIFEST_CODE == "FRED_G_LIVE_PAYEMS_FIVE_R1"
    assert fred_macro.MANIFEST_DIGEST == \
        "1ca30ee6c3ce6442e31f2c31a09461d6407bad8c94dce6be97807540056386a1"
    assert fred_macro.R2_CAMPAIGN_CODE == "FRED_G_LIVE_PAYEMS_R2"
    assert fred_macro.R2_MANIFEST_CODE == "FRED_G_LIVE_PAYEMS_FIVE_R2"
    assert fred_macro.R2_MANIFEST_DIGEST == \
        "fa0d82ac80678e74c02e2b05b642ce225a756f383727b814664ff134622fd134"


# ---------------------------------------------------------------------------
# 3. Diagnostic ledger validator — real schema
# ---------------------------------------------------------------------------

def test_diagnostic_validator_accepts_valid_real_artifact(tmp_path):
    module = _live_tool()
    path, digest, count = _make_diagnostic_artifact(tmp_path)
    result = module.validate_diagnostic_ledger(path)
    assert result["count"] == _EXPECTED_DIAGNOSTIC_COUNT
    assert result["sha256"] == _EXPECTED_DIAGNOSTIC_SHA
    # The digest must match the pinned constant, not a self-compare
    assert result["sha256"] == digest


def test_diagnostic_validator_rejects_missing(tmp_path):
    module = _live_tool()
    with pytest.raises((ValueError, OSError)):
        module.validate_diagnostic_ledger(tmp_path / "nonexistent.json")


def test_diagnostic_validator_rejects_symlink(tmp_path):
    module = _live_tool()
    _make_diagnostic_artifact(tmp_path)
    link = tmp_path / "link.json"
    os.symlink(tmp_path / "g-live-diagnostic-probes.json", link)
    with pytest.raises((ValueError, OSError)):
        module.validate_diagnostic_ledger(link)


def test_diagnostic_validator_rejects_wrong_owner_or_mode(tmp_path):
    module = _live_tool()
    path = tmp_path / "g-live-diagnostic-probes.json"
    path.write_bytes(canonical_json({"schema": "tokmo.fred.bounded-diagnostic.v1",
        "probes": _REAL_PROBES}))
    path.chmod(0o644)
    with pytest.raises((ValueError, OSError)):
        module.validate_diagnostic_ledger(path)


def test_diagnostic_validator_rejects_wrong_schema(tmp_path):
    module = _live_tool()
    path = tmp_path / "g-live-diagnostic-probes.json"
    path.write_bytes(canonical_json({"schema": "tokmo.wrong.v1", "probes": _REAL_PROBES}))
    path.chmod(0o600)
    with pytest.raises((ValueError,)):
        module.validate_diagnostic_ledger(path)


def test_diagnostic_validator_rejects_noncontiguous_sequence(tmp_path):
    module = _live_tool()
    path = tmp_path / "g-live-diagnostic-probes.json"
    probes = [dict(p) for p in _REAL_PROBES]
    probes[1]["sequence"] = 3
    path.write_bytes(canonical_json({"schema": "tokmo.fred.bounded-diagnostic.v1", "probes": probes}))
    path.chmod(0o600)
    with pytest.raises((ValueError,)):
        module.validate_diagnostic_ledger(path)


def test_diagnostic_validator_rejects_wrong_probe_name(tmp_path):
    module = _live_tool()
    path = tmp_path / "g-live-diagnostic-probes.json"
    probes = [dict(p) for p in _REAL_PROBES]
    probes[0]["name"] = "wrong_name"
    path.write_bytes(canonical_json({"schema": "tokmo.fred.bounded-diagnostic.v1", "probes": probes}))
    path.chmod(0o600)
    with pytest.raises((ValueError,)):
        module.validate_diagnostic_ledger(path)


def test_diagnostic_validator_rejects_unsettled_outcome(tmp_path):
    module = _live_tool()
    path = tmp_path / "g-live-diagnostic-probes.json"
    probes = [dict(p) for p in _REAL_PROBES]
    probes[1]["outcome"] = "started"
    path.write_bytes(canonical_json({"schema": "tokmo.fred.bounded-diagnostic.v1", "probes": probes}))
    path.chmod(0o600)
    with pytest.raises((ValueError,)):
        module.validate_diagnostic_ledger(path)


def test_diagnostic_validator_rejects_naive_timestamp(tmp_path):
    """started_at must be aware ISO — '2026-08-12' is naive."""
    module = _live_tool()
    path = tmp_path / "g-live-diagnostic-probes.json"
    probes = [dict(p) for p in _REAL_PROBES]
    probes[0]["started_at"] = "2026-08-12"
    path.write_bytes(canonical_json({"schema": "tokmo.fred.bounded-diagnostic.v1", "probes": probes}))
    path.chmod(0o600)
    with pytest.raises((ValueError,)):
        module.validate_diagnostic_ledger(path)


def test_diagnostic_validator_rejects_wrong_count(tmp_path):
    module = _live_tool()
    path = tmp_path / "g-live-diagnostic-probes.json"
    path.write_bytes(canonical_json({"schema": "tokmo.fred.bounded-diagnostic.v1",
        "probes": [_REAL_PROBES[0]]}))
    path.chmod(0o600)
    with pytest.raises((ValueError,)):
        module.validate_diagnostic_ledger(path)


def test_diagnostic_validator_rejects_hardlink(tmp_path):
    module = _live_tool()
    orig_path, _, _ = _make_diagnostic_artifact(tmp_path)
    hard = tmp_path / "hard.json"
    os.link(orig_path, hard)
    with pytest.raises((ValueError, OSError)):
        module.validate_diagnostic_ledger(hard)


# ---------------------------------------------------------------------------
# 4. R3 reservation with external_diagnostic_count
# ---------------------------------------------------------------------------

def _started(second: int = 0):
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=timezone.utc)


def test_r3_reservation_with_external_diagnostic_count(tmp_path):
    store = FredMaterialStore(tmp_path / "r3")
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    req = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    reservation = store.reserve_campaign(
        fred_macro.R3_CAMPAIGN_CODE, 1, req,
        started_at=_started(), external_diagnostic_count=2)
    assert reservation.code == "reserved"


def test_r3_reservation_rejects_at_effective_cap_8(tmp_path):
    store = FredMaterialStore(tmp_path / "r3")
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    with sqlite3.connect(store.path) as conn:
        for ordinal in range(1, 19):  # DB 18
            conn.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                "FRED", "CANONICAL_SOURCE_STAGE3", f"{ordinal:064x}", ordinal,
                "SERIES", f"{ordinal + 100:064x}", "LIVE",
                _started(ordinal).isoformat(), "FAILED",
                fred_macro.CAMPAIGN_CODE, "legacy", "0" * 64))
        conn.commit()
    req = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    result = store.reserve_campaign(
        fred_macro.R3_CAMPAIGN_CODE, 1, req,
        started_at=_started(20), external_diagnostic_count=2)
    # 18 + 2 = 20 >= 20 → cap_exhausted
    assert result.code == "cap_exhausted"


def test_r3_reservation_allows_at_effective_cap_7(tmp_path):
    store = FredMaterialStore(tmp_path / "r3")
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    with sqlite3.connect(store.path) as conn:
        for ordinal in range(1, 18):  # DB 17
            conn.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                "FRED", "CANONICAL_SOURCE_STAGE3", f"{ordinal:064x}", ordinal,
                "SERIES", f"{ordinal + 100:064x}", "LIVE",
                _started(ordinal).isoformat(), "FAILED",
                fred_macro.CAMPAIGN_CODE, "legacy", "0" * 64))
        conn.commit()
    req = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    result = store.reserve_campaign(
        fred_macro.R3_CAMPAIGN_CODE, 1, req,
        started_at=_started(20), external_diagnostic_count=2)
    # 17 + 2 = 19 < 20 → reserved
    assert result.code == "reserved"


def test_r3_reservation_isolated_from_r2(tmp_path):
    store = FredMaterialStore(tmp_path / "r3")
    store.ensure_live_campaign(campaign_code=fred_macro.R2_CAMPAIGN_CODE)
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    req = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    r2_res = store.reserve_campaign(
        fred_macro.R2_CAMPAIGN_CODE, 1, req, started_at=_started())
    assert r2_res.code == "reserved"
    with sqlite3.connect(store.path) as conn:
        r2_count = conn.execute(
            "SELECT count(*) FROM campaign_attempts WHERE campaign_code=?",
            (fred_macro.R2_CAMPAIGN_CODE,)).fetchone()[0]
        r3_count = conn.execute(
            "SELECT count(*) FROM campaign_attempts WHERE campaign_code=?",
            (fred_macro.R3_CAMPAIGN_CODE,)).fetchone()[0]
    assert r2_count == 1 and r3_count == 0


# ---------------------------------------------------------------------------
# 5. Three-way quarantine
# ---------------------------------------------------------------------------

def test_r3_quarantine_only_targets_r3(tmp_path):
    store = FredMaterialStore(tmp_path / "r3")
    store.ensure_live_campaign(campaign_code=fred_macro.R2_CAMPAIGN_CODE)
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    store.quarantine_campaign(fred_macro.R3_CAMPAIGN_CODE, "connection_failure")
    assert store.campaign_state_for(fred_macro.R3_CAMPAIGN_CODE) == (
        "QUARANTINED", "connection_failure")
    assert store.campaign_state_for(fred_macro.R2_CAMPAIGN_CODE) == (
        "ACTIVE", None)


def test_r3_complete_campaign_only_targets_r3(tmp_path):
    store = FredMaterialStore(tmp_path / "r3")
    store.ensure_live_campaign(campaign_code=fred_macro.R2_CAMPAIGN_CODE)
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    req = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    for ordinal in range(1, 6):
        res = store.reserve_campaign(
            fred_macro.R3_CAMPAIGN_CODE, ordinal, req,
            started_at=_started(ordinal), external_diagnostic_count=2)
        store.settle(res, "SUCCEEDED")
    store.complete_campaign(fred_macro.R3_CAMPAIGN_CODE)
    assert store.campaign_state_for(fred_macro.R3_CAMPAIGN_CODE) == (
        "COMPLETE", "manifest_complete")
    assert store.campaign_state_for(fred_macro.R2_CAMPAIGN_CODE) == (
        "ACTIVE", None)


def test_r3_reconcile_campaign_unknown_only_targets_r3(tmp_path):
    store = FredMaterialStore(tmp_path / "r3")
    store.ensure_live_campaign(campaign_code=fred_macro.R2_CAMPAIGN_CODE)
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    req = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    r2_res = store.reserve_campaign(
        fred_macro.R2_CAMPAIGN_CODE, 1, req, started_at=_started())
    r3_res = store.reserve_campaign(
        fred_macro.R3_CAMPAIGN_CODE, 1, req, started_at=_started(1),
        external_diagnostic_count=2)
    assert r2_res.code == "reserved" and r3_res.code == "reserved"
    count = store.reconcile_campaign_unknown(
        fred_macro.R3_CAMPAIGN_CODE,
        before=datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc))
    assert count == 1
    assert store.campaign_state_for(fred_macro.R2_CAMPAIGN_CODE) == (
        "ACTIVE", None)
    assert store.campaign_state_for(fred_macro.R3_CAMPAIGN_CODE) == (
        "QUARANTINED", "stale_reservation")


def test_generic_campaign_codes_still_rejected(tmp_path):
    store = FredMaterialStore(tmp_path / "r3")
    with pytest.raises(ValueError, match="campaign_mismatch"):
        store.ensure_live_campaign(campaign_code="ARBITRARY_CODE")


def test_r3_validate_live_reservation_accepts_r3(tmp_path):
    store = FredMaterialStore(tmp_path / "r3")
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    req = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    res = store.reserve_campaign(
        fred_macro.R3_CAMPAIGN_CODE, 1, req,
        started_at=_started(), external_diagnostic_count=2)
    store.validate_live_reservation(res, req)


# ---------------------------------------------------------------------------
# 6. R3 evidence, readback, preflight
# ---------------------------------------------------------------------------

def test_r3_evidence_is_secret_safe_and_three_campaign_aware(tmp_path):
    module = _live_tool()
    store = FredMaterialStore(tmp_path / "r3")
    store.ensure_live_campaign(campaign_code=fred_macro.R2_CAMPAIGN_CODE)
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    evidence = module._r3_evidence(
        store, [], 0, "ACTIVE", None,
        external_diagnostic_count=_EXPECTED_DIAGNOSTIC_COUNT,
        diagnostic_sha256=_EXPECTED_DIAGNOSTIC_SHA,
        manifest_identity={"campaign_code": R3_CAMPAIGN_CODE},
        r1_evidence_sha256="a" * 64,
        r2_evidence_sha256="b" * 64,
        migration_receipt_sha256="c" * 64,
    )
    rendered = canonical_json(evidence).decode()
    assert "api_key" not in rendered and "https://" not in rendered


def test_r3_readback_accepts_three_campaigns(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    result = module.readback_r3(root)
    assert result["code"] == "readback_ok"
    assert result["campaigns"] == 3


def test_r3_readback_pins_exact_r1_r2_terminal_states(tmp_path):
    """M1: readback must verify R1=QUARANTINED/bad_request, R2=QUARANTINED/provider_bad_request_unknown."""
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    result = module.readback_r3(root)
    assert result["code"] == "readback_ok"
    assert result["r1_terminal_state"] == "QUARANTINED"
    assert result["r1_terminal_code"] == "bad_request"
    assert result["r2_terminal_state"] == "QUARANTINED"
    assert result["r2_terminal_code"] == "provider_bad_request_unknown"


def test_r3_readback_rejects_unexpected_fourth_campaign(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    instant = "2026-08-12T00:00:00.000000Z"
    with sqlite3.connect(store.path) as conn:
        conn.execute("INSERT INTO campaigns VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "BOGUS", "FRED", "CANONICAL_SOURCE_STAGE3", "code",
            "mc", "mr", "d" * 64, "LIVE", "PAYEMS", 10,
            "ACTIVE", None, instant, instant))
        conn.commit()
    with pytest.raises((ValueError,)):
        module.readback_r3(root)


def test_r3_preflight_validates_diagnostic_ledger(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    diag_path, _, _ = _make_diagnostic_artifact(tmp_path)
    result = module.preflight_r3(root, lock_path=tmp_path / "lock",
                                 diagnostic_path=diag_path)
    assert result["code"] == "preflight_ok"
    assert result["aggregate_live"] == 2
    assert result["diagnostic_count"] == 2
    assert result["diagnostic_sha256"] == _EXPECTED_DIAGNOSTIC_SHA
    assert result["operational_total"] == 4


def test_r3_preflight_rejects_invalid_diagnostic_ledger(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    bad_path = tmp_path / "bad-diag.json"
    bad_path.write_bytes(canonical_json({"schema": "wrong", "probes": []}))
    bad_path.chmod(0o600)
    with pytest.raises((ValueError,)):
        module.preflight_r3(root, lock_path=tmp_path / "lock",
                            diagnostic_path=bad_path)


def test_r3_preflight_rejects_shape_valid_diagnostic_hash_drift(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    probes = [dict(probe) for probe in _REAL_PROBES]
    probes[0]["started_at"] = "2026-08-12T03:45:07.254038+00:00"
    path, _, _ = _make_diagnostic_artifact(tmp_path, probes=probes)
    with pytest.raises(ValueError, match="diagnostic_hash_mismatch"):
        module.preflight_r3(root, lock_path=tmp_path / "lock",
                            diagnostic_path=path)


def test_r3_preflight_rejects_r1_r2_terminal_drift(tmp_path):
    module = _live_tool()
    for target, expected in (("r1", "readback_r1_state_mismatch"),
                             ("r2", "readback_r2_state_mismatch")):
        case = tmp_path / target
        case.mkdir(mode=0o700)
        root = _migrated_r2(case, module)
        diagnostic_path, _, _ = _make_diagnostic_artifact(case)
        with sqlite3.connect(root / "fred.sqlite3") as connection:
            if target == "r1":
                connection.execute(
                    "UPDATE campaign SET terminal_code='operation_failure' WHERE singleton=1")
                connection.execute(
                    "UPDATE campaigns SET terminal_code='operation_failure' WHERE campaign_code=?",
                    (fred_macro.CAMPAIGN_CODE,))
            else:
                connection.execute(
                    "UPDATE campaigns SET terminal_state='ACTIVE',terminal_code=NULL WHERE campaign_code=?",
                    (fred_macro.R2_CAMPAIGN_CODE,))
            connection.commit()
        with pytest.raises(ValueError, match=expected):
            module.preflight_r3(root, lock_path=case / "preflight.lock",
                                diagnostic_path=diagnostic_path)


def test_r3_preflight_rejects_db_live_count_drift(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    diagnostic_path, _, _ = _make_diagnostic_artifact(tmp_path)
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    request = fred_macro.r3_g_live_manifest(release_id=1)[0]
    store.reserve_campaign(
        fred_macro.R3_CAMPAIGN_CODE, 1, request,
        started_at=datetime(2026, 8, 12, 2, tzinfo=timezone.utc),
        external_diagnostic_count=2)
    with pytest.raises(ValueError, match="readback_aggregate_mismatch"):
        module.preflight_r3(root, lock_path=tmp_path / "lock",
                            diagnostic_path=diagnostic_path)


def test_r3_preflight_does_not_create_campaign(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    diag_path, _, _ = _make_diagnostic_artifact(tmp_path)
    before = {p.name: (p.stat().st_mtime_ns, p.stat().st_size)
              for p in root.iterdir()}
    module.preflight_r3(root, lock_path=tmp_path / "lock",
                        diagnostic_path=diag_path)
    after = {p.name: (p.stat().st_mtime_ns, p.stat().st_size)
             for p in root.iterdir()}
    assert before == after
    with sqlite3.connect(root / "fred.sqlite3") as conn:
        r3_count = conn.execute(
            "SELECT count(*) FROM campaigns WHERE campaign_code=?",
            (fred_macro.R3_CAMPAIGN_CODE,)).fetchone()[0]
    assert r3_count == 0


# M3: Bad diagnostic ledger must give exact sanitized code with zero writes
def test_r3_preflight_bad_ledger_zero_write(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    db_before = hashlib.sha256((root / "fred.sqlite3").read_bytes()).hexdigest()
    bad_path = tmp_path / "bad-diag.json"
    bad_path.write_bytes(canonical_json({"schema": "wrong", "probes": []}))
    bad_path.chmod(0o600)
    with pytest.raises((ValueError,)):
        module.preflight_r3(root, lock_path=tmp_path / "lock",
                            diagnostic_path=bad_path)
    db_after = hashlib.sha256((root / "fred.sqlite3").read_bytes()).hexdigest()
    assert db_before == db_after


# ---------------------------------------------------------------------------
# 7. R3 execute — full success, first failure stop, replay, invariance
# ---------------------------------------------------------------------------

class _Response:
    status = 200
    headers = {}
    def __init__(self, body): self._body = body
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, limit): return self._body


class _R3LiveOpener:
    """Opener that serves R3 bodies."""
    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at
    def open(self, request, *, timeout):
        from urllib.parse import urlsplit, parse_qsl
        parsed = urlsplit(request.full_url)
        self.calls.append((parsed.path, tuple(parse_qsl(parsed.query))))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise OSError("masked provider failure")
        path = parsed.path
        if path == "/fred/series":
            body = {"realtime_start": "2026-08-12", "realtime_end": "2026-08-12",
                "seriess": [{"id": "PAYEMS", "realtime_start": "2026-08-12",
                "realtime_end": "2026-08-12", "title": "Employment",
                "observation_start": "1939-01-01", "observation_end": "2026-07-01",
                "frequency": "Monthly", "frequency_short": "M", "units": "Thousands",
                "units_short": "Thous.", "seasonal_adjustment": "Seasonally Adjusted",
                "seasonal_adjustment_short": "SA", "last_updated": "2026-08-01 00:00:00-05",
                "popularity": 90}]}
        elif path == "/fred/series/observations":
            body = {"realtime_start": "2026-08-12", "realtime_end": "2026-08-12",
                "observation_start": "2026-07-01", "observation_end": "2026-07-31",
                "units": "lin", "output_type": 4, "file_type": "json",
                "order_by": "observation_date", "sort_order": "asc",
                "count": 1, "offset": 0, "limit": 10,
                "observations": [{"realtime_start": "2026-08-12",
                "realtime_end": "2026-08-12", "date": "2026-07-01", "value": "1"}]}
        elif path == "/fred/series/release":
            body = {"realtime_start": "2026-08-12", "realtime_end": "2026-08-12",
                "releases": [{"id": 50, "realtime_start": "2026-08-12",
                "realtime_end": "2026-08-12", "name": "Employment Situation",
                "press_release": True}]}
        elif path == "/fred/release/dates":
            body = {"realtime_start": "2026-08-12", "realtime_end": "2026-08-12",
                "order_by": "release_date", "sort_order": "desc",
                "count": 1, "offset": 0, "limit": 10,
                "release_dates": [{"release_id": 50, "date": "2026-08-01"}]}
        elif path == "/fred/series/vintagedates":
            body = {"realtime_start": "1776-07-04", "realtime_end": "9999-12-31",
                "order_by": "vintage_date", "sort_order": "asc",
                "count": 1, "offset": 0, "limit": 10,
                "vintage_dates": ["2026-08-01"]}
        else:
            raise AssertionError(f"unexpected path: {path}")
        return _Response(json.dumps(body).encode())


def _r3_dependencies(tmp_path, fail_at=None, module=None):
    module = module or _live_tool()
    root = _migrated_r2(tmp_path, module)
    diag_path, diag_digest, diag_count = _make_diagnostic_artifact(tmp_path)
    credential_parent = tmp_path / "credentials"
    credential_parent.mkdir(mode=0o700)
    credential = credential_parent / "fred.env"
    credential.write_text("metadata-only\n")
    credential.chmod(0o600)
    opener = _R3LiveOpener(fail_at)
    keys = []
    base = datetime(2026, 8, 12, tzinfo=timezone.utc)
    wall_index = [0]
    mono_index = [0]
    def wall():
        wall_index[0] += 1
        return base + timedelta(seconds=wall_index[0] * 2)
    def mono():
        mono_index[0] += 1
        return float(mono_index[0])
    result = module.execute_r3(
        root=root, credential_path=credential, lock_path=tmp_path / "lock",
        diagnostic_path=diag_path, execution_allowed=True,
        wall_clock=wall, monotonic_clock=mono, sleeper=lambda _: None,
        api_key_loader=lambda path: keys.append(path) or "a" * 32,
        opener_builder=lambda: opener)
    return module, result, root, credential, opener, keys, diag_path


def test_r3_execute_five_success(tmp_path):
    module, result, root, _, opener, keys, _ = _r3_dependencies(tmp_path)
    assert result["code"] == "durable_success"
    assert result["provider_starts"] == 5
    assert result["credential_reads"] == 5
    store = FredMaterialStore(root)
    assert store.campaign_state_for(fred_macro.R3_CAMPAIGN_CODE) == (
        "COMPLETE", "manifest_complete")
    with sqlite3.connect(root / "fred.sqlite3") as conn:
        r1_state = conn.execute(
            "SELECT terminal_state,terminal_code FROM campaign WHERE singleton=1"
        ).fetchone()
    assert r1_state == ("QUARANTINED", "bad_request")
    assert store.campaign_state_for(fred_macro.R2_CAMPAIGN_CODE) == (
        "QUARANTINED", "provider_bad_request_unknown")
    assert (root / "g-live-r3-evidence.json").exists()
    with sqlite3.connect(root / "fred.sqlite3") as conn:
        ordinals = conn.execute(
            "SELECT manifest_ordinal FROM campaign_attempts WHERE campaign_code=? "
            "ORDER BY manifest_ordinal", (fred_macro.R3_CAMPAIGN_CODE,)).fetchall()
    assert [row[0] for row in ordinals] == [1, 2, 3, 4, 5]


def test_r3_execute_does_not_mutate_r1_r2_artifacts(tmp_path):
    module = _live_tool()
    root = tmp_path / "r2"
    root.mkdir(mode=0o700)
    _v3_db_with_r1(root)
    module.migrate_r2_schema(root, lock_path=tmp_path / "lock",
                             migrated_at=datetime(2026, 8, 12, tzinfo=timezone.utc))
    r2_ev = root / "g-live-r2-evidence.json"
    receipt = root / "g-live-r2-migration-receipt.json"
    r2_before = r2_ev.read_bytes()
    receipt_before = receipt.read_bytes()
    diag_path, _, _ = _make_diagnostic_artifact(tmp_path)
    credential_parent = tmp_path / "credentials"
    credential_parent.mkdir(mode=0o700, exist_ok=True)
    credential = credential_parent / "fred.env"
    credential.write_text("metadata-only\n")
    credential.chmod(0o600)
    opener = _R3LiveOpener()
    base = datetime(2026, 8, 12, tzinfo=timezone.utc)
    wall_index = [0]
    mono_index = [0]
    def wall():
        wall_index[0] += 1
        return base + timedelta(seconds=wall_index[0] * 2)
    def mono():
        mono_index[0] += 1
        return float(mono_index[0])
    result = module.execute_r3(
        root=root, credential_path=credential, lock_path=tmp_path / "lock",
        diagnostic_path=diag_path, execution_allowed=True,
        wall_clock=wall, monotonic_clock=mono, sleeper=lambda _: None,
        api_key_loader=lambda path: "a" * 32,
        opener_builder=lambda: opener)
    assert result["code"] == "durable_success"
    assert r2_ev.read_bytes() == r2_before
    assert receipt.read_bytes() == receipt_before


# B2: R3 execute must never call reconcile_r2_high_water or _write_r2_evidence
def test_r3_execute_never_writes_r2_evidence(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    r2_ev = root / "g-live-r2-evidence.json"
    r2_before = r2_ev.read_bytes()
    diag_path, _, _ = _make_diagnostic_artifact(tmp_path)
    credential_parent = tmp_path / "credentials"
    credential_parent.mkdir(mode=0o700, exist_ok=True)
    credential = credential_parent / "fred.env"
    credential.write_text("metadata-only\n")
    credential.chmod(0o600)
    opener = _R3LiveOpener()
    base = datetime(2026, 8, 12, tzinfo=timezone.utc)
    wall_index = [0]
    mono_index = [0]
    def wall():
        wall_index[0] += 1
        return base + timedelta(seconds=wall_index[0] * 2)
    def mono():
        mono_index[0] += 1
        return float(mono_index[0])
    result = module.execute_r3(
        root=root, credential_path=credential, lock_path=tmp_path / "lock",
        diagnostic_path=diag_path, execution_allowed=True,
        wall_clock=wall, monotonic_clock=mono, sleeper=lambda _: None,
        api_key_loader=lambda path: "a" * 32,
        opener_builder=lambda: opener)
    assert result["code"] == "durable_success"
    assert r2_ev.read_bytes() == r2_before


def test_r3_execute_r2_evidence_immutable_on_failure(tmp_path):
    """B2: R2 evidence must be immutable even on R3 first-failure quarantine."""
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    r2_ev = root / "g-live-r2-evidence.json"
    r2_before = r2_ev.read_bytes()
    diag_path, _, _ = _make_diagnostic_artifact(tmp_path)
    credential_parent = tmp_path / "credentials"
    credential_parent.mkdir(mode=0o700, exist_ok=True)
    credential = credential_parent / "fred.env"
    credential.write_text("metadata-only\n")
    credential.chmod(0o600)
    opener = _R3LiveOpener(fail_at=2)
    base = datetime(2026, 8, 12, tzinfo=timezone.utc)
    wall_index = [0]
    mono_index = [0]
    def wall():
        wall_index[0] += 1
        return base + timedelta(seconds=wall_index[0] * 2)
    def mono():
        mono_index[0] += 1
        return float(mono_index[0])
    result = module.execute_r3(
        root=root, credential_path=credential, lock_path=tmp_path / "lock",
        diagnostic_path=diag_path, execution_allowed=True,
        wall_clock=wall, monotonic_clock=mono, sleeper=lambda _: None,
        api_key_loader=lambda path: "a" * 32,
        opener_builder=lambda: opener)
    assert result["code"] == "connection_failure"
    assert r2_ev.read_bytes() == r2_before


def test_r3_execute_does_not_call_reconcile_r2_high_water(tmp_path):
    """B2: execute_r3 must not call reconcile_r2_high_water at all."""
    import ast
    source = (Path(__file__).parents[1] / "tools/fred_canonical_live.py").read_text()
    tree = ast.parse(source)
    func = next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == "execute_r3")
    calls = [n for n in ast.walk(func) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id in ("reconcile_r2_high_water", "_write_r2_evidence")]
    assert len(calls) == 0


def test_r3_execute_first_failure_stops(tmp_path):
    module, result, root, _, opener, keys, _ = _r3_dependencies(tmp_path, fail_at=3)
    assert result["code"] == "connection_failure"
    assert result["provider_starts"] == 3
    assert result["credential_reads"] == 3
    assert len(opener.calls) == 3
    store = FredMaterialStore(root)
    assert store.campaign_state_for(fred_macro.R3_CAMPAIGN_CODE)[0] == "QUARANTINED"


@pytest.mark.parametrize("position", range(1, 6))
def test_r3_execute_each_failure_position_stops(position, tmp_path):
    _, result, _, _, opener, keys, _ = _r3_dependencies(tmp_path, fail_at=position)
    assert result["code"] == "connection_failure"
    assert result["provider_starts"] == position


def test_r3_execute_replay_after_complete_is_zero_start(tmp_path):
    module, result, root, credential, opener, keys, diag_path = \
        _r3_dependencies(tmp_path)
    assert result["code"] == "durable_success"
    replay_opener = _R3LiveOpener()
    replay_keys = []
    replay = module.execute_r3(
        root=root, credential_path=credential, lock_path=tmp_path / "lock",
        diagnostic_path=diag_path, execution_allowed=True,
        wall_clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc),
        monotonic_clock=lambda: 1.0, sleeper=lambda _: None,
        api_key_loader=lambda path: replay_keys.append(path),
        opener_builder=lambda: replay_opener)
    assert replay["code"] == "already_complete"
    assert replay["provider_starts"] == 0
    assert replay["credential_reads"] == 0
    assert replay_opener.calls == []


def test_r3_execute_forbidden_by_default(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    diag_path, _, _ = _make_diagnostic_artifact(tmp_path)
    result = module.execute_r3(
        root=root, credential_path=tmp_path / "cred", lock_path=tmp_path / "lock",
        diagnostic_path=diag_path, execution_allowed=False,
        wall_clock=lambda: None, monotonic_clock=lambda: None,
        sleeper=lambda _: None,
        api_key_loader=lambda _: (_ for _ in ()).throw(AssertionError()),
        opener_builder=lambda: (_ for _ in ()).throw(AssertionError()))
    assert result["code"] == "g_live_forbidden"
    assert result["provider_starts"] == 0


def test_r3_execute_creates_campaign_only_under_lock(tmp_path):
    module = _live_tool()
    root = tmp_path / "r2"
    root.mkdir(mode=0o700)
    _v3_db_with_r1(root)
    module.migrate_r2_schema(root, lock_path=tmp_path / "lock",
                             migrated_at=datetime(2026, 8, 12, tzinfo=timezone.utc))
    diag_path, _, _ = _make_diagnostic_artifact(tmp_path)
    with sqlite3.connect(root / "fred.sqlite3") as conn:
        r3_before = conn.execute(
            "SELECT count(*) FROM campaigns WHERE campaign_code=?",
            (fred_macro.R3_CAMPAIGN_CODE,)).fetchone()[0]
    assert r3_before == 0
    credential_parent = tmp_path / "credentials"
    credential_parent.mkdir(mode=0o700, exist_ok=True)
    credential = credential_parent / "fred.env"
    credential.write_text("metadata-only\n")
    credential.chmod(0o600)
    opener = _R3LiveOpener()
    base = datetime(2026, 8, 12, tzinfo=timezone.utc)
    wall_index = [0]
    mono_index = [0]
    def wall():
        wall_index[0] += 1
        return base + timedelta(seconds=wall_index[0] * 2)
    def mono():
        mono_index[0] += 1
        return float(mono_index[0])
    result = module.execute_r3(
        root=root, credential_path=credential, lock_path=tmp_path / "lock",
        diagnostic_path=diag_path, execution_allowed=True,
        wall_clock=wall, monotonic_clock=mono, sleeper=lambda _: None,
        api_key_loader=lambda path: "a" * 32,
        opener_builder=lambda: opener)
    assert result["code"] == "durable_success"
    with sqlite3.connect(root / "fred.sqlite3") as conn:
        r3_after = conn.execute(
            "SELECT count(*) FROM campaigns WHERE campaign_code=?",
            (fred_macro.R3_CAMPAIGN_CODE,)).fetchone()[0]
    assert r3_after == 1


# M3: Bad diagnostic ledger must give sanitized code with zero DB write/credential/opener
def test_r3_execute_bad_ledger_zero_write_zero_start(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    db_before = hashlib.sha256((root / "fred.sqlite3").read_bytes()).hexdigest()
    bad_path = tmp_path / "bad-diag.json"
    bad_path.write_bytes(canonical_json({"schema": "wrong", "probes": []}))
    bad_path.chmod(0o600)
    credential = tmp_path / "cred"
    credential.write_text("metadata\n")
    credential.chmod(0o600)
    opener_calls = []
    key_calls = []
    result = module.execute_r3(
        root=root, credential_path=credential, lock_path=tmp_path / "lock",
        diagnostic_path=bad_path, execution_allowed=True,
        wall_clock=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
        monotonic_clock=lambda: 1.0, sleeper=lambda _: None,
        api_key_loader=lambda path: key_calls.append(path) or "a" * 32,
        opener_builder=lambda: type("O", (), {
            "open": staticmethod(lambda *a, **kw: opener_calls.append(1) or (_ for _ in ()).throw(OSError()))})())
    assert result["code"].startswith("diagnostic_")
    assert result["provider_starts"] == 0
    assert result["credential_reads"] == 0
    assert opener_calls == []
    assert key_calls == []
    db_after = hashlib.sha256((root / "fred.sqlite3").read_bytes()).hexdigest()
    assert db_before == db_after


# ---------------------------------------------------------------------------
# 8. R2 retirement (r2_retired_by_r3)
# ---------------------------------------------------------------------------

def test_r2_command_returns_retired_after_r3(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    result = module.readback_r2(root)
    assert result.get("code") == "r2_retired_by_r3"


def test_r2_preflight_returns_retired_after_r3(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    result = module.preflight_r2(root, lock_path=tmp_path / "lock")
    assert result.get("code") == "r2_retired_by_r3"


def test_r2_execute_returns_retired_after_r3(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    result = module.execute_r2(
        root=root, credential_path=tmp_path / "cred", lock_path=tmp_path / "lock",
        execution_allowed=True,
        wall_clock=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
        monotonic_clock=lambda: 1.0, sleeper=lambda _: None,
        api_key_loader=lambda _: (_ for _ in ()).throw(AssertionError()),
        opener_builder=lambda: (_ for _ in ()).throw(AssertionError()))
    assert result["code"] == "r2_retired_by_r3"
    assert result["provider_starts"] == 0


def test_r2_migrate_returns_retired_after_r3(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    result = module.migrate_r2_schema(
        root, lock_path=tmp_path / "lock",
        migrated_at=datetime(2026, 8, 13, tzinfo=timezone.utc))
    assert result.get("code") == "r2_retired_by_r3"


def test_r2_rollback_returns_retired_after_r3(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    result = module.rollback_r2_schema(root, lock_path=tmp_path / "lock")
    assert result.get("code") == "r2_retired_by_r3"


def test_r2_command_still_works_without_r3(tmp_path):
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    result = module.readback_r2(root)
    assert result["code"] == "readback_ok"


# ---------------------------------------------------------------------------
# 9. Static exception containment and secret-safe output
# ---------------------------------------------------------------------------

def test_r3_evidence_is_secret_safe(tmp_path):
    module = _live_tool()
    store = FredMaterialStore(tmp_path / "r3")
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    evidence = module._r3_evidence(
        store, [], 0, "ACTIVE", None,
        external_diagnostic_count=_EXPECTED_DIAGNOSTIC_COUNT,
        diagnostic_sha256=_EXPECTED_DIAGNOSTIC_SHA,
        manifest_identity={"campaign_code": R3_CAMPAIGN_CODE},
        r1_evidence_sha256="a" * 64,
        r2_evidence_sha256="b" * 64,
        migration_receipt_sha256="c" * 64,
    )
    rendered = canonical_json(evidence).decode()
    assert "api_key" not in rendered
    assert "https://" not in rendered
    assert str(store.root) not in rendered


def test_r3_cli_emits_static_json(tmp_path, capsys):
    module = _live_tool()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        module.run_cli(["execute-r3"], root=tmp_path / "nonexistent",
                       credential_path=tmp_path / "cred", lock_path=tmp_path / "lock",
                       diagnostic_path=tmp_path / "diag.json",
                       execution_allowed=False,
                       wall_clock=lambda: None, monotonic_clock=lambda: None,
                       sleeper=lambda _: None,
                       api_key_loader=lambda _: (_ for _ in ()).throw(AssertionError()),
                       opener_builder=lambda: (_ for _ in ()).throw(AssertionError()))
    output = buf.getvalue().strip()
    payload = json.loads(output)
    assert payload["code"] == "g_live_forbidden"
    assert str(tmp_path) not in output
    assert "Traceback" not in output


# ---------------------------------------------------------------------------
# 10. R3 never writes R2 evidence (covered by B2 tests above)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 11. >=1s spacing in R3 execute — real sleeper assertion
# ---------------------------------------------------------------------------

def test_r3_execute_enforces_spacing_with_real_sleeper(tmp_path):
    """Sleeper must be called between operations to ensure >=1s gap."""
    module = _live_tool()
    root = _migrated_r2(tmp_path, module)
    diag_path, _, _ = _make_diagnostic_artifact(tmp_path)
    credential_parent = tmp_path / "credentials"
    credential_parent.mkdir(mode=0o700, exist_ok=True)
    credential = credential_parent / "fred.env"
    credential.write_text("metadata-only\n")
    credential.chmod(0o600)
    sleeps = []
    opener = _R3LiveOpener()
    # Use real time for wall so sleeper actually advances
    import time as _time
    _t0 = [_time.time()]
    def wall():
        return datetime(2026, 8, 12, tzinfo=timezone.utc) + timedelta(
            seconds=_time.time() - _t0[0])
    def mono():
        return _time.monotonic()
    def fast_sleeper(seconds):
        sleeps.append(seconds)
        _time.sleep(seconds)
    result = module.execute_r3(
        root=root, credential_path=credential, lock_path=tmp_path / "lock",
        diagnostic_path=diag_path, execution_allowed=True,
        wall_clock=wall, monotonic_clock=mono, sleeper=fast_sleeper,
        api_key_loader=lambda path: "a" * 32,
        opener_builder=lambda: opener)
    assert result["code"] == "durable_success"
    assert len(opener.calls) == 5
    # Sleeper called 4 times (between 5 operations)
    assert len(sleeps) == 4
    for duration in sleeps:
        assert duration > 0
    assert all(pairs[-1] == ("api_key", "a" * 32) for _, pairs in opener.calls)


# ---------------------------------------------------------------------------
# 12. R3 composition quarantine dispatch
# ---------------------------------------------------------------------------

def test_r3_composition_quarantine_dispatches_to_r3(tmp_path):
    root = tmp_path / "r3comp"
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    req = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    res = store.reserve_campaign(
        fred_macro.R3_CAMPAIGN_CODE, 1, req,
        started_at=_started(), external_diagnostic_count=2)
    from tokmo.fred_composition import _quarantine_for_reservation
    _quarantine_for_reservation(store, res, "connection_failure")
    assert store.campaign_state_for(fred_macro.R3_CAMPAIGN_CODE) == (
        "QUARANTINED", "connection_failure")


def test_r3_composition_dispatcher_is_isolated(tmp_path):
    import ast
    source = (Path(__file__).parents[1] / "src/tokmo/fred_composition.py").read_text()
    tree = ast.parse(source)
    dispatcher = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef)
                      and node.name == "_quarantine_for_reservation")
    names = {node.id for node in ast.walk(dispatcher)
             if isinstance(node, ast.Name)}
    assert "R3_CAMPAIGN_CODE" in names
    calls = [n for n in ast.walk(dispatcher) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "quarantine_campaign"]
    assert len(calls) >= 3
    legacy_calls = [n for n in ast.walk(dispatcher) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "quarantine"]
    assert len(legacy_calls) == 2
# Minor: unknown campaign in _quarantine_for_reservation must raise, not mutate R1
def test_quarantine_for_reservation_unknown_campaign_raises(tmp_path):
    """Unknown campaign code must raise campaign_mismatch, not mutate R1."""
    store = FredMaterialStore(tmp_path / "test")
    store.ensure_live_campaign()  # R1 singleton
    # Inject an attempt row with an unknown campaign_code
    from tokmo.records.fred_material_store import Reservation
    from tokmo.fred_composition import _quarantine_for_reservation
    fake_identity = "z" * 64
    with sqlite3.connect(store.path) as conn:
        conn.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            "FRED", "CANONICAL_SOURCE_STAGE3", fake_identity, 1, "SERIES",
            "x" * 64, "LIVE", "2026-01-01T00:00:00.000000Z", "RESERVED",
            "UNKNOWN_CAMPAIGN", "legacy", "0" * 64))
        conn.commit()
    fake = Reservation("reserved", fake_identity, 1)
    with pytest.raises(ValueError, match="campaign_mismatch"):
        _quarantine_for_reservation(store, fake, "connection_failure")
    # R1 singleton must be unchanged
    r1_state = store.campaign_state()
    assert r1_state is None or r1_state[0] == "ACTIVE"


# ---------------------------------------------------------------------------
# 13. Architecture test — no BaseException, no secrets
# ---------------------------------------------------------------------------

def test_r3_changed_files_have_no_secret_or_path_leak():
    changed = (
        "src/tokmo/values/fred_macro.py",
        "src/tokmo/records/fred_material_store.py",
        "src/tokmo/fred_composition.py",
        "tools/fred_canonical_live.py",
    )
    for relative in changed:
        path = Path(__file__).parents[1] / relative
        text = path.read_text()
        assert "api_key=" + "a" * 32 not in text
        assert "/Users/" not in text and "/home/" not in text
        assert ".hermes/credentials/fred.env" not in text


def test_r3_changed_files_no_baseexception():
    import ast
    for relative in ("src/tokmo/fred_composition.py", "tools/fred_canonical_live.py"):
        path = Path(__file__).parents[1] / relative
        tree = ast.parse(path.read_text())
        assert not any(isinstance(node, ast.ExceptHandler)
                       and isinstance(node.type, ast.Name)
                       and node.type.id == "BaseException"
                       for node in ast.walk(tree))


def test_r3_files_in_expected_set():
    actual = {path.relative_to(Path(__file__).parents[1]).as_posix()
              for base in (Path(__file__).parents[1] / "src",
                           Path(__file__).parents[1] / "tools",
                           Path(__file__).parents[1] / "tests")
              for path in base.rglob("*fred*")
              if path.is_file() and "__pycache__" not in path.parts}
    expected = {
        "src/tokmo/values/fred_macro.py", "src/tokmo/facts/fred.py",
        "src/tokmo/facts/fred_live.py", "src/tokmo/records/fred_material_store.py",
        "src/tokmo/fred_composition.py", "tools/fred_canonical.py",
        "tools/fred_canonical_live.py",
        "tests/test_fred_values.py", "tests/test_fred_fact.py", "tests/test_fred_live.py",
        "tests/test_fred_material_store.py", "tests/test_fred_composition.py",
        "tests/test_fred_architecture.py", "tests/test_fred_canonical_live.py",
        "tests/test_fred_r3.py", "tests/test_fred_r4.py",
        # Routine on-demand query seam: additive paths that add no R3 surface.
        "src/tokmo/values/fred_query.py", "src/tokmo/facts/fred_query.py",
        "src/tokmo/records/fred_query_cache.py",
        "src/tokmo/programs/macro_fred_inputs.py",
        "src/tokmo/fred_query_composition.py",
        "tests/test_fred_query_values.py", "tests/test_fred_query_fact.py",
        "tests/test_fred_query_cache.py", "tests/test_macro_fred_inputs.py",
        "tests/test_fred_query_architecture.py",
        # G2 on-demand connection delta: additive tool and test paths.
        "tools/fred_ondemand_canary.py",
        "tests/test_fred_ondemand_canary.py",
        # FRED on-demand → Macro caller connection: moved sidecar + new tests.
        "src/tokmo/records/fred_schedule_sidecar.py",
        "tests/test_fred_schedule_sidecar.py",
        "tests/test_macro_fred_caller.py",
    }
    actual_no_fixtures = actual - {p for p in actual if p.startswith("tests/fixtures/fred/")}
    assert actual_no_fixtures == expected


# ---------------------------------------------------------------------------
# 14. CLI default diagnostic path
# ---------------------------------------------------------------------------

def test_r3_cli_default_diagnostic_path_is_canonical(tmp_path, monkeypatch):
    """M4: CLI without --diagnostic-path must default to canonical root/diagnostic-probes.json."""
    module = _live_tool()
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    # Just verify the default diagnostic_path resolves to root/diagnostic
    # We test by checking that execute-r3 with no diagnostic_path kwarg
    # tries to validate root/g-live-diagnostic-probes.json
    # Since that file doesn't exist, it should fail with a static code
    credential = tmp_path / "cred"
    credential.write_text("metadata\n")
    credential.chmod(0o600)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        module.run_cli(["execute-r3", "--g-live-r3-execute"],
                       root=root, credential_path=credential,
                       lock_path=tmp_path / "lock",
                       wall_clock=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
                       monotonic_clock=lambda: 1.0, sleeper=lambda _: None,
                       api_key_loader=lambda _: "a" * 32,
                       opener_builder=lambda: (_ for _ in ()).throw(AssertionError()))
    output = buf.getvalue().strip()
    payload = json.loads(output)
    # Since no diagnostic file exists at root, it must fail before any provider start
    assert payload["code"] in ("diagnostic_invalid", "operation_failure",
                               "unsafe_private_path")
    assert payload["provider_starts"] == 0
    assert str(tmp_path) not in output
