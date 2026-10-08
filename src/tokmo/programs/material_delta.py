"""Pure, deterministic material delta builder.

This module extracts delta detection from ``candidate_builder.py``,
producing typed :class:`~tokmo.values.analysis_admission.MaterialDeltaV1`
objects plus proposed baseline/history advances. It does not persist
advances — persistence remains atomic with the admitted/rejected tick
manifest exactly as Slice E prevents a baseline from moving ahead of
durable candidate state.

``build_candidates`` in ``candidate_builder.py`` delegates to this builder
and projects admitted deltas back into ``CandidateEnvelopeV1`` rows,
preserving exact Slice E semantics under the default v1 policy.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
from types import MappingProxyType
from typing import Mapping

from tokmo.values import canonical_decimal
from tokmo.values.analysis_admission import MaterialDeltaV1
from tokmo.values.analysis_barrier import (
    AnalysisBarrierResultV1, MarketReadiness, VerifiedMaterialV1,
)
from tokmo.values.analysis_contracts import (
    ChangeBasis,
    EvidenceRefV1,
    FreshnessState,
    CRITICAL_INCIDENT_KINDS_V1,
    MaterialKindV1,
    NumericStateV1,
    QUOTE_WATCH_THRESHOLD_RATIO,
    QuoteTriggerKindV1,
    TriggerHorizonV1,
    TrustClass,
    evidence_id_for,
    subject_id_for,
)
from tokmo.values.material_pack import JobKind, canonical_encode
from tokmo.values.current_holdings import classify_market
from tokmo.values.session_gate import Market
from tokmo.values.ops_kernel import CandidateAssignment


# Threshold constants (same values as candidate_builder.py — the single
# owner of these literals is now event_admission.py, but the delta builder
# needs them for numeric-state computation which is pre-admission logic).
QUOTE_MOVE_THRESHOLD_RATIO = Decimal("0.005")
QUOTE_CUMULATIVE_THRESHOLD_RATIO = Decimal("0.01")
QUOTE_DRIFT_TICK_THRESHOLD = 3
QUOTE_MOVE_NOISE_FLOOR_RATIO = Decimal("0.001")
_RATIO_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True)
class BaselineAdvanceV1:
    kind: MaterialKindV1
    subject_id: str
    fields: Mapping[str, object]
    account_scope: str | None
    source_tick_id: int
    baseline_hash: str

    def __post_init__(self):
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))


@dataclass(frozen=True)
class HistoryAdvanceV1:
    subject_id: str
    tick_id: int
    observed_at: datetime
    last_price: str


@dataclass(frozen=True)
class MaterialDeltaResultV1:
    material_binding_digest: str
    deltas: tuple[MaterialDeltaV1, ...]
    baseline_advances: tuple[BaselineAdvanceV1, ...]
    history_advances: tuple[HistoryAdvanceV1, ...]
    numeric_state_counts: Mapping[str, int] = MappingProxyType({})
    trigger_fire_counts: Mapping[str, int] = MappingProxyType({})
    mask_digest: str | None = None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def material_binding_digest(barrier: AnalysisBarrierResultV1) -> str:
    payload = {
        "v": 1,
        "domain": "tokmo-v3:material-binding",
        "tick_id": barrier.tick_id,
        **({} if barrier.mask_digest is None
           else {"mask_digest": barrier.mask_digest}),
        "material_ticks": {
            job.value: barrier.material_ticks.get(job)
            for job in sorted(JobKind, key=lambda item: item.value)
        },
        "packs": [{
            "job": material.job.value,
            "pack_tick_id": material.pack_tick_id,
            "captured_at": _iso(material.captured_at),
            "content_hash": material.content_hash,
            "file_sha256": material.file_sha256,
        } for material in sorted(barrier.materials.values(), key=lambda item: item.job.value)],
    }
    return hashlib.sha256(canonical_encode(payload)).hexdigest()


def _parse_time(value, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("material timestamp must be aware")
    return parsed


def _evidence(kind: MaterialKindV1, source: str, record: str, row,
              material: VerifiedMaterialV1, observed: datetime) -> EvidenceRefV1:
    content_hash = hashlib.sha256(canonical_encode({
        "v": 1, "domain": "tokmo-v3:evidence-content",
        "row": row, "pack": material.content_hash,
    })).hexdigest()
    return EvidenceRefV1(
        evidence_id=evidence_id_for(kind, source, record, content_hash),
        kind=kind, source=source, source_record_key=record,
        observed_at=observed, fetched_at=material.captured_at,
        stored_at=material.captured_at, acquired_at=material.captured_at,
        content_hash=content_hash, schema_version=1, parser_version="material-pack-v1",
        trust_class=TrustClass.OFFICIAL_PROVIDER, freshness_state=FreshnessState.FRESH,
        expires_at=material.captured_at + timedelta(seconds=600),
    )


def _advance(kind, subject, fields, account_scope, tick):
    return BaselineAdvanceV1(
        kind, subject, fields, account_scope, tick,
        hashlib.sha256(canonical_encode(fields)).hexdigest(),
    )


def _ratio(current: str, prior: str) -> str:
    base = Decimal(prior)
    if base == 0:
        return "0"
    value = ((Decimal(current) - base) / base).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP,
    )
    return canonical_decimal(value)


def _round6(value: Decimal) -> Decimal:
    return value.quantize(_RATIO_QUANTUM, rounding=ROUND_HALF_UP)


def _pair_delta(current, prior):
    if prior is None:
        return None, None
    elapsed_min = Decimal(str(
        (current["observed_at"] - prior["observed_at"]).total_seconds()
    )) / Decimal(60)
    if elapsed_min <= 0:
        return None, None
    base = Decimal(prior["last_price"])
    if base == 0:
        return None, None
    return (Decimal(current["last_price"]) - base) / base, elapsed_min


def _tick_ratio(ring):
    if len(ring) < 2:
        return None
    base = Decimal(ring[1]["last_price"])
    if base == 0:
        return None
    return _round6((Decimal(ring[0]["last_price"]) - base) / base)


def _horizon_delta(ring, minutes):
    now_observed = ring[0]["observed_at"]
    threshold = now_observed - timedelta(minutes=minutes)
    eligible = [
        row for row in ring
        if row["observed_at"] <= threshold and row["observed_at"] < now_observed
    ]
    if not eligible:
        return None, None
    reference = max(eligible, key=lambda row: row["observed_at"])
    base = Decimal(reference["last_price"])
    if base == 0:
        return None, None
    return _round6((Decimal(ring[0]["last_price"]) - base) / base), reference


def _drift_streak(ring):
    direction = 0
    streak = 0
    for index in range(len(ring) - 1):
        delta, _ = _pair_delta(ring[index], ring[index + 1])
        if delta is None or delta == 0:
            break
        sign = 1 if delta > 0 else -1
        if direction == 0:
            direction = sign
        elif sign != direction:
            break
        streak += 1
    return streak * direction


_NO_RING = {
    "state": NumericStateV1.DATA_INCOMPLETE, "tick_ratio": None,
    "delta_5": None, "delta_15": None, "delta_30": None,
    "ref_15": None, "ref_30": None, "velocity": None, "acceleration": None,
    "reversal": False, "streak": 0,
}


def _quote_numeric(ring):
    if not ring:
        return dict(_NO_RING)
    prev = ring[1] if len(ring) > 1 else None
    older = ring[2] if len(ring) > 2 else None
    delta_now, elapsed_now = _pair_delta(ring[0], prev)
    delta_prev, elapsed_prev = _pair_delta(prev, older) if prev is not None else (None, None)
    velocity = None if delta_now is None else _round6(delta_now / elapsed_now)
    prior_velocity = None if delta_prev is None else _round6(delta_prev / elapsed_prev)
    acceleration = (
        None if velocity is None or prior_velocity is None
        else _round6((velocity - prior_velocity) / elapsed_prev)
    )
    reversal = (
        delta_now is not None and delta_prev is not None
        and abs(delta_now) >= QUOTE_MOVE_NOISE_FLOOR_RATIO
        and abs(delta_prev) >= QUOTE_MOVE_NOISE_FLOOR_RATIO
        and (delta_now > 0) != (delta_prev > 0)
    )
    streak = _drift_streak(ring)
    tick_ratio = _tick_ratio(ring)
    delta_5, _ = _horizon_delta(ring, 5)
    delta_15, ref_15 = _horizon_delta(ring, 15)
    delta_30, ref_30 = _horizon_delta(ring, 30)
    if len(ring) < 2:
        state = NumericStateV1.DATA_INCOMPLETE
    elif abs(streak) >= QUOTE_DRIFT_TICK_THRESHOLD:
        state = NumericStateV1.PERSISTENT_DRIFT
    elif reversal:
        state = NumericStateV1.REVERSAL
    elif (
        (tick_ratio is not None and abs(tick_ratio) >= QUOTE_MOVE_THRESHOLD_RATIO)
        or any(
            delta is not None and abs(delta) >= QUOTE_CUMULATIVE_THRESHOLD_RATIO
            for delta in (delta_15, delta_30)
        )
    ):
        state = NumericStateV1.MATERIAL_MOVE
    elif tick_ratio is not None and abs(tick_ratio) >= QUOTE_WATCH_THRESHOLD_RATIO:
        state = NumericStateV1.WATCH
    else:
        state = NumericStateV1.NORMAL
    return {
        "state": state, "tick_ratio": tick_ratio,
        "delta_5": delta_5, "delta_15": delta_15, "delta_30": delta_30,
        "ref_15": ref_15, "ref_30": ref_30,
        "velocity": velocity, "acceleration": acceleration,
        "reversal": reversal, "streak": streak,
    }


def _quote_trigger(first_observation, ring, numeric, previous):
    if first_observation:
        return QuoteTriggerKindV1.FIRST_OBSERVATION, TriggerHorizonV1.NONE, None
    if numeric["reversal"]:
        return (
            QuoteTriggerKindV1.REVERSAL, TriggerHorizonV1.TICK,
            ring[1]["last_price"],
        )
    if abs(numeric["streak"]) == QUOTE_DRIFT_TICK_THRESHOLD:
        return (
            QuoteTriggerKindV1.PERSISTENT_DRIFT, TriggerHorizonV1.STREAK,
            ring[QUOTE_DRIFT_TICK_THRESHOLD]["last_price"],
        )
    for delta_key, ref_key, horizon in (
        ("delta_15", "ref_15", TriggerHorizonV1.HORIZON_15M),
        ("delta_30", "ref_30", TriggerHorizonV1.HORIZON_30M),
    ):
        current_delta = numeric[delta_key]
        prior_delta = previous[delta_key]
        if (
            current_delta is not None
            and abs(current_delta) >= QUOTE_CUMULATIVE_THRESHOLD_RATIO
            and (
                prior_delta is None
                or abs(prior_delta) < QUOTE_CUMULATIVE_THRESHOLD_RATIO
            )
        ):
            return (
                QuoteTriggerKindV1.CUMULATIVE_DRIFT, horizon,
                numeric[ref_key]["last_price"],
            )
    if (
        numeric["tick_ratio"] is not None
        and abs(numeric["tick_ratio"]) >= QUOTE_MOVE_THRESHOLD_RATIO
    ):
        return (
            QuoteTriggerKindV1.THRESHOLD_MOVE, TriggerHorizonV1.TICK,
            ring[1]["last_price"],
        )
    if numeric["state"] is not previous["state"]:
        return (
            QuoteTriggerKindV1.STATE_TRANSITION, TriggerHorizonV1.TICK,
            ring[1]["last_price"] if len(ring) > 1 else None,
        )
    return None


def _decimal_text(value) -> str | None:
    return None if value is None else canonical_decimal(value)


MANIFEST_MAX_CANDIDATES_PER_TICK = 512


def quote_history_subjects(barrier: AnalysisBarrierResultV1) -> tuple[str, ...]:
    quotes = barrier.materials.get(JobKind.QUOTES)
    if quotes is None:
        return ()
    return tuple(sorted({
        subject_id_for(MaterialKindV1.QUOTE_STATE, [market, row["symbol"]])
        for market, rows in quotes.payload["markets"].items()
        for row in rows
    }))


def build_material_deltas(
    barrier: AnalysisBarrierResultV1,
    *,
    baselines: Mapping[tuple[MaterialKindV1, str], BaselineAdvanceV1],
    history: Mapping[str, tuple] | None = None,
    now: datetime,
    incident_rows=(),
    resolve_scope=None,  # N5: AccountScopeResolver (required for production)
) -> MaterialDeltaResultV1:
    """Build typed material deltas from the Slice D barrier.

    Pure and deterministic over barrier/baseline/history/incident inputs.
    Returns deltas plus proposed baseline/history advances but does not
    persist them.
    """
    history = {} if history is None else history
    deltas: list[MaterialDeltaV1] = []
    advances: list[BaselineAdvanceV1] = []
    history_advances: list[HistoryAdvanceV1] = []
    numeric_state_counts: dict[str, int] = {}
    trigger_fire_counts: dict[str, int] = {}
    proceed = {market.value for market in barrier.proceed_markets}
    held_symbols = set()
    # N5: key by (PortfolioScope, market, symbol) for scope separation
    held_quantity: dict[tuple, Decimal] = {}

    # m4: resolve_scope is required — fail loud rather than silently producing
    # empty held_symbols and UNHELD/None money_impact for every QUOTE_STATE.
    if resolve_scope is None:
        raise ValueError("resolve_scope is required")

    binding = material_binding_digest(barrier)

    # -- Holdings
    holdings = barrier.materials.get(JobKind.HOLDINGS)
    if holdings is not None:
        for account in holdings.payload["accounts"]:
            # N5: resolve scope
            if resolve_scope is not None:
                scope = resolve_scope(account["account_scope"])
            else:
                scope = None  # fail-closed: unknown scope
            for row in account["holdings"]:
                classified = classify_market(row["market_raw"], row["market_field"])
                if classified is None:
                    continue
                market = classified.value
                if market not in proceed:
                    continue
                # N5: key by (scope, market, symbol)
                from tokmo.values.portfolio_scope import PortfolioScope
                if isinstance(scope, PortfolioScope):
                    key = (scope, market, row["symbol"])
                    held_symbols.add((scope, market, row["symbol"]))
                    held_quantity[key] = (
                        held_quantity.get(key, Decimal(0))
                        + Decimal(row["quantity"])
                    )
                subject = subject_id_for(
                    MaterialKindV1.HOLDINGS_CHANGE,
                    [market, account["account_scope"], row["symbol"]],
                )
                fields = {
                    "market": market, "account_scope": account["account_scope"],
                    "symbol": row["symbol"], "account_type_label": account["account_type_label"],
                    "ordinal_at_observation": account["ordinal"], "currency": row["currency"],
                    "quantity": row["quantity"], "avg_price": row["avg_price"], "present": True,
                }
                prior = baselines.get((MaterialKindV1.HOLDINGS_CHANGE, subject))
                advances.append(_advance(
                    MaterialKindV1.HOLDINGS_CHANGE, subject, fields,
                    account["account_scope"], barrier.tick_id,
                ))
                if prior is not None and (
                    prior.fields["quantity"] != row["quantity"]
                    or prior.fields["avg_price"] != row["avg_price"]
                ):
                    change = ("QUANTITY_CHANGED" if prior.fields["quantity"] != row["quantity"]
                              else "AVG_PRICE_CHANGED")
                    evidence = _evidence(
                        MaterialKindV1.HOLDINGS_CHANGE, "toss-holdings-v1",
                        f"{market}:{row['symbol']}", row, holdings, holdings.captured_at,
                    )
                    current_fields = {
                        "market": market, "account_type_label": account["account_type_label"],
                        "portfolio_relation": "HELD",
                        "ordinal": account["ordinal"], "symbol": row["symbol"],
                        "change_kind": change, "baseline_tick_id": prior.source_tick_id,
                        "current_quantity": row["quantity"], "current_avg_price": row["avg_price"],
                        "baseline_quantity": prior.fields["quantity"],
                        "baseline_avg_price": prior.fields["avg_price"],
                        "quantity_delta_ratio": _ratio(row["quantity"], prior.fields["quantity"]),
                        "currency": row["currency"],
                    }
                    baseline_fields = {
                        "market": market,
                        "symbol": row["symbol"],
                        "quantity": prior.fields["quantity"],
                        "avg_price": prior.fields["avg_price"],
                    }
                    deltas.append(MaterialDeltaV1(
                        subject_id=subject,
                        material_kind=MaterialKindV1.HOLDINGS_CHANGE,
                        market=market, symbol=row["symbol"],
                        tick_id=barrier.tick_id,
                        material_binding_digest=binding,
                        evidence_refs=(evidence,),
                        current_fields=current_fields,
                        baseline_fields=baseline_fields,
                        change_basis=ChangeBasis.BASELINE_DELTA,
                        freshness_state=FreshnessState.FRESH,
                        created_at=now,
                    ))

    # -- Quotes
    quotes = barrier.materials.get(JobKind.QUOTES)
    if quotes is not None:
        for market, rows in quotes.payload["markets"].items():
            if market not in proceed:
                continue
            for row in rows:
                subject = subject_id_for(MaterialKindV1.QUOTE_STATE, [market, row["symbol"]])
                prior = baselines.get((MaterialKindV1.QUOTE_STATE, subject))
                baseline_fields = {"market": market, "symbol": row["symbol"], "last_price": row["last_price"]}
                advances.append(_advance(MaterialKindV1.QUOTE_STATE, subject, baseline_fields, None,
                                         barrier.tick_id))
                observed = _parse_time(
                    row["provider_timestamp"], _parse_time(row["fetched_at"], now),
                )
                prior_ring = tuple(sorted(
                    (
                        item for item in history.get(subject, ())
                        if item["tick_id"] < barrier.tick_id
                    ),
                    key=lambda item: item["tick_id"], reverse=True,
                ))[:7]
                ring = ({
                    "tick_id": barrier.tick_id, "observed_at": observed,
                    "last_price": row["last_price"],
                },) + prior_ring
                numeric = _quote_numeric(ring)
                previous = _quote_numeric(prior_ring)
                state_value = numeric["state"].value
                numeric_state_counts[state_value] = (
                    numeric_state_counts.get(state_value, 0) + 1)
                history_advances.append(HistoryAdvanceV1(
                    subject, barrier.tick_id, observed, row["last_price"],
                ))
                first_observation = prior is None or (
                    prior.source_tick_id == barrier.tick_id and not prior_ring
                )
                trigger = _quote_trigger(first_observation, ring, numeric, previous)
                if trigger is None:
                    continue
                trigger_kind, trigger_horizon, reference_price = trigger
                trigger_fire_counts[trigger_kind.value] = (
                    trigger_fire_counts.get(trigger_kind.value, 0) + 1)
                money_impact = None
                if (
                    barrier.materials.get(JobKind.HOLDINGS) is not None
                    and reference_price is not None
                ):
                    # N5: find all scopes that hold this (market, symbol)
                    scopes_for_symbol = {
                        s for (s, m, sym) in held_symbols
                        if m == market and sym == row["symbol"]
                    }
                    if len(scopes_for_symbol) == 1:
                        scope_key = scopes_for_symbol.pop()
                        key = (scope_key, market, row["symbol"])
                        if key in held_quantity:
                            money_impact = canonical_decimal((
                                held_quantity[key]
                                * (Decimal(row["last_price"]) - Decimal(reference_price))
                            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                    # cross-scope (>1): money_impact stays None
                evidence = _evidence(
                    MaterialKindV1.QUOTE_STATE, "toss-current-price-v1",
                    f"{market}:{row['symbol']}", row, quotes, observed,
                )
                current_fields = {
                    "market": market, "symbol": row["symbol"], "last_price": row["last_price"],
                    "portfolio_relation": (
                        "HELD" if any(
                            sym == row["symbol"] and mkt == market
                            for (_, mkt, sym) in held_symbols
                        ) else "UNHELD"
                    ),
                    "currency_raw": row["currency_raw"],
                    "provider_timestamp": row["provider_timestamp"],
                    "fetched_at": _parse_time(row["fetched_at"], now),
                    "change_basis": (ChangeBasis.BASELINE_DELTA.value if prior
                                     else ChangeBasis.CURRENT_FACT_ONLY.value),
                    "baseline_price": prior.fields["last_price"] if prior else None,
                    "price_delta_ratio": _ratio(row["last_price"], prior.fields["last_price"])
                    if prior else None,
                    "numeric_state": numeric["state"].value,
                    "trigger_kind": trigger_kind.value,
                    "trigger_reference_price": (
                        None if reference_price is None
                        else canonical_decimal(Decimal(reference_price))
                    ),
                    "trigger_horizon": trigger_horizon.value,
                    "price_delta_ratio_5m": _decimal_text(numeric["delta_5"]),
                    "price_delta_ratio_15m": _decimal_text(numeric["delta_15"]),
                    "price_delta_ratio_30m": _decimal_text(numeric["delta_30"]),
                    "velocity_per_min": _decimal_text(numeric["velocity"]),
                    "acceleration_per_min2": _decimal_text(numeric["acceleration"]),
                    "reversal": numeric["reversal"],
                    "persistent_drift_ticks": numeric["streak"],
                    "money_impact": money_impact,
                }
                deltas.append(MaterialDeltaV1(
                    subject_id=subject,
                    material_kind=MaterialKindV1.QUOTE_STATE,
                    market=market, symbol=row["symbol"],
                    tick_id=barrier.tick_id,
                    material_binding_digest=binding,
                    evidence_refs=(evidence,),
                    current_fields=current_fields,
                    baseline_fields=baseline_fields if prior else None,
                    change_basis=(ChangeBasis.BASELINE_DELTA if prior
                                  else ChangeBasis.CURRENT_FACT_ONLY),
                    freshness_state=FreshnessState.FRESH,
                    created_at=now,
                ))

    # -- Completed Executions
    trades = barrier.materials.get(JobKind.TRADES)
    if trades is not None:
        for account in trades.payload["accounts"]:
            for row in account["inserted"]:
                market = row["classified_market"]
                market_value = Market(market)
                if market in proceed:
                    assignment = CandidateAssignment.CRITICAL
                elif (
                    barrier.market_readiness[market_value]
                    is MarketReadiness.SKIP_CLOSED
                ):
                    if barrier.mask_digest is not None:
                        from tokmo.values.open_market_mask import MaskBindingError
                        raise MaskBindingError(
                            "closed-market execution crossed the masked "
                            "analysis barrier")
                    assignment = CandidateAssignment.EXPLICITLY_DEFERRED
                else:
                    continue
                subject = subject_id_for(
                    MaterialKindV1.COMPLETED_EXECUTION,
                    [market, row["instrument_raw"], row["source_record_key"]],
                )
                evidence = _evidence(
                    MaterialKindV1.COMPLETED_EXECUTION, "toss-orders-v1",
                    row["source_record_key"], row, trades, _parse_time(row["filled_at"], now),
                )
                current_fields = {
                    "market": market, "symbol": row["instrument_raw"],
                    "source_record_key": row["source_record_key"], "trade_date": row["trade_date"],
                    "transaction_type_raw": row["transaction_type_raw"],
                    "quantity": row["quantity"], "unit_price": row["unit_price"],
                    "amount": row["amount"], "currency_raw": row["currency_raw"],
                    "filled_at": _parse_time(row["filled_at"], now),
                }
                deltas.append(MaterialDeltaV1(
                    subject_id=subject,
                    material_kind=MaterialKindV1.COMPLETED_EXECUTION,
                    market=market, symbol=row["instrument_raw"],
                    tick_id=barrier.tick_id,
                    material_binding_digest=binding,
                    evidence_refs=(evidence,),
                    current_fields=current_fields,
                    baseline_fields=None,
                    change_basis=ChangeBasis.CURRENT_FACT_ONLY,
                    freshness_state=FreshnessState.FRESH,
                    created_at=now,
                ))

    # -- Incidents
    for incident in incident_rows:
        key = incident.key
        subject = subject_id_for(
            MaterialKindV1.OPERATIONAL_INCIDENT, [key.kind, key.subject],
        )
        fields = {
            "incident_kind": key.kind, "incident_subject": key.subject,
            "state": incident.state.value, "impact_ref": incident.impact_ref,
        }
        prior = baselines.get((MaterialKindV1.OPERATIONAL_INCIDENT, subject))
        advances.append(_advance(
            MaterialKindV1.OPERATIONAL_INCIDENT, subject, fields, None,
            barrier.tick_id,
        ))
        prior_pair = None if prior is None else (
            prior.fields["state"], prior.fields["impact_ref"],
        )
        pair = (incident.state.value, incident.impact_ref)
        if prior_pair == pair:
            continue
        transition = (
            "FIRST" if prior is None
            else "RESOLVED"
            if incident.state.value == "RESOLVED"
            else "IMPACT_CHANGED"
        )
        row = (
            key.kind, key.subject, incident.state.value,
            incident.impact_ref, incident.count,
        )
        evidence = EvidenceRefV1(
            evidence_id_for(
                MaterialKindV1.OPERATIONAL_INCIDENT, "tokmo-ops-kernel-v1",
                f"{key.kind}:{key.subject}",
                hashlib.sha256(canonical_encode({
                    "v": 1, "domain": "tokmo-v3:evidence-content",
                    "row": row, "pack": "",
                })).hexdigest(),
            ),
            MaterialKindV1.OPERATIONAL_INCIDENT, "tokmo-ops-kernel-v1",
            f"{key.kind}:{key.subject}", incident.last_seen_at,
            incident.last_seen_at, incident.last_seen_at, incident.last_seen_at,
            hashlib.sha256(canonical_encode({
                "v": 1, "domain": "tokmo-v3:evidence-content",
                "row": row, "pack": "",
            })).hexdigest(),
            2, "ops-kernel-incident-v1", TrustClass.DERIVED,
            FreshnessState.FRESH,
            incident.last_seen_at + timedelta(seconds=600),
        )
        current_fields = {
            "incident_kind": key.kind, "incident_subject": key.subject,
            "transition": transition, "count": incident.count,
            "impact_ref": incident.impact_ref,
        }
        deltas.append(MaterialDeltaV1(
            subject_id=subject,
            material_kind=MaterialKindV1.OPERATIONAL_INCIDENT,
            market="GLOBAL", symbol=None,
            tick_id=barrier.tick_id,
            material_binding_digest=binding,
            evidence_refs=(evidence,),
            current_fields=current_fields,
            baseline_fields=None,
            change_basis=ChangeBasis.CURRENT_FACT_ONLY,
            freshness_state=FreshnessState.FRESH,
            created_at=now,
        ))

    deltas.sort(key=lambda d: (d.material_kind.value, d.subject_id, d.delta_id))
    advances.sort(key=lambda a: (a.kind.value, a.subject_id))
    history_advances.sort(key=lambda h: h.subject_id)
    if len(deltas) > MANIFEST_MAX_CANDIDATES_PER_TICK:
        raise ValueError("delta capacity exceeded")
    return MaterialDeltaResultV1(
        binding, tuple(deltas), tuple(advances), tuple(history_advances),
        MappingProxyType(dict(sorted(numeric_state_counts.items()))),
        MappingProxyType(dict(sorted(trigger_fire_counts.items()))),
        mask_digest=barrier.mask_digest,
    )
