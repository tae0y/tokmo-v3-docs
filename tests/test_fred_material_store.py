import hashlib
import os
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from tokmo.records.fred_material_store import (
    DDL,
    EXPECTED_SCHEMA,
    SCHEMA_FINGERPRINT,
    FredMaterialStore,
    _schema_semantics,
)
from tokmo.values.fred_macro import (
    R2_CAMPAIGN_CODE,
    ExecutionMode,
    FredOperation,
    FredRequest,
    invocation_identity,
)


def _started(second: int = 0):
    return datetime(2026, 1, 1, 0, 0, second, tzinfo=UTC)


def test_reservation_cap_replay_and_immutable_acquisition_split(tmp_path):
    root = tmp_path / "private"
    store = FredMaterialStore(root)
    request = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    first = store.reserve(request, started_at=_started())
    assert first.code == "reserved" and first.ordinal == 1
    store.settle(first, "SUCCEEDED")
    assert (
        store.reserve(request, started_at=_started(1)).code
        == "already_attempted_fixture"
    )
    db = root / "fred.sqlite3"
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600
    source = {"operation": "SERIES", "series_id": "PAYEMS", "title": "Employment"}
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 1, 2, tzinfo=UTC)
    second = store.reserve(
        FredRequest(FredOperation.RELEASE_DATES, release_id=200, limit=1),
        started_at=_started(1),
    )
    third = store.reserve(
        FredRequest(FredOperation.RELEASE_DATES, release_id=201, limit=1),
        started_at=_started(2),
    )
    one = store.store_material(
        source,
        material_identity="series:PAYEMS",
        invocation_identity=second.invocation_identity,
        ordinal=second.ordinal,
        fetched_at=t1,
        batch_identity="b1",
    )
    two = store.store_material(
        source,
        material_identity="series:PAYEMS",
        invocation_identity=third.invocation_identity,
        ordinal=third.ordinal,
        fetched_at=t2,
        batch_identity="b2",
    )
    assert one.source_sha256 == two.source_sha256
    assert one.available_at == t1
    with sqlite3.connect(db) as connection:
        material = connection.execute(
            "SELECT source_canonical_bytes,source_sha256,available_at FROM materials"
        ).fetchone()
        acquisitions = connection.execute(
            "SELECT count(*) FROM acquisitions"
        ).fetchone()[0]
        assert hashlib.sha256(material[0]).hexdigest() == material[1]
        assert b"fetched_at" not in material[0] and b"available_at" not in material[0]
        assert acquisitions == 2
    with pytest.raises(ValueError, match="immutable_conflict"):
        fourth = store.reserve(
            FredRequest(FredOperation.RELEASE_DATES, release_id=202, limit=1),
            started_at=_started(3),
        )
        store.store_material(
            dict(source, title="Changed"),
            material_identity="series:PAYEMS",
            invocation_identity=fourth.invocation_identity,
            ordinal=fourth.ordinal,
            fetched_at=t2,
            batch_identity="b3",
        )

    store = FredMaterialStore(tmp_path / "cap")
    for number in range(1, 11):
        other = FredRequest(FredOperation.RELEASE_DATES, release_id=number, limit=1)
        reservation = store.reserve(other, started_at=_started(number))
        assert reservation.code == "reserved"
    capped = store.reserve(
        FredRequest(FredOperation.RELEASE_DATES, release_id=99, limit=1),
        started_at=_started(11),
    )
    assert capped.code == "cap_exhausted"


def test_cap_minus_one_concurrency_allows_exactly_one_start(tmp_path):
    store = FredMaterialStore(tmp_path / "race")
    for release_id in range(1, 10):
        assert (
            store.reserve(
                FredRequest(
                    FredOperation.RELEASE_DATES, release_id=release_id, limit=1
                ),
                started_at=_started(release_id),
            ).code
            == "reserved"
        )
    barrier = threading.Barrier(2)

    def reserve(release_id):
        barrier.wait(timeout=5)
        return store.reserve(
            FredRequest(FredOperation.RELEASE_DATES, release_id=release_id, limit=1),
            started_at=_started(10),
        ).code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = tuple(pool.map(reserve, (100, 101)))
    assert sorted(codes) == ["cap_exhausted", "reserved"]


def test_fixture_provenance_join_reconciliation_and_zero_rows_fail_closed(tmp_path):
    store = FredMaterialStore(tmp_path / "private")
    request = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    reservation = store.reserve(
        request, execution_mode=ExecutionMode.FIXTURE, started_at=_started()
    )
    assert reservation.invocation_identity == invocation_identity(
        request, ExecutionMode.FIXTURE
    )
    assert store.reconcile_unknown() == 1
    assert (
        store.reserve(
            request, execution_mode=ExecutionMode.FIXTURE, started_at=_started(1)
        ).code
        == "already_attempted_fixture"
    )
    with pytest.raises(ValueError, match="empty_page"):
        store.store_page(
            (),
            reservation=reservation,
            operation="SERIES",
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_store_page_real_invocation_join_and_singleton_batch_evidence(tmp_path):
    store = FredMaterialStore(tmp_path / "private")
    request = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    reservation = store.reserve(
        request, execution_mode=ExecutionMode.FIXTURE, started_at=_started()
    )
    row = ({"series_id": "PAYEMS", "title": "Employment"},)
    store.store_page(
        row,
        reservation=reservation,
        operation="SERIES",
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        provider_count=None,
        completeness="COMPLETE",
        completeness_evidence="OFFICIAL_SINGLETON_ENVELOPE",
    )
    with sqlite3.connect(store.path) as connection:
        joined = connection.execute(
            "SELECT count(*) FROM acquisitions a JOIN attempts t USING(invocation_identity)"
        ).fetchone()[0]
        assert joined == 1
        assert (
            connection.execute("SELECT execution_mode FROM acquisitions").fetchone()[0]
            == "FIXTURE"
        )
        batch = connection.execute(
            "SELECT row_count,provider_count,completeness,completeness_evidence FROM batches"
        ).fetchone()
        assert batch == (1, None, "COMPLETE", "OFFICIAL_SINGLETON_ENVELOPE")


def test_store_page_immutable_conflict_rolls_back_entire_transaction(tmp_path):
    store = FredMaterialStore(tmp_path / "private")
    initial = store.reserve(
        FredRequest(FredOperation.SERIES, series_id="PAYEMS"), started_at=_started()
    )
    fetched = datetime(2026, 1, 1, tzinfo=UTC)
    store.store_page(
        ({"series_id": "PAYEMS", "title": "Employment"},),
        reservation=initial,
        operation="SERIES",
        fetched_at=fetched,
    )

    conflicting = store.reserve(
        FredRequest(FredOperation.SERIES, series_id="UNRATE"), started_at=_started(1)
    )
    with sqlite3.connect(store.path) as connection:
        before = {
            table: connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
            for table in ("materials", "acquisitions", "batches", "attempts")
        }
    evidence_before = store.evidence_path.read_bytes()

    with pytest.raises(ValueError, match="immutable_conflict"):
        store.store_page(
            (
                {"series_id": "UNRATE", "title": "Unemployment"},
                {"series_id": "PAYEMS", "title": "Mutated employment"},
            ),
            reservation=conflicting,
            operation="SERIES",
            fetched_at=datetime(2026, 1, 2, tzinfo=UTC),
        )

    reopened = FredMaterialStore(store.root)
    with sqlite3.connect(reopened.path) as connection:
        after = {
            table: connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
            for table in ("materials", "acquisitions", "batches", "attempts")
        }
    assert after == before
    assert reopened.evidence_path.read_bytes() == evidence_before


def test_durable_wall_start_spacing_reserves_before_io(tmp_path):
    store = FredMaterialStore(tmp_path / "private")
    base = datetime(2026, 1, 1, tzinfo=UTC)
    one = store.reserve(
        FredRequest(FredOperation.RELEASE_DATES, release_id=1, limit=1),
        execution_mode=ExecutionMode.FIXTURE,
        started_at=base,
    )
    too_soon = store.reserve(
        FredRequest(FredOperation.RELEASE_DATES, release_id=2, limit=1),
        execution_mode=ExecutionMode.FIXTURE,
        started_at=datetime(2026, 1, 1, 0, 0, 0, 500000, tzinfo=UTC),
    )
    spaced = store.reserve(
        FredRequest(FredOperation.RELEASE_DATES, release_id=2, limit=1),
        execution_mode=ExecutionMode.FIXTURE,
        started_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
    )
    assert one.code == "reserved"
    assert too_soon.code == "pacing_required"
    assert spaced.code == "reserved"


def test_fixture_attempts_and_material_cannot_satisfy_live_identity_or_cap(tmp_path):
    store = FredMaterialStore(tmp_path / "private")
    request = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    fixture = store.reserve(
        request, execution_mode=ExecutionMode.FIXTURE, started_at=_started()
    )
    store.ensure_live_campaign()
    with pytest.raises(ValueError, match="fixture_live_campaign_forbidden"):
        store.reserve(
            request, execution_mode=ExecutionMode.FIXTURE, started_at=_started(1)
        )
    live = store.reserve(
        request, execution_mode=ExecutionMode.LIVE, started_at=_started()
    )
    assert fixture.invocation_identity != live.invocation_identity
    assert fixture.ordinal == live.ordinal == 1
    row = ({"series_id": "PAYEMS", "title": "Employment"},)
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="fixture_live_campaign_forbidden"):
        store.store_page(
            row, reservation=fixture, operation="SERIES", fetched_at=instant
        )
    store.store_page(row, reservation=live, operation="SERIES", fetched_at=instant)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM materials").fetchone()[0] == 1
        assert set(
            row[0] for row in connection.execute("SELECT execution_mode FROM materials")
        ) == {"LIVE"}


def test_release_date_evidence_is_preserved_without_inventing_an_instant(tmp_path):
    store = FredMaterialStore(tmp_path / "private")
    reservation = store.reserve(
        FredRequest(FredOperation.RELEASE_DATES, release_id=50, limit=1),
        started_at=_started(),
    )
    fetched = datetime(2026, 1, 2, 12, tzinfo=UTC)
    store.store_page(
        ({"release_id": 50, "date": "2026-01-02"},),
        reservation=reservation,
        operation="RELEASE_DATES",
        fetched_at=fetched,
    )
    with sqlite3.connect(store.path) as connection:
        available, evidence, source_date = connection.execute(
            "SELECT available_at,available_at_evidence,source_release_date_evidence FROM acquisitions"
        ).fetchone()
    assert datetime.fromisoformat(available.replace("Z", "+00:00")) == fetched
    assert evidence == "FETCHED_AT"
    assert source_date == "2026-01-02"


def test_v4_campaign_schema_and_campaign_local_mapping(tmp_path):
    store = FredMaterialStore(tmp_path / "v4")
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        } >= {"campaigns", "campaign_attempts"}
    store.ensure_live_campaign(campaign_code=R2_CAMPAIGN_CODE)
    first = store.reserve_campaign(
        R2_CAMPAIGN_CODE,
        1,
        FredRequest(FredOperation.SERIES, series_id="PAYEMS"),
        started_at=_started(),
    )
    assert first.code == "reserved" and first.ordinal == 1
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT campaign_code,manifest_ordinal,invocation_identity FROM campaign_attempts"
        ).fetchone() == (R2_CAMPAIGN_CODE, 1, first.invocation_identity)
        assert connection.execute(
            "SELECT terminal_state,campaign_cap FROM campaigns"
        ).fetchone() == ("ACTIVE", 10)


def test_campaign_reservation_aggregate_cap_is_atomic(tmp_path):
    store = FredMaterialStore(tmp_path / "campaign-cap")
    store.ensure_live_campaign(campaign_code=R2_CAMPAIGN_CODE)
    with sqlite3.connect(store.path) as connection:
        for ordinal in range(1, 21):  # DB 20 + external 0 = 20 → cap_exhausted
            connection.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "FRED",
                    "CANONICAL_SOURCE_STAGE3",
                    f"{ordinal:064x}",
                    ordinal,
                    "SERIES",
                    f"{ordinal + 100:064x}",
                    "LIVE",
                    _started(ordinal).isoformat(),
                    "FAILED",
                    "FRED_G_LIVE_PAYEMS_R1",
                    "legacy",
                    "0" * 64,
                ),
            )
    result = store.reserve_campaign(
        R2_CAMPAIGN_CODE,
        1,
        FredRequest(FredOperation.SERIES, series_id="PAYEMS"),
        started_at=_started(21),
    )
    assert result.code == "cap_exhausted"


def test_v4_schema_fingerprint_is_pinned():
    """m2: The V4 fingerprint is pinned to the exact reviewed value."""
    assert (
        SCHEMA_FINGERPRINT
        == "e59e518dcacd24286f84b0538fa90eebd697a876a5e77abd0f854ddfa01ec580"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        (
            "campaign_cap INTEGER NOT NULL CHECK(campaign_cap=10)",
            "campaign_cap INTEGER NOT NULL CHECK(campaign_cap=11)",
        ),
        (
            "FOREIGN KEY (campaign_code) REFERENCES campaigns(campaign_code)",
            "FOREIGN KEY (campaign_code) REFERENCES campaigns(manifest_code)",
        ),
        (
            "FOREIGN KEY (invocation_identity) REFERENCES attempts(invocation_identity)",
            "FOREIGN KEY (invocation_identity) REFERENCES attempts(request_identity)",
        ),
        (
            "CREATE INDEX campaign_attempts_invocation_idx ON campaign_attempts(invocation_identity);",
            "",
        ),
    ],
)
def test_v4_fingerprint_kills_check_each_fk_and_explicit_index(mutation):
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(DDL.replace(*mutation))
        assert _schema_semantics(connection) != EXPECTED_SCHEMA
    finally:
        connection.close()
