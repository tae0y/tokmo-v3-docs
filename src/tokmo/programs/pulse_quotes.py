"""Pulse quote stage: lookahead gate, targets, monotonic upsert (§13–§15).

Read-only inputs: the 300s-owned calendar lookahead window, the latest
COMPLETE canonical holdings snapshot, and the git-tracked watchlists. The
stage never refreshes the calendar, never re-fetches holdings, and writes
only the shared ``LatestQuoteStore`` — one provider batch per open market,
guarded so an older observation never overwrites a newer row and a
currency-conflicting observation is refused per symbol.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType

from tokmo.records.calendar_lookahead import (
    CalendarLookaheadConflictError,
    CalendarLookaheadCorruptError,
    judge_lookahead_session,
)
from tokmo.records.latest_quotes import LatestQuoteStoreReadError
from tokmo.values.cadence_policy import QUOTE_BATCH_SYMBOLS_V1
from tokmo.values.current_holdings import CurrentHoldingsV1, classify_market
from tokmo.values.pulse_policy import PulseTickV1
from tokmo.values.session_gate import Market

QUOTE_SOURCE = "toss-current-price-v1"


@dataclass(frozen=True)
class PulseQuoteBatchOutcomeV1:
    """Injected provider outcome: per-symbol partial success is explicit."""

    code: str
    facts: tuple = ()
    failed_symbols: tuple = ()


@dataclass(frozen=True)
class PulseQuotesStageResult:
    status: str
    reason: str
    tick_id: int
    open_markets: tuple
    market_reasons: Mapping
    holdings_status: str
    holdings_as_of: "datetime | None"
    targets: Mapping
    upserted: tuple
    dropped_stale: tuple
    rejected_currency_conflict: tuple
    failed_symbols: tuple
    wire_starts: int
    quote_observed_at: "datetime | None" = field(default=None)


def _map_failure_reason(code: str) -> str:
    if code == "http_429":
        return "failed_provider_rate_limit"
    if code == "auth_rejection":
        return "failed_provider_auth"
    if code in ("contract_mismatch", "parse_error"):
        return "failed_provider_contract"
    return "failed_provider_other"


def _effective_instants(fact):
    return (fact.timestamp, fact.fetched_at)


def _is_older(new_fact, existing) -> bool:
    new_provider, new_fetched = _effective_instants(new_fact)
    old_provider, old_fetched = _effective_instants(existing)
    if new_provider is not None and old_provider is not None:
        return new_provider < old_provider
    return new_fetched < old_fetched


def run_pulse_quotes(
    *,
    tick: PulseTickV1,
    lookahead_reader: Callable,
    holdings_reader: Callable,
    watchlist_symbols,
    token_provider: Callable,
    quotes_provider: Callable,
    quote_store,
    clock: Callable[[], datetime],
    now: datetime,
    wire_recorder: Callable,
    pension_unquotable_symbols: frozenset = frozenset(),
):
    """One Pulse quote stage. Provider wires: TOKEN ≤1, PRICE_BATCH ≤1/market.

    ``wire_recorder(operation, native_identity)`` is called before every
    provider start; both markets closed (or fail-closed) means the recorder
    and the token provider are never touched.
    """
    if not isinstance(tick, PulseTickV1):
        raise ValueError("pulse quote stage requires a PulseTickV1")
    market_reasons = {}
    verdicts = {}
    for market in (Market.KR, Market.US):
        try:
            days = lookahead_reader(market)
        except (CalendarLookaheadCorruptError, CalendarLookaheadConflictError):
            market_reasons[market] = "lookahead_corrupt"
            continue
        except Exception:
            market_reasons[market] = "lookahead_read_failed"
            continue
        verdict = judge_lookahead_session(market, tuple(days), now)
        verdicts[market] = verdict
        if not verdict.open:
            market_reasons[market] = verdict.reason
    open_markets = tuple(
        market
        for market in (Market.KR, Market.US)
        if market in verdicts and verdicts[market].open
    )

    holdings = None
    holdings_status = "ok"
    try:
        holdings = holdings_reader()
    except Exception:
        holdings = None
    if holdings is not None and not isinstance(holdings, CurrentHoldingsV1):
        holdings = None
    if holdings is None:
        holdings_status = "HOLDINGS_UNAVAILABLE"
    holdings_as_of = None if holdings is None else holdings.captured_at

    def _result(
        status,
        reason,
        targets=(),
        upserted=(),
        dropped=(),
        conflicts=(),
        failed=(),
        wires=0,
        observed=None,
    ):
        return PulseQuotesStageResult(
            status,
            reason,
            tick.tick_id,
            tuple(market.value for market in open_markets),
            MappingProxyType(dict(market_reasons)),
            holdings_status,
            holdings_as_of,
            MappingProxyType(dict(targets) if isinstance(targets, dict) else {}),
            tuple(upserted),
            tuple(dropped),
            tuple(conflicts),
            tuple(failed),
            wires,
            observed,
        )

    if not open_markets:
        return _result("skipped", "skipped_market_closed")

    # --- target derivation (§14) -----------------------------------------
    holding_targets = {market: set() for market in open_markets}
    pension_only = set()
    if holdings is not None:
        from tokmo.programs.quotes_sync import pension_only_symbols

        pension_only = pension_only_symbols(holdings) & frozenset(
            pension_unquotable_symbols
        )
        for account in holdings.accounts:
            for item in account.holdings:
                market = classify_market(item.market_raw, item.market_field)
                if market in holding_targets and item.symbol not in pension_only:
                    holding_targets[market].add(item.symbol)
    targets = {}
    for market in open_markets:
        symbols = set(holding_targets[market])
        watch = (
            watchlist_symbols.get(market, frozenset())
            if watchlist_symbols is not None
            else frozenset()
        )
        if not isinstance(watch, frozenset) or not all(
            isinstance(symbol, str) and symbol for symbol in watch
        ):
            market_reasons[market] = "failed_watchlist_contract"
            continue
        symbols.update(watch)
        if symbols:
            targets[market] = tuple(sorted(symbols))

    if not targets:
        failed_markets = [
            m
            for m in open_markets
            if market_reasons.get(m) == "failed_watchlist_contract"
        ]
        if failed_markets:
            return _result("failed", "failed_watchlist_contract")
        return _result("skipped", "skipped_zero_quote_targets")

    # --- provider batches (§12.2/§15) ------------------------------------
    token = None
    wire_starts = 0
    upserted = []
    dropped_stale = []
    conflicts = []
    failed_symbols = []
    batch_ordinal = 0
    observed_candidates = []
    for market in open_markets:
        symbols = targets.get(market)
        if symbols is None:
            continue
        if len(symbols) > QUOTE_BATCH_SYMBOLS_V1:
            market_reasons[market] = "quote_target_overflow"
            continue
        if token is None:
            # Lazy per-tick token: the provider owns its own wire recording
            # so quotes and execution share one TOKEN start per tick.
            token = token_provider()
        wire_recorder("PRICE_BATCH", f"quotes:{batch_ordinal}")
        batch_ordinal += 1
        wire_starts += 1
        try:
            outcome = quotes_provider(market, symbols)
        except Exception:
            market_reasons[market] = "failed_provider_other"
            continue
        code = getattr(outcome, "code", None)
        if code != "success":
            market_reasons[market] = _map_failure_reason(code)
            continue
        facts = getattr(outcome, "facts", ())
        returned = [getattr(fact, "symbol", None) for fact in facts]
        if len(set(returned)) != len(returned) or not set(returned) <= set(symbols):
            market_reasons[market] = "failed_provider_contract"
            continue
        failed_symbols.extend(
            symbol
            for symbol in getattr(outcome, "failed_symbols", ())
            if symbol in symbols
        )
        admitted = []
        for fact in facts:
            existing = None
            try:
                existing = quote_store.read_latest(
                    source=QUOTE_SOURCE, symbols=(fact.symbol,)
                )[0]
            except LatestQuoteStoreReadError as error:
                if error.reason in ("store_locked",):
                    market_reasons[market] = "STORE_BUSY"
                    admitted = None
                    break
                existing = None
            if existing is not None:
                if existing.currency_raw != fact.currency_raw:
                    conflicts.append(fact.symbol)
                    continue
                if _is_older(fact, existing):
                    dropped_stale.append(fact.symbol)
                    continue
            admitted.append(fact)
        if admitted is None:
            continue
        if admitted:
            saved = quote_store.save_batch(tuple(admitted))
            if getattr(saved, "status", None) != "SAVED":
                if getattr(saved, "reason", None) == "store_locked":
                    market_reasons[market] = "STORE_BUSY"
                else:
                    market_reasons[market] = "failed_store"
                continue
            upserted.extend(fact.symbol for fact in admitted)
            observed_candidates.extend(
                fact.timestamp or fact.fetched_at for fact in admitted
            )

    failed_markets = [market for market in open_markets if market in market_reasons]
    observed = max(observed_candidates) if observed_candidates else None
    if not failed_markets:
        status, reason = "saved", "saved"
    elif len(failed_markets) == len(open_markets):
        status = "failed"
        reason = market_reasons[failed_markets[0]]
    else:
        status, reason = "partial", "partial_markets"
    return _result(
        status,
        reason,
        targets,
        upserted,
        dropped_stale,
        conflicts,
        failed_symbols,
        wire_starts,
        observed,
    )
