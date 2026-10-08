"""Provider-specific whitelist projections; no I/O and no persistence."""

from types import MappingProxyType

from tokmo.values.analysis_contracts import CandidateEnvelopeV1

_GLM_KEYS = frozenset({
    "symbol", "account_type_label", "change_kind", "change_basis", "transition",
    "portfolio_relation",
    "quantity_delta_ratio", "price_delta_ratio", "incident_kind", "count",
    "numeric_state", "trigger_kind", "trigger_horizon", "reversal",
    "persistent_drift_ticks",
    "price_delta_ratio_5m", "price_delta_ratio_15m", "price_delta_ratio_30m",
    "velocity_per_min", "acceleration_per_min2",
})
_SOL_EXTRA_KEYS = frozenset({
    "current_quantity", "current_avg_price", "baseline_quantity",
    "baseline_avg_price", "last_price", "baseline_price", "trade_date",
    "quantity", "unit_price", "amount", "filled_at", "currency", "currency_raw",
    "ordinal", "money_impact", "trigger_reference_price",
})


def _project(candidate: CandidateEnvelopeV1, allowed: frozenset[str]):
    result = {
        "candidate_id": candidate.candidate_id,
        "kind": candidate.kind.value,
        "market": candidate.market,
    }
    for key in sorted(allowed):
        if key in candidate.fields:
            output_key = "incident_count" if key == "count" else key
            result[output_key] = candidate.fields[key]
    return MappingProxyType(result)


def project_for_glm(candidate: CandidateEnvelopeV1):
    return _project(candidate, _GLM_KEYS)


def project_for_sol(candidate: CandidateEnvelopeV1):
    return _project(candidate, _GLM_KEYS | _SOL_EXTRA_KEYS)
