"""Quotes/trades provider→consumer cutover seam (Plan §18/§8.3).

Before cutover: 300s remains the quotes/trades provider owner.
After cutover: Pulse is the provider, 300s becomes a consumer reading
LatestQuoteStore and TradeLedger with wire0.

This module provides read-only consumer functions for the 300s cadence
to use post-cutover. They never start provider wires — they only read
shared stores. The active production registry constant is unchanged
by this module; cutover happens only through a reviewed constant change.
"""

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tokmo.records.latest_quotes import LatestQuoteStore
    from tokmo.records.trade_ledger_store import TradeLedgerStore
    from tokmo.values.market_observations import TossCurrentPriceFact
    from tokmo.values.trade_ledger import TradeLedgerEvent


def read_latest_quotes_for_consumer(
    *,
    store: "LatestQuoteStore",
    source: str,
    symbols: tuple,
) -> tuple:
    """Read latest quotes from the shared store — wire zero.

    The 300s cadence calls this post-cutover instead of starting a
    PRICE_BATCH wire. The returned rows carry their original
    provider_timestamp and fetched_at so consumers never promote stale
    quotes as fresh. (LatestQuoteStore rows do not expose stored_at as
    a separate field — provider_timestamp/fetched_at are the freshness
    timestamps.)
    """
    return store.read_latest(source=source, symbols=symbols)


def read_ledger_delta_for_consumer(
    *,
    store: "TradeLedgerStore",
    from_stored_at: datetime,
    to_stored_at: datetime,
    source: str,
    account_scope: str,
) -> tuple:
    """Read TradeLedger rows in a half-open [from_stored_at, to_stored_at)
    window — wire zero.

    Post-cutover the 300s cadence reads execution deltas from the ledger
    rather than starting ORDERS_PAGE/ORDER_DETAIL wires. The half-open
    window [prev, cur) includes prev and excludes cur, preventing
    duplicate or missing rows across tick boundaries.

    Each returned ``LedgerStoredRow`` carries the immutable semantic
    ``TradeLedgerEvent`` (never altered) paired with its store-persisted
    ``stored_at`` timezone-aware UTC datetime — the timestamp the store
    assigned when the event was written, not a field on the event itself.

    ``source`` and ``account_scope`` are mandatory: this function does
    not fabricate a scope. If either is empty, ``ValueError`` is
    raised.

    Typed store exceptions (``StoreCorruptError``, ``StoreLockedError``,
    ``StoreReadError``) propagate to the caller — they are never masked
    as an empty result. A corrupt or locked store is a truthful failure,
    not "zero executions."
    """
    if not source or not account_scope:
        raise ValueError(
            "source and account_scope are mandatory for ledger delta read")

    return store.read_stored_window(
        source=source,
        account_scope=account_scope,
        from_stored_at=from_stored_at,
        to_stored_at=to_stored_at,
    )
