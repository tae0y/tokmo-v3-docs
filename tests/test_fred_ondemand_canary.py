"""G2 Delta SDS §4.4-§4.8 / §8: the on-demand tool contract, offline only.

Every test here drives the real tool entrypoints with transports injected at
the tool boundary, so the persistence, reload and budget wiring under test is
the wiring that ships. No test in this file constructs a socket or touches a
production root. Credential-format tests read only synthetic tmp-path values.
"""

import ast
import importlib.util
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).parents[1]
UTC = UTC


def _tool_module():
    spec = importlib.util.spec_from_file_location(
        "fred_ondemand_tool", ROOT / "tools/fred_ondemand_canary.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# A19-A21: year-aware month arithmetic, derived exactly once.
# ---------------------------------------------------------------------------


def test_a19_a_normal_release_month_maps_to_the_previous_calendar_month():
    module = _tool_module()
    query, terminal = module.derive_query_window_v1(
        release_date=date(2026, 8, 7), as_of=datetime(2026, 8, 7, 12, 45, tzinfo=UTC)
    )
    assert terminal == (2026, 7)
    assert query.observation_end == date(2026, 7, 31)
    assert query.observation_start == date(2026, 5, 1)
    assert module.observation_limit(query) == 3


def test_a20_january_rolls_over_to_december_of_the_previous_year():
    module = _tool_module()
    query, terminal = module.derive_query_window_v1(
        release_date=date(2026, 1, 9), as_of=datetime(2026, 1, 9, 13, 45, tzinfo=UTC)
    )
    assert terminal == (2025, 12)
    assert query.observation_end == date(2025, 12, 31)
    assert query.observation_start == date(2025, 10, 1)
    assert module.observation_limit(query) == 3
    # A bare ``month - 1`` yields month 0 of 2026 and is disproved here.
    assert terminal != (2026, 0)
    assert query.observation_end.year == 2025


@pytest.mark.parametrize(
    "year,month,expected",
    [
        (2026, 1, (2025, 12)),
        (2026, 2, (2026, 1)),
        (2026, 12, (2026, 11)),
        (2025, 1, (2024, 12)),
    ],
)
def test_a20_previous_calendar_month_is_year_aware_everywhere(year, month, expected):
    assert _tool_module().previous_calendar_month(year, month) == expected


@pytest.mark.parametrize(
    "release_date,end",
    [
        (date(2026, 3, 6), date(2026, 2, 28)),
        (date(2028, 3, 10), date(2028, 2, 29)),
        (date(2026, 1, 9), date(2025, 12, 31)),
        (date(2026, 5, 8), date(2026, 4, 30)),
    ],
)
def test_the_window_end_is_always_the_last_day_of_the_terminal_month(release_date, end):
    module = _tool_module()
    query, terminal = module.derive_query_window_v1(
        release_date=release_date, as_of=datetime(2026, 8, 12, tzinfo=UTC)
    )
    assert query.observation_end == end
    assert (query.observation_end.year, query.observation_end.month) == terminal


def test_a21_the_terminal_month_is_derived_once_and_reaches_both_consumers():
    module = _tool_module()
    # The derivation lives in macro_fred_inputs.py (moved from the tool).
    source = (ROOT / "src/tokmo/programs/macro_fred_inputs.py").read_text()
    tree = ast.parse(source)
    derivations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = {
            name.id
            for target in node.targets
            for name in ast.walk(target)
            if isinstance(name, ast.Name)
        }
        if "expected_terminal_month" not in names:
            continue
        if isinstance(node.targets[0], ast.Name):
            derivations.append(ast.unparse(node.value))
    assert derivations == [
        "previous_calendar_month(release_date.year, release_date.month)"
    ], derivations
    # Every consumer site in the tool only unpacks the single derivation's
    # return value; scan the tool source separately for those consumers.
    tool_source = (ROOT / "tools/fred_ondemand_canary.py").read_text()
    tool_tree = ast.parse(tool_source)
    consumers = []
    for node in ast.walk(tool_tree):
        if not isinstance(node, ast.Assign):
            continue
        names = {
            name.id
            for target in node.targets
            for name in ast.walk(target)
            if isinstance(name, ast.Name)
        }
        if "expected_terminal_month" not in names:
            continue
        if not isinstance(node.targets[0], ast.Name):
            consumers.append(ast.unparse(node.value))
    assert consumers and all(
        value.startswith("derive_query_window_v1(") for value in consumers
    ), consumers
    # The single derivation reaches the window, and the guard re-reads it from
    # the query itself rather than deriving it a second time.
    query, terminal = module.derive_query_window_v1(
        release_date=date(2026, 1, 9), as_of=datetime(2026, 1, 9, tzinfo=UTC)
    )
    assert (query.observation_end.year, query.observation_end.month) == terminal
    guard_source = (ROOT / "src/tokmo/facts/fred_query.py").read_text()
    guard = guard_source[guard_source.index("class TerminalMonthGuardedPortV1") :]
    assert "query.observation_end" in guard
    assert "previous_calendar_month" not in guard_source
    assert "release_date" not in guard.split("def query_series")[1]


def test_the_window_derivation_refuses_untyped_input():
    module = _tool_module()
    with pytest.raises(ValueError, match="invalid_release_date"):
        module.derive_query_window_v1(
            release_date="2026-08-07", as_of=datetime(2026, 8, 7, tzinfo=UTC)
        )
    with pytest.raises(ValueError, match="aware_utc_required"):
        module.derive_query_window_v1(
            release_date=date(2026, 8, 7), as_of=datetime(2026, 8, 7)
        )


# ---------------------------------------------------------------------------
# Offline drivers: the tool's own entrypoints with transports injected at the
# tool boundary (A32). Nothing below constructs a socket.
# ---------------------------------------------------------------------------

import json

from tokmo.fred_query_composition import (
    FredFixtureTransportOutcomeV1,
    FredFixtureTransportV1,
    build_fixture_api_key_provider_v1,
)
from tokmo.values.fred_macro import FredOperation

NOW = datetime(2026, 8, 7, 12, 45, tzinfo=UTC)
RELEASE_ID = 50
RELEASE_DATE = date(2026, 8, 7)
SLOT = "PAYEMS:2026-08-07"
OFFICIAL_AT = datetime(2026, 8, 7, 12, 30, tzinfo=UTC)

MAY = {
    "realtime_start": "2026-06-05",
    "realtime_end": "9999-12-31",
    "date": "2026-05-01",
    "value": "150800",
}
JUNE = {
    "realtime_start": "2026-07-02",
    "realtime_end": "9999-12-31",
    "date": "2026-06-01",
    "value": "150900",
}
JULY = {
    "realtime_start": "2026-08-07",
    "realtime_end": "9999-12-31",
    "date": "2026-07-01",
    "value": "151000",
}


def _ics_bytes():
    return _tool_module().read_pinned_ics_fixture(
        ROOT / "tests/fixtures/bls/official/bls-ics.md"
    )


def _series_release_outcome(release_id=RELEASE_ID):
    return FredFixtureTransportOutcomeV1(
        "success",
        json.dumps(
            {
                "realtime_start": "2026-08-07",
                "realtime_end": "2026-08-07",
                "releases": [
                    {
                        "id": release_id,
                        "realtime_start": "2026-08-07",
                        "realtime_end": "2026-08-07",
                        "name": "Employment Situation",
                        "press_release": True,
                    }
                ],
            }
        ).encode(),
    )


def _release_dates_outcome(days=("2026-08-07", "2026-07-02"), count=311):
    return FredFixtureTransportOutcomeV1(
        "success",
        json.dumps(
            {
                "realtime_start": "1776-07-04",
                "realtime_end": "9999-12-31",
                "order_by": "release_date",
                "sort_order": "desc",
                "count": count,
                "offset": 0,
                "limit": 2,
                "release_dates": [
                    {"release_id": RELEASE_ID, "date": day} for day in days
                ],
            }
        ).encode(),
    )


def _observations_outcome(rows=(MAY, JUNE, JULY), count=None):
    return FredFixtureTransportOutcomeV1(
        "success",
        json.dumps(
            {
                "realtime_start": "1776-07-04",
                "realtime_end": "9999-12-31",
                "observation_start": "2026-05-01",
                "observation_end": "2026-07-31",
                "units": "lin",
                "output_type": 4,
                "file_type": "json",
                "order_by": "observation_date",
                "sort_order": "asc",
                "count": len(rows) if count is None else count,
                "offset": 0,
                "limit": 3,
                "observations": list(rows),
            }
        ).encode(),
    )


class _FixtureBlsTransport:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self._outcomes:
            raise AssertionError("unplanned_bls_start")
        return self._outcomes.pop(0)


def _bls(module, *, body=None, code="success"):
    if code != "success":
        return _FixtureBlsTransport([module.BlsTransportOutcome(code)])
    return _FixtureBlsTransport(
        [module.BlsTransportOutcome("success", _ics_bytes() if body is None else body)]
    )


def _run(
    module,
    root,
    *,
    fred_outcomes,
    bls,
    now=NOW,
    rediscover_schedule=False,
    credential=None,
):
    fred = FredFixtureTransportV1(outcomes=tuple(fred_outcomes))
    result = module.run_canary_v1(
        paths=module.paths_for_root(
            root, credential=credential or (root.parent / "fred.env")
        ),
        fred_transport=fred,
        bls_transport=bls,
        api_key_provider=build_fixture_api_key_provider_v1(),
        clock=lambda: now,
        rediscover_schedule=rediscover_schedule,
    )
    return result, fred


def _full_discovery(module, root, **kwargs):
    return _run(
        module,
        root,
        fred_outcomes=(
            _series_release_outcome(),
            _release_dates_outcome(),
            _observations_outcome(),
        ),
        bls=_bls(module),
        **kwargs,
    )


# --- A26/A31: the sidecar is atomic, private and secret-free ----------------


def test_a26_the_sidecar_is_written_atomically_at_0600_in_canonical_bytes(tmp_path):
    import stat as stat_module

    module = _tool_module()
    root = tmp_path / "ondemand"
    result, _ = _full_discovery(module, root)
    assert result["code"] == "enrichment_complete"
    sidecar = root / module.SIDECAR_NAME
    info = os.lstat(sidecar)
    assert stat_module.S_ISREG(info.st_mode)
    assert stat_module.S_IMODE(info.st_mode) == 0o600
    assert stat_module.S_IMODE(os.lstat(root).st_mode) == 0o700
    raw = sidecar.read_bytes()
    payload = json.loads(raw)
    assert module.encode_sidecar_payload_v1(payload) == raw
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert list(payload) == sorted(payload)
    # No temporary file survives a successful write.
    assert [path.name for path in root.iterdir() if path.name.startswith(".")] == []
    assert sorted(path.name for path in root.iterdir()) == [
        "fred-query-cache.sqlite3",
        module.SIDECAR_NAME,
    ]


def test_a26_the_write_uses_the_reviewed_atomic_primitive():
    source = (ROOT / "tools/fred_ondemand_canary.py").read_text()
    assert "write_atomic_json" in source
    assert "from tokmo.records.atomic_json import" in source


def test_a31_the_sidecar_carries_no_secret_and_no_raw_calendar(tmp_path):
    module = _tool_module()
    root = tmp_path / "ondemand"
    _full_discovery(module, root)
    raw = (root / module.SIDECAR_NAME).read_bytes()
    payload = json.loads(raw)
    lowered = raw.lower()
    for forbidden in (
        b"api_key",
        b"apikey",
        b"begin:vcalendar",
        b"vevent",
        b"dtstart",
        b"summary:",
        b"credential",
        b"fred.env",
        b"consumer price",
        b"file_type",
        b"?",
        build_fixture_api_key_provider_v1()().encode(),
    ):
        assert forbidden not in lowered, forbidden
    assert set(payload) == {
        "schema",
        "schema_version",
        "series_id",
        "release_id",
        "release_date",
        "official_release_at",
        "release_slot",
        "valid_until",
        "provenance",
        "provenance_digest",
        "payload_digest",
    }
    assert set(payload["provenance"]) == {
        "fred_release_id",
        "fred_release_date",
        "bls_source_id",
        "bls_source_url",
        "bls_summary",
        "bls_tzid",
        "bls_resolved_zone",
        "official_release_at_utc",
        "bls_body_sha256",
        "bls_body_bytes",
        "grace_seconds",
        "join_rule",
    }
    assert payload["provenance"]["bls_body_bytes"] == len(_ics_bytes())
    assert len(payload["provenance"]["bls_body_sha256"]) == 64


# --- A27: the load rejection matrix -----------------------------------------


def _seed_sidecar(
    module,
    root,
    *,
    mutate=None,
    mode=0o600,
    release_date=RELEASE_DATE,
    official_at=OFFICIAL_AT,
):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    from tokmo.values.fred_query import build_fred_release_schedule_v1

    provenance = module.build_schedule_provenance_v1(
        release_id=RELEASE_ID,
        release_date=release_date,
        official_release_at=official_at,
        body_sha256="d" * 64,
        body_bytes=1064,
    )
    schedule = build_fred_release_schedule_v1(
        series_id="PAYEMS",
        release_id=RELEASE_ID,
        release_date=release_date,
        official_release_at=official_at,
        schedule_provenance=provenance,
    )
    payload = module.build_sidecar_payload_v1(
        schedule=schedule,
        release_id=RELEASE_ID,
        release_date=release_date,
        official_release_at=official_at,
        provenance=provenance,
    )
    sidecar = root / module.SIDECAR_NAME
    if mutate is None:
        module.write_schedule_sidecar_v1(sidecar, payload)
    else:
        sidecar.write_bytes(mutate(payload, module))
    os.chmod(sidecar, mode)
    return sidecar, payload


@pytest.mark.parametrize(
    "name,reason,mutate",
    [
        (
            "wrong_schema",
            "sidecar_schema_mismatch",
            lambda payload, module: module.encode_sidecar_payload_v1(
                _with_digest(
                    module, dict(payload, schema="tokmo.fred-ondemand.schedule.r1")
                )
            ),
        ),
        (
            "wrong_version",
            "sidecar_schema_mismatch",
            lambda payload, module: module.encode_sidecar_payload_v1(
                _with_digest(module, dict(payload, schema_version=2))
            ),
        ),
        (
            "noncanonical_bytes",
            "noncanonical_sidecar",
            lambda payload, module: json.dumps(payload, indent=2).encode() + b"\n",
        ),
        (
            "payload_digest",
            "sidecar_payload_digest_mismatch",
            lambda payload, module: module.encode_sidecar_payload_v1(
                dict(payload, payload_digest="0" * 64)
            ),
        ),
        (
            "provenance_digest",
            "sidecar_provenance_digest_mismatch",
            lambda payload, module: module.encode_sidecar_payload_v1(
                _with_digest(module, dict(payload, provenance_digest="0" * 64))
            ),
        ),
        (
            "foreign_domain",
            "sidecar_provenance_digest_mismatch",
            lambda payload, module: module.encode_sidecar_payload_v1(
                _with_digest(
                    module,
                    dict(
                        payload,
                        provenance_digest=_r1_digest(module, payload["provenance"]),
                    ),
                )
            ),
        ),
        (
            "slot_mismatch",
            "release_slot_mismatch",
            lambda payload, module: module.encode_sidecar_payload_v1(
                _with_digest(module, dict(payload, release_slot="PAYEMS:2026-08-06"))
            ),
        ),
        (
            "unregistered_series",
            "unregistered_series",
            lambda payload, module: module.encode_sidecar_payload_v1(
                _with_digest(module, dict(payload, series_id="UNRATE"))
            ),
        ),
        (
            "nonpositive_release",
            "invalid_release_id",
            lambda payload, module: module.encode_sidecar_payload_v1(
                _with_digest(module, dict(payload, release_id=0))
            ),
        ),
        (
            "valid_until_drift",
            "schedule_reconstruction_mismatch",
            lambda payload, module: module.encode_sidecar_payload_v1(
                _with_digest(
                    module, dict(payload, valid_until="2099-01-01T00:00:00.000000Z")
                )
            ),
        ),
        ("truncated_json", "invalid_sidecar_json", lambda payload, module: b"{"),
        (
            "mutated_provenance_field",
            "sidecar_provenance_digest_mismatch",
            lambda payload, module: module.encode_sidecar_payload_v1(
                _with_digest(
                    module,
                    dict(
                        payload,
                        provenance=dict(payload["provenance"], bls_body_bytes=1),
                    ),
                )
            ),
        ),
    ],
)
def test_a27_every_load_validation_failure_is_fail_closed(
    tmp_path, name, reason, mutate
):
    """Each check is pinned by the exact reason it must reject with, so no
    single validation step can be removed and hidden behind another."""
    module = _tool_module()
    root = tmp_path / "ondemand"
    _seed_sidecar(module, root, mutate=mutate)
    with pytest.raises(module.ScheduleSidecarInvalid, match=reason):
        module.load_schedule_sidecar_v1(root / module.SIDECAR_NAME, now=NOW)


def _with_digest(module, payload):
    import hashlib

    from tokmo.values.fred_query import canonical_json

    body = {key: value for key, value in payload.items() if key != "payload_digest"}
    return dict(body, payload_digest=hashlib.sha256(canonical_json(body)).hexdigest())


def _r1_digest(module, provenance):
    from tokmo.values.fred_query import domain_digest

    return domain_digest("tokmo.fred-query.schedule.r1", provenance)


def test_a27_unsafe_path_metadata_rejects(tmp_path):
    module = _tool_module()
    root = tmp_path / "ondemand"
    _seed_sidecar(module, root, mode=0o644)
    with pytest.raises(module.ScheduleSidecarInvalid):
        module.load_schedule_sidecar_v1(root / module.SIDECAR_NAME, now=NOW)


def test_a27_an_absent_sidecar_is_not_an_error(tmp_path):
    module = _tool_module()
    root = tmp_path / "ondemand"
    root.mkdir(mode=0o700, parents=True)
    assert module.load_schedule_sidecar_v1(root / module.SIDECAR_NAME, now=NOW) is None


def test_a27_a_valid_sidecar_round_trips_and_reports_its_expiry(tmp_path):
    module = _tool_module()
    root = tmp_path / "ondemand"
    _seed_sidecar(module, root)
    loaded = module.load_schedule_sidecar_v1(root / module.SIDECAR_NAME, now=NOW)
    assert loaded.release_id == RELEASE_ID
    assert loaded.release_date == RELEASE_DATE
    assert loaded.official_release_at == OFFICIAL_AT
    assert loaded.schedule.release_slot == SLOT
    assert loaded.expired is False
    assert module.load_schedule_sidecar_v1(
        root / module.SIDECAR_NAME,
        now=loaded.schedule.valid_until + timedelta(microseconds=1),
    ).expired


# --- A28/A29/A30: the three sidecar outcomes through the tool ---------------


def test_a28_a_valid_sidecar_is_reloaded_before_admission_with_zero_starts(tmp_path):
    module = _tool_module()
    root = tmp_path / "ondemand"
    result, fred = _full_discovery(module, root)
    assert result["fred_starts"] == 3 and result["bls_starts"] == 1

    # Second invocation inside the freshness interval: the schedule comes from
    # the sidecar and the admission decision comes from the cache.
    again, fred_again = _run(
        module,
        root,
        fred_outcomes=(),
        bls=_FixtureBlsTransport([]),
        now=NOW + timedelta(minutes=5),
    )
    assert again["due_state"] == "NOT_DUE_FRESH"
    # The stored success is reused as-is; no new material was fetched.
    assert again["code"] == "enrichment_complete"
    assert again["coverage"] == "USED"
    assert again["revision_identity"] == result["revision_identity"]
    assert (again["fred_starts"], again["bls_starts"], again["observation_starts"]) == (
        0,
        0,
        0,
    )
    assert again["schedule_source"] == "sidecar"
    assert fred_again.requests == []


def test_a29_an_absent_sidecar_triggers_bounded_rediscovery_then_persists(tmp_path):
    module = _tool_module()
    root = tmp_path / "ondemand"
    assert not (root / module.SIDECAR_NAME).exists()
    result, fred = _full_discovery(module, root)
    assert result["schedule_source"] == "discovery"
    assert result["fred_starts"] == 3 <= module.FRED_MAX_STARTS
    assert result["bls_starts"] == 1 <= module.BLS_MAX_STARTS
    assert [request.operation for request, _key in fred.requests] == [
        FredOperation.SERIES_RELEASE,
        FredOperation.RELEASE_DATES,
        FredOperation.SERIES_OBSERVATIONS,
    ]
    assert (root / module.SIDECAR_NAME).exists()
    assert result["release_slot"] == SLOT
    assert result["official_release_at"] == "2026-08-07T12:30:00.000000Z"
    assert result["eligible_from"] == "2026-08-07T12:45:00.000000Z"


def test_a29_an_expired_sidecar_rediscovers_under_a_fresh_budget(tmp_path):
    module = _tool_module()
    root = tmp_path / "ondemand"
    # A sidecar for the previous release, already past its 21-day window at
    # the moment this invocation runs.
    _seed_sidecar(
        module,
        root,
        release_date=date(2026, 7, 2),
        official_at=datetime(2026, 7, 2, 12, 30, tzinfo=UTC),
    )
    stale = module.load_schedule_sidecar_v1(root / module.SIDECAR_NAME, now=NOW)
    assert stale.expired is True
    result, fred = _run(
        module,
        root,
        fred_outcomes=(
            _series_release_outcome(),
            _release_dates_outcome(),
            _observations_outcome(),
        ),
        bls=_bls(module),
        now=NOW,
    )
    assert result["schedule_source"] == "expired_rediscovery"
    assert (
        result["fred_starts"],
        result["bls_starts"],
        result["observation_starts"],
    ) == (3, 1, 1)
    assert result["release_slot"] == SLOT
    assert (
        module.load_schedule_sidecar_v1(
            root / module.SIDECAR_NAME, now=NOW
        ).release_date
        == RELEASE_DATE
    )


def test_a30_a_corrupt_sidecar_stops_with_zero_starts_and_no_cache_write(tmp_path):
    module = _tool_module()
    root = tmp_path / "ondemand"
    _seed_sidecar(
        module,
        root,
        mutate=lambda payload, mod: mod.encode_sidecar_payload_v1(
            dict(payload, payload_digest="0" * 64)
        ),
    )
    before = (root / module.SIDECAR_NAME).read_bytes()
    bls = _FixtureBlsTransport([])
    result, fred = _run(module, root, fred_outcomes=(), bls=bls)
    assert result["code"] == "schedule_sidecar_invalid"
    assert (
        result["fred_starts"],
        result["bls_starts"],
        result["observation_starts"],
    ) == (0, 0, 0)
    assert result["stopped_at"] == "sidecar"
    assert fred.requests == [] and bls.calls == 0
    # No automatic refetch, and the corrupt bytes are left untouched.
    assert (root / module.SIDECAR_NAME).read_bytes() == before
    from tokmo.records.fred_query_cache import FredQueryCacheV1

    cache = FredQueryCacheV1(root, opened_at=NOW)
    assert cache.read_success_for_series("PAYEMS", now=NOW) is None


def test_a30_recovery_from_a_corrupt_sidecar_needs_the_explicit_flag(tmp_path):
    module = _tool_module()
    root = tmp_path / "ondemand"
    _seed_sidecar(
        module,
        root,
        mutate=lambda payload, mod: mod.encode_sidecar_payload_v1(
            dict(payload, payload_digest="0" * 64)
        ),
    )
    result, fred = _run(
        module,
        root,
        fred_outcomes=(
            _series_release_outcome(),
            _release_dates_outcome(),
            _observations_outcome(),
        ),
        bls=_bls(module),
        rediscover_schedule=True,
    )
    assert result["code"] == "enrichment_complete"
    assert result["schedule_source"] == "rediscovery"
    assert (result["fred_starts"], result["bls_starts"]) == (3, 1)
    assert (
        module.load_schedule_sidecar_v1(root / module.SIDECAR_NAME, now=NOW) is not None
    )


# --- A32/A33: the tool path and the whole consumption table -----------------


def test_a32_the_full_success_row_runs_through_the_real_tool_entrypoint(tmp_path):
    module = _tool_module()
    root = tmp_path / "ondemand"
    result, _ = _full_discovery(module, root)
    assert result["code"] == "enrichment_complete"
    assert result["coverage"] == "USED"
    assert result["due_state"] == "DUE_MISSING"
    assert result["expected_terminal_month"] == [2026, 7]
    assert result["direction"] == "EXPANDING"
    assert result["current_date"] == "2026-07-01"
    assert result["prior_date"] == "2026-06-01"
    assert result["observation_starts"] == 1
    readback = module.run_readback_v1(root=root, clock=lambda: NOW)
    assert readback["code"] == "readback_ok"
    assert readback["coverage"] == "USED"
    assert readback["success"]["newest_observation_date"] == "2026-07-01"
    assert [row["state"] for row in readback["attempts"]] == ["SUCCEEDED"]
    assert (
        readback["fred_starts"],
        readback["bls_starts"],
        readback["observation_starts"],
    ) == (0, 0, 0)


@pytest.mark.parametrize(
    "name,fred_outcomes,bls_code,expected",
    [
        (
            "step_1_fails",
            (FredFixtureTransportOutcomeV1("timeout"),),
            "success",
            (1, 0, 0, "series_release"),
        ),
        (
            "step_2_fails",
            (_series_release_outcome(), FredFixtureTransportOutcomeV1("http_5xx")),
            "success",
            (2, 0, 0, "release_dates"),
        ),
        (
            "step_3_fails",
            (_series_release_outcome(), _release_dates_outcome()),
            "timeout",
            (2, 1, 0, "bls_calendar"),
        ),
    ],
)
def test_a33_the_first_failure_stops_every_dependent_operation(
    tmp_path, name, fred_outcomes, bls_code, expected
):
    module = _tool_module()
    root = tmp_path / "ondemand" / name
    bls = _bls(module) if bls_code == "success" else _bls(module, code=bls_code)
    result, fred = _run(module, root, fred_outcomes=fred_outcomes, bls=bls)
    fred_starts, bls_starts, observation_starts, stopped_at = expected
    assert result["code"] == "schedule_unavailable"
    assert (
        result["fred_starts"],
        result["bls_starts"],
        result["observation_starts"],
    ) == (fred_starts, bls_starts, observation_starts)
    assert result["stopped_at"] == stopped_at
    assert not (root / module.SIDECAR_NAME).exists()


def test_a33_a_join_rejection_spends_three_starts_and_settles_nothing(tmp_path):
    module = _tool_module()
    root = tmp_path / "ondemand"
    # A calendar with no Employment Situation event on the FRED release date.
    body = _ics_bytes().replace(b"20260807T083000", b"20260814T083000")
    result, _ = _run(
        module,
        root,
        fred_outcomes=(_series_release_outcome(), _release_dates_outcome()),
        bls=_bls(module, body=body),
    )
    assert result["code"] == "schedule_unavailable"
    assert (
        result["fred_starts"],
        result["bls_starts"],
        result["observation_starts"],
    ) == (2, 1, 0)
    assert result["stopped_at"] == "schedule_join"
    assert result["failure_code"] == "BlsCalendarRejection"
    assert not (root / module.SIDECAR_NAME).exists()


def test_a33_a_guard_rejection_still_spends_the_fourth_row_and_keeps_the_sidecar(
    tmp_path,
):
    module = _tool_module()
    root = tmp_path / "ondemand"
    result, _ = _run(
        module,
        root,
        fred_outcomes=(
            _series_release_outcome(),
            _release_dates_outcome(),
            _observations_outcome(rows=(MAY, JUNE), count=2),
        ),
        bls=_bls(module),
    )
    assert result["code"] == "schedule_released_data_not_available"
    assert (
        result["fred_starts"],
        result["bls_starts"],
        result["observation_starts"],
    ) == (3, 1, 1)
    assert result["failure_code"] == "NOT_YET_RELEASED"
    assert result["coverage"] == "OMITTED_UNAVAILABLE"
    # The schedule was persisted before the observation request, so the next
    # invocation cannot be forced into a rediscovery.
    assert (
        module.load_schedule_sidecar_v1(root / module.SIDECAR_NAME, now=NOW) is not None
    )


def test_a33_a_sidecar_hit_that_is_due_spends_exactly_one_fred_start(tmp_path):
    module = _tool_module()
    root = tmp_path / "ondemand"
    _seed_sidecar(module, root)
    result, fred = _run(
        module,
        root,
        fred_outcomes=(_observations_outcome(),),
        bls=_FixtureBlsTransport([]),
    )
    assert result["schedule_source"] == "sidecar"
    assert (
        result["fred_starts"],
        result["bls_starts"],
        result["observation_starts"],
    ) == (1, 0, 1)
    assert result["code"] == "enrichment_complete"


def test_a33_the_dual_budgets_raise_before_any_socket_is_constructed():
    module = _tool_module()
    fred_budget = module.FredStartBudget(maximum=3)
    bls_budget = module.BlsStartBudget()
    assert (fred_budget.maximum, bls_budget.maximum) == (3, 1)
    fred = FredFixtureTransportV1(
        outcomes=tuple(_series_release_outcome() for _index in range(4))
    )
    wire = module.BudgetedFredTransportV1(fred, fred_budget)
    for _index in range(3):
        wire(
            module.build_series_release_request(series_id="PAYEMS")
            if hasattr(module, "build_series_release_request")
            else _request(),
            "a" * 32,
        )
    with pytest.raises(ValueError, match="fred_start_budget_exceeded"):
        wire(_request(), "a" * 32)
    assert len(fred.requests) == 3, "the refused call reached no transport"

    calendar = _FixtureBlsTransport(
        [
            module.BlsTransportOutcome("success", _ics_bytes()),
            module.BlsTransportOutcome("success", _ics_bytes()),
        ]
    )
    bls_wire = module.BudgetedBlsTransportV1(calendar, bls_budget)
    bls_wire()
    with pytest.raises(ValueError, match="bls_start_budget_exceeded"):
        bls_wire()
    assert calendar.calls == 1, "the refused call reached no transport"


def _request():
    from tokmo.facts.fred_query import build_series_release_request

    return build_series_release_request(series_id="PAYEMS")


def test_the_budget_wrapper_spends_before_delegating():
    module = _tool_module()
    source = (ROOT / "src/tokmo/fred_query_composition.py").read_text()
    body = source[
        source.index("class BudgetedFredTransportV1") : source.index(
            "MACRO_FRED_RECEIPT_FIELDS"
        )
    ]
    call = body[body.index("def __call__(self, request, api_key)") :]
    spend = call.index("self._budget.spend()")
    delegate = call.index("self._transport(")
    assert spend < delegate, "the budget must be spent before delegation"


# --- A34-A37: shape parity, byte invariance, zero ambient access ------------


def test_a34_the_exact_planned_window_parses_complete_through_the_real_adapter():
    from tokmo.facts.fred import build_query_pairs, parse_fred_response
    from tokmo.facts.fred_query import build_series_observations_request
    from tokmo.values.fred_macro import PageCompleteness

    module = _tool_module()
    query, _terminal = module.derive_query_window_v1(
        release_date=RELEASE_DATE, as_of=NOW
    )
    request = build_series_observations_request(query)
    assert build_query_pairs(request) == (
        ("series_id", "PAYEMS"),
        ("realtime_start", "1776-07-04"),
        ("realtime_end", "9999-12-31"),
        ("observation_start", "2026-05-01"),
        ("observation_end", "2026-07-31"),
        ("units", "lin"),
        ("output_type", "4"),
        ("sort_order", "asc"),
        ("limit", "3"),
        ("offset", "0"),
        ("file_type", "json"),
    )
    page = parse_fred_response(request, _observations_outcome().body)
    assert page.completeness is PageCompleteness.COMPLETE
    assert len(page.rows) == 3
    assert page.rows[-1]["date"] == "2026-07-01"


# The G1 baseline bytes of every path this delta must not touch. A change
# here is an immediate STOP, not a merge conflict to resolve.
# Re-pinned by the 2026-08-13 caller-connection/recurring-activation deltas for
# the exact files whose reviewed bytes changed. All other pins are unchanged.
BYTE_UNCHANGED = {
    "src/tokmo/programs/macro_fred_inputs.py": "eabb80f8621c0273cfc127a92c73a352ba4fdbe832bd5172c70823f811484865",
    "src/tokmo/records/fred_query_cache.py": "f7202c19e2bef011278626c3f70ae9f80fd449a2fca8322fc378d22d83f5593a",
    "src/tokmo/fred_query_composition.py": "1d04c911ef17234d30c26f278899160d1aeba67deb9cd32cce3d5543b4001740",
    "src/tokmo/programs/market_regime_core.py": "ece7111b3ff183c8f359b71ef6c77d23d7d855ecd138a6753eda0fcea302d4fe",
    "src/tokmo/values/market_regime_core.py": "e9c73a28e9afd61b694ad0a9359468af2a34b006029ace088f7912ea9cf85da7",
}


@pytest.mark.parametrize("relative,digest", sorted(BYTE_UNCHANGED.items()))
def test_a35_the_forbidden_paths_are_byte_unchanged(relative, digest):
    import hashlib

    assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest


def test_a36_the_whole_fixture_path_opens_no_socket_credential_or_home_root(
    tmp_path, monkeypatch
):
    import builtins
    import socket
    import sqlite3

    module = _tool_module()
    opened, connected = [], []
    real_open, real_os_open = builtins.open, os.open
    real_connect = sqlite3.connect

    def guarded_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def guarded_os_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_os_open(path, *args, **kwargs)

    def guarded_connect(target, *args, **kwargs):
        connected.append(str(target))
        return real_connect(target, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("network_forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    monkeypatch.setattr(sqlite3, "connect", guarded_connect)

    result, _ = _full_discovery(module, tmp_path / "ondemand")
    assert result["code"] == "enrichment_complete"
    touched = opened + connected
    assert touched, "the guarded run touched no path at all"
    for path in touched:
        lowered = path.lower()
        assert ".hermes" not in lowered, path
        assert "credential" not in lowered and ".env" not in lowered, path
        assert ".local/share/tokmo-v3" not in lowered, path
        assert "launchagents" not in lowered, path
        resolved = Path(path)
        if resolved.is_absolute() and str(resolved).startswith(str(Path.home())):
            assert str(resolved).startswith(str(ROOT)), path
    for target in connected:
        cleaned = target.removeprefix("file://").split("?", 1)[0]
        assert cleaned.startswith(str(tmp_path)), target


def test_a37_no_bls_host_literal_exists_anywhere_under_src():
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if "__pycache__" not in path.parts and "bls.gov" in path.read_text()
    ]
    assert offenders == []
    tool = (ROOT / "tools/fred_ondemand_canary.py").read_text()
    assert tool.count("https://www.bls.gov/schedule/news_release/bls.ics") == 1


def test_a37_the_tool_names_no_scheduler_alert_or_order_effect():
    source = (ROOT / "tools/fred_ondemand_canary.py").read_text().lower()
    for token in (
        "cron",
        "launchagent",
        "launchd",
        "plist",
        "telegram",
        "webhook",
        "smtp",
        "place_order",
        "submit_order",
        "order_id",
        "orderid",
        "send_alert",
        "openai",
        "anthropic",
    ):
        assert token not in source, token


def test_a37_the_tool_touches_no_active_lane_store():
    """B7: no cadence lease, ops kernel, regime store or calendar cache."""
    source = (ROOT / "tools/fred_ondemand_canary.py").read_text().lower()
    for token in (
        "cadence",
        "ops_kernel",
        "opskernel",
        "ops_tick",
        "market_regime",
        "regime",
        "calendar_cache",
        "session_gate",
        "pulse",
        "trade_ledger",
        "holdings",
        "toss",
    ):
        assert token not in source, token


def test_the_authenticated_transport_is_never_repointed_at_the_public_host():
    source = (ROOT / "tools/fred_ondemand_canary.py").read_text()
    transport = ast.parse(source)
    node = next(
        item
        for item in ast.walk(transport)
        if isinstance(item, ast.ClassDef) and item.name == "BlsIcsTransport"
    )
    executable = "\n".join(
        ast.unparse(child)
        for child in node.body
        if not (isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant))
    )
    for token in (
        "api_key",
        "load_fred_api_key",
        "FredLiveTransport",
        "credential",
        "Authorization",
        "Cookie",
    ):
        assert token not in executable, token
    from tokmo.facts.fred_live import HOST

    assert HOST == "api.stlouisfed.org"
    module = _tool_module()
    assert module.BLS_HOST != HOST


def test_the_public_transport_follows_no_redirect_and_uses_no_proxy():
    """Structural mirror of facts/fred_live.py: redirects return None and the
    empty ProxyHandler displaces the environment-reading default one."""
    import urllib.request

    module = _tool_module()
    opener = module.build_bls_opener()
    handlers = {type(handler).__name__ for handler in opener.handlers}
    assert "_NoRedirect" in handlers
    assert "HTTPRedirectHandler" not in handlers
    assert (
        module._NoRedirect.redirect_request(
            module._NoRedirect(), None, None, 302, "", {}, "https://elsewhere"
        )
        is None
    )
    # An empty ProxyHandler registers no ``*_open`` method, so it never joins
    # the handler chain — and passing it makes build_opener skip the default
    # ProxyHandler that would otherwise read proxy environment variables.
    assert not any(
        isinstance(handler, urllib.request.ProxyHandler) for handler in opener.handlers
    )
    source = (ROOT / "tools/fred_ondemand_canary.py").read_text()
    assert "urllib.request.ProxyHandler({})" in source
    live = (ROOT / "src/tokmo/facts/fred_live.py").read_text()
    assert "urllib.request.ProxyHandler({})" in live


# --- (b) B1-B8: production-path offline preflight ---------------------------


def _preflight(module, tmp_path, *, credential_mode=0o600, campaign=True):
    root = tmp_path / "preflight-root"
    credential = tmp_path / "credentials" / "fred.env"
    credential.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    credential.write_text("FRED_API_KEY=" + "a" * 32 + "\n")
    os.chmod(credential, credential_mode)
    campaign_root = tmp_path / "campaign" / "v1"
    if campaign:
        campaign_root.mkdir(mode=0o700, parents=True)
        (campaign_root / "fred.sqlite3").write_bytes(b"campaign-bytes")
        os.chmod(campaign_root / "fred.sqlite3", 0o600)
    production = tmp_path / "production" / "v1" / module.SIDECAR_NAME
    return (
        module.run_preflight_v1(
            root=root,
            credential=credential,
            campaign_root=campaign_root,
            production_sidecar=production,
            ics_fixture=ROOT / "tests/fixtures/bls/official/bls-ics.md",
            clock=lambda: NOW,
        ),
        root,
        credential,
        campaign_root,
        production,
    )


def test_b1_the_root_is_private_the_database_is_0600_and_reopen_revalidates(tmp_path):
    module = _tool_module()
    result, root, _credential, _campaign, _production = _preflight(module, tmp_path)
    assert result["code"] == "preflight_ok"
    assert result["root_mode"] == "0o700"
    assert result["database_mode"] == "0o600"
    assert result["sqlite_sidecars"] == []
    assert result["reopen_success"] is True
    assert result["root"] == str(root)


def test_b2_the_credential_is_checked_by_metadata_and_never_read(tmp_path, monkeypatch):
    import builtins

    module = _tool_module()
    reads = []
    real_open, real_os_open = builtins.open, os.open

    def guarded_open(file, *args, **kwargs):
        reads.append(str(file))
        return real_open(file, *args, **kwargs)

    def guarded_os_open(path, *args, **kwargs):
        reads.append(str(path))
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    result, _root, credential, _campaign, _production = _preflight(module, tmp_path)
    assert result["credential"] == {
        "present": True,
        "safe": True,
        "mode": "0o600",
        "size_bytes": 46,
        "owner_match": True,
        "reason": None,
    }
    assert result["credential_reads"] == 0
    assert str(credential) not in reads


def test_b2_a_world_readable_credential_is_reported_unsafe(tmp_path):
    module = _tool_module()
    result, *_rest = _preflight(module, tmp_path, credential_mode=0o644)
    assert result["credential"]["safe"] is False
    assert result["credential"]["reason"] == "credential_unsafe"


def test_b3_the_campaign_root_is_byte_and_stat_invariant(tmp_path):
    module = _tool_module()
    result, _root, _credential, campaign_root, _production = _preflight(
        module, tmp_path
    )
    assert result["campaign_root_unchanged"] is True
    fingerprint = module.directory_fingerprint_v1(campaign_root)
    assert (
        fingerprint["entries"][0]["sha256"]
        == __import__("hashlib").sha256(b"campaign-bytes").hexdigest()
    )


def test_b4_the_reconstructed_fixture_drives_a_deterministic_offline_join(tmp_path):
    """Revision D B4: determinism and provenance for *these* bytes.

    This asserts nothing about an unseen live response. Equality with what a
    real fetch would return is unverifiable offline and is deliberately not
    claimed here; only the D2-gated live canary (§8.3 C2) can close that gap.
    """
    module = _tool_module()
    result, *_rest = _preflight(module, tmp_path)
    assert result["official_release_at"] == "2026-08-07T12:30:00.000000Z"
    assert result["eligible_from"] == "2026-08-07T12:45:00.000000Z"
    assert result["release_slot"] == SLOT
    assert result["expected_terminal_month"] == [2026, 7]
    assert (
        result["observation_start"],
        result["observation_end"],
        result["observation_limit"],
    ) == ("2026-05-01", "2026-07-31", 3)
    assert result["sidecar_reload_matches"] is True
    assert result["sidecar_mode"] == "0o600"

    # Complete §4.10 provenance for the reconstructed bytes.
    body = _ics_bytes()
    calendar = module.parse_bls_calendar_v1(body)
    assert result["bls_body_sha256"] == calendar.body_sha256
    assert result["bls_body_bytes"] == len(body)
    meta = _fixture_metadata()
    assert result["bls_body_sha256"] == meta["reconstructed_wire_sha256"]
    assert result["bls_body_bytes"] == int(meta["reconstructed_wire_length"])
    assert meta["status"] == "RECONSTRUCTED_CONTRACT"
    assert meta["provider_request"] == "none"


def test_b4_the_join_is_identical_across_repeated_runs(tmp_path):
    module = _tool_module()
    first, *_rest = _preflight(module, tmp_path / "one")
    second, *_rest = _preflight(module, tmp_path / "two")
    for key in (
        "official_release_at",
        "eligible_from",
        "release_slot",
        "schedule_provenance_digest",
        "bls_body_sha256",
        "bls_body_bytes",
        "expected_terminal_month",
    ):
        assert first[key] == second[key], key
    assert (first["fred_starts"], first["bls_starts"]) == (0, 0)
    assert (second["fred_starts"], second["bls_starts"]) == (0, 0)


def test_b4_the_join_is_identical_across_a_fresh_process(tmp_path):
    """A fresh interpreter must reproduce the same instant and digest."""
    import subprocess
    import sys

    module = _tool_module()
    inside, *_rest = _preflight(module, tmp_path / "inprocess")

    root = tmp_path / "subprocess-root"
    credential = tmp_path / "sub-credentials" / "fred.env"
    credential.parent.mkdir(mode=0o700, parents=True)
    credential.write_text("FRED_API_KEY=" + "a" * 32 + "\n")
    os.chmod(credential, 0o600)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/fred_ondemand_canary.py"),
            "preflight",
            "--root",
            str(root),
            "--credential",
            str(credential),
            "--campaign-root",
            str(tmp_path / "sub-campaign"),
            "--production-sidecar",
            str(tmp_path / "sub-production" / module.SIDECAR_NAME),
            "--ics-fixture",
            str(ROOT / "tests/fixtures/bls/official/bls-ics.md"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ROOT),
    )
    assert completed.returncode == 0, completed.stderr
    fresh = json.loads(completed.stdout)
    assert fresh["code"] == "preflight_ok"
    for key in (
        "official_release_at",
        "eligible_from",
        "release_slot",
        "schedule_provenance_digest",
        "bls_body_sha256",
        "bls_body_bytes",
    ):
        assert fresh[key] == inside[key], key
    assert (
        fresh["fred_starts"],
        fresh["bls_starts"],
        fresh["observation_starts"],
        fresh["credential_reads"],
    ) == (0, 0, 0, 0)
    assert fresh["production_sidecar_absent"] is True
    assert "api_key" not in completed.stdout


def _fixture_metadata() -> dict:
    text = (ROOT / "tests/fixtures/bls/official/bls-ics.md").read_text()
    begin, end = "<!--BLS-FIXTURE-META-BEGIN-->", "<!--BLS-FIXTURE-META-END-->"
    block = text[text.index(begin) + len(begin) : text.index(end)]
    fields = {}
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("- ") and ":" in line:
            key, value = line[2:].split(":", 1)
            fields[key.strip()] = value.strip()
    return fields


def test_b5_preflight_leaves_no_sidecar_in_the_production_root(tmp_path):
    module = _tool_module()
    result, root, _credential, _campaign, production = _preflight(module, tmp_path)
    assert result["production_sidecar_absent"] is True
    assert not os.path.lexists(production)
    assert not os.path.lexists(production.parent)
    # The fixture-derived schedule is confined to the injected working root.
    assert (root / module.SIDECAR_NAME).exists()


def test_b5_preflight_refuses_to_work_inside_the_production_root(tmp_path):
    module = _tool_module()
    root = tmp_path / "root"
    with pytest.raises(ValueError, match="preflight_root_conflict"):
        module.run_preflight_v1(
            root=root,
            credential=tmp_path / "fred.env",
            campaign_root=tmp_path / "campaign",
            production_sidecar=root / module.SIDECAR_NAME,
            ics_fixture=ROOT / "tests/fixtures/bls/official/bls-ics.md",
            clock=lambda: NOW,
        )


def test_b5_preflight_constructs_no_transport_at_all(tmp_path):
    module = _tool_module()
    result, *_rest = _preflight(module, tmp_path)
    assert (
        result["fred_starts"],
        result["bls_starts"],
        result["observation_starts"],
    ) == (0, 0, 0)
    source = (ROOT / "tools/fred_ondemand_canary.py").read_text()
    body = source[
        source.index("def run_preflight_v1") : source.index("def run_readback_v1")
    ]
    for token in (
        "FredLiveTransport",
        "BlsIcsTransport",
        "build_live_opener",
        "build_bls_opener",
        "load_fred_api_key",
        "api_key",
    ):
        assert token not in body, token


def test_b6_canary_without_the_execute_flag_is_forbidden_with_zero_of_everything(
    capsys,
):
    module = _tool_module()
    loads = []
    code = module.run_cli(
        ["canary", "--root", "/nonexistent"],
        clock=lambda: NOW,
        api_key_loader=lambda path: loads.append(path),
    )
    assert code == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "code": "ondemand_forbidden",
        "fred_starts": 0,
        "bls_starts": 0,
        "observation_starts": 0,
        "credential_reads": 0,
        "required_flag": module.EXECUTE_FLAG,
    }
    assert loads == []


def test_the_cli_refuses_every_subcommand_without_an_explicit_root(capsys):
    module = _tool_module()
    for argv in (
        ["preflight"],
        ["readback"],
        ["canary", "--fred-ondemand-canary-execute"],
    ):
        assert (
            module.run_cli(
                argv, clock=lambda: NOW, api_key_loader=lambda path: "unused"
            )
            == 2
        )
        assert json.loads(capsys.readouterr().out)["code"] == ("explicit_root_required")


def test_b8_readback_prints_coverage_success_and_attempts_offline(tmp_path, capsys):
    module = _tool_module()
    root = tmp_path / "ondemand"
    _full_discovery(module, root)
    capsys.readouterr()
    code = module.run_cli(
        ["readback", "--root", str(root)],
        clock=lambda: NOW,
        api_key_loader=lambda path: "unused",
    )
    printed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert printed["code"] == "readback_ok"
    assert printed["coverage"] == "USED"
    assert printed["success"]["due_slot"] == SLOT
    assert [row["state"] for row in printed["attempts"]] == ["SUCCEEDED"]
    assert printed["sidecar_present"] is True
    assert printed["sidecar_mode"] == "0o600"
    assert (
        printed["fred_starts"],
        printed["bls_starts"],
        printed["observation_starts"],
    ) == (0, 0, 0)
    assert "api_key" not in json.dumps(printed)


def test_b8_readback_on_an_empty_root_is_honest_about_having_nothing(tmp_path):
    module = _tool_module()
    result = module.run_readback_v1(root=tmp_path / "empty", clock=lambda: NOW)
    assert result["coverage"] is None
    assert result["success"] is None and result["attempts"] == []
    assert result["sidecar_present"] is False


def test_the_preflight_cli_runs_end_to_end_offline(tmp_path, capsys):
    module = _tool_module()
    credential = tmp_path / "credentials" / "fred.env"
    credential.parent.mkdir(mode=0o700, parents=True)
    credential.write_text("FRED_API_KEY=" + "a" * 32 + "\n")
    os.chmod(credential, 0o600)
    code = module.run_cli(
        [
            "preflight",
            "--root",
            str(tmp_path / "root"),
            "--credential",
            str(credential),
            "--campaign-root",
            str(tmp_path / "campaign"),
            "--production-sidecar",
            str(tmp_path / "production" / module.SIDECAR_NAME),
            "--ics-fixture",
            str(ROOT / "tests/fixtures/bls/official/bls-ics.md"),
        ],
        clock=lambda: NOW,
        api_key_loader=lambda path: "unused",
    )
    printed = json.loads(capsys.readouterr().out)
    assert code == 0 and printed["code"] == "preflight_ok"
    assert printed["production_sidecar_absent"] is True


# --- A33: the §5 consumption table, all nine rows in one place --------------


def _row(module, tmp_path, name):
    """Drive one §5 row end to end and return its consumed counts."""
    root = tmp_path / name
    if name in ("sidecar_invalid",):
        _seed_sidecar(
            module,
            root,
            mutate=lambda payload, mod: mod.encode_sidecar_payload_v1(
                dict(payload, payload_digest="0" * 64)
            ),
        )
        result, _fred = _run(
            module, root, fred_outcomes=(), bls=_FixtureBlsTransport([])
        )
    elif name == "sidecar_not_due_fresh":
        _full_discovery(module, root)
        result, _fred = _run(
            module,
            root,
            fred_outcomes=(),
            bls=_FixtureBlsTransport([]),
            now=NOW + timedelta(minutes=5),
        )
    elif name == "sidecar_due":
        _seed_sidecar(module, root)
        result, _fred = _run(
            module,
            root,
            fred_outcomes=(_observations_outcome(),),
            bls=_FixtureBlsTransport([]),
        )
    elif name == "step_1_fails":
        result, _fred = _run(
            module,
            root,
            fred_outcomes=(FredFixtureTransportOutcomeV1("timeout"),),
            bls=_bls(module),
        )
    elif name == "step_2_fails":
        result, _fred = _run(
            module,
            root,
            fred_outcomes=(
                _series_release_outcome(),
                FredFixtureTransportOutcomeV1("http_5xx"),
            ),
            bls=_bls(module),
        )
    elif name == "step_3_fails":
        result, _fred = _run(
            module,
            root,
            fred_outcomes=(_series_release_outcome(), _release_dates_outcome()),
            bls=_bls(module, code="timeout"),
        )
    elif name == "join_rejects":
        result, _fred = _run(
            module,
            root,
            fred_outcomes=(_series_release_outcome(), _release_dates_outcome()),
            bls=_bls(
                module,
                body=_ics_bytes().replace(b"20260807T083000", b"20260814T083000"),
            ),
        )
    elif name == "step_4_fails":
        result, _fred = _run(
            module,
            root,
            fred_outcomes=(
                _series_release_outcome(),
                _release_dates_outcome(),
                FredFixtureTransportOutcomeV1("http_429"),
            ),
            bls=_bls(module),
        )
    elif name == "guard_rejects":
        result, _fred = _run(
            module,
            root,
            fred_outcomes=(
                _series_release_outcome(),
                _release_dates_outcome(),
                _observations_outcome(rows=(MAY, JUNE), count=2),
            ),
            bls=_bls(module),
        )
    else:
        result, _fred = _full_discovery(module, root)
    return (
        result["fred_starts"],
        result["bls_starts"],
        result["observation_starts"],
    ), result


@pytest.mark.parametrize(
    "name,expected",
    [
        ("sidecar_invalid", (0, 0, 0)),
        ("sidecar_not_due_fresh", (0, 0, 0)),
        ("sidecar_due", (1, 0, 1)),
        ("step_1_fails", (1, 0, 0)),
        ("step_2_fails", (2, 0, 0)),
        ("step_3_fails", (2, 1, 0)),
        ("join_rejects", (2, 1, 0)),
        ("step_4_fails", (3, 1, 1)),
        ("guard_rejects", (3, 1, 1)),
        ("full_success", (3, 1, 1)),
    ],
)
def test_a33_every_row_of_the_consumption_table_is_exact(tmp_path, name, expected):
    module = _tool_module()
    counts, result = _row(module, tmp_path, name)
    assert counts == expected, result
    assert counts[0] <= module.FRED_MAX_STARTS
    assert counts[1] <= module.BLS_MAX_STARTS
    assert sum(counts[:2]) <= module.FRED_MAX_STARTS + module.BLS_MAX_STARTS


def test_a33_a_step_four_transport_failure_settles_a_typed_failure_only(tmp_path):
    module = _tool_module()
    counts, result = _row(module, tmp_path, "step_4_fails")
    assert counts == (3, 1, 1)
    assert result["code"] == "enrichment_omitted"
    assert result["failure_code"] == "TRANSIENT_TRANSPORT"
    assert result["coverage"] == "OMITTED_UNAVAILABLE"
    # The schedule survives the failure, so the next invocation reuses it.
    assert (
        module.load_schedule_sidecar_v1(
            tmp_path / "step_4_fails" / module.SIDECAR_NAME, now=NOW
        )
        is not None
    )


# ---------------------------------------------------------------------------
# H1-H6: BLS declared-contact User-Agent hotfix (RCA 2026-08-13).
#
# The static BLS_USER_AGENT carried no contact-owner information, violating
# BLS Terms for automated clients. These tests pin the contract of the
# replacement: a privately loaded declared-contact UA that is never printed
# or persisted.
# ---------------------------------------------------------------------------


def test_h1_no_non_contact_static_ua_constant_remains():
    """The old non-contact static UA must not survive in the source."""
    source = (ROOT / "tools/fred_ondemand_canary.py").read_text()
    assert "tokmo-fred-ondemand/1" not in source, (
        "the non-contact static UA constant was not removed"
    )
    # The constant name is removed entirely.
    assert "BLS_USER_AGENT =" not in source, (
        "BLS_USER_AGENT = assignment should be removed"
    )


def test_h2_load_bls_user_agent_reads_a_safe_0600_file_with_at_contact(tmp_path):
    """The loader mirrors the fd-anchored metadata-safe pattern of
    load_fred_api_key and requires a declared-contact value containing '@'.
    """
    module = _tool_module()
    ua_path = tmp_path / "bls_ua.env"
    ua_path.write_text("BLS_USER_AGENT=Tokmo-FRED-Ondemand/1 (ops@example.invalid)\n")
    os.chmod(ua_path, 0o600)
    ua = module.load_bls_user_agent(ua_path)
    assert ua == "Tokmo-FRED-Ondemand/1 (ops@example.invalid)"


def test_h2_load_bls_user_agent_rejects_an_absent_file(tmp_path):
    module = _tool_module()
    with pytest.raises(ValueError, match="credential_unsafe"):
        module.load_bls_user_agent(tmp_path / "nonexistent.env")


def test_h2_load_bls_user_agent_rejects_an_unsafe_mode(tmp_path):
    module = _tool_module()
    ua_path = tmp_path / "bls_ua.env"
    ua_path.write_text("BLS_USER_AGENT=Tokmo/1 (ops@example.invalid)\n")
    os.chmod(ua_path, 0o644)
    with pytest.raises(ValueError, match="credential_unsafe"):
        module.load_bls_user_agent(ua_path)


def test_h2_load_bls_user_agent_rejects_a_value_without_at(tmp_path):
    """A UA without '@' is not a declared-contact value per BLS Terms and
    the existing SEC declared-contact contract (config.py)."""
    module = _tool_module()
    ua_path = tmp_path / "bls_ua.env"
    ua_path.write_text("BLS_USER_AGENT=tokmo-no-contact\n")
    os.chmod(ua_path, 0o600)
    with pytest.raises(ValueError, match="credential_grammar"):
        module.load_bls_user_agent(ua_path)


def test_h2_load_bls_user_agent_rejects_non_printing_ascii(tmp_path):
    module = _tool_module()
    ua_path = tmp_path / "bls_ua.env"
    ua_path.write_bytes(b"BLS_USER_AGENT=Tokmo/1 (ops@\x1ftest.invalid)\n")
    os.chmod(ua_path, 0o600)
    with pytest.raises(ValueError, match="credential_grammar"):
        module.load_bls_user_agent(ua_path)


def test_h3_bls_ics_transport_requires_user_agent_at_construction():
    """Fail-closed: the transport must not be constructible without a
    declared-contact UA validated at construction time."""
    module = _tool_module()
    opener = module.build_bls_opener()
    # Missing user_agent keyword.
    with pytest.raises(TypeError):
        module.BlsIcsTransport(opener=opener)


def test_h3_bls_ics_transport_rejects_a_non_contact_ua():
    """The construction-time '@' check prevents a non-contact UA from ever
    reaching the wire."""
    module = _tool_module()
    opener = module.build_bls_opener()
    with pytest.raises(ValueError, match="bls_user_agent_no_contact"):
        module.BlsIcsTransport(opener=opener, user_agent="no-contact-here")


def test_h4_the_ua_is_never_reprable():
    """The UA value must not appear in the transport's repr."""
    module = _tool_module()
    opener = module.build_bls_opener()
    transport = module.BlsIcsTransport(
        opener=opener, user_agent="Tokmo/1 (secret@example.invalid)"
    )
    assert "secret@example.invalid" not in repr(transport)
    assert "secret@example.invalid" not in str(transport)


def test_h5_a_distinctive_ua_does_not_leak_into_the_result_or_sidecar(tmp_path):
    """Leak-safety: a distinctive UA injected through the real BlsIcsTransport
    type must not appear in any result field or sidecar byte.

    Unlike the old H5 (which checked only that the literal constant name
    ``BLS_USER_AGENT`` was absent from artifacts that never could contain it),
    this test passes a distinctive sentinel UA through ``BlsIcsTransport``,
    wraps that transport in the real ``BudgetedBlsTransportV1``, and monkey-
    patches the opener so no socket is constructed. The sentinel UA is
    therefore consumed by the real wire-building code inside ``__call__``
    before the mock returns fixture ICS bytes. If any code path the canary
    exercises were to persist or echo the UA, the sentinel would surface.
    """
    module = _tool_module()
    root = tmp_path / "ondemand"
    sentinel = "ZZ-LEAK-SENTINEL-ua@test.invalid"
    # Construct the real transport type with the sentinel UA.
    transport = module.BlsIcsTransport(
        opener=module.build_bls_opener(), user_agent=sentinel
    )

    class _FakeResponse:
        status = 200

        def __init__(self, body):
            self._body = body
            self.headers = {
                "Content-Encoding": "identity",
                "Content-Length": str(len(body)),
            }

        def read(self, limit=-1):
            return self._body[:limit] if limit > 0 else self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    ics_body = _ics_bytes()
    # Monkeypatch the opener so no socket is constructed; the request is
    # built (consuming the UA) but the response is a fixture ICS body.
    transport._opener.open = lambda request, timeout=None: _FakeResponse(ics_body)
    fred = FredFixtureTransportV1(
        outcomes=(
            _series_release_outcome(),
            _release_dates_outcome(),
            _observations_outcome(),
        )
    )
    bls_wire = module.BudgetedBlsTransportV1(transport, module.BlsStartBudget())
    result = module.run_canary_v1(
        paths=module.paths_for_root(root, credential=root.parent / "fred.env"),
        fred_transport=fred,
        bls_transport=bls_wire,
        api_key_provider=build_fixture_api_key_provider_v1(),
        clock=lambda: NOW,
        rediscover_schedule=False,
    )
    assert result["code"] == "enrichment_complete"
    result_blob = json.dumps(result)
    sidecar_blob = (root / module.SIDECAR_NAME).read_bytes().decode()
    # The sentinel must not appear in any output artifact.
    assert "ZZ-LEAK-SENTINEL" not in result_blob, "UA leaked into result"
    assert "ZZ-LEAK-SENTINEL" not in sidecar_blob, "UA leaked into sidecar"
    readback = module.run_readback_v1(root=root, clock=lambda: NOW)
    assert "ZZ-LEAK-SENTINEL" not in json.dumps(readback), "UA leaked into readback"


def test_h6_no_new_bls_host_or_url_literal_is_introduced():
    """The hotfix changes only the UA source; the BLS URL/host literals
    remain exactly as they were."""
    module = _tool_module()
    source = (ROOT / "tools/fred_ondemand_canary.py").read_text()
    assert source.count("https://www.bls.gov/schedule/news_release/bls.ics") == 1
    assert module.BLS_HOST == "www.bls.gov"


# ---------------------------------------------------------------------------
# H7-H9: CLI seam — typed refusal, loader/API-key non-call, path coherence.
# ---------------------------------------------------------------------------


def test_h7_canary_cli_emits_typed_refusal_when_bls_ua_is_absent(tmp_path, capsys):
    """When the BLS UA file is absent, the CLI must emit exactly one typed
    static JSON refusal with exit 2, zero starts/reads, and no value or
    path leak. Neither the API key loader nor any transport may be called.
    """
    module = _tool_module()
    root = tmp_path / "root"
    credential = tmp_path / "creds" / "fred.env"
    credential.parent.mkdir(mode=0o700, parents=True)
    credential.write_text("FRED_API_KEY=" + "a" * 32 + "\n")
    os.chmod(credential, 0o600)
    # BLS UA file does not exist at the sibling path.
    api_calls = []

    def api_key_loader(path):
        api_calls.append(path)

    def bls_ua_loader(path):
        raise ValueError("credential_unsafe")

    code = module.run_cli(
        [
            "canary",
            "--fred-ondemand-canary-execute",
            "--root",
            str(root),
            "--credential",
            str(credential),
            "--bls-ua-credential",
            str(tmp_path / "creds" / "bls_ua.env"),
        ],
        clock=lambda: NOW,
        api_key_loader=api_key_loader,
        bls_ua_loader=bls_ua_loader,
    )
    assert code == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "code": "bls_user_agent_unavailable",
        "fred_starts": 0,
        "bls_starts": 0,
        "observation_starts": 0,
        "credential_reads": 0,
    }
    # No value or path must leak.
    blob = json.dumps(printed)
    assert "bls_ua" not in blob
    assert "creds" not in blob
    assert "fred.env" not in blob
    # The API key loader must not have been called.
    assert api_calls == []


def test_h7_canary_cli_emits_typed_refusal_when_bls_ua_is_unsafe(tmp_path, capsys):
    """An unsafe-mode BLS UA file must produce the same typed refusal."""
    module = _tool_module()
    root = tmp_path / "root"
    credential = tmp_path / "creds" / "fred.env"
    credential.parent.mkdir(mode=0o700, parents=True)
    credential.write_text("FRED_API_KEY=" + "a" * 32 + "\n")
    os.chmod(credential, 0o600)
    ua_path = tmp_path / "creds" / "bls_ua.env"
    ua_path.write_text("BLS_USER_AGENT=Tokmo/1 (ops@test.invalid)\n")
    os.chmod(ua_path, 0o644)  # unsafe mode

    code = module.run_cli(
        [
            "canary",
            "--fred-ondemand-canary-execute",
            "--root",
            str(root),
            "--credential",
            str(credential),
            "--bls-ua-credential",
            str(ua_path),
        ],
        clock=lambda: NOW,
        api_key_loader=lambda path: "unused",
        bls_ua_loader=module.load_bls_user_agent,
    )
    assert code == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed["code"] == "bls_user_agent_unavailable"
    assert (
        printed["fred_starts"],
        printed["bls_starts"],
        printed["observation_starts"],
        printed["credential_reads"],
    ) == (0, 0, 0, 0)


def test_h8_default_bls_ua_path_is_sibling_of_default_credential(tmp_path, capsys):
    """When neither --credential nor --bls-ua-credential is supplied, the
    default BLS UA path must be the sibling of the default credential path
    (i.e. in the same ``~/.hermes/credentials/`` directory).

    This test constructs both files in a fake home directory and proves
    the CLI resolves the UA path correctly by reaching run_canary_v1
    (which would fail with a different error if the path were wrong).
    """
    import pwd as pwd_module

    module = _tool_module()
    fake_home = tmp_path / "fake_home"
    creds = fake_home / ".hermes" / "credentials"
    creds.mkdir(mode=0o700, parents=True)
    fred_env = creds / "fred.env"
    fred_env.write_text("FRED_API_KEY=" + "a" * 32 + "\n")
    os.chmod(fred_env, 0o600)
    bls_ua = creds / "bls_ua.env"
    bls_ua.write_text("BLS_USER_AGENT=Tokmo/1 (ops@test.invalid)\n")
    os.chmod(bls_ua, 0o600)

    root = tmp_path / "root"
    fred_outcomes = (
        _series_release_outcome(),
        _release_dates_outcome(),
        _observations_outcome(),
    )
    original_getpwuid = pwd_module.getpwuid

    def fake_getpwuid(uid):
        result = original_getpwuid(uid)
        return pwd_module.struct_passwd(
            (
                result.pw_name,
                result.pw_passwd,
                result.pw_uid,
                result.pw_gid,
                result.pw_gecos,
                str(fake_home),
                result.pw_shell,
            )
        )

    with mock.patch.object(pwd_module, "getpwuid", side_effect=fake_getpwuid):
        with (
            mock.patch.object(module, "FredLiveTransport") as mock_fred,
            mock.patch.object(module, "BlsIcsTransport") as mock_bls,
        ):
            mock_fred.return_value = FredFixtureTransportV1(
                outcomes=tuple(fred_outcomes)
            )
            mock_bls.return_value = _bls(module)
            module.run_cli(
                ["canary", "--fred-ondemand-canary-execute", "--root", str(root)],
                clock=lambda: NOW,
                api_key_loader=module.load_fred_api_key,
                bls_ua_loader=module.load_bls_user_agent,
            )
    # If the default BLS UA path resolution worked, we should reach
    # run_canary_v1 and get enrichment_complete or a failure code — not
    # bls_user_agent_unavailable.
    printed = json.loads(capsys.readouterr().out)
    assert printed["code"] != "bls_user_agent_unavailable", (
        "default BLS UA path was not resolved as sibling of credential"
    )


def test_h9_credential_override_makes_bls_ua_sibling_of_override(tmp_path, capsys):
    """When --credential is an override path, the default BLS UA path must
    be its sibling — not the production default."""
    module = _tool_module()
    root = tmp_path / "root"
    alt_creds = tmp_path / "alt_creds"
    alt_creds.mkdir(mode=0o700, parents=True)
    fred_env = alt_creds / "fred.env"
    fred_env.write_text("FRED_API_KEY=" + "b" * 32 + "\n")
    os.chmod(fred_env, 0o600)
    # BLS UA is in the alt directory (sibling of the override credential).
    bls_ua = alt_creds / "bls_ua.env"
    bls_ua.write_text("BLS_USER_AGENT=Tokmo/1 (ops@test.invalid)\n")
    os.chmod(bls_ua, 0o600)
    # Production-default sibling must NOT exist.
    prod_creds = tmp_path / "prod_creds"
    prod_creds.mkdir(mode=0o700, parents=True)
    prod_fred = prod_creds / "fred.env"
    prod_fred.write_text("FRED_API_KEY=" + "c" * 32 + "\n")
    os.chmod(prod_fred, 0o600)

    # Track which path the loader was called with.
    loaded_paths = []

    def tracking_loader(path):
        loaded_paths.append(str(path))
        return module.load_bls_user_agent(path)

    code = module.run_cli(
        [
            "canary",
            "--fred-ondemand-canary-execute",
            "--root",
            str(root),
            "--credential",
            str(fred_env),
        ],
        clock=lambda: NOW,
        api_key_loader=lambda path: "unused",
        bls_ua_loader=tracking_loader,
    )
    # The loader must have been called with the sibling of the override.
    assert len(loaded_paths) == 1
    assert loaded_paths[0] == str(alt_creds / "bls_ua.env"), (
        "default BLS UA path must be sibling of --credential override"
    )
