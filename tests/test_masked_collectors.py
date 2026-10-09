"""Stage 2-C: holdings/quotes admission is bound to OpenMarketMaskV1.

With a mask the collectors never reinterpret session verdicts
(``session_result`` may be ``None``); a missing/mismatched binding fails
closed with provider I/O 0 and state mutation 0; an empty mask is a truthful
SKIP; packs and receipts carry the exact mask digest.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest

from tokmo.facts.toss import TossCampaignOutcome, derive_account_scope
from tokmo.facts.toss_prices import TossPriceQueryOutcome
from tokmo.programs import quotes_sync as quotes_sync_module
from tokmo.programs.holdings_sync import run_holdings_sync
from tokmo.programs.quotes_sync import run_quotes_sync
from tokmo.values.cadence_policy import QUOTE_BATCH_SYMBOLS_V1, cadence_tick_v1
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)
from tokmo.values.holdings import TossAccountHoldingsFact
from tokmo.values.market_observations import TossCurrentPriceFact
from tokmo.values.open_market_mask import MaskBindingError, build_open_market_mask
from tokmo.values.session_gate import Market, SessionVerdict
from tokmo.values.toss_market_calendar import CalendarSession

NOW = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
TICK = cadence_tick_v1("cadence-300s-v1", NOW)
NEXT_TICK = cadence_tick_v1("cadence-300s-v1", NOW + timedelta(seconds=300))

US_SESSION = CalendarSession(
    datetime(2026, 8, 3, 13, 30, tzinfo=UTC),
    datetime(2026, 8, 3, 20, 0, tzinfo=UTC),
)
KR_SESSION = CalendarSession(
    datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
    datetime(2026, 8, 3, 6, 30, tzinfo=UTC),
)


def mask_for(kr_reason, us_reason, *, tick=None):
    tick = TICK if tick is None else tick
    verdicts = (
        SessionVerdict(
            Market.KR,
            kr_reason == "open",
            kr_reason,
            KR_SESSION if kr_reason != "no_regular" else None,
        ),
        SessionVerdict(
            Market.US,
            us_reason == "open",
            us_reason,
            US_SESSION if us_reason != "no_regular" else None,
        ),
    )
    return build_open_market_mask(
        policy_version=tick.policy_version,
        scheduled_for_utc=tick.scheduled_for_utc,
        tick_id=tick.tick_id,
        verdicts=verdicts,
        local_dates={Market.KR: date(2026, 8, 3), Market.US: date(2026, 8, 3)},
    )


US_ONLY = mask_for("after_close", "open")
KR_ONLY = mask_for("open", "before_open")
BOTH_CLOSED = mask_for("after_close", "after_close")
WRONG_TICK = mask_for("after_close", "open", tick=NEXT_TICK)


def _lock(tmp_path, job):
    path = tmp_path / "ops" / "locks" / f"{job}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class ReceiptSink:
    def __init__(self):
        self.value = None

    def read(self):
        return self.value

    def replace(self, value):
        self.value = value


class PackSink:
    def __init__(self):
        self.pack = None

    def write(self, pack):
        self.pack = pack
        return __import__("pathlib").Path(f"/tmp/{pack.tick_id}.json")

    def list_packs(self, job):
        return (
            [(self.pack.tick_id, __import__("pathlib").Path("/tmp/x"), "0" * 64)]
            if self.pack
            else []
        )

    def reap_orphans(self, job, keep, refs):
        return 0


def mixed_holdings():
    scope = derive_account_scope("123")
    return CurrentHoldingsV1(
        1,
        (
            AccountHoldingsV1(
                scope,
                1,
                (
                    HoldingItemV1("005930", "1", "1", "KRW", "KR", "market"),
                    HoldingItemV1("AAPL", "1", "1", "USD", "US", "market"),
                    HoldingItemV1("MYSTERY", "1", "1", None, "NASDAQ", "market"),
                ),
            ),
        ),
        True,
        NOW,
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )


def run_holdings(tmp_path, *, mask, provider, store=None, packs=None, receipts=None):
    return run_holdings_sync(
        tick=TICK,
        holdings_provider=provider,
        store=store if store is not None else object(),
        pack_writer=packs or PackSink(),
        receipt_store=receipts if receipts is not None else ReceiptSink(),
        reservation_seal=lambda *args: None,
        incident_writer=lambda *args: None,
        clock=lambda: NOW,
        now=NOW,
        job_lock_path=_lock(tmp_path, "holdings"),
        mask=mask,
    )


def run_quotes(
    tmp_path,
    *,
    mask,
    holdings,
    provider,
    packs=None,
    receipts=None,
    incident_writer=None,
    quote_store=None,
):
    @dataclass
    class Save:
        stored_at: datetime
        status: str = "SAVED"
        inserted_count: int = 0
        updated_count: int = 0
        reason: str = "saved"

    return run_quotes_sync(
        tick=TICK,
        holdings=holdings,
        quotes_provider=provider,
        quote_store=quote_store
        or (
            lambda outcome: Save(
                NOW + timedelta(seconds=1), inserted_count=len(outcome.facts)
            )
        ),
        pack_writer=packs or PackSink(),
        receipt_store=receipts if receipts is not None else ReceiptSink(),
        reservation_seal=lambda *args: None,
        incident_writer=incident_writer or (lambda *args: None),
        clock=lambda: NOW,
        now=NOW,
        job_lock_path=_lock(tmp_path, "quotes"),
        mask=mask,
    )


# -- fail closed on mismatched binding (provider 0 / mutation 0) -----------------


def test_holdings_mask_tick_mismatch_fails_closed(tmp_path):
    receipts = ReceiptSink()

    class Store:
        def replace(self, *args, **kwargs):
            pytest.fail("store must not mutate")

    with pytest.raises(MaskBindingError):
        run_holdings(
            tmp_path,
            mask=WRONG_TICK,
            provider=lambda: pytest.fail("provider must not run"),
            store=Store(),
            receipts=receipts,
        )
    assert receipts.value is None


def test_quotes_mask_tick_mismatch_fails_closed(tmp_path):
    receipts = ReceiptSink()
    with pytest.raises(MaskBindingError):
        run_quotes(
            tmp_path,
            mask=WRONG_TICK,
            holdings=mixed_holdings(),
            provider=lambda batch: pytest.fail("provider must not run"),
            receipts=receipts,
            quote_store=lambda outcome: pytest.fail("store must not mutate"),
        )
    assert receipts.value is None


def test_holdings_mask_must_be_a_real_mask(tmp_path):
    with pytest.raises(MaskBindingError):
        run_holdings(
            tmp_path,
            mask="not-a-mask",
            provider=lambda: pytest.fail("provider must not run"),
        )


# -- both closed: truthful SKIP, provider 0 --------------------------------------


def test_holdings_empty_mask_truthful_skip(tmp_path):
    receipts = ReceiptSink()
    result = run_holdings(
        tmp_path,
        mask=BOTH_CLOSED,
        provider=lambda: pytest.fail("provider must not run"),
        receipts=receipts,
    )
    assert (result.status, result.reason) == ("skipped", "skipped_market_closed")
    assert receipts.value.mask_digest == BOTH_CLOSED.digest
    assert receipts.value.markets["KR"].status == "closed"
    assert receipts.value.markets["US"].status == "closed"


def test_quotes_empty_mask_truthful_skip(tmp_path):
    receipts = ReceiptSink()
    result = run_quotes(
        tmp_path,
        mask=BOTH_CLOSED,
        holdings=mixed_holdings(),
        provider=lambda batch: pytest.fail("provider must not run"),
        receipts=receipts,
    )
    assert (result.status, result.reason) == ("skipped", "skipped_market_closed")
    assert receipts.value.mask_digest == BOTH_CLOSED.digest


# -- US-only open: full canonical snapshot, mask-only projection/subjects --------


def holdings_outcome():
    scopes = (derive_account_scope("123"),)
    fact = TossAccountHoldingsFact(
        "toss-holdings-v1",
        NOW,
        1,
        1,
        (
            HoldingItemV1("005930", "1", "1", "KRW", "KR", "market"),
            HoldingItemV1("AAPL", "2", "180", "USD", "US", "market"),
        ),
    )
    return TossCampaignOutcome(
        "success",
        "holdings",
        1,
        (fact,),
        (),
        account_identities=(AccountScopeIdentityV1(scopes[0], None, 1, None),),
        _identity_fingerprints={scopes[0]: "a" * 64},
    )


def test_holdings_us_only_persists_full_snapshot_and_projects_us(tmp_path):
    stored = {}

    class Store:
        def replace(self, state, *, identity_fingerprints):
            stored["state"] = state

    packs, receipts = PackSink(), ReceiptSink()
    result = run_holdings(
        tmp_path,
        mask=US_ONLY,
        provider=holdings_outcome,
        store=Store(),
        packs=packs,
        receipts=receipts,
    )
    assert result.status == "saved"
    # canonical snapshot keeps the closed-market (KR) holding
    persisted = {
        item.symbol for account in stored["state"].accounts for item in account.holdings
    }
    assert persisted == {"005930", "AAPL"}
    # explicit open-market projection admits US only
    projected = {
        item.symbol
        for account in result.projection.accounts
        for item in account.holdings
    }
    assert projected == {"AAPL"}
    assert result.projection.mask_digest == US_ONLY.digest
    assert result.mask_digest == US_ONLY.digest
    # pack payload stays the full snapshot; partition and digest are mask-bound
    assert packs.pack.market_partition == ("US",)
    assert packs.pack.mask_digest == US_ONLY.digest
    pack_symbols = {
        row["symbol"]
        for account in packs.pack.payload["accounts"]
        for row in account["holdings"]
    }
    assert pack_symbols == {"005930", "AAPL"}
    assert receipts.value.mask_digest == US_ONLY.digest
    assert receipts.value.markets["US"].status == "open"
    assert receipts.value.markets["KR"].status == "closed"


def test_quotes_us_only_admits_only_us_subjects(tmp_path):
    batches, incidents = [], []

    def provider(symbols):
        batches.append(symbols)
        return TossPriceQueryOutcome(
            "success",
            tuple(
                TossCurrentPriceFact(
                    "toss-current-price-v1",
                    symbol,
                    "10",
                    "USD",
                    NOW,
                    NOW + timedelta(seconds=1),
                )
                for symbol in symbols
            ),
        )

    packs, receipts = PackSink(), ReceiptSink()
    result = run_quotes(
        tmp_path,
        mask=US_ONLY,
        holdings=mixed_holdings(),
        provider=provider,
        packs=packs,
        receipts=receipts,
        incident_writer=lambda kind, payload: incidents.append((kind, payload)),
    )
    assert result.status == "saved"
    fetched = {symbol for batch in batches for symbol in batch}
    assert fetched == {"AAPL"}  # KR closed subject and unknown subject: wire 0
    assert incidents == [("market-classification-unknown", frozenset({"MYSTERY"}))]
    assert set(packs.pack.payload["markets"]) == {"US"}
    assert packs.pack.mask_digest == US_ONLY.digest
    assert receipts.value.mask_digest == US_ONLY.digest


def test_quotes_kr_only_inverse(tmp_path):
    batches = []

    def provider(symbols):
        batches.append(symbols)
        return TossPriceQueryOutcome(
            "success",
            tuple(
                TossCurrentPriceFact(
                    "toss-current-price-v1",
                    symbol,
                    "10",
                    "KRW",
                    NOW,
                    NOW + timedelta(seconds=1),
                )
                for symbol in symbols
            ),
        )

    packs = PackSink()
    result = run_quotes(
        tmp_path,
        mask=KR_ONLY,
        holdings=mixed_holdings(),
        provider=provider,
        packs=packs,
    )
    assert result.status == "saved"
    assert {symbol for batch in batches for symbol in batch} == {"005930"}
    assert set(packs.pack.payload["markets"]) == {"KR"}


def test_quotes_zero_open_market_subjects_skips_without_wire(tmp_path):
    scope = derive_account_scope("123")
    kr_only_holdings = CurrentHoldingsV1(
        1,
        (
            AccountHoldingsV1(
                scope, 1, (HoldingItemV1("005930", "1", "1", "KRW", "KR", "market"),)
            ),
        ),
        True,
        NOW,
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    result = run_quotes(
        tmp_path,
        mask=US_ONLY,
        holdings=kr_only_holdings,
        provider=lambda batch: pytest.fail("provider must not run"),
    )
    assert (result.status, result.reason) == ("skipped", "skipped_zero_quote_targets")


# -- provider batch limit comes from the central policy module -------------------


def test_quote_batch_limit_is_central_policy_value(tmp_path, monkeypatch):
    assert quotes_sync_module.QUOTE_BATCH_SYMBOLS_V1 == QUOTE_BATCH_SYMBOLS_V1
    monkeypatch.setattr(quotes_sync_module, "QUOTE_BATCH_SYMBOLS_V1", 2)
    scope = derive_account_scope("123")
    holdings = CurrentHoldingsV1(
        1,
        (
            AccountHoldingsV1(
                scope,
                1,
                tuple(
                    HoldingItemV1(f"US{index}", "1", "1", "USD", "US", "market")
                    for index in range(3)
                ),
            ),
        ),
        True,
        NOW,
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    batches = []

    def provider(symbols):
        batches.append(symbols)
        return TossPriceQueryOutcome(
            "success",
            tuple(
                TossCurrentPriceFact(
                    "toss-current-price-v1",
                    symbol,
                    "10",
                    "USD",
                    NOW,
                    NOW + timedelta(seconds=1),
                )
                for symbol in symbols
            ),
        )

    result = run_quotes(
        tmp_path,
        mask=US_ONLY,
        holdings=holdings,
        provider=provider,
    )
    assert result.status == "saved"
    assert [len(batch) for batch in batches] == [2, 1]
