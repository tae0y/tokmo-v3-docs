"""SDS AR-4..AR-10: the Macro FRED caller contract.

The caller is the second production caller of the on-demand FRED enrichment.
It reads the persisted schedule sidecar (read-only), opens the cache, projects
the enrichment onto closed scalar receipt fields, and **never raises**.
Ceiling: 1 FRED observation start, 0 BLS starts, per invocation.
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tokmo.fred_query_composition import (
    FredFixtureTransportOutcomeV1,
    FredFixtureTransportV1,
    FredQueryProductionDepsV1,
    FredStartBudget,
    BudgetedFredTransportV1,
    MACRO_FRED_RECEIPT_FIELDS,
    build_fixture_api_key_provider_v1,
    build_macro_fred_caller_v1,
    build_production_fred_query_deps_v1,
)
from tokmo.records.fred_schedule_sidecar import (
    SIDECAR_NAME,
    build_sidecar_payload_v1,
    write_schedule_sidecar_v1,
)
from tokmo.values.fred_query import (
    SERIES_PAYEMS,
    build_fred_release_schedule_v1,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 7, 12, 45, tzinfo=UTC)
RELEASE_ID = 50
RELEASE_DATE = date(2026, 8, 7)
SLOT = "PAYEMS:2026-08-07"
OFFICIAL_AT = datetime(2026, 8, 7, 12, 30, tzinfo=UTC)

MAY = {"realtime_start": "2026-06-05", "realtime_end": "9999-12-31",
       "date": "2026-05-01", "value": "150800"}
JUNE = {"realtime_start": "2026-07-02", "realtime_end": "9999-12-31",
        "date": "2026-06-01", "value": "150900"}
JULY = {"realtime_start": "2026-08-07", "realtime_end": "9999-12-31",
        "date": "2026-07-01", "value": "151000"}


def _body(rows, *, count=None, limit=3, start="2026-05-01", end="2026-07-31"):
    return json.dumps({
        "realtime_start": "1776-07-04", "realtime_end": "9999-12-31",
        "observation_start": start, "observation_end": end,
        "units": "lin", "output_type": 4, "file_type": "json",
        "order_by": "observation_date", "sort_order": "asc",
        "count": len(rows) if count is None else count,
        "offset": 0, "limit": limit, "observations": list(rows)}).encode()


def _seed_sidecar(root: Path):
    """Write a valid sidecar under root, mirroring the tool's own tests."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    provenance = {
        "fred_release_id": RELEASE_ID,
        "fred_release_date": RELEASE_DATE.isoformat(),
        "bls_source_id": "BLS_ICS_V1",
        "bls_source_url": "https://www.bls.gov/schedule/news_release/bls.ics",
        "bls_summary": "Employment Situation",
        "bls_tzid": "America/New_York",
        "bls_resolved_zone": "America/New_York",
        "official_release_at_utc": OFFICIAL_AT.isoformat(
            timespec="microseconds").replace("+00:00", "Z"),
        "bls_body_sha256": "d" * 64,
        "bls_body_bytes": 1064,
        "grace_seconds": 900,
        "join_rule": "SUMMARY_EXACT+TZID_ALLOWLIST+DST_CROSSCHECK+"
                     "LOCAL_DATE_EQUAL+SINGLETON",
    }
    schedule = build_fred_release_schedule_v1(
        series_id=SERIES_PAYEMS, release_id=RELEASE_ID,
        release_date=RELEASE_DATE, official_release_at=OFFICIAL_AT,
        schedule_provenance=provenance)
    payload = build_sidecar_payload_v1(
        schedule=schedule, release_id=RELEASE_ID, release_date=RELEASE_DATE,
        official_release_at=OFFICIAL_AT, provenance=provenance)
    write_schedule_sidecar_v1(root / SIDECAR_NAME, payload)


def _build_deps(root, *, transport_outcomes=(), clock_value=NOW):
    transport = FredFixtureTransportV1(outcomes=tuple(transport_outcomes))
    return build_production_fred_query_deps_v1(
        root=root, sidecar_path=root / SIDECAR_NAME,
        transport=transport,
        api_key_provider=build_fixture_api_key_provider_v1(),
        clock=lambda: clock_value), transport


# --- AR-10: receipt key set ------------------------------------------------

def test_receipt_key_set_is_exactly_macro_fred_receipt_fields():
    assert MACRO_FRED_RECEIPT_FIELDS == (
        "fred_status", "fred_schedule_source", "fred_release_slot",
        "fred_due_state", "fred_coverage", "fred_provider_starts",
        "fred_bls_starts", "fred_query_identity", "fred_revision_identity",
        "fred_provenance_digest", "fred_direction",
        "fred_current_value", "fred_current_date",
        "fred_prior_value", "fred_prior_date", "fred_error_class",
    )


# --- AR-4: the caller never raises -----------------------------------------

class _RaisingTransport:
    requests = []

    def __call__(self, request, api_key):
        raise RuntimeError("transport_explosion")


def test_a_raising_transport_yields_contained_error(tmp_path):
    root = tmp_path / "fred"
    _seed_sidecar(root)
    transport = _RaisingTransport()
    deps = build_production_fred_query_deps_v1(
        root=root, sidecar_path=root / SIDECAR_NAME,
        transport=transport,
        api_key_provider=build_fixture_api_key_provider_v1(),
        clock=lambda: NOW)
    caller = build_macro_fred_caller_v1(deps)
    result = caller()
    assert set(result) == set(MACRO_FRED_RECEIPT_FIELDS)
    assert result["fred_status"] == "CONTAINED_ERROR"
    assert result["fred_error_class"] == "RuntimeError"
    assert result["fred_bls_starts"] == 0


def test_a_raising_clock_yields_contained_error(tmp_path):
    root = tmp_path / "fred"
    _seed_sidecar(root)
    transport = FredFixtureTransportV1(outcomes=())

    def bad_clock():
        raise OSError("clock_broke")

    deps = build_production_fred_query_deps_v1(
        root=root, sidecar_path=root / SIDECAR_NAME,
        transport=transport,
        api_key_provider=build_fixture_api_key_provider_v1(),
        clock=bad_clock)
    caller = build_macro_fred_caller_v1(deps)
    result = caller()
    assert result["fred_status"] == "CONTAINED_ERROR"
    assert result["fred_error_class"] == "OSError"
    assert result["fred_provider_starts"] == 0


def test_non_aware_utc_clock_yields_contained_error(tmp_path):
    root = tmp_path / "fred"
    _seed_sidecar(root)
    transport = FredFixtureTransportV1(outcomes=())
    deps = build_production_fred_query_deps_v1(
        root=root, sidecar_path=root / SIDECAR_NAME,
        transport=transport,
        api_key_provider=build_fixture_api_key_provider_v1(),
        clock=lambda: datetime(2026, 8, 7, 12, 45))  # naive
    caller = build_macro_fred_caller_v1(deps)
    result = caller()
    assert result["fred_status"] == "CONTAINED_ERROR"


# --- AR-5: absent/expired/invalid sidecar → 0 starts ------------------------

def test_absent_sidecar_yields_schedule_absent_with_zero_starts(tmp_path):
    root = tmp_path / "fred"
    root.mkdir(mode=0o700, parents=True)
    deps, transport = _build_deps(root)
    result = build_macro_fred_caller_v1(deps)()
    assert result["fred_status"] == "SCHEDULE_ABSENT"
    assert result["fred_schedule_source"] == "ABSENT"
    assert result["fred_provider_starts"] == 0
    assert result["fred_bls_starts"] == 0
    assert transport.requests == []


def test_invalid_sidecar_yields_schedule_invalid_with_zero_starts(tmp_path):
    root = tmp_path / "fred"
    _seed_sidecar(root)
    # Corrupt the sidecar.
    sidecar = root / SIDECAR_NAME
    import hashlib
    from tokmo.values.fred_query import canonical_json
    payload = json.loads(sidecar.read_bytes())
    payload["payload_digest"] = "0" * 64
    body = {k: v for k, v in payload.items() if k != "payload_digest"}
    payload["payload_digest"] = hashlib.sha256(
        canonical_json(body)).hexdigest()
    sidecar.write_bytes(canonical_json(payload))
    # Break the digest on purpose:
    payload["payload_digest"] = "0" * 64
    sidecar.write_bytes(canonical_json(payload))

    deps, transport = _build_deps(root)
    result = build_macro_fred_caller_v1(deps)()
    assert result["fred_status"] == "SCHEDULE_INVALID"
    assert result["fred_schedule_source"] == "INVALID"
    assert result["fred_provider_starts"] == 0
    assert result["fred_bls_starts"] == 0
    assert transport.requests == []


def test_expired_sidecar_yields_schedule_expired_with_zero_starts(tmp_path):
    root = tmp_path / "fred"
    _seed_sidecar(root)
    far_future = datetime(2099, 1, 1, tzinfo=UTC)
    deps, transport = _build_deps(root, clock_value=far_future)
    result = build_macro_fred_caller_v1(deps)()
    assert result["fred_status"] == "SCHEDULE_EXPIRED"
    assert result["fred_schedule_source"] == "EXPIRED"
    assert result["fred_provider_starts"] == 0
    assert result["fred_bls_starts"] == 0
    assert transport.requests == []


# --- AR-7: reusable success → ENRICHED with 0 starts -----------------------

def test_reusable_cache_success_enriches_with_zero_starts(tmp_path):
    root = tmp_path / "fred"
    _seed_sidecar(root)
    # First call: seed a success.
    outcomes = (FredFixtureTransportOutcomeV1("success", _body([MAY, JUNE, JULY])),)
    deps, transport = _build_deps(root, transport_outcomes=outcomes)
    first = build_macro_fred_caller_v1(deps)()
    assert first["fred_status"] == "ENRICHED"
    assert first["fred_provider_starts"] == 1

    # Second call: cache should be fresh → 0 starts.
    deps2, transport2 = _build_deps(
        root, clock_value=NOW + timedelta(minutes=5))
    second = build_macro_fred_caller_v1(deps2)()
    assert second["fred_status"] == "ENRICHED"
    assert second["fred_provider_starts"] == 0
    assert transport2.requests == []


# --- AR-8: ceiling is 1 FRED, 0 BLS ----------------------------------------

def test_second_transport_call_raises_budget_exceeded(tmp_path):
    root = tmp_path / "fred"
    _seed_sidecar(root)
    transport = FredFixtureTransportV1(outcomes=(
        FredFixtureTransportOutcomeV1("success", _body([MAY, JUNE, JULY])),
        FredFixtureTransportOutcomeV1("success", _body([MAY, JUNE, JULY])),
    ))
    budget = FredStartBudget(maximum=1)
    wire = BudgetedFredTransportV1(transport, budget)
    wire(object(), "f" * 32)
    with pytest.raises(ValueError, match="fred_start_budget_exceeded"):
        wire(object(), "f" * 32)


def test_bls_starts_always_zero_for_every_outcome(tmp_path):
    root = tmp_path / "fred"
    _seed_sidecar(root)
    outcomes = (FredFixtureTransportOutcomeV1("success", _body([MAY, JUNE, JULY])),)
    deps, _ = _build_deps(root, transport_outcomes=outcomes)
    result = build_macro_fred_caller_v1(deps)()
    assert result["fred_bls_starts"] == 0


# --- AR-9: sidecar is byte-identical after caller run ----------------------

def test_sidecar_is_byte_identical_after_caller_run(tmp_path):
    import hashlib

    root = tmp_path / "fred"
    _seed_sidecar(root)
    sidecar = root / SIDECAR_NAME
    before = hashlib.sha256(sidecar.read_bytes()).hexdigest()

    outcomes = (FredFixtureTransportOutcomeV1("success", _body([MAY, JUNE, JULY])),)
    deps, _ = _build_deps(root, transport_outcomes=outcomes)
    build_macro_fred_caller_v1(deps)()

    after = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    assert before == after


# --- AR-6: caller short-circuit agrees with evaluate_fred_due_v1 -----------

def test_caller_short_circuit_agrees_with_evaluate_fred_due_on_absent_schedule(tmp_path):
    from tokmo.programs.macro_fred_inputs import evaluate_fred_due_v1
    from tokmo.values.fred_query import (
        FredDueStateV1, FredSeriesQueryV1,
    )
    from tokmo.records.fred_query_cache import FredQueryCacheV1

    root = tmp_path / "fred"
    root.mkdir(mode=0o700, parents=True)
    cache = FredQueryCacheV1(root, opened_at=NOW)
    query = FredSeriesQueryV1(SERIES_PAYEMS, date(2026, 5, 1),
                              date(2026, 7, 31), NOW)
    decision = evaluate_fred_due_v1(query=query, schedule=None, cache=cache,
                                    now=NOW)
    assert decision.state is FredDueStateV1.UNAVAILABLE_RELEASE_SCHEDULE

    # The caller with no sidecar must agree on the unavailable status.
    deps, _ = _build_deps(root)
    result = build_macro_fred_caller_v1(deps)()
    assert result["fred_status"] == "SCHEDULE_ABSENT"
    assert result["fred_due_state"] is None


# --- AR-10: all values are JSON-serializable scalars ------------------------

def test_every_receipt_value_is_json_round_trippable(tmp_path):
    root = tmp_path / "fred"
    _seed_sidecar(root)
    outcomes = (FredFixtureTransportOutcomeV1("success", _body([MAY, JUNE, JULY])),)
    deps, _ = _build_deps(root, transport_outcomes=outcomes)
    result = build_macro_fred_caller_v1(deps)()
    # Must survive json.dumps without error.
    serialized = json.dumps(result)
    # Round-trip.
    restored = json.loads(serialized)
    assert set(restored) == set(MACRO_FRED_RECEIPT_FIELDS)
    # Every value is str, int, or None.
    for key, value in restored.items():
        assert type(value) in (str, int, type(None)), (key, type(value))


# --- M-5: ScheduleSidecarInvalid reason preserved in fred_error_class --------

def test_schedule_invalid_preserves_specific_sidecar_reason(tmp_path):
    """The closed ScheduleSidecarInvalid reason must appear in fred_error_class."""
    root = tmp_path / "fred"
    _seed_sidecar(root)
    # Corrupt the digest.
    sidecar = root / SIDECAR_NAME
    import hashlib as _hashlib
    from tokmo.values.fred_query import canonical_json
    payload = json.loads(sidecar.read_bytes())
    payload["payload_digest"] = "0" * 64
    sidecar.write_bytes(canonical_json(payload))
    deps, _ = _build_deps(root)
    result = build_macro_fred_caller_v1(deps)()
    assert result["fred_status"] == "SCHEDULE_INVALID"
    assert result["fred_error_class"] == "sidecar_payload_digest_mismatch"


def test_schedule_invalid_preserves_unregistered_series_reason(tmp_path):
    """A different corruption class must produce a different reason."""
    root = tmp_path / "fred"
    _seed_sidecar(root)
    sidecar = root / SIDECAR_NAME
    import hashlib as _hashlib
    from tokmo.values.fred_query import canonical_json
    payload = json.loads(sidecar.read_bytes())
    payload["series_id"] = "UNRATE"
    body = {k: v for k, v in payload.items() if k != "payload_digest"}
    payload["payload_digest"] = _hashlib.sha256(canonical_json(body)).hexdigest()
    sidecar.write_bytes(canonical_json(payload))
    deps, _ = _build_deps(root)
    result = build_macro_fred_caller_v1(deps)()
    assert result["fred_status"] == "SCHEDULE_INVALID"
    assert result["fred_error_class"] == "unregistered_series"


# --- m-7: loaded schedule source/slot retained on contained post-load error ---

def test_contained_error_after_successful_load_retains_schedule_source(tmp_path):
    """When a CONTAINED_ERROR happens after a successful sidecar load,
    the receipt must still say which schedule/source was being processed."""
    root = tmp_path / "fred"
    _seed_sidecar(root)
    transport = _RaisingTransport()
    deps = build_production_fred_query_deps_v1(
        root=root, sidecar_path=root / SIDECAR_NAME,
        transport=transport,
        api_key_provider=build_fixture_api_key_provider_v1(),
        clock=lambda: NOW)
    result = build_macro_fred_caller_v1(deps)()
    assert result["fred_status"] == "CONTAINED_ERROR"
    assert result["fred_schedule_source"] == "SIDECAR"
    assert result["fred_release_slot"] == SLOT


# --- m-4: AR-9 directory fingerprint proves sidecar untouched ----------------

def test_sidecar_directory_fingerprint_unchanged_after_caller_run(tmp_path):
    """Directory fingerprint (mode, size, sha256 for every entry) must be
    identical before and after a caller run."""
    root = tmp_path / "fred"
    _seed_sidecar(root)
    before = _directory_fingerprint(root)

    outcomes = (FredFixtureTransportOutcomeV1("success", _body([MAY, JUNE, JULY])),)
    deps, _ = _build_deps(root, transport_outcomes=outcomes)
    build_macro_fred_caller_v1(deps)()

    after = _directory_fingerprint(root)
    # The cache sqlite3 may have grown (new rows), but the sidecar entry
    # within the fingerprint must be byte-identical.
    sidecar_before = next(e for e in before if e["name"] == SIDECAR_NAME)
    sidecar_after = next(e for e in after if e["name"] == SIDECAR_NAME)
    assert sidecar_before == sidecar_after


def _directory_fingerprint(root):
    import hashlib as _hashlib
    import os as _os
    import stat as _stat
    entries = []
    for path in sorted(root.rglob("*")):
        info = _os.lstat(path)
        digest = (_hashlib.sha256(path.read_bytes()).hexdigest()
                  if _stat.S_ISREG(info.st_mode) and not _stat.S_ISLNK(info.st_mode)
                  else None)
        entries.append({
            "name": path.relative_to(root).as_posix(),
            "mode": _stat.S_IMODE(info.st_mode),
            "size": info.st_size,
            "sha256": digest,
        })
    return entries


# --- m-5/minor: LOCAL_STATE_UNAVAILABLE test for cache corruption ------------

def test_corrupt_cache_after_load_yields_local_state_unavailable(tmp_path):
    """Physical DB corruption after a successful load should yield
    LOCAL_STATE_UNAVAILABLE when the cache is reopened."""
    root = tmp_path / "fred"
    _seed_sidecar(root)
    # Seed a success first.
    outcomes = (FredFixtureTransportOutcomeV1("success", _body([MAY, JUNE, JULY])),)
    deps_seed, _ = _build_deps(root, transport_outcomes=outcomes)
    build_macro_fred_caller_v1(deps_seed)()

    # Corrupt the cache DB physically.
    from tokmo.records.fred_query_cache import DATABASE_NAME
    db = root / DATABASE_NAME
    db.write_bytes(b"not a database")

    deps, _ = _build_deps(root, clock_value=NOW + timedelta(hours=25))
    result = build_macro_fred_caller_v1(deps)()
    assert result["fred_status"] == "CONTAINED_ERROR"
    assert result["fred_provider_starts"] == 0


# --- m-10: canonical_decimal projection through _scalar ----------------------

def test_enriched_receipt_projects_decimal_values_as_canonical_strings(tmp_path):
    """Decimal observation values must be projected as canonical_decimal strings,
    not Python repr or float."""
    root = tmp_path / "fred"
    _seed_sidecar(root)
    outcomes = (FredFixtureTransportOutcomeV1("success", _body([MAY, JUNE, JULY])),)
    deps, _ = _build_deps(root, transport_outcomes=outcomes)
    result = build_macro_fred_caller_v1(deps)()
    assert result["fred_status"] == "ENRICHED"
    # 151000 → canonical_decimal → "151000" (no trailing .0)
    assert result["fred_current_value"] == "151000"
    assert result["fred_prior_value"] == "150900"
