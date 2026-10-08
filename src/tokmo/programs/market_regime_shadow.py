"""Read-only market-regime shadow orchestration primitives."""

from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from tokmo.values.session_gate import MARKET_TIMEZONE, Market
from tokmo.facts.market_regime_toss_input import (
    candle_native_identity, candle_request_identity, completed_session_boundary,
    normalize_daily_candles,
)
from tokmo.programs.market_regime_core import SPEC_DIGEST, evaluate_market_regime_core_v1
from tokmo.programs.quotes_sync import derive_analysis_targets
from tokmo.records.regular_session_history import RegularSessionHistoryStore
from tokmo.records.market_regime_shadow_state import MarketRegimeShadowStateStore
from tokmo.values.ops_kernel import (
    CadenceBudgetExceeded, CadenceDuplicateError, CadencePolicyMismatchError,
)
from tokmo.values.current_holdings import classify_market
from tokmo.values.market_regime_core import AdjustmentMode

_ANALYSIS_SYMBOL = re.compile(r"[A-Z0-9._-]{1,32}\Z", re.ASCII)

def resolve_completed_session_by_market(*, scheduled_for_utc: datetime,
                                        market: Market, today_row,
                                        history_store, official_close: datetime):
    if (scheduled_for_utc.tzinfo is None or scheduled_for_utc.utcoffset() is None
            or official_close.tzinfo is None or official_close.utcoffset() is None):
        raise ValueError("aware timestamps required")
    zone = ZoneInfo(MARKET_TIMEZONE[market])
    local_now = scheduled_for_utc.astimezone(zone)
    if (today_row is not None and today_row.get("session") == "regular"
            and local_now >= official_close.astimezone(zone)):
        return local_now.date()
    cursor = local_now.date() - timedelta(days=1)
    for _ in range(35):
        row = history_store.read(market, cursor)
        if row is None:
            return None
        if row["session"] == "regular":
            return cursor
        cursor -= timedelta(days=1)
    return None


def _identity_digest(*, policy_version, market, symbol, completed_session,
                     adjustment_mode, request_identity):
    values = ("market-regime-core-v1", SPEC_DIGEST, policy_version, market.value,
              symbol, completed_session.isoformat(), adjustment_mode.value,
              request_identity)
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def _pack_payload(pack):
    if pack is None or isinstance(pack, (str, int, bool)):
        return pack
    if isinstance(pack, Decimal):
        return str(pack)
    if isinstance(pack, Enum):
        return pack.value
    if isinstance(pack, (date, datetime)):
        return pack.isoformat()
    if is_dataclass(pack) and not isinstance(pack, type):
        return {field.name: _pack_payload(getattr(pack, field.name))
                for field in fields(pack)}
    if isinstance(pack, dict):
        return {key: _pack_payload(value) for key, value in pack.items()}
    if isinstance(pack, (tuple, list)):
        return [_pack_payload(value) for value in pack]
    raise TypeError("unsupported result payload")


class _AfterAdmissionAbort(RuntimeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _ProviderFailure(RuntimeError):
    pass


def _recurring_selection(root, deps):
    """Canonical recurring portfolio selection; ``(selected, failure_reason)``.

    Holdings union active watchlist with the exact historical quote
    semantics; fail-closed on any source or admissibility violation.
    """
    holdings = {market: [] for market in Market}
    try:
        from tokmo.records.current_holdings_store import CurrentHoldingsStore
        current = CurrentHoldingsStore(
            root / "stores" / "current_holdings.json", clock=deps.clock,
        ).read()
        if current is not None:
            for account in current.accounts:
                for item in account.holdings:
                    market = classify_market(item.market_raw, item.market_field)
                    if market is None:
                        holdings.setdefault(None, []).append(item.symbol)
                    else:
                        holdings[market].append(item.symbol)
    except Exception:
        return None, "target_source_invalid"
    classification_unknown = holdings.pop(None, [])
    selected, unknown = derive_analysis_targets(
        holdings=holdings, watchlist_symbols=deps.watchlist_symbols,
        market_scope=frozenset(Market),
        pension_excluded=deps.pension_unquotable_symbols,
    )
    if unknown or classification_unknown:
        return None, "unknown_target"
    if any(
        not isinstance(symbol, str)
        or _ANALYSIS_SYMBOL.fullmatch(symbol) is None
        for symbols in selected.values() for symbol in symbols
    ):
        return None, "unknown_target"
    return selected, None


def _resolve_market_session(*, market, scheduled_for_utc, history, cache):
    """Per-market completed-session resolution; ``None`` when unresolvable."""
    entry = None if cache is None else (
        cache.kr if market is Market.KR else cache.us
    )
    today = scheduled_for_utc.astimezone(
        ZoneInfo(MARKET_TIMEZONE[market])
    ).date()
    today_row = history.read(market, today)
    if entry is None or entry.trade_date != today.isoformat():
        return None
    regular = entry.sessions.get("regularMarket")
    close = None if regular is None else datetime.fromisoformat(
        regular.end.replace("Z", "+00:00")
    )
    if close is None:
        # No regular session today: resolver walks official history only.
        close = scheduled_for_utc
        today_row = None
    return resolve_completed_session_by_market(
        scheduled_for_utc=scheduled_for_utc, market=market,
        today_row=today_row, history_store=history, official_close=close,
    )


def _execute_symbol_analysis(*, state, deps, executor, token, policy, market,
                             symbol, completed, adjustment, identity,
                             request_identity, invocation_id):
    """Admit and settle exactly one symbol's claim→fetch→evaluate chain."""
    def _started(started_claim):
        identity_lease = state.acquire_identity_lease(identity)
        if identity_lease is None:
            raise _AfterAdmissionAbort("in_progress_after_admission")
        with identity_lease:
            claim = state.claim(
                identity_digest=identity, program_id="market-regime-core-v1",
                spec_digest=SPEC_DIGEST, policy_version=policy.policy_version,
                market=market, symbol=symbol, completed_session_date=completed,
                adjustment_mode=adjustment, request_identity=request_identity,
                owner_invocation_id=invocation_id, owner_started_at=deps.clock(),
                owner_lease_key=identity_lease.key, identity_lease=identity_lease,
            )
            if claim.state == "reused":
                raise _AfterAdmissionAbort("in_progress_after_admission")
            if claim.state == "recovery_required":
                raise _AfterAdmissionAbort("recovery_required_after_admission")
            if claim.state != "owner":
                raise _AfterAdmissionAbort("in_progress_after_admission")
            fetched_at = deps.clock()
            try:
                outcome = deps.stock_candles_fetch(
                    claim=started_claim, bearer=token.bearer(), symbol=symbol,
                    before=completed_session_boundary(market, completed), adjusted=True,
                )
                if getattr(outcome, "code", None) != "success":
                    raise ValueError("candle_provider_failed")
                value = getattr(outcome, "value", None)
                rows = getattr(value, "candles", None)
                if rows is None:
                    raise ValueError("candle_contract_mismatch")
                series = normalize_daily_candles(
                    rows, market=market, symbol=symbol, completed_session=completed,
                    adjustment_mode=adjustment,
                )
                pack = evaluate_market_regime_core_v1(series)
                evaluated_at = deps.clock()
                state.succeed(
                    identity, input_digest=series.input_digest,
                    pack_digest=pack.pack_digest, fetched_at=fetched_at,
                    evaluated_at=evaluated_at, completed_session_date=completed,
                    normalized_bar_count=len(series.bars),
                    result_payload=_pack_payload(pack),
                )
            except Exception as error:
                state.fail(identity, failure_code=type(error).__name__,
                           finished_at=deps.clock())
                raise _ProviderFailure(type(error).__name__) from error
        return outcome

    return executor.execute(
        "STOCK_CANDLES_DAILY",
        candle_native_identity("mr", market, symbol, completed, 1),
        _started,
    )


def _drain_reason(*, admissible, terminal, in_progress, resolved):
    """Bounded numeric coverage reason: counts only, never symbols."""
    parts = []
    pending_total = 0
    for market in sorted(Market, key=lambda item: item.value):
        total = admissible.get(market, 0)
        done = terminal.get(market, 0)
        parts.append(f"{market.value.lower()}_{done}_of_{total}")
        if market in resolved:
            pending_total += total - done - in_progress.get(market, 0)
    return "_".join(parts) + f"_pending_{pending_total}"


def run_market_regime_shadow(*, root, deps, policy, tick, executor, token,
                             cache_store, invocation_id):
    """Recurring per-tick portfolio drain; return ``(status, reason)``.

    All admissible holdings+watchlist symbols are drained in deterministic
    ``(market.value, symbol)`` order, at most ``market_daily_candle_starts``
    candle admissions per tick. The claim store is the durable
    pending/coverage ledger: probe-before-admission keeps every
    SUCCEEDED/FAILED/in-progress identity at zero wire. Provider failures are
    isolated per symbol; a ``CadenceBudgetExceeded`` mid-drain is a normal
    termination the next tick inherits.
    """
    selected, failure = _recurring_selection(root, deps)
    if failure is not None:
        return "failed", failure
    targets = tuple(
        (market, symbol) for market in sorted(selected, key=lambda item: item.value)
        for symbol in selected[market]
    )
    if not targets:
        return "skipped", "no_eligible_target"

    history = RegularSessionHistoryStore(
        root / "ops" / "regular_session_history.sqlite3", clock=deps.clock,
    )
    try:
        cache = cache_store.read()
    except Exception:
        cache = None
    markets_present = tuple(
        sorted({market for market, _ in targets}, key=lambda item: item.value)
    )
    sessions = {}
    for market in markets_present:
        try:
            sessions[market] = _resolve_market_session(
                market=market, scheduled_for_utc=tick.scheduled_for_utc,
                history=history, cache=cache,
            )
        except Exception:
            # Per-market isolation: an unresolvable market skips its own
            # symbols only; the other market drains normally.
            sessions[market] = None
    resolved = frozenset(
        market for market in markets_present if sessions[market] is not None
    )
    if not resolved:
        return "skipped", "completed_session_unavailable"

    adjustment = AdjustmentMode.ADJUSTED
    state = MarketRegimeShadowStateStore(
        root / "ops" / "market_regime_shadow.sqlite3"
    )
    admissible = {market: len(selected[market]) for market in markets_present}
    terminal = {market: 0 for market in markets_present}
    in_progress = {market: 0 for market in markets_present}
    pending = []
    for market, symbol in targets:
        completed = sessions[market]
        if completed is None:
            continue
        request_identity = candle_request_identity(
            lane="mr", market=market, symbol=symbol,
            completed_session=completed, adjusted=True,
        )
        identity = _identity_digest(
            policy_version=policy.policy_version, market=market, symbol=symbol,
            completed_session=completed, adjustment_mode=adjustment,
            request_identity=request_identity,
        )
        existing = state.probe(identity)
        if existing is None:
            pending.append((market, symbol, completed, identity, request_identity))
        elif existing.state == "in_progress":
            in_progress[market] += 1
        else:
            # reused or failed: terminal for this session, zero wire.
            terminal[market] += 1
    if not pending:
        return "skipped", "same_completed_session"

    # The tick's shared bearer is acquired before any symbol identity exists,
    # so a token failure consumes zero identities (F6).
    try:
        token.bearer()
    except Exception:
        return "failed", "token_unavailable"

    budget = policy.request_limits.market_daily_candle_starts
    progressed = 0
    failed_new = 0
    admitted = 0
    stop_reason = None
    abort_reason = None
    for market, symbol, completed, identity, request_identity in pending:
        if admitted >= budget:
            break
        admitted += 1
        try:
            _execute_symbol_analysis(
                state=state, deps=deps, executor=executor, token=token,
                policy=policy, market=market, symbol=symbol,
                completed=completed, adjustment=adjustment, identity=identity,
                request_identity=request_identity, invocation_id=invocation_id,
            )
        except CadenceBudgetExceeded:
            # Normal termination: nothing started, the next tick inherits.
            admitted -= 1
            stop_reason = "admission_budget_exhausted"
            break
        except CadenceDuplicateError:
            abort_reason = "admission_duplicate"
            continue
        except CadencePolicyMismatchError:
            stop_reason = "admission_policy_mismatch"
            break
        except _AfterAdmissionAbort as error:
            abort_reason = error.reason
            continue
        except _ProviderFailure:
            # Isolated terminal FAILED for this identity; drain continues.
            failed_new += 1
            terminal[market] += 1
            continue
        except Exception:
            return "failed", "admission_internal_error"
        progressed += 1
        terminal[market] += 1

    reason = _drain_reason(admissible=admissible, terminal=terminal,
                           in_progress=in_progress, resolved=resolved)
    if failed_new:
        return "failed", reason
    if progressed:
        return "ok", reason
    if stop_reason is not None:
        return "skipped", stop_reason
    if abort_reason is not None:
        return "skipped", abort_reason
    return "skipped", reason


def read_market_regime_coverage(*, root, deps, policy_version,
                                observed_now_utc, cache_store):
    """Read-only per-market coverage/readiness for one drain policy (§8).

    A deterministic data-availability readout: probes the claim store per
    admissible symbol and reads the latest recurring receipt's shadow-stage
    record. No provider wire, no claim, no model trigger — READY only
    declares that the deterministic Market pack set is consumable.
    """
    result = {"policy_version": policy_version, "markets": {},
              "last_stage": _last_shadow_stage(root, policy_version)}
    selected, failure = _recurring_selection(root, deps)
    if failure is not None:
        result["reason"] = failure
        return result
    history = RegularSessionHistoryStore(
        root / "ops" / "regular_session_history.sqlite3", clock=deps.clock,
    )
    try:
        cache = cache_store.read()
    except Exception:
        cache = None
    state = MarketRegimeShadowStateStore(
        root / "ops" / "market_regime_shadow.sqlite3"
    )
    for market in sorted(selected, key=lambda item: item.value):
        symbols = selected[market]
        entry = {"admissible": len(symbols), "completed_session": None,
                 "terminal": None, "succeeded": None, "failed": None,
                 "in_progress": None, "pending": None, "ready": False}
        try:
            completed = _resolve_market_session(
                market=market, scheduled_for_utc=observed_now_utc,
                history=history, cache=cache,
            )
        except Exception:
            completed = None
        if completed is not None:
            entry["completed_session"] = completed.isoformat()
            counts = {"succeeded": 0, "failed": 0, "in_progress": 0,
                      "pending": 0}
            for symbol in symbols:
                request_identity = candle_request_identity(
                    lane="mr", market=market, symbol=symbol,
                    completed_session=completed, adjusted=True,
                )
                identity = _identity_digest(
                    policy_version=policy_version, market=market,
                    symbol=symbol, completed_session=completed,
                    adjustment_mode=AdjustmentMode.ADJUSTED,
                    request_identity=request_identity,
                )
                existing = state.probe(identity)
                if existing is None:
                    counts["pending"] += 1
                elif existing.state == "reused":
                    counts["succeeded"] += 1
                elif existing.state == "failed":
                    counts["failed"] += 1
                else:
                    counts["in_progress"] += 1
            entry.update(counts)
            entry["terminal"] = counts["succeeded"] + counts["failed"]
            entry["ready"] = (
                len(symbols) > 0 and counts["pending"] == 0
                and counts["in_progress"] == 0 and counts["succeeded"] >= 1
            )
        result["markets"][market.value] = entry
    return result


def _last_shadow_stage(root, policy_version):
    """Latest recurring receipt's ``market_regime_shadow`` stage record."""
    path = root / "ops" / "cadence_ticks.sqlite3"
    if not path.exists():
        return None
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT scheduled_for_utc, stages_json FROM cadence_tick_receipts"
            " WHERE policy_version = ? AND stages_json IS NOT NULL"
            " ORDER BY scheduled_for_utc DESC LIMIT 1",
            (policy_version,),
        ).fetchone()
    if row is None:
        return None
    try:
        stages = json.loads(row["stages_json"])
    except ValueError:
        return None
    for stage in stages:
        if stage.get("stage") == "market_regime_shadow":
            return {"scheduled_for_utc": row["scheduled_for_utc"],
                    "status": stage.get("status"),
                    "reason": stage.get("reason")}
    return None


@dataclass(frozen=True)
class TargetedRegimeTargetV1:
    """Typed identity provenance for one explicit-authority analysis.

    ``requested_by`` is ephemeral type-level provenance only: never
    persisted, never part of the identity digest. The dedicated type exists
    so a derived selection cannot masquerade as an explicit user target.
    """

    market: Market
    symbol: str
    requested_by: str


def run_market_regime_targeted(*, root, deps, policy, tick, executor, token,
                               cache_store, invocation_id,
                               target: TargetedRegimeTargetV1):
    """Explicit-target one-shot; returns ``(status, reason)``.

    Separate seam from the recurring cap-one selector: it never reads
    holdings or watchlist and never substitutes another symbol. An
    unservable target fails closed with zero provider I/O (token included)
    before admission, and exactly one admitted fetch (retry 0) after it.
    """
    if (type(target) is not TargetedRegimeTargetV1
            or not isinstance(target.market, Market)
            or type(target.symbol) is not str
            or _ANALYSIS_SYMBOL.fullmatch(target.symbol) is None
            or type(target.requested_by) is not str
            or not target.requested_by):
        return "failed", "invalid_target"
    market = target.market
    symbol = target.symbol
    history = RegularSessionHistoryStore(
        root / "ops" / "regular_session_history.sqlite3", clock=deps.clock,
    )
    try:
        cache = cache_store.read()
        entry = None if cache is None else (
            cache.kr if market is Market.KR else cache.us
        )
        today = tick.scheduled_for_utc.astimezone(
            ZoneInfo(MARKET_TIMEZONE[market])
        ).date()
        today_row = history.read(market, today)
        if entry is None or entry.trade_date != today.isoformat():
            return "skipped", "completed_session_unavailable"
        regular = entry.sessions.get("regularMarket")
        close = None if regular is None else datetime.fromisoformat(
            regular.end.replace("Z", "+00:00")
        )
        if close is None:
            # No regular session today: resolver walks official history only.
            close = tick.scheduled_for_utc
            today_row = None
        completed = resolve_completed_session_by_market(
            scheduled_for_utc=tick.scheduled_for_utc, market=market,
            today_row=today_row, history_store=history, official_close=close,
        )
    except Exception:
        return "failed", "completed_session_invalid"
    if completed is None:
        return "skipped", "completed_session_unavailable"

    adjustment = AdjustmentMode.ADJUSTED
    request_identity = candle_request_identity(
        lane="mr", market=market, symbol=symbol, completed_session=completed,
        adjusted=True,
    )
    identity = _identity_digest(
        policy_version=policy.policy_version, market=market, symbol=symbol,
        completed_session=completed, adjustment_mode=adjustment,
        request_identity=request_identity,
    )
    state = MarketRegimeShadowStateStore(
        root / "ops" / "market_regime_shadow.sqlite3"
    )
    existing = state.probe(identity)
    if existing is not None:
        if existing.state == "reused":
            return "skipped", "same_completed_session"
        if existing.state == "in_progress":
            return "skipped", "in_progress"
        if existing.state == "failed":
            return "skipped", "failed"

    def _started(started_claim):
        identity_lease = state.acquire_identity_lease(identity)
        if identity_lease is None:
            raise _AfterAdmissionAbort("in_progress_after_admission")
        with identity_lease:
            claim = state.claim(
                identity_digest=identity, program_id="market-regime-core-v1",
                spec_digest=SPEC_DIGEST, policy_version=policy.policy_version,
                market=market, symbol=symbol, completed_session_date=completed,
                adjustment_mode=adjustment, request_identity=request_identity,
                owner_invocation_id=invocation_id, owner_started_at=deps.clock(),
                owner_lease_key=identity_lease.key, identity_lease=identity_lease,
            )
            if claim.state == "reused":
                raise _AfterAdmissionAbort("in_progress_after_admission")
            if claim.state == "recovery_required":
                raise _AfterAdmissionAbort("recovery_required_after_admission")
            if claim.state != "owner":
                raise _AfterAdmissionAbort("in_progress_after_admission")
            fetched_at = deps.clock()
            try:
                outcome = deps.stock_candles_fetch(
                    claim=started_claim, bearer=token.bearer(), symbol=symbol,
                    before=completed_session_boundary(market, completed),
                    adjusted=True,
                )
                if getattr(outcome, "code", None) != "success":
                    raise ValueError("candle_provider_failed")
                value = getattr(outcome, "value", None)
                rows = getattr(value, "candles", None)
                if rows is None:
                    raise ValueError("candle_contract_mismatch")
                series = normalize_daily_candles(
                    rows, market=market, symbol=symbol,
                    completed_session=completed, adjustment_mode=adjustment,
                )
                if series.symbol != target.symbol:
                    raise ValueError("target_identity_mismatch")
                pack = evaluate_market_regime_core_v1(series)
                evaluated_at = deps.clock()
                state.succeed(
                    identity, input_digest=series.input_digest,
                    pack_digest=pack.pack_digest, fetched_at=fetched_at,
                    evaluated_at=evaluated_at, completed_session_date=completed,
                    normalized_bar_count=len(series.bars),
                    result_payload=_pack_payload(pack),
                )
            except Exception as error:
                state.fail(identity, failure_code=type(error).__name__,
                           finished_at=deps.clock())
                raise _ProviderFailure(type(error).__name__) from error
        return outcome

    try:
        executor.execute(
            "STOCK_CANDLES_DAILY",
            candle_native_identity("mr", market, symbol, completed, 1),
            _started,
        )
    except CadenceBudgetExceeded:
        return "skipped", "admission_budget_exhausted"
    except CadenceDuplicateError:
        return "skipped", "admission_duplicate"
    except CadencePolicyMismatchError:
        return "skipped", "admission_policy_mismatch"
    except _AfterAdmissionAbort as error:
        return "skipped", error.reason
    except _ProviderFailure as error:
        return "failed", f"exception_{error}"
    except Exception:
        return "failed", "admission_internal_error"
    return "ok", None
