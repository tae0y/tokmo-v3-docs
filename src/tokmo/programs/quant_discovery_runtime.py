"""Fixture/local one-shot for the Quant discovery engine.

After the engine stabilizes, NEWS may separately explain or challenge divergence.
NEWS is not admission, ranking, composite, or trade trigger authority.
"""

from dataclasses import dataclass
from datetime import date, timezone
from decimal import Decimal
from hashlib import sha256
import json

from tokmo.facts.toss_reads import TossReadOperation, build_read_request, query_read
from tokmo.programs.market_regime_shadow import resolve_completed_session_by_market
from tokmo.programs.quant_discovery_entry import build_reference_entry_zone
from tokmo.programs.quant_discovery_features import compute_price_features
from tokmo.programs.quant_discovery_ranking import compute_peer_standing
from tokmo.records.quant_reports import QuantReportStore
from tokmo.values.current_holdings import CurrentHoldingsV1, classify_market
from tokmo.values.pension_holdings import PensionHoldingsRevisionV1, pension_account_scope
from tokmo.values.quant_discovery import (
    DecisionCardV1, PriceBarV1, PriceSeriesV1, QuantCandidateV1,
    QuantMarketProfileV1, QuantProductKind, QuantRunIdentityV1,
    QuantRunReceiptV1, QuantRunReportV1, QuantUnavailableCode, FactorStatus,
)
from tokmo.values.session_gate import Market
from tokmo.values.toss_market_data import CandlePageResponse
from tokmo.values.toss_rankings import RankingResponse
from tokmo.values.toss_reference import StockInfo
from tokmo.values._primitives import MISSING


@dataclass(frozen=True)
class DueResult:
    due: bool
    reason: object = None
    completed_iso_week: str | None = None


@dataclass(frozen=True)
class PensionProjection:
    symbols: tuple[tuple[str, QuantMarketProfileV1], ...]
    unavailable_code: QuantUnavailableCode | None


@dataclass(frozen=True)
class TargetUnion:
    market: tuple[tuple[str, QuantMarketProfileV1], ...]
    personal: tuple[tuple[str, QuantMarketProfileV1], ...]
    pension: tuple[tuple[str, QuantMarketProfileV1], ...]
    all_unique: tuple[tuple[str, QuantMarketProfileV1], ...]


@dataclass(frozen=True)
class CandidateFunnelResultV1:
    candidates: tuple[QuantCandidateV1, ...]
    exclusions: tuple[tuple[str, QuantUnavailableCode], ...]


@dataclass(frozen=True)
class RunResult:
    report: QuantRunReportV1
    receipt: QuantRunReceiptV1
    symbol_unavailable: tuple[tuple[str, QuantUnavailableCode], ...] = ()
    replay_counts: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class ReplayResult:
    report: QuantRunReportV1
    receipt: QuantRunReceiptV1
    operation_counts: tuple[tuple[str, int], ...]


def _profile(market):
    return QuantMarketProfileV1(market.value)


def project_personal_symbols(holdings: CurrentHoldingsV1 | None):
    if holdings is None:
        return ()
    found = set()
    for account in holdings.accounts:
        if account.account_scope == pension_account_scope():
            continue
        for item in account.holdings:
            market = classify_market(item.market_raw, item.market_field)
            if market is not None:
                found.add((item.symbol, _profile(market)))
    return tuple(sorted(found, key=lambda item: (item[1].value, item[0])))


def project_pension_symbols(revision: PensionHoldingsRevisionV1 | None):
    if revision is None:
        return PensionProjection((), QuantUnavailableCode.PENSION_ACTIVE_REVISION_ABSENT)
    found = set()
    for item in revision.holdings:
        market = classify_market(item.market_raw, item.market_field)
        if market is not None:
            found.add((item.symbol, _profile(market)))
    return PensionProjection(tuple(sorted(found, key=lambda item: (item[1].value, item[0]))), None)


def plan_target_union(*, discovery, public_personal, active_pension, reviewed_peers):
    key = lambda item: (item[1].value, item[0])
    market = tuple(dict.fromkeys(discovery))
    personal = tuple(sorted(set(public_personal), key=key))
    pension = tuple(sorted(set(active_pension), key=key))
    extras = sorted(set(personal + pension + tuple(reviewed_peers)) - set(market), key=key)
    return TargetUnion(market, personal, pension, market + tuple(extras))


def admit_ranking_candidates(profile, ranking, stocks):
    if not isinstance(profile, QuantMarketProfileV1) or not isinstance(ranking, RankingResponse):
        raise TypeError("typed profile and ranking required")
    if not isinstance(stocks, tuple) or not all(isinstance(stock, StockInfo) for stock in stocks):
        raise TypeError("typed exact stock outcome required")
    by_symbol = {stock.symbol: stock for stock in stocks}
    admitted = []
    exclusions = []
    seen_ranks = set()
    seen_symbols = set()
    expected_currency = "KRW" if profile is QuantMarketProfileV1.KR else "USD"
    def ranking_key(value):
        valid_rank = isinstance(value.rank, int) and not isinstance(value.rank, bool)
        return (0 if valid_rank else 1, value.rank if valid_rank else 0,
                value.symbol if isinstance(value.symbol, str) else "")

    for item in sorted(ranking.rankings, key=ranking_key):
        symbol = item.symbol if isinstance(item.symbol, str) else ""
        malformed = (isinstance(item.rank, bool) or not isinstance(item.rank, int) or item.rank <= 0
                     or not symbol or item.rank in seen_ranks or symbol in seen_symbols)
        if malformed:
            exclusions.append((symbol, QuantUnavailableCode.PROVIDER_CONTRACT_MISMATCH))
            continue
        seen_ranks.add(item.rank)
        seen_symbols.add(symbol)
        stock = by_symbol.get(item.symbol)
        if (stock is None or stock.status != "ACTIVE" or stock.currency != expected_currency
                or item.currency != stock.currency):
            exclusions.append((symbol, QuantUnavailableCode.STOCK_METADATA_UNAVAILABLE))
            continue
        admitted.append(QuantCandidateV1(item.symbol, profile, stock.name, stock.security_type,
                                         (item.rank,), Decimal(item.trading_amount),
                                         None if item.price.change_rate in (None, MISSING) else Decimal(item.price.change_rate),
                                         None))
    return CandidateFunnelResultV1(tuple(admitted), tuple(exclusions))


def evaluate_market_due(*, completed_session, terminal_exists, scheduled_for_utc=None,
                        market=None, today_row=None, history_store=None, official_close=None):
    if scheduled_for_utc is not None:
        completed_session = resolve_completed_session_by_market(scheduled_for_utc=scheduled_for_utc, market=market,
            today_row=today_row, history_store=history_store, official_close=official_close)
    if completed_session is None:
        return DueResult(False, QuantUnavailableCode.NO_COMPLETED_SESSION)
    return DueResult(not terminal_exists, "ALREADY_TERMINAL" if terminal_exists else None)


def evaluate_pension_week_due(*, completed_session: date | None, completed_week_final_proof: bool, terminal_exists: bool):
    if completed_session is None:
        return DueResult(False, QuantUnavailableCode.NO_COMPLETED_SESSION)
    if completed_week_final_proof is not True:
        return DueResult(False, QuantUnavailableCode.PENSION_WEEK_NOT_FINAL)
    iso = completed_session.isocalendar()
    return DueResult(not terminal_exists, "ALREADY_TERMINAL" if terminal_exists else None, f"{iso.year}-W{iso.week:02d}")


def _identity(profile, session, profile_revision, report_revision):
    scalars = [QuantProductKind.MARKET_DISCOVERY.value, profile.value, session.isoformat(), profile_revision, None, report_revision]
    digest = sha256(json.dumps(scalars, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return QuantRunIdentityV1(QuantProductKind.MARKET_DISCOVERY, profile, session, None,
                              profile_revision, None, report_revision, digest)


def _product_identity(*, product, market, completed_session, completed_iso_week,
                      profile_revision, peer_registry_revision, report_revision):
    scalars = [product.value, market.value,
               completed_session.isoformat() if completed_session else None,
               completed_iso_week, profile_revision, peer_registry_revision, report_revision]
    digest = sha256(json.dumps(scalars, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return QuantRunIdentityV1(product, market, completed_session, completed_iso_week,
                              profile_revision, peer_registry_revision, report_revision, digest)


def _authority_revision(domain, *scalars):
    return sha256(json.dumps([domain, *scalars], separators=(",", ":"),
                             ensure_ascii=False).encode()).hexdigest()


def _personal_authority_revision(deps, holdings):
    if holdings is None:
        return _authority_revision("quant-personal-authority-absent-v1", deps.profile_revision)
    captured_at = holdings.captured_at.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return _authority_revision("quant-personal-authority-snapshot-v1", deps.profile_revision,
                               holdings.schema_version, captured_at)


def _pension_authority_revision(deps, active_revision, final_proof):
    if active_revision is None:
        return _authority_revision("quant-pension-authority-absent-v1", deps.profile_revision)
    domain = ("quant-pension-authority-final-v1" if final_proof is True
              else "quant-pension-authority-not-final-v1")
    return _authority_revision(domain, deps.profile_revision, active_revision.revision_id)


def persist_terminal(store, report, outcome):
    digest = store.append_if_absent(report)
    if store.read_digest(report.identity) != digest:
        raise ValueError("report digest read-back mismatch")
    return store.finish(report.identity, outcome, report.operation_counts, digest)


def replay_terminal(store, identity):
    receipt = store.read_receipt(identity)
    if receipt is None:
        return None
    report = store.read(identity)
    if report is None or store.read_digest(identity) != receipt.report_sha256:
        raise ValueError("receipt-bound report digest mismatch")
    return ReplayResult(report, receipt, (("WIRE", 0), ("RECOMPUTE", 0), ("RESEND", 0)))


def _unavailable(code):
    if code == "contract_mismatch":
        return QuantUnavailableCode.PROVIDER_CONTRACT_MISMATCH
    if code in {"success", None}:
        return None
    return QuantUnavailableCode.PROVIDER_FAILURE


def _series(candidate, page):
    bars = tuple(PriceBarV1(c.timestamp.date(), Decimal(c.open_price), Decimal(c.high_price),
                            Decimal(c.low_price), Decimal(c.close_price), Decimal(c.volume), None)
                 for c in sorted(page.candles, key=lambda item: item.timestamp))
    return PriceSeriesV1(candidate.symbol, candidate.market, bars)


def run_quant_once(*, deps, now, activation_enabled=False):
    del now
    if activation_enabled:
        raise ValueError("activation is not enabled")
    if len(deps.profiles) != 1:
        raise ValueError("single market G1 only")
    store = QuantReportStore(deps.root)
    final = None
    for profile in deps.profiles:
        session = deps.completed_sessions.get(profile)
        if session is None:
            continue
        identity = _identity(profile, session, deps.profile_revision, deps.report_revision)
        replay = replay_terminal(store, identity)
        if replay is not None:
            final = RunResult(replay.report, replay.receipt, (), replay.operation_counts)
            continue
        counts = {"RANKINGS": 0, "STOCKS": 0, "STOCK_CANDLES": 0}
        ranking_request = build_read_request(TossReadOperation.RANKINGS, token=deps.token,
            type="MARKET_TRADING_AMOUNT", marketCountry=profile.value, duration="1d")
        counts["RANKINGS"] += 1
        ranking_outcome = query_read(ranking_request, transport=deps.transport, clock=deps.clock)
        market_unavailable = []
        funnel_exclusions = ()
        terminal_outcome = None
        if not isinstance(ranking_outcome.value, RankingResponse):
            market_unavailable.append(_unavailable(ranking_outcome.code) or QuantUnavailableCode.RANKING_NO_DATA)
            candidates = ()
            terminal_outcome = "FAILED"
        elif not ranking_outcome.value.rankings:
            market_unavailable.append(QuantUnavailableCode.RANKING_NO_DATA)
            candidates = ()
            terminal_outcome = "NO_DATA"
        else:
            shortlist = RankingResponse(ranking_outcome.value.rankings[:deps.shortlist_cap], ranking_outcome.value.ranked_at)
            symbols = tuple(sorted(item.symbol for item in shortlist.rankings))
            stock_request = build_read_request(TossReadOperation.STOCKS, token=deps.token, symbols=symbols)
            counts["STOCKS"] += 1
            stock_outcome = query_read(stock_request, transport=deps.transport, clock=deps.clock)
            if not isinstance(stock_outcome.value, tuple):
                market_unavailable.append(_unavailable(stock_outcome.code) or QuantUnavailableCode.STOCK_METADATA_UNAVAILABLE)
                candidates = ()
                terminal_outcome = "FAILED"
            else:
                funnel = admit_ranking_candidates(profile, shortlist, stock_outcome.value)
                candidates = funnel.candidates
                funnel_exclusions = funnel.exclusions
                market_unavailable.extend(code for _, code in funnel_exclusions)
                if not candidates and any(
                    code is QuantUnavailableCode.PROVIDER_CONTRACT_MISMATCH
                    for _, code in funnel_exclusions
                ):
                    terminal_outcome = "FAILED"
        discovery = tuple((candidate.symbol, profile) for candidate in candidates)
        pension = project_pension_symbols(deps.pension_revision).symbols
        targets = plan_target_union(discovery=discovery, public_personal=deps.personal_symbols,
                                    active_pension=pension, reviewed_peers=deps.reviewed_peers)
        market_targets = tuple(symbol for symbol, market in targets.all_unique if market is profile)
        if len(market_targets) > deps.candle_start_cap:
            raise ValueError("candle start cap exceeded before candle reads")
        by_candidate = {candidate.symbol: candidate for candidate in candidates}
        cards = []
        symbol_unavailable = list(funnel_exclusions)
        candle_provider_failures = 0
        for symbol in market_targets:
            request = build_read_request(TossReadOperation.STOCK_CANDLES, token=deps.token, symbol=symbol, interval="1d", count=200, adjusted=True)
            counts["STOCK_CANDLES"] += 1
            outcome = query_read(request, transport=deps.transport, clock=deps.clock)
            if not isinstance(outcome.value, CandlePageResponse) or not outcome.value.candles:
                code = _unavailable(outcome.code) or QuantUnavailableCode.PROVIDER_FAILURE
                symbol_unavailable.append((symbol, code))
                market_unavailable.append(code)
                candle_provider_failures += 1
                continue
            latest_local_date = max(candle.timestamp.date() for candle in outcome.value.candles)
            if latest_local_date != session:
                symbol_unavailable.append((symbol, QuantUnavailableCode.CANDLE_SESSION_NOT_READY))
                market_unavailable.append(QuantUnavailableCode.CANDLE_SESSION_NOT_READY)
                continue
            candidate = by_candidate.get(symbol)
            if candidate is None:
                continue
            series = _series(candidate, outcome.value)
            features = compute_price_features(series, completed_session_date=session)
            cards.append(DecisionCardV1(symbol, profile, candidate.name, features.features.factors, (),
                                        build_reference_entry_zone(series, features.features),
                                        (() if features.unavailable_code is None else (features.unavailable_code,))))
        operation_counts = tuple((name, counts[name]) for name in ("RANKINGS", "STOCKS", "STOCK_CANDLES"))
        unavailable_codes = tuple(dict.fromkeys(code for code in market_unavailable if code is not None))
        report = QuantRunReportV1(identity, tuple(sorted(cards, key=lambda card: card.symbol)), unavailable_codes, operation_counts)
        if terminal_outcome is None:
            if cards:
                terminal_outcome = "SUCCEEDED_WITH_CARDS"
            elif candle_provider_failures and candle_provider_failures == len(market_targets):
                terminal_outcome = "FAILED"
            else:
                terminal_outcome = "SUCCEEDED_SILENT"
        receipt = persist_terminal(store, report, terminal_outcome)
        final = RunResult(report, receipt, tuple(symbol_unavailable))
    return final


def _persist_product_no_io(*, deps, identity, code=None, outcome="NO_DATA", store=None,
                           symbol_unavailable=()):
    store = store or QuantReportStore(deps.root)
    replay = replay_terminal(store, identity)
    if replay is not None:
        return RunResult(replay.report, replay.receipt, (), replay.operation_counts)
    counts = (("STOCKS", 0), ("STOCK_CANDLES", 0))
    report = QuantRunReportV1(identity, (), (() if code is None else (code,)), counts)
    return RunResult(report, persist_terminal(store, report, outcome), tuple(symbol_unavailable))


def _run_peer_product(*, deps, identity, held_symbols, product, completed_session, store,
                      as_of_date, activation_enabled=False):
    if activation_enabled:
        raise ValueError("activation is not enabled")
    max_review_age_days = getattr(deps, "peer_review_max_age_days", 90)
    if type(max_review_age_days) is not int or max_review_age_days < 1:
        raise ValueError("invalid peer review max age")
    candidate_groups = tuple(group for group in deps.peer_registry.groups
        if group.product_kind is product and group.market is identity.market
        and group.reference_symbol in held_symbols
        and 0 <= (as_of_date - group.last_reviewed).days <= max_review_age_days)
    by_reference = {symbol: tuple(group for group in candidate_groups
                                  if group.reference_symbol == symbol)
                    for symbol in held_symbols}
    groups = tuple(matches[0] for symbol in held_symbols
                   if len(matches := by_reference[symbol]) == 1)
    unconfirmed = tuple(symbol for symbol in held_symbols if len(by_reference[symbol]) != 1)
    if not groups and unconfirmed:
        return _persist_product_no_io(deps=deps, identity=identity,
            code=QuantUnavailableCode.PEER_UNCONFIRMED, store=store,
            symbol_unavailable=((symbol, QuantUnavailableCode.PEER_UNCONFIRMED)
                                for symbol in unconfirmed))
    if not held_symbols:
        return _persist_product_no_io(deps=deps, identity=identity, store=store)
    targets = tuple(sorted({symbol for group in groups
                            for symbol in (group.reference_symbol,) + group.peer_symbols}))
    counts = {"STOCKS": 0, "STOCK_CANDLES": 0}
    request = build_read_request(TossReadOperation.STOCKS, token=deps.token, symbols=targets)
    counts["STOCKS"] += 1
    stock_outcome = query_read(request, transport=deps.transport, clock=deps.clock)
    if not isinstance(stock_outcome.value, tuple):
        operation_counts = tuple(counts.items())
        report = QuantRunReportV1(identity, (),
            (_unavailable(stock_outcome.code) or QuantUnavailableCode.STOCK_METADATA_UNAVAILABLE,), operation_counts)
        return RunResult(report, persist_terminal(store, report, "FAILED"))
    stocks = {stock.symbol: stock for stock in stock_outcome.value}
    expected_currency = "KRW" if identity.market is QuantMarketProfileV1.KR else "USD"
    expected_types = {symbol: group.instrument_type for group in groups
                      for symbol in (group.reference_symbol,) + group.peer_symbols}
    if (set(stocks) != set(targets)
            or any(stock.status != "ACTIVE" or stock.currency != expected_currency
                   or stock.security_type != expected_types[stock.symbol]
                   for stock in stocks.values())):
        operation_counts = tuple(counts.items())
        report = QuantRunReportV1(identity, (), (QuantUnavailableCode.STOCK_METADATA_UNAVAILABLE,), operation_counts)
        return RunResult(report, persist_terminal(store, report, "FAILED"))
    if len(targets) > deps.candle_start_cap:
        raise ValueError("candle start cap exceeded before candle reads")
    features_by_symbol = {}
    unavailable = []
    provider_failures = 0
    for symbol in targets:
        candle_request = build_read_request(TossReadOperation.STOCK_CANDLES, token=deps.token,
            symbol=symbol, interval="1d", count=200, adjusted=True)
        counts["STOCK_CANDLES"] += 1
        outcome = query_read(candle_request, transport=deps.transport, clock=deps.clock)
        if not isinstance(outcome.value, CandlePageResponse) or not outcome.value.candles:
            unavailable.append(_unavailable(outcome.code) or QuantUnavailableCode.PROVIDER_FAILURE)
            provider_failures += 1
            continue
        if max(c.timestamp.date() for c in outcome.value.candles) != completed_session:
            unavailable.append(QuantUnavailableCode.CANDLE_SESSION_NOT_READY)
            continue
        candidate = QuantCandidateV1(symbol, identity.market, stocks[symbol].name,
                                     stocks[symbol].security_type, (), None, None, None)
        series = _series(candidate, outcome.value)
        features_by_symbol[symbol] = (series, compute_price_features(series, completed_session_date=completed_session))
    cards = []
    for group in groups:
        reference = features_by_symbol.get(group.reference_symbol)
        cohort = (group.reference_symbol,) + group.peer_symbols
        if reference is None or any(symbol not in features_by_symbol for symbol in cohort):
            continue
        standings = []
        for factor in reference[1].features.factors:
            rows = []
            for symbol in cohort:
                peer_factor = next(item for item in features_by_symbol[symbol][1].features.factors
                                   if item.name == factor.name)
                if peer_factor.status is FactorStatus.AVAILABLE:
                    rows.append((factor.name, symbol, peer_factor.raw_value, peer_factor.orientation, True))
            standings.extend(item for item in compute_peer_standing(rows)
                             if item.symbol == group.reference_symbol)
        cards.append(DecisionCardV1(group.reference_symbol, identity.market,
            stocks[group.reference_symbol].name, reference[1].features.factors, tuple(standings),
            build_reference_entry_zone(reference[0], reference[1].features),
            (() if reference[1].unavailable_code is None else (reference[1].unavailable_code,))))
    operation_counts = tuple(counts.items())
    codes = tuple(dict.fromkeys(unavailable + ([QuantUnavailableCode.PEER_UNCONFIRMED]
                                               if unconfirmed else [])))
    report = QuantRunReportV1(identity, tuple(sorted(cards, key=lambda card: card.symbol)), codes, operation_counts)
    outcome = "SUCCEEDED_WITH_CARDS" if cards else ("FAILED" if provider_failures == len(targets) else "SUCCEEDED_SILENT")
    symbol_unavailable = tuple((symbol, QuantUnavailableCode.PEER_UNCONFIRMED)
                               for symbol in unconfirmed)
    return RunResult(report, persist_terminal(store, report, outcome), symbol_unavailable)


def run_personal_peer_once(*, deps, holdings: CurrentHoldingsV1 | None,
                           completed_session: date, now, activation_enabled=False):
    if activation_enabled:
        raise ValueError("activation is not enabled")
    market = deps.profiles[0]
    identity = _product_identity(product=QuantProductKind.PERSONAL_PEER, market=market,
        completed_session=completed_session, completed_iso_week=None,
        profile_revision=_personal_authority_revision(deps, holdings),
        peer_registry_revision=deps.peer_registry.revision, report_revision=deps.report_revision)
    store = QuantReportStore(deps.root)
    replay = replay_terminal(store, identity)
    if replay is not None:
        return RunResult(replay.report, replay.receipt, (), replay.operation_counts)
    if holdings is None:
        return _persist_product_no_io(deps=deps, identity=identity,
            code=QuantUnavailableCode.PERSONAL_SOURCE_UNAVAILABLE, store=store)
    held = project_personal_symbols(holdings)
    symbols = tuple(symbol for symbol, profile in held if profile is market)
    return _run_peer_product(deps=deps, identity=identity, held_symbols=symbols,
        product=QuantProductKind.PERSONAL_PEER, completed_session=completed_session,
        store=store, as_of_date=now.date(), activation_enabled=activation_enabled)


def run_pension_efficiency_once(*, deps, active_revision: PensionHoldingsRevisionV1 | None,
                                completed_session: date, completed_week_final_proof: bool,
                                now, market=QuantMarketProfileV1.KR, activation_enabled=False):
    if activation_enabled:
        raise ValueError("activation is not enabled")
    if market is not QuantMarketProfileV1.KR:
        raise ValueError("pension efficiency is KR only")
    iso = completed_session.isocalendar()
    completed_week = f"{iso.year}-W{iso.week:02d}"
    profile_revision = _pension_authority_revision(deps, active_revision,
                                                   completed_week_final_proof)
    identity = _product_identity(product=QuantProductKind.PENSION_EFFICIENCY, market=market,
        completed_session=None, completed_iso_week=completed_week,
        profile_revision=profile_revision, peer_registry_revision=deps.peer_registry.revision,
        report_revision=deps.report_revision)
    store = QuantReportStore(deps.root)
    replay = replay_terminal(store, identity)
    if replay is not None:
        return RunResult(replay.report, replay.receipt, (), replay.operation_counts)
    if active_revision is None:
        return _persist_product_no_io(deps=deps, identity=identity,
            code=QuantUnavailableCode.PENSION_ACTIVE_REVISION_ABSENT, store=store)
    if completed_week_final_proof is not True:
        return _persist_product_no_io(deps=deps, identity=identity,
            code=QuantUnavailableCode.PENSION_WEEK_NOT_FINAL, store=store)
    symbols = tuple(symbol for symbol, profile in project_pension_symbols(active_revision).symbols
                    if profile is QuantMarketProfileV1.KR)
    return _run_peer_product(deps=deps, identity=identity, held_symbols=symbols,
        product=QuantProductKind.PENSION_EFFICIENCY, completed_session=completed_session,
        store=store, as_of_date=now.date(), activation_enabled=activation_enabled)
