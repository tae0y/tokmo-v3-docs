"""Delta SDS §6 acceptance 6, 8a, 9, 15: the routine query cache.

Attempts are append-only settlement history keyed by
``(query_identity, due_slot, attempt_seq)``. A claim is one
``BEGIN IMMEDIATE`` transaction that reconciles stale claims, refuses to
start while any ``STARTED`` row is active, then allocates the next
sequence. Reconciliation converts an expired claim to a terminal local
failure and never deletes the row, because the transport may already have
started before the crash.
"""

import sqlite3
import stat
import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tokmo.records.fred_query_cache import (
    FredQueryCacheCorruption,
    FredQueryCacheV1,
    decode_fact,
    encode_fact,
)
from tokmo.values.fred_query import (
    FAILURE_COOLDOWN,
    LOCAL_STATE_UNKNOWN_START,
    SERIES_PAYEMS,
    STARTED_LEASE,
    FredCompletenessV1,
    FredObservationV1,
    FredQueryFailureCodeV1,
    FredSeriesQueryV1,
    FredValueStateV1,
    build_fred_series_fact_v1,
)

UTC = UTC
T0 = datetime(2026, 8, 7, 12, 45, tzinfo=UTC)
SLOT = "PAYEMS:2026-08-07"
OTHER_SLOT = "PAYEMS:2026-09-04"

QUERY = FredSeriesQueryV1(SERIES_PAYEMS, date(2026, 6, 1), date(2026, 7, 31), T0)
OTHER_QUERY = FredSeriesQueryV1(SERIES_PAYEMS, date(2026, 5, 1), date(2026, 7, 31), T0)


def _fact(*, query=QUERY, fetched_at=T0, value="151000"):
    observations = (
        FredObservationV1(
            date(2026, 6, 1),
            Decimal(150900),
            FredValueStateV1.PRESENT,
            date(2026, 7, 2),
            date(9999, 12, 31),
        ),
        FredObservationV1(
            date(2026, 7, 1),
            Decimal(value),
            FredValueStateV1.PRESENT,
            date(2026, 8, 7),
            date(9999, 12, 31),
        ),
    )
    return build_fred_series_fact_v1(
        query_identity=query.query_identity,
        series_id=SERIES_PAYEMS,
        observations=observations,
        fetched_at=fetched_at,
        release_id=None,
        release_date=None,
        vintage_date=None,
        completeness=FredCompletenessV1.COMPLETE,
    )


def _cache(root, *, opened_at=T0):
    return FredQueryCacheV1(root, opened_at=opened_at)


def _rows(root, statement, args=()):
    connection = sqlite3.connect(root / "fred-query-cache.sqlite3")
    try:
        return connection.execute(statement, args).fetchall()
    finally:
        connection.close()


def test_fresh_cache_is_private_and_carries_the_declared_schema(tmp_path):
    root = tmp_path / "fred-query"
    cache = _cache(root)
    path = root / "fred-query-cache.sqlite3"
    assert stat.S_IMODE(path.lstat().st_mode) == 0o600
    assert stat.S_IMODE(root.lstat().st_mode) == 0o700
    columns = {row[1] for row in _rows(root, "PRAGMA table_info('query_attempt')")}
    assert columns == {
        "query_identity",
        "due_slot",
        "attempt_seq",
        "state",
        "started_at",
        "finished_at",
        "failure_code",
    }
    assert {row[1] for row in _rows(root, "PRAGMA table_info('query_success')")} == {
        "query_identity",
        "series_id",
        "revision_identity",
        "fetched_at",
        "fact_json",
        "fact_digest",
        "due_slot",
    }
    key = [
        row[1] for row in _rows(root, "PRAGMA table_info('query_attempt')") if row[5]
    ]
    assert set(key) == {"query_identity", "due_slot", "attempt_seq"}
    assert _rows(root, "PRAGMA journal_mode") == [("delete",)]
    for suffix in ("-journal", "-wal", "-shm"):
        assert not Path(f"{path}{suffix}").exists()
    assert cache.read_success_for_series(SERIES_PAYEMS, now=T0) is None


def test_claim_allocates_sequences_and_blocks_while_a_claim_is_active(tmp_path):
    cache = _cache(tmp_path / "fred-query")
    first = cache.claim(QUERY.query_identity, SLOT, now=T0)
    assert first.attempt_seq == 1 and first.started_at == T0
    assert (
        cache.claim(QUERY.query_identity, SLOT, now=T0 + timedelta(minutes=5)) is None
    )
    cache.settle_failure(
        first, FredQueryFailureCodeV1.TRANSIENT_TRANSPORT, now=T0 + timedelta(seconds=2)
    )
    second = cache.claim(QUERY.query_identity, SLOT, now=T0 + timedelta(hours=7))
    assert second.attempt_seq == 2
    assert [
        row[0]
        for row in _rows(
            tmp_path / "fred-query",
            "SELECT attempt_seq FROM query_attempt ORDER BY attempt_seq",
        )
    ] == [1, 2]


def test_terminal_attempts_are_append_only_and_never_overwritten(tmp_path):
    root = tmp_path / "fred-query"
    cache = _cache(root)
    first = cache.claim(QUERY.query_identity, SLOT, now=T0)
    cache.settle_failure(
        first, FredQueryFailureCodeV1.RESPONSE_CONTRACT, now=T0 + timedelta(seconds=1)
    )
    second = cache.claim(QUERY.query_identity, SLOT, now=T0 + timedelta(hours=7))
    cache.settle_success(
        second, _fact(fetched_at=T0 + timedelta(hours=7)), now=T0 + timedelta(hours=7)
    )
    rows = _rows(
        root,
        "SELECT attempt_seq,state,failure_code FROM query_attempt ORDER BY attempt_seq",
    )
    assert rows == [(1, "FAILED", "RESPONSE_CONTRACT"), (2, "SUCCEEDED", None)]
    assert (
        cache.read_attempt_state(
            QUERY.query_identity, SLOT, now=T0 + timedelta(hours=8)
        ).highest_attempt_seq
        == 2
    )


def test_same_slot_stale_refresh_replaces_the_fact_and_preserves_history(tmp_path):
    root = tmp_path / "fred-query"
    cache = _cache(root)
    first = cache.claim(QUERY.query_identity, SLOT, now=T0)
    cache.settle_success(first, _fact(), now=T0)
    later = T0 + timedelta(hours=25)
    second = cache.claim(QUERY.query_identity, SLOT, now=later)
    assert second.attempt_seq == 2
    cache.settle_success(second, _fact(fetched_at=later), now=later)
    stored = cache.read_success_for_series(SERIES_PAYEMS, now=later)
    assert stored.fetched_at == later and stored.due_slot == SLOT
    assert stored.revision_identity == _fact().revision_identity
    assert len(_rows(root, "SELECT 1 FROM query_success")) == 1
    assert [
        row[0]
        for row in _rows(
            root, "SELECT attempt_seq FROM query_attempt ORDER BY attempt_seq"
        )
    ] == [1, 2]


def test_failure_never_deletes_or_overwrites_the_prior_success(tmp_path):
    cache = _cache(tmp_path / "fred-query")
    first = cache.claim(QUERY.query_identity, SLOT, now=T0)
    cache.settle_success(first, _fact(), now=T0)
    later = T0 + timedelta(hours=30)
    second = cache.claim(QUERY.query_identity, SLOT, now=later)
    cache.settle_failure(second, FredQueryFailureCodeV1.TRANSIENT_TRANSPORT, now=later)
    stored = cache.read_success_for_series(SERIES_PAYEMS, now=later)
    assert stored is not None and stored.fetched_at == T0
    state = cache.read_attempt_state(QUERY.query_identity, SLOT, now=later)
    assert state.last_failure_at == later
    assert state.last_failure_code == "TRANSIENT_TRANSPORT"
    assert state.active_started_at is None


def test_expired_claim_reconciles_to_local_state_unknown_start_and_is_kept(tmp_path):
    root = tmp_path / "fred-query"
    crashed = _cache(root)
    crashed.claim(QUERY.query_identity, SLOT, now=T0)
    inside = _cache(root, opened_at=T0 + STARTED_LEASE)
    state = inside.read_attempt_state(
        QUERY.query_identity, SLOT, now=T0 + STARTED_LEASE
    )
    assert state.active_started_at == T0
    assert _rows(root, "SELECT state FROM query_attempt") == [("STARTED",)]
    assert inside.claim(QUERY.query_identity, SLOT, now=T0 + STARTED_LEASE) is None

    after = T0 + STARTED_LEASE + timedelta(seconds=1)
    reopened = _cache(root, opened_at=after)
    assert _rows(
        root, "SELECT attempt_seq,state,failure_code,finished_at FROM query_attempt"
    ) == [
        (
            1,
            "FAILED",
            LOCAL_STATE_UNKNOWN_START,
            after.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        )
    ]
    state = reopened.read_attempt_state(QUERY.query_identity, SLOT, now=after)
    assert state.active_started_at is None and state.last_failure_at == after
    assert (
        reopened.claim(
            QUERY.query_identity,
            SLOT,
            now=after + FAILURE_COOLDOWN - timedelta(seconds=1),
        )
        is not None
    )


def test_same_process_tick_reconciles_without_reopen_and_is_idempotent(tmp_path):
    root = tmp_path / "fred-query"
    cache = _cache(root)
    cache.claim(QUERY.query_identity, SLOT, now=T0)
    after = T0 + STARTED_LEASE + timedelta(minutes=1)
    assert (
        cache.read_attempt_state(
            QUERY.query_identity, SLOT, now=after
        ).active_started_at
        is None
    )
    first = _rows(root, "SELECT * FROM query_attempt")
    assert cache.reconcile_stale_claims(now=after + timedelta(hours=9)) == 0
    assert _rows(root, "SELECT * FROM query_attempt") == first
    claim = cache.claim(QUERY.query_identity, SLOT, now=after + FAILURE_COOLDOWN)
    assert claim.attempt_seq == 2
    assert len(_rows(root, "SELECT * FROM query_attempt")) == 2


def test_concurrent_claims_insert_exactly_one_attempt(tmp_path):
    root = tmp_path / "fred-query"
    _cache(root)
    caches = [_cache(root) for _ in range(4)]
    claims, errors = [], []
    lock = threading.Lock()
    barrier = threading.Barrier(4, timeout=30)

    def worker(cache):
        try:
            barrier.wait()
            claim = cache.claim(QUERY.query_identity, SLOT, now=T0)
        except sqlite3.OperationalError:
            # A busy deadline is a legitimate non-claim: it inserts no row.
            return
        except BaseException as error:  # noqa: BLE001 - surfaced by the assert
            with lock:
                errors.append(repr(error))
            return
        if claim is not None:
            with lock:
                claims.append(claim)

    threads = [threading.Thread(target=worker, args=(cache,)) for cache in caches]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(claims) == 1 and claims[0].attempt_seq == 1
    assert len(_rows(root, "SELECT * FROM query_attempt")) == 1


def test_settlement_addresses_the_exact_three_part_primary_key(tmp_path):
    cache = _cache(tmp_path / "fred-query")
    claim = cache.claim(QUERY.query_identity, SLOT, now=T0)
    cache.settle_success(claim, _fact(), now=T0)
    with pytest.raises(ValueError, match="settlement_conflict"):
        cache.settle_success(claim, _fact(), now=T0)
    other = cache.claim(OTHER_QUERY.query_identity, OTHER_SLOT, now=T0)
    with pytest.raises(ValueError, match="settlement_fact_mismatch"):
        cache.settle_success(other, _fact(), now=T0)


def test_corrupt_stored_fact_is_typed_local_state_not_a_refetch(tmp_path):
    root = tmp_path / "fred-query"
    cache = _cache(root)
    claim = cache.claim(QUERY.query_identity, SLOT, now=T0)
    cache.settle_success(claim, _fact(), now=T0)
    connection = sqlite3.connect(root / "fred-query-cache.sqlite3")
    try:
        connection.execute("UPDATE query_success SET fact_json=?", (b'{"x":1}\n',))
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(FredQueryCacheCorruption):
        cache.read_success_for_series(SERIES_PAYEMS, now=T0)
    with pytest.raises(FredQueryCacheCorruption):
        _cache(root, opened_at=T0)


def test_fact_encoding_round_trips_and_rejects_tampered_bytes(tmp_path):
    fact = _fact()
    assert decode_fact(encode_fact(fact)) == fact
    with pytest.raises(FredQueryCacheCorruption):
        decode_fact(encode_fact(fact).replace(b"151000", b"999999"))
    with pytest.raises(FredQueryCacheCorruption):
        decode_fact(b"not json")


def test_reopen_by_fresh_pathname_preserves_state_and_leaves_no_sidecars(tmp_path):
    root = tmp_path / "fred-query"
    cache = _cache(root)
    claim = cache.claim(QUERY.query_identity, SLOT, now=T0)
    cache.settle_success(claim, _fact(), now=T0)
    reopened = FredQueryCacheV1(Path(str(root)), opened_at=T0 + timedelta(minutes=1))
    stored = reopened.read_success_for_series(
        SERIES_PAYEMS, now=T0 + timedelta(minutes=1)
    )
    assert stored.query_identity == QUERY.query_identity
    assert stored.fact == _fact()
    assert stored.due_slot == SLOT
    assert (
        reopened.read_attempt_state(
            QUERY.query_identity, SLOT, now=T0 + timedelta(minutes=1)
        ).highest_attempt_seq
        == 1
    )
    path = root / "fred-query-cache.sqlite3"
    for suffix in ("-journal", "-wal", "-shm"):
        assert not Path(f"{path}{suffix}").exists()


def test_cache_requires_an_injected_root_and_aware_utc_instants(tmp_path):
    with pytest.raises(ValueError, match="injected_root_required"):
        FredQueryCacheV1(str(tmp_path / "fred-query"), opened_at=T0)
    with pytest.raises(ValueError, match="aware_utc_required"):
        FredQueryCacheV1(
            tmp_path / "fred-query", opened_at=datetime(2026, 8, 7, 12, 45)
        )
    cache = _cache(tmp_path / "fred-query")
    with pytest.raises(ValueError, match="aware_utc_required"):
        cache.claim(QUERY.query_identity, SLOT, now=datetime(2026, 8, 7, 12, 45))
    with pytest.raises(ValueError, match="invalid_query_identity"):
        cache.claim("short", SLOT, now=T0)


# --- AR-20: count_observation_attempts_v1 is observation-only (M-1) -----------


def test_count_observation_attempts_is_zero_on_a_fresh_cache(tmp_path):
    cache = _cache(tmp_path / "fred-query")
    assert cache.count_observation_attempts_v1() == 0


def test_count_observation_attempts_grows_by_one_per_claim(tmp_path):
    root = tmp_path / "fred-query"
    cache = _cache(root)
    cache.claim(QUERY.query_identity, SLOT, now=T0)
    assert cache.count_observation_attempts_v1() == 1
    cache.settle_failure(
        cache.claim(QUERY.query_identity, SLOT, now=T0 + timedelta(hours=7)),
        FredQueryFailureCodeV1.TRANSIENT_TRANSPORT,
        now=T0 + timedelta(hours=7),
    )
    assert _cache(root).count_observation_attempts_v1() == 2
    assert [
        row[0]
        for row in _rows(
            root, "SELECT attempt_seq FROM query_attempt ORDER BY attempt_seq"
        )
    ] == [1, 2]


def test_count_observation_attempts_is_a_read_only_count_with_no_mutation(tmp_path):
    """The accessor must open no mutating transaction."""
    root = tmp_path / "fred-query"
    cache = _cache(root)
    cache.claim(QUERY.query_identity, SLOT, now=T0)
    before = _rows(root, "SELECT * FROM query_attempt")
    count = cache.count_observation_attempts_v1()
    after = _rows(root, "SELECT * FROM query_attempt")
    assert count == 1
    assert before == after
