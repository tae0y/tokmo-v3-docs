"""Slice 5 — profile runtimes against injected fakes.

KR: one STOCKS batch start, exact-echo failure terminal, truthful
``NO_PACK kr_fundamentals_paused`` after candle admission. US: thin adapter
whose plan and executed wires are byte-equal to the existing V1 lane, writing
into the V2 store through the receipt bridge. All fixture data is synthetic /
RECONSTRUCTED_CONTRACT (Plan/SDS §13).
"""

from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest


POLICY_VERSION = "cadence-300s-quant-profiles-shadow-v1"
SESSION = date(2026, 8, 21)
NOW = datetime(2026, 8, 21, 21, 10, tzinfo=timezone.utc)
KR_SYMBOLS = ("RCS001", "RCS002")
US_SYMBOLS = ("AAA", "BBB", "CCC", "DDD")
TOSS_SCOPE = "toss-acct-" + "c" * 24


class _Claim:
    def __init__(self, operation):
        self.operation = operation


class _Executor:
    def __init__(self):
        self.starts = []

    def execute(self, operation, native_identity, callback):
        self.starts.append((operation, native_identity))
        return callback(_Claim(operation))


class _Token:
    def __init__(self):
        self.bearers = 0

    def bearer(self):
        self.bearers += 1
        return "fixture-bearer"


def _policy():
    from tokmo.values.cadence_policy import (
        CADENCE_QUANT_PROFILES_SHADOW_POLICY_V1,
    )

    return CADENCE_QUANT_PROFILES_SHADOW_POLICY_V1


def _kr_context(symbols=KR_SYMBOLS):
    from tokmo.values.quant_profiles import (
        KR_PROFILE_ID,
        KR_UNIVERSE_POLICY_VERSION,
        QUANT_PROFILE_REGISTRY,
        QuantProfileDueContextV1,
        QuantProfileDueDecisionV1,
        QuantProfileDueIdentityV1,
    )
    from tokmo.values.session_gate import Market

    identity = QuantProfileDueIdentityV1(
        KR_PROFILE_ID, POLICY_VERSION, Market.KR, SESSION,
        KR_UNIVERSE_POLICY_VERSION,
    )
    return QuantProfileDueContextV1(
        profile_id=KR_PROFILE_ID,
        decision=QuantProfileDueDecisionV1.DUE,
        identity=identity,
        completed_session_date=SESSION,
        local_session_date=SESSION,
        predicted_symbols=symbols,
        deadline_margin_seconds=90,
        membership_plan_starts=QUANT_PROFILE_REGISTRY[
            KR_PROFILE_ID
        ].membership_plan_starts(len(symbols)),
        membership_starts_ceiling=_policy().request_limits.membership_starts,
    )


def _stock_info(symbol):
    from tokmo.values.toss_reference import KrMarketDetail, StockInfo

    return StockInfo(
        symbol=symbol,
        name=symbol,
        english_name=symbol,
        isin_code=f"KR7{symbol}000"[:12].ljust(12, "0"),
        market="KOSPI",
        security_type="STOCK",
        is_common_share=True,
        status="ACTIVE",
        currency="KRW",
        shares_outstanding="1000",
        list_date=date(2020, 1, 2),
        delist_date=None,
        korean_market_detail=KrMarketDetail(
            liquidation_trading=False,
            nxt_supported=True,
            krx_trading_suspended=False,
        ),
    )


def _kr_deps(*, outcome=None, calls=None):
    calls = calls if calls is not None else []

    def stocks_membership_fetch(*, claim, bearer, symbols):
        calls.append((claim.operation, bearer, tuple(symbols)))
        if outcome is not None:
            return outcome
        return SimpleNamespace(
            code="success",
            value=tuple(_stock_info(symbol) for symbol in symbols),
        )

    return SimpleNamespace(
        clock=lambda: NOW,
        stocks_membership_fetch=stocks_membership_fetch,
        calls=calls,
    )


def _store(tmp_path):
    from tokmo.records.quant_profile_receipts import QuantProfileReceiptStore

    return QuantProfileReceiptStore(tmp_path / "quant_profile_receipts.sqlite3")


def test_kr_membership_executes_exactly_one_stocks_wire_and_claims_v2_row(tmp_path):
    from tokmo.programs.quant_profile_kr import run_kr_profile_membership
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    executor = _Executor()
    deps = _kr_deps()
    receipts = _store(tmp_path)
    context = _kr_context()
    outcome = run_kr_profile_membership(
        deps=deps,
        policy=_policy(),
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        token=_Token(),
        context=context,
        receipts=receipts,
    )
    assert (outcome.status, outcome.reason) == ("ok", "acquired")
    assert outcome.membership_starts == 1
    assert executor.starts == [
        ("MEMBERSHIP_TOSS_STOCKS", "membership:toss-stocks:2026-08-21"),
    ]
    assert deps.calls == [
        ("MEMBERSHIP_TOSS_STOCKS", "fixture-bearer", KR_SYMBOLS),
    ]
    row = receipts.read_due(context.identity.identity_digest)
    assert row.state is QuantTerminalStateV1.CLAIMED
    assert row.profile_id == "quant-kr-toss-membership-v1"


def test_kr_cohort_echo_mismatch_and_transport_failure_are_terminal(tmp_path):
    from tokmo.programs.quant_profile_kr import run_kr_profile_membership
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    # echo mismatch: provider answers a subset
    receipts = _store(tmp_path)
    context = _kr_context()
    echo_deps = _kr_deps(outcome=SimpleNamespace(
        code="success", value=(_stock_info("RCS001"),),
    ))
    outcome = run_kr_profile_membership(
        deps=echo_deps,
        policy=_policy(),
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=_Executor(),
        token=_Token(),
        context=context,
        receipts=receipts,
    )
    assert (outcome.status, outcome.reason) == (
        "failed", "source_failure:cohort_echo",
    )
    row = receipts.read_due(context.identity.identity_digest)
    assert row.state is QuantTerminalStateV1.FAILED
    assert row.reason_code == "source_failure:cohort_echo"

    # provider/contract failure code maps to a typed source failure
    failed_store = _store(tmp_path / "second")
    (tmp_path / "second").mkdir(exist_ok=True)
    failure_deps = _kr_deps(outcome=SimpleNamespace(
        code="contract_mismatch", value=None,
    ))
    outcome = run_kr_profile_membership(
        deps=failure_deps,
        policy=_policy(),
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=_Executor(),
        token=_Token(),
        context=context,
        receipts=failed_store,
    )
    assert (outcome.status, outcome.reason) == (
        "failed", "source_failure:contract_mismatch",
    )


def _holdings_state(captured_at):
    from tokmo.values.current_holdings import (
        AccountHoldingsV1,
        AccountScopeIdentityV1,
        CurrentHoldingsV1,
        HoldingItemV1,
    )

    return CurrentHoldingsV1(
        1,
        (AccountHoldingsV1(TOSS_SCOPE, 1, tuple(
            HoldingItemV1(symbol, "1", "100", "KRW", "KR", None)
            for symbol in KR_SYMBOLS
        )),),
        True,
        captured_at,
        {TOSS_SCOPE: AccountScopeIdentityV1(TOSS_SCOPE, "brokerage", 1, None)},
    )


def _candle_outcome(rows=3):
    return SimpleNamespace(
        code="success",
        value=SimpleNamespace(candles=tuple(
            SimpleNamespace(
                timestamp=NOW - timedelta(days=index + 1),
                close_price="100",
                currency="KRW",
            )
            for index in range(rows)
        )),
    )


def test_kr_shadow_settles_truthful_no_pack_paused_after_candle_admission(tmp_path):
    from tokmo.programs.quant_profile_kr import (
        run_kr_profile_membership,
        run_kr_profile_shadow,
    )
    from tokmo.values.current_holdings import canonical_holdings_bytes
    from tokmo.values.quant_profiles import decode_membership_material
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    root = tmp_path / "root"
    (root / "stores").mkdir(parents=True)
    holdings = _holdings_state(NOW + timedelta(seconds=60))
    (root / "stores" / "current_holdings.json").write_bytes(
        canonical_holdings_bytes(holdings)
    )
    executor = _Executor()
    deps = _kr_deps()
    candle_calls = []

    def stock_candles_fetch(*, claim, bearer, symbol, before, adjusted):
        candle_calls.append((claim.operation, symbol, before, adjusted))
        return _candle_outcome()

    deps.stock_candles_fetch = stock_candles_fetch
    receipts = _store(tmp_path)
    context = _kr_context()
    membership = run_kr_profile_membership(
        deps=deps,
        policy=_policy(),
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        token=_Token(),
        context=context,
        receipts=receipts,
    )
    assert membership.status == "ok"
    status, reason = run_kr_profile_shadow(
        root=root,
        deps=deps,
        policy=_policy(),
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        token=_Token(),
        membership=membership,
        receipts=receipts,
    )
    assert (status, reason) == ("ok", "kr_fundamentals_paused")
    assert executor.starts == [
        ("MEMBERSHIP_TOSS_STOCKS", "membership:toss-stocks:2026-08-21"),
        ("STOCK_CANDLES_DAILY", "candles:qt:KR:RCS001:2026-08-21:1"),
        ("STOCK_CANDLES_DAILY", "candles:qt:KR:RCS002:2026-08-21:1"),
    ]
    assert [call[3] for call in candle_calls] == [False, False]

    row = receipts.read_due(context.identity.identity_digest)
    assert row.state is QuantTerminalStateV1.NO_PACK
    assert row.reason_code == "kr_fundamentals_paused"
    result = receipts.read_result(context.identity.identity_digest)
    assert result.degraded is True
    assert result.limitations == ("KR_FUNDAMENTALS_PAUSED",)
    assert (result.cohort_size, result.admissible_count) == (2, 2)
    assert (result.membership_starts, result.candle_page1_starts,
            result.candle_page2_starts) == (1, 2, 0)
    assert result.pack_digest == ""
    assert result.symbols == KR_SYMBOLS
    material = decode_membership_material(result.membership_material)
    assert [row_["symbol"] for row_ in material["members"]] == list(KR_SYMBOLS)
    assert material["request_identity"] == "membership:toss-stocks:2026-08-21"


def _us_bodies():
    listed = (
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|"
        "Round Lot Size|ETF|NextShares\n"
        + "\n".join(
            f"{symbol}|{symbol} Common Stock|Q|N|N|100|N|N"
            for symbol in US_SYMBOLS[:2]
        )
        + "\nFile Creation Time: 082120260125|||||||\n"
    ).encode()
    other = (
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|"
        "Test Issue|NASDAQ Symbol\n"
        + "\n".join(
            f"{symbol}|{symbol} Common Stock|N|{symbol}|N|100|N|{symbol}"
            for symbol in US_SYMBOLS[2:]
        )
        + "\nFile Creation Time: 082120260125|||||||\n"
    ).encode()
    registry = json.dumps({
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [
            [index + 1, symbol, symbol, "Nasdaq" if index < 2 else "NYSE"]
            for index, symbol in enumerate(US_SYMBOLS)
        ],
    }).encode()
    return listed, other, registry


def _us_deps():
    from tokmo.facts.official_membership_live import LiveOutcomeV1
    from tokmo.facts.quant_recurring_membership import EndpointKindV1
    from tokmo.values.official_membership_observation import OfficialSicSnapshotV1

    listed, other, registry = _us_bodies()

    def membership_fetch(*, claim, identity):
        del claim
        if identity.endpoint_kind is EndpointKindV1.SEC_SUBMISSIONS:
            index = int(identity.cik)
            symbol = US_SYMBOLS[index - 1]
            body = json.dumps({
                "cik": index,
                "sic": "3571",
                "tickers": [symbol],
                "exchanges": ["Nasdaq" if index < 3 else "NYSE"],
            }).encode()
        else:
            body = {
                EndpointKindV1.NASDAQ_LISTED: listed,
                EndpointKindV1.OTHER_LISTED: other,
                EndpointKindV1.SEC_REGISTRY: registry,
            }[identity.endpoint_kind]
        return LiveOutcomeV1(True, "OK", body, NOW, 200)

    return SimpleNamespace(
        clock=lambda: NOW,
        membership_fetch=membership_fetch,
        sec_user_agent=lambda: "fixture contact@example.test",
        sic_snapshot_loader=lambda: OfficialSicSnapshotV1(
            "sec-sic-html-v1",
            "RECONSTRUCTED_CONTRACT",
            ("3571",),
            "fixture-sec-sic",
        ),
    )


def _us_context():
    from tokmo.values.quant_profiles import (
        US_PROFILE_ID,
        US_UNIVERSE_POLICY_VERSION,
        QUANT_PROFILE_REGISTRY,
        QuantProfileDueContextV1,
        QuantProfileDueDecisionV1,
        QuantProfileDueIdentityV1,
    )
    from tokmo.values.session_gate import Market

    identity = QuantProfileDueIdentityV1(
        US_PROFILE_ID, POLICY_VERSION, Market.US, SESSION,
        US_UNIVERSE_POLICY_VERSION,
    )
    return QuantProfileDueContextV1(
        profile_id=US_PROFILE_ID,
        decision=QuantProfileDueDecisionV1.DUE,
        identity=identity,
        completed_session_date=SESSION,
        local_session_date=SESSION,
        predicted_symbols=US_SYMBOLS,
        deadline_margin_seconds=90,
        membership_plan_starts=QUANT_PROFILE_REGISTRY[
            US_PROFILE_ID
        ].membership_plan_starts(len(US_SYMBOLS)),
        membership_starts_ceiling=_policy().request_limits.membership_starts,
    )


def test_us_adapter_plan_is_byte_equal_to_v1_membership_operation_plan():
    from tokmo.facts.quant_recurring_membership import membership_operation_plan
    from tokmo.programs.quant_profile_us import us_profile_membership_plan

    assert us_profile_membership_plan(US_SYMBOLS, SESSION) == (
        membership_operation_plan(US_SYMBOLS, SESSION)
    )


def test_us_adapter_runs_the_v1_lane_wires_into_the_v2_store(tmp_path):
    from tokmo.facts.quant_recurring_membership import membership_operation_plan
    from tokmo.programs.quant_profile_us import run_us_profile_membership
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    executor = _Executor()
    receipts = _store(tmp_path)
    context = _us_context()
    execution = run_us_profile_membership(
        root=tmp_path,
        deps=_us_deps(),
        policy=_policy(),
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        context=context,
        receipts=receipts,
    )
    assert execution.membership.status == "ok"
    assert execution.membership.membership_starts == 7
    assert tuple(executor.starts) == membership_operation_plan(
        US_SYMBOLS, SESSION,
    )
    row = receipts.read_due(context.identity.identity_digest)
    assert row.state is QuantTerminalStateV1.CLAIMED
    assert row.profile_id == "quant-us-nyse-nasdaq-v1"
    assert row.market == "US"
    # V1 evidence DB is never created by the profile path
    assert not (tmp_path / "ops" / "quant_recurring.sqlite3").exists()


def test_us_bridge_material_carries_sec_ticker_form_boundary_mapping(tmp_path):
    from tokmo.facts.quant_membership import sec_ticker_form
    from tokmo.programs.quant_profile_us import (
        run_us_profile_membership,
        us_membership_material,
    )
    from tokmo.values.quant_profiles import decode_membership_material

    assert sec_ticker_form("BRK.B") == "BRK-B"  # boundary rule, reused exactly
    receipts = _store(tmp_path)
    context = _us_context()
    execution = run_us_profile_membership(
        root=tmp_path,
        deps=_us_deps(),
        policy=_policy(),
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=_Executor(),
        context=context,
        receipts=receipts,
    )
    material = us_membership_material(
        profile_identity=context.identity,
        acquisition=execution.membership.acquisition,
        symbols=US_SYMBOLS,
    )
    decoded = decode_membership_material(material)
    assert decoded["profile_id"] == "quant-us-nyse-nasdaq-v1"
    assert [row["symbol"] for row in decoded["members"]] == list(US_SYMBOLS)
    assert all(
        row["sec_ticker_form"] == sec_ticker_form(row["symbol"])
        for row in decoded["members"]
    )
    assert {row["venue"] for row in decoded["members"]} == {"NASDAQ", "NYSE"}
    assert {row["security_type"] for row in decoded["members"]} == {
        "COMMON_EQUITY",
    }
