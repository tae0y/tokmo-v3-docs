"""Pure canonical-portfolio to AI-safe EVENT payload projection."""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import hashlib

from tokmo.values.analysis_admission import AdmissionDecisionV1, MaterialDeltaV1
from tokmo.values.analysis_contracts import AnalysisKind, MaterialKindV1
from tokmo.values.ops_kernel import CandidateAssignment
from tokmo.values.portfolio_ai_payload import (
    CandidateScopeBindingV1, CandidateSuppressionReasonV1, CandidateSuppressionV1,
    PortfolioCandidateSummaryV1, PortfolioEventCandidatePayloadV1,
    PortfolioPayloadBuildResultV1, PortfolioPayloadBuildStatus,
    PortfolioPayloadEvidenceRefV1, PortfolioPositionDetailV1,
    CompletedExecutionPayloadV1,
    compute_portfolio_payload_digest_v1, map_execution_direction_v1,
    normalize_instrument_symbol_v1,
)
from tokmo.values.material_pack import canonical_encode
from tokmo.values.portfolio_calculations import (
    PortfolioPresentationV1, PortfolioSnapshotEnvelopeV1, PortfolioScope,
    SnapshotStatus,
)
from tokmo.values.session_gate import Market

_ZERO_DIGEST = "0" * 64
_KIND_RANK = {
    MaterialKindV1.COMPLETED_EXECUTION: 0,
    MaterialKindV1.HOLDINGS_CHANGE: 1,
    MaterialKindV1.QUOTE_STATE: 2,
}


@dataclass(frozen=True)
class PortfolioCandidateContextV1:
    candidate_id: str
    delta: MaterialDeltaV1
    decision: AdmissionDecisionV1
    assignment: CandidateAssignment
    scope_binding: CandidateScopeBindingV1 | None


def _suppression(context, reason):
    return CandidateSuppressionV1(context.candidate_id, reason)


def _assignment_reason(assignment):
    return {
        CandidateAssignment.SAMPLED_AUDIT: CandidateSuppressionReasonV1.SAMPLED_AUDIT_NO_CALL,
        CandidateAssignment.EXPLICITLY_DEFERRED: CandidateSuppressionReasonV1.EXPLICITLY_DEFERRED_NO_CALL,
        CandidateAssignment.TERMINAL: CandidateSuppressionReasonV1.TERMINAL_ASSIGNMENT_NO_CALL,
    }.get(assignment)


def _reconcile(calc, shown):
    pairs = (
        (calc.portfolio_scope, shown.portfolio_scope), (calc.market, shown.market),
        (calc.symbol, shown.symbol), (calc.currency, shown.currency),
        (calc.quantity, shown.quantity), (calc.pricing_state, shown.pricing_state),
        (calc.unrealized_return, shown.unrealized_return),
        (calc.average_cost, shown.average_cost_native),
        (calc.current_price, shown.current_price_native),
        (calc.total_cost_basis_native, shown.invested_cost_basis_native),
        (calc.priced_market_value_native, shown.evaluated_market_value_native),
        (calc.unrealized_pnl_native, shown.unrealized_pnl_native),
    )
    return all(left == right for left, right in pairs)


def _payload(context, calc, shown, scope, partition, partition_shown,
             semantic_digest, material_binding_digest, mask_digest,
             execution=None):
    detail = PortfolioPositionDetailV1(
        calc.portfolio_scope, calc.market, calc.symbol, calc.currency,
        calc.quantity, shown.average_cost_native, shown.current_price_native,
        shown.invested_cost_basis_native, shown.evaluated_market_value_native,
        shown.evaluated_market_value_krw, shown.unrealized_pnl_native,
        shown.unrealized_pnl_krw, shown.unrealized_return.state,
        shown.unrealized_return.value, shown.pricing_state,
        calc.currency_partition_market_value_weight,
        calc.market_currency_partition_market_value_weight,
        calc.currency_partition_total_cost_basis_weight,
        calc.market_currency_partition_total_cost_basis_weight,
        calc.scope_krw_priced_market_value_weight,
        calc.scope_krw_total_cost_basis_weight,
        calc.holdings_observed_at, calc.quote_observed_at,
        shown.fx_binding_digest,
    )
    summary = PortfolioCandidateSummaryV1(
        scope.portfolio_scope, scope.fx_status, scope.evaluated_market_value_krw,
        scope.unrealized_pnl_krw, scope.unrealized_return.state,
        scope.unrealized_return.value, partition.market, partition.currency,
        partition.position_count, partition.priced_position_count,
        partition.unpriced_position_count, partition_shown.total_cost_basis_native,
        partition_shown.priced_cost_basis_native,
        partition_shown.evaluated_market_value_native,
        partition_shown.evaluated_market_value_krw,
        partition_shown.unrealized_pnl_native,
        partition_shown.unrealized_pnl_krw,
        partition_shown.unrealized_return.state,
        partition_shown.unrealized_return.value,
        partition_shown.priced_cost_basis_coverage_ratio.state,
        partition_shown.priced_cost_basis_coverage_ratio.value,
        partition_shown.fx_binding_digest,
        calc.pension_freshness_class,
        (None if calc.pension_source_as_of is None
         else calc.pension_source_as_of.isoformat()),
    )
    evidence = tuple(PortfolioPayloadEvidenceRefV1(
        row.evidence_id, row.kind, row.source, row.observed_at, row.content_hash,
        row.schema_version, row.parser_version, row.trust_class,
        row.freshness_state, row.expires_at,
    ) for row in context.delta.evidence_refs)
    result = PortfolioEventCandidatePayloadV1(
        1, AnalysisKind.EVENT, context.candidate_id, context.delta.delta_id,
        context.delta.material_kind, calc.market, calc.symbol,
        context.delta.tick_id, semantic_digest, material_binding_digest,
        context.decision.policy_version, context.decision.policy_digest,
        context.decision.admission_reason_codes, mask_digest, detail, summary,
        execution, evidence, _ZERO_DIGEST,
    )
    return replace(result, payload_digest=compute_portfolio_payload_digest_v1(result))


def _same_decimal_fact(value: Decimal, raw: object) -> bool:
    try:
        candidate = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return False
    return candidate.is_finite() and value == candidate


def build_portfolio_event_payloads_v1(
    snapshot: PortfolioSnapshotEnvelopeV1,
    presentation: PortfolioPresentationV1 | None,
    contexts: tuple[PortfolioCandidateContextV1, ...],
    material_binding_digest: str,
    mask_digest: str | None,
) -> PortfolioPayloadBuildResultV1:
    admitted = len(contexts)
    if snapshot.status is SnapshotStatus.SCOPE_RESOLUTION_INCOMPLETE:
        return PortfolioPayloadBuildResultV1(
            PortfolioPayloadBuildStatus.PRIVATE_SCOPE_UNRESOLVED, snapshot.status,
            snapshot.semantic_digest, (), tuple(_suppression(row,
                CandidateSuppressionReasonV1.POSITION_NOT_CANONICALLY_MATCHED) for row in contexts),
            admitted, 0, admitted,
        )
    if snapshot.status is not SnapshotStatus.COMPLETE or snapshot.body is None or presentation is None:
        return PortfolioPayloadBuildResultV1(
            PortfolioPayloadBuildStatus.SNAPSHOT_INCOMPLETE, snapshot.status,
            snapshot.semantic_digest, (), tuple(_suppression(row,
                CandidateSuppressionReasonV1.POSITION_NOT_CANONICALLY_MATCHED) for row in contexts),
            admitted, 0, admitted,
        )
    body = snapshot.body
    calc_index = {(x.portfolio_scope, x.market, x.symbol, x.currency): x for x in body.positions}
    shown_index = {(x.portfolio_scope, x.market, x.symbol, x.currency): x for x in presentation.positions}
    partition_index = {(x.portfolio_scope, x.market, x.currency): x for x in body.partition_totals}
    shown_partition_index = {(x.portfolio_scope, x.market, x.currency): x
                             for scope in presentation.scopes for x in scope.partitions}
    scope_index = {x.portfolio_scope: x for x in presentation.scopes}
    if (len(calc_index) != len(body.positions) or len(shown_index) != len(presentation.positions)
            or len(partition_index) != len(body.partition_totals)):
        return PortfolioPayloadBuildResultV1(
            PortfolioPayloadBuildStatus.CONTRACT_INVALID, snapshot.status,
            snapshot.semantic_digest, (), tuple(_suppression(row,
                CandidateSuppressionReasonV1.POSITION_NOT_CANONICALLY_MATCHED) for row in contexts),
            admitted, 0, admitted,
        )
    eligible = []
    suppressions = []
    for context in contexts:
        assignment_reason = _assignment_reason(context.assignment)
        if assignment_reason is not None:
            suppressions.append(_suppression(context, assignment_reason))
            continue
        delta = context.delta
        if delta.material_kind is MaterialKindV1.OPERATIONAL_INCIDENT:
            suppressions.append(_suppression(context, CandidateSuppressionReasonV1.OPERATIONAL_INCIDENT_NO_PORTFOLIO_DETAIL))
            continue
        try:
            market = Market(delta.market)
        except ValueError:
            suppressions.append(_suppression(context, CandidateSuppressionReasonV1.POSITION_NOT_CANONICALLY_MATCHED))
            continue
        expected_currency = "KRW" if market is Market.KR else "USD"
        matches = []
        execution_payload = None
        if delta.material_kind is MaterialKindV1.QUOTE_STATE:
            matches = [row for key, row in calc_index.items()
                       if key[1] is market and key[2] == delta.symbol and key[3] == expected_currency]
            if not matches:
                reason = (
                    CandidateSuppressionReasonV1.UNHELD_NO_POSITION
                    if delta.current_fields.get("portfolio_relation") == "UNHELD"
                    else CandidateSuppressionReasonV1.HELD_POSITION_NOT_CANONICALLY_MATCHED
                )
                suppressions.append(_suppression(context, reason))
                continue
        elif delta.material_kind is MaterialKindV1.HOLDINGS_CHANGE:
            if delta.current_fields.get("change_kind") == "POSITION_CLOSED":
                suppressions.append(_suppression(context, CandidateSuppressionReasonV1.POSITION_CLOSED_NO_CURRENT_POSITION))
                continue
            if context.scope_binding is not None:
                row = calc_index.get((context.scope_binding.portfolio_scope, market, delta.symbol, expected_currency))
                matches = [] if row is None else [row]
            if not matches:
                suppressions.append(_suppression(context, CandidateSuppressionReasonV1.POSITION_NOT_CANONICALLY_MATCHED))
                continue
        elif delta.material_kind is MaterialKindV1.COMPLETED_EXECUTION:
            raw_symbol = delta.current_fields.get("symbol")
            normalized_symbol = normalize_instrument_symbol_v1(market, raw_symbol)
            if normalized_symbol is None:
                suppressions.append(_suppression(context, CandidateSuppressionReasonV1.EXECUTION_INSTRUMENT_UNMATCHED))
                continue
            raw_key = delta.current_fields.get("source_record_key")
            if type(raw_key) is not str:
                suppressions.append(_suppression(context, CandidateSuppressionReasonV1.EXECUTION_INSTRUMENT_UNMATCHED))
                continue
            key_digest = hashlib.sha256(canonical_encode({
                "v": 1, "domain": "tokmo-v3:trade-source-record-key:v1",
                "source_record_key": raw_key,
            })).hexdigest()
            scope = context.scope_binding.portfolio_scope if context.scope_binding else None
            facts = [fact for fact in body.trade_facts.completed_executions
                     if (scope is None or fact.portfolio_scope is scope)
                     and fact.classified_market is market
                     and normalize_instrument_symbol_v1(market, fact.instrument_raw) == normalized_symbol
                     and fact.source_record_key_digest == key_digest
                     and fact.trade_date == delta.current_fields.get("trade_date")
                     and fact.transaction_type_raw == delta.current_fields.get("transaction_type_raw")
                     and _same_decimal_fact(fact.quantity, delta.current_fields.get("quantity"))
                     and _same_decimal_fact(fact.unit_price, delta.current_fields.get("unit_price"))
                     and _same_decimal_fact(fact.amount, delta.current_fields.get("amount"))
                     and fact.currency_raw == delta.current_fields.get("currency_raw")
                     and fact.filled_at == delta.current_fields.get("filled_at")]
            if len(facts) != 1:
                suppressions.append(_suppression(context, CandidateSuppressionReasonV1.EXECUTION_INSTRUMENT_UNMATCHED))
                continue
            fact = facts[0]
            if fact.currency_raw != expected_currency:
                suppressions.append(_suppression(context, CandidateSuppressionReasonV1.POSITION_NOT_CANONICALLY_MATCHED))
                continue
            row = calc_index.get((fact.portfolio_scope, market, normalized_symbol, expected_currency))
            if row is None:
                suppressions.append(_suppression(context, CandidateSuppressionReasonV1.POSITION_NOT_CANONICALLY_MATCHED))
                continue
            try:
                direction = map_execution_direction_v1(fact.transaction_type_raw)
                execution_payload = CompletedExecutionPayloadV1(
                    fact.portfolio_scope, market, normalized_symbol, direction,
                    fact.transaction_type_raw, fact.quantity, fact.unit_price,
                    fact.amount, expected_currency, fact.trade_date, fact.filled_at,
                    "COMPLETED_EXECUTION_FACT",
                )
            except ValueError:
                suppressions.append(_suppression(context, CandidateSuppressionReasonV1.EXECUTION_INSTRUMENT_UNMATCHED))
                continue
            matches = [row]
        else:
            suppressions.append(_suppression(context, CandidateSuppressionReasonV1.EXECUTION_INSTRUMENT_UNMATCHED))
            continue
        candidates = []
        for calc in matches:
            key = (calc.portfolio_scope, calc.market, calc.symbol, calc.currency)
            shown = shown_index.get(key)
            pkey = (calc.portfolio_scope, calc.market, calc.currency)
            partition = partition_index.get(pkey)
            partition_shown = shown_partition_index.get(pkey)
            scope = scope_index.get(calc.portfolio_scope)
            if not shown or not partition or not partition_shown or not scope or not _reconcile(calc, shown):
                return PortfolioPayloadBuildResultV1(
                    PortfolioPayloadBuildStatus.CONTRACT_INVALID, snapshot.status,
                    snapshot.semantic_digest, (), tuple(_suppression(row,
                        CandidateSuppressionReasonV1.POSITION_NOT_CANONICALLY_MATCHED) for row in contexts),
                    admitted, 0, admitted,
                )
            candidate = _payload(context, calc, shown, scope, partition, partition_shown,
                                 snapshot.semantic_digest, material_binding_digest, mask_digest,
                                 execution_payload)
            rank = (_KIND_RANK[delta.material_kind], -len(context.decision.admission_reason_codes),
                    0 if market is Market.KR else 1, calc.symbol, context.candidate_id,
                    calc.portfolio_scope.value)
            candidates.append((rank, candidate))
        eligible.extend(candidates)
    eligible.sort(key=lambda row: row[0])
    # One scope-diverse pair when possible, with distinct candidate ids.
    selected = []
    if eligible:
        for rank, candidate in eligible:
            if any(x.candidate_id == candidate.candidate_id for x in selected):
                continue
            if len(selected) == 1 and any(
                other.detail.portfolio_scope is not selected[0].detail.portfolio_scope
                and other.candidate_id != selected[0].candidate_id for _, other in eligible
            ) and candidate.detail.portfolio_scope is selected[0].detail.portfolio_scope:
                continue
            selected.append(candidate)
            if len(selected) == 2:
                break
    selected_ids = {x.candidate_id for x in selected}
    already = {x.candidate_id for x in suppressions}
    for _, candidate in eligible:
        if candidate.candidate_id not in selected_ids and candidate.candidate_id not in already:
            suppressions.append(CandidateSuppressionV1(
                candidate.candidate_id, CandidateSuppressionReasonV1.CAP_NOT_SELECTED,
            ))
            already.add(candidate.candidate_id)
    selected.sort(key=lambda row: (row.detail.portfolio_scope.value, row.candidate_id))
    suppressions.sort(key=lambda row: row.candidate_id)
    status = PortfolioPayloadBuildStatus.READY if selected else PortfolioPayloadBuildStatus.NO_CANDIDATE
    return PortfolioPayloadBuildResultV1(
        status, snapshot.status, snapshot.semantic_digest, tuple(selected), tuple(suppressions),
        admitted, len(selected), len(suppressions),
    )
