"""Ingest legacy pension trade records into the unified trade ledger.

Maps ``PensionTradeRecordV1`` (parsed from the legacy pension MD by
``pension_legacy_migration``) onto the shared ``TradeLedgerEvent`` shape under
the already-registered ``retirement_excel_v1`` source contract. Pension
executions then live in the same ``trade_ledger`` store as Toss personal
executions, isolated by ``(source, account_scope)``:

    personal : source="toss-orders-v1"      account_scope="toss-acct-<digest>"
    pension  : source="retirement_excel_v1" account_scope="retirement_pension_1"

The contract is full-history sequential, so every ingest submits the complete
ordered history with keys ``row-000001..row-00000N``. Toss-only fields
(``settlement_date``, ``fee``, ``tax``) are left ``None`` — never fabricated;
``balance_quantity`` (a running balance derivable from the ledger) has no
ledger column and is intentionally not stored.
"""

from __future__ import annotations

from collections.abc import Sequence

from tokmo.programs.pension_legacy_migration import PensionTradeRecordV1
from tokmo.records.trade_ledger_canonical import canonical_decimal_string
from tokmo.values.trade_ledger import ImportAuditMeta, TradeLedgerEvent

# Matches the registered SourceContract in trade_ledger_preflight.SOURCE_CONTRACTS.
PENSION_TRADE_SOURCE = "retirement_excel_v1"
PENSION_TRADE_SCOPE = "retirement_pension_1"
PENSION_TRADE_IMPORTER_VERSION = "pension-legacy-md-v1"
PENSION_TRADE_CURRENCY = "KRW"


def _event_kind(transaction_type_raw: str) -> str:
    """A 정정(correction) row is a CORRECTION event; everything else EXECUTION."""
    return "CORRECTION" if "정정" in transaction_type_raw else "EXECUTION"


def build_pension_trade_events(
    trades: Sequence[PensionTradeRecordV1],
) -> tuple[TradeLedgerEvent, ...]:
    """Map ordered pension trade records to sequential ledger events.

    Row keys are ``row-000001..row-00000N`` in the given order, satisfying the
    ``retirement_excel_v1`` full-history sequential contract. The caller must
    pass the complete, in-order trade history.
    """
    events: list[TradeLedgerEvent] = []
    for index, trade in enumerate(trades, start=1):
        events.append(
            TradeLedgerEvent(
                source=PENSION_TRADE_SOURCE,
                account_scope=PENSION_TRADE_SCOPE,
                source_record_key=f"row-{index:06d}",
                event_kind=_event_kind(trade.transaction_type_raw),
                supersedes_source_record_key=None,
                trade_date=trade.trade_date.date(),
                transaction_type_raw=trade.transaction_type_raw,
                instrument_raw=trade.product_name,
                quantity=canonical_decimal_string(str(trade.trade_quantity)),
                unit_price=canonical_decimal_string(str(trade.unit_price)),
                amount=canonical_decimal_string(str(trade.amount)),
                medium_raw=trade.trade_medium,
                currency_raw=PENSION_TRADE_CURRENCY,
                settlement_date=None,
                fee=None,
                tax=None,
            )
        )
    return tuple(events)


def ingest_pension_trades(trades, store, *, file_sha256):
    """Build events from ``trades`` and append them to the ledger ``store``.

    Returns the store's ``TradeBatchSaveReceipt``. Re-running with the same
    history is idempotent: previously stored rows classify as duplicates under
    the store's ``(source, account_scope, source_record_key)`` uniqueness.
    """
    events = build_pension_trade_events(trades)
    return store.append_batch(
        events,
        import_audit=ImportAuditMeta(file_sha256, PENSION_TRADE_IMPORTER_VERSION),
    )
