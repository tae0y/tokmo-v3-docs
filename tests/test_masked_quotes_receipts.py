"""Stage 2 repair B3: every masked quotes terminal path carries the digest.

The masked execution context owns one mandatory non-empty exact mask digest;
no receipt-writing path (success, provider exception, provider failure code,
provider contract failure, store failure, pack failure, skip, lock outcomes)
can omit it.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pytest

from tokmo.facts.toss import derive_account_scope
from tokmo.facts.toss_prices import TossPriceQueryOutcome
from tokmo.programs.quotes_sync import run_quotes_sync
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)
from tokmo.values.market_observations import TossCurrentPriceFact
from tokmo.values.cadence_policy import cadence_tick_v1
from tokmo.values.open_market_mask import build_open_market_mask
from tokmo.values.session_gate import Market, SessionVerdict
from tokmo.values.toss_market_calendar import CalendarSession

NOW = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
TICK = cadence_tick_v1("cadence-300s-v1", NOW)
SESSION = CalendarSession(
    datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc),
    datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc),
)


def us_only_mask():
    verdicts = (
        SessionVerdict(Market.KR, False, "after_close", SESSION),
        SessionVerdict(Market.US, True, "open", SESSION),
    )
    return build_open_market_mask(
        policy_version="cadence-300s-v1", scheduled_for_utc=TICK.start_utc,
        tick_id=TICK.tick_id, verdicts=verdicts,
        local_dates={Market.KR: date(2026, 8, 4), Market.US: date(2026, 8, 3)},
    )


MASK = us_only_mask()


class ReceiptSink:
    def __init__(self):
        self.value = None

    def read(self):
        return self.value

    def replace(self, value):
        self.value = value


class PackSink:
    def __init__(self, fail=False):
        self.fail = fail
        self.pack = None

    def write(self, pack):
        if self.fail:
            raise OSError("disk full")
        self.pack = pack
        return __import__("pathlib").Path(f"/tmp/{pack.tick_id}.json")

    def list_packs(self, job):
        return []

    def reap_orphans(self, job, keep, refs):
        return 0


@dataclass
class Save:
    stored_at: datetime
    status: str = "SAVED"
    inserted_count: int = 1
    updated_count: int = 0
    reason: str = "saved"


def us_holdings():
    scope = derive_account_scope("123")
    return CurrentHoldingsV1(
        1, (AccountHoldingsV1(scope, 1, (
            HoldingItemV1("AAPL", "1", "1", "USD", "US", "market"),
        )),), True, NOW,
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )


def good_provider(symbols):
    return TossPriceQueryOutcome("success", tuple(
        TossCurrentPriceFact("toss-current-price-v1", symbol, "10", "USD",
                             NOW, NOW + timedelta(seconds=1))
        for symbol in symbols
    ))


def run(tmp_path, *, provider=good_provider, quote_store=None, packs=None,
        holdings=None, watchlist_symbols=None):
    receipts = ReceiptSink()
    lock = tmp_path / "locks" / "quotes.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    result = run_quotes_sync(
        tick=TICK,
        holdings=us_holdings() if holdings is None else holdings,
        quotes_provider=provider,
        quote_store=quote_store or (
            lambda outcome: Save(NOW, inserted_count=len(outcome.facts))),
        pack_writer=packs or PackSink(),
        receipt_store=receipts, reservation_seal=lambda *args: None,
        incident_writer=lambda *args: None, clock=lambda: NOW, now=NOW,
        job_lock_path=lock, mask=MASK,
        watchlist_symbols=watchlist_symbols,
    )
    return result, receipts


def assert_terminal(result, receipts, status, reason):
    assert (result.status, result.reason) == (status, reason)
    assert receipts.value is not None
    assert receipts.value.attempt.reason == reason
    assert receipts.value.mask_digest == MASK.digest


def test_watchlist_extends_quote_targets_without_mutating_holdings(tmp_path):
    canonical = us_holdings()
    seen = []

    def provider(symbols):
        seen.append(tuple(symbols))
        return good_provider(symbols)

    result, _ = run(
        tmp_path, provider=provider, holdings=canonical,
        watchlist_symbols={Market.US: frozenset({"V"})},
    )

    assert result.status == "saved"
    assert seen == [("AAPL", "V")]
    assert tuple(item.symbol for item in canonical.accounts[0].holdings) == ("AAPL",)


def test_success_receipt_carries_exact_digest(tmp_path):
    result, receipts = run(tmp_path)
    assert_terminal(result, receipts, "saved", "saved")


def test_provider_exception_receipt_carries_exact_digest(tmp_path):
    def provider(symbols):
        raise RuntimeError("transport down")

    result, receipts = run(tmp_path, provider=provider)
    assert_terminal(result, receipts, "failed", "failed_provider_other")


def test_provider_failure_code_receipt_carries_exact_digest(tmp_path):
    result, receipts = run(
        tmp_path, provider=lambda symbols: TossPriceQueryOutcome("http_429", ()),
    )
    assert_terminal(result, receipts, "failed", "failed_provider_rate_limit")


def test_provider_contract_failure_receipt_carries_exact_digest(tmp_path):
    # wrong symbols returned: contract mismatch after a "success" code
    result, receipts = run(
        tmp_path, provider=lambda symbols: TossPriceQueryOutcome("success", tuple(
            TossCurrentPriceFact("toss-current-price-v1", "WRONG", "10", "USD",
                                 NOW, NOW)
            for _symbol in symbols
        )),
    )
    assert_terminal(result, receipts, "failed", "failed_provider_contract")


def test_store_failure_receipt_carries_exact_digest(tmp_path):
    def store(outcome):
        raise OSError("sqlite locked")

    result, receipts = run(tmp_path, quote_store=store)
    assert_terminal(result, receipts, "failed", "failed_store")


def test_pack_failure_receipt_carries_exact_digest(tmp_path):
    result, receipts = run(tmp_path, packs=PackSink(fail=True))
    assert_terminal(result, receipts, "failed", "failed_pack")


def test_holdings_absent_skip_receipt_carries_exact_digest(tmp_path):
    """When holdings is None, quotes collector now reports
    skipped_zero_quote_targets (unified zero-subjects path) rather than
    the legacy skipped_holdings_absent which trades still emits."""
    receipts = ReceiptSink()
    result = run_quotes_sync(
        tick=TICK, holdings=None,
        quotes_provider=lambda symbols: pytest.fail("provider must not run"),
        quote_store=lambda outcome: pytest.fail("store must not run"),
        pack_writer=PackSink(), receipt_store=receipts,
        reservation_seal=lambda *args: None, incident_writer=lambda *args: None,
        clock=lambda: NOW, now=NOW,
        job_lock_path=tmp_path / "quotes.lock", mask=MASK,
    )
    assert_terminal(result, receipts, "skipped", "skipped_zero_quote_targets")


def test_lock_contention_skip_receipt_carries_exact_digest(tmp_path):
    import fcntl
    import os

    lock = tmp_path / "quotes.lock"
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        receipts = ReceiptSink()
        result = run_quotes_sync(
            tick=TICK, holdings=us_holdings(),
            quotes_provider=lambda symbols: pytest.fail("provider must not run"),
            quote_store=lambda outcome: pytest.fail("store must not run"),
            pack_writer=PackSink(), receipt_store=receipts,
            reservation_seal=lambda *args: None,
            incident_writer=lambda *args: None,
            clock=lambda: NOW, now=NOW, job_lock_path=lock, mask=MASK,
        )
        assert_terminal(result, receipts, "skipped", "skipped_lock_contention")
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Watchlist quote targets (adversarial coverage)
# ---------------------------------------------------------------------------

def _run_direct(tmp_path, *, holdings, watchlist_symbols, provider,
                packs=None):
    """run_quotes_sync with holdings passed through verbatim (None stays
    None — the module-level ``run`` helper substitutes canonical
    holdings)."""
    receipts = ReceiptSink()
    packs = packs or PackSink()
    lock = tmp_path / "locks" / "quotes.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    result = run_quotes_sync(
        tick=TICK, holdings=holdings,
        quotes_provider=provider,
        quote_store=lambda outcome: Save(
            NOW, inserted_count=len(outcome.facts)),
        pack_writer=packs, receipt_store=receipts,
        reservation_seal=lambda *args: None,
        incident_writer=lambda *args: None,
        clock=lambda: NOW, now=NOW, job_lock_path=lock, mask=MASK,
        watchlist_symbols=watchlist_symbols,
    )
    return result, receipts, packs


def test_watchlist_only_none_holdings_one_call_saves_quote(tmp_path):
    """holdings=None + active US watchlist V in a US-open mask: exactly one
    provider call for V, quote saved, and no holdings are synthesized."""
    batches = []

    def provider(symbols):
        batches.append(tuple(symbols))
        return good_provider(symbols)

    result, receipts, packs = _run_direct(
        tmp_path, holdings=None,
        watchlist_symbols={Market.US: frozenset({"V"})},
        provider=provider,
    )
    assert (result.status, result.reason) == ("saved", "saved")
    assert batches == [("V",)]
    assert receipts.value.mask_digest == MASK.digest
    payload = packs.pack.payload["markets"]
    assert list(payload) == ["US"]
    assert [row["symbol"] for row in payload["US"]] == ["V"]


def test_closed_market_watchlist_makes_network_zero(tmp_path):
    """A watchlist symbol whose market is not in the open mask never
    reaches the provider: zero targets, zero network."""
    result, receipts, _ = _run_direct(
        tmp_path, holdings=None,
        watchlist_symbols={Market.KR: frozenset({"005930"})},
        provider=lambda symbols: pytest.fail("provider must not run"),
    )
    assert_terminal(result, receipts, "skipped", "skipped_zero_quote_targets")


def test_zero_union_emits_registered_reason(tmp_path):
    """No holdings + empty watchlist: the skip reason is a registered
    receipt token (the receipt layer rejects unknown reasons)."""
    from tokmo.values.receipt_v1 import RECEIPT_REASON_TOKENS

    result, receipts, _ = _run_direct(
        tmp_path, holdings=None,
        watchlist_symbols={Market.US: frozenset()},
        provider=lambda symbols: pytest.fail("provider must not run"),
    )
    assert_terminal(result, receipts, "skipped", "skipped_zero_quote_targets")
    assert result.reason in RECEIPT_REASON_TOKENS


def test_holdings_watchlist_overlap_dedups(tmp_path):
    """A symbol both held and watchlisted is queried exactly once."""
    batches = []

    def provider(symbols):
        batches.append(tuple(symbols))
        return good_provider(symbols)

    result, receipts, _ = _run_direct(
        tmp_path, holdings=us_holdings(),
        watchlist_symbols={Market.US: frozenset({"AAPL", "V"})},
        provider=provider,
    )
    assert (result.status, result.reason) == ("saved", "saved")
    assert batches == [("AAPL", "V")]


def test_watchlist_batch_capacity_preserved_without_truncation(tmp_path):
    """A union larger than one provider batch is chunked at the central
    policy size and fully covered — never truncated."""
    from tokmo.values.cadence_policy import QUOTE_BATCH_SYMBOLS_V1

    symbols = frozenset(
        f"S{i:03d}" for i in range(QUOTE_BATCH_SYMBOLS_V1 + 1))
    batches = []

    def provider(batch):
        batches.append(tuple(batch))
        return good_provider(batch)

    result, receipts, _ = _run_direct(
        tmp_path, holdings=None,
        watchlist_symbols={Market.US: symbols},
        provider=provider,
    )
    assert (result.status, result.reason) == ("saved", "saved")
    assert [len(batch) for batch in batches] == [QUOTE_BATCH_SYMBOLS_V1, 1]
    covered = {symbol for batch in batches for symbol in batch}
    assert covered == set(symbols)


def test_invalid_watchlist_contract_fails_closed_with_receipt(tmp_path):
    """A malformed watchlist mapping fails closed with a truthful,
    registered failed receipt — not an unhandled receipt-layer error."""
    result, receipts, _ = _run_direct(
        tmp_path, holdings=None,
        watchlist_symbols={Market.US: ["V"]},
        provider=lambda symbols: pytest.fail("provider must not run"),
    )
    assert_terminal(result, receipts, "failed", "failed_watchlist_contract")


def test_watchlist_symbol_empty_string_fails_closed(tmp_path):
    result, receipts, _ = _run_direct(
        tmp_path, holdings=None,
        watchlist_symbols={Market.US: frozenset({"V", ""})},
        provider=lambda symbols: pytest.fail("provider must not run"),
    )
    assert_terminal(result, receipts, "failed", "failed_watchlist_contract")
