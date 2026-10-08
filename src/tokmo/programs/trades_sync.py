"""Completed-execution ingestion orchestration with injected read-only seams."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
import fcntl
import hashlib
import os
import sqlite3
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from tokmo.values.completed_execution import (
    CompletedExecutionFact, ExecutionCompletenessViolation,
    is_completed_execution, to_completed_execution, to_trade_ledger_event,
)
from tokmo.values.current_holdings import CurrentHoldingsV1, classify_market
from tokmo.values.material_pack import (
    JobKind, build_material_pack_v1, canonical_pack_file_bytes,
)
from tokmo.values.receipt_v1 import (
    AttemptStatus, AttemptV1, CompletenessV1, LastSuccessV1,
    MarketFreshness, ReceiptV1,
)
from tokmo.values.session_gate import Market
from tokmo.values.ops_kernel import (
    OpsKernelBudgetExceeded, OpsKernelConflictError, OpsKernelCooldownActive,
    ProducerRole,
)
from tokmo.values.open_market_mask import require_tick_mask_binding
from tokmo.values.request_parity import (
    REQUEST_OPERATION_SPECS, OrdersPageCheckpointV1, canonical_params_digest,
    decode_orders_page_checkpoint_v1,
    encode_orders_page_checkpoint_v1, orders_page_digest_v1,
)
from tokmo.values.trade_ledger import ImportAuditMeta, TradeBatchSaveReceipt, TradeLedgerEvent


class TradesStoreSeam(Protocol):
    def append_batch(
        self, events: tuple[TradeLedgerEvent, ...], *,
        import_audit: ImportAuditMeta,
    ) -> TradeBatchSaveReceipt: ...


class RegularSessionDates(Protocol):
    def regular_session_dates(
        self, market: Market, anchor_date: date, count: int,
    ) -> tuple[date, ...]: ...


@dataclass(frozen=True)
class TradesSyncResult:
    status: str
    reason: str
    tick_id: int
    pack_path: str | None
    pack_sha256: str | None
    captured_at: datetime | None
    completeness: CompletenessV1 | None


@dataclass(frozen=True)
class _State:
    account_scope: str
    watermark_date: str | None
    pending_cursor: str | None
    pending_window_from: str | None
    pending_window_to: str | None
    backfill_cursor: str | None
    backfill_active: bool
    last_backfill_date: str | None
    watermark_frozen: bool
    updated_at: str


@dataclass(frozen=True)
class _Retry:
    account_scope: str
    source_record_key: str
    classified_market: Market
    first_seen_tick_id: int
    attempts: int
    last_attempt_tick_id: int
    session_age: int
    reference_date: str
    terminal_state: str | None


@dataclass(frozen=True)
class _Audit:
    source: str
    account_scope: str
    source_record_key: str
    filled_at_utc: str
    tick_id: int
    stored_at: str


@dataclass(frozen=True)
class _Pending:
    account_scope: str
    order_id: str
    symbol: str
    currency: str | None
    status: str
    reason: str
    first_seen_tick_id: int
    updated_at: str


_ACCEPTED = frozenset({
    "FILLED", "CANCELED", "REJECTED", "REPLACED", "PARTIAL_FILLED",
})
_AMBIGUOUS = frozenset({"CANCEL_REJECTED", "REPLACE_REJECTED"})
_AUTH = frozenset({"auth_rejection", "forbidden"})
_CONTRACT = frozenset({"contract_mismatch", "schema_mismatch"})


def provider_call_plan(*, detail_ready: int) -> tuple[int, int]:
    if type(detail_ready) is not int or detail_ready < 0:
        raise ValueError("invalid detail_ready")
    return min(detail_ready, 2), 10


def bisect_identity_conflicts(events, append):
    if not events:
        raise RuntimeError("empty_conflict_batch")
    receipt = append(tuple(events))
    if receipt.status == "SAVED":
        return ()
    if receipt.reason != "batch_identity_conflict":
        raise RuntimeError(receipt.reason)
    if len(events) == 1:
        return (events[0],)
    split = len(events) // 2
    return (
        bisect_identity_conflicts(events[:split], append)
        + bisect_identity_conflicts(events[split:], append)
    )


def _lock(path):
    fd = None
    try:
        target = Path(path)
        if target.is_symlink() or target.parent.is_symlink():
            return -1
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(target, flags, 0o600)
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        if fd is not None:
            os.close(fd)
        return None
    except (OSError, TypeError, ValueError):
        if fd is not None:
            os.close(fd)
        return -1


def _market_states(session_result, mask):
    """Market open flags: mask when bound (never reinterpreted), else verdicts."""
    if mask is not None:
        return {market.value: market in mask.markets for market in mask.entries}
    return {
        verdict.market.value: verdict.open for verdict in session_result.verdicts
    }


def _market_rows(states, prior=None, tick_id=None, captured=None):
    previous = {} if prior is None else prior
    return {
        market: MarketFreshness(
            market, "open" if is_open else "closed",
            tick_id if tick_id is not None else (
                None if previous.get(market) is None
                else previous[market].last_success_tick_id
            ),
            captured if tick_id is not None else (
                None if previous.get(market) is None
                else previous[market].captured_at
            ),
        )
        for market, is_open in states.items()
    }


def _attempt(receipt_store, states, tick_id, status, reason, complete, clock,
             mask_digest=None):
    previous = receipt_store.read()
    receipt_store.replace(ReceiptV1(
        1, JobKind.TRADES, AttemptV1(tick_id, status, reason, clock()),
        None if previous is None else previous.last_success,
        _market_rows(
            states, None if previous is None else previous.markets,
        ),
        complete, mask_digest=mask_digest,
    ))


def _provider_reason(code):
    if code in _AUTH:
        return "failed_provider_auth"
    if code == "http_429":
        return "failed_provider_rate_limit"
    if code in _CONTRACT:
        return "failed_provider_contract"
    if code == "ceiling_exceeded":
        return "failed_ceiling"
    return "failed_provider_other"


def _request_exit(code):
    if code in _AUTH:
        return "AUTH_FAIL"
    if code == "http_429":
        return "RATE_LIMITED"
    if code in _CONTRACT:
        return "PARSE_FAIL"
    if code == "redirect":
        return "REDIRECT"
    if code in {"timeout", "dns_failure", "tls_failure", "connection_failure",
                "network_error", "transport_error"}:
        return "TRANSPORT_FAIL"
    return "PROVIDER_ERROR"


def _store_transient(error):
    return (
        error.__class__.__name__ == "ScanStateLockedError"
        or (isinstance(error, sqlite3.Error)
            and error.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))
    )


def _calendar(calendar, market, anchor, count):
    try:
        result = calendar.regular_session_dates(market, anchor, count)
    except (TypeError, ValueError):
        raise ValueError("failed_calendar")
    if type(result) is not tuple or len(result) < count or any(
            type(item) is not date for item in result):
        raise ValueError("failed_calendar")
    return result


# The official Toss stock-order contract uses the top-level currency as the
# exact market key. No normalization, symbol lookup, or holdings evidence is
# permitted: malformed/unsupported values fail closed durably.
_MARKET_CURRENCY = MappingProxyType({Market.KR: "KRW", Market.US: "USD"})
_CURRENCY_MARKET = MappingProxyType({"KRW": Market.KR, "USD": Market.US})


def _currency_compatible(market, currency):
    return type(currency) is str and currency == _MARKET_CURRENCY[market]


def _order_market(currency):
    return _CURRENCY_MARKET.get(currency) if type(currency) is str else None


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _inserted_row(event, fact):
    return {
        "source_record_key": event.source_record_key,
        "trade_date": event.trade_date.isoformat(),
        "transaction_type_raw": event.transaction_type_raw,
        "instrument_raw": event.instrument_raw,
        "quantity": event.quantity, "unit_price": event.unit_price,
        "amount": event.amount, "medium_raw": event.medium_raw,
        "currency_raw": event.currency_raw,
        "classified_market": fact.classified_market.value,
        "filled_at": _iso(fact.filled_at),
    }


def _window(state, market, today, calendar):
    if state is not None and state.pending_window_from is not None:
        return state.pending_window_from, state.pending_window_to
    anchor = (
        today if state is None or state.watermark_date is None
        else date.fromisoformat(state.watermark_date)
    )
    sessions = _calendar(
        calendar, market, anchor,
        20 if state is None or state.watermark_date is None else 3,
    )
    return min(sessions).isoformat(), today.isoformat()


def _backfill_due(state, market, today, calendar):
    if state is None or state.watermark_date is None:
        return False
    if state.backfill_active:
        return True
    sessions = _calendar(calendar, market, today, 20)
    latest = max(session for session in sessions if session <= today)
    if state.last_backfill_date is None:
        return True
    previous = date.fromisoformat(state.last_backfill_date)
    return latest.isocalendar()[:2] != previous.isocalendar()[:2]


def _regular_due(state, today):
    if state is None or state.watermark_date is None:
        return True
    if state.pending_window_from is not None or state.pending_cursor is not None:
        return True
    return date.fromisoformat(state.watermark_date) < today.fromordinal(
        today.toordinal() - 1
    )


def _process_order(order, scope, scan_state, tick_id,
                   timestamp, incident_writer, authoritative_market=None,
                   mask=None):
    quarantined = set(scan_state.read_quarantine(scope))
    if order.order_id in quarantined:
        return "quarantine", None
    if order.status in _AMBIGUOUS:
        first = order.order_id not in quarantined
        scan_state.add_quarantine_keys(scope, (order.order_id,), "ambiguous_duplicate_risk")
        if first:
            incident_writer("execution-quarantine", frozenset({scope}))
        return "quarantine", None
    if order.status not in _ACCEPTED:
        first = order.order_id not in quarantined
        scan_state.add_quarantine_keys(scope, (order.order_id,), "unknown_order_status")
        if first:
            incident_writer("execution-quarantine", frozenset({scope}))
        return "quarantine", None
    if not is_completed_execution(True, order):
        return "skip", None
    if authoritative_market is not None:
        market = authoritative_market
        if mask is not None and not _currency_compatible(market, order.currency):
            # The detail contradicts the durable classification: reconverge.
            market = None
    else:
        market = _order_market(order.currency)
    if market is None:
        prior = {row.order_id for row in scan_state.read_market_pending(scope)}
        scan_state.add_market_pending(_Pending(
            scope, order.order_id, order.symbol, order.currency, order.status,
            "market_resolution_pending", tick_id, timestamp,
        ))
        if order.order_id not in prior:
            incident_writer("execution-market-pending", frozenset({scope}))
        return "incomplete", None
    if mask is not None and market not in mask.markets:
        # Classified but its market is closed this tick: durably withheld,
        # never detail-fetched, never in the closed market's ledger/material.
        prior = {row.order_id for row in scan_state.read_market_pending(scope)}
        scan_state.add_market_pending(_Pending(
            scope, order.order_id, order.symbol, order.currency, order.status,
            "market_closed_pending", tick_id, timestamp,
        ))
        if order.order_id not in prior:
            incident_writer("execution-market-closed", frozenset({scope}))
        return "incomplete", None
    fact = to_completed_execution(
        order, account_scope=scope, classified_market=market,
        page_provenance_closed=True,
    )
    if isinstance(fact, ExecutionCompletenessViolation):
        return "needs_detail", (order, market)
    scan_state.resolve_market_pending(scope, order.order_id)
    return "event", (to_trade_ledger_event(fact), fact)


def _save_batch(store, scan_state, scope, pairs, tick_id, timestamp,
                incident_writer):
    events = tuple(pair[0] for pair in pairs)
    before = {
        event.source_record_key: scan_state.read_ingest_audit(
            ("toss-orders-v1", scope, event.source_record_key)
        ) is not None for event in events
    }
    append = lambda batch: store.append_batch(
        tuple(batch), import_audit=ImportAuditMeta(None, "toss-orders-sync-v1"),
    )
    try:
        receipt = append(events)
    except Exception as error:
        transient = _store_transient(error)
        return ("transient" if transient else "deterministic"), "ledger_append", (), (), ()
    conflicts = ()
    if receipt.status != "SAVED":
        if receipt.reason != "batch_identity_conflict":
            status = "transient" if receipt.reason == "store_locked" else "deterministic"
            return status, "ledger_append", (), (), ()
        try:
            conflicts = bisect_identity_conflicts(events, append)
        except RuntimeError:
            return "deterministic", "ledger_append", (), (), ()
        keys = tuple(event.source_record_key for event in conflicts)
        scan_state.add_quarantine_keys(scope, keys, "batch_identity_conflict")
        incident_writer("execution-identity-conflict", frozenset({scope}))
    conflict_keys = {event.source_record_key for event in conflicts}
    successful = [pair for pair in pairs if pair[0].source_record_key not in conflict_keys]
    inserted = []
    duplicates = []
    for event, fact in successful:
        try:
            scan_state.write_ingest_audit(_Audit(
                "toss-orders-v1", scope, event.source_record_key,
                _iso(fact.filled_at), tick_id, timestamp,
            ))
        except Exception as error:
            transient = _store_transient(error)
            return ("transient" if transient else "deterministic"), "ingest_audit", (), (), ()
        if before[event.source_record_key] or (
                receipt.inserted_count == 0 and receipt.duplicate_count > 0):
            duplicates.append(event.source_record_key)
        else:
            inserted.append(_inserted_row(event, fact))
    return "ok", None, tuple(inserted), tuple(duplicates), tuple(conflict_keys)


def _run(*, tick, session_result, holdings, orders_provider, detail_provider,
         store, scan_state, pack_writer, receipt_store, reservation_seal,
         incident_writer, regular_session_dates, clock, now,
         ops_kernel=None, invocation_id=None, mask=None):
    complete0 = CompletenessV1(0, 0, False, 0, 0)
    states = _market_states(session_result, mask)
    mask_digest = None if mask is None else mask.digest

    def _anchor(market):
        """Session-date anchor: the classified market's own local date."""
        return now.date() if mask is None else mask.entries[market].local_date

    if mask is not None and not mask.markets:
        _attempt(
            receipt_store, states, tick.tick_id, AttemptStatus.SKIPPED,
            "skipped_market_closed", complete0, clock, mask_digest=mask_digest,
        )
        return TradesSyncResult(
            "skipped", "skipped_market_closed", tick.tick_id,
            None, None, None, complete0,
        )
    if holdings is None:
        _attempt(
            receipt_store, states, tick.tick_id, AttemptStatus.SKIPPED,
            "skipped_holdings_absent", complete0, clock, mask_digest=mask_digest,
        )
        return TradesSyncResult(
            "skipped", "skipped_holdings_absent", tick.tick_id,
            None, None, None, complete0,
        )
    try:
        reservation_seal("trades", str(tick.tick_id), _iso(now), "reserved")
    except Exception:
        _attempt(
            receipt_store, states, tick.tick_id, AttemptStatus.FAILED,
            "failed_store", complete0, clock, mask_digest=mask_digest,
        )
        return TradesSyncResult(
            "failed", "failed_store", tick.tick_id, None, None, None, complete0,
        )
    # A pack has no visibility without its receipt.  Remove a same-tick orphan
    # before replay so ledger DUPLICATE classification can produce new final
    # bytes without colliding with the abandoned write-once file.
    try:
        prior_receipt = receipt_store.read()
        listed = pack_writer.list_packs(JobKind.TRADES)
        keep = {
            item[0] for item in listed if item[0] != tick.tick_id
        }
        if prior_receipt is not None and prior_receipt.last_success is not None:
            keep.add(prior_receipt.last_success.tick_id)
        pack_writer.reap_orphans(JobKind.TRADES, frozenset(keep), frozenset())
    except Exception:
        _attempt(
            receipt_store, states, tick.tick_id, AttemptStatus.FAILED,
            "failed_pack", complete0, clock, mask_digest=mask_digest,
        )
        return TradesSyncResult(
            "failed", "failed_pack", tick.tick_id, None, None, None, complete0,
        )

    timestamp = _iso(clock())
    scan_scopes = [account.account_scope for account in holdings.accounts]
    if scan_scopes:
        rotation = tick.tick_id % len(scan_scopes)
        scan_scopes = scan_scopes[rotation:] + scan_scopes[:rotation]
    retries = scan_state.read_incomplete_retry()
    retry_scopes = sorted({
        row.account_scope for row in retries
        if row.account_scope not in scan_scopes
    })
    scopes = scan_scopes + retry_scopes
    account_data = {
        scope: {"inserted": [], "duplicates": [], "quarantine": [], "incomplete": []}
        for scope in scopes
    }
    failures = set()
    pagination = False
    detail_calls = 0
    detail_budget, orders_budget = provider_call_plan(detail_ready=2)

    # Durable retries have first claim on the fixed DETAIL partition.
    retry_rotation = sorted(retries, key=lambda row: (
        (scopes.index(row.account_scope) if row.account_scope in scopes else len(scopes)),
        row.source_record_key,
    ))
    retry_pairs = {scope: [] for scope in scopes}
    retry_ready = []
    for row in retry_rotation:
        if mask is not None and row.classified_market not in mask.markets:
            # Closed-market retry rows keep their durable state untouched;
            # no session-age advance, no detail start (SDS §6.2, G-8).
            continue
        anchor = _anchor(row.classified_market)
        try:
            sessions = _calendar(
                regular_session_dates, row.classified_market, anchor, 20,
            )
        except ValueError:
            _attempt(
                receipt_store, states, tick.tick_id, AttemptStatus.FAILED,
                "failed_calendar", complete0, clock, mask_digest=mask_digest,
            )
            return TradesSyncResult(
                "failed", "failed_calendar", tick.tick_id, None, None, None, complete0,
            )
        reference = date.fromisoformat(row.reference_date)
        age = min(20, len({
            session for session in sessions
            if reference < session <= anchor
        }))
        if age <= row.session_age:
            continue
        retry_ready.append((row, age))
    for row, age in retry_ready[:detail_budget]:
        try:
            outcome = detail_provider(
                account_scope=row.account_scope, order_id=row.source_record_key,
            )
        except (OpsKernelBudgetExceeded, OpsKernelCooldownActive,
                OpsKernelConflictError):
            raise
        detail_calls += 1
        if outcome is None or outcome.code != "success":
            scan_state.add_incomplete_retry(_Retry(
                row.account_scope, row.source_record_key, row.classified_market,
                row.first_seen_tick_id, row.attempts + 1, tick.tick_id, age,
                row.reference_date,
                "INCOMPLETE_EXPIRED" if age >= 20 else None,
            ))
            continue
        disposition, value = _process_order(
            outcome.value, row.account_scope, scan_state, tick.tick_id,
            timestamp, incident_writer,
            row.classified_market, mask=mask,
        )
        if disposition == "event":
            retry_pairs[row.account_scope].append(value)
        else:
            scan_state.add_incomplete_retry(_Retry(
                row.account_scope, row.source_record_key, row.classified_market,
                row.first_seen_tick_id, row.attempts + 1, tick.tick_id, age,
                row.reference_date,
                "INCOMPLETE_EXPIRED" if age >= 20 else None,
            ))

    scan_states = {}
    pages = {scope: 0 for scope in scopes}
    calls = 0
    active = list(scan_scopes)
    while active and calls < orders_budget:
        scope = active.pop(0)
        state = scan_state.read_state(scope)
        scan_states[scope] = state
        account = next(item for item in holdings.accounts if item.account_scope == scope)
        markets = {
            value for item in account.holdings
            if (value := classify_market(item.market_raw, item.market_field)) is not None
        }
        # Page scanning is account-scoped (SDS §7.2); only per-record
        # classification needs holdings evidence. No single-market shortcut
        # and no KR default (G-8): every scan market anchors its own
        # local-date/session window and the account page window is their
        # union. Masked ticks anchor on the exact open-market mask; legacy
        # ticks use the account's classified markets, or both markets when
        # nothing is classified.
        if mask is not None:
            scan_markets = sorted(mask.markets, key=lambda item: item.value)
        else:
            scan_markets = sorted(
                markets or set(Market), key=lambda item: item.value,
            )
        today_scope = max(_anchor(value) for value in scan_markets)
        try:
            backfill = not _regular_due(state, today_scope) and any(
                _backfill_due(state, value, _anchor(value), regular_session_dates)
                for value in scan_markets
            )
            if backfill:
                window_from = min(
                    min(_calendar(
                        regular_session_dates, value, _anchor(value), 20,
                    )).isoformat()
                    for value in scan_markets
                )
                window_to = today_scope.isoformat()
            else:
                pairs_by_market = [
                    _window(state, value, _anchor(value), regular_session_dates)
                    for value in scan_markets
                ]
                window_from = min(pair[0] for pair in pairs_by_market)
                window_to = max(pair[1] for pair in pairs_by_market)
        except ValueError:
            _attempt(
                receipt_store, states, tick.tick_id, AttemptStatus.FAILED,
                "failed_calendar", complete0, clock, mask_digest=mask_digest,
            )
            return TradesSyncResult(
                "failed", "failed_calendar", tick.tick_id, None, None, None, complete0,
            )
        cursor = (
            state.backfill_cursor if backfill
            else (None if state is None else state.pending_cursor)
        )
        params = {
            "account_scope": scope, "window_from": window_from,
            "window_to": window_to, "request_cursor": cursor,
        }
        digest = canonical_params_digest("ORDERS_PAGE", params)
        ordinal = calls
        parity = ops_kernel is not None
        planned_rows = () if not parity else ops_kernel.read_planned_requests(
            tick.tick_id, "TRADES")
        planned = next((row for row in planned_rows
                        if row["operation"] == "ORDERS_PAGE"
                        and ((cursor is None and row["ordinal"] == calls)
                             or (cursor is not None and row["params_digest"] == digest))), None)
        if parity and planned is None:
            # Plan this page when no caller pre-planned it.  A first page
            # (cursor is None) is self-planned at the next free ordinal exactly
            # like a continuation page, so a composition that does not pre-plan
            # (the integrated cadence) still starts, wires, and settles the page
            # under the kernel; a caller that does pre-plan (Slice F fixtures) is
            # matched above and skips this branch.
            used = {row["ordinal"] for row in planned_rows}
            available = next((value for value in range(10) if value not in used), None)
            if available is None:
                incident_writer("orders-page-ceiling", frozenset({scope}))
                raise OpsKernelBudgetExceeded("orders page ceiling exceeded")
            try:
                ops_kernel.plan_requests(ProducerRole.COLLECTOR, tick.tick_id, "TRADES", ({
                    "ordinal": available, "operation": "ORDERS_PAGE",
                    "params_digest": digest, "method": REQUEST_OPERATION_SPECS["ORDERS_PAGE"][0],
                    "host": "openapi" + ".tossinvest.com",
                    "normalized_path_template": REQUEST_OPERATION_SPECS["ORDERS_PAGE"][1],
                    "account_ordinal": None, "market": None,
                    "local_trade_date": None, "canonical_params": params,
                },), invocation_id=invocation_id, now=clock())
            except OpsKernelBudgetExceeded:
                incident_writer("orders-page-ceiling", frozenset({scope}))
                raise
            except OpsKernelConflictError:
                incident_writer("orders-page-binding-conflict", frozenset({scope}))
                raise
            planned = next(row for row in ops_kernel.read_planned_requests(
                tick.tick_id, "TRADES") if row["ordinal"] == available)
        if planned is not None:
            ordinal = planned["ordinal"]
        calls += 1
        pages[scope] += 1
        if parity and (invocation_id is None or planned is None
                       or planned["operation"] != "ORDERS_PAGE"):
            incident_writer("orders-page-binding-conflict", frozenset({scope}))
            failures.add(scope)
            continue
        if parity and planned["state"] in ("CHECKPOINTED", "APPLYING"):
            applied = ops_kernel.begin_page_apply(
                ProducerRole.COLLECTOR, tick.tick_id, ordinal,
                invocation_id=invocation_id, now=clock())
            raw = applied["orders_page"]
            scope = raw["account_scope"]
            window_from, window_to = raw["window_from"], raw["window_to"]
            cursor = raw["request_cursor"]
            try:
                page = decode_orders_page_checkpoint_v1(
                    raw["orders_json"].encode(), schema_version=raw["schema_version"],
                    tick_id=raw["tick_id"], request_ordinal=raw["request_ordinal"],
                    account_scope=raw["account_scope"], window_from=raw["window_from"],
                    window_to=raw["window_to"], request_cursor=raw["request_cursor"],
                    has_next=raw["has_next"], next_cursor=raw["next_cursor"])
            except ValueError:
                raise RuntimeError("persisted orders page corrupt") from None
        elif parity and planned["state"] in ("SETTLED", "UNKNOWN", "CANCELLED_UNSTARTED"):
            continue
        else:
            if parity and canonical_params_digest("ORDERS_PAGE", {
                "account_scope": scope, "window_from": window_from,
                "window_to": window_to, "request_cursor": cursor,
            }) != planned["params_digest"]:
                failures.add(scope)
                continue
            if parity:
                ops_kernel.start_request(
                    ProducerRole.COLLECTOR, tick.tick_id, "TRADES", ordinal,
                    invocation_id=invocation_id, now=clock())
            try:
                outcome = orders_provider(
                    account_scope=scope, status="CLOSED", from_date=window_from,
                    to_date=window_to, cursor=cursor, limit=100,
                )
            except (OpsKernelBudgetExceeded, OpsKernelCooldownActive,
                    OpsKernelConflictError):
                raise
            except Exception:
                if parity:
                    ops_kernel.settle_request(
                        ProducerRole.COLLECTOR, tick.tick_id, "TRADES", ordinal,
                        "PROVIDER_ERROR", invocation_id=invocation_id, now=clock())
                failures.add(scope)
                continue
            if outcome.code != "success":
                if parity:
                    ops_kernel.settle_request(
                        ProducerRole.COLLECTOR, tick.tick_id, "TRADES", ordinal,
                        _request_exit(outcome.code), invocation_id=invocation_id, now=clock())
                failures.add(scope)
                if _provider_reason(outcome.code) in {
                        "failed_provider_auth", "failed_provider_contract", "failed_ceiling"}:
                    break
                continue
            page = outcome.value
            if parity:
                checkpoint = OrdersPageCheckpointV1(
                    1, tick.tick_id, ordinal, scope, window_from, window_to,
                    cursor, tuple(page.orders), page.has_next, page.next_cursor)
                encoded = encode_orders_page_checkpoint_v1(checkpoint)
                ops_kernel.checkpoint_orders_page(
                    ProducerRole.COLLECTOR, tick.tick_id, ordinal,
                    account_scope=scope, window_from=window_from, window_to=window_to,
                    request_cursor=cursor, orders_json=encoded.decode(),
                    has_next=page.has_next, next_cursor=page.next_cursor,
                    page_digest=orders_page_digest_v1(encoded),
                    invocation_id=invocation_id, now=clock())
                ops_kernel.begin_page_apply(
                    ProducerRole.COLLECTOR, tick.tick_id, ordinal,
                    invocation_id=invocation_id, now=clock())
        pairs = list(retry_pairs[scope])
        retry_pairs[scope] = []
        account_append_ok = True
        for order in page.orders:
            disposition, value = _process_order(
                order, scope, scan_state,
                tick.tick_id, timestamp, incident_writer, mask=mask,
            )
            if disposition == "needs_detail":
                if detail_calls < detail_budget:
                    try:
                        detailed = detail_provider(
                            account_scope=scope, order_id=order.order_id,
                        )
                    except (OpsKernelBudgetExceeded, OpsKernelCooldownActive,
                            OpsKernelConflictError):
                        raise
                    detail_calls += 1
                    if detailed is not None and detailed.code == "success":
                        disposition, value = _process_order(
                            detailed.value, scope, scan_state, tick.tick_id,
                            timestamp, incident_writer,
                            mask=mask,
                        )
                if disposition == "needs_detail":
                    retry_market = value[1]
                    existing = {
                        (row.account_scope, row.source_record_key): row
                        for row in scan_state.read_incomplete_retry()
                    }.get((scope, order.order_id))
                    scan_state.add_incomplete_retry(_Retry(
                        scope, order.order_id, retry_market,
                        tick.tick_id if existing is None
                        else existing.first_seen_tick_id,
                        1 if existing is None else existing.attempts + 1,
                        tick.tick_id, 0 if existing is None else existing.session_age,
                        _anchor(retry_market).isoformat(), None,
                    ))
                    if existing is None:
                        incident_writer("execution-incomplete", frozenset({scope}))
                    disposition = "incomplete"
            if disposition == "event":
                pairs.append(value)
            elif disposition == "quarantine":
                account_data[scope]["quarantine"].append(order.order_id)
            elif disposition == "incomplete":
                account_data[scope]["incomplete"].append(order.order_id)
        frozen = False if state is None else state.watermark_frozen
        if pairs:
            apply_status, failing_step, inserted, duplicates, conflicts = _save_batch(
                store, scan_state, scope, pairs, tick.tick_id, timestamp,
                incident_writer,
            )
            if apply_status != "ok":
                failures.add(scope)
                account_append_ok = False
                if parity and apply_status == "deterministic":
                    raw = ops_kernel.begin_page_apply(
                        ProducerRole.COLLECTOR, tick.tick_id, ordinal,
                        invocation_id=invocation_id, now=clock())["orders_page"]
                    try:
                        incident_writer("store-apply-failed", (
                            tick.tick_id, ordinal, raw["page_digest"], failing_step,
                            "STORE_APPLY_FAILED",
                        ))
                    except Exception as error:
                        raise RuntimeError("store_corrupt: store apply audit failed") from error
                    ops_kernel.settle_request(
                        ProducerRole.COLLECTOR, tick.tick_id, "TRADES", ordinal,
                        "STORE_APPLY_FAILED", invocation_id=invocation_id, now=clock())
            else:
                retry_keys = {
                    row.source_record_key for row in retries
                    if row.account_scope == scope
                }
                for event, _fact in pairs:
                    if event.source_record_key in retry_keys:
                        scan_state.resolve_incomplete_retry(
                            scope, event.source_record_key,
                        )
            account_data[scope]["inserted"].extend(inserted)
            account_data[scope]["duplicates"].extend(duplicates)
            account_data[scope]["quarantine"].extend(conflicts)
            frozen = frozen or bool(conflicts)
        if not account_append_ok:
            scan_states[scope] = state
            continue
        next_cursor = page.next_cursor if page.has_next else None
        pagination = pagination or page.has_next
        initial = state is None or state.watermark_date is None
        prior_watermark = None if state is None else state.watermark_date
        transition_date = (
            date.fromisoformat(window_to) if parity or mask is not None
            else now.date()
        )
        new_state = _State(
            scope,
            (
                prior_watermark
                if page.has_next or frozen or not account_append_ok
                else (
                    prior_watermark if backfill and not initial
                    else transition_date.fromordinal(
                        transition_date.toordinal() - 1
                    ).isoformat()
                )
            ),
            (None if backfill else next_cursor),
            (None if backfill or not page.has_next else window_from),
            (None if backfill or not page.has_next else window_to),
            (next_cursor if backfill else (
                None if state is None else state.backfill_cursor
            )),
            backfill and page.has_next,
            (
                transition_date.isoformat()
                if (backfill or initial) and not page.has_next
                else (None if state is None else state.last_backfill_date)
            ),
            frozen, timestamp,
        )
        scan_state.save_state(new_state)
        if parity and account_append_ok:
            ops_kernel.settle_request(
                ProducerRole.COLLECTOR, tick.tick_id, "TRADES", ordinal,
                "EMPTY_OK" if not page.orders else "OK",
                invocation_id=invocation_id, now=clock())
        scan_states[scope] = new_state
        if page.has_next and pages[scope] < 2:
            active.append(scope)

    # A successful DETAIL retry is a ledger candidate independent of ORDERS.
    # Provider failure or the fixed ORDERS call partition must not discard it.
    for scope, pairs in retry_pairs.items():
        if not pairs:
            continue
        apply_status, _failing_step, inserted, duplicates, conflicts = _save_batch(
            store, scan_state, scope, pairs, tick.tick_id, timestamp,
            incident_writer,
        )
        if apply_status != "ok":
            failures.add(scope)
            continue
        account_data[scope]["inserted"].extend(inserted)
        account_data[scope]["duplicates"].extend(duplicates)
        account_data[scope]["quarantine"].extend(conflicts)
        for event, _fact in pairs:
            scan_state.resolve_incomplete_retry(scope, event.source_record_key)

    pagination = bool(active) or any(
        state is not None and state.pending_cursor is not None
        for state in scan_states.values()
    )
    for scope in scopes:
        account_data[scope]["quarantine"] = sorted(set(
            account_data[scope]["quarantine"] + list(scan_state.read_quarantine(scope))
        ))
        account_data[scope]["incomplete"] = sorted(set(
            account_data[scope]["incomplete"]
            + [row.source_record_key for row in scan_state.read_incomplete_retry()
               if row.account_scope == scope]
            + [row.order_id for row in scan_state.read_market_pending(scope)]
        ))
    quarantine_count = sum(len(row["quarantine"]) for row in account_data.values())
    incomplete_count = sum(len(row["incomplete"]) for row in account_data.values())
    complete = CompletenessV1(
        len(scopes) - len(failures), len(failures), pagination,
        quarantine_count, incomplete_count,
    )
    payload = {
        "accounts": [{
            "account_scope": scope,
            "inserted": account_data[scope]["inserted"],
            "duplicates": sorted(set(account_data[scope]["duplicates"])),
            "quarantine": account_data[scope]["quarantine"],
            "incomplete": account_data[scope]["incomplete"],
        } for scope in scopes],
        "pagination_pending": pagination,
    }
    pack = build_material_pack_v1(
        schema_version=1, job=JobKind.TRADES, tick_id=tick.tick_id,
        market_partition=(
            tuple(sorted(market.value for market in mask.markets))
            if mask is not None else tuple(
                verdict.market.value for verdict in session_result.verdicts
            )
        ),
        payload=payload, captured_at=now,
        source_timestamps={
            "provider": _iso(now), "fetched": _iso(now), "stored": timestamp,
        },
        parser_version="toss-orders-v1",
        mask_digest=mask_digest,
    )
    try:
        path = pack_writer.write(pack)
    except Exception:
        _attempt(
            receipt_store, states, tick.tick_id, AttemptStatus.FAILED,
            "failed_pack", complete, clock, mask_digest=mask_digest,
        )
        return TradesSyncResult(
            "failed", "failed_pack", tick.tick_id, None, None, None, complete,
        )
    digest = hashlib.sha256(canonical_pack_file_bytes(pack)).hexdigest()
    completed = clock()
    if failures:
        status = AttemptStatus.PARTIAL
        reason = "partial_accounts"
        result_status = "partial"
    elif quarantine_count:
        status = AttemptStatus.PARTIAL
        reason = "partial_quarantine"
        result_status = "partial"
    elif incomplete_count:
        status = AttemptStatus.PARTIAL
        reason = "partial_incomplete"
        result_status = "partial"
    elif pagination:
        status = AttemptStatus.PARTIAL
        reason = "partial_pagination"
        result_status = "partial"
    else:
        status = AttemptStatus.SAVED
        reason = "saved"
        result_status = "saved"
    previous = receipt_store.read()
    success = (
        LastSuccessV1(tick.tick_id, str(path), digest, now, completed)
        if status is AttemptStatus.SAVED
        else (None if previous is None else previous.last_success)
    )
    receipt_store.replace(ReceiptV1(
        1, JobKind.TRADES, AttemptV1(tick.tick_id, status, reason, completed),
        success, _market_rows(
            states, None if previous is None else previous.markets,
            tick.tick_id if status is AttemptStatus.SAVED else None,
            now if status is AttemptStatus.SAVED else None,
        ), complete, mask_digest=mask_digest,
    ))
    recent = sorted(
        (item[0] for item in pack_writer.list_packs(JobKind.TRADES)), reverse=True,
    )[:12]
    pack_writer.reap_orphans(JobKind.TRADES, frozenset(recent), frozenset())
    return TradesSyncResult(
        result_status, reason, tick.tick_id, str(path), digest, now, complete,
    )


# Unforgeable module-private capability (Stage 2 repair M1): the mask-less
# legacy mode of the locked implementation exists only behind this exact
# token, presented solely by the sealed ``tokmo.programs.legacy_g2`` module.
# A production caller passing ``mask=None`` — with or without a forged token —
# fails closed before any lock, provider I/O, or scan-state/receipt mutation.
_LEGACY_G2_CAPABILITY = object()


def _require_masked_or_legacy(tick, mask, session_result, legacy):
    legacy_mode = legacy is _LEGACY_G2_CAPABILITY
    if tick is None:
        raise PermissionError("tick identity is mandatory")
    if (mask is None) != legacy_mode:
        raise PermissionError(
            "mask-less operation is sealed to the legacy G2 capability"
        )
    if not legacy_mode and session_result is not None:
        raise PermissionError("session verdict reinterpretation is legacy-only")


def run_trades_sync(
    *, tick, mask, holdings: CurrentHoldingsV1 | None,
    orders_provider, detail_provider, store: TradesStoreSeam, scan_state,
    pack_writer, receipt_store, reservation_seal, incident_writer,
    regular_session_dates: RegularSessionDates,
    clock, now,
    job_lock_path: Path, ops_kernel=None, invocation_id=None,
    stage_mode="provider", consumer_audit_reader=None,
    consumer_window_from=None, watermark_advance=None,
) -> TradesSyncResult:
    """Production Stage 2 trades collector.

    The canonical ``CadenceTickV1`` and the tick's ``OpenMarketMaskV1`` are
    mandatory; policy version, scheduled slot, tick id, and digest are all
    compared before any provider I/O, lock, or scan-state/receipt mutation
    (B1/M1).
    """
    require_tick_mask_binding(mask, tick)
    if stage_mode == "consumer":
        if consumer_audit_reader is None or consumer_window_from is None:
            raise ValueError("consumer dependencies required")
        consumed = consume_execution_window(
            tick=tick, audit_reader=consumer_audit_reader,
            window_from_utc=consumer_window_from,
            window_to_utc=tick.scheduled_for_utc)
        complete = CompletenessV1(1, 0, False, 0, 0)
        if consumed.status != "saved":
            return TradesSyncResult(
                "failed", consumed.reason, tick.tick_id, None, None, None,
                complete)
        captured = clock()
        payload = {"executions": [{
            "source": row[0], "account_scope": row[1],
            "source_record_key": row[2], "stored_at": row[3],
        } for row in consumed.record_keys]}
        pack = build_material_pack_v1(
            schema_version=1, job=JobKind.TRADES, tick_id=tick.tick_id,
            market_partition=tuple(sorted(m.value for m in mask.markets)),
            payload=payload, captured_at=captured,
            source_timestamps={
                "provider": None, "fetched": None,
                "stored": max((row[3] for row in consumed.record_keys),
                              default=None)},
            parser_version="toss-orders-v1", mask_digest=mask.digest)
        try:
            path = pack_writer.write(pack)
            digest = hashlib.sha256(canonical_pack_file_bytes(pack)).hexdigest()
            completed = clock()
            states = _market_states(None, mask)
            receipt_store.replace(ReceiptV1(
                1, JobKind.TRADES,
                AttemptV1(tick.tick_id, AttemptStatus.SAVED, "saved", completed),
                LastSuccessV1(tick.tick_id, str(path), digest, captured, completed),
                _market_rows(states, {}, tick.tick_id, captured), complete,
                mask_digest=mask.digest))
            if watermark_advance is not None:
                watermark_advance(tick.scheduled_for_utc, completed, tick.tick_id)
        except Exception:
            return TradesSyncResult(
                "failed", "failed_pack", tick.tick_id, None, None, None,
                complete)
        return TradesSyncResult(
            "saved", "saved", tick.tick_id, str(path), digest, captured, complete)
    return _locked_trades_sync(
        tick=tick, session_result=None, holdings=holdings,
        orders_provider=orders_provider, detail_provider=detail_provider,
        store=store, scan_state=scan_state, pack_writer=pack_writer,
        receipt_store=receipt_store, reservation_seal=reservation_seal,
        incident_writer=incident_writer,
        regular_session_dates=regular_session_dates, clock=clock, now=now,
        job_lock_path=job_lock_path, ops_kernel=ops_kernel,
        invocation_id=invocation_id, mask=mask,
    )


def _locked_trades_sync(
    *, tick, session_result, holdings: CurrentHoldingsV1 | None,
    orders_provider, detail_provider, store: TradesStoreSeam, scan_state,
    pack_writer, receipt_store, reservation_seal, incident_writer,
    regular_session_dates: RegularSessionDates,
    clock, now,
    job_lock_path: Path, ops_kernel, invocation_id, mask, legacy=None,
) -> TradesSyncResult:
    _require_masked_or_legacy(tick, mask, session_result, legacy)
    if (ops_kernel is None) != (invocation_id is None):
        raise ValueError("invalid request parity configuration")
    states = _market_states(session_result, mask)
    mask_digest = None if mask is None else mask.digest
    fd = _lock(job_lock_path)
    complete = CompletenessV1(0, 0, False, 0, 0)
    if fd == -1:
        _attempt(
            receipt_store, states, tick.tick_id, AttemptStatus.FAILED,
            "failed_store", complete, clock, mask_digest=mask_digest,
        )
        return TradesSyncResult(
            "failed", "failed_store", tick.tick_id, None, None, None, complete,
        )
    if fd is None:
        _attempt(
            receipt_store, states, tick.tick_id, AttemptStatus.SKIPPED,
            "skipped_lock_contention", complete, clock, mask_digest=mask_digest,
        )
        return TradesSyncResult(
            "skipped", "skipped_lock_contention", tick.tick_id,
            None, None, None, complete,
        )
    try:
        return _run(
            tick=tick, session_result=session_result, holdings=holdings,
            orders_provider=orders_provider, detail_provider=detail_provider,
            store=store, scan_state=scan_state, pack_writer=pack_writer,
            receipt_store=receipt_store, reservation_seal=reservation_seal,
            incident_writer=incident_writer,
            regular_session_dates=regular_session_dates, clock=clock, now=now,
            ops_kernel=ops_kernel, invocation_id=invocation_id, mask=mask,
        )
    except (OpsKernelBudgetExceeded, OpsKernelCooldownActive,
            OpsKernelConflictError):
        _attempt(
            receipt_store, states, tick.tick_id, AttemptStatus.FAILED,
            "failed_ceiling", complete, clock, mask_digest=mask_digest,
        )
        return TradesSyncResult(
            "failed", "failed_ceiling", tick.tick_id,
            None, None, None, complete,
        )
    finally:
        os.close(fd)


@dataclass(frozen=True)
class ConsumedExecutionWindowV1:
    """One half-open [prev, cur) stored_at window of ledger executions."""

    status: str
    reason: str
    tick_id: int
    window_from: str
    window_to: str
    record_keys: tuple


def consume_execution_window(*, tick, audit_reader, window_from_utc,
                             window_to_utc):
    """Cutover consumer seam (60s Pulse Plan §18.2): provider wire zero.

    After cutover the 300s tick derives its trades material from the
    Pulse-owned durable ingestion evidence through the injected
    ``audit_reader`` — ``() -> rows`` where each row carries
    ``(source, account_scope, source_record_key, stored_at)``. Window
    membership is ``window_from <= stored_at < window_to`` so consecutive
    windows partition the timeline with zero duplication and zero loss.
    The pack identity stays this 300s tick; a Pulse tick identity is
    refused at the type level.
    """
    from tokmo.values.cadence_policy import CadenceTickV1

    if not isinstance(tick, CadenceTickV1):
        raise ValueError("execution consumption binds a 300s cadence tick only")
    for boundary in (window_from_utc, window_to_utc):
        if (not isinstance(boundary, datetime) or boundary.tzinfo is None
                or boundary.utcoffset() is None):
            raise ValueError("window boundaries must be aware datetimes")
    if window_from_utc >= window_to_utc:
        raise ValueError("window must be non-empty and forward")
    try:
        rows = tuple(audit_reader())
    except Exception:
        return ConsumedExecutionWindowV1(
            "failed", "failed_store_read", tick.tick_id,
            _iso(window_from_utc), _iso(window_to_utc), (),
        )
    selected = []
    for row in rows:
        stored_text = row["stored_at"]
        stored = datetime.fromisoformat(stored_text.replace("Z", "+00:00"))
        if window_from_utc <= stored < window_to_utc:
            selected.append(
                (row["source"], row["account_scope"],
                 row["source_record_key"], stored_text))
    selected.sort()
    return ConsumedExecutionWindowV1(
        "saved", "saved", tick.tick_id,
        _iso(window_from_utc), _iso(window_to_utc), tuple(selected),
    )
