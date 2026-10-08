"""Stage 2-F: one OpenMarketMaskV1 flows end-to-end with one exact digest.

The mask is built exactly once from judged verdicts, then the identical
object flows through holdings -> quotes -> trades -> barrier -> candidate
build. Every material pack, every receipt, the barrier result, and the
candidate material binding carry that one digest; the canonical holdings
snapshot stays account-wide while every analysis-facing subject set is
mask-limited (US-only here, KR canonical holding preserved).
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tokmo.facts.toss import TossCampaignOutcome, derive_account_scope
from tokmo.facts.toss_prices import TossPriceQueryOutcome
from tokmo.facts.toss_reads import TossReadOutcome
from tokmo.programs.analysis_barrier import assemble_analysis_materials
from tokmo.programs.candidate_builder import build_candidates, material_binding_digest
from tokmo.values.portfolio_scope import resolve_account_scope_pattern  # N5
from tokmo.programs.holdings_sync import run_holdings_sync
from tokmo.programs.quotes_sync import run_quotes_sync
from tokmo.programs.trades_sync import run_trades_sync
from tokmo.records.current_holdings_store import CurrentHoldingsStore
from tokmo.records.material_pack_store import MaterialPackWriter
from tokmo.records.order_scan_state import OrderScanStateStore
from tokmo.records.receipt_store import ReceiptStore
from tokmo.values.analysis_barrier import MarketReadiness
from tokmo.values.current_holdings import AccountScopeIdentityV1, HoldingItemV1
from tokmo.values.holdings import TossAccountHoldingsFact
from tokmo.values.market_observations import TossCurrentPriceFact
from tokmo.values.cadence_policy import cadence_tick_v1
from tokmo.values.material_pack import JobKind
from tokmo.values.open_market_mask import build_open_market_mask
from tokmo.values.session_gate import Market, SessionVerdict, judge_sessions
from tokmo.values.toss_market_calendar import CalendarSession
from tokmo.values.toss_orders import Order, OrderExecution, PaginatedOrderResponse

NOW = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
TICK = cadence_tick_v1("cadence-300s-v1", NOW)
START = TICK.start_utc
SCOPE = derive_account_scope("123")

US_SESSION = CalendarSession(
    datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc),
    datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc),
)
KR_SESSION = CalendarSession(
    datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc),
    datetime(2026, 8, 3, 6, 30, tzinfo=timezone.utc),
)


def clock():
    return START


def test_one_mask_digest_flows_through_every_stage(tmp_path):
    # Build the mask exactly once, from judged verdicts (US open, KR closed).
    verdicts = (
        SessionVerdict(Market.KR, False, "after_close", KR_SESSION),
        SessionVerdict(Market.US, True, "open", US_SESSION),
    )
    mask = build_open_market_mask(
        policy_version="cadence-300s-v1", scheduled_for_utc=START,
        tick_id=TICK.tick_id, verdicts=verdicts,
        local_dates={Market.KR: date(2026, 8, 3), Market.US: date(2026, 8, 3)},
    )
    assert mask.markets == frozenset({Market.US})

    packs = MaterialPackWriter(tmp_path / "ops", clock=clock)
    receipts = {
        job: ReceiptStore(tmp_path / "ops" / "receipts" / f"{job.value}.json",
                          clock=clock)
        for job in JobKind
    }
    holdings_store = CurrentHoldingsStore(
        tmp_path / "stores" / "current_holdings.json", clock=clock)

    # -- holdings: account-wide canonical snapshot, mask-bound projection ------
    fact = TossAccountHoldingsFact(
        "toss-holdings-v1", START, 1, 1, (
            HoldingItemV1("005930", "5", "70000", "KRW", "KR", None),
            HoldingItemV1("AAPL", "2", "180", "USD", "US", None),
        ),
    )
    outcome = TossCampaignOutcome(
        "success", "holdings", 1, (fact,), (),
        account_identities=(AccountScopeIdentityV1(SCOPE, "brokerage", 1, None),),
        _identity_fingerprints={SCOPE: "a" * 64},
    )
    holdings_result = run_holdings_sync(
        tick=TICK, holdings_provider=lambda: outcome,
        store=holdings_store, pack_writer=packs,
        receipt_store=receipts[JobKind.HOLDINGS],
        reservation_seal=lambda *args: None, incident_writer=lambda *args: None,
        clock=clock, now=START,
        job_lock_path=tmp_path / "locks" / "holdings.lock", mask=mask,
    )
    assert holdings_result.status == "saved"
    canonical = holdings_store.read()
    assert {
        item.symbol for account in canonical.accounts for item in account.holdings
    } == {"005930", "AAPL"}
    assert {
        item.symbol for account in holdings_result.projection.accounts
        for item in account.holdings
    } == {"AAPL"}

    # -- quotes: mask-admitted subjects only ------------------------------------
    @dataclass
    class Save:
        stored_at: datetime
        status: str = "SAVED"
        inserted_count: int = 1
        updated_count: int = 0
        reason: str = "saved"

    fetched = []

    def quotes_provider(symbols):
        fetched.extend(symbols)
        return TossPriceQueryOutcome("success", tuple(
            TossCurrentPriceFact("toss-current-price-v1", symbol, "181.25",
                                 "USD", START, START)
            for symbol in symbols
        ))

    quotes_result = run_quotes_sync(
        tick=TICK, holdings=canonical,
        quotes_provider=quotes_provider,
        quote_store=lambda outcome: Save(START, inserted_count=len(outcome.facts)),
        pack_writer=packs, receipt_store=receipts[JobKind.QUOTES],
        reservation_seal=lambda *args: None, incident_writer=lambda *args: None,
        clock=clock, now=START,
        job_lock_path=tmp_path / "locks" / "quotes.lock", mask=mask,
    )
    assert quotes_result.status == "saved"
    assert fetched == ["AAPL"]

    # -- trades: only open uniquely classified records --------------------------
    order = Order(
        "us-order-1", "AAPL", "SELL", "MARKET", "DAY", "FILLED", "1", "USD",
        START, OrderExecution("1", "181", "181", None, None, START, None),
    )

    class Ledger:
        def append_batch(self, batch, *, import_audit):
            return SimpleNamespace(
                status="SAVED", reason="", inserted_count=len(batch),
                duplicate_count=0,
            )

    class Calendar:
        def regular_session_dates(self, market, anchor_date, count):
            return tuple(anchor_date - timedelta(days=offset)
                         for offset in range(count))

    trades_result = run_trades_sync(
        tick=TICK, holdings=canonical,
        orders_provider=lambda **kwargs: TossReadOutcome(
            "success", PaginatedOrderResponse((order,), None, False)),
        detail_provider=lambda **kwargs: pytest.fail("detail not needed"),
        store=Ledger(),
        scan_state=OrderScanStateStore(tmp_path / "stores" / "scan.sqlite3",
                                       clock=clock),
        pack_writer=packs, receipt_store=receipts[JobKind.TRADES],
        reservation_seal=lambda *args: None, incident_writer=lambda *args: None,
        regular_session_dates=Calendar(), clock=clock, now=START,
        job_lock_path=tmp_path / "locks" / "trades.lock", mask=mask,
    )
    assert trades_result.status == "saved"

    # -- every pack and receipt carries the one exact digest --------------------
    for job in JobKind:
        receipt = receipts[job].read()
        assert receipt.mask_digest == mask.digest, job
        success = receipt.last_success
        pack = packs.read_and_verify(Path(success.pack_path), success.pack_sha256)
        assert pack.mask_digest == mask.digest, job
        assert set(pack.market_partition) <= {"US"}, job

    # -- barrier and candidate build bind the same identity ---------------------
    barrier = assemble_analysis_materials(
        tick=TICK, mask=mask,
        receipt_readers={job: receipts[job].read for job in JobKind},
        pack_reader=lambda path, digest: packs.read_and_verify(Path(path), digest),
        now=START,
    )
    assert barrier.mask_digest == mask.digest
    assert barrier.market_readiness[Market.US] is MarketReadiness.READY
    assert barrier.market_readiness[Market.KR] is MarketReadiness.SKIP_CLOSED
    assert barrier.proceed_markets == frozenset({Market.US})

    built = build_candidates(barrier, baselines={}, now=START, resolve_scope=resolve_account_scope_pattern)
    assert built.mask_digest == mask.digest
    assert built.material_binding_digest == material_binding_digest(barrier)
    # analysis-facing subjects are exclusively open-market
    subject_markets = {
        advance.fields["market"] for advance in built.baseline_advances
        if "market" in advance.fields
    }
    assert subject_markets <= {"US"}
    assert not any("005930" in advance.subject_id for advance in built.baseline_advances)


def test_verdict_judgment_feeds_mask_without_reinterpretation(tmp_path):
    # judge_sessions -> mask: both closed yields the truthful cheap SKIP shape.
    kr = None
    us = None
    verdicts = judge_sessions(kr, us, NOW)
    mask = build_open_market_mask(
        policy_version="cadence-300s-v1", scheduled_for_utc=START,
        tick_id=TICK.tick_id, verdicts=verdicts,
        local_dates={Market.KR: date(2026, 8, 3), Market.US: date(2026, 8, 3)},
    )
    assert mask.markets == frozenset()
    assert {entry.reason for entry in mask.entries.values()} == {"no_regular"}
