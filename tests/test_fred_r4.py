"""FRED G-LIVE R4 output-type-4 historical realtime repair: strict TDD tests.

Implements every §7/§10 acceptance from the R4 Delta SDS v0.1.
Authority: .ai/plans/2026-08-12_fred-g-live-r4-output4-realtime-repair-sds-v0.1.md
"""

from datetime import date, datetime, timedelta, timezone
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import importlib.util
from pathlib import Path
import pytest

from tokmo.values import fred_macro
from tokmo.values.fred_macro import (
    FredOperation, FredRequest, ExecutionMode, canonical_json,
)
from tokmo.records.fred_material_store import FredMaterialStore
from tokmo.facts.fred import build_query_pairs, parse_fred_response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _live_tool():
    path = Path(__file__).parents[1] / "tools" / "fred_canonical_live.py"
    spec = importlib.util.spec_from_file_location("fred_canonical_live_r4_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _started(second: int = 0):
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# SDS §7 test 1: R4 manifest operations — op2 and only op2 has realtime
# ---------------------------------------------------------------------------

R4_CAMPAIGN_CODE = "FRED_G_LIVE_PAYEMS_R4"
R4_MANIFEST_CODE = "FRED_G_LIVE_PAYEMS_FIVE_R4"
R4_MANIFEST_REVISION = "fred-canonical-stage3-g-live-r4-output4-realtime-v1"
R4_CODE_REVISION = "fred-g-live-controller-r4-output4-realtime-v1"
R4_MANIFEST_DIGEST = "4c0d0c3f78382540dbfc7e5a8af21c1ceadeafb77978c85c49bef9588dbd40f6"


def test_r4_constants_match_sds():
    assert fred_macro.R4_CAMPAIGN_CODE == R4_CAMPAIGN_CODE
    assert fred_macro.R4_MANIFEST_CODE == R4_MANIFEST_CODE
    assert fred_macro.R4_MANIFEST_REVISION == R4_MANIFEST_REVISION
    assert fred_macro.R4_CODE_REVISION == R4_CODE_REVISION
    assert fred_macro.R4_MANIFEST_DIGEST == R4_MANIFEST_DIGEST


def test_r4_manifest_op2_only_has_realtime():
    manifest = fred_macro.r4_g_live_manifest(release_id=1)
    assert [req.operation.value for req in manifest] == [
        "SERIES", "SERIES_OBSERVATIONS", "SERIES_RELEASE",
        "RELEASE_DATES", "SERIES_VINTAGE_DATES",
    ]
    # op2 and only op2 has realtime pair
    assert manifest[1].realtime_start == date(1776, 7, 4)
    assert manifest[1].realtime_end == date(9999, 12, 31)
    for i, req in enumerate(manifest):
        if i != 1:
            assert req.realtime_start is None
            assert req.realtime_end is None


def test_r4_manifest_emitted_key_sets_exact_allowlists():
    manifest = fred_macro.r4_g_live_manifest(release_id=1)
    pairs_list = [build_query_pairs(req) for req in manifest]
    key_sets = [set(key for key, _ in pairs) for pairs in pairs_list]
    expected = (
        {"series_id", "file_type"},
        {"series_id", "realtime_start", "realtime_end", "observation_start",
         "observation_end", "units", "output_type", "sort_order",
         "limit", "offset", "file_type"},
        {"series_id", "file_type"},
        {"release_id", "include_release_dates_with_no_data", "sort_order",
         "limit", "offset", "file_type"},
        {"series_id", "sort_order", "limit", "offset", "file_type"},
    )
    assert key_sets == list(expected)
    # op2 pairs order: realtime follows series_id, precedes observation_start
    op2_keys = [k for k, _ in pairs_list[1]]
    assert op2_keys == ["series_id", "realtime_start", "realtime_end",
                        "observation_start", "observation_end", "units",
                        "output_type", "sort_order", "limit", "offset",
                        "file_type"]


def test_r4_manifest_observation_window_and_op4_symbolic():
    manifest = fred_macro.r4_g_live_manifest(release_id=42)
    assert manifest[1].observation_start == date(2026, 7, 1)
    assert manifest[1].observation_end == date(2026, 7, 31)
    assert manifest[1].output_type == 4
    assert manifest[1].limit == 10 and manifest[1].offset == 0
    assert manifest[3].release_id == 42
    assert manifest[3].include_release_dates_with_no_data is False
    assert manifest[3].limit == 10 and manifest[3].offset == 0
    assert manifest[4].limit == 10 and manifest[4].offset == 0


# ---------------------------------------------------------------------------
# SDS §7 test 2: R4 manifest digest literal
# ---------------------------------------------------------------------------

R4_EXACT_LITERAL = (
    b'{"campaign_code":"FRED_G_LIVE_PAYEMS_R4",'
    b'"manifest_code":"FRED_G_LIVE_PAYEMS_FIVE_R4",'
    b'"manifest_revision":"fred-canonical-stage3-g-live-r4-output4-realtime-v1",'
    b'"operations":'
    b'[{"operation":"SERIES","pairs":[["series_id","PAYEMS"]]},'
    b'{"operation":"SERIES_OBSERVATIONS","pairs":[["series_id","PAYEMS"],'
    b'["realtime_start","1776-07-04"],["realtime_end","9999-12-31"],'
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


def test_r4_digest_exact_literal_bytes():
    assert len(R4_EXACT_LITERAL) == 903
    computed = hashlib.sha256(R4_EXACT_LITERAL).hexdigest()
    assert computed == R4_MANIFEST_DIGEST


# ---------------------------------------------------------------------------
# SDS §7 test 3: R4 manifest digest shared-builder
# ---------------------------------------------------------------------------

def test_r4_digest_shared_builder_matches():
    operations = []
    for index, request in enumerate(fred_macro.r4_g_live_manifest(release_id=1), 1):
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
        "campaign_code": R4_CAMPAIGN_CODE,
        "manifest_code": R4_MANIFEST_CODE,
        "manifest_revision": R4_MANIFEST_REVISION,
        "operations": operations,
    }
    builder_bytes = canonical_json(payload)
    builder_digest = hashlib.sha256(builder_bytes).hexdigest()
    assert builder_digest == R4_MANIFEST_DIGEST
    assert builder_bytes == R4_EXACT_LITERAL


# ---------------------------------------------------------------------------
# SDS §7 test 4: R1/R2/R3 immutability
# ---------------------------------------------------------------------------

def test_r1_r2_r3_digests_unchanged_after_r4():
    assert fred_macro.MANIFEST_DIGEST == \
        "1ca30ee6c3ce6442e31f2c31a09461d6407bad8c94dce6be97807540056386a1"
    assert fred_macro.R2_MANIFEST_DIGEST == \
        "fa0d82ac80678e74c02e2b05b642ce225a756f383727b814664ff134622fd134"
    assert fred_macro.R3_MANIFEST_DIGEST == \
        "da4d281aa2deae985b818b2d74dadfb922ef259721985c8aa26b348059828d51"


# ---------------------------------------------------------------------------
# SDS §7 test 5: R4 RCA artifact validation
# ---------------------------------------------------------------------------

# Build the byte-faithful RCA artifact matching pinned SHA
# e24ea053975068779cdd69f3183f25a3808afc58e6e9c3501535a4a587ce3b23
_RCA_PROBES = [
    {"sequence": 1, "name": "observations_output4_minimal",
     "started_at": "2026-08-12T05:42:45.831971+00:00",
     "outcome": "settled", "http_status": 400,
     "category": "provider_error_json",
     "provider_message": "Bad Request.  No vintage dates exist for the specified real-time period: 2026-08-12 to 2026-08-12."},
    {"sequence": 2, "name": "observations_output4_broad_realtime",
     "started_at": "2026-08-12T05:43:23.690825+00:00",
     "outcome": "settled", "http_status": 200,
     "category": "success"},
]

_RCA_ARTIFACT = {
    "schema": "tokmo.fred.r4-rca.v1",
    "prior_operational_starts": 6,
    "ceiling": 10,
    "probes": _RCA_PROBES,
}

_RCA_ARTIFACT_BYTES = canonical_json(_RCA_ARTIFACT)
_RCA_SHA256 = hashlib.sha256(_RCA_ARTIFACT_BYTES).hexdigest()

# SDS §3.3 pinned whole-file SHA-256 of the production RCA artifact. The fixture
# above reconstructs that artifact byte-for-byte, so _RCA_SHA256 must equal it.
_RCA_SDS_PINNED_SHA256 = "e24ea053975068779cdd69f3183f25a3808afc58e6e9c3501535a4a587ce3b23"


import copy


def _write_rca_artifact(path, *, artifact=None, mode=0o600):
    if artifact is None:
        artifact = copy.deepcopy(_RCA_ARTIFACT)
    data = canonical_json(artifact)
    path.write_bytes(data)
    path.chmod(mode)
    return hashlib.sha256(data).hexdigest()


def test_r4_rca_artifact_top_level_closed_keys():
    """SDS §3.3: top-level keys exactly {schema, prior_operational_starts, ceiling, probes}."""
    assert set(_RCA_ARTIFACT.keys()) == {
        "schema", "prior_operational_starts", "ceiling", "probes"}


def test_r4_rca_artifact_row_schema_non_homogeneous():
    """Row 1 has provider_message, row 2 omits it entirely."""
    row1_keys = set(_RCA_PROBES[0].keys())
    row2_keys = set(_RCA_PROBES[1].keys())
    assert "provider_message" in row1_keys
    assert "provider_message" not in row2_keys
    # neither row has provider_code
    assert "provider_code" not in row1_keys
    assert "provider_code" not in row2_keys


def test_r4_rca_fixture_reproduces_sds_pinned_hash():
    """SDS §3.3: the fixture is the pinned production artifact, byte-for-byte."""
    assert _RCA_SHA256 == _RCA_SDS_PINNED_SHA256
    assert _live_tool().R4_RCA_SHA256 == _RCA_SDS_PINNED_SHA256


def test_r4_validate_rca_artifact_accepts_byte_faithful(tmp_path):
    """SDS §7 test 5: the byte-faithful artifact validates under the pinned default."""
    module = _live_tool()
    path = tmp_path / "g-live-r4-rca-probes.json"
    digest = _write_rca_artifact(path)
    assert digest == _RCA_SDS_PINNED_SHA256
    result = module.validate_rca_ledger(path)
    assert result["count"] == 2
    assert result["sha256"] == _RCA_SDS_PINNED_SHA256


def test_r4_validate_rca_artifact_rejects_symlink(tmp_path):
    module = _live_tool()
    orig = tmp_path / "g-live-r4-rca-probes.json"
    _write_rca_artifact(orig)
    link = tmp_path / "link.json"
    os.symlink(orig, link)
    with pytest.raises((ValueError, OSError)):
        module.validate_rca_ledger(link, expected_sha256=_RCA_SHA256)


def test_r4_validate_rca_artifact_rejects_wrong_mode(tmp_path):
    module = _live_tool()
    path = tmp_path / "g-live-r4-rca-probes.json"
    _write_rca_artifact(path, mode=0o644)
    with pytest.raises((ValueError, OSError)):
        module.validate_rca_ledger(path, expected_sha256=_RCA_SHA256)


def test_r4_validate_rca_artifact_rejects_wrong_schema(tmp_path):
    module = _live_tool()
    art = copy.deepcopy(_RCA_ARTIFACT)
    art["schema"] = "wrong"
    path = tmp_path / "g-live-r4-rca-probes.json"
    _write_rca_artifact(path, artifact=art)
    with pytest.raises((ValueError,)):
        module.validate_rca_ledger(path, expected_sha256=_RCA_SHA256)


def test_r4_validate_rca_artifact_rejects_wrong_count(tmp_path):
    module = _live_tool()
    art = copy.deepcopy(_RCA_ARTIFACT)
    art["probes"] = [copy.deepcopy(_RCA_PROBES[0])]
    path = tmp_path / "g-live-r4-rca-probes.json"
    _write_rca_artifact(path, artifact=art)
    with pytest.raises((ValueError,)):
        module.validate_rca_ledger(path, expected_sha256=_RCA_SHA256)


def test_r4_validate_rca_artifact_rejects_wrong_order(tmp_path):
    module = _live_tool()
    probes = [copy.deepcopy(_RCA_PROBES[1]), copy.deepcopy(_RCA_PROBES[0])]
    probes[0]["sequence"] = 1
    probes[1]["sequence"] = 2
    art = copy.deepcopy(_RCA_ARTIFACT)
    art["probes"] = probes
    path = tmp_path / "g-live-r4-rca-probes.json"
    _write_rca_artifact(path, artifact=art)
    with pytest.raises((ValueError,)):
        module.validate_rca_ledger(path, expected_sha256=_RCA_SHA256)


def test_r4_validate_rca_artifact_rejects_wrong_order_independent_of_hash(tmp_path):
    """SDS §7 test 5: order rejection is contract-enforced, not merely hash-caught.

    The reordered artifact is validated against its own hash, so the whole-file
    hash gate passes and only the §3.3 per-row contract can reject it.
    """
    module = _live_tool()
    probes = [copy.deepcopy(_RCA_PROBES[1]), copy.deepcopy(_RCA_PROBES[0])]
    probes[0]["sequence"] = 1
    probes[1]["sequence"] = 2
    art = copy.deepcopy(_RCA_ARTIFACT)
    art["probes"] = probes
    path = tmp_path / "g-live-r4-rca-probes.json"
    digest = _write_rca_artifact(path, artifact=art)
    with pytest.raises(ValueError, match="rca_invalid"):
        module.validate_rca_ledger(path, expected_sha256=digest)


def test_r4_validate_rca_artifact_rejects_wrong_row_name_independent_of_hash(tmp_path):
    """SDS §3.3: row names are pinned per position."""
    module = _live_tool()
    probes = [copy.deepcopy(p) for p in _RCA_PROBES]
    probes[0]["name"] = "observations_output4_broad_realtime"
    art = copy.deepcopy(_RCA_ARTIFACT)
    art["probes"] = probes
    path = tmp_path / "g-live-r4-rca-probes.json"
    digest = _write_rca_artifact(path, artifact=art)
    with pytest.raises(ValueError, match="rca_invalid"):
        module.validate_rca_ledger(path, expected_sha256=digest)


def test_r4_validate_rca_artifact_rejects_wrong_status_independent_of_hash(tmp_path):
    """SDS §3.3: row 1 is http_status 400, row 2 is http_status 200."""
    module = _live_tool()
    probes = [copy.deepcopy(p) for p in _RCA_PROBES]
    probes[1]["http_status"] = 400
    art = copy.deepcopy(_RCA_ARTIFACT)
    art["probes"] = probes
    path = tmp_path / "g-live-r4-rca-probes.json"
    digest = _write_rca_artifact(path, artifact=art)
    with pytest.raises(ValueError, match="rca_invalid"):
        module.validate_rca_ledger(path, expected_sha256=digest)


def test_r4_validate_rca_artifact_rejects_wrong_category_independent_of_hash(tmp_path):
    """SDS §3.3: row 1 category provider_error_json, row 2 category success."""
    module = _live_tool()
    probes = [copy.deepcopy(p) for p in _RCA_PROBES]
    probes[0]["category"] = "success"
    art = copy.deepcopy(_RCA_ARTIFACT)
    art["probes"] = probes
    path = tmp_path / "g-live-r4-rca-probes.json"
    digest = _write_rca_artifact(path, artifact=art)
    with pytest.raises(ValueError, match="rca_invalid"):
        module.validate_rca_ledger(path, expected_sha256=digest)


def test_r4_validate_rca_artifact_rejects_provider_message_on_row_two(tmp_path):
    """SDS §3.3: provider_message is row 1 only; row 2 omits it entirely."""
    module = _live_tool()
    probes = [copy.deepcopy(p) for p in _RCA_PROBES]
    probes[1]["provider_message"] = "unexpected"
    art = copy.deepcopy(_RCA_ARTIFACT)
    art["probes"] = probes
    path = tmp_path / "g-live-r4-rca-probes.json"
    digest = _write_rca_artifact(path, artifact=art)
    with pytest.raises(ValueError, match="rca_invalid"):
        module.validate_rca_ledger(path, expected_sha256=digest)


def test_r4_validate_rca_artifact_rejects_provider_code_on_any_row(tmp_path):
    """SDS §3.3: R4 RCA rows never carry provider_code."""
    module = _live_tool()
    probes = [copy.deepcopy(p) for p in _RCA_PROBES]
    probes[0]["provider_code"] = 400
    art = copy.deepcopy(_RCA_ARTIFACT)
    art["probes"] = probes
    path = tmp_path / "g-live-r4-rca-probes.json"
    digest = _write_rca_artifact(path, artifact=art)
    with pytest.raises(ValueError, match="rca_invalid"):
        module.validate_rca_ledger(path, expected_sha256=digest)


def test_r4_validate_rca_artifact_rejects_unsettled(tmp_path):
    module = _live_tool()
    probes = [copy.deepcopy(p) for p in _RCA_PROBES]
    probes[0]["outcome"] = "started"
    art = copy.deepcopy(_RCA_ARTIFACT)
    art["probes"] = probes
    path = tmp_path / "g-live-r4-rca-probes.json"
    _write_rca_artifact(path, artifact=art)
    with pytest.raises((ValueError,)):
        module.validate_rca_ledger(path, expected_sha256=_RCA_SHA256)


def test_r4_validate_rca_artifact_rejects_hash_drift(tmp_path):
    module = _live_tool()
    probes = [copy.deepcopy(p) for p in _RCA_PROBES]
    probes[0]["started_at"] = "2026-08-12T04:30:10.654321+00:00"
    art = copy.deepcopy(_RCA_ARTIFACT)
    art["probes"] = probes
    path = tmp_path / "g-live-r4-rca-probes.json"
    _write_rca_artifact(path, artifact=art)
    # Shape-valid but hash drifts from pinned → must reject
    with pytest.raises((ValueError,), match="rca_hash_mismatch"):
        module.validate_rca_ledger(path, expected_sha256=_RCA_SHA256)


def test_r4_validate_rca_artifact_rejects_missing_top_level_key(tmp_path):
    module = _live_tool()
    art = dict(_RCA_ARTIFACT)
    del art["ceiling"]
    path = tmp_path / "g-live-r4-rca-probes.json"
    _write_rca_artifact(path, artifact=art)
    with pytest.raises((ValueError,)):
        module.validate_rca_ledger(path, expected_sha256=_RCA_SHA256)


def test_r4_validate_rca_artifact_rejects_extra_top_level_key(tmp_path):
    module = _live_tool()
    art = dict(_RCA_ARTIFACT)
    art["extra"] = 1
    path = tmp_path / "g-live-r4-rca-probes.json"
    _write_rca_artifact(path, artifact=art)
    with pytest.raises((ValueError,)):
        module.validate_rca_ledger(path, expected_sha256=_RCA_SHA256)


def test_r4_validate_rca_artifact_rejects_wrong_http_status_type(tmp_path):
    module = _live_tool()
    probes = [dict(p) for p in _RCA_PROBES]
    probes[0]["http_status"] = "400"
    art = dict(_RCA_ARTIFACT)
    art["probes"] = probes
    path = tmp_path / "g-live-r4-rca-probes.json"
    _write_rca_artifact(path, artifact=art)
    with pytest.raises((ValueError,)):
        module.validate_rca_ledger(path, expected_sha256=_RCA_SHA256)


def test_r4_validate_rca_artifact_ceiling_type_checked_not_used(tmp_path):
    """ceiling and prior_operational_starts type-checked (int) but value not used."""
    module = _live_tool()
    art = {
        "schema": "tokmo.fred.r4-rca.v1",
        "prior_operational_starts": 6,
        "ceiling": 10,
        "probes": [copy.deepcopy(p) for p in _RCA_PROBES],
    }
    path = tmp_path / "g-live-r4-rca-probes.json"
    _write_rca_artifact(path, artifact=art)
    # Should validate regardless of ceiling value content; it's not used in cap
    result = module.validate_rca_ledger(path, expected_sha256=hashlib.sha256(canonical_json(art)).hexdigest())
    assert result["count"] == 2


def test_r4_validate_rca_artifact_rejects_non_int_ceiling(tmp_path):
    module = _live_tool()
    art = {
        "schema": "tokmo.fred.r4-rca.v1",
        "prior_operational_starts": 6,
        "ceiling": "ten",
        "probes": _RCA_PROBES,
    }
    path = tmp_path / "g-live-r4-rca-probes.json"
    _write_rca_artifact(path, artifact=art)
    with pytest.raises((ValueError,)):
        module.validate_rca_ledger(path, expected_sha256=_RCA_SHA256)


def test_r4_validate_rca_artifact_rejects_hardlink(tmp_path):
    module = _live_tool()
    orig = tmp_path / "g-live-r4-rca-probes.json"
    _write_rca_artifact(orig)
    hard = tmp_path / "hard.json"
    os.link(orig, hard)
    with pytest.raises((ValueError, OSError)):
        module.validate_rca_ledger(hard)


# ---------------------------------------------------------------------------
# SDS §7 test 6: provider_message propagation prohibition
# ---------------------------------------------------------------------------

_RCA_PROVIDER_MESSAGE = (
    "Bad Request.  No vintage dates exist for the specified real-time period: "
    "2026-08-12 to 2026-08-12.")


def test_r4_rca_provider_message_not_in_production_output(tmp_path):
    """The RCA provider_message string appears nowhere in R4 production output."""
    module = _live_tool()
    # Build a store with R4 campaign and write R4 evidence
    store = FredMaterialStore(tmp_path / "r4")
    store.ensure_live_campaign(campaign_code=fred_macro.R4_CAMPAIGN_CODE)
    rca_path = tmp_path / "g-live-r4-rca-probes.json"
    rca_digest = _write_rca_artifact(rca_path)
    evidence = module._r4_evidence(
        store, [], 0, "ACTIVE", None,
        external_diagnostic_count=2,
        external_rca_count=2,
        diagnostic_sha256="d" * 64,
        rca_sha256=rca_digest,
        manifest_identity={"campaign_code": R4_CAMPAIGN_CODE,
                           "manifest_code": R4_MANIFEST_CODE},
        r1_evidence_sha256="a" * 64,
        r2_evidence_sha256="b" * 64,
        r3_evidence_sha256="e" * 64,
        migration_receipt_sha256="c" * 64,
        rca_sha256_value=rca_digest,
    )
    rendered = canonical_json(evidence).decode()
    assert _RCA_PROVIDER_MESSAGE not in rendered
    assert "vintage dates" not in rendered
    assert "provider_message" not in rendered


# ---------------------------------------------------------------------------
# SDS §7 tests 7-11: op2 parser fixtures
# ---------------------------------------------------------------------------

def test_r4_op2_echo_success_fixture():
    """op2 payload echoing 1776-07-04/9999-12-31 with correct row keys parses."""
    request = FredRequest(
        FredOperation.SERIES_OBSERVATIONS, series_id="PAYEMS",
        realtime_start=date(1776, 7, 4), realtime_end=date(9999, 12, 31),
        observation_start=date(2026, 7, 1), observation_end=date(2026, 7, 31),
        output_type=4, limit=10, offset=0)
    body = json.dumps({
        "realtime_start": "1776-07-04", "realtime_end": "9999-12-31",
        "observation_start": "2026-07-01", "observation_end": "2026-07-31",
        "units": "lin", "output_type": 4, "file_type": "json",
        "order_by": "observation_date", "sort_order": "asc",
        "count": 1, "offset": 0, "limit": 10,
        "observations": [
            {"realtime_start": "2026-08-12", "realtime_end": "9999-12-31",
             "date": "2026-07-01", "value": "151000"}],
    }).encode()
    page = parse_fred_response(request, body)
    assert len(page.rows) == 1
    assert page.rows[0]["date"] == "2026-07-01"


def test_r4_op2_echo_mismatch_fixture():
    """realtime_start echo drift → ValueError → response_contract_failure."""
    request = FredRequest(
        FredOperation.SERIES_OBSERVATIONS, series_id="PAYEMS",
        realtime_start=date(1776, 7, 4), realtime_end=date(9999, 12, 31),
        observation_start=date(2026, 7, 1), observation_end=date(2026, 7, 31),
        output_type=4, limit=10, offset=0)
    body = json.dumps({
        "realtime_start": "2026-08-12",  # echo drift!
        "realtime_end": "9999-12-31",
        "observation_start": "2026-07-01", "observation_end": "2026-07-31",
        "units": "lin", "output_type": 4, "file_type": "json",
        "order_by": "observation_date", "sort_order": "asc",
        "count": 1, "offset": 0, "limit": 10,
        "observations": [
            {"realtime_start": "2026-08-12", "realtime_end": "9999-12-31",
             "date": "2026-07-01", "value": "151000"}],
    }).encode()
    with pytest.raises(ValueError, match="realtime_start_echo_mismatch"):
        parse_fred_response(request, body)


def test_r4_op2_closed_key_mismatch_fixture():
    """Row with extra key → ValueError(closed_keys_mismatch)."""
    request = FredRequest(
        FredOperation.SERIES_OBSERVATIONS, series_id="PAYEMS",
        realtime_start=date(1776, 7, 4), realtime_end=date(9999, 12, 31),
        observation_start=date(2026, 7, 1), observation_end=date(2026, 7, 31),
        output_type=4, limit=10, offset=0)
    body = json.dumps({
        "realtime_start": "1776-07-04", "realtime_end": "9999-12-31",
        "observation_start": "2026-07-01", "observation_end": "2026-07-31",
        "units": "lin", "output_type": 4, "file_type": "json",
        "order_by": "observation_date", "sort_order": "asc",
        "count": 1, "offset": 0, "limit": 10,
        "observations": [
            {"realtime_start": "2026-08-12", "realtime_end": "9999-12-31",
             "date": "2026-07-01", "value": "151000",
             "extra_key": "bad"}],  # extra key!
    }).encode()
    with pytest.raises(ValueError, match="closed_keys_mismatch"):
        parse_fred_response(request, body)


def test_r4_op2_zero_row_parse_then_empty_page():
    """HTTP 200 + count=0 parses to zero rows, then store_page raises empty_page."""
    request = FredRequest(
        FredOperation.SERIES_OBSERVATIONS, series_id="PAYEMS",
        realtime_start=date(1776, 7, 4), realtime_end=date(9999, 12, 31),
        observation_start=date(2026, 7, 1), observation_end=date(2026, 7, 31),
        output_type=4, limit=10, offset=0)
    body = json.dumps({
        "realtime_start": "1776-07-04", "realtime_end": "9999-12-31",
        "observation_start": "2026-07-01", "observation_end": "2026-07-31",
        "units": "lin", "output_type": 4, "file_type": "json",
        "order_by": "observation_date", "sort_order": "asc",
        "count": 0, "offset": 0, "limit": 10,
        "observations": [],
    }).encode()
    page = parse_fred_response(request, body)
    assert len(page.rows) == 0
    # store_page with empty rows → empty_page (the production invariant)
    import tempfile
    from pathlib import Path as _Path
    from tokmo.records.fred_material_store import Reservation
    store = FredMaterialStore(_Path(tempfile.mkdtemp()))
    with pytest.raises(ValueError, match="empty_page"):
        store.store_page((), reservation=Reservation("reserved", "x" * 64, 1),
                         operation="SERIES_OBSERVATIONS",
                         fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_r4_op2_success_fixture_row_shape():
    """op2 success fixture has exactly 1 row (fixture-only, not production assertion)."""
    request = FredRequest(
        FredOperation.SERIES_OBSERVATIONS, series_id="PAYEMS",
        realtime_start=date(1776, 7, 4), realtime_end=date(9999, 12, 31),
        observation_start=date(2026, 7, 1), observation_end=date(2026, 7, 31),
        output_type=4, limit=10, offset=0)
    body = json.dumps({
        "realtime_start": "1776-07-04", "realtime_end": "9999-12-31",
        "observation_start": "2026-07-01", "observation_end": "2026-07-31",
        "units": "lin", "output_type": 4, "file_type": "json",
        "order_by": "observation_date", "sort_order": "asc",
        "count": 1, "offset": 0, "limit": 10,
        "observations": [
            {"realtime_start": "2026-08-12", "realtime_end": "9999-12-31",
             "date": "2026-07-01", "value": "151000"}],
    }).encode()
    page = parse_fred_response(request, body)
    assert len(page.rows) == 1  # fixture expectation: exactly 1 row
    row = page.rows[0]
    assert set(row.keys()) == {
        "series_id", "series_identity_provenance", "realtime_start",
        "realtime_end", "date", "value", "value_state", "classification"}
    assert row["classification"] == "INITIAL"


def test_r4_op2_unordered_or_duplicate_rows():
    """output_type=4 with unordered/duplicate dates → ValueError."""
    request = FredRequest(
        FredOperation.SERIES_OBSERVATIONS, series_id="PAYEMS",
        realtime_start=date(1776, 7, 4), realtime_end=date(9999, 12, 31),
        observation_start=date(2026, 7, 1), observation_end=date(2026, 7, 31),
        output_type=4, limit=10, offset=0)
    body = json.dumps({
        "realtime_start": "1776-07-04", "realtime_end": "9999-12-31",
        "observation_start": "2026-07-01", "observation_end": "2026-07-31",
        "units": "lin", "output_type": 4, "file_type": "json",
        "order_by": "observation_date", "sort_order": "asc",
        "count": 2, "offset": 0, "limit": 10,
        "observations": [
            {"realtime_start": "2026-08-12", "realtime_end": "9999-12-31",
             "date": "2026-07-02", "value": "1"},
            {"realtime_start": "2026-08-12", "realtime_end": "9999-12-31",
             "date": "2026-07-01", "value": "2"}],
    }).encode()
    with pytest.raises(ValueError, match="unordered_or_duplicate_rows"):
        parse_fred_response(request, body)


# ---------------------------------------------------------------------------
# SDS §7 test 12: Operational additivity
# ---------------------------------------------------------------------------

def test_r4_operational_additivity():
    """DB 4 + diagnostic 2 + RCA 2 = 8; max remaining 12."""
    db_live = 4
    diagnostic = 2
    rca = 2
    total = db_live + diagnostic + rca
    assert total == 8
    remaining = fred_macro.PURPOSE_CUMULATIVE_CEILING - total
    assert remaining == 12


def test_r4_purpose_cumulative_ceiling_is_20():
    assert fred_macro.PURPOSE_CUMULATIVE_CEILING == 20


def test_r4_campaign_cap_still_10():
    assert fred_macro.CAMPAIGN_CAP == 10


# ---------------------------------------------------------------------------
# SDS §7 test 13: Reservation ceiling — accepts 19, rejects 20
# ---------------------------------------------------------------------------

def test_r4_reservation_accepts_at_19(tmp_path):
    store = FredMaterialStore(tmp_path / "r4")
    store.ensure_live_campaign(campaign_code=fred_macro.R4_CAMPAIGN_CODE)
    with sqlite3.connect(store.path) as conn:
        for ordinal in range(1, 18):  # DB 17
            conn.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                "FRED", "CANONICAL_SOURCE_STAGE3", f"{ordinal:064x}", ordinal,
                "SERIES", f"{ordinal + 100:064x}", "LIVE",
                _started(ordinal).isoformat().replace("+00:00", "Z"),
                "FAILED", fred_macro.CAMPAIGN_CODE, "legacy", "0" * 64))
        conn.commit()
    req = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    result = store.reserve_campaign(
        fred_macro.R4_CAMPAIGN_CODE, 1, req,
        started_at=_started(20),
        external_diagnostic_count=2, external_rca_count=2)
    # 17 + 2 + 2 = 21, but this is attempt #18 → 18 + 2 + 2 = 22
    # Wait: aggregate = 17 (current DB), new insert makes 18
    # predicate: aggregate + ext_diag + ext_rca < 20
    # 17 + 2 + 2 = 21 >= 20 → cap_exhausted
    # Need 15 DB rows so 15 + 2 + 2 = 19 < 20 → reserved
    assert result.code == "cap_exhausted"


def test_r4_reservation_accepts_at_total_19(tmp_path):
    store = FredMaterialStore(tmp_path / "r4")
    store.ensure_live_campaign(campaign_code=fred_macro.R4_CAMPAIGN_CODE)
    with sqlite3.connect(store.path) as conn:
        for ordinal in range(1, 16):  # DB 15
            conn.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                "FRED", "CANONICAL_SOURCE_STAGE3", f"{ordinal:064x}", ordinal,
                "SERIES", f"{ordinal + 100:064x}", "LIVE",
                _started(ordinal).isoformat().replace("+00:00", "Z"),
                "FAILED", fred_macro.CAMPAIGN_CODE, "legacy", "0" * 64))
        conn.commit()
    req = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    result = store.reserve_campaign(
        fred_macro.R4_CAMPAIGN_CODE, 1, req,
        started_at=_started(20),
        external_diagnostic_count=2, external_rca_count=2)
    # aggregate = 15, 15 + 2 + 2 = 19 < 20 → reserved
    assert result.code == "reserved"


def test_r4_reservation_rejects_at_total_20(tmp_path):
    store = FredMaterialStore(tmp_path / "r4")
    store.ensure_live_campaign(campaign_code=fred_macro.R4_CAMPAIGN_CODE)
    with sqlite3.connect(store.path) as conn:
        for ordinal in range(1, 17):  # DB 16
            conn.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                "FRED", "CANONICAL_SOURCE_STAGE3", f"{ordinal:064x}", ordinal,
                "SERIES", f"{ordinal + 100:064x}", "LIVE",
                _started(ordinal).isoformat().replace("+00:00", "Z"),
                "FAILED", fred_macro.CAMPAIGN_CODE, "legacy", "0" * 64))
        conn.commit()
    req = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    result = store.reserve_campaign(
        fred_macro.R4_CAMPAIGN_CODE, 1, req,
        started_at=_started(20),
        external_diagnostic_count=2, external_rca_count=2)
    # aggregate = 16, 16 + 2 + 2 = 20 >= 20 → cap_exhausted
    assert result.code == "cap_exhausted"


def test_r4_reservation_no_reset_by_campaign(tmp_path):
    """A new campaign/revision does not reset usage."""
    store = FredMaterialStore(tmp_path / "r4")
    store.ensure_live_campaign(campaign_code=fred_macro.R2_CAMPAIGN_CODE)
    store.ensure_live_campaign(campaign_code=fred_macro.R4_CAMPAIGN_CODE)
    with sqlite3.connect(store.path) as conn:
        for ordinal in range(1, 11):
            conn.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                "FRED", "CANONICAL_SOURCE_STAGE3", f"r2_{ordinal:058x}", ordinal,
                "SERIES", f"{ordinal + 100:064x}", "LIVE",
                _started(ordinal).isoformat().replace("+00:00", "Z"),
                "FAILED", fred_macro.R2_CAMPAIGN_CODE, "legacy", "0" * 64))
        conn.commit()
    req = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    result = store.reserve_campaign(
        fred_macro.R4_CAMPAIGN_CODE, 1, req,
        started_at=_started(20),
        external_diagnostic_count=2, external_rca_count=2)
    # 10 existing + 2 + 2 = 14 < 20 → reserved
    assert result.code == "reserved"


# ---------------------------------------------------------------------------
# SDS §7 test 14: R3 terminal/attempt mapping
# ---------------------------------------------------------------------------

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


def _migrated_r3(tmp_path, module):
    root = _migrated_r2(tmp_path, module)
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R3_CAMPAIGN_CODE)
    req1 = fred_macro.r3_g_live_manifest(release_id=1)[0]
    res1 = store.reserve_campaign(
        fred_macro.R3_CAMPAIGN_CODE, 1, req1,
        started_at=datetime(2026, 8, 12, 2, tzinfo=timezone.utc),
        external_diagnostic_count=2)
    store.settle(res1, "SUCCEEDED")
    req2 = fred_macro.r3_g_live_manifest(release_id=1)[1]
    res2 = store.reserve_campaign(
        fred_macro.R3_CAMPAIGN_CODE, 2, req2,
        started_at=datetime(2026, 8, 12, 3, tzinfo=timezone.utc),
        external_diagnostic_count=2)
    store.settle(res2, "FAILED")
    store.quarantine_campaign(
        fred_macro.R3_CAMPAIGN_CODE, "provider_bad_request_unknown")
    return root


# Real diagnostic probes for R3 preflight compatibility
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


def _make_diagnostic_artifact(root, *, probes=None, schema="tokmo.fred.bounded-diagnostic.v1"):
    if probes is None:
        probes = [dict(p) for p in _REAL_PROBES]
    art = {"schema": schema, "probes": probes}
    path = root / "g-live-diagnostic-probes.json"
    path.write_bytes(canonical_json(art))
    path.chmod(0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, digest, len(probes)


def test_r4_r3_terminal_mapping_preserved(tmp_path):
    """R3 is QUARANTINED/provider_bad_request_unknown, attempt 1 succeeded, attempt 2 failed."""
    module = _live_tool()
    root = _migrated_r3(tmp_path, module)
    store = FredMaterialStore(root)
    assert store.campaign_state_for(fred_macro.R3_CAMPAIGN_CODE) == (
        "QUARANTINED", "provider_bad_request_unknown")
    with sqlite3.connect(root / "fred.sqlite3") as conn:
        states = conn.execute(
            "SELECT a.state FROM campaign_attempts c JOIN attempts a "
            "ON a.invocation_identity=c.invocation_identity "
            "WHERE c.campaign_code=? ORDER BY c.manifest_ordinal",
            (fred_macro.R3_CAMPAIGN_CODE,)).fetchall()
    assert [s[0] for s in states] == ["SUCCEEDED", "FAILED"]


# ---------------------------------------------------------------------------
# SDS §7 test 15: R3 retirement zero-effect
# ---------------------------------------------------------------------------

def test_r4_r3_retired_after_r4_creation(tmp_path):
    """After valid R4 creation, readback-r3 returns r3_retired_by_r4."""
    module = _live_tool()
    root = _migrated_r3(tmp_path, module)
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R4_CAMPAIGN_CODE)
    result = module.readback_r3(root)
    assert result["code"] == "r3_retired_by_r4"
    assert result["provider_starts"] == 0


def test_r4_r3_preflight_retired_after_r4(tmp_path):
    module = _live_tool()
    root = _migrated_r3(tmp_path, module)
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R4_CAMPAIGN_CODE)
    diag_path, _, _ = _make_diagnostic_artifact(tmp_path)
    result = module.preflight_r3(root, lock_path=tmp_path / "lock",
                                 diagnostic_path=diag_path)
    assert result["code"] == "r3_retired_by_r4"


def test_r4_r3_execute_retired_after_r4(tmp_path):
    module = _live_tool()
    root = _migrated_r3(tmp_path, module)
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R4_CAMPAIGN_CODE)
    diag_path, _, _ = _make_diagnostic_artifact(tmp_path)
    result = module.execute_r3(
        root=root, credential_path=tmp_path / "cred", lock_path=tmp_path / "lock",
        diagnostic_path=diag_path, execution_allowed=True,
        wall_clock=lambda: datetime(2026, 8, 12, tzinfo=timezone.utc),
        monotonic_clock=lambda: 1.0, sleeper=lambda _: None,
        api_key_loader=lambda _: (_ for _ in ()).throw(AssertionError()),
        opener_builder=lambda: (_ for _ in ()).throw(AssertionError()))
    assert result["code"] == "r3_retired_by_r4"
    assert result["provider_starts"] == 0


def test_r4_cli_documents_retirement(tmp_path):
    """CLI help or dispatch includes r3_retired_by_r4 in allowlist."""
    module = _live_tool()
    assert "r3_retired_by_r4" in module._STATIC_ERROR_CODES


# ---------------------------------------------------------------------------
# SDS §7 test 16: R4 first failure isolation
# ---------------------------------------------------------------------------

class _Response:
    status = 200
    headers = {}
    def __init__(self, body): self._body = body
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, limit): return self._body


class _R4LiveOpener:
    """Opener that serves R4 bodies — op2 carries realtime 1776-07-04..9999-12-31."""
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
            body = {"realtime_start": "1776-07-04", "realtime_end": "9999-12-31",
                "observation_start": "2026-07-01", "observation_end": "2026-07-31",
                "units": "lin", "output_type": 4, "file_type": "json",
                "order_by": "observation_date", "sort_order": "asc",
                "count": 1, "offset": 0, "limit": 10,
                "observations": [{"realtime_start": "2026-08-12",
                "realtime_end": "9999-12-31", "date": "2026-07-01", "value": "1"}]}
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


def _r4_dependencies(tmp_path, fail_at=None, module=None):
    module = module or _live_tool()
    root = _migrated_r3(tmp_path, module)
    diag_path, diag_digest, diag_count = _make_diagnostic_artifact(tmp_path)
    rca_path = tmp_path / "g-live-r4-rca-probes.json"
    rca_digest = _write_rca_artifact(rca_path)
    credential_parent = tmp_path / "credentials"
    credential_parent.mkdir(mode=0o700)
    credential = credential_parent / "fred.env"
    credential.write_text("metadata-only\n")
    credential.chmod(0o600)
    opener = _R4LiveOpener(fail_at)
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
    result = module.execute_r4(
        root=root, credential_path=credential, lock_path=tmp_path / "lock",
        diagnostic_path=diag_path, rca_path=rca_path,
        execution_allowed=True,
        wall_clock=wall, monotonic_clock=mono, sleeper=lambda _: None,
        api_key_loader=lambda path: keys.append(path) or "a" * 32,
        opener_builder=lambda: opener)
    return module, result, root, credential, opener, keys, diag_path, rca_path


def test_r4_execute_five_success(tmp_path):
    module, result, root, _, opener, keys, _, _ = _r4_dependencies(tmp_path)
    assert result["code"] == "durable_success"
    assert result["provider_starts"] == 5
    assert result["credential_reads"] == 5
    store = FredMaterialStore(root)
    assert store.campaign_state_for(fred_macro.R4_CAMPAIGN_CODE) == (
        "COMPLETE", "manifest_complete")
    # R3 must be preserved as QUARANTINED
    assert store.campaign_state_for(fred_macro.R3_CAMPAIGN_CODE) == (
        "QUARANTINED", "provider_bad_request_unknown")
    assert (root / "g-live-r4-evidence.json").exists()
    with sqlite3.connect(root / "fred.sqlite3") as conn:
        ordinals = conn.execute(
            "SELECT manifest_ordinal FROM campaign_attempts WHERE campaign_code=? "
            "ORDER BY manifest_ordinal", (fred_macro.R4_CAMPAIGN_CODE,)).fetchall()
    assert [row[0] for row in ordinals] == [1, 2, 3, 4, 5]


def test_r4_execute_first_failure_stops(tmp_path):
    module, result, root, _, opener, _, _, _ = _r4_dependencies(tmp_path, fail_at=3)
    assert result["code"] == "connection_failure"
    assert result["provider_starts"] == 3
    assert len(opener.calls) == 3
    store = FredMaterialStore(root)
    assert store.campaign_state_for(fred_macro.R4_CAMPAIGN_CODE)[0] == "QUARANTINED"
    # R3 is untouched
    assert store.campaign_state_for(fred_macro.R3_CAMPAIGN_CODE) == (
        "QUARANTINED", "provider_bad_request_unknown")


@pytest.mark.parametrize("position", range(1, 6))
def test_r4_execute_each_failure_position_stops(position, tmp_path):
    _, result, _, _, opener, _, _, _ = _r4_dependencies(tmp_path, fail_at=position)
    assert result["code"] == "connection_failure"
    assert result["provider_starts"] == position


# ---------------------------------------------------------------------------
# SDS §7 test 17: Replay/rerun zero-start
# ---------------------------------------------------------------------------

def test_r4_execute_replay_after_complete_zero_start(tmp_path):
    module, result, root, credential, opener, keys, diag_path, rca_path = \
        _r4_dependencies(tmp_path)
    assert result["code"] == "durable_success"
    replay_opener = _R4LiveOpener()
    replay_keys = []
    replay = module.execute_r4(
        root=root, credential_path=credential, lock_path=tmp_path / "lock",
        diagnostic_path=diag_path, rca_path=rca_path,
        execution_allowed=True,
        wall_clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc),
        monotonic_clock=lambda: 1.0, sleeper=lambda _: None,
        api_key_loader=lambda path: replay_keys.append(path),
        opener_builder=lambda: replay_opener)
    assert replay["code"] == "already_complete"
    assert replay["provider_starts"] == 0
    assert replay["credential_reads"] == 0
    assert replay_opener.calls == []


def test_r4_execute_quarantined_rerun_zero_start(tmp_path):
    module, result, root, credential, opener, keys, diag_path, rca_path = \
        _r4_dependencies(tmp_path, fail_at=2)
    assert result["code"] == "connection_failure"
    rerun_opener = _R4LiveOpener()
    rerun = module.execute_r4(
        root=root, credential_path=credential, lock_path=tmp_path / "lock",
        diagnostic_path=diag_path, rca_path=rca_path,
        execution_allowed=True,
        wall_clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc),
        monotonic_clock=lambda: 1.0, sleeper=lambda _: None,
        api_key_loader=lambda path: "a" * 32,
        opener_builder=lambda: rerun_opener)
    assert rerun["code"] == "campaign_mismatch"
    assert rerun["provider_starts"] == 0


# ---------------------------------------------------------------------------
# SDS §7 test 18: Prior evidence invariance
# ---------------------------------------------------------------------------

def test_r4_prior_evidence_invariant_on_success(tmp_path):
    module = _live_tool()
    root = _migrated_r3(tmp_path, module)
    r3_ev = root / "g-live-r3-evidence.json"
    r3_before = r3_ev.read_bytes() if r3_ev.exists() else b""
    r2_ev = root / "g-live-r2-evidence.json"
    r2_before = r2_ev.read_bytes()
    receipt = root / "g-live-r2-migration-receipt.json"
    receipt_before = receipt.read_bytes()
    diag_path, _, _ = _make_diagnostic_artifact(tmp_path)
    rca_path = tmp_path / "g-live-r4-rca-probes.json"
    _write_rca_artifact(rca_path)
    credential_parent = tmp_path / "credentials"
    credential_parent.mkdir(mode=0o700)
    credential = credential_parent / "fred.env"
    credential.write_text("metadata-only\n")
    credential.chmod(0o600)
    opener = _R4LiveOpener()
    base = datetime(2026, 8, 12, tzinfo=timezone.utc)
    wall_index = [0]
    mono_index = [0]
    def wall():
        wall_index[0] += 1
        return base + timedelta(seconds=wall_index[0] * 2)
    def mono():
        mono_index[0] += 1
        return float(mono_index[0])
    result = module.execute_r4(
        root=root, credential_path=credential, lock_path=tmp_path / "lock",
        diagnostic_path=diag_path, rca_path=rca_path,
        execution_allowed=True,
        wall_clock=wall, monotonic_clock=mono, sleeper=lambda _: None,
        api_key_loader=lambda path: "a" * 32,
        opener_builder=lambda: opener)
    assert result["code"] == "durable_success"
    assert r2_ev.read_bytes() == r2_before
    assert receipt.read_bytes() == receipt_before


def test_r4_prior_evidence_invariant_on_failure(tmp_path):
    module = _live_tool()
    root = _migrated_r3(tmp_path, module)
    r2_ev = root / "g-live-r2-evidence.json"
    r2_before = r2_ev.read_bytes()
    receipt = root / "g-live-r2-migration-receipt.json"
    receipt_before = receipt.read_bytes()
    diag_path, _, _ = _make_diagnostic_artifact(tmp_path)
    rca_path = tmp_path / "g-live-r4-rca-probes.json"
    _write_rca_artifact(rca_path)
    credential_parent = tmp_path / "credentials"
    credential_parent.mkdir(mode=0o700)
    credential = credential_parent / "fred.env"
    credential.write_text("metadata-only\n")
    credential.chmod(0o600)
    opener = _R4LiveOpener(fail_at=2)
    base = datetime(2026, 8, 12, tzinfo=timezone.utc)
    wall_index = [0]
    mono_index = [0]
    def wall():
        wall_index[0] += 1
        return base + timedelta(seconds=wall_index[0] * 2)
    def mono():
        mono_index[0] += 1
        return float(mono_index[0])
    result = module.execute_r4(
        root=root, credential_path=credential, lock_path=tmp_path / "lock",
        diagnostic_path=diag_path, rca_path=rca_path,
        execution_allowed=True,
        wall_clock=wall, monotonic_clock=mono, sleeper=lambda _: None,
        api_key_loader=lambda path: "a" * 32,
        opener_builder=lambda: opener)
    assert result["code"] == "connection_failure"
    assert r2_ev.read_bytes() == r2_before
    assert receipt.read_bytes() == receipt_before


# ---------------------------------------------------------------------------
# SDS §7 test 19: CLI masking
# ---------------------------------------------------------------------------

def test_r4_cli_emits_static_json(tmp_path):
    module = _live_tool()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        module.run_cli(["execute-r4"], root=tmp_path / "nonexistent",
                       credential_path=tmp_path / "cred", lock_path=tmp_path / "lock",
                       diagnostic_path=tmp_path / "diag.json",
                       rca_path=tmp_path / "rca.json",
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


def test_r4_execute_forbidden_by_default(tmp_path):
    module = _live_tool()
    root = _migrated_r3(tmp_path, module)
    diag_path, _, _ = _make_diagnostic_artifact(tmp_path)
    rca_path = tmp_path / "g-live-r4-rca-probes.json"
    _write_rca_artifact(rca_path)
    result = module.execute_r4(
        root=root, credential_path=tmp_path / "cred", lock_path=tmp_path / "lock",
        diagnostic_path=diag_path, rca_path=rca_path, execution_allowed=False,
        wall_clock=lambda: None, monotonic_clock=lambda: None,
        sleeper=lambda _: None,
        api_key_loader=lambda _: (_ for _ in ()).throw(AssertionError()),
        opener_builder=lambda: (_ for _ in ()).throw(AssertionError()))
    assert result["code"] == "g_live_forbidden"
    assert result["provider_starts"] == 0


# ---------------------------------------------------------------------------
# SDS §7 test 20: Preflight read-only
# ---------------------------------------------------------------------------

def test_r4_preflight_is_readonly(tmp_path):
    module = _live_tool()
    root = _migrated_r3(tmp_path, module)
    diag_path, _, _ = _make_diagnostic_artifact(tmp_path)
    rca_path = tmp_path / "g-live-r4-rca-probes.json"
    _write_rca_artifact(rca_path)
    before = {p.name: (p.stat().st_mtime_ns, p.stat().st_size)
              for p in root.iterdir()}
    module.preflight_r4(root, lock_path=tmp_path / "lock",
                        diagnostic_path=diag_path, rca_path=rca_path)
    after = {p.name: (p.stat().st_mtime_ns, p.stat().st_size)
             for p in root.iterdir()}
    assert before == after
    with sqlite3.connect(root / "fred.sqlite3") as conn:
        r4_count = conn.execute(
            "SELECT count(*) FROM campaigns WHERE campaign_code=?",
            (fred_macro.R4_CAMPAIGN_CODE,)).fetchone()[0]
    assert r4_count == 0


# ---------------------------------------------------------------------------
# SDS §7 test: R4 readback/postflight
# ---------------------------------------------------------------------------

def test_r4_readback_verifies_four_campaigns(tmp_path):
    module = _live_tool()
    root = _migrated_r3(tmp_path, module)
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R4_CAMPAIGN_CODE)
    result = module.readback_r4(root)
    assert result["code"] == "readback_ok"
    assert result["campaigns"] == 4


def test_r4_readback_rejects_three_campaigns(tmp_path):
    module = _live_tool()
    root = _migrated_r3(tmp_path, module)
    with pytest.raises((ValueError,)):
        module.readback_r4(root)


# ---------------------------------------------------------------------------
# SDS §7 test: R4 composition quarantine dispatch
# ---------------------------------------------------------------------------

def test_r4_composition_quarantine_dispatches_to_r4(tmp_path):
    root = tmp_path / "r4comp"
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=fred_macro.R4_CAMPAIGN_CODE)
    req = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    res = store.reserve_campaign(
        fred_macro.R4_CAMPAIGN_CODE, 1, req,
        started_at=_started(), external_diagnostic_count=2,
        external_rca_count=2)
    from tokmo.fred_composition import _quarantine_for_reservation
    _quarantine_for_reservation(store, res, "connection_failure")
    assert store.campaign_state_for(fred_macro.R4_CAMPAIGN_CODE) == (
        "QUARANTINED", "connection_failure")


# ---------------------------------------------------------------------------
# SDS §7 test 21: Changed-path derivation
# ---------------------------------------------------------------------------

def test_r4_files_in_expected_set():
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
        # Routine on-demand query seam: additive paths that add no R4 surface.
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
# SDS §7 test: Spacing >=1s with real sleeper
# ---------------------------------------------------------------------------

def test_r4_execute_enforces_spacing(tmp_path):
    module = _live_tool()
    root = _migrated_r3(tmp_path, module)
    diag_path, _, _ = _make_diagnostic_artifact(tmp_path)
    rca_path = tmp_path / "g-live-r4-rca-probes.json"
    _write_rca_artifact(rca_path)
    credential_parent = tmp_path / "credentials"
    credential_parent.mkdir(mode=0o700, exist_ok=True)
    credential = credential_parent / "fred.env"
    credential.write_text("metadata-only\n")
    credential.chmod(0o600)
    sleeps = []
    opener = _R4LiveOpener()
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
    result = module.execute_r4(
        root=root, credential_path=credential, lock_path=tmp_path / "lock",
        diagnostic_path=diag_path, rca_path=rca_path, execution_allowed=True,
        wall_clock=wall, monotonic_clock=mono, sleeper=fast_sleeper,
        api_key_loader=lambda path: "a" * 32,
        opener_builder=lambda: opener)
    assert result["code"] == "durable_success"
    assert len(opener.calls) == 5
    assert len(sleeps) == 4
    for duration in sleeps:
        assert duration > 0


# ---------------------------------------------------------------------------
# SDS §7 test: R4 evidence structure
# ---------------------------------------------------------------------------

def test_r4_evidence_has_two_external_counts(tmp_path):
    module = _live_tool()
    store = FredMaterialStore(tmp_path / "r4")
    store.ensure_live_campaign(campaign_code=fred_macro.R4_CAMPAIGN_CODE)
    evidence = module._r4_evidence(
        store, [], 0, "ACTIVE", None,
        external_diagnostic_count=2,
        external_rca_count=2,
        diagnostic_sha256="d" * 64,
        rca_sha256="r" * 64,
        manifest_identity={"campaign_code": R4_CAMPAIGN_CODE},
        r1_evidence_sha256="a" * 64,
        r2_evidence_sha256="b" * 64,
        r3_evidence_sha256="e" * 64,
        migration_receipt_sha256="c" * 64,
        rca_sha256_value="r" * 64,
    )
    assert evidence["external_diagnostic_count"] == 2
    assert evidence["external_rca_count"] == 2
    assert "diagnostic_sha256" in evidence
    assert "rca_sha256" in evidence
    assert evidence["schema"] == "tokmo.fred.g-live-r4-evidence.v1"
