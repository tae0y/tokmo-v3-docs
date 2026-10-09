"""Quotes/trades provider→consumer cutover seam (Plan §18/§8.3).

Before cutover: 300s remains the quotes/trades provider owner.
After cutover: Pulse is the provider, 300s becomes a consumer reading
LatestQuoteStore and TradeLedger with wire0.

This seam validates: before/after owner gap0/dup0, 300s consumer reads
latest quote/ledger wire0, active production constant unchanged.
"""

import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest

from tokmo.values.operation_lane_registry import (
    OPERATION_LANE_REGISTRY_V1,
    OperationLane,
    OperationLaneRegistryV1,
    OperationLaneV1,
    active_owner,
)

CLOCK_NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
CUTOVER_EPOCH = int(datetime(2026, 8, 10, 12, 0, tzinfo=UTC).timestamp())
# Align to 300s grid
CUTOVER_EPOCH = CUTOVER_EPOCH - (CUTOVER_EPOCH % 300) + 300

# Trade ledger fixtures (mirrors test_trade_ledger_store.py patterns).
SOURCE = "retirement_excel_v1"
SCOPE = "retirement_pension_1"


def _ledger_event(number):
    from datetime import date

    from tokmo.values.trade_ledger import TradeLedgerEvent

    return TradeLedgerEvent(
        source=SOURCE,
        account_scope=SCOPE,
        source_record_key=f"row-{number:06d}",
        event_kind="EXECUTION",
        supersedes_source_record_key=None,
        trade_date=date(2026, 1, 1),
        transaction_type_raw="ETF매수",
        instrument_raw=f"Fixture ETF {number % 4}",
        quantity=str(1 + number % 5),
        unit_price=str(1000 + number),
        amount=str((1 + number % 5) * (1000 + number)),
        medium_raw="FixtureApp",
        currency_raw=None,
        settlement_date=None,
        fee=None,
        tax=None,
    )


def _audit():
    from tokmo.values.trade_ledger import ImportAuditMeta

    return ImportAuditMeta(file_sha256="ab" * 32, importer_version="fixture-importer-1")


def _make_store(tmp_path, *, clock_times=None):
    """Create a store with an injected clock that returns a sequence of
    times. If clock_times is provided, the clock pops from it; otherwise
    uses CLOCK_NOW."""
    from tokmo.records.trade_ledger_store import TradeLedgerStore

    times = list(clock_times) if clock_times else [CLOCK_NOW]
    counter = {"i": 0}

    def clock():
        idx = min(counter["i"], len(times) - 1)
        counter["i"] += 1
        return times[idx]

    ids = iter(f"event-id-{i:04d}" for i in range(10000))
    return TradeLedgerStore(
        tmp_path / "stores" / "trade_ledger.sqlite3",
        clock=clock,
        id_factory=lambda: next(ids),
    )


def test_before_cutover_300s_owns_quotes_and_trades():
    """Pre-cutover: the registry assigns PRICE_BATCH, ORDERS_PAGE,
    ORDER_DETAIL to CADENCE_300S."""
    entry = OPERATION_LANE_REGISTRY_V1.entries["PRICE_BATCH"]
    pre_epoch = CUTOVER_EPOCH - 1
    assert active_owner(entry, epoch=pre_epoch) is OperationLane.CADENCE_300S

    entry = OPERATION_LANE_REGISTRY_V1.entries["ORDERS_PAGE"]
    assert active_owner(entry, epoch=pre_epoch) is OperationLane.CADENCE_300S

    entry = OPERATION_LANE_REGISTRY_V1.entries["ORDER_DETAIL"]
    assert active_owner(entry, epoch=pre_epoch) is OperationLane.CADENCE_300S


def test_after_cutover_pulse_owns_quotes_and_trades():
    """Post-cutover: Pulse owns PRICE_BATCH/ORDERS_PAGE/ORDER_DETAIL."""
    post_cutover_registry = OperationLaneRegistryV1(
        entries={
            op: (
                OperationLaneV1(
                    op,
                    OperationLane.PULSE_60S,
                    OperationLane.CADENCE_300S,
                    CUTOVER_EPOCH,
                )
                if op in ("PRICE_BATCH", "ORDERS_PAGE", "ORDER_DETAIL")
                else OperationLaneV1(op, OperationLane.CADENCE_300S, None, None)
            )
            for op in sorted(OPERATION_LANE_REGISTRY_V1.entries)
        }
    )
    post_epoch = CUTOVER_EPOCH + 1
    assert (
        active_owner(
            post_cutover_registry.entries["PRICE_BATCH"],
            epoch=post_epoch,
        )
        is OperationLane.PULSE_60S
    )
    assert (
        active_owner(
            post_cutover_registry.entries["ORDERS_PAGE"],
            epoch=post_epoch,
        )
        is OperationLane.PULSE_60S
    )
    assert (
        active_owner(
            post_cutover_registry.entries["ORDER_DETAIL"],
            epoch=post_epoch,
        )
        is OperationLane.PULSE_60S
    )


def test_cutover_boundary_no_gap_no_dup():
    """At the exact cutover epoch, the owner flips with no gap and no dual
    ownership. The epoch-aligned boundary means the slot starting at
    CUTOVER_EPOCH belongs to Pulse, and the slot ending at CUTOVER_EPOCH
    belongs to 300s."""
    post_cutover_registry = OperationLaneRegistryV1(
        entries={
            op: (
                OperationLaneV1(
                    op,
                    OperationLane.PULSE_60S,
                    OperationLane.CADENCE_300S,
                    CUTOVER_EPOCH,
                )
                if op in ("PRICE_BATCH", "ORDERS_PAGE", "ORDER_DETAIL")
                else OperationLaneV1(op, OperationLane.CADENCE_300S, None, None)
            )
            for op in sorted(OPERATION_LANE_REGISTRY_V1.entries)
        }
    )
    entry = post_cutover_registry.entries["PRICE_BATCH"]
    # One epoch before: 300s owns it
    assert active_owner(entry, epoch=CUTOVER_EPOCH - 1) is OperationLane.CADENCE_300S
    # At cutover epoch: Pulse owns it
    assert active_owner(entry, epoch=CUTOVER_EPOCH) is OperationLane.PULSE_60S
    # One epoch after: Pulse owns it
    assert active_owner(entry, epoch=CUTOVER_EPOCH + 1) is OperationLane.PULSE_60S


def test_consumer_reads_latest_quote_wire_zero(tmp_path):
    """Post-cutover, a 300s consumer reads LatestQuoteStore rows without
    any provider wire — wire0."""
    from tokmo.programs.quotes_trades_consumer import read_latest_quotes_for_consumer
    from tokmo.records.latest_quotes import LatestQuoteStore
    from tokmo.values.market_observations import TossCurrentPriceFact

    store = LatestQuoteStore(
        tmp_path / "stores" / "latest_quotes.sqlite3",
        clock=lambda: CLOCK_NOW,
    )
    fact = TossCurrentPriceFact(
        source="toss-current-price-v1",
        symbol="AAPL",
        last_price="220.5",
        currency_raw="USD",
        timestamp=CLOCK_NOW,
        fetched_at=CLOCK_NOW,
    )
    store.save_batch((fact,))
    rows = read_latest_quotes_for_consumer(
        store=store,
        source="toss-current-price-v1",
        symbols=("AAPL",),
    )
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"


# --- M1/m4: real ledger event boundary tests (RED → GREEN) ---


def test_consumer_reads_ledger_window_with_real_event(tmp_path):
    """Post-cutover, a consumer reads a real ledger event that was
    persisted inside the window — not an empty default-returns path."""
    from tokmo.programs.quotes_trades_consumer import (
        read_ledger_delta_for_consumer,
    )

    t0 = CLOCK_NOW
    t1 = t0 + timedelta(seconds=1)
    t2 = t0 + timedelta(minutes=5)
    store = _make_store(tmp_path, clock_times=[t1])
    store.append_batch((_ledger_event(1),), import_audit=_audit())

    rows = read_ledger_delta_for_consumer(
        store=store,
        from_stored_at=t0,
        to_stored_at=t2,
        source=SOURCE,
        account_scope=SCOPE,
    )
    assert len(rows) == 1
    assert rows[0].event.source_record_key == "row-000001"
    assert rows[0].stored_at == t1


def test_consumer_ledger_half_open_prev_included_current_excluded(tmp_path):
    """The window [prev, cur) includes prev and excludes cur. Events
    persisted exactly at prev are included; events persisted exactly at
    cur are excluded."""
    from tokmo.programs.quotes_trades_consumer import (
        read_ledger_delta_for_consumer,
    )

    prev = CLOCK_NOW
    cur = prev + timedelta(minutes=5)

    # Append two events in one batch with distinct source_record_keys.
    # The clock returns the same stored_at for all events in a batch.
    # Use source_record_keys that pass the sequential contract.
    store = _make_store(tmp_path, clock_times=[prev])
    events = (_ledger_event(1), _ledger_event(2))
    store.append_batch(events, import_audit=_audit())

    # All events stored at prev → included in [prev, cur)
    rows = read_ledger_delta_for_consumer(
        store=store,
        from_stored_at=prev,
        to_stored_at=cur,
        source=SOURCE,
        account_scope=SCOPE,
    )
    assert len(rows) == 2

    # A window [cur, cur+5m) that excludes prev → zero rows
    after = read_ledger_delta_for_consumer(
        store=store,
        from_stored_at=cur,
        to_stored_at=cur + timedelta(minutes=5),
        source=SOURCE,
        account_scope=SCOPE,
    )
    assert len(after) == 0


def test_consumer_ledger_adjacent_windows_gap0_dup0(tmp_path):
    """Two adjacent windows [t0, t1) and [t1, t2) must cover all events
    with zero gap and zero duplication — no event missed, no event
    counted twice. Events are stored at distinct timestamps that straddle
    the boundary."""
    from tokmo.programs.quotes_trades_consumer import (
        read_ledger_delta_for_consumer,
    )

    t0 = CLOCK_NOW
    # Batch 1: events 1-3, stored at t0+1s
    # Batch 2: events 4-6, stored at t0+3s
    # Use a source/scope with non-sequential contract to allow separate
    # batches, or accumulate sequentially.
    # Since retirement_excel_v1 requires sequential full history, we
    # append a prefix (1-3) then cumulative (1-6) to get 4-6 as new.
    mid_t = t0 + timedelta(seconds=2)
    e2_t = t0 + timedelta(seconds=1)  # batch 1 stored_at
    e2_t2 = t0 + timedelta(seconds=3)  # batch 2 stored_at

    store = _make_store(tmp_path, clock_times=[e2_t, e2_t2])

    # First batch: events 1-3
    batch1 = tuple(_ledger_event(n) for n in range(1, 4))
    store.append_batch(batch1, import_audit=_audit())

    # Second batch: cumulative 1-6 (1-3 are duplicates, 4-6 are new)
    batch2 = tuple(_ledger_event(n) for n in range(1, 7))
    store.append_batch(batch2, import_audit=_audit())

    # Window 1: [t0, mid_t) → events from batch 1 only (stored at e2_t)
    w1 = read_ledger_delta_for_consumer(
        store=store,
        from_stored_at=t0,
        to_stored_at=mid_t,
        source=SOURCE,
        account_scope=SCOPE,
    )
    w1_keys = {r.event.source_record_key for r in w1}
    assert w1_keys == {"row-000001", "row-000002", "row-000003"}

    # Window 2: [mid_t, mid_t+5m) → events from batch 2 (stored at e2_t2)
    w2 = read_ledger_delta_for_consumer(
        store=store,
        from_stored_at=mid_t,
        to_stored_at=mid_t + timedelta(minutes=5),
        source=SOURCE,
        account_scope=SCOPE,
    )
    w2_keys = {r.event.source_record_key for r in w2}
    assert w2_keys == {"row-000004", "row-000005", "row-000006"}

    # Gap0/dup0: no overlap
    assert w1_keys & w2_keys == set()
    assert w1_keys | w2_keys == {
        "row-000001",
        "row-000002",
        "row-000003",
        "row-000004",
        "row-000005",
        "row-000006",
    }


def test_consumer_ledger_equivalent_timezone_offsets_return_same_rows(tmp_path):
    """Window bounds representing the same instants must not change results."""
    from tokmo.programs.quotes_trades_consumer import read_ledger_delta_for_consumer

    stored_at = CLOCK_NOW + timedelta(minutes=30)
    store = _make_store(tmp_path, clock_times=[stored_at])
    store.append_batch((_ledger_event(1),), import_audit=_audit())

    utc_rows = read_ledger_delta_for_consumer(
        store=store,
        from_stored_at=CLOCK_NOW,
        to_stored_at=CLOCK_NOW + timedelta(hours=1),
        source=SOURCE,
        account_scope=SCOPE,
    )
    kst = timezone(timedelta(hours=9))
    kst_rows = read_ledger_delta_for_consumer(
        store=store,
        from_stored_at=CLOCK_NOW.astimezone(kst),
        to_stored_at=(CLOCK_NOW + timedelta(hours=1)).astimezone(kst),
        source=SOURCE,
        account_scope=SCOPE,
    )

    assert kst_rows == utc_rows
    assert len(kst_rows) == 1


def test_consumer_ledger_normalizes_non_utc_store_clock(tmp_path):
    """Persisted timestamps are canonical UTC even if the store clock is KST."""
    from tokmo.programs.quotes_trades_consumer import read_ledger_delta_for_consumer

    kst = timezone(timedelta(hours=9))
    stored_at = (CLOCK_NOW + timedelta(minutes=30)).astimezone(kst)
    store = _make_store(tmp_path, clock_times=[stored_at])
    store.append_batch((_ledger_event(1),), import_audit=_audit())

    rows = read_ledger_delta_for_consumer(
        store=store,
        from_stored_at=CLOCK_NOW,
        to_stored_at=CLOCK_NOW + timedelta(hours=1),
        source=SOURCE,
        account_scope=SCOPE,
    )

    assert len(rows) == 1
    assert rows[0].stored_at == stored_at.astimezone(UTC)


def test_consumer_ledger_missing_scope_raises_not_empty(tmp_path):
    """Empty source or account_scope must raise ValueError, not silently
    return []."""
    from tokmo.programs.quotes_trades_consumer import (
        read_ledger_delta_for_consumer,
    )

    store = _make_store(tmp_path)
    with pytest.raises(ValueError):
        read_ledger_delta_for_consumer(
            store=store,
            from_stored_at=CLOCK_NOW - timedelta(minutes=5),
            to_stored_at=CLOCK_NOW,
            source="",
            account_scope="",
        )


# --- M2: exception propagation tests ---


def test_consumer_ledger_corrupt_store_propagates_exception(tmp_path):
    """A corrupt store must propagate a typed StoreCorruptError, not be
    masked as an empty list."""
    from tokmo.records.trade_ledger_privacy import StoreCorruptError
    from tokmo.records.trade_ledger_store import TradeLedgerStore

    # Write garbage to the DB file so it is not a valid SQLite database
    db_path = tmp_path / "stores" / "trade_ledger.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"not a sqlite database")

    ids = iter(f"event-id-{i:04d}" for i in range(100))
    store = TradeLedgerStore(
        db_path,
        clock=lambda: CLOCK_NOW,
        id_factory=lambda: next(ids),
    )
    with pytest.raises(StoreCorruptError):
        store.read_stored_window(
            source=SOURCE,
            account_scope=SCOPE,
            from_stored_at=CLOCK_NOW - timedelta(minutes=5),
            to_stored_at=CLOCK_NOW,
        )


def test_window_decoder_rejects_duplicate_source_record_keys(tmp_path):
    """Window reads preserve read_scope corruption parity for duplicate keys."""
    from tokmo.records.trade_ledger_privacy import StoreCorruptError
    from tokmo.records.trade_ledger_store import (
        _SELECT_WINDOW,
        _stored_rows_from_window_rows,
    )

    store = _make_store(tmp_path)
    store.append_batch((_ledger_event(1),), import_audit=_audit())
    db_path = tmp_path / "stores" / "trade_ledger.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            _SELECT_WINDOW,
            (
                SOURCE,
                SCOPE,
                CLOCK_NOW.isoformat(),
                (CLOCK_NOW + timedelta(minutes=1)).isoformat(),
            ),
        ).fetchone()
    finally:
        connection.close()

    assert row is not None
    with pytest.raises(StoreCorruptError):
        _stored_rows_from_window_rows(
            (row, row),
            source=SOURCE,
            account_scope=SCOPE,
        )


def test_consumer_ledger_empty_store_raises_not_empty(tmp_path):
    """A non-existent store file must raise StoreReadError, not return []."""
    from tokmo.programs.quotes_trades_consumer import (
        read_ledger_delta_for_consumer,
    )
    from tokmo.records.trade_ledger_privacy import StoreReadError

    store = _make_store(tmp_path)
    # Store does not exist yet — read must fail truthfully
    with pytest.raises(StoreReadError):
        read_ledger_delta_for_consumer(
            store=store,
            from_stored_at=CLOCK_NOW - timedelta(minutes=5),
            to_stored_at=CLOCK_NOW,
            source=SOURCE,
            account_scope=SCOPE,
        )


def test_active_production_constant_schedules_only_pulse_provider_operations():
    for op in (
        "PRICE_BATCH",
        "ORDERS_PAGE",
        "ORDER_DETAIL",
        "CALENDAR_DAY_KR",
        "CALENDAR_DAY_US",
        "ACCOUNTS",
        "ACCOUNT_HOLDINGS",
        "STOCK_CANDLES_DAILY",
    ):
        entry = OPERATION_LANE_REGISTRY_V1.entries[op]
        if op in ("PRICE_BATCH", "ORDERS_PAGE", "ORDER_DETAIL"):
            assert entry.recurring_owner is OperationLane.PULSE_60S
            assert entry.fallback_owner is OperationLane.CADENCE_300S
            assert entry.cutover_epoch_utc is not None
        else:
            assert entry.recurring_owner is OperationLane.CADENCE_300S
            assert entry.cutover_epoch_utc is None
            assert entry.fallback_owner is None
