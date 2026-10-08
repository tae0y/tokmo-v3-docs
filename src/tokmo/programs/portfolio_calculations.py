"""Pure canonical portfolio snapshot calculation over verified barrier materials."""

from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext
from enum import Enum
import hashlib
import re
from typing import Callable

from tokmo.values import canonical_decimal
from tokmo.values.analysis_barrier import AnalysisBarrierResultV1, MarketReadiness
from tokmo.values.current_holdings import classify_market
from tokmo.values.material_pack import JobKind, canonical_encode
from tokmo.values.portfolio_calculations import (
    CompletedExecutionFactV1, ConcentrationBasis, FxEvidenceEnvelopeV1, FxStatus,
    MarketPopulationStatusV1, MaterialEvidenceBindingV1,
    OpaqueTradeAccountQualityV1, PartitionPresentationV1, PartitionTotalsV1,
    PortfolioPresentationV1, PortfolioScope, PortfolioSnapshotBodyV1,
    PortfolioSnapshotEnvelopeV1, PositionCalcV1, PositionPresentationV1,
    PricingState, RatioState, RatioValueV1, ScopeKrwTotalsV1,
    ScopePresentationV1, ScopeUnresolved,
    SnapshotStatus, TopPositionV1, TradeFactsV1, TradeGlobalQualityState,
    TradeGlobalQualityV1, TradeMarketCompletenessV1, TradeMarketState,
)
from tokmo.values.account_scope_patterns import ACCOUNT_SCOPE_RE, PENSION_SCOPE_RE
from tokmo.values.session_gate import Market


FX_MAX_AGE_SECONDS_V1 = 600
PORTFOLIO_CONCENTRATION_TOP_N_V1 = 5
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ScopeResolver = Callable[[str], PortfolioScope | ScopeUnresolved]


class _InvalidMaterial(ValueError):
    def __init__(self, token: str):
        super().__init__(token)
        self.token = token


class _UnresolvedScopes(Exception):
    def __init__(self, count: int):
        super().__init__("unresolved scopes")
        self.count = count


def _utc6(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware datetime required")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _decimal(value, token="INVALID_DECIMAL") -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise _InvalidMaterial(token) from None
    if not result.is_finite() or canonical_decimal(result) != value:
        raise _InvalidMaterial(token)
    return result


def _hash_object(value) -> str:
    return hashlib.sha256(canonical_encode(value)).hexdigest()


def _material_bindings(barrier):
    return tuple(
        MaterialEvidenceBindingV1(
            job, material.pack_tick_id, material.captured_at,
            material.content_hash, material.file_sha256,
        )
        for job, material in sorted(barrier.materials.items(), key=lambda item: item[0].value)
    )


def _material_present(barrier, job, market):
    material = barrier.materials.get(job)
    return material is not None and market.value in material.market_partition


def _reference_at(barrier):
    if JobKind.HOLDINGS not in barrier.materials:
        if barrier.proceed_markets or not barrier.materials:
            raise _InvalidMaterial("HOLDINGS_MATERIAL_ABSENT")
        # Evidence-only disposition: when no market may proceed, verified
        # QUOTES/TRADES may supply reference_at solely for readiness/digest
        # evidence. They never authorize holdings, positions, or FX conversion.
    return max(material.captured_at for material in barrier.materials.values())


def _reference_object(barrier, bindings, reference_at):
    return {
        "v": 1,
        "domain": "tokmo-v3:portfolio-reference-material:v1",
        "tick_id": barrier.tick_id,
        "reference_at": _utc6(reference_at),
        "proceed_markets": sorted(market.value for market in barrier.proceed_markets),
        "markets": [{
            "market": market.value,
            "readiness": barrier.market_readiness[market].value,
            "stale_jobs": sorted(job.value for job in barrier.stale_jobs[market]),
            "proceed": market in barrier.proceed_markets,
            "holdings_material_present": _material_present(barrier, JobKind.HOLDINGS, market),
            "quotes_material_present": _material_present(barrier, JobKind.QUOTES, market),
            "trades_material_present": _material_present(barrier, JobKind.TRADES, market),
        } for market in (Market.KR, Market.US)],
        "material_bindings": [{
            "job": binding.job.value,
            "pack_tick_id": binding.pack_tick_id,
            "captured_at": _utc6(binding.captured_at),
            "content_hash": binding.content_hash,
            "file_sha256": binding.file_sha256,
        } for binding in bindings],
    }


def reference_material_digest_v1(barrier: AnalysisBarrierResultV1) -> str:
    bindings = _material_bindings(barrier)
    return _hash_object(_reference_object(barrier, bindings, _reference_at(barrier)))


def _fx_content_object(envelope):
    return {
        "v": 1, "domain": "tokmo-v3:fx-evidence-content:v1",
        "schema_version": envelope.schema_version, "source": envelope.source,
        "base_currency": envelope.base_currency, "quote_currency": envelope.quote_currency,
        "rate": envelope.rate, "observed_at": _utc6(envelope.observed_at),
        "acquired_at": _utc6(envelope.acquired_at), "valid_from": _utc6(envelope.valid_from),
        "valid_until": _utc6(envelope.valid_until),
        "max_age_seconds": envelope.max_age_seconds,
        "reference_at": _utc6(envelope.reference_at),
        "reference_material_digest": envelope.reference_material_digest,
    }


def fx_content_hash_v1(envelope: FxEvidenceEnvelopeV1) -> str:
    return _hash_object(_fx_content_object(envelope))


_FX_FIELD_TYPES = {
    "schema_version": int,
    "source": str,
    "base_currency": str,
    "quote_currency": str,
    "rate": str,
    "observed_at": datetime,
    "acquired_at": datetime,
    "valid_from": datetime,
    "valid_until": datetime,
    "max_age_seconds": int,
    "reference_at": datetime,
    "reference_material_digest": str,
    "content_hash": str,
}
_FX_TIME_FIELDS = (
    "observed_at", "acquired_at", "valid_from", "valid_until", "reference_at",
)
_FX_CLOSED_TYPES = {
    int: "INT", str: "STRING", dict: "MAPPING", float: "FLOAT",
    Decimal: "DECIMAL", bytes: "BYTES", datetime: "DATETIME",
    bool: "BOOL", type(None): "NONE",
}


def _prepare_fx_evidence(envelope):
    """Return an exact, semantic-safe typed envelope or no public evidence."""
    if envelope is None:
        return None
    if type(envelope) is not FxEvidenceEnvelopeV1:
        return None
    invalid_fields = []
    for name, expected in _FX_FIELD_TYPES.items():
        value = getattr(envelope, name)
        if type(value) is not expected:
            invalid_fields.append({
                "field": name,
                "actual_type": _FX_CLOSED_TYPES.get(type(value), "OTHER"),
            })
    if not invalid_fields:
        try:
            for name in _FX_TIME_FIELDS:
                value = getattr(envelope, name)
                if value.tzinfo is None or value.utcoffset() is None:
                    invalid_fields.append({"field": name, "actual_type": "NAIVE_DATETIME"})
            if not invalid_fields:
                envelope = replace(envelope, **{
                    name: getattr(envelope, name).astimezone(timezone.utc)
                    for name in _FX_TIME_FIELDS
                })
        except Exception:
            invalid_fields.append({"field": "datetime", "actual_type": "INVALID_DATETIME"})
    if invalid_fields:
        return None
    return envelope


def _validate_fx(envelope, reference_at, reference_digest):
    if envelope is None:
        return FxStatus.UNAVAILABLE, None, None
    try:
        rate = _decimal(envelope.rate, "INVALID_FX")
        times = (envelope.observed_at, envelope.acquired_at, envelope.valid_from,
                 envelope.valid_until, envelope.reference_at)
        if any(value.tzinfo is None or value.utcoffset() is None for value in times):
            raise ValueError
        if (envelope.schema_version != 1 or envelope.source != "toss-exchange-rate-v1"
                or envelope.base_currency != "USD" or envelope.quote_currency != "KRW"
                or rate <= 0 or envelope.max_age_seconds != FX_MAX_AGE_SECONDS_V1
                or envelope.reference_at != reference_at
                or envelope.reference_material_digest != reference_digest
                or not _HASH_RE.fullmatch(envelope.content_hash)
                or fx_content_hash_v1(envelope) != envelope.content_hash
                or not envelope.valid_from <= reference_at < envelope.valid_until
                or not envelope.observed_at <= envelope.acquired_at <= reference_at
                or reference_at - envelope.observed_at
                > timedelta(seconds=FX_MAX_AGE_SECONDS_V1)):
            raise ValueError
        digest = _hash_object({
            "v": 1, "domain": "tokmo-v3:fx-evidence-binding:v1",
            "content_hash": envelope.content_hash,
            "reference_at": _utc6(envelope.reference_at),
            "reference_material_digest": envelope.reference_material_digest,
        })
        return FxStatus.AVAILABLE, rate, digest
    except (AttributeError, TypeError, ValueError, _InvalidMaterial):
        return FxStatus.INVALID, None, None


def _parse_time(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _InvalidMaterial("INVALID_QUOTE")
    return parsed


def _quote_index(barrier):
    material = barrier.materials.get(JobKind.QUOTES)
    result = {}
    if material is None:
        return result
    markets = material.payload["markets"]
    for market_text, rows in markets.items():
        market = Market(market_text)
        expected = "KRW" if market is Market.KR else "USD"
        for row in rows:
            key = (market, row["symbol"])
            if key in result or row["currency_raw"] != expected:
                raise _InvalidMaterial("INVALID_QUOTE")
            price = _decimal(row["last_price"], "INVALID_QUOTE")
            if price < 0:
                raise _InvalidMaterial("INVALID_QUOTE")
            result[key] = (price, _parse_time(row["provider_timestamp"] or row["stored_at"]))
    return result


def _ratio(numerator, denominator, zero_state):
    if denominator > 0:
        return RatioValueV1(RatioState.DEFINED, numerator / denominator)
    if denominator == 0:
        return RatioValueV1(zero_state, None)
    raise _InvalidMaterial("INVALID_HOLDINGS_COST")


def _resolve_holdings(barrier, resolve_scope):
    material = barrier.materials.get(JobKind.HOLDINGS)
    if material is None:
        if barrier.proceed_markets:
            raise _InvalidMaterial("HOLDINGS_MATERIAL_ABSENT")
        return {}, {}, 0
    aggregate = {}
    resolved = {}
    unresolved = 0
    for account in material.payload["accounts"]:
        eligible = []
        for row in account["holdings"]:
            market = classify_market(row["market_raw"], row["market_field"])
            if market is None:
                raise _InvalidMaterial("INVALID_HOLDINGS_MARKET")
            if market in barrier.proceed_markets:
                eligible.append((market, row))
        if not eligible:
            continue
        raw_scope = account["account_scope"]
        if (not ACCOUNT_SCOPE_RE.fullmatch(raw_scope)
                and not PENSION_SCOPE_RE.fullmatch(raw_scope)):
            raise _InvalidMaterial("INVALID_ACCOUNT_SCOPE")
        scope = resolve_scope(raw_scope)
        if not isinstance(scope, PortfolioScope):
            unresolved += 1
            continue
        resolved[raw_scope] = scope
        for market, row in eligible:
            expected = "KRW" if market is Market.KR else "USD"
            if row["currency"] != expected or not row["symbol"]:
                raise _InvalidMaterial("INVALID_HOLDINGS_CURRENCY")
            quantity = _decimal(row["quantity"], "INVALID_HOLDINGS_COST")
            average = _decimal(row["avg_price"], "INVALID_HOLDINGS_COST")
            cost = quantity * average
            if quantity < 0 or average < 0 or cost < 0:
                raise _InvalidMaterial("INVALID_HOLDINGS_COST")
            key = (scope, market, row["symbol"], expected)
            old_q, old_cost = aggregate.get(key, (Decimal(0), Decimal(0)))
            aggregate[key] = (old_q + quantity, old_cost + cost)
    return aggregate, resolved, unresolved


def _base_positions(barrier, aggregate, quotes):
    if not aggregate:
        return []
    holdings_at = barrier.materials[JobKind.HOLDINGS].captured_at
    pension_provenance = barrier.materials[JobKind.HOLDINGS].payload.get(
        "pension_provenance"
    )
    result = []
    for (scope, market, symbol, currency), (quantity, total_cost) in sorted(
            aggregate.items(), key=lambda item: (
                item[0][0].value, item[0][1].value, item[0][2], item[0][3])):
        if quantity < 0 or total_cost < 0 or (quantity == 0 and total_cost != 0):
            raise _InvalidMaterial("INVALID_HOLDINGS_COST")
        average = total_cost / quantity if quantity else Decimal(0)
        quote_row = quotes.get((market, symbol))
        if JobKind.QUOTES in barrier.stale_jobs[market]:
            state, price, quote_at = PricingState.STALE_QUOTE, None, None
        elif quote_row is None:
            state, price, quote_at = PricingState.UNPRICED, None, None
        else:
            state, (price, quote_at) = PricingState.PRICED, quote_row
        if state is PricingState.PRICED:
            market_value = quantity * price
            priced_cost = total_cost
            pnl = market_value - priced_cost
            ratio = _ratio(pnl, priced_cost, RatioState.ZERO_COST_UNDEFINED)
        else:
            market_value = priced_cost = pnl = None
            ratio = RatioValueV1(RatioState.UNAVAILABLE_UNPRICED, None)
        result.append(PositionCalcV1(
            portfolio_scope=scope, market=market, symbol=symbol, currency=currency,
            quantity=quantity, average_cost=average,
            total_cost_basis_native=total_cost, current_price=price,
            priced_market_value_native=market_value, priced_cost_basis_native=priced_cost,
            unrealized_pnl_native=pnl, unrealized_return=ratio, pricing_state=state,
            currency_partition_market_value_weight=None,
            market_currency_partition_market_value_weight=None,
            currency_partition_total_cost_basis_weight=None,
            market_currency_partition_total_cost_basis_weight=None,
            scope_krw_priced_market_value_weight=None,
            scope_krw_total_cost_basis_weight=None,
            internal_total_cost_basis_krw=None, priced_market_value_krw=None,
            internal_priced_cost_basis_krw=None, unrealized_pnl_krw=None,
            fx_binding_digest=None, holdings_observed_at=holdings_at,
            quote_observed_at=quote_at,
            pension_freshness_class=(
                pension_provenance["freshness_class"]
                if scope is PortfolioScope.PENSION and pension_provenance else None
            ),
            pension_source_as_of=(
                _parse_time(pension_provenance["source_as_of"])
                if scope is PortfolioScope.PENSION and pension_provenance else None
            ),
        ))
    return result


def _native_weights(positions):
    currency_mv = {}
    currency_cost = {}
    partition_mv = {}
    partition_cost = {}
    for p in positions:
        ckey = (p.portfolio_scope, p.currency)
        pkey = (p.portfolio_scope, p.market, p.currency)
        currency_cost[ckey] = currency_cost.get(ckey, Decimal(0)) + p.total_cost_basis_native
        partition_cost[pkey] = partition_cost.get(pkey, Decimal(0)) + p.total_cost_basis_native
        if p.priced_market_value_native is not None:
            currency_mv[ckey] = currency_mv.get(ckey, Decimal(0)) + p.priced_market_value_native
            partition_mv[pkey] = partition_mv.get(pkey, Decimal(0)) + p.priced_market_value_native
    result = []
    for p in positions:
        ckey = (p.portfolio_scope, p.currency)
        pkey = (p.portfolio_scope, p.market, p.currency)
        mv = p.priced_market_value_native
        result.append(replace(
            p,
            currency_partition_market_value_weight=(
                mv / currency_mv[ckey] if mv is not None and currency_mv.get(ckey, 0) != 0 else None),
            market_currency_partition_market_value_weight=(
                mv / partition_mv[pkey] if mv is not None and partition_mv.get(pkey, 0) != 0 else None),
            currency_partition_total_cost_basis_weight=(
                p.total_cost_basis_native / currency_cost[ckey] if currency_cost[ckey] != 0 else None),
            market_currency_partition_total_cost_basis_weight=(
                p.total_cost_basis_native / partition_cost[pkey] if partition_cost[pkey] != 0 else None),
        ))
    return result


def _partition_totals(positions, fx_rate, fx_digest):
    groups = {}
    for position in positions:
        groups.setdefault((position.portfolio_scope, position.market, position.currency), []).append(position)
    totals = []
    for key, rows in sorted(groups.items(), key=lambda item: (
            item[0][0].value, item[0][1].value, item[0][2])):
        scope, market, currency = key
        priced = [row for row in rows if row.pricing_state is PricingState.PRICED]
        total_cost = sum((row.total_cost_basis_native for row in rows), Decimal(0))
        priced_cost = sum((row.priced_cost_basis_native for row in priced), Decimal(0))
        market_value = sum((row.priced_market_value_native for row in priced), Decimal(0))
        pnl = market_value - priced_cost
        gains = sum((max(row.unrealized_pnl_native, Decimal(0)) for row in priced), Decimal(0))
        losses = sum((min(row.unrealized_pnl_native, Decimal(0)) for row in priced), Decimal(0))
        conversion = Decimal(1) if currency == "KRW" else fx_rate
        binding = fx_digest if currency == "USD" and conversion is not None else None
        convert = lambda value: None if conversion is None else value * conversion
        unrealized_return_state = (
            RatioState.UNAVAILABLE_UNPRICED
            if not priced else RatioState.ZERO_COST_UNDEFINED
        )
        totals.append(PartitionTotalsV1(
            scope, market, currency, len(rows), len(priced), len(rows) - len(priced),
            sum((row.quantity for row in rows), Decimal(0)), total_cost, priced_cost,
            market_value, pnl, _ratio(pnl, priced_cost, unrealized_return_state),
            gains, losses, convert(total_cost), convert(priced_cost), convert(market_value),
            convert(pnl), convert(gains), convert(losses), binding,
            _ratio(priced_cost, total_cost, RatioState.ZERO_DENOMINATOR_UNDEFINED),
            sum(row.unrealized_pnl_native > 0 for row in priced),
            sum(row.unrealized_pnl_native < 0 for row in priced),
            sum(row.unrealized_pnl_native == 0 for row in priced),
        ))
    return totals


def _convert_positions(positions, fx_rate, fx_digest, scope_available):
    converted = []
    scope_mv = {}
    scope_cost = {}
    interim = []
    for p in positions:
        conversion = Decimal(1) if p.currency == "KRW" else fx_rate
        binding = fx_digest if p.currency == "USD" and conversion is not None else None
        conv = lambda value: None if value is None or conversion is None else value * conversion
        row = replace(
            p, internal_total_cost_basis_krw=conv(p.total_cost_basis_native),
            priced_market_value_krw=conv(p.priced_market_value_native),
            internal_priced_cost_basis_krw=conv(p.priced_cost_basis_native),
            unrealized_pnl_krw=conv(p.unrealized_pnl_native), fx_binding_digest=binding,
        )
        interim.append(row)
        if scope_available[p.portfolio_scope]:
            scope_cost[p.portfolio_scope] = scope_cost.get(p.portfolio_scope, Decimal(0)) + row.internal_total_cost_basis_krw
            if row.priced_market_value_krw is not None:
                scope_mv[p.portfolio_scope] = scope_mv.get(p.portfolio_scope, Decimal(0)) + row.priced_market_value_krw
    for row in interim:
        if not scope_available[row.portfolio_scope]:
            converted.append(replace(row, scope_krw_priced_market_value_weight=None,
                                     scope_krw_total_cost_basis_weight=None))
            continue
        mv_den = scope_mv.get(row.portfolio_scope, Decimal(0))
        cost_den = scope_cost.get(row.portfolio_scope, Decimal(0))
        converted.append(replace(
            row,
            scope_krw_priced_market_value_weight=(
                row.priced_market_value_krw / mv_den
                if row.priced_market_value_krw is not None and mv_den != 0 else None),
            scope_krw_total_cost_basis_weight=(
                row.internal_total_cost_basis_krw / cost_den if cost_den != 0 else None),
        ))
    return converted


def _scope_totals(positions, scopes, fx_status, scope_available):
    result = []
    for scope in sorted(scopes, key=lambda value: value.value):
        rows = [row for row in positions if row.portfolio_scope is scope]
        has_usd = any(row.currency == "USD" for row in rows)
        status = (FxStatus.NOT_REQUIRED_SINGLE_KRW if not has_usd else fx_status)
        if not scope_available[scope]:
            result.append(ScopeKrwTotalsV1(
                scope, status, None, None, None, None,
                RatioValueV1(RatioState.UNAVAILABLE_FX, None), None, None, (), (),
                PORTFOLIO_CONCENTRATION_TOP_N_V1,
            ))
            continue
        total_cost = sum((row.internal_total_cost_basis_krw for row in rows), Decimal(0))
        priced = [row for row in rows if row.priced_market_value_krw is not None]
        priced_cost = sum((row.internal_priced_cost_basis_krw for row in priced), Decimal(0))
        market_value = sum((row.priced_market_value_krw for row in priced), Decimal(0))
        pnl = market_value - priced_cost
        gains = sum((max(row.unrealized_pnl_krw, Decimal(0)) for row in priced), Decimal(0))
        losses = sum((min(row.unrealized_pnl_krw, Decimal(0)) for row in priced), Decimal(0))
        def top(basis):
            if basis is ConcentrationBasis.PRICED_MARKET_VALUE_KRW:
                candidates = [(row, row.priced_market_value_krw) for row in priced]
                denominator = market_value
            else:
                candidates = [(row, row.internal_total_cost_basis_krw) for row in rows]
                denominator = total_cost
            candidates.sort(key=lambda item: (-item[1], item[0].market.value,
                                               item[0].symbol, item[0].currency))
            if denominator == 0:
                return ()
            return tuple(TopPositionV1(
                scope, row.market, row.symbol, row.currency, basis, value,
                value / denominator,
            ) for row, value in candidates[:PORTFOLIO_CONCENTRATION_TOP_N_V1])
        result.append(ScopeKrwTotalsV1(
            scope, status, total_cost, priced_cost, market_value, pnl,
            _ratio(pnl, priced_cost, RatioState.ZERO_COST_UNDEFINED), gains, losses,
            top(ConcentrationBasis.PRICED_MARKET_VALUE_KRW),
            top(ConcentrationBasis.TOTAL_COST_BASIS_KRW),
            PORTFOLIO_CONCENTRATION_TOP_N_V1,
        ))
    return result


def project_portfolio_presentation(
    body: PortfolioSnapshotBodyV1,
) -> PortfolioPresentationV1:
    """Project the canonical audit body into the only user/model-facing DTO."""
    positions = tuple(
        PositionPresentationV1(
            portfolio_scope=row.portfolio_scope,
            market=row.market,
            symbol=row.symbol,
            currency=row.currency,
            quantity=row.quantity,
            average_cost_native=row.average_cost,
            current_price_native=row.current_price,
            invested_cost_basis_native=row.total_cost_basis_native,
            evaluated_market_value_native=row.priced_market_value_native,
            evaluated_market_value_krw=(
                row.priced_market_value_krw if row.currency == "USD" else None),
            unrealized_pnl_native=row.unrealized_pnl_native,
            unrealized_pnl_krw=(
                row.unrealized_pnl_krw if row.currency == "USD" else None),
            unrealized_return=row.unrealized_return,
            pricing_state=row.pricing_state,
            fx_binding_digest=(
                row.fx_binding_digest
                if row.currency == "USD"
                and (row.priced_market_value_krw is not None
                     or row.unrealized_pnl_krw is not None)
                else None
            ),
        )
        for row in body.positions
    )
    partition_by_scope = {}
    for row in body.partition_totals:
        partition_by_scope.setdefault(row.portfolio_scope, []).append(
            PartitionPresentationV1(
                portfolio_scope=row.portfolio_scope,
                market=row.market,
                currency=row.currency,
                total_cost_basis_native=row.total_cost_basis_native,
                priced_cost_basis_native=row.priced_cost_basis_native,
                evaluated_market_value_native=row.priced_market_value_native,
                evaluated_market_value_krw=(
                    row.priced_market_value_krw if row.currency == "USD" else None),
                unrealized_pnl_native=row.unrealized_pnl_native,
                unrealized_pnl_krw=(
                    row.unrealized_pnl_krw if row.currency == "USD" else None),
                unrealized_return=row.unrealized_return,
                priced_cost_basis_coverage_ratio=row.priced_cost_basis_coverage_ratio,
                fx_binding_digest=(
                    row.fx_binding_digest
                    if row.currency == "USD"
                    and (row.priced_market_value_krw is not None
                         or row.unrealized_pnl_krw is not None)
                    else None
                ),
            )
        )
    # m6: carry freshness_class per scope from scope_krw_totals.
    scope_freshness = {}
    for row in body.scope_krw_totals:
        scope_freshness[row.portfolio_scope] = getattr(row, "pension_freshness_class", None)
    scopes = tuple(
        ScopePresentationV1(
            portfolio_scope=row.portfolio_scope,
            fx_status=row.fx_status,
            evaluated_market_value_krw=row.priced_market_value_krw,
            unrealized_pnl_krw=row.unrealized_pnl_krw,
            unrealized_return=row.unrealized_return,
            partitions=tuple(partition_by_scope.get(row.portfolio_scope, ())),
            freshness_class=scope_freshness.get(row.portfolio_scope),
        )
        for row in body.scope_krw_totals
    )
    return PortfolioPresentationV1(
        positions=positions, scopes=scopes, material_bindings=body.material_bindings)


def _trade_facts(barrier, resolved, resolve_scope):
    material = barrier.materials.get(JobKind.TRADES)
    accounts = () if material is None else material.payload["accounts"]
    pagination = False if material is None else material.payload["pagination_pending"]
    qualities = tuple(sorted((OpaqueTradeAccountQualityV1(
        len(account["duplicates"]), len(account["quarantine"]), len(account["incomplete"])
    ) for account in accounts), key=lambda row: (
        row.duplicate_count, row.quarantine_count, row.incomplete_count)))
    duplicate_count = sum(row.duplicate_count for row in qualities)
    quarantine_count = sum(row.quarantine_count for row in qualities)
    incomplete_count = sum(row.incomplete_count for row in qualities)
    partial = duplicate_count + quarantine_count + incomplete_count > 0
    if pagination and partial:
        quality_state = TradeGlobalQualityState.PAGINATION_PENDING_AND_UNATTRIBUTED_PARTIAL
    elif pagination:
        quality_state = TradeGlobalQualityState.PAGINATION_PENDING
    elif partial:
        quality_state = TradeGlobalQualityState.UNATTRIBUTED_PARTIAL
    else:
        quality_state = TradeGlobalQualityState.COMPLETE
    inserted = {market: [] for market in Market}
    for account in accounts:
        for row in account["inserted"]:
            inserted[Market(row["classified_market"])].append((account["account_scope"], row))
    market_rows = []
    projected = []
    unresolved_scopes = set()
    for market in (Market.KR, Market.US):
        present = _material_present(barrier, JobKind.TRADES, market)
        stale = JobKind.TRADES in barrier.stale_jobs[market]
        if not present:
            state = TradeMarketState.ABSENT
        elif market not in barrier.proceed_markets:
            state = TradeMarketState.EXCLUDED_NON_PROCEED
        elif stale:
            state = TradeMarketState.STALE
        elif inserted[market]:
            state = TradeMarketState.COMPLETE_WITH_EXECUTIONS
        else:
            state = TradeMarketState.COMPLETE_EMPTY
        eligible = state in (TradeMarketState.COMPLETE_EMPTY,
                             TradeMarketState.COMPLETE_WITH_EXECUTIONS)
        if eligible:
            for raw_scope, row in inserted[market]:
                scope = resolved.get(raw_scope)
                if scope is None:
                    if raw_scope in unresolved_scopes:
                        continue
                    if not ACCOUNT_SCOPE_RE.fullmatch(raw_scope):
                        raise _InvalidMaterial("INVALID_ACCOUNT_SCOPE")
                    scope = resolve_scope(raw_scope)
                    if not isinstance(scope, PortfolioScope):
                        unresolved_scopes.add(raw_scope)
                        continue
                    resolved[raw_scope] = scope
                key_digest = _hash_object({
                    "v": 1, "domain": "tokmo-v3:trade-source-record-key:v1",
                    "source_record_key": row["source_record_key"],
                })
                projected.append(CompletedExecutionFactV1(
                    scope, key_digest, row["trade_date"], row["transaction_type_raw"],
                    row["instrument_raw"], _decimal(row["quantity"]),
                    _decimal(row["unit_price"]), _decimal(row["amount"]),
                    row["medium_raw"], row["currency_raw"], market,
                    _parse_time(row["filled_at"]),
                ))
        market_rows.append(TradeMarketCompletenessV1(
            market, barrier.market_readiness[market], eligible, state, present, stale,
            len(inserted[market]), len(inserted[market]) if eligible else 0,
        ))
    if unresolved_scopes:
        raise _UnresolvedScopes(len(unresolved_scopes))
    projected.sort(key=lambda row: (
        row.portfolio_scope.value, row.classified_market.value, row.trade_date,
        row.source_record_key_digest))
    return TradeFactsV1(
        tuple(market_rows), TradeGlobalQualityV1(
            quality_state, material is not None, pagination, qualities,
            duplicate_count, quarantine_count, incomplete_count,
        ), tuple(projected),
    )


def _semantic(value):
    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, datetime):
        return _utc6(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _semantic(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (tuple, list)):
        return [_semantic(item) for item in value]
    raise TypeError(f"unsupported semantic type: {type(value).__name__}")


def compute_portfolio_snapshot(
        barrier: AnalysisBarrierResultV1, *, resolve_scope: ScopeResolver,
        fx_evidence: FxEvidenceEnvelopeV1 | None) -> PortfolioSnapshotEnvelopeV1:
    if not isinstance(barrier, AnalysisBarrierResultV1) or not callable(resolve_scope):
        raise TypeError("typed barrier and resolver required")
    try:
        with localcontext() as context:
            context.prec = 50
            context.traps[InvalidOperation] = True
            context.traps[DivisionByZero] = True
            bindings = _material_bindings(barrier)
            reference_at = _reference_at(barrier)
            reference_digest = _hash_object(_reference_object(
                barrier, bindings, reference_at))
            quotes = _quote_index(barrier)
            aggregate, resolved, unresolved = _resolve_holdings(barrier, resolve_scope)
            if unresolved:
                return PortfolioSnapshotEnvelopeV1(
                    SnapshotStatus.SCOPE_RESOLUTION_INCOMPLETE, None, None,
                    unresolved, None,
                )
            positions = _native_weights(_base_positions(barrier, aggregate, quotes))
            typed_fx_evidence = _prepare_fx_evidence(fx_evidence)
            if fx_evidence is None:
                fx_status, fx_rate, fx_digest = FxStatus.UNAVAILABLE, None, None
            elif typed_fx_evidence is None:
                fx_status, fx_rate, fx_digest = FxStatus.INVALID, None, None
            else:
                fx_status, fx_rate, fx_digest = _validate_fx(
                    typed_fx_evidence, reference_at, reference_digest)

            scopes = {position.portfolio_scope for position in positions}
            scope_available = {
                scope: (not any(p.currency == "USD" for p in positions
                                if p.portfolio_scope is scope)
                        or fx_status is FxStatus.AVAILABLE)
                for scope in scopes
            }
            positions = _convert_positions(positions, fx_rate, fx_digest, scope_available)
            partitions = _partition_totals(positions, fx_rate, fx_digest)
            scope_totals = _scope_totals(positions, scopes, fx_status, scope_available)
            try:
                trade_facts = _trade_facts(barrier, resolved, resolve_scope)
            except _UnresolvedScopes as error:
                return PortfolioSnapshotEnvelopeV1(
                    SnapshotStatus.SCOPE_RESOLUTION_INCOMPLETE, None, None,
                    error.count, None)
            population = tuple(MarketPopulationStatusV1(
                market, barrier.market_readiness[market], market in barrier.proceed_markets,
                tuple(sorted(barrier.stale_jobs[market], key=lambda job: job.value)),
                _material_present(barrier, JobKind.HOLDINGS, market),
                _material_present(barrier, JobKind.QUOTES, market),
                _material_present(barrier, JobKind.TRADES, market),
                sum(position.market is market for position in positions),
                sum(row.classified_market is market for row in trade_facts.completed_executions),
            ) for market in (Market.KR, Market.US))
            body = PortfolioSnapshotBodyV1(
                1, barrier.tick_id, reference_at, population, tuple(positions),
                tuple(partitions), tuple(scope_totals), trade_facts, bindings,
                typed_fx_evidence, fx_digest,
            )
            semantic_object = {
                "v": 1, "domain": "tokmo-v3:portfolio-snapshot-body:v1",
                "body": _semantic(body),
            }
            digest = _hash_object(semantic_object)
            return PortfolioSnapshotEnvelopeV1(
                SnapshotStatus.COMPLETE, body, digest, 0, None)
    except _InvalidMaterial as error:
        return PortfolioSnapshotEnvelopeV1(
            SnapshotStatus.MATERIAL_INVALID, None, None, 0, error.token)
