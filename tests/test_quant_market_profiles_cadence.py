"""Slice 6/7 — shared orchestrator through public ``run_production_cadence_once``.

Fixture deps only (``build_fixture_cadence_deps`` + ``dataclasses.replace``),
mirroring test_quant_recurring_architecture.py style. All symbols are
synthetic / RECONSTRUCTED_CONTRACT (Plan/SDS §13).
"""

import ast
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
SLOT = datetime(2026, 8, 21, 6, 45, tzinfo=UTC)
SESSION = date(2026, 8, 21)
KR_SYMBOLS = ("RCS001", "RCS002")
US_SYMBOLS = ("AAA", "BBB")
PROFILES_POLICY_ID = "cadence-300s-quant-profiles-shadow-v1"


def _isolate(monkeypatch):
    import tokmo.programs.production_cadence as module

    monkeypatch.setattr(
        module,
        "run_market_regime_shadow",
        lambda **kwargs: ("skipped", "test_isolation"),
    )
    return module


def _fixture(*, kr_after, us_synthetic_post_close, kr_symbols, us_symbols):
    kr_calendar = {
        "regular_start": "2026-08-21T00:00:00Z",
        "regular_end": "2026-08-21T06:30:00Z",
    }
    if kr_after:
        kr_calendar.update(
            {
                "after_start": "2026-08-21T06:30:00Z",
                "after_end": "2026-08-21T08:00:00Z",
            }
        )
    if us_synthetic_post_close:
        us_calendar = {
            "regular_start": "2026-08-21T01:00:00Z",
            "regular_end": "2026-08-21T02:00:00Z",
            "after_start": "2026-08-21T02:00:00Z",
            "after_end": "2026-08-21T23:00:00Z",
        }
    else:
        us_calendar = {
            "regular_start": "2026-08-21T13:30:00Z",
            "regular_end": "2026-08-21T20:00:00Z",
        }
    holdings = [
        {
            "symbol": symbol,
            "quantity": "1",
            "avg_price": "100",
            "currency": "KRW",
            "market_raw": "KR",
        }
        for symbol in kr_symbols
    ] + [
        {
            "symbol": symbol,
            "quantity": "1",
            "avg_price": "100",
            "currency": "USD",
            "market_raw": "US",
        }
        for symbol in us_symbols
    ]
    return {
        "token": "fixture-bearer",
        "calendar": {"KR": kr_calendar, "US": us_calendar},
        "accounts": [{"seed": "1", "holdings": holdings}],
        "quotes": {},
        "orders": {},
    }


def _seed_prior_holdings(root, *, kr_symbols=(), us_symbols=()):
    from tokmo.values.current_holdings import (
        AccountHoldingsV1,
        AccountScopeIdentityV1,
        CurrentHoldingsV1,
        HoldingItemV1,
        canonical_holdings_bytes,
    )

    scope = "toss-acct-" + "d" * 24
    items = tuple(
        HoldingItemV1(symbol, "1", "100", "KRW", "KR", None) for symbol in kr_symbols
    ) + tuple(
        HoldingItemV1(symbol, "1", "100", "USD", "US", None) for symbol in us_symbols
    )
    state = CurrentHoldingsV1(
        1,
        (AccountHoldingsV1(scope, 1, items),),
        True,
        SLOT - timedelta(minutes=5),
        {scope: AccountScopeIdentityV1(scope, "brokerage", 1, None)},
    )
    (root / "stores").mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / "stores" / "current_holdings.json").write_bytes(
        canonical_holdings_bytes(state)
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


def _stocks_fetch(calls, *, mismatch=False):
    from tokmo.programs.cadence_wire import require_started_claim

    def stocks_membership_fetch(*, claim, bearer, symbols):
        require_started_claim(claim, "MEMBERSHIP_TOSS_STOCKS")
        calls.append(tuple(symbols))
        del bearer
        answered = symbols[:-1] if mismatch else symbols
        return SimpleNamespace(
            code="success",
            value=tuple(_stock_info(symbol) for symbol in answered),
        )

    return stocks_membership_fetch


def _candles_fetch(calls):
    from tokmo.programs.cadence_wire import require_started_claim

    def stock_candles_fetch(*, claim, bearer, symbol, before, adjusted):
        require_started_claim(claim, "STOCK_CANDLES_DAILY")
        del bearer, before
        calls.append((symbol, adjusted))
        return SimpleNamespace(
            code="success",
            value=SimpleNamespace(
                candles=tuple(
                    SimpleNamespace(
                        timestamp=SLOT - timedelta(days=index + 1),
                        close_price="100",
                        currency="KRW",
                    )
                    for index in range(3)
                )
            ),
        )

    return stock_candles_fetch


def _us_membership_fetch(*, fail=False):
    from tokmo.facts.official_membership_live import LiveOutcomeV1
    from tokmo.facts.quant_recurring_membership import EndpointKindV1

    listed = (
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|"
        "Round Lot Size|ETF|NextShares\n"
        + "\n".join(
            f"{symbol}|{symbol} Common Stock|Q|N|N|100|N|N" for symbol in US_SYMBOLS
        )
        + "\nFile Creation Time: 082120260125|||||||\n"
    ).encode()
    other = (
        b"ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|"
        b"Test Issue|NASDAQ Symbol\n"
        b"ZZZ|ZZZ Common Stock|N|ZZZ|N|100|N|ZZZ\n"
        b"File Creation Time: 082120260125|||||||\n"
    )
    registry = json.dumps(
        {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [
                [index + 1, symbol, symbol, "Nasdaq"]
                for index, symbol in enumerate(US_SYMBOLS)
            ],
        }
    ).encode()

    def membership_fetch(*, claim, identity):
        del claim
        if fail:
            raise RuntimeError("fixture membership outage")
        if identity.endpoint_kind is EndpointKindV1.SEC_SUBMISSIONS:
            index = int(identity.cik)
            symbol = US_SYMBOLS[index - 1]
            body = json.dumps(
                {
                    "cik": index,
                    "sic": "3571",
                    "tickers": [symbol],
                    "exchanges": ["Nasdaq"],
                }
            ).encode()
        elif identity.endpoint_kind is EndpointKindV1.NASDAQ_LISTED:
            body = listed
        elif identity.endpoint_kind is EndpointKindV1.OTHER_LISTED:
            body = other
        else:
            body = registry
        return LiveOutcomeV1(True, "OK", body, SLOT + timedelta(seconds=1), 200)

    return membership_fetch


def _sic_snapshot():
    from tokmo.values.official_membership_observation import OfficialSicSnapshotV1

    return OfficialSicSnapshotV1(
        "sec-sic-html-v1",
        "RECONSTRUCTED_CONTRACT",
        ("3571",),
        "fixture-sec-sic",
    )


def _deps(
    fixture,
    *,
    stocks_calls,
    candle_calls,
    clock_slot,
    stocks_mismatch=False,
    us_fail=False,
):
    from tokmo.programs.cadence_fixture_deps import build_fixture_cadence_deps

    deps, probe = build_fixture_cadence_deps(
        fixture=fixture,
        clock=lambda: clock_slot["value"] + timedelta(seconds=1),
    )
    deps = replace(
        deps,
        stocks_membership_fetch=_stocks_fetch(
            stocks_calls,
            mismatch=stocks_mismatch,
        ),
        stock_candles_fetch=_candles_fetch(candle_calls),
        membership_fetch=_us_membership_fetch(fail=us_fail),
        sic_snapshot_loader=_sic_snapshot,
    )
    return deps, probe


def _run(module, root, deps, slot, *, policy_version=PROFILES_POLICY_ID):
    return module.run_production_cadence_once(
        root=root,
        policy_version=policy_version,
        scheduled_for_utc=slot,
        observed_now_utc=slot + timedelta(seconds=1),
        deps=deps,
    )


def _stages(result):
    return {item.stage: (item.status, item.reason) for item in result.stages}


def _profile_store(root):
    from tokmo.records.quant_profile_receipts import QuantProfileReceiptStore

    return QuantProfileReceiptStore(root / "ops" / "quant_profile_receipts.sqlite3")


def _kr_digest():
    from tokmo.values.quant_profiles import (
        KR_PROFILE_ID,
        KR_UNIVERSE_POLICY_VERSION,
        QuantProfileDueIdentityV1,
    )
    from tokmo.values.session_gate import Market

    return QuantProfileDueIdentityV1(
        KR_PROFILE_ID,
        PROFILES_POLICY_ID,
        Market.KR,
        SESSION,
        KR_UNIVERSE_POLICY_VERSION,
    ).identity_digest


def _us_digest():
    from tokmo.values.quant_profiles import (
        US_PROFILE_ID,
        US_UNIVERSE_POLICY_VERSION,
        QuantProfileDueIdentityV1,
    )
    from tokmo.values.session_gate import Market

    return QuantProfileDueIdentityV1(
        US_PROFILE_ID,
        PROFILES_POLICY_ID,
        Market.US,
        SESSION,
        US_UNIVERSE_POLICY_VERSION,
    ).identity_digest


def test_default_market_policy_creates_no_v2_store_and_no_quant_wires(
    monkeypatch,
    tmp_path,
):
    module = _isolate(monkeypatch)
    root = tmp_path / "shadow"
    stocks_calls, candle_calls = [], []
    clock_slot = {"value": SLOT}
    deps, probe = _deps(
        _fixture(
            kr_after=True,
            us_synthetic_post_close=False,
            kr_symbols=KR_SYMBOLS,
            us_symbols=(),
        ),
        stocks_calls=stocks_calls,
        candle_calls=candle_calls,
        clock_slot=clock_slot,
    )
    _seed_prior_holdings(root, kr_symbols=KR_SYMBOLS)
    result = _run(
        module,
        root,
        deps,
        SLOT,
        policy_version="cadence-300s-market-portfolio-v1",
    )
    stages = _stages(result)
    assert stages["quant_membership"] == ("skipped", "policy_not_pinned")
    assert stages["quant_shadow"] == ("skipped", "policy_not_pinned")
    assert not (root / "ops" / "quant_profile_receipts.sqlite3").exists()
    assert not (root / "ops" / "quant_recurring.sqlite3").exists()
    assert stocks_calls == [] and candle_calls == []
    assert "MEMBERSHIP_TOSS_STOCKS" not in probe.calls


def test_legacy_w4_policy_keeps_v1_lane_tokens_and_never_creates_v2_store(
    monkeypatch,
    tmp_path,
):
    module = _isolate(monkeypatch)
    root = tmp_path / "shadow"
    stocks_calls, candle_calls = [], []
    clock_slot = {"value": SLOT}
    deps, _ = _deps(
        _fixture(
            kr_after=True,
            us_synthetic_post_close=False,
            kr_symbols=KR_SYMBOLS,
            us_symbols=(),
        ),
        stocks_calls=stocks_calls,
        candle_calls=candle_calls,
        clock_slot=clock_slot,
    )
    _seed_prior_holdings(root, kr_symbols=KR_SYMBOLS)
    result = _run(
        module,
        root,
        deps,
        SLOT,
        policy_version="cadence-300s-market-quant4-shadow-v1",
    )
    stages = _stages(result)
    # V1 lane tokens are un-prefixed; the US session is not post-close here.
    assert stages["quant_membership"] == ("skipped", "no_completed_session")
    assert stages["quant_shadow"] == ("skipped", "no_completed_session")
    assert not (root / "ops" / "quant_profile_receipts.sqlite3").exists()
    assert not (root / "ops" / "quant_recurring.sqlite3").exists()
    assert stocks_calls == []


def test_kr_due_settles_truthful_no_pack_paused_through_public_runner(
    monkeypatch,
    tmp_path,
):
    module = _isolate(monkeypatch)
    root = tmp_path / "shadow"
    stocks_calls, candle_calls = [], []
    clock_slot = {"value": SLOT}
    deps, probe = _deps(
        _fixture(
            kr_after=True,
            us_synthetic_post_close=False,
            kr_symbols=KR_SYMBOLS,
            us_symbols=(),
        ),
        stocks_calls=stocks_calls,
        candle_calls=candle_calls,
        clock_slot=clock_slot,
    )
    _seed_prior_holdings(root, kr_symbols=KR_SYMBOLS)
    result = _run(module, root, deps, SLOT)
    stages = _stages(result)
    assert stages["quant_membership"] == ("ok", "kr:acquired")
    assert stages["quant_shadow"] == ("ok", "kr:kr_fundamentals_paused")
    assert stocks_calls == [KR_SYMBOLS]
    assert sorted(symbol for symbol, _ in candle_calls) == list(KR_SYMBOLS)

    from tokmo.values.quant_recurring import QuantTerminalStateV1

    store = _profile_store(root)
    due = store.read_due(_kr_digest())
    assert due.state is QuantTerminalStateV1.NO_PACK
    assert due.reason_code == "kr_fundamentals_paused"
    row = store.read_result(_kr_digest())
    assert (
        row.membership_starts,
        row.candle_page1_starts,
        row.candle_page2_starts,
    ) == (1, 2, 0)
    assert (row.cohort_size, row.admissible_count) == (2, 2)
    assert row.limitations == ("KR_FUNDAMENTALS_PAUSED",)
    assert row.pack_digest == ""
    # V1 evidence stores stay untouched
    assert not (root / "ops" / "quant_recurring.sqlite3").exists()
    assert not (root / "official-membership" / "observations.sqlite3").exists()


def test_both_due_serializes_kr_first_then_us_then_zero_io_dedupe(
    monkeypatch,
    tmp_path,
):
    module = _isolate(monkeypatch)
    root = tmp_path / "shadow"
    stocks_calls, candle_calls = [], []
    clock_slot = {"value": SLOT}
    fixture = _fixture(
        kr_after=True,
        us_synthetic_post_close=True,
        kr_symbols=KR_SYMBOLS,
        us_symbols=US_SYMBOLS,
    )
    deps, probe = _deps(
        fixture,
        stocks_calls=stocks_calls,
        candle_calls=candle_calls,
        clock_slot=clock_slot,
    )
    _seed_prior_holdings(root, kr_symbols=KR_SYMBOLS, us_symbols=US_SYMBOLS)

    from tokmo.values.quant_recurring import QuantTerminalStateV1

    # Tick 1: both profiles are due; only KR (first in fixed order) executes.
    result_1 = _run(module, root, deps, SLOT)
    stages_1 = _stages(result_1)
    assert stages_1["quant_membership"] == ("ok", "kr:acquired")
    assert stages_1["quant_shadow"] == ("ok", "kr:kr_fundamentals_paused")
    store = _profile_store(root)
    assert store.read_due(_kr_digest()).state is QuantTerminalStateV1.NO_PACK
    assert store.read_due(_us_digest()) is None
    membership_starts_1 = dict(probe.calls)

    # Tick 2: KR is already terminal, US executes with the byte-equal V1 plan.
    slot_2 = SLOT + timedelta(seconds=300)
    clock_slot["value"] = slot_2
    result_2 = _run(module, root, deps, slot_2)
    stages_2 = _stages(result_2)
    assert stages_2["quant_membership"] == ("ok", "us:acquired")
    assert stages_2["quant_shadow"] == (
        "skipped",
        "us:fundamental_store_absent",
    )
    us_due = store.read_due(_us_digest())
    assert us_due.state is QuantTerminalStateV1.NO_PACK
    assert us_due.reason_code == "fundamental_store_absent"
    us_row = store.read_result(_us_digest())
    assert us_row.membership_starts == 5  # 3 + N with N=2, byte-equal formula
    assert us_row.symbols == US_SYMBOLS

    # Tick 3: both identities are terminal — zero quant I/O.
    slot_3 = SLOT + timedelta(seconds=600)
    clock_slot["value"] = slot_3
    stocks_before = list(stocks_calls)
    candles_before = list(candle_calls)
    result_3 = _run(module, root, deps, slot_3)
    stages_3 = _stages(result_3)
    assert stages_3["quant_membership"] == (
        "skipped",
        "kr:already_terminal,us:already_terminal",
    )
    assert stages_3["quant_shadow"] == (
        "skipped",
        "kr:already_terminal,us:already_terminal",
    )
    assert stocks_calls == stocks_before
    assert candle_calls == candles_before


def test_executed_profile_failure_is_partial_and_other_receipt_intact(
    monkeypatch,
    tmp_path,
):
    module = _isolate(monkeypatch)
    root = tmp_path / "shadow"
    clock_slot = {"value": SLOT}
    fixture = _fixture(
        kr_after=True,
        us_synthetic_post_close=True,
        kr_symbols=KR_SYMBOLS,
        us_symbols=US_SYMBOLS,
    )
    stocks_calls, candle_calls = [], []
    deps, _ = _deps(
        fixture,
        stocks_calls=stocks_calls,
        candle_calls=candle_calls,
        clock_slot=clock_slot,
    )
    _seed_prior_holdings(root, kr_symbols=KR_SYMBOLS, us_symbols=US_SYMBOLS)

    from tokmo.values.production_cadence import CadenceOutcome
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    # Tick 1: KR terminal lands.
    _run(module, root, deps, SLOT)
    store = _profile_store(root)
    kr_before = store.read_due(_kr_digest())
    assert kr_before.state is QuantTerminalStateV1.NO_PACK

    # Tick 2: US executes and its membership source fails => tick PARTIAL.
    slot_2 = SLOT + timedelta(seconds=300)
    clock_slot["value"] = slot_2
    failing_deps, _ = _deps(
        fixture,
        stocks_calls=stocks_calls,
        candle_calls=candle_calls,
        clock_slot=clock_slot,
        us_fail=True,
    )
    result = _run(module, root, failing_deps, slot_2)
    stages = _stages(result)
    assert stages["quant_membership"] == (
        "failed",
        "us:source_failure:fixture membership outage",
    )
    assert stages["quant_shadow"][0] == "skipped"
    assert result.outcome is CadenceOutcome.PARTIAL
    assert result.reason == "stage_failed_quant_membership"
    assert result.exit_code == 2

    us_due = store.read_due(_us_digest())
    assert us_due.state is QuantTerminalStateV1.FAILED
    # The KR receipt from tick 1 is byte-identical — never rewritten.
    assert store.read_due(_kr_digest()) == kr_before


def test_kr_membership_echo_failure_records_failed_stage_and_partial(
    monkeypatch,
    tmp_path,
):
    module = _isolate(monkeypatch)
    root = tmp_path / "shadow"
    stocks_calls, candle_calls = [], []
    clock_slot = {"value": SLOT}
    deps, _ = _deps(
        _fixture(
            kr_after=True,
            us_synthetic_post_close=False,
            kr_symbols=KR_SYMBOLS,
            us_symbols=(),
        ),
        stocks_calls=stocks_calls,
        candle_calls=candle_calls,
        clock_slot=clock_slot,
        stocks_mismatch=True,
    )
    _seed_prior_holdings(root, kr_symbols=KR_SYMBOLS)

    from tokmo.values.production_cadence import CadenceOutcome
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    result = _run(module, root, deps, SLOT)
    stages = _stages(result)
    assert stages["quant_membership"] == (
        "failed",
        "kr:source_failure:cohort_echo",
    )
    assert result.outcome is CadenceOutcome.PARTIAL
    assert result.reason == "stage_failed_quant_membership"
    store = _profile_store(root)
    assert store.read_due(_kr_digest()).state is QuantTerminalStateV1.FAILED
    assert candle_calls == []


def test_kr_acceptance_predicate_passes_compliant_terminal_and_fails_each_clause(
    monkeypatch,
    tmp_path,
):
    module = _isolate(monkeypatch)
    root = tmp_path / "shadow"
    stocks_calls, candle_calls = [], []
    clock_slot = {"value": SLOT}
    deps, _ = _deps(
        _fixture(
            kr_after=True,
            us_synthetic_post_close=False,
            kr_symbols=KR_SYMBOLS,
            us_symbols=(),
        ),
        stocks_calls=stocks_calls,
        candle_calls=candle_calls,
        clock_slot=clock_slot,
    )
    _seed_prior_holdings(root, kr_symbols=KR_SYMBOLS)
    tick_result = _run(module, root, deps, SLOT)

    from tokmo.values.quant_profiles import (
        QuantProfileEffectEvidenceV1,
        kr_profile_acceptance_v1,
    )
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    store = _profile_store(root)
    due = store.read_due(_kr_digest())
    result = store.read_result(_kr_digest())
    operation_started = dict(tick_result.wire["operation_started"])
    evidence = QuantProfileEffectEvidenceV1(
        membership_wires_settled=operation_started.get(
            "MEMBERSHIP_TOSS_STOCKS",
            0,
        ),
        candle_wires_settled=operation_started.get("STOCK_CANDLES_DAILY", 0),
        alert_transports=0,
        order_transports=0,
        model_calls=0,
        writes_outside_profile_store=0,
    )
    verdict = kr_profile_acceptance_v1(
        due=due,
        result=result,
        evidence=evidence,
    )
    assert verdict.passed
    assert verdict.failed_clauses == ()

    def clause(**overrides):
        due_row = replace(
            due,
            **{
                key: value
                for key, value in overrides.items()
                if key in ("state", "reason_code")
            },
        )
        result_row = replace(
            result,
            **{
                key: value
                for key, value in overrides.items()
                if key not in ("state", "reason_code")
            },
        )
        return kr_profile_acceptance_v1(
            due=due_row,
            result=result_row,
            evidence=evidence,
        )

    # B2: a consistently-hashed material for another session must fail the
    # typed binding clause — hash equality alone is not acceptance.
    from tokmo.values.quant_profiles import (
        KR_PROFILE_ID,
        decode_membership_material,
        encode_membership_material,
    )

    decoded = decode_membership_material(result.membership_material)
    cross_session = encode_membership_material(
        profile_id=KR_PROFILE_ID,
        policy_version=decoded["policy_version"],
        completed_session_date=date(2026, 8, 20),
        request_identity="membership:toss-stocks:2026-08-20",
        observed_at=SLOT,
        members=tuple(decoded["members"]),
    )
    cases = {
        "terminal_state": {"state": QuantTerminalStateV1.SUCCEEDED},
        "reason_code": {"reason_code": "holdings_mutated"},
        "degraded": {"degraded": False},
        "limitation": {"limitations": ("OTHER",)},
        "admissible_count": {"admissible_count": 1},
        "membership_starts": {"membership_starts": 2},
        "candle_pages": {"candle_page1_starts": 1},
        "digests": {
            "membership_material_digest": sha256(b"unbound").hexdigest(),
        },
        "material_binding": {
            "membership_material": cross_session,
            "membership_material_digest": sha256(cross_session).hexdigest(),
        },
        "pack_digest": {"pack_digest": sha256(b"pack").hexdigest()},
    }
    for expected_clause, overrides in cases.items():
        broken = clause(**overrides)
        assert not broken.passed, expected_clause
        assert expected_clause in broken.failed_clauses, expected_clause


def test_us_acceptance_predicate_requires_succeeded_plus_invariants(
    monkeypatch,
    tmp_path,
):
    module = _isolate(monkeypatch)
    root = tmp_path / "shadow"
    stocks_calls, candle_calls = [], []
    clock_slot = {"value": SLOT}
    fixture = _fixture(
        kr_after=False,
        us_synthetic_post_close=True,
        kr_symbols=(),
        us_symbols=US_SYMBOLS,
    )
    deps, _ = _deps(
        fixture,
        stocks_calls=stocks_calls,
        candle_calls=candle_calls,
        clock_slot=clock_slot,
    )
    _seed_prior_holdings(root, us_symbols=US_SYMBOLS)
    result = _run(module, root, deps, SLOT)
    stages = _stages(result)
    assert stages["quant_membership"] == ("ok", "us:acquired")
    assert stages["quant_shadow"] == (
        "skipped",
        "us:fundamental_store_absent",
    )

    from tokmo.values.quant_profiles import us_profile_acceptance_v1
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    store = _profile_store(root)
    due = store.read_due(_us_digest())
    row = store.read_result(_us_digest())
    # The truthful NO_PACK terminal fails US acceptance — no SUCCEEDED claim.
    verdict = us_profile_acceptance_v1(due=due, result=row)
    assert not verdict.passed
    assert "terminal_state" in verdict.failed_clauses

    # A compliant SUCCEEDED shape passes; each broken invariant fails.
    succeeded_due = replace(due, state=QuantTerminalStateV1.SUCCEEDED)
    succeeded_row = replace(
        row,
        admissible_count=2,
        candle_page1_starts=2,
        pack_digest=sha256(b"pack").hexdigest(),
    )
    assert us_profile_acceptance_v1(
        due=succeeded_due,
        result=succeeded_row,
    ).passed
    assert (
        "membership_starts"
        in us_profile_acceptance_v1(
            due=succeeded_due,
            result=replace(succeeded_row, membership_starts=6),
        ).failed_clauses
    )
    assert (
        "pack_digest"
        in us_profile_acceptance_v1(
            due=succeeded_due,
            result=replace(succeeded_row, pack_digest=""),
        ).failed_clauses
    )


# ---------------------------------------------------------------------------
# Slice 7 — composition seam + architecture guards (§13)
# ---------------------------------------------------------------------------

_PROFILE_MODULES = (
    "src/tokmo/values/quant_profiles.py",
    "src/tokmo/facts/quant_kr_membership.py",
    "src/tokmo/programs/quant_profile_kr.py",
    "src/tokmo/programs/quant_profile_us.py",
    "src/tokmo/programs/quant_market_profiles.py",
)


def _imports_of(path):
    tree = ast.parse((ROOT / path).read_text())
    modules = set()
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            names.setdefault(node.module, set()).update(
                alias.name for alias in node.names
            )
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules, names


def test_profile_modules_import_no_live_transports_or_source_parsers():
    for path in _PROFILE_MODULES:
        modules, names = _imports_of(path)
        live = {name for name in modules if name.endswith("_live")}
        assert not live, (path, sorted(live))
        assert "tokmo.facts.toss_read_parsers" not in modules, path
        parser_symbols = {
            name
            for name in names.get("tokmo.facts.quant_membership", set())
            if name.startswith("parse_")
        }
        assert not parser_symbols, (path, sorted(parser_symbols))


def test_v1_schema_bytes_and_stage_order_are_unchanged():
    from tokmo.records import quant_recurring_receipts
    from tokmo.values.production_cadence import CADENCE_STAGE_ORDER

    assert sha256(quant_recurring_receipts._SCHEMA.encode()).hexdigest() == (
        "3916ee8a5e3406bd728f2aa7d7b16b784bc4b6aebe0e9c58e1513a088ebf3b1b"
    )
    assert len(CADENCE_STAGE_ORDER) == 19
    assert CADENCE_STAGE_ORDER[5] == "quant_membership"
    assert CADENCE_STAGE_ORDER[-2:] == ("quant_shadow", "sink")


def test_real_composition_binds_stocks_membership_fetch_claim_gated(
    monkeypatch,
    tmp_path,
):
    from tokmo import composition

    (tmp_path / composition.TOSS_ENV_NAME).write_text(
        "TOSS_CLIENT_ID=fixture-id\nTOSS_CLIENT_SECRET=fixture-secret\n"
    )
    watchlists = tmp_path / "watchlists"
    watchlists.mkdir()
    for market in ("KR", "US"):
        (watchlists / f"{market}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "market": market,
                    "entries": [],
                }
            )
        )
    deps = composition.build_production_cadence_deps(
        project_root=tmp_path,
        shadow_root=tmp_path / "shadow",
        clock=lambda: datetime(2026, 8, 21, tzinfo=UTC),
    )
    assert callable(deps.stocks_membership_fetch)
    # an unreceipted callback is impossible: claim gate before any transport
    with pytest.raises(PermissionError):
        deps.stocks_membership_fetch(
            claim=None,
            bearer="x",
            symbols=("RCS001",),
        )


# ---------------------------------------------------------------------------
# G2 targeted repairs — B1/B2/M1/M2
# ---------------------------------------------------------------------------


def _preclaim_kr(root, *, claimed_at):
    """Pre-create a CLAIMED KR row exactly as a crashed prior tick would."""
    from tokmo.values.quant_profiles import (
        KR_PROFILE_ID,
        KR_UNIVERSE_POLICY_VERSION,
        QuantProfileDueIdentityV1,
    )
    from tokmo.values.session_gate import Market

    identity = QuantProfileDueIdentityV1(
        KR_PROFILE_ID,
        PROFILES_POLICY_ID,
        Market.KR,
        SESSION,
        KR_UNIVERSE_POLICY_VERSION,
    )
    (root / "ops").mkdir(mode=0o700, parents=True, exist_ok=True)
    store = _profile_store(root)
    lease = store.acquire_identity_lease(identity.identity_digest)
    with lease:
        store.claim(
            identity,
            owner_invocation_id="crashed-prior-invocation",
            owner_lease_key=lease.key,
            claimed_at=claimed_at,
            identity_lease=lease,
        )
    return identity


def test_b1_stale_claim_is_reconciled_to_unknown_with_zero_quant_wire(
    monkeypatch,
    tmp_path,
):
    module = _isolate(monkeypatch)
    root = tmp_path / "shadow"
    stocks_calls, candle_calls = [], []
    clock_slot = {"value": SLOT}
    deps, _ = _deps(
        _fixture(
            kr_after=True,
            us_synthetic_post_close=False,
            kr_symbols=KR_SYMBOLS,
            us_symbols=(),
        ),
        stocks_calls=stocks_calls,
        candle_calls=candle_calls,
        clock_slot=clock_slot,
    )
    _seed_prior_holdings(root, kr_symbols=KR_SYMBOLS)
    # stale: older than child_timeout + termination_margin (300s)
    _preclaim_kr(root, claimed_at=SLOT - timedelta(seconds=1000))

    from tokmo.values.quant_recurring import QuantTerminalStateV1

    result = _run(module, root, deps, SLOT)
    stages = _stages(result)
    due = _profile_store(root).read_due(_kr_digest())
    assert due.state is QuantTerminalStateV1.UNKNOWN
    assert due.reason_code == "stale_claim"
    # UNKNOWN blocks re-execution: truthful already-terminal, zero quant wire
    assert stages["quant_membership"][1].startswith("kr:already_terminal")
    assert stocks_calls == [] and candle_calls == []


def test_b1_fresh_claim_stays_in_flight_with_zero_quant_wire(
    monkeypatch,
    tmp_path,
):
    module = _isolate(monkeypatch)
    root = tmp_path / "shadow"
    stocks_calls, candle_calls = [], []
    clock_slot = {"value": SLOT}
    deps, _ = _deps(
        _fixture(
            kr_after=True,
            us_synthetic_post_close=False,
            kr_symbols=KR_SYMBOLS,
            us_symbols=(),
        ),
        stocks_calls=stocks_calls,
        candle_calls=candle_calls,
        clock_slot=clock_slot,
    )
    _seed_prior_holdings(root, kr_symbols=KR_SYMBOLS)
    # fresh: inside the child_timeout + termination_margin window
    _preclaim_kr(root, claimed_at=SLOT - timedelta(seconds=10))

    from tokmo.values.quant_recurring import QuantTerminalStateV1

    result = _run(module, root, deps, SLOT)
    stages = _stages(result)
    due = _profile_store(root).read_due(_kr_digest())
    # a still-fresh CLAIMED row is neither re-claimed nor called terminal
    assert due.state is QuantTerminalStateV1.CLAIMED
    assert due.owner_invocation_id == "crashed-prior-invocation"
    assert stages["quant_membership"][0] == "skipped"
    assert stages["quant_membership"][1].startswith("kr:claim_in_flight")
    assert stocks_calls == [] and candle_calls == []


def test_m1_kr_candle_provider_exception_is_terminal_failed_and_partial(
    monkeypatch,
    tmp_path,
):
    module = _isolate(monkeypatch)
    root = tmp_path / "shadow"
    stocks_calls = []
    clock_slot = {"value": SLOT}
    deps, _ = _deps(
        _fixture(
            kr_after=True,
            us_synthetic_post_close=False,
            kr_symbols=KR_SYMBOLS,
            us_symbols=(),
        ),
        stocks_calls=stocks_calls,
        candle_calls=[],
        clock_slot=clock_slot,
    )

    from tokmo.programs.cadence_wire import require_started_claim

    def broken_candles(*, claim, bearer, symbol, before, adjusted):
        require_started_claim(claim, "STOCK_CANDLES_DAILY")
        raise RuntimeError("fixture candle outage")

    deps = replace(deps, stock_candles_fetch=broken_candles)
    _seed_prior_holdings(root, kr_symbols=KR_SYMBOLS)

    from tokmo.values.production_cadence import CadenceOutcome
    from tokmo.values.quant_profiles import kr_profile_acceptance_v1
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    result = _run(module, root, deps, SLOT)
    stages = _stages(result)
    assert stages["quant_membership"] == ("ok", "kr:acquired")
    assert stages["quant_shadow"] == ("failed", "kr:candle_unavailable")
    assert result.outcome is CadenceOutcome.PARTIAL
    assert result.reason == "stage_failed_quant_shadow"
    assert result.exit_code == 2

    store = _profile_store(root)
    due = store.read_due(_kr_digest())
    assert due.state is QuantTerminalStateV1.FAILED
    assert due.reason_code == "candle_unavailable"
    assert store.read_result(_kr_digest()) is None
    # no false acceptance from a failed terminal
    assert not kr_profile_acceptance_v1(due=due, result=None).passed
    # the other profile's receipts are untouched
    assert store.read_due(_us_digest()) is None


def test_m1_kr_candle_budget_exhaustion_is_terminal_failed_and_partial(
    monkeypatch,
    tmp_path,
):
    module = _isolate(monkeypatch)
    # shrink the quant candle lane so exhaustion is reachable (mirrors the
    # established _inject_quant_limits monkeypatch style)
    from tokmo.values.cadence_policy import (
        CADENCE_QUANT_PROFILES_SHADOW_POLICY_V1,
        TickRequestLimitsV1,
    )

    small = replace(
        CADENCE_QUANT_PROFILES_SHADOW_POLICY_V1,
        request_limits=TickRequestLimitsV1(
            total_wire_starts=40,
            daily_candle_starts=6,
            market_daily_candle_starts=3,
            quant_daily_candle_starts=3,
            membership_starts=7,
        ),
    )
    monkeypatch.setattr(
        module,
        "CADENCE_POLICY_REGISTRY",
        {
            small.policy_version: small,
        },
    )
    root = tmp_path / "shadow"
    stocks_calls = []
    clock_slot = {"value": SLOT}
    deps, _ = _deps(
        _fixture(
            kr_after=True,
            us_synthetic_post_close=False,
            kr_symbols=KR_SYMBOLS,
            us_symbols=(),
        ),
        stocks_calls=stocks_calls,
        candle_calls=[],
        clock_slot=clock_slot,
    )

    from tokmo.programs.cadence_wire import require_started_claim

    def paging_candles(*, claim, bearer, symbol, before, adjusted):
        require_started_claim(claim, "STOCK_CANDLES_DAILY")
        del bearer, before, adjusted, symbol
        return SimpleNamespace(
            code="success",
            value=SimpleNamespace(
                candles=tuple(
                    SimpleNamespace(
                        timestamp=SLOT - timedelta(days=index + 1),
                        close_price="100",
                        currency="KRW",
                    )
                    for index in range(200)  # every page is full => page 2 due
                )
            ),
        )

    deps = replace(deps, stock_candles_fetch=paging_candles)
    _seed_prior_holdings(root, kr_symbols=KR_SYMBOLS)

    from tokmo.values.production_cadence import CadenceOutcome
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    result = _run(module, root, deps, SLOT)
    stages = _stages(result)
    assert stages["quant_shadow"] == ("failed", "kr:candle_budget_exhausted")
    assert result.outcome is CadenceOutcome.PARTIAL
    assert result.reason == "stage_failed_quant_shadow"
    assert result.exit_code == 2
    store = _profile_store(root)
    due = store.read_due(_kr_digest())
    assert due.state is QuantTerminalStateV1.FAILED
    assert due.reason_code == "candle_budget_exhausted"
    assert store.read_result(_kr_digest()) is None


def test_m2_kr_acceptance_requires_exact_limitations_and_effect_evidence(
    monkeypatch,
    tmp_path,
):
    module = _isolate(monkeypatch)
    root = tmp_path / "shadow"
    stocks_calls, candle_calls = [], []
    clock_slot = {"value": SLOT}
    deps, _ = _deps(
        _fixture(
            kr_after=True,
            us_synthetic_post_close=False,
            kr_symbols=KR_SYMBOLS,
            us_symbols=(),
        ),
        stocks_calls=stocks_calls,
        candle_calls=candle_calls,
        clock_slot=clock_slot,
    )
    _seed_prior_holdings(root, kr_symbols=KR_SYMBOLS)
    tick_result = _run(module, root, deps, SLOT)

    from tokmo.values.quant_profiles import (
        QuantProfileEffectEvidenceV1,
        kr_profile_acceptance_v1,
    )

    store = _profile_store(root)
    due = store.read_due(_kr_digest())
    row = store.read_result(_kr_digest())

    # promotion-facing evidence built from the tick's actual kernel-backed
    # wire settlement counts, plus the caller's explicit effect attestation
    operation_started = dict(tick_result.wire["operation_started"])
    evidence = QuantProfileEffectEvidenceV1(
        membership_wires_settled=operation_started.get(
            "MEMBERSHIP_TOSS_STOCKS",
            0,
        ),
        candle_wires_settled=operation_started.get("STOCK_CANDLES_DAILY", 0),
        alert_transports=0,
        order_transports=0,
        model_calls=0,
        writes_outside_profile_store=0,
    )
    assert evidence.membership_wires_settled == 1
    assert evidence.candle_wires_settled == 2

    compliant = kr_profile_acceptance_v1(due=due, result=row, evidence=evidence)
    assert compliant.passed and compliant.failed_clauses == ()

    # missing / unbound evidence fails — zero is never assumed
    missing = kr_profile_acceptance_v1(due=due, result=row)
    assert not missing.passed
    assert "effect_evidence" in missing.failed_clauses

    # any nonzero prohibited effect fails
    for field in (
        "alert_transports",
        "order_transports",
        "model_calls",
        "writes_outside_profile_store",
    ):
        dirty = kr_profile_acceptance_v1(
            due=due,
            result=row,
            evidence=replace(evidence, **{field: 1}),
        )
        assert not dirty.passed, field
        assert "prohibited_effects" in dirty.failed_clauses, field

    # settled wires must match the recorded result exactly
    for field, value in (
        ("membership_wires_settled", 0),
        ("candle_wires_settled", 3),
    ):
        mismatched = kr_profile_acceptance_v1(
            due=due,
            result=row,
            evidence=replace(evidence, **{field: value}),
        )
        assert not mismatched.passed, field
        assert "wire_settlement" in mismatched.failed_clauses, field

    # limitations must be exactly ("KR_FUNDAMENTALS_PAUSED",) — an extra
    # limitation is not acceptance-compliant even though it contains the token
    extra = kr_profile_acceptance_v1(
        due=due,
        result=replace(
            row,
            limitations=("KR_FUNDAMENTALS_PAUSED", "RECONSTRUCTED_EXTRA"),
        ),
        evidence=evidence,
    )
    assert not extra.passed
    assert "limitation" in extra.failed_clauses
