"""AI-safe portfolio EVENT payload contract tests (AP-01..AP-46 pure surface)."""

import hashlib
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from test_portfolio_calculations import (
    PRIVATE_PENSION,
    PRIVATE_PERSONAL,
    account,
    holding,
    public_barrier,
    quote,
)

from tokmo.programs.event_admission import (
    DEFAULT_EVENT_ADMISSION_POLICY_V1,
    evaluate_event_admission,
)
from tokmo.programs.material_delta import build_material_deltas
from tokmo.programs.portfolio_ai_payload import (
    PortfolioCandidateContextV1,
    build_portfolio_event_payloads_v1,
)
from tokmo.programs.portfolio_calculations import (
    compute_portfolio_snapshot,
    project_portfolio_presentation,
)
from tokmo.values.analysis_contracts import AnalysisKind, MaterialKindV1
from tokmo.values.material_pack import canonical_encode
from tokmo.values.ops_kernel import CandidateAssignment
from tokmo.values.portfolio_ai_payload import (
    CandidateSuppressionReasonV1,
    CompletedExecutionPayloadV1,
    ExecutionDirectionV1,
    PortfolioCandidateSummaryV1,
    PortfolioEventCandidatePayloadV1,
    PortfolioPayloadBuildStatus,
    PortfolioPositionDetailV1,
    canonical_portfolio_payload_bytes_v1,
    compute_portfolio_payload_digest_v1,
    map_execution_direction_v1,
    normalize_instrument_symbol_v1,
    normalize_portfolio_payload_v1,
)
from tokmo.values.portfolio_calculations import (
    FxStatus,
    PortfolioScope,
    PricingState,
    RatioState,
    ScopeUnresolved,
)
from tokmo.values.portfolio_scope import resolve_account_scope_pattern  # N5
from tokmo.values.session_gate import Market

UTC = UTC
NOW = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
H64 = "a" * 64


def _detail(scope=PortfolioScope.PERSONAL, market=Market.KR, currency="KRW"):
    return PortfolioPositionDetailV1(
        scope,
        market,
        "005930" if market is Market.KR else "SYNUS1",
        currency,
        Decimal("2.00"),
        Decimal(80),
        Decimal(100),
        Decimal(160),
        Decimal(200),
        None if currency == "KRW" else Decimal(300000),
        Decimal(40),
        None if currency == "KRW" else Decimal(60000),
        RatioState.DEFINED,
        Decimal("0.25"),
        PricingState.PRICED,
        Decimal(1),
        Decimal(1),
        Decimal(1),
        Decimal(1),
        Decimal(1),
        Decimal(1),
        NOW,
        NOW,
        None if currency == "KRW" else H64,
    )


def _summary(scope=PortfolioScope.PERSONAL, market=Market.KR, currency="KRW"):
    return PortfolioCandidateSummaryV1(
        scope,
        FxStatus.NOT_REQUIRED_SINGLE_KRW if currency == "KRW" else FxStatus.AVAILABLE,
        Decimal(200) if currency == "KRW" else Decimal(300000),
        Decimal(40) if currency == "KRW" else Decimal(60000),
        RatioState.DEFINED,
        Decimal("0.25"),
        market,
        currency,
        1,
        1,
        0,
        Decimal(160),
        Decimal(160),
        Decimal(200),
        None if currency == "KRW" else Decimal(300000),
        Decimal(40),
        None if currency == "KRW" else Decimal(60000),
        RatioState.DEFINED,
        Decimal("0.25"),
        RatioState.DEFINED,
        Decimal(1),
        None if currency == "KRW" else H64,
    )


def payload(scope=PortfolioScope.PERSONAL, market=Market.KR, currency="KRW"):
    detail = _detail(scope, market, currency)
    candidate = PortfolioEventCandidatePayloadV1(
        1,
        AnalysisKind.EVENT,
        "candidate-1",
        "delta-1",
        MaterialKindV1.QUOTE_STATE,
        market,
        detail.symbol,
        7,
        H64,
        "b" * 64,
        "event-admission-v1",
        "c" * 64,
        ("event_quote_threshold_move",),
        None,
        detail,
        _summary(scope, market, currency),
        None,
        (),
        H64,
    )
    return replace(
        candidate, payload_digest=compute_portfolio_payload_digest_v1(candidate)
    )


def test_normalizer_requires_public_conversion_and_emits_decimal_utc6():
    candidate = payload()
    with pytest.raises(TypeError):
        canonical_encode(candidate)
    normalized = normalize_portfolio_payload_v1(candidate)
    assert normalized["detail"]["quantity"] == "2"
    assert normalized["detail"]["holdings_observed_at"] == "2026-08-04T00:00:00.000000Z"
    assert canonical_portfolio_payload_bytes_v1(candidate) == canonical_encode(
        normalized
    )


def test_normalizer_is_deterministic_and_rejects_unsafe_types():
    assert normalize_portfolio_payload_v1(Decimal("-0.0")) == "0"
    assert normalize_portfolio_payload_v1(frozenset({"b", "a"})) == ["a", "b"]
    shifted = NOW.astimezone(timezone(timedelta(hours=9)))
    assert normalize_portfolio_payload_v1(shifted) == "2026-08-04T00:00:00.000000Z"
    for bad in (1.0, b"x", {"x"}, Decimal("NaN"), datetime(2026, 8, 4)):
        with pytest.raises(TypeError, match="unsupported portfolio payload type"):
            normalize_portfolio_payload_v1(bad)


def test_payload_digest_recomputes_and_binds_semantic_dimensions():
    candidate = payload()
    assert compute_portfolio_payload_digest_v1(candidate) == candidate.payload_digest
    mutated = replace(candidate, tick_id=8)
    assert compute_portfolio_payload_digest_v1(mutated) != candidate.payload_digest
    raw = canonical_portfolio_payload_bytes_v1(candidate)
    assert hashlib.sha256(raw).hexdigest()
    assert b"account_type_label" not in raw
    assert b"ordinal" not in raw
    assert b"source_record_key" not in raw
    assert b'"realized_pnl":' not in raw
    assert b"internal_" not in raw


def test_us_money_contract_has_no_krw_price_average_or_principal_fields():
    normalized = normalize_portfolio_payload_v1(
        payload(
            PortfolioScope.PENSION,
            Market.US,
            "USD",
        )
    )
    detail = normalized["detail"]
    assert detail["average_cost_native"] == "80"
    assert detail["current_price_native"] == "100"
    assert detail["invested_cost_basis_native"] == "160"
    assert detail["evaluated_market_value_krw"] == "300000"
    assert detail["unrealized_pnl_krw"] == "60000"
    assert not any(
        key.endswith("price_krw")
        or key == "average_cost_krw"
        or key == "invested_cost_basis_krw"
        for key in detail
    )


def test_every_suppression_reason_is_closed_and_unique():
    values = [item.value for item in CandidateSuppressionReasonV1]
    assert len(values) == len(set(values)) == 11


def test_execution_symbol_and_raw_fact_grammar_is_closed():
    assert normalize_instrument_symbol_v1(Market.KR, "005930") == "005930"
    assert normalize_instrument_symbol_v1(Market.US, "BRK.B") == "BRK.B"
    for market, raw in (
        (Market.KR, "5930"),
        (Market.KR, " 005930"),
        (Market.US, "aapl"),
        (Market.US, "Fixture ETF Alpha"),
    ):
        assert normalize_instrument_symbol_v1(market, raw) is None
    valid = CompletedExecutionPayloadV1(
        PortfolioScope.PERSONAL,
        Market.US,
        "AAPL",
        ExecutionDirectionV1.BUY,
        "BUY",
        Decimal(1),
        Decimal(100),
        Decimal(100),
        "USD",
        "2026-08-04",
        NOW,
        "COMPLETED_EXECUTION_FACT",
    )
    assert valid.symbol == "AAPL"
    from dataclasses import asdict

    base = asdict(valid)
    invalid = (
        {"symbol": "not a ticker!!"},
        {"direction_raw_fact": "BUY\nDROP"},
        {"direction_raw_fact": "X" * 33},
        {"trade_date": "not-a-date"},
        {"currency": "KRW"},
        {"completion_status": "COMPLETED"},
        {"quantity": Decimal("NaN")},
        {"amount_native": Decimal(-1)},
    )
    for mutation in invalid:
        with pytest.raises(ValueError):
            CompletedExecutionPayloadV1(**(base | mutation))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BUY", ExecutionDirectionV1.BUY),
        ("buy", ExecutionDirectionV1.BUY),
        ("매수", ExecutionDirectionV1.BUY),
        ("SELL", ExecutionDirectionV1.SELL),
        ("매도", ExecutionDirectionV1.SELL),
        ("매수취소", ExecutionDirectionV1.OTHER),
        ("NOT_A_BUY", ExecutionDirectionV1.OTHER),
        ("PRE-SELL", ExecutionDirectionV1.OTHER),
        ("BUYBACK", ExecutionDirectionV1.OTHER),
    ],
)
def test_execution_direction_uses_exact_closed_tokens(raw, expected):
    assert map_execution_direction_v1(raw) is expected


def test_generated_portfolio_payload_fixtures_are_exact_v1():
    generator = Path(__file__).parent / "fixtures/portfolio_ai_payload/generate_v1.py"
    completed = subprocess.run(
        [sys.executable, str(generator), "--check"],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _resolver(raw):
    return {
        PRIVATE_PERSONAL: PortfolioScope.PERSONAL,
        PRIVATE_PENSION: PortfolioScope.PENSION,
    }.get(raw, ScopeUnresolved.UNRESOLVED)


def _contexts(barrier, now=NOW):
    built = build_material_deltas(
        barrier,
        baselines={},
        history={},
        now=now,
        resolve_scope=resolve_account_scope_pattern,
    )
    decisions = evaluate_event_admission(
        DEFAULT_EVENT_ADMISSION_POLICY_V1, built.deltas, now=now
    )
    by_decision = {row.delta_id: row for row in decisions}
    return built, tuple(
        PortfolioCandidateContextV1(
            f"candidate-{i}",
            delta,
            by_decision[delta.delta_id],
            CandidateAssignment.FULL_REVIEW,
            None,
        )
        for i, delta in enumerate(built.deltas)
        if by_decision[delta.delta_id].decision == "ADMIT"
    )


def test_personal_pension_quote_diversity_uses_two_minimal_details(tmp_path):
    ph, pt = account(
        PRIVATE_PERSONAL, holdings=(holding("SYNUS1", "2", "80", "USD", "US"),)
    )
    rh, rt = account(
        PRIVATE_PENSION, holdings=(holding("SYNUS2", "3", "90", "USD", "US"),)
    )
    barrier = public_barrier(
        tmp_path,
        holding_accounts=(ph, rh),
        trade_accounts=(pt, rt),
        quotes={
            "KR": [],
            "US": [quote("SYNUS1", "100", "USD"), quote("SYNUS2", "110", "USD")],
        },
    )
    snap = compute_portfolio_snapshot(
        barrier, resolve_scope=_resolver, fx_evidence=None
    )
    assert snap.body is not None
    built, contexts = _contexts(barrier)
    result = build_portfolio_event_payloads_v1(
        snap,
        project_portfolio_presentation(snap.body),
        contexts,
        built.material_binding_digest,
        barrier.mask_digest,
    )
    assert (
        result.status is PortfolioPayloadBuildStatus.READY
        and result.selected_count == 2
    )
    assert {row.detail.portfolio_scope for row in result.selected} == {
        PortfolioScope.PERSONAL,
        PortfolioScope.PENSION,
    }


def test_unheld_suppression_does_not_block_matched_candidate(tmp_path):
    ph, pt = account(
        PRIVATE_PERSONAL, holdings=(holding("SYNUS1", "2", "80", "USD", "US"),)
    )
    barrier = public_barrier(
        tmp_path,
        holding_accounts=(ph,),
        trade_accounts=(pt,),
        quotes={
            "KR": [],
            "US": [quote("SYNUS1", "100", "USD"), quote("UNHELD", "50", "USD")],
        },
    )
    snap = compute_portfolio_snapshot(
        barrier, resolve_scope=_resolver, fx_evidence=None
    )
    assert snap.body is not None
    built, contexts = _contexts(barrier)
    result = build_portfolio_event_payloads_v1(
        snap,
        project_portfolio_presentation(snap.body),
        contexts,
        built.material_binding_digest,
        barrier.mask_digest,
    )
    assert result.selected_count == 1 and result.selected[0].symbol == "SYNUS1"
    assert any(
        row.reason is CandidateSuppressionReasonV1.UNHELD_NO_POSITION
        for row in result.suppressions
    )


def test_held_quote_missing_canonical_position_has_distinct_closed_reason(tmp_path):
    ph, pt = account(
        PRIVATE_PERSONAL, holdings=(holding("SYNUS1", "2", "80", "USD", "US"),)
    )
    barrier = public_barrier(
        tmp_path,
        holding_accounts=(ph,),
        trade_accounts=(pt,),
        quotes={"KR": [], "US": [quote("SYNUS1", "100", "USD")]},
    )
    snap = compute_portfolio_snapshot(
        barrier, resolve_scope=_resolver, fx_evidence=None
    )
    built, contexts = _contexts(barrier)
    context = contexts[0]
    missing = replace(
        context,
        delta=replace(
            context.delta,
            symbol="MISSING",
            current_fields=context.delta.current_fields
            | {"portfolio_relation": "HELD"},
        ),
    )
    result = build_portfolio_event_payloads_v1(
        snap,
        project_portfolio_presentation(snap.body),
        (missing,),
        built.material_binding_digest,
        barrier.mask_digest,
    )
    assert result.selected_count == 0
    assert (
        result.suppressions[0].reason
        is CandidateSuppressionReasonV1.HELD_POSITION_NOT_CANONICALLY_MATCHED
    )


def test_unresolved_private_identity_is_whole_build_no_call(tmp_path):
    ph, pt = account(
        "toss-acct-333333333333333333333333",
        holdings=(holding("SYNUS1", "2", "80", "USD", "US"),),
    )
    barrier = public_barrier(
        tmp_path,
        holding_accounts=(ph,),
        trade_accounts=(pt,),
        quotes={"KR": [], "US": [quote("SYNUS1", "100", "USD")]},
    )
    snap = compute_portfolio_snapshot(
        barrier, resolve_scope=_resolver, fx_evidence=None
    )
    built, contexts = _contexts(barrier)
    result = build_portfolio_event_payloads_v1(
        snap, None, contexts, built.material_binding_digest, barrier.mask_digest
    )
    assert (
        result.status is PortfolioPayloadBuildStatus.PRIVATE_SCOPE_UNRESOLVED
        and result.selected == ()
    )


def test_priced_krw_calc_presentation_gate_stays_ready(tmp_path):
    ph, pt = account(
        PRIVATE_PERSONAL, holdings=(holding("005930", "2", "80000", "KRW", "KR"),)
    )
    barrier = public_barrier(
        tmp_path,
        holding_accounts=(ph,),
        trade_accounts=(pt,),
        quotes={"KR": [quote("005930", "100000", "KRW")], "US": []},
    )
    snap = compute_portfolio_snapshot(
        barrier, resolve_scope=_resolver, fx_evidence=None
    )
    assert snap.body is not None
    built, contexts = _contexts(barrier)
    result = build_portfolio_event_payloads_v1(
        snap,
        project_portfolio_presentation(snap.body),
        contexts,
        built.material_binding_digest,
        barrier.mask_digest,
    )
    assert result.status is PortfolioPayloadBuildStatus.READY
    assert result.selected[0].detail.evaluated_market_value_krw is None
