"""Same-tick fresh assembly, bounded candles, and degraded Quant settlement."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_quant_recurring_membership_runtime import (
    NOW,
    SYMBOLS,
    _context,
    _deps,
    _Executor,
    _SettlementExecutor,
)


def _write_holdings(root, captured_at):
    from tokmo.records.current_holdings_store import CurrentHoldingsStore
    from tokmo.values.current_holdings import (
        AccountHoldingsV1,
        AccountScopeIdentityV1,
        CurrentHoldingsV1,
        HoldingItemV1,
    )

    scope = "toss-acct-" + "9" * 24
    state = CurrentHoldingsV1(
        1,
        (
            AccountHoldingsV1(
                scope,
                1,
                tuple(
                    HoldingItemV1(symbol, "1", "100", "USD", "US", None)
                    for symbol in SYMBOLS
                ),
            ),
        ),
        True,
        captured_at,
        {scope: AccountScopeIdentityV1(scope, "personal", 1, None)},
    )
    CurrentHoldingsStore(
        root / "stores" / "current_holdings.json", clock=lambda: captured_at
    ).replace(state, identity_fingerprints={scope: "d" * 64})


def _material(symbol, year=2025, period_end=date(2025, 12, 31)):
    from tokmo.values.fundamental_materials import (
        AccountingRegime,
        MetricEvidence,
        MetricSelection,
        OfficialFundamentalMaterialV1,
        StatementScope,
    )

    metric = MetricEvidence(Decimal(100), "x", "USD", MetricSelection.EXACT)
    shares = MetricEvidence(Decimal(10), "shares", "shares", MetricSelection.EXACT)
    return OfficialFundamentalMaterialV1(
        "sec",
        symbol,
        symbol,
        "US_NASDAQ",
        AccountingRegime.US_GAAP,
        StatementScope.CONSOLIDATED,
        year,
        None,
        period_end,
        f"f-{symbol}-{year}",
        "2026-01-01",
        datetime(2026, 1, 2, tzinfo=UTC),
        None,
        "us-gaap",
        "2025",
        "v1",
        metric,
        metric,
        metric,
        metric,
        metric,
        shares,
        "a",
        "b",
        datetime(2026, 1, 1, tzinfo=UTC),
        NOW + timedelta(minutes=1),
    )


def _candle_rows(*, count, end_date):
    from tokmo.values.toss_market_data import Candle

    rows = []
    cursor = end_date
    while len(rows) < count:
        if cursor.weekday() < 5:
            rows.append(
                Candle(
                    datetime.combine(cursor, datetime.min.time(), UTC),
                    "10",
                    "11",
                    "9",
                    "10",
                    "1000",
                    "USD",
                )
            )
        cursor -= timedelta(days=1)
    return tuple(rows)


class _Token:
    def __init__(self, *, fail=False):
        self.calls = 0
        self.fail = fail

    def bearer(self):
        self.calls += 1
        if self.fail:
            raise ValueError("token unavailable")
        return "fixture-bearer"


def _membership(tmp_path):
    from tokmo.programs.quant_recurring_shadow import run_quant_membership_acquisition
    from tokmo.records.quant_recurring_receipts import QuantRecurringReceiptStore
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1

    receipts = QuantRecurringReceiptStore(tmp_path / "ops" / "quant_recurring.sqlite3")
    outcome = run_quant_membership_acquisition(
        root=tmp_path,
        deps=_deps(),
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=_Executor(),
        context=_context(),
        receipts=receipts,
    )
    assert outcome.status == "ok"
    return outcome, receipts


def _shadow_deps(*, page1_count=199):
    calls = []

    def stock_candles_fetch(*, claim, bearer, symbol, before, adjusted):
        del claim
        assert bearer == "fixture-bearer"
        assert adjusted is False
        ordinal = sum(1 for called_symbol, _ in calls if called_symbol == symbol)
        calls.append((symbol, before))
        if ordinal == 0:
            rows = _candle_rows(count=page1_count, end_date=date(2026, 8, 13))
        else:
            rows = _candle_rows(count=50, end_date=before.date())
        return SimpleNamespace(code="success", value=SimpleNamespace(candles=rows))

    return SimpleNamespace(
        clock=lambda: NOW + timedelta(minutes=1),
        stock_candles_fetch=stock_candles_fetch,
        candle_calls=calls,
    )


def test_missing_fundamental_store_is_truthful_no_pack_with_zero_candle_starts(
    tmp_path,
):
    from tokmo.programs.quant_recurring_shadow import run_quant_recurring_shadow
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    membership, receipts = _membership(tmp_path)
    _write_holdings(tmp_path, NOW + timedelta(minutes=1))
    executor = _Executor()
    token = _Token()
    status, reason = run_quant_recurring_shadow(
        root=tmp_path,
        deps=_shadow_deps(),
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        token=token,
        membership=membership,
        receipts=receipts,
    )
    assert (status, reason) == ("skipped", "fundamental_store_absent")
    assert executor.starts == []
    assert token.calls == 0
    assert receipts.read_due(membership.identity.identity_digest).state is (
        QuantTerminalStateV1.NO_PACK
    )


def test_absent_fresh_holdings_settles_no_pack_without_token_or_candles(tmp_path):
    from tokmo.programs.quant_recurring_shadow import run_quant_recurring_shadow
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    membership, receipts = _membership(tmp_path)
    executor = _Executor()
    token = _Token()
    status, reason = run_quant_recurring_shadow(
        root=tmp_path,
        deps=_shadow_deps(),
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        token=token,
        membership=membership,
        receipts=receipts,
    )
    assert (status, reason) == ("skipped", "holdings_unavailable")
    assert executor.starts == []
    assert token.calls == 0
    assert receipts.read_due(membership.identity.identity_digest).state is (
        QuantTerminalStateV1.NO_PACK
    )
    assert receipts.read_result(membership.identity.identity_digest) is not None


def test_fresh_four_member_success_uses_four_page1_starts_and_produces_no_ranks(
    monkeypatch,
    tmp_path,
):
    import tokmo.programs.quant_recurring_shadow as module
    from tokmo.programs.quant_multifactor_core import MIN_COHORT
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    packs = []
    evaluate = module.evaluate_quant_multifactor_core_v1

    def capture_pack(cohort_input):
        pack = evaluate(cohort_input)
        packs.append(pack)
        return pack

    monkeypatch.setattr(module, "evaluate_quant_multifactor_core_v1", capture_pack)
    monkeypatch.setattr(
        module,
        "read_admissible_materials_ro",
        lambda path, *, canonical_symbol, market, as_of, limit: (
            _material(canonical_symbol),
            _material(canonical_symbol, 2024, date(2024, 12, 31)),
        ),
    )
    membership, receipts = _membership(tmp_path)
    _write_holdings(tmp_path, NOW + timedelta(minutes=1))
    executor = _Executor()
    token = _Token()
    status, reason = module.run_quant_recurring_shadow(
        root=tmp_path,
        deps=_shadow_deps(page1_count=199),
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        token=token,
        membership=membership,
        receipts=receipts,
    )
    assert (status, reason) == ("ok", "degraded_pack")
    assert len(executor.starts) == 4
    assert all(identity.endswith(":1") for _, identity in executor.starts)
    assert token.calls == 1
    result = receipts.read_result(membership.identity.identity_digest)
    assert result.pack_digest
    assert result.degraded is True
    assert {"COHORT_INSUFFICIENT", "COVERAGE_INSUFFICIENT_BELOW_MIN_COHORT"} <= set(
        result.limitations
    )
    assert len(packs) == 1
    pack = packs[0]
    assert pack.cohort_size == 4
    assert pack.min_cohort == MIN_COHORT == 8
    assert all(
        member.factor_count == member.fundamental_factor_count == 0
        and member.composite_score is None
        for member in pack.member_results
    )
    assert all(
        metric.winsorized_value is None
        and metric.average_rank is None
        and metric.percentile is None
        for member in pack.member_results
        for factor in member.factors
        for metric in factor.metrics
    )
    assert receipts.read_due(membership.identity.identity_digest).state is (
        QuantTerminalStateV1.SUCCEEDED
    )


def test_full_page1_requests_exactly_one_page2_per_symbol_and_reuses_token(
    monkeypatch,
    tmp_path,
):
    import tokmo.programs.quant_recurring_shadow as module
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1

    monkeypatch.setattr(
        module,
        "read_admissible_materials_ro",
        lambda path, *, canonical_symbol, market, as_of, limit: (
            _material(canonical_symbol),
        ),
    )
    membership, receipts = _membership(tmp_path)
    _write_holdings(tmp_path, NOW + timedelta(minutes=1))
    executor = _Executor()
    token = _Token()
    status, _ = module.run_quant_recurring_shadow(
        root=tmp_path,
        deps=_shadow_deps(page1_count=200),
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        token=token,
        membership=membership,
        receipts=receipts,
    )
    assert status == "ok"
    assert len(executor.starts) == 8
    assert [identity.rsplit(":", 1)[1] for _, identity in executor.starts] == [
        "1",
        "2",
        "1",
        "2",
        "1",
        "2",
        "1",
        "2",
    ]
    assert token.calls == 1


def test_candle_error_response_settles_wire_failed(monkeypatch, tmp_path):
    import tokmo.programs.quant_recurring_shadow as module
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1

    monkeypatch.setattr(
        module,
        "read_admissible_materials_ro",
        lambda *args, **kwargs: (_material(kwargs["canonical_symbol"]),),
    )
    membership, receipts = _membership(tmp_path)
    _write_holdings(tmp_path, NOW + timedelta(minutes=1))
    deps = _shadow_deps()
    deps.stock_candles_fetch = lambda **kwargs: SimpleNamespace(
        code="error", value=None
    )
    executor = _SettlementExecutor()

    status, reason = module.run_quant_recurring_shadow(
        root=tmp_path,
        deps=deps,
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        token=_Token(),
        membership=membership,
        receipts=receipts,
    )

    assert (status, reason) == ("skipped", "candle_unavailable")
    assert executor.settlements == [("candles:qt:US:AAA:2026-08-13:1", "FAILED")]


def test_third_symbol_page1_failure_counts_all_started_pages(monkeypatch, tmp_path):
    import tokmo.programs.quant_recurring_shadow as module
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1

    monkeypatch.setattr(
        module,
        "read_admissible_materials_ro",
        lambda *args, **kwargs: (_material(kwargs["canonical_symbol"]),),
    )
    membership, receipts = _membership(tmp_path)
    _write_holdings(tmp_path, NOW + timedelta(minutes=1))
    deps = _shadow_deps()
    successful_fetch = deps.stock_candles_fetch
    attempts = 0

    def fail_third_page1(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 3:
            raise RuntimeError("fixture page1 failure")
        return successful_fetch(**kwargs)

    deps.stock_candles_fetch = fail_third_page1
    executor = _SettlementExecutor()
    status, reason = module.run_quant_recurring_shadow(
        root=tmp_path,
        deps=deps,
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        token=_Token(),
        membership=membership,
        receipts=receipts,
    )

    assert (status, reason) == ("skipped", "candle_unavailable")
    result = receipts.read_result(membership.identity.identity_digest)
    assert result.candle_page1_starts == 3
    assert result.candle_page2_starts == 0
    assert [settlement for _, settlement in executor.settlements] == [
        "SUCCEEDED",
        "SUCCEEDED",
        "FAILED",
    ]


def test_ninth_quant_candle_is_refused_before_executor_callback(tmp_path):
    import tokmo.programs.quant_recurring_shadow as module
    from tokmo.values.ops_kernel import CadenceBudgetExceeded
    from tokmo.values.quant_recurring import QuantSessionBindingV1

    executor = _Executor()
    starts = module._QuantCandleStarts()
    with pytest.raises(CadenceBudgetExceeded):
        module._fetch_candle_pages(
            deps=_shadow_deps(page1_count=200),
            executor=executor,
            bearer="fixture-bearer",
            binding=QuantSessionBindingV1(_context().identity),
            symbol="AAA",
            remaining_starts=1,
            starts=starts,
        )
    assert len(executor.starts) == 1
    assert (starts.page1, starts.page2) == (1, 0)


def test_holdings_captured_before_membership_or_token_failure_spends_zero_candles(
    monkeypatch,
    tmp_path,
):
    import tokmo.programs.quant_recurring_shadow as module
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1

    monkeypatch.setattr(
        module,
        "read_admissible_materials_ro",
        lambda *args, **kwargs: (_material(kwargs["canonical_symbol"]),),
    )
    membership, receipts = _membership(tmp_path)
    _write_holdings(tmp_path, NOW - timedelta(seconds=1))
    executor = _Executor()
    status, reason = module.run_quant_recurring_shadow(
        root=tmp_path,
        deps=_shadow_deps(),
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        token=_Token(),
        membership=membership,
        receipts=receipts,
    )
    assert (status, reason) == ("skipped", "holdings_identity")
    assert executor.starts == []

    other_root = tmp_path / "token"
    membership, receipts = _membership(other_root)
    _write_holdings(other_root, NOW + timedelta(minutes=1))
    executor = _Executor()
    status, reason = module.run_quant_recurring_shadow(
        root=other_root,
        deps=_shadow_deps(),
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        token=_Token(fail=True),
        membership=membership,
        receipts=receipts,
    )
    assert (status, reason) == ("failed", "token_unavailable")
    assert executor.starts == []


def test_whole_tick_membership_candles_and_receipt_share_exactly_one_date(
    monkeypatch,
    capsys,
    tmp_path,
):
    import tokmo.programs.quant_recurring_shadow as module
    from tokmo.records.quant_recurring_receipts import QuantRecurringReceiptStore
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1

    monkeypatch.setattr(
        module,
        "read_admissible_materials_ro",
        lambda path, *, canonical_symbol, market, as_of, limit: (
            _material(canonical_symbol),
        ),
    )
    context = _context()
    receipts = QuantRecurringReceiptStore(tmp_path / "ops" / "quant_recurring.sqlite3")
    executor = _Executor()
    membership = module.run_quant_membership_acquisition(
        root=tmp_path,
        deps=_deps(),
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        context=context,
        receipts=receipts,
    )
    _write_holdings(tmp_path, NOW + timedelta(minutes=1))
    status, reason = module.run_quant_recurring_shadow(
        root=tmp_path,
        deps=_shadow_deps(page1_count=199),
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        token=_Token(),
        membership=membership,
        receipts=receipts,
    )
    assert (status, reason) == ("ok", "degraded_pack")
    membership_dates = {
        identity.split(":")[2]
        for operation, identity in executor.starts
        if operation.startswith("MEMBERSHIP_")
    }
    candle_dates = {
        identity.split(":")[-2]
        for operation, identity in executor.starts
        if operation == "STOCK_CANDLES_DAILY"
    }
    due = receipts.read_due(context.identity.identity_digest)
    assert membership_dates | candle_dates | {due.completed_session_date} == {
        context.completed_session_date.isoformat()
    }
    assert len(membership_dates) == len(candle_dates) == 1
    assert due.identity_digest == context.identity.identity_digest
    assert len(executor.starts) == 11
    assert capsys.readouterr().out == ""
    assert not (tmp_path / "official-membership" / "campaign.sqlite3").exists()
