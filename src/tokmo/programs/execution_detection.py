"""Pulse incremental completed-execution ingestion (Plan §17).

Mask-free by contract: execution facts are collected on every tick whether
markets are open or closed — the 300s ``market_closed_pending`` defer was a
material-consistency device of the trades lane and never applies here.
Routing derives from ``AccountSourceProfileV1``: only live-order-capable
Toss scopes with a persisted native ref are scanned (pension Excel scopes
are NOT_APPLICABLE, never failures), ``status="CLOSED"`` completed orders
only, per-scope durable cursor/watermark, ORDERS_PAGE ≤2 and ORDER_DETAIL
≤1 per tick, ledger appends idempotent on the UNIQUE source key. No order
write path exists or can be expressed.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType

from tokmo.records.order_scan_state import (
    IncompleteRetryRow,
    IngestAuditRow,
    MarketPendingRow,
    ScanStateV1,
)
from tokmo.values.account_source_profile import (
    derive_account_source_profiles,
    orders_scan_status,
    toss_orders_target_scopes,
)
from tokmo.values.completed_execution import (
    ExecutionCompletenessViolation,
    is_completed_execution,
    to_completed_execution,
    to_trade_ledger_event,
)
from tokmo.values.current_holdings import CurrentHoldingsV1
from tokmo.values.pulse_policy import (
    PULSE_TRADES_CHILD_LIMITS_V1,
    PulseTickV1,
)
from tokmo.values.session_gate import Market
from tokmo.values.trade_event import (
    TradeEventTimesV1,
    trade_event_id,
)
from tokmo.values.trade_ledger import ImportAuditMeta

LEDGER_SOURCE = "toss-orders-v1"
IMPORTER_VERSION = "pulse-execution-v1"
SCAN_WINDOW_DAYS = 7
ORDERS_PAGE_LIMIT = 20

_ACCEPTED = frozenset(
    {
        "FILLED",
        "CANCELED",
        "REJECTED",
        "REPLACED",
        "PARTIAL_FILLED",
    }
)
_AMBIGUOUS = frozenset({"CANCEL_REJECTED", "REPLACE_REJECTED"})


@dataclass(frozen=True)
class PulseOrdersPageOutcomeV1:
    """Injected ORDERS_PAGE outcome (fixture or composition-parsed)."""

    code: str
    orders: tuple = ()
    next_cursor: "str | None" = None
    has_next: bool = False


@dataclass(frozen=True)
class NewTradeExecutionV1:
    """One newly INSERTED completed execution feeding the dialogue lane."""

    trade_event_id: str
    account_scope: str
    source_record_key: str
    symbol: str
    side: str
    currency: str
    filled_quantity: str
    average_filled_price: str
    filled_amount: str
    classified_market: str
    times: TradeEventTimesV1


@dataclass(frozen=True)
class PulseScanCompletenessV1:
    obligated_scopes: int
    scanned_scopes: int
    failed_scopes: int
    not_applicable_scopes: int


@dataclass(frozen=True)
class PulseExecutionStageResult:
    status: str
    reason: str
    tick_id: int
    scope_status: Mapping
    inserted_count: int
    duplicate_count: int
    conflict_count: int
    incomplete_count: int
    new_events: tuple
    completeness: "PulseScanCompletenessV1 | None"
    pagination_pending: bool
    wire_starts: int


def _order_market(currency) -> "Market | None":
    if currency == "KRW":
        return Market.KR
    if currency == "USD":
        return Market.US
    return None


def _map_failure(code: str) -> str:
    if code == "account_unmapped":
        return "account_unmapped"
    if code == "http_429":
        return "failed_provider_rate_limit"
    if code in ("auth_rejection", "forbidden"):
        return "failed_provider_auth"
    if code in ("contract_mismatch", "schema_mismatch", "parse_error"):
        return "failed_provider_contract"
    return "failed_provider_other"


def run_pulse_execution(
    *,
    tick: PulseTickV1,
    holdings_reader: Callable,
    native_refs_reader: Callable,
    token_provider: Callable,
    orders_page_fetch: Callable,
    order_detail_fetch: Callable,
    ledger_store,
    scan_state_store,
    clock: Callable[[], datetime],
    now: datetime,
    wire_recorder: Callable,
) -> PulseExecutionStageResult:
    if not isinstance(tick, PulseTickV1):
        raise ValueError("pulse execution stage requires a PulseTickV1")

    def _result(
        status,
        reason,
        scope_status=None,
        inserted=(),
        duplicates=0,
        conflicts=0,
        incomplete=0,
        completeness=None,
        pagination=False,
        wires=0,
    ):
        return PulseExecutionStageResult(
            status,
            reason,
            tick.tick_id,
            MappingProxyType(dict(scope_status or {})),
            len(inserted) if isinstance(inserted, (list, tuple)) else inserted,
            duplicates,
            conflicts,
            incomplete,
            tuple(inserted) if isinstance(inserted, (list, tuple)) else (),
            completeness,
            pagination,
            wires,
        )

    try:
        holdings = holdings_reader()
    except Exception:
        holdings = None
    if not isinstance(holdings, CurrentHoldingsV1):
        return _result("skipped", "skipped_holdings_absent")
    try:
        refs = native_refs_reader()
    except Exception:
        refs = None
    ref_scopes = frozenset() if refs is None else frozenset(refs.scopes())
    profiles = derive_account_source_profiles(holdings, native_ref_scopes=ref_scopes)
    scope_status = {}
    for profile in profiles:
        scope_status[profile.account_scope] = orders_scan_status(profile)
    target_scopes = list(toss_orders_target_scopes(profiles))
    obligated = [
        profile.account_scope
        for profile in profiles
        if scope_status[profile.account_scope] != "NOT_APPLICABLE"
    ]
    for scope in obligated:
        if scope_status[scope] == "ACCOUNT_UNMAPPED":
            scope_status[scope] = "account_unmapped"

    # Fair rotation across ticks, same device as the 300s lane.
    if target_scopes:
        rotation = tick.tick_id % len(target_scopes)
        target_scopes = target_scopes[rotation:] + target_scopes[:rotation]

    timestamp = clock().isoformat().replace("+00:00", "Z")
    page_budget = PULSE_TRADES_CHILD_LIMITS_V1.orders_page_starts
    detail_budget = PULSE_TRADES_CHILD_LIMITS_V1.order_detail_starts
    pages_used = 0
    details_used = 0
    token = None
    wire_starts = 0
    inserted_events = []
    duplicate_count = 0
    conflict_count = 0
    incomplete_count = 0
    pagination_pending = False

    def _token():
        # Lazy per-tick token; the injected provider owns wire recording so
        # both Pulse stages share a single TOKEN start per tick.
        nonlocal token
        if token is None:
            token = token_provider()
        return token

    def _classify_and_convert(scope, order, *, allow_detail):
        """Returns (kind, payload). kind: event|duplicate-skip|quarantine|
        pending|incomplete|skip."""
        nonlocal details_used, wire_starts, incomplete_count
        if order.status in _AMBIGUOUS:
            return "quarantine", None
        if order.status not in _ACCEPTED:
            return "skip", None
        if not is_completed_execution(True, order):
            return "skip", None
        market = _order_market(order.currency)
        if market is None:
            prior = {
                row.order_id for row in scan_state_store.read_market_pending(scope)
            }
            scan_state_store.add_market_pending(
                MarketPendingRow(
                    scope,
                    order.order_id,
                    order.symbol,
                    order.currency,
                    order.status,
                    "market_resolution_pending",
                    tick.tick_id,
                    timestamp,
                )
            )
            del prior
            return "pending", None
        fact = to_completed_execution(
            order,
            account_scope=scope,
            classified_market=market,
            page_provenance_closed=True,
        )
        if isinstance(fact, ExecutionCompletenessViolation):
            if allow_detail and details_used < detail_budget:
                wire_recorder("ORDER_DETAIL", f"detail:{order.order_id}")
                wire_starts += 1
                details_used += 1
                _token()
                detailed = order_detail_fetch(
                    account_scope=scope,
                    native_ref=refs.reveal(scope),
                    order_id=order.order_id,
                )
                if detailed is not None:
                    return _classify_and_convert(scope, detailed, allow_detail=False)
            scan_state_store.add_incomplete_retry(
                IncompleteRetryRow(
                    scope,
                    order.order_id,
                    market,
                    tick.tick_id,
                    1,
                    tick.tick_id,
                    0,
                    now.date().isoformat(),
                    None,
                )
            )
            incomplete_count += 1
            return "incomplete", None
        scan_state_store.resolve_market_pending(scope, order.order_id)
        return "event", (to_trade_ledger_event(fact), fact)

    def _append_events(scope, pairs):
        """Idempotent ledger append; classify INSERTED/DUPLICATE/CONFLICT."""
        nonlocal duplicate_count, conflict_count
        events = tuple(pair[0] for pair in pairs)
        if not events:
            return True
        before = {
            event.source_record_key: scan_state_store.read_ingest_audit(
                (LEDGER_SOURCE, scope, event.source_record_key)
            )
            is not None
            for event in events
        }
        receipt = ledger_store.append_batch(
            events, import_audit=ImportAuditMeta(None, IMPORTER_VERSION)
        )
        conflict_keys = set()
        if receipt.status != "SAVED":
            if receipt.reason != "batch_identity_conflict":
                scope_status[scope] = "failed_store"
                return False
            # Bisect to isolate the conflicting keys, quarantine them.
            remaining = list(events)
            saved_any = False
            for event in remaining:
                single = ledger_store.append_batch(
                    (event,), import_audit=ImportAuditMeta(None, IMPORTER_VERSION)
                )
                if single.status == "SAVED":
                    saved_any = True
                else:
                    conflict_keys.add(event.source_record_key)
            del saved_any
            if conflict_keys:
                scan_state_store.add_quarantine_keys(
                    scope, tuple(sorted(conflict_keys)), "batch_identity_conflict"
                )
                conflict_count += len(conflict_keys)
        for event, fact in pairs:
            key = event.source_record_key
            if key in conflict_keys:
                continue
            scan_state_store.write_ingest_audit(
                IngestAuditRow(
                    LEDGER_SOURCE,
                    scope,
                    key,
                    fact.filled_at.astimezone().isoformat(),
                    tick.tick_id,
                    timestamp,
                )
            )
            if before[key]:
                duplicate_count += 1
                continue
            stored_now = clock()
            event_id = trade_event_id(LEDGER_SOURCE, scope, key)
            inserted_events.append(
                NewTradeExecutionV1(
                    event_id,
                    scope,
                    key,
                    fact.symbol,
                    fact.side,
                    fact.currency,
                    fact.filled_quantity,
                    fact.average_filled_price,
                    fact.filled_amount,
                    fact.classified_market.value,
                    TradeEventTimesV1(
                        filled_at=fact.filled_at,
                        provider_observed_at=None,
                        discovered_at=now,
                        stored_at=stored_now,
                        notified_at=None,
                    ),
                )
            )
        return True

    for scope in target_scopes:
        if pages_used >= page_budget:
            pagination_pending = True
            scope_status[scope] = "deferred_page_budget"
            continue
        state = scan_state_store.read_state(scope)
        cursor = None if state is None else state.pending_cursor
        watermark = None if state is None else state.watermark_date
        window_from = (
            state.pending_window_from
            if state is not None
            and state.pending_cursor is not None
            and state.pending_window_from is not None
            else (
                watermark or (now.date() - timedelta(days=SCAN_WINDOW_DAYS)).isoformat()
            )
        )
        window_to = (
            state.pending_window_to
            if state is not None
            and state.pending_cursor is not None
            and state.pending_window_to is not None
            else now.date().isoformat()
        )
        scope_failed = False
        scope_orders = []
        scope_pagination = False
        while pages_used < page_budget:
            _token()
            wire_recorder("ORDERS_PAGE", f"orders:{pages_used}")
            wire_starts += 1
            pages_used += 1
            try:
                outcome = orders_page_fetch(
                    account_scope=scope,
                    native_ref=refs.reveal(scope),
                    status="CLOSED",
                    from_date=window_from,
                    to_date=window_to,
                    cursor=cursor,
                    limit=ORDERS_PAGE_LIMIT,
                )
            except Exception:
                scope_status[scope] = "failed_provider_other"
                scope_failed = True
                break
            code = getattr(outcome, "code", None)
            if code != "success":
                scope_status[scope] = _map_failure(code)
                scope_failed = True
                break
            scope_orders.extend(outcome.orders)
            if outcome.has_next and outcome.next_cursor is not None:
                cursor = outcome.next_cursor
                scope_pagination = True
                continue
            cursor = None
            scope_pagination = False
            break
        if scope_failed:
            continue
        pairs = []
        for order in scope_orders:
            kind, payload = _classify_and_convert(scope, order, allow_detail=True)
            if kind == "event":
                pairs.append(payload)
            elif kind == "quarantine":
                scan_state_store.add_quarantine_keys(
                    scope, (order.order_id,), "ambiguous_status"
                )
        if not _append_events(scope, pairs):
            continue
        if scope_pagination and cursor is not None:
            pagination_pending = True
            scan_state_store.save_state(
                ScanStateV1(
                    scope,
                    watermark,
                    cursor,
                    window_from,
                    window_to,
                    None,
                    False,
                    None,
                    False,
                    timestamp,
                )
            )
        else:
            scan_state_store.save_state(
                ScanStateV1(
                    scope,
                    window_to,
                    None,
                    None,
                    None,
                    None,
                    False,
                    None,
                    False,
                    timestamp,
                )
            )
        if scope_status.get(scope) == "SCAN_REQUIRED":
            scope_status[scope] = "scanned"

    scanned = sum(1 for scope in obligated if scope_status.get(scope) == "scanned")
    failed = sum(
        1
        for scope in obligated
        if scope_status.get(scope) not in ("scanned", "deferred_page_budget")
    )
    completeness = PulseScanCompletenessV1(
        obligated_scopes=len(obligated),
        scanned_scopes=scanned,
        failed_scopes=failed,
        not_applicable_scopes=sum(
            1 for value in scope_status.values() if value == "NOT_APPLICABLE"
        ),
    )
    if failed:
        status, reason = "partial", "partial_accounts"
    elif pagination_pending:
        status, reason = "saved", "pagination_pending"
    else:
        status, reason = "saved", "saved"
    return _result(
        status,
        reason,
        scope_status,
        inserted_events,
        duplicate_count,
        conflict_count,
        incomplete_count,
        completeness,
        pagination_pending,
        wire_starts,
    )
