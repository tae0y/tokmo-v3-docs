import fcntl
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from tokmo.facts.toss import TossCampaignOutcome, derive_account_scope
from tokmo.facts.toss_prices import TossPriceQueryOutcome
from tokmo.programs.holdings_sync import (
    _map_holdings_outcome_reason,
)
from tokmo.programs.holdings_sync import (
    _try_job_lock as _try_holdings_job_lock,
)
from tokmo.programs.legacy_g2 import (
    legacy_run_holdings_sync as run_holdings_sync,
)
from tokmo.programs.legacy_g2 import (
    legacy_run_quotes_sync as run_quotes_sync,
)
from tokmo.programs.quotes_sync import (
    _map_quotes_outcome_reason,
)
from tokmo.programs.quotes_sync import (
    _try_job_lock as _try_quotes_job_lock,
)
from tokmo.programs.session_gate import SessionGateResult
from tokmo.records.current_holdings_store import CurrentHoldingsStore
from tokmo.records.latest_quotes import LatestQuoteStore
from tokmo.records.material_pack_store import MaterialPackWriter
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.records.receipt_store import ReceiptStore
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeCollisionError,
    AccountScopeDuplicateError,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)
from tokmo.values.holdings import TossAccountHoldingsFact
from tokmo.values.market_observations import TossCurrentPriceFact
from tokmo.values.ops_kernel import (
    OpsKernelBudgetExceeded,
    OpsKernelConflictError,
    OpsKernelCooldownActive,
)
from tokmo.values.ops_tick import tick_id_from_utc
from tokmo.values.session_gate import Market, SessionVerdict

NOW = datetime(2026, 7, 30, 1, tzinfo=UTC)


def _lock(tmp_path, job):
    path = tmp_path / "ops" / "locks" / f"{job}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _sessions(kr=True, us=False):
    verdicts = (
        SessionVerdict(Market.KR, kr, "open" if kr else "after_close", None),
        SessionVerdict(Market.US, us, "open" if us else "after_close", None),
    )
    return SessionGateResult(NOW, verdicts, kr or us, False)


class ReceiptSink:
    def __init__(self):
        self.value = None
        self.events = []

    def read(self):
        return self.value

    def replace(self, value):
        self.events.append("receipt")
        self.value = value


class PackSink:
    def __init__(self):
        self.pack = None
        self.events = []

    def write(self, pack):
        self.events.append("pack")
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


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("http_429", "failed_provider_rate_limit"),
        ("auth_rejection", "failed_provider_auth"),
        ("forbidden", "failed_provider_auth"),
        ("contract_mismatch", "failed_provider_contract"),
        ("account_count_unexpected", "failed_provider_contract"),
        ("ceiling_exceeded", "failed_ceiling"),
        ("timeout", "failed_provider_other"),
        ("future_code", "failed_provider_other"),
    ],
)
def test_holdings_outcome_mapping_is_total_static(code, reason):
    assert _map_holdings_outcome_reason(code) == reason


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("http_429", "failed_provider_rate_limit"),
        ("auth_rejection", "failed_provider_auth"),
        ("contract_mismatch", "failed_provider_contract"),
        ("parse_error", "failed_provider_contract"),
        ("ceiling_exceeded", "failed_ceiling"),
        ("future_code", "failed_provider_other"),
    ],
)
def test_quotes_outcome_mapping_is_total_static(code, reason):
    assert _map_quotes_outcome_reason(code) == reason


@pytest.mark.parametrize("job", ("holdings", "quotes"))
@pytest.mark.parametrize(
    "denial",
    (
        OpsKernelBudgetExceeded,
        OpsKernelCooldownActive,
        OpsKernelConflictError,
    ),
)
def test_f10_typed_gate_denial_is_failed_ceiling_before_transport(
    tmp_path, job, denial
):
    transport = []
    kernel = OpsKernelStore(tmp_path / "ops.sqlite3")
    receipts = ReceiptStore(tmp_path / "receipts" / f"{job}.json", clock=lambda: NOW)
    packs = MaterialPackWriter(tmp_path / "packs", clock=lambda: NOW)

    def denied(*args, **kwargs):
        raise denial("runtime request denied")

    if job == "holdings":
        result = run_holdings_sync(
            tick=tick_id_from_utc(NOW),
            session_result=_sessions(),
            holdings_provider=denied,
            store=CurrentHoldingsStore(
                tmp_path / "state" / "holdings.json", clock=lambda: NOW
            ),
            pack_writer=packs,
            receipt_store=receipts,
            reservation_seal=lambda *args: None,
            incident_writer=lambda *args: None,
            clock=lambda: NOW,
            now=NOW,
            job_lock_path=_lock(tmp_path, job),
        )
    else:
        scope = derive_account_scope("123")
        holdings = CurrentHoldingsV1(
            1,
            (
                AccountHoldingsV1(
                    scope,
                    1,
                    (HoldingItemV1("005930", "1", "1", "KRW", "KR", "market"),),
                ),
            ),
            True,
            NOW,
            {scope: AccountScopeIdentityV1(scope, None, 1, None)},
        )
        quote_store = LatestQuoteStore(
            tmp_path / "state" / "quotes.sqlite3", clock=lambda: NOW
        )
        result = run_quotes_sync(
            tick=tick_id_from_utc(NOW),
            session_result=_sessions(),
            holdings=holdings,
            quotes_provider=denied,
            quote_store=lambda outcome: quote_store.save_batch(outcome.facts),
            pack_writer=packs,
            receipt_store=receipts,
            reservation_seal=lambda *args: None,
            incident_writer=lambda *args: None,
            clock=lambda: NOW,
            now=NOW,
            job_lock_path=_lock(tmp_path, job),
        )

    assert transport == []
    assert kernel.read_planned_requests(tick_id_from_utc(NOW).tick_id) == ()
    assert (result.status, result.reason) == ("failed", "failed_ceiling")
    assert (receipts.read().attempt.status.value, receipts.read().attempt.reason) == (
        "failed",
        "failed_ceiling",
    )


def test_first_closed_holdings_run_writes_receipt_without_provider_call(tmp_path):
    receipts = ReceiptSink()
    called = []
    result = run_holdings_sync(
        tick=tick_id_from_utc(NOW),
        session_result=_sessions(False, False),
        holdings_provider=lambda: called.append(True),
        store=object(),
        pack_writer=PackSink(),
        receipt_store=receipts,
        reservation_seal=lambda *args: None,
        incident_writer=lambda *args: None,
        clock=lambda: NOW,
        now=NOW,
        job_lock_path=_lock(tmp_path, "holdings"),
    )
    assert (result.status, result.reason) == ("skipped", "skipped_market_closed")
    assert not called
    assert receipts.value.last_success is None
    assert receipts.value.markets["KR"].last_success_tick_id is None


def test_holdings_nonblocking_job_lock_contention_skips(tmp_path):
    receipt_path = tmp_path / "ops" / "receipts" / "holdings.json"
    lock_path = tmp_path / "ops" / "locks" / "holdings.lock"
    lock_path.parent.mkdir(parents=True)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        receipts = ReceiptStore(receipt_path, clock=lambda: NOW)
        result = run_holdings_sync(
            tick=tick_id_from_utc(NOW),
            session_result=_sessions(),
            holdings_provider=lambda: pytest.fail("provider must not run"),
            store=object(),
            pack_writer=PackSink(),
            receipt_store=receipts,
            reservation_seal=lambda *args: None,
            incident_writer=lambda *args: None,
            clock=lambda: NOW,
            now=NOW,
            job_lock_path=lock_path,
        )
        assert (result.status, result.reason) == (
            "skipped",
            "skipped_lock_contention",
        )
    finally:
        os.close(fd)


def test_holdings_success_commits_store_then_pack_then_receipt(tmp_path):
    events = []
    scope = derive_account_scope("123")
    fact = TossAccountHoldingsFact("toss-holdings-v1", NOW, 1, 1, ())
    outcome = TossCampaignOutcome(
        "success",
        "holdings",
        1,
        (fact,),
        (),
        account_identities=(AccountScopeIdentityV1(scope, None, 1, None),),
        _identity_fingerprints={scope: "a" * 64},
    )

    class Store:
        def replace(self, state, *, identity_fingerprints):
            events.append("store")
            assert state.global_inventory

    packs, receipts = PackSink(), ReceiptSink()
    packs.events = events
    receipts.events = events
    result = run_holdings_sync(
        tick=tick_id_from_utc(NOW),
        session_result=_sessions(),
        holdings_provider=lambda: outcome,
        store=Store(),
        pack_writer=packs,
        receipt_store=receipts,
        reservation_seal=lambda *args: None,
        incident_writer=lambda *args: None,
        clock=lambda: NOW,
        now=NOW,
        job_lock_path=_lock(tmp_path, "holdings"),
    )
    assert result.status == "saved"
    assert events[:3] == ["store", "pack", "receipt"]
    assert packs.pack.payload["global_inventory"] is True
    assert packs.pack.market_partition == ("KR",)


def test_quotes_filters_unknown_deduplicates_batches_and_uses_store_timestamp(tmp_path):
    scope = derive_account_scope("123")
    items = tuple(
        HoldingItemV1(f"{index:06d}", "1", "1", "KRW", "KR", "market")
        for index in range(201)
    ) + (
        HoldingItemV1("000001", "1", "1", "KRW", "KR", "market"),
        HoldingItemV1("MYSTERY", "1", "1", None, "NASDAQ", "market"),
        HoldingItemV1("USONLY", "1", "1", "USD", "US", "market"),
    )
    holdings = CurrentHoldingsV1(
        1,
        (AccountHoldingsV1(scope, 1, items),),
        True,
        NOW,
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    batches, incidents = [], []
    stored_at = NOW + timedelta(seconds=2)

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

    @dataclass
    class Save:
        stored_at: datetime
        status: str = "SAVED"
        inserted_count: int = 0
        updated_count: int = 0
        reason: str = "saved"

    packs = PackSink()
    result = run_quotes_sync(
        tick=tick_id_from_utc(NOW),
        session_result=_sessions(),
        holdings=holdings,
        quotes_provider=provider,
        quote_store=lambda outcome: Save(
            stored_at,
            inserted_count=len(outcome.facts),
        ),
        pack_writer=packs,
        receipt_store=ReceiptSink(),
        reservation_seal=lambda *args: None,
        incident_writer=lambda kind, payload: incidents.append((kind, payload)),
        clock=lambda: NOW + timedelta(seconds=3),
        now=NOW,
        job_lock_path=_lock(tmp_path, "quotes"),
    )
    assert result.status == "saved"
    assert [len(batch) for batch in batches] == [200, 1]
    assert "USONLY" not in {symbol for batch in batches for symbol in batch}
    assert incidents == [("market-classification-unknown", frozenset({"MYSTERY"}))]
    rows = packs.pack.payload["markets"]["KR"]
    assert {row["stored_at"] for row in rows} == {
        stored_at.isoformat().replace("+00:00", "Z")
    }


def test_quotes_kr_only_success_preserves_previous_us_freshness(tmp_path):
    scope = derive_account_scope("123")
    holdings = CurrentHoldingsV1(
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
    previous_us_time = NOW - timedelta(days=1)
    receipts = ReceiptSink()
    from tokmo.values.material_pack import JobKind
    from tokmo.values.receipt_v1 import (
        AttemptStatus,
        AttemptV1,
        LastSuccessV1,
        MarketFreshness,
        ReceiptV1,
    )

    receipts.value = ReceiptV1(
        1,
        JobKind.QUOTES,
        AttemptV1(1, AttemptStatus.SAVED, "saved", previous_us_time),
        LastSuccessV1(1, "/tmp/old", "0" * 64, previous_us_time, previous_us_time),
        {
            "KR": MarketFreshness("KR", "open", 1, previous_us_time),
            "US": MarketFreshness("US", "open", 1, previous_us_time),
        },
        None,
    )

    @dataclass
    class Save:
        stored_at: datetime
        status: str = "SAVED"
        inserted_count: int = 1
        updated_count: int = 0
        reason: str = "saved"

    outcome = TossPriceQueryOutcome(
        "success",
        (
            TossCurrentPriceFact(
                "toss-current-price-v1",
                "005930",
                "10",
                "KRW",
                NOW,
                NOW,
            ),
        ),
    )
    result = run_quotes_sync(
        tick=tick_id_from_utc(NOW),
        session_result=_sessions(True, False),
        holdings=holdings,
        quotes_provider=lambda symbols: outcome,
        quote_store=lambda value: Save(NOW),
        pack_writer=PackSink(),
        receipt_store=receipts,
        reservation_seal=lambda *args: None,
        incident_writer=lambda *args: None,
        clock=lambda: NOW,
        now=NOW,
        job_lock_path=_lock(tmp_path, "quotes"),
    )
    assert result.markets["US"].last_success_tick_id == 1
    assert result.markets["US"].captured_at == previous_us_time
    assert result.markets["US"].status == "closed"
    assert result.markets == receipts.value.markets


def test_quotes_skip_refreshes_status_but_preserves_previous_freshness(tmp_path):
    previous_at = NOW - timedelta(days=1)
    receipts = ReceiptSink()
    from tokmo.values.material_pack import JobKind
    from tokmo.values.receipt_v1 import (
        AttemptStatus,
        AttemptV1,
        LastSuccessV1,
        MarketFreshness,
        ReceiptV1,
    )

    receipts.value = ReceiptV1(
        1,
        JobKind.QUOTES,
        AttemptV1(1, AttemptStatus.SAVED, "saved", previous_at),
        LastSuccessV1(1, "/tmp/old", "0" * 64, previous_at, previous_at),
        {
            "KR": MarketFreshness("KR", "open", 1, previous_at),
            "US": MarketFreshness("US", "open", 1, previous_at),
        },
        None,
    )
    result = run_quotes_sync(
        tick=tick_id_from_utc(NOW),
        session_result=_sessions(False, False),
        holdings=None,
        quotes_provider=lambda _: pytest.fail("no provider"),
        quote_store=lambda _: pytest.fail("no store"),
        pack_writer=PackSink(),
        receipt_store=receipts,
        reservation_seal=lambda *args: None,
        incident_writer=lambda *args: None,
        clock=lambda: NOW,
        now=NOW,
        job_lock_path=_lock(tmp_path, "quotes"),
    )
    assert result.markets == receipts.value.markets
    assert {key: value.status for key, value in result.markets.items()} == {
        "KR": "closed",
        "US": "closed",
    }
    assert all(value.last_success_tick_id == 1 for value in result.markets.values())


@pytest.mark.parametrize("malformation", ["empty", "none_fetched_at"])
def test_malformed_quotes_success_fails_provider_contract_without_throwing(
    tmp_path, malformation
):
    scope = derive_account_scope("123")
    holdings = CurrentHoldingsV1(
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
    facts = (
        ()
        if malformation == "empty"
        else (
            TossCurrentPriceFact(
                "toss-current-price-v1",
                "005930",
                "10",
                "KRW",
                NOW,
                None,
            ),
        )
    )
    receipts = ReceiptSink()
    outcome = TossPriceQueryOutcome("success", facts)
    result = run_quotes_sync(
        tick=tick_id_from_utc(NOW),
        session_result=_sessions(),
        holdings=holdings,
        quotes_provider=lambda _: outcome,
        quote_store=lambda _: pytest.fail("malformed facts must not store"),
        pack_writer=PackSink(),
        receipt_store=receipts,
        reservation_seal=lambda *args: None,
        incident_writer=lambda *args: None,
        clock=lambda: NOW,
        now=NOW,
        job_lock_path=_lock(tmp_path, "quotes"),
    )
    assert (result.status, result.reason) == (
        "failed",
        "failed_provider_contract",
    )
    assert receipts.value.attempt.reason == "failed_provider_contract"
    assert result.markets == receipts.value.markets


def test_inconsistent_quote_store_receipt_fails_store_without_throwing(tmp_path):
    scope = derive_account_scope("123")
    holdings = CurrentHoldingsV1(
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
    outcome = TossPriceQueryOutcome(
        "success",
        (
            TossCurrentPriceFact(
                "toss-current-price-v1",
                "005930",
                "10",
                "KRW",
                NOW,
                NOW,
            ),
        ),
    )
    receipts = ReceiptSink()
    malformed_save = type(
        "Save",
        (),
        {
            "status": "SAVED",
            "reason": "saved",
            "inserted_count": 0,
            "updated_count": 0,
            "stored_at": NOW,
        },
    )()
    result = run_quotes_sync(
        tick=tick_id_from_utc(NOW),
        session_result=_sessions(),
        holdings=holdings,
        quotes_provider=lambda _: outcome,
        quote_store=lambda _: malformed_save,
        pack_writer=PackSink(),
        receipt_store=receipts,
        reservation_seal=lambda *args: None,
        incident_writer=lambda *args: None,
        clock=lambda: NOW,
        now=NOW,
        job_lock_path=_lock(tmp_path, "quotes"),
    )
    assert (result.status, result.reason) == ("failed", "failed_store")
    assert result.markets == receipts.value.markets


@pytest.mark.parametrize("runner", ["holdings", "quotes"])
def test_invalid_job_lock_path_fails_closed_without_provider(tmp_path, runner):
    calls = []
    bad = tmp_path / "lock-is-a-directory"
    bad.mkdir()
    common = dict(
        tick=tick_id_from_utc(NOW),
        session_result=_sessions(),
        pack_writer=PackSink(),
        receipt_store=ReceiptSink(),
        reservation_seal=lambda *args: None,
        incident_writer=lambda *args: None,
        clock=lambda: NOW,
        now=NOW,
        job_lock_path=bad,
    )
    if runner == "holdings":
        result = run_holdings_sync(
            holdings_provider=lambda: calls.append(True),
            store=object(),
            **common,
        )
    else:
        result = run_quotes_sync(
            holdings=None,
            quotes_provider=lambda x: calls.append(True),
            quote_store=lambda x: None,
            **common,
        )
    assert not calls
    assert (result.status, result.reason) == ("failed", "failed_store")


@pytest.mark.parametrize("account_count", [0, 1])
def test_malformed_holdings_success_becomes_provider_contract(tmp_path, account_count):
    outcome = TossCampaignOutcome(
        "success",
        "holdings",
        account_count,
        (),
        (),
        account_identities=(),
        _identity_fingerprints={},
    )
    result = run_holdings_sync(
        tick=tick_id_from_utc(NOW),
        session_result=_sessions(),
        holdings_provider=lambda: outcome,
        store=object(),
        pack_writer=PackSink(),
        receipt_store=ReceiptSink(),
        reservation_seal=lambda *args: None,
        incident_writer=lambda *args: None,
        clock=lambda: NOW,
        now=NOW,
        job_lock_path=_lock(tmp_path, "holdings"),
    )
    assert (result.status, result.reason) == (
        "failed",
        "failed_provider_contract",
    )


def test_same_scope_same_fingerprint_duplicate_is_provider_contract(tmp_path):
    scope = derive_account_scope("123")
    identity = AccountScopeIdentityV1(scope, None, 1, None)
    facts = (
        TossAccountHoldingsFact("toss-holdings-v1", NOW, 1, 2, ()),
        TossAccountHoldingsFact("toss-holdings-v1", NOW, 2, 2, ()),
    )
    outcome = TossCampaignOutcome(
        "success",
        "holdings",
        2,
        facts,
        (),
        account_identities=(identity, identity),
        _identity_fingerprints={scope: "a" * 64},
    )
    result = run_holdings_sync(
        tick=tick_id_from_utc(NOW),
        session_result=_sessions(),
        holdings_provider=lambda: outcome,
        store=object(),
        pack_writer=PackSink(),
        receipt_store=ReceiptSink(),
        reservation_seal=lambda *args: None,
        incident_writer=lambda *args: pytest.fail("not a collision"),
        clock=lambda: NOW,
        now=NOW,
        job_lock_path=_lock(tmp_path, "holdings"),
    )
    assert (result.status, result.reason) == (
        "failed",
        "failed_provider_contract",
    )


def test_typed_collision_stops_before_pack_and_emits_incident(tmp_path):
    scope = derive_account_scope("123")
    fact = TossAccountHoldingsFact("toss-holdings-v1", NOW, 1, 1, ())
    outcome = TossCampaignOutcome(
        "success",
        "holdings",
        1,
        (fact,),
        (),
        account_identities=(AccountScopeIdentityV1(scope, None, 1, None),),
        _identity_fingerprints={scope: "b" * 64},
    )

    class CollidingStore:
        def replace(self, state, *, identity_fingerprints):
            raise AccountScopeCollisionError("collision")

    incidents = []
    packs = PackSink()
    result = run_holdings_sync(
        tick=tick_id_from_utc(NOW),
        session_result=_sessions(),
        holdings_provider=lambda: outcome,
        store=CollidingStore(),
        pack_writer=packs,
        receipt_store=ReceiptSink(),
        reservation_seal=lambda *args: None,
        incident_writer=lambda kind, payload: incidents.append((kind, payload)),
        clock=lambda: NOW,
        now=NOW,
        job_lock_path=_lock(tmp_path, "holdings"),
    )
    assert (result.status, result.reason) == ("failed", "failed_store")
    assert packs.pack is None
    assert incidents == [
        ("holdings-identity-collision", frozenset({scope})),
    ]


@pytest.mark.parametrize(
    ("error", "reason", "incident_count"),
    [
        (AccountScopeDuplicateError("duplicate"), "failed_provider_contract", 0),
        (AccountScopeCollisionError("collision"), "failed_store", 1),
        (RuntimeError("unexpected"), "failed_provider_other", 0),
    ],
)
def test_holdings_provider_identity_exception_is_closed_and_preserves_success(
    tmp_path, error, reason, incident_count
):
    receipts = ReceiptSink()
    previous_at = NOW - timedelta(days=1)
    from tokmo.values.material_pack import JobKind
    from tokmo.values.receipt_v1 import (
        AttemptStatus,
        AttemptV1,
        LastSuccessV1,
        MarketFreshness,
        ReceiptV1,
    )

    receipts.value = ReceiptV1(
        1,
        JobKind.HOLDINGS,
        AttemptV1(1, AttemptStatus.SAVED, "saved", previous_at),
        LastSuccessV1(1, "/tmp/old", "0" * 64, previous_at, previous_at),
        {
            "KR": MarketFreshness("KR", "open", 1, previous_at),
            "US": MarketFreshness("US", "open", 1, previous_at),
        },
        None,
    )
    incidents = []
    packs = PackSink()
    result = run_holdings_sync(
        tick=tick_id_from_utc(NOW),
        session_result=_sessions(True, False),
        holdings_provider=lambda: (_ for _ in ()).throw(error),
        store=object(),
        pack_writer=packs,
        receipt_store=receipts,
        reservation_seal=lambda *args: None,
        incident_writer=lambda *args: incidents.append(args),
        clock=lambda: NOW,
        now=NOW,
        job_lock_path=_lock(tmp_path, "holdings"),
    )
    assert (result.status, result.reason) == ("failed", reason)
    assert receipts.value.last_success.tick_id == 1
    assert packs.pack is None
    assert len(incidents) == incident_count
    if incident_count:
        assert incidents[0][0] == "holdings-identity-collision"


@pytest.mark.parametrize(
    "lock_fn",
    [_try_holdings_job_lock, _try_quotes_job_lock],
)
def test_job_lock_closes_fd_once_when_flock_raises_other_oserror(
    tmp_path, monkeypatch, lock_fn
):
    module = __import__(lock_fn.__module__, fromlist=["unused"])
    closes = []
    monkeypatch.setattr(module.os, "open", lambda *args: 41)
    monkeypatch.setattr(
        module.fcntl,
        "flock",
        lambda *args: (_ for _ in ()).throw(OSError("flock failed")),
    )
    monkeypatch.setattr(module.os, "close", lambda fd: closes.append(fd))
    assert lock_fn(tmp_path / "job.lock") == -1
    assert closes == [41]
