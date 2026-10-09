"""Collector B: latest quotes for open, observed holdings markets."""

import fcntl
import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from tokmo.values.account_scope_patterns import (
    ACCOUNT_SCOPE_RE,
    PENSION_SCOPE_RE,
)
from tokmo.values.cadence_policy import QUOTE_BATCH_SYMBOLS_V1, CadenceTickV1
from tokmo.values.current_holdings import CurrentHoldingsV1, classify_market
from tokmo.values.material_pack import (
    JobKind,
    build_material_pack_v1,
    canonical_pack_file_bytes,
)
from tokmo.values.open_market_mask import require_tick_mask_binding
from tokmo.values.ops_kernel import (
    OpsKernelBudgetExceeded,
    OpsKernelConflictError,
    OpsKernelCooldownActive,
)
from tokmo.values.receipt_v1 import (
    AttemptStatus,
    AttemptV1,
    LastSuccessV1,
    MarketFreshness,
    ReceiptV1,
)
from tokmo.values.session_gate import Market


def derive_analysis_targets(
    holdings, watchlist_symbols, market_scope, pension_excluded
):
    """Pure canonical target selection shared by quotes and daily analysis.

    This helper deliberately owns no cap, incident, receipt, or terminal
    behavior.  Callers retain those policy decisions.
    """
    selected = {}
    excluded = frozenset(pension_excluded)
    for market in sorted(market_scope, key=lambda item: item.value):
        # Preserve the historical quote contract exactly: exclusions apply to
        # holdings only, while watchlist spelling/case and non-empty symbols
        # pass through unchanged.  Analysis callers own any stricter grammar.
        candidates = {
            symbol for symbol in holdings.get(market, ()) if symbol not in excluded
        }
        candidates.update(watchlist_symbols.get(market, ()))
        if candidates:
            selected[market] = tuple(sorted(candidates))
    return selected, frozenset()


@dataclass(frozen=True)
class QuotesSyncResult:
    status: str
    reason: str
    tick_id: int
    pack_path: str | None
    pack_sha256: str | None
    markets: Mapping[str, MarketFreshness]


def pension_only_symbols(holdings: CurrentHoldingsV1 | None) -> frozenset[str]:
    """Return symbols held only in PENSION accounts (absent from PERSONAL).

    A symbol held in at least one PERSONAL account is quotable and excluded.
    Only symbols appearing exclusively under pension-excel-* scopes are
    returned, since those typically represent assets with no live quote feed.
    """
    if holdings is None:
        return frozenset()
    personal_symbols: set[str] = set()
    pension_symbols: set[str] = set()
    for account in holdings.accounts:
        scope = account.account_scope
        target = (
            personal_symbols
            if ACCOUNT_SCOPE_RE.fullmatch(scope)
            else pension_symbols
            if PENSION_SCOPE_RE.fullmatch(scope)
            else None
        )
        if target is None:
            continue
        for item in account.holdings:
            target.add(item.symbol)
    return frozenset(pension_symbols - personal_symbols)


@dataclass(frozen=True)
class ConsumedQuoteRowV1:
    """One store-consumed quote with its original instants preserved."""

    symbol: str
    last_price: str
    currency_raw: str
    provider_timestamp: "str | None"
    fetched_at: str
    stored_at: "str | None"


@dataclass(frozen=True)
class QuoteConsumeResultV1:
    status: str
    reason: str
    tick_id: int
    rows_by_market: Mapping


def consume_latest_quotes(*, tick, symbols_by_market, latest_reader):
    """Cutover consumer seam (60s Pulse Plan §18.1): provider wire zero.

    After cutover the 300s tick builds its quote material by reading the
    Pulse-owned ``LatestQuoteStore`` through the injected ``latest_reader``
    — ``(market, symbols) -> rows`` where each row carries the original
    ``provider_timestamp/fetched_at/stored_at``. The instants pass through
    unchanged: a stale quote is never promoted to fresh, and the pack
    identity stays this 300s tick (a Pulse tick identity is refused). A
    busy store is a typed ``STORE_BUSY`` failure, never a silent reuse of
    an older success as if it were current.
    """
    if not isinstance(tick, CadenceTickV1):
        raise ValueError("quote consumption binds a 300s cadence tick only")
    rows = {}
    for market in sorted(symbols_by_market, key=lambda item: item.value):
        symbols = tuple(symbols_by_market[market])
        if not symbols:
            continue
        try:
            fetched = latest_reader(market, symbols)
        except Exception as error:
            reason = (
                "STORE_BUSY"
                if getattr(error, "reason", None) == "store_locked"
                else "failed_store_read"
            )
            return QuoteConsumeResultV1(
                "failed", reason, tick.tick_id, MappingProxyType({})
            )
        consumed = []
        for row in fetched:
            consumed.append(
                ConsumedQuoteRowV1(
                    row["symbol"],
                    row["last_price"],
                    row["currency_raw"],
                    row.get("provider_timestamp"),
                    row["fetched_at"],
                    row.get("stored_at"),
                )
            )
        rows[market] = tuple(consumed)
    return QuoteConsumeResultV1("saved", "saved", tick.tick_id, MappingProxyType(rows))


def _map_quotes_outcome_reason(code: str) -> str:
    if code == "success":
        return "saved"
    if code == "http_429":
        return "failed_provider_rate_limit"
    if code == "auth_rejection":
        return "failed_provider_auth"
    if code in {"contract_mismatch", "parse_error"}:
        return "failed_provider_contract"
    if code == "ceiling_exceeded":
        return "failed_ceiling"
    return "failed_provider_other"


def _market_states(session_result, mask):
    """Market open flags: mask when bound (never reinterpreted), else verdicts."""
    if mask is not None:
        return {market.value: market in mask.markets for market in mask.entries}
    return {verdict.market.value: verdict.open for verdict in session_result.verdicts}


def _effective_markets(
    states, previous, refreshed=frozenset(), tick_id=None, captured_at=None
):
    prior = {} if previous is None else previous.markets
    effective = {}
    for key, is_open in states.items():
        old = prior.get(key)
        if Market(key) in refreshed:
            success_tick, success_at = tick_id, captured_at
        else:
            success_tick = None if old is None else old.last_success_tick_id
            success_at = None if old is None else old.captured_at
        effective[key] = MarketFreshness(
            key,
            "open" if is_open else "closed",
            success_tick,
            success_at,
        )
    return effective


def _attempt(receipt_store, states, tick_id, status, reason, clock, mask_digest):
    # mask_digest is mandatory (B3): the execution context owns the tick's
    # exact digest (None only on the sealed legacy verdict path) and every
    # terminal receipt path must pass it explicitly.
    attempt = AttemptV1(tick_id, status, reason, clock())
    prev = receipt_store.read()
    markets = _effective_markets(states, prev)
    if prev is None:
        receipt = ReceiptV1(
            1,
            JobKind.QUOTES,
            attempt,
            None,
            markets,
            None,
            mask_digest=mask_digest,
        )
    else:
        receipt = ReceiptV1(
            prev.schema_version,
            prev.job,
            attempt,
            prev.last_success,
            markets,
            prev.completeness,
            mask_digest=mask_digest,
        )
    receipt_store.replace(receipt)
    return dict(receipt.markets)


def _effective_success_markets(
    receipt_store, states, queried_markets, tick_id, captured_at
):
    previous = receipt_store.read()
    return _effective_markets(
        states,
        previous,
        queried_markets,
        tick_id,
        captured_at,
    )


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


def _aware(value):
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _valid_quotes_success(outcome, symbols):
    facts = getattr(outcome, "facts", None)
    if type(facts) is not tuple or len(facts) != len(symbols) or not facts:
        return False
    returned = []
    for fact in facts:
        symbol = getattr(fact, "symbol", None)
        returned.append(symbol)
        if (
            type(symbol) is not str
            or not symbol
            or type(getattr(fact, "last_price", None)) is not str
            or not fact.last_price
            or type(getattr(fact, "currency_raw", None)) is not str
            or not fact.currency_raw
            or not _aware(getattr(fact, "fetched_at", None))
            or (
                getattr(fact, "timestamp", None) is not None
                and not _aware(fact.timestamp)
            )
        ):
            return False
    return len(set(returned)) == len(returned) and set(returned) == set(symbols)


def _valid_save_receipt(saved, fact_count):
    return (
        getattr(saved, "status", None) == "SAVED"
        and getattr(saved, "reason", None) == "saved"
        and type(getattr(saved, "inserted_count", None)) is int
        and type(getattr(saved, "updated_count", None)) is int
        and saved.inserted_count >= 0
        and saved.updated_count >= 0
        and saved.inserted_count + saved.updated_count == fact_count
        and _aware(getattr(saved, "stored_at", None))
    )


def _run_quotes_sync(
    *,
    tick,
    session_result,
    holdings: CurrentHoldingsV1 | None,
    quotes_provider: Callable,
    quote_store: Callable,
    pack_writer,
    receipt_store,
    reservation_seal,
    incident_writer: Callable,
    clock: Callable[[], datetime],
    now: datetime,
    mask=None,
    watchlist_symbols=None,
    pension_unquotable_symbols: frozenset[str] = frozenset(),
):
    del reservation_seal, now
    states = _market_states(session_result, mask)
    digest = None if mask is None else mask.digest

    def _terminal(status, reason):
        """One receipt owner for every terminal path: the digest of this
        execution context can never be omitted (B3)."""
        markets = _attempt(
            receipt_store,
            states,
            tick.tick_id,
            status,
            reason,
            clock,
            digest,
        )
        result_status = "skipped" if status is AttemptStatus.SKIPPED else "failed"
        return QuotesSyncResult(
            result_status,
            reason,
            tick.tick_id,
            None,
            None,
            markets,
        )

    if mask is not None:
        open_markets = tuple(sorted(mask.markets, key=lambda item: item.value))
    else:
        open_markets = tuple(
            verdict.market for verdict in session_result.verdicts if verdict.open
        )
    if not open_markets:
        return _terminal(AttemptStatus.SKIPPED, "skipped_market_closed")
    holding_targets = {market: [] for market in open_markets}
    unknown = set()
    pension_excluded = pension_only_symbols(holdings) & frozenset(
        pension_unquotable_symbols
    )
    if holdings is not None:
        for account in holdings.accounts:
            for item in account.holdings:
                market = classify_market(item.market_raw, item.market_field)
                if market is None:
                    unknown.add(item.symbol)
                elif market in holding_targets:
                    holding_targets[market].append(item.symbol)
    watchlist_targets = {market: () for market in open_markets}
    if watchlist_symbols is not None:
        for market in open_markets:
            symbols = watchlist_symbols.get(market, frozenset())
            if not isinstance(symbols, frozenset) or not all(
                isinstance(symbol, str) and symbol for symbol in symbols
            ):
                return _terminal(AttemptStatus.FAILED, "failed_watchlist_contract")
            watchlist_targets[market] = symbols
    selected, invalid = derive_analysis_targets(
        holding_targets,
        watchlist_targets,
        frozenset(open_markets),
        pension_excluded,
    )
    unknown.update(invalid)
    if unknown:
        incident_writer("market-classification-unknown", frozenset(unknown))
    if not selected:
        return _terminal(AttemptStatus.SKIPPED, "skipped_zero_quote_targets")
    rows = {market: [] for market in open_markets if market in selected}
    provider_times = []
    fetched_times = []
    stored_times = []
    for market in open_markets:
        symbols = selected.get(market, ())
        for start in range(0, len(symbols), QUOTE_BATCH_SYMBOLS_V1):
            batch = symbols[start : start + QUOTE_BATCH_SYMBOLS_V1]
            try:
                outcome = quotes_provider(batch)
                code = outcome.code
            except (
                OpsKernelBudgetExceeded,
                OpsKernelCooldownActive,
                OpsKernelConflictError,
            ):
                code = "ceiling_exceeded"
                outcome = None
            except Exception:
                code = "protocol_failure"
                outcome = None
            if code != "success":
                return _terminal(AttemptStatus.FAILED, _map_quotes_outcome_reason(code))
            try:
                valid_outcome = _valid_quotes_success(outcome, batch)
            except Exception:
                valid_outcome = False
            if not valid_outcome:
                return _terminal(AttemptStatus.FAILED, "failed_provider_contract")
            try:
                saved = quote_store(outcome)
            except Exception:
                saved = None
            try:
                valid_save = _valid_save_receipt(saved, len(outcome.facts))
            except Exception:
                valid_save = False
            if not valid_save:
                return _terminal(AttemptStatus.FAILED, "failed_store")
            stored_times.append(saved.stored_at)
            for fact in outcome.facts:
                if fact.timestamp is not None:
                    provider_times.append(fact.timestamp)
                fetched_times.append(fact.fetched_at)
                rows[market].append(
                    {
                        "symbol": fact.symbol,
                        "last_price": fact.last_price,
                        "currency_raw": fact.currency_raw,
                        "provider_timestamp": (
                            None if fact.timestamp is None else _iso(fact.timestamp)
                        ),
                        "fetched_at": _iso(fact.fetched_at),
                        "stored_at": _iso(saved.stored_at),
                    }
                )
    captured = clock()
    pack = build_material_pack_v1(
        schema_version=1,
        job=JobKind.QUOTES,
        tick_id=tick.tick_id,
        market_partition=tuple(market.value for market in rows),
        payload={"markets": {market.value: value for market, value in rows.items()}},
        captured_at=captured,
        source_timestamps={
            "provider": None if not provider_times else _iso(max(provider_times)),
            "fetched": _iso(max(fetched_times)),
            "stored": _iso(max(stored_times)),
        },
        parser_version="toss-current-price-v1",
        mask_digest=digest,
    )
    try:
        path = pack_writer.write(pack)
    except Exception:
        return _terminal(AttemptStatus.FAILED, "failed_pack")
    pack_sha = hashlib.sha256(canonical_pack_file_bytes(pack)).hexdigest()
    completed = clock()
    markets = _effective_success_markets(
        receipt_store,
        states,
        frozenset(rows),
        tick.tick_id,
        captured,
    )
    receipt_store.replace(
        ReceiptV1(
            1,
            JobKind.QUOTES,
            AttemptV1(tick.tick_id, AttemptStatus.SAVED, "saved", completed),
            LastSuccessV1(tick.tick_id, str(path), pack_sha, captured, completed),
            markets,
            None,
            mask_digest=digest,
        )
    )
    recent = sorted(
        (item[0] for item in pack_writer.list_packs(JobKind.QUOTES)),
        reverse=True,
    )[:12]
    pack_writer.reap_orphans(JobKind.QUOTES, frozenset(recent), frozenset())
    return QuotesSyncResult(
        "saved",
        "saved",
        tick.tick_id,
        str(path),
        pack_sha,
        markets,
    )


def _try_job_lock(job_lock_path: Path):
    fd = None
    try:
        path = Path(job_lock_path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
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


# Unforgeable module-private capability (Stage 2 repair M1): the mask-less
# legacy mode of the locked implementation exists only behind this exact
# token, presented solely by the sealed ``tokmo.programs.legacy_g2`` module.
# A production caller passing ``mask=None`` — with or without a forged token —
# fails closed before any lock, provider I/O, or receipt mutation.
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


def run_quotes_sync(
    *,
    tick,
    mask,
    holdings: CurrentHoldingsV1 | None,
    quotes_provider: Callable,
    quote_store: Callable,
    pack_writer,
    receipt_store,
    reservation_seal,
    incident_writer: Callable,
    clock: Callable[[], datetime],
    now: datetime,
    job_lock_path: Path,
    watchlist_symbols=None,
    pension_unquotable_symbols: frozenset[str] = frozenset(),
    stage_mode="provider",
    latest_consumer_reader=None,
):
    """Production Stage 2 quotes collector.

    The canonical ``CadenceTickV1`` and the tick's ``OpenMarketMaskV1`` are
    mandatory; policy version, scheduled slot, tick id, and digest are all
    compared before any provider I/O, lock, or receipt mutation (B1/M1).
    """
    require_tick_mask_binding(mask, tick)
    return _locked_quotes_sync(
        tick=tick,
        session_result=None,
        holdings=holdings,
        quotes_provider=quotes_provider,
        quote_store=quote_store,
        pack_writer=pack_writer,
        receipt_store=receipt_store,
        reservation_seal=reservation_seal,
        incident_writer=incident_writer,
        clock=clock,
        now=now,
        job_lock_path=job_lock_path,
        mask=mask,
        watchlist_symbols=watchlist_symbols,
        pension_unquotable_symbols=pension_unquotable_symbols,
        stage_mode=stage_mode,
        latest_consumer_reader=latest_consumer_reader,
    )


def _locked_quotes_sync(
    *,
    tick,
    session_result,
    holdings: CurrentHoldingsV1 | None,
    quotes_provider: Callable,
    quote_store: Callable,
    pack_writer,
    receipt_store,
    reservation_seal,
    incident_writer: Callable,
    clock: Callable[[], datetime],
    now: datetime,
    job_lock_path: Path,
    mask,
    legacy=None,
    watchlist_symbols=None,
    pension_unquotable_symbols: frozenset[str] = frozenset(),
    stage_mode="provider",
    latest_consumer_reader=None,
):
    _require_masked_or_legacy(tick, mask, session_result, legacy)
    states = _market_states(session_result, mask)
    digest = None if mask is None else mask.digest
    fd = _try_job_lock(job_lock_path)
    if fd == -1:
        markets = _attempt(
            receipt_store,
            states,
            tick.tick_id,
            AttemptStatus.FAILED,
            "failed_store",
            clock,
            mask_digest=digest,
        )
        return QuotesSyncResult(
            "failed",
            "failed_store",
            tick.tick_id,
            None,
            None,
            markets,
        )
    if fd is None:
        markets = _attempt(
            receipt_store,
            states,
            tick.tick_id,
            AttemptStatus.SKIPPED,
            "skipped_lock_contention",
            clock,
            mask_digest=digest,
        )
        return QuotesSyncResult(
            "skipped",
            "skipped_lock_contention",
            tick.tick_id,
            None,
            None,
            markets,
        )
    try:
        if stage_mode == "consumer":
            if latest_consumer_reader is None:
                raise ValueError("consumer reader required")
            open_markets = tuple(sorted(mask.markets, key=lambda item: item.value))
            holding_targets = {market: [] for market in open_markets}
            if holdings is not None:
                for account in holdings.accounts:
                    for item in account.holdings:
                        market = classify_market(item.market_raw, item.market_field)
                        if market in holding_targets:
                            holding_targets[market].append(item.symbol)
            selected, _ = derive_analysis_targets(
                holding_targets,
                watchlist_symbols or {},
                frozenset(open_markets),
                pension_only_symbols(holdings) & pension_unquotable_symbols,
            )
            consumed = consume_latest_quotes(
                tick=tick,
                symbols_by_market=selected,
                latest_reader=latest_consumer_reader,
            )
            if consumed.status != "saved":
                markets = _attempt(
                    receipt_store,
                    states,
                    tick.tick_id,
                    AttemptStatus.FAILED,
                    consumed.reason,
                    clock,
                    mask_digest=digest,
                )
                return QuotesSyncResult(
                    "failed", consumed.reason, tick.tick_id, None, None, markets
                )
            rows = {
                market: [row.__dict__ for row in values]
                for market, values in consumed.rows_by_market.items()
            }
            captured = clock()
            all_rows = [row for values in rows.values() for row in values]
            timestamps = lambda key: [row[key] for row in all_rows if row[key]]
            pack = build_material_pack_v1(
                schema_version=1,
                job=JobKind.QUOTES,
                tick_id=tick.tick_id,
                market_partition=tuple(market.value for market in rows),
                payload={
                    "markets": {market.value: values for market, values in rows.items()}
                },
                captured_at=captured,
                source_timestamps={
                    "provider": max(timestamps("provider_timestamp"), default=None),
                    "fetched": max(timestamps("fetched_at"), default=None),
                    "stored": max(timestamps("stored_at"), default=None),
                },
                parser_version="toss-current-price-v1",
                mask_digest=digest,
            )
            path = pack_writer.write(pack)
            pack_sha = hashlib.sha256(canonical_pack_file_bytes(pack)).hexdigest()
            completed = clock()
            markets = _effective_success_markets(
                receipt_store, states, frozenset(rows), tick.tick_id, captured
            )
            receipt_store.replace(
                ReceiptV1(
                    1,
                    JobKind.QUOTES,
                    AttemptV1(tick.tick_id, AttemptStatus.SAVED, "saved", completed),
                    LastSuccessV1(
                        tick.tick_id, str(path), pack_sha, captured, completed
                    ),
                    markets,
                    None,
                    mask_digest=digest,
                )
            )
            return QuotesSyncResult(
                "saved", "saved", tick.tick_id, str(path), pack_sha, markets
            )
        return _run_quotes_sync(
            tick=tick,
            session_result=session_result,
            holdings=holdings,
            quotes_provider=quotes_provider,
            quote_store=quote_store,
            pack_writer=pack_writer,
            receipt_store=receipt_store,
            reservation_seal=reservation_seal,
            incident_writer=incident_writer,
            clock=clock,
            now=now,
            mask=mask,
            watchlist_symbols=watchlist_symbols,
            pension_unquotable_symbols=pension_unquotable_symbols,
        )
    finally:
        if fd >= 0:
            os.close(fd)
