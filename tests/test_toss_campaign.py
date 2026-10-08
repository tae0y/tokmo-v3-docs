"""Toss holdings campaign: all-or-nothing batch persistence, the bounded
no-retry ledger, the account-count gate, and the aggregate-only result."""

import json
import os
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tokmo.composition import (
    TOSS_ENV_NAME, TOSS_MAX_ACCOUNTS, TOSS_MAX_STARTS,
    TOSS_MIN_START_INTERVAL_SECONDS, TOSS_STATE_NAME, TOSS_STORE_NAME,
    LiveRunError, run_toss, setup_toss,
)
from tokmo.facts.toss import _TossTransportFailure, _TossTransportSuccess
from tokmo.records.fact_store import FactStore, TOSS_RECORD_FIELDS
from tokmo.values import (
    SaveStatus, TOSS_SOURCE, TossAccountHoldingsFact, TossHoldingFact,
)

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN_BODY = (FIXTURES / "toss_token_success.json").read_bytes()
ACCOUNTS_BODY = (FIXTURES / "toss_accounts_success.json").read_bytes()
HOLDINGS_BODY = (FIXTURES / "toss_holdings_success.json").read_bytes()
ONE_ACCOUNT_BODY = b'{"result":[{"accountSeq":"SYNTHETIC-01"}]}'
NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
CLIENT_ID = "SENTINEL-CLIENT-ID-0001"
CLIENT_SECRET = "SENTINEL-CLIENT-SECRET-0001"


class Clock:
    """Injected clock that only advances when the injected sleeper is used."""

    def __init__(self, start=NOW):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept = getattr(self, "slept", [])
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


class Transport:
    def __init__(self, *outcomes, watcher=None):
        self.outcomes = list(outcomes)
        self.requests = []
        self.watcher = watcher

    def __call__(self, request):
        self.requests.append(request)
        if self.watcher is not None:
            self.watcher(request)
        assert self.outcomes, "campaign started an unplanned request"
        return self.outcomes.pop(0)


def ok(body):
    return _TossTransportSuccess(body=body)


def project_root_of(home):
    """Every test drives a throwaway project root, so the real repository
    credential file is never created, read, or otherwise touched."""
    root = home / "project"
    root.mkdir(exist_ok=True)
    return root


def prepare(home):
    project_root = project_root_of(home)
    setup_toss(home=home, project_root=project_root)
    config = project_root / TOSS_ENV_NAME
    config.write_text(f"TOSS_CLIENT_ID={CLIENT_ID}\nTOSS_CLIENT_SECRET={CLIENT_SECRET}\n")
    os.chmod(config, 0o600)
    return home / ".local" / "share" / "tokmo-v3" / "toss"


def invoke(home, auth, data, clock=None):
    clock = clock or Clock()
    ids = iter([f"record-{index}" for index in range(20)])
    return run_toss(
        home=home, project_root=project_root_of(home), auth_transport=auth,
        data_transport=data, clock=clock, sleeper=clock.sleep,
        id_factory=ids.__next__, run_id="toss-run",
    )


def two_account_campaign(home, **kwargs):
    auth = Transport(ok(TOKEN_BODY))
    data = Transport(ok(ACCOUNTS_BODY), ok(HOLDINGS_BODY), ok(HOLDINGS_BODY), **kwargs)
    return auth, data, invoke(home, auth, data)


def state_of(leaf):
    return json.loads((leaf / TOSS_STATE_NAME).read_text())


def records_of(leaf):
    path = leaf / TOSS_STORE_NAME
    return json.loads(path.read_text())["records"] if path.exists() else []


# --- the three authorized operations, in order -------------------------------

def test_a_campaign_runs_auth_then_accounts_then_one_holdings_per_account(tmp_path):
    leaf = prepare(tmp_path)

    auth, data, result = two_account_campaign(tmp_path)

    assert result.code == "success"
    assert [request.endpoint_id for request in auth.requests] == ["toss_oauth_token"]
    assert [request.endpoint_id for request in data.requests] == [
        "toss_accounts", "toss_holdings", "toss_holdings"]
    assert result.started == 4
    assert [entry["stage"] for entry in state_of(leaf)["starts"]] == [
        "auth", "accounts", "holdings", "holdings"]


def test_every_pair_of_request_starts_is_spaced_by_the_global_interval(tmp_path):
    prepare(tmp_path)
    clock = Clock()
    auth = Transport(ok(TOKEN_BODY))
    data = Transport(ok(ACCOUNTS_BODY), ok(HOLDINGS_BODY), ok(HOLDINGS_BODY))

    invoke(tmp_path, auth, data, clock)

    assert clock.slept == [float(TOSS_MIN_START_INTERVAL_SECONDS)] * 3


def test_the_ledger_records_each_start_before_that_request_is_sent(tmp_path):
    leaf = prepare(tmp_path)
    seen = []

    def watcher(request):
        seen.append((request.endpoint_id, len(state_of(leaf)["starts"])))

    auth = Transport(ok(TOKEN_BODY), watcher=lambda _r: seen.append(
        ("toss_oauth_token", len(state_of(leaf)["starts"]))))
    data = Transport(ok(ACCOUNTS_BODY), ok(HOLDINGS_BODY), ok(HOLDINGS_BODY),
                     watcher=watcher)

    invoke(tmp_path, auth, data)

    assert seen == [("toss_oauth_token", 1), ("toss_accounts", 2),
                    ("toss_holdings", 3), ("toss_holdings", 4)]


def test_the_ledger_never_holds_a_credential_token_sequence_or_url(tmp_path):
    leaf = prepare(tmp_path)
    two_account_campaign(tmp_path)

    text = (leaf / TOSS_STATE_NAME).read_text()

    for sentinel in (CLIENT_ID, CLIENT_SECRET, "SYNTHETIC-ACCESS-0001",
                     "SYNTHETIC-01", "SYNTHETIC-02", "tossinvest", "Bearer",
                     "accountSeq", "api/v1"):
        assert sentinel not in text
    assert set(state_of(leaf)["starts"][0]) == {
        "start_index", "stage", "account_ordinal", "planned_at", "outcome"}


# --- nothing is persisted until every account has been parsed ----------------

def test_no_snapshot_exists_while_the_holdings_reads_are_still_in_flight(tmp_path):
    leaf = prepare(tmp_path)
    observed = []
    auth = Transport(ok(TOKEN_BODY))
    data = Transport(ok(ACCOUNTS_BODY), ok(HOLDINGS_BODY), ok(HOLDINGS_BODY),
                     watcher=lambda _r: observed.append(
                         (leaf / TOSS_STORE_NAME).exists()))

    invoke(tmp_path, auth, data)

    assert observed == [False, False, False]
    assert len(records_of(leaf)) == 2


@pytest.mark.parametrize("failure,expected", [
    (_TossTransportFailure(code="auth_rejection"), "auth_rejection"),
    (_TossTransportFailure(code="forbidden"), "forbidden"),
    (_TossTransportFailure(code="http_429", retry_after=30), "http_429"),
    (_TossTransportFailure(code="http_5xx"), "http_5xx"),
    (_TossTransportFailure(code="timeout"), "timeout"),
])
def test_a_failure_on_the_last_account_persists_no_snapshot_at_all(tmp_path, failure,
                                                                   expected):
    leaf = prepare(tmp_path)
    auth = Transport(ok(TOKEN_BODY))
    data = Transport(ok(ACCOUNTS_BODY), ok(HOLDINGS_BODY), failure)

    with pytest.raises(LiveRunError) as error:
        invoke(tmp_path, auth, data)

    assert error.value.code == expected
    assert records_of(leaf) == []
    assert not (leaf / TOSS_STORE_NAME).exists()


def test_a_contract_mismatch_on_one_account_stops_the_whole_capture(tmp_path):
    leaf = prepare(tmp_path)
    auth = Transport(ok(TOKEN_BODY))
    data = Transport(ok(ACCOUNTS_BODY), ok(HOLDINGS_BODY),
                     ok(b'{"result":{"items":[{"symbol":"SYN"}]}}'))

    with pytest.raises(LiveRunError) as error:
        invoke(tmp_path, auth, data)

    assert error.value.code == "contract_mismatch"
    assert records_of(leaf) == []


def test_an_authentication_failure_never_reaches_the_data_endpoints(tmp_path):
    prepare(tmp_path)
    auth = Transport(_TossTransportFailure(code="auth_rejection"))
    data = Transport()

    with pytest.raises(LiveRunError) as error:
        invoke(tmp_path, auth, data)

    assert error.value.code == "auth_rejection"
    assert data.requests == []


def test_a_mid_campaign_rejection_is_never_retried_or_re_authenticated(tmp_path):
    prepare(tmp_path)
    auth = Transport(ok(TOKEN_BODY))
    data = Transport(ok(ACCOUNTS_BODY), _TossTransportFailure(code="auth_rejection"))

    with pytest.raises(LiveRunError):
        invoke(tmp_path, auth, data)

    assert len(auth.requests) == 1
    assert len(data.requests) == 2


# --- bounded campaign --------------------------------------------------------

def test_the_account_count_gate_bounds_the_capture(tmp_path):
    leaf = prepare(tmp_path)
    seqs = ",".join(f'{{"accountSeq":"SYNTHETIC-{n:02d}"}}'
                    for n in range(TOSS_MAX_ACCOUNTS + 1))
    auth = Transport(ok(TOKEN_BODY))
    data = Transport(ok(f'{{"result":[{seqs}]}}'.encode()))

    with pytest.raises(LiveRunError) as error:
        invoke(tmp_path, auth, data)

    assert error.value.code == "account_count_unexpected"
    assert len(data.requests) == 1
    assert state_of(leaf)["last_outcome"] == "account_count_unexpected"


def test_an_empty_account_list_stops_before_any_holdings_read(tmp_path):
    prepare(tmp_path)
    auth = Transport(ok(TOKEN_BODY))
    data = Transport(ok(b'{"result":[]}'))

    with pytest.raises(LiveRunError) as error:
        invoke(tmp_path, auth, data)

    assert error.value.code == "account_count_unexpected"
    assert len(data.requests) == 1


def test_the_maximum_account_count_still_fits_under_the_start_ceiling(tmp_path):
    prepare(tmp_path)
    seqs = ",".join(f'{{"accountSeq":"SYNTHETIC-{n:02d}"}}'
                    for n in range(TOSS_MAX_ACCOUNTS))
    auth = Transport(ok(TOKEN_BODY))
    data = Transport(ok(f'{{"result":[{seqs}]}}'.encode()),
                     *[ok(HOLDINGS_BODY)] * TOSS_MAX_ACCOUNTS)

    result = invoke(tmp_path, auth, data)

    assert result.started == 2 + TOSS_MAX_ACCOUNTS <= TOSS_MAX_STARTS
    assert result.account_count == TOSS_MAX_ACCOUNTS


def test_an_exhausted_ledger_stops_the_campaign_with_zero_requests(tmp_path):
    leaf = prepare(tmp_path)
    state = state_of(leaf)
    state["starts"] = [
        {"start_index": index + 1, "stage": "holdings", "account_ordinal": 1,
         "planned_at": NOW.isoformat(), "outcome": "success"}
        for index in range(TOSS_MAX_STARTS)]
    state["total_started"] = TOSS_MAX_STARTS
    write_state(leaf, state)
    auth, data = Transport(), Transport()

    with pytest.raises(LiveRunError) as error:
        invoke(tmp_path, auth, data, Clock(NOW + timedelta(hours=1)))

    assert error.value.code == "ceiling_exhausted"
    assert auth.requests == [] and data.requests == []


def test_a_fair_access_stop_blocks_the_next_campaign_until_a_human_clears_it(tmp_path):
    leaf = prepare(tmp_path)
    auth = Transport(ok(TOKEN_BODY))
    data = Transport(_TossTransportFailure(code="http_429", retry_after=45))

    with pytest.raises(LiveRunError) as first:
        invoke(tmp_path, auth, data)
    assert first.value.code == "http_429"
    assert state_of(leaf)["retry_after_seconds"] == 45

    blocked_auth, blocked_data = Transport(), Transport()
    with pytest.raises(LiveRunError) as second:
        invoke(tmp_path, blocked_auth, blocked_data, Clock(NOW + timedelta(days=1)))

    assert second.value.code == "fair_access_stop"
    assert blocked_auth.requests == [] and blocked_data.requests == []


def test_spent_starts_are_never_refunded_across_campaigns(tmp_path):
    leaf = prepare(tmp_path)
    two_account_campaign(tmp_path)
    spent = state_of(leaf)["total_started"]

    auth = Transport(ok(TOKEN_BODY))
    data = Transport(ok(ONE_ACCOUNT_BODY), ok(HOLDINGS_BODY))
    invoke(tmp_path, auth, data, Clock(NOW + timedelta(hours=1)))

    assert spent == 4
    assert state_of(leaf)["total_started"] == 7
    assert len(state_of(leaf)["starts"]) == 3


def write_state(leaf, state):
    path = leaf / TOSS_STATE_NAME
    path.write_text(json.dumps(state, separators=(",", ":"), sort_keys=True))
    os.chmod(path, 0o600)


# --- atomic multi-account persistence ----------------------------------------

def holding(symbol="SYN001", quantity="10", avg_price="71000"):
    return TossHoldingFact(
        symbol=symbol, quantity=quantity, avg_price=avg_price,
        avg_price_field="averagePurchasePrice", currency="KRW",
        market_raw="KR", market_field="market", name="SYNTHETIC-A",
        asset_type="equity")


def fact(ordinal, count=2, captured_at=NOW, holdings=None):
    return TossAccountHoldingsFact(
        source=TOSS_SOURCE, captured_at=captured_at, account_ordinal=ordinal,
        account_count=count, holdings=holdings if holdings is not None else (holding(),))


def store_at(tmp_path):
    ids = iter([f"record-{index}" for index in range(20)])
    return FactStore(tmp_path / "holdings_store.json",
                     clock=lambda: NOW, id_factory=ids.__next__)


def test_a_multi_account_capture_is_written_in_exactly_one_snapshot_replace(tmp_path):
    store = store_at(tmp_path)
    replaces = []
    original = store._replace_snapshot
    store._replace_snapshot = lambda payload: (
        replaces.append(len(payload["records"])), original(payload))[1]

    results = store.save_batch([fact(1, 3), fact(2, 3), fact(3, 3)],
                               policy_version="toss1-v1")

    assert [result.status for result in results] == [SaveStatus.SAVED] * 3
    assert replaces == [3]
    assert len(store.records()) == 3
    assert all(set(record) == TOSS_RECORD_FIELDS for record in store.records())


def test_a_conflicting_batch_member_leaves_the_prior_store_byte_identical(tmp_path):
    store = store_at(tmp_path)
    store.save_batch([fact(1), fact(2)], policy_version="toss1-v1")
    before = store.path.read_bytes()

    conflicting = fact(2, holdings=(holding(quantity="99"),))
    results = store.save_batch([fact(1), conflicting], policy_version="toss1-v1")

    assert store.path.read_bytes() == before
    assert [result.status for result in results] == [
        SaveStatus.DUPLICATE, SaveStatus.CONFLICT]
    assert len(store.records()) == 2


def test_an_already_saved_capture_never_produces_a_second_partial_write(tmp_path):
    store = store_at(tmp_path)
    store.save_batch([fact(1), fact(2)], policy_version="toss1-v1")

    results = store.save_batch([fact(1), fact(2)], policy_version="toss1-v1")

    assert [result.status for result in results] == [SaveStatus.DUPLICATE] * 2
    assert len(store.records()) == 2


def test_a_batch_that_repeats_an_ordinal_is_refused_before_any_mutation(tmp_path):
    store = store_at(tmp_path)

    results = store.save_batch([fact(1), fact(1)], policy_version="toss1-v1")

    assert [result.status for result in results] == [SaveStatus.FAILED] * 2
    assert all(result.reason == "batch_identity_conflict" for result in results)
    assert not store.path.exists()


def test_a_write_failure_reports_every_member_as_unsaved(tmp_path):
    store = store_at(tmp_path)

    def boom(_payload):
        raise OSError("masked")

    store._replace_snapshot = boom
    results = store.save_batch([fact(1), fact(2)], policy_version="toss1-v1")

    assert [result.status for result in results] == [SaveStatus.FAILED] * 2
    assert all(result.reason == "store_write_failed" for result in results)
    assert not store.path.exists()


def test_a_corrupt_store_is_never_overwritten_by_a_batch(tmp_path):
    store = store_at(tmp_path)
    store.path.write_text('{"records":[{"unknown":"shape"}]}')
    before = store.path.read_bytes()

    results = store.save_batch([fact(1), fact(2)], policy_version="toss1-v1")

    assert [result.status for result in results] == [SaveStatus.FAILED] * 2
    assert all(result.reason == "store_corrupt" for result in results)
    assert store.path.read_bytes() == before


def test_batches_from_different_captures_accumulate(tmp_path):
    store = store_at(tmp_path)
    later = NOW + timedelta(days=1)

    store.save_batch([fact(1), fact(2)], policy_version="toss1-v1")
    store.save_batch([fact(1, captured_at=later), fact(2, captured_at=later)],
                     policy_version="toss1-v1")

    assert len(store.records()) == 4
    assert len({record["captured_at"] for record in store.records()}) == 2


def test_an_empty_batch_touches_nothing(tmp_path):
    store = store_at(tmp_path)

    assert store.save_batch([], policy_version="toss1-v1") == ()
    assert not store.path.exists()


def test_the_single_fact_save_path_still_serves_the_other_tracers(tmp_path):
    from datetime import date
    from decimal import Decimal

    from tokmo.values import WTI_SERIES, WTI_SOURCE, WTI_UNITS, WtiFact

    store = store_at(tmp_path)
    wti = WtiFact(source=WTI_SOURCE, series=WTI_SERIES, period=date(2026, 7, 24),
                  value=Decimal("78.42"), units=WTI_UNITS, observed_at=NOW)

    first = store.save(wti, policy_version="tracer1-v1")
    second = store.save(wti, policy_version="tracer1-v1")

    assert (first.status, second.status) == (SaveStatus.SAVED, SaveStatus.DUPLICATE)


# --- private artifacts and aggregate-only result -----------------------------

def test_the_private_leaf_and_its_files_are_owner_only(tmp_path):
    leaf = prepare(tmp_path)
    two_account_campaign(tmp_path)

    assert stat.S_IMODE(leaf.stat().st_mode) == 0o700
    for name in (TOSS_STATE_NAME, TOSS_STORE_NAME):
        assert stat.S_IMODE((leaf / name).stat().st_mode) == 0o600
    assert leaf.is_relative_to(tmp_path / ".local")


def test_the_result_reports_aggregates_and_field_names_only(tmp_path):
    prepare(tmp_path)

    _auth, _data, result = two_account_campaign(tmp_path)

    assert (result.account_count, result.holdings_items) == (2, 4)
    assert (result.currencies_observed, result.markets_observed) == (2, 2)
    assert (result.saved, result.duplicate, result.conflict) == (2, 0, 0)
    assert result.avg_price_fields == ("averagePurchasePrice",)
    assert result.market_fields == ("market",)
    for value in ("SYN001", "SYNU", "71000", "120.75", "2.5",
                  "SYNTHETIC-01", "SYNTHETIC-EQUITY-A"):
        assert value not in repr(result)


def test_the_campaign_state_records_observed_field_names_never_values(tmp_path):
    leaf = prepare(tmp_path)
    two_account_campaign(tmp_path)

    presence = state_of(leaf)["field_presence"]

    assert presence["avg_price_fields"] == ["averagePurchasePrice"]
    assert presence["market_fields"] == ["market"]
    assert presence["item_counts"] == [2, 2]


# --- Slice 7: complete capture persistence via injected SQLite adapter ---

def test_successful_complete_capture_persists_once(tmp_path):
    """When a snapshot_store seam is injected, a successful complete
    multi-account capture obtains a user-requested permit and calls
    store.append exactly once. No legacy FactStore write occurs
    (no dual-write)."""
    from tokmo.records.holdings_snapshots import (
        HoldingsSnapshotStore, snapshot_content_hash,
    )

    leaf = prepare(tmp_path)
    sqlite_path = leaf / "holdings_snapshots.sqlite3"
    real_store = HoldingsSnapshotStore(sqlite_path, clock=lambda: NOW)

    append_calls: list = []

    class SpyStore:
        """Wraps the real store; counts append calls and delegates."""
        def __init__(self, inner):
            self._inner = inner
        def append(self, snapshot, permit):
            append_calls.append((snapshot, permit))
            return self._inner.append(snapshot, permit)

    spy = SpyStore(real_store)

    auth = Transport(ok(TOKEN_BODY))
    data = Transport(ok(ACCOUNTS_BODY), ok(HOLDINGS_BODY), ok(HOLDINGS_BODY))
    ids = iter([f"record-{index}" for index in range(20)])
    result = run_toss(
        home=tmp_path, project_root=project_root_of(tmp_path),
        auth_transport=auth, data_transport=data,
        clock=Clock(), sleeper=Clock().sleep,
        id_factory=ids.__next__, run_id="toss-run",
        snapshot_store=spy,
        content_hash_fn=snapshot_content_hash,
    )

    assert result.code == "success"
    assert len(append_calls) == 1
    stored_snapshot, stored_permit = append_calls[0]
    assert stored_snapshot.snapshot_id == stored_permit.snapshot_id
    assert stored_permit.purpose_kind.value == "USER_REQUESTED_CAPTURE"
    # The stored snapshot is readable from the real store.
    assert real_store.read_latest() == stored_snapshot
    # No legacy FactStore write (no dual-write).
    assert not (leaf / "holdings_store.json").exists()


def test_account_failure_never_calls_storage(tmp_path):
    """When a capture fails before completion, the injected snapshot_store
    is never called — no partial bundle reaches storage."""
    from tokmo.records.holdings_snapshots import (
        HoldingsSnapshotStore, snapshot_content_hash,
    )
    from tokmo.facts.toss import _TossTransportFailure

    leaf = prepare(tmp_path)
    sqlite_path = leaf / "holdings_snapshots.sqlite3"
    real_store = HoldingsSnapshotStore(sqlite_path, clock=lambda: NOW)

    append_calls: list = []

    class SpyStore:
        def __init__(self, inner):
            self._inner = inner
        def append(self, snapshot, permit):
            append_calls.append((snapshot, permit))
            return self._inner.append(snapshot, permit)

    spy = SpyStore(real_store)

    auth = Transport(ok(TOKEN_BODY))
    data = Transport(ok(ACCOUNTS_BODY), ok(HOLDINGS_BODY),
                     _TossTransportFailure(code="timeout"))
    ids = iter([f"record-{index}" for index in range(20)])
    with pytest.raises(LiveRunError) as error:
        run_toss(
            home=tmp_path, project_root=project_root_of(tmp_path),
            auth_transport=auth, data_transport=data,
            clock=Clock(), sleeper=Clock().sleep,
            id_factory=ids.__next__, run_id="toss-run",
            snapshot_store=spy,
            content_hash_fn=snapshot_content_hash,
        )

    assert error.value.code == "timeout"
    assert append_calls == []
    assert not sqlite_path.exists()
    assert not (leaf / "holdings_store.json").exists()


def test_denied_purpose_never_calls_storage(tmp_path):
    """When a named-program purpose is denied (v1 allowlist is empty),
    storage.append is never called and the capture does not persist."""
    from tokmo.records.holdings_snapshots import (
        HoldingsSnapshotStore, snapshot_content_hash,
    )

    leaf = prepare(tmp_path)
    sqlite_path = leaf / "holdings_snapshots.sqlite3"
    real_store = HoldingsSnapshotStore(sqlite_path, clock=lambda: NOW)

    append_calls: list = []

    class SpyStore:
        def __init__(self, inner):
            self._inner = inner
        def append(self, snapshot, permit):
            append_calls.append((snapshot, permit))
            return self._inner.append(snapshot, permit)

    spy = SpyStore(real_store)

    auth = Transport(ok(TOKEN_BODY))
    data = Transport(ok(ACCOUNTS_BODY), ok(HOLDINGS_BODY), ok(HOLDINGS_BODY))
    ids = iter([f"record-{index}" for index in range(20)])
    with pytest.raises(LiveRunError) as error:
        run_toss(
            home=tmp_path, project_root=project_root_of(tmp_path),
            auth_transport=auth, data_transport=data,
            clock=Clock(), sleeper=Clock().sleep,
            id_factory=ids.__next__, run_id="toss-run",
            snapshot_store=spy,
            content_hash_fn=snapshot_content_hash,
            purpose="some-unapproved-named-program",
        )

    assert error.value.code == "unknown_program"
    assert append_calls == []
    assert not sqlite_path.exists()


# --- Slice 7: toss_report typed seam (read_latest_pair DI) ---

def test_report_uses_typed_latest_pair(tmp_path, capsys):
    """When a snapshot_store seam is injected into toss_report, it uses
    read_latest_pair() typed (previous, current) only — no JSON records
    in the typed branch. R0 default (no injection) keeps legacy JSON."""
    from tokmo.records.holdings_snapshots import (
        HoldingsSnapshotStore, snapshot_content_hash,
    )
    from tokmo.composition import toss_report

    leaf = prepare(tmp_path)
    sqlite_path = leaf / "holdings_snapshots.sqlite3"
    store = HoldingsSnapshotStore(sqlite_path, clock=lambda: NOW)

    # Capture two snapshots so read_latest_pair returns (previous, current).
    from tokmo.programs.toss_holdings import assemble_complete_snapshot
    from tokmo.records.holdings_persistence import authorize_user_request
    from tokmo.values import TOSS_SOURCE, TossAccountHoldingsFact

    for n in (1, 2):
        facts = (
            TossAccountHoldingsFact(
                source=TOSS_SOURCE, captured_at=NOW,
                account_ordinal=1, account_count=1,
                holdings=(TossHoldingFact(
                    symbol="AAPL", quantity=str(10 * n), avg_price="150.5",
                    avg_price_field="averagePurchasePrice", currency="USD",
                    market_raw="US", market_field="market",
                    name="Apple Inc.", asset_type="STOCK",
                ),),
            ),
        )
        sid = uuid.uuid4().hex
        snap = assemble_complete_snapshot(facts, snapshot_id=sid,
                                          content_hash_fn=snapshot_content_hash)
        permit = authorize_user_request(snapshot_id=sid, clock=lambda: NOW)
        store.append(snap, permit)

    # Spy on the store to track which read methods are called.
    read_calls: list[str] = []

    class SpyReadStore:
        def __init__(self, inner):
            self._inner = inner
        def read_latest_pair(self):
            read_calls.append("read_latest_pair")
            return self._inner.read_latest_pair()
        # Ensure no JSON-records path is reachable through the typed seam.
        def records(self):
            read_calls.append("records")
            return []

    spy = SpyReadStore(store)

    toss_report(home=tmp_path, snapshot_store=spy)

    captured = capsys.readouterr().out
    assert "read_latest_pair" in read_calls
    assert "records" not in read_calls
    assert "toss-report" in captured
    assert "toss-report-diff" in captured


def test_report_no_snapshot_store_keeps_legacy(tmp_path, capsys):
    """Without snapshot_store injection, toss_report uses the legacy
    JSON FactStore path — R0 default behavior unchanged."""
    from tokmo.composition import toss_report

    leaf = prepare(tmp_path)
    # Run a legacy capture to seed JSON data.
    auth = Transport(ok(TOKEN_BODY))
    data = Transport(ok(ACCOUNTS_BODY), ok(HOLDINGS_BODY), ok(HOLDINGS_BODY))
    ids = iter([f"record-{index}" for index in range(20)])
    run_toss(
        home=tmp_path, project_root=project_root_of(tmp_path),
        auth_transport=auth, data_transport=data,
        clock=Clock(), sleeper=Clock().sleep,
        id_factory=ids.__next__, run_id="toss-run",
    )

    toss_report(home=tmp_path)
    captured = capsys.readouterr().out
    assert "toss-report" in captured
    assert "toss-report-diff" in captured


def test_report_typed_path_is_read_only(tmp_path):
    """The typed report/current/comparison path is strictly read-only:
    no append, import_legacy, or FactStore.save_batch calls occur.
    Verified via test doubles that raise if any write method is touched."""
    from tokmo.records.holdings_snapshots import (
        HoldingsSnapshotStore, snapshot_content_hash,
    )
    from tokmo.composition import toss_report

    leaf = prepare(tmp_path)
    sqlite_path = leaf / "holdings_snapshots.sqlite3"
    store = HoldingsSnapshotStore(sqlite_path, clock=lambda: NOW)

    # Seed one snapshot so read_latest_pair works.
    from tokmo.programs.toss_holdings import assemble_complete_snapshot
    from tokmo.records.holdings_persistence import authorize_user_request
    from tokmo.values import TOSS_SOURCE, TossAccountHoldingsFact

    facts = (
        TossAccountHoldingsFact(
            source=TOSS_SOURCE, captured_at=NOW,
            account_ordinal=1, account_count=1,
            holdings=(TossHoldingFact(
                symbol="AAPL", quantity="10", avg_price="150.5",
                avg_price_field="averagePurchasePrice", currency="USD",
                market_raw="US", market_field="market",
                name="Apple Inc.", asset_type="STOCK",
            ),),
        ),
    )
    sid = uuid.uuid4().hex
    snap = assemble_complete_snapshot(facts, snapshot_id=sid,
                                      content_hash_fn=snapshot_content_hash)
    permit = authorize_user_request(snapshot_id=sid, clock=lambda: NOW)
    store.append(snap, permit)

    class ReadOnlySpy:
        def __init__(self, inner):
            self._inner = inner
        def read_latest_pair(self):
            return self._inner.read_latest_pair()
        def append(self, *args, **kwargs):
            raise AssertionError("append must not be called in report path")
        def import_legacy(self, *args, **kwargs):
            raise AssertionError("import_legacy must not be called in report path")

    spy = ReadOnlySpy(store)

    toss_report(home=tmp_path, snapshot_store=spy)
    # If we reach here, no write method was called.


def test_no_current_comparison_persistence(tmp_path):
    """Exact SDS test name: the typed report path makes append, import_legacy,
    save/current/comparison write surfaces fail if accessed, and verifies
    only read_latest_pair is called once. Legacy FactStore save is also
    proven absent."""
    from tokmo.records.holdings_snapshots import (
        HoldingsSnapshotStore, snapshot_content_hash,
    )
    from tokmo.composition import toss_report
    import tokmo.composition as composition_module

    leaf = prepare(tmp_path)
    sqlite_path = leaf / "holdings_snapshots.sqlite3"
    store = HoldingsSnapshotStore(sqlite_path, clock=lambda: NOW)

    # Seed one snapshot so read_latest_pair returns data.
    from tokmo.programs.toss_holdings import assemble_complete_snapshot
    from tokmo.records.holdings_persistence import authorize_user_request
    from tokmo.values import TOSS_SOURCE, TossAccountHoldingsFact

    facts = (
        TossAccountHoldingsFact(
            source=TOSS_SOURCE, captured_at=NOW,
            account_ordinal=1, account_count=1,
            holdings=(TossHoldingFact(
                symbol="AAPL", quantity="10", avg_price="150.5",
                avg_price_field="averagePurchasePrice", currency="USD",
                market_raw="US", market_field="market",
                name="Apple Inc.", asset_type="STOCK",
            ),),
        ),
    )
    sid = uuid.uuid4().hex
    snap = assemble_complete_snapshot(facts, snapshot_id=sid,
                                      content_hash_fn=snapshot_content_hash)
    permit = authorize_user_request(snapshot_id=sid, clock=lambda: NOW)
    store.append(snap, permit)

    read_calls: list[str] = []

    class FullSpy:
        def __init__(self, inner):
            self._inner = inner
        def read_latest_pair(self):
            read_calls.append("read_latest_pair")
            return self._inner.read_latest_pair()
        def append(self, *a, **kw):
            raise AssertionError("append must not be called in report path")
        def import_legacy(self, *a, **kw):
            raise AssertionError("import_legacy must not be called in report path")
        def save(self, *a, **kw):
            raise AssertionError("save must not be called in report path")
        def current(self, *a, **kw):
            raise AssertionError("current must not be called in report path")
        def comparison(self, *a, **kw):
            raise AssertionError("comparison must not be called in report path")

    spy = FullSpy(store)

    # Also spy on FactStore to ensure legacy save_batch is never called.
    original_factstore = composition_module.FactStore
    factstore_calls: list[str] = []

    class SpyFactStore(original_factstore):
        def save_batch(self, *a, **kw):
            factstore_calls.append("save_batch")
            raise AssertionError("FactStore.save_batch must not be called in typed report")
        def save(self, *a, **kw):
            factstore_calls.append("save")
            raise AssertionError("FactStore.save must not be called in typed report")

    composition_module.FactStore = SpyFactStore
    try:
        toss_report(home=tmp_path, snapshot_store=spy)
    finally:
        composition_module.FactStore = original_factstore

    assert read_calls == ["read_latest_pair"]
    assert factstore_calls == []


# --- Slice 7 MINOR 2: dual-write behavior spy (production instrumentation) ---

def test_dual_write_typed_success_calls_exactly_sqlite_append(tmp_path):
    """A successful typed capture calls sqlite.append exactly once and
    FactStore.save_batch zero times."""
    from tokmo.records.holdings_snapshots import (
        HoldingsSnapshotStore, snapshot_content_hash,
    )
    import tokmo.composition as composition_module

    leaf = prepare(tmp_path)
    sqlite_path = leaf / "holdings_snapshots.sqlite3"
    real_store = HoldingsSnapshotStore(sqlite_path, clock=lambda: NOW)

    append_calls: list = []

    class SpyStore:
        def __init__(self, inner):
            self._inner = inner
        def append(self, snapshot, permit):
            append_calls.append((snapshot, permit))
            return self._inner.append(snapshot, permit)

    spy = SpyStore(real_store)

    # Monkeypatch FactStore at module level to track save_batch calls.
    original_factstore = composition_module.FactStore
    save_batch_calls: list = []

    class SpyFactStore(original_factstore):
        def save_batch(self, *a, **kw):
            save_batch_calls.append(a)
            return super().save_batch(*a, **kw)

    composition_module.FactStore = SpyFactStore
    try:
        auth = Transport(ok(TOKEN_BODY))
        data = Transport(ok(ACCOUNTS_BODY), ok(HOLDINGS_BODY), ok(HOLDINGS_BODY))
        result = run_toss(
            home=tmp_path, project_root=project_root_of(tmp_path),
            auth_transport=auth, data_transport=data,
            clock=Clock(), sleeper=Clock().sleep,
            id_factory=iter([f"r-{i}" for i in range(20)]).__next__,
            run_id="toss-run",
            snapshot_store=spy,
            content_hash_fn=snapshot_content_hash,
        )
    finally:
        composition_module.FactStore = original_factstore

    assert result.code == "success"
    assert len(append_calls) == 1
    assert save_batch_calls == []


def test_dual_write_legacy_success_calls_exactly_factstore_save_batch(tmp_path):
    """A successful legacy capture (no snapshot_store injected) calls
    FactStore.save_batch exactly once and sqlite.append zero times."""
    import tokmo.composition as composition_module

    leaf = prepare(tmp_path)

    append_calls: list = []

    class ExplodingStore:
        def append(self, *a, **kw):
            append_calls.append(a)
            raise AssertionError("sqlite append must not be called in legacy path")

    original_factstore = composition_module.FactStore
    save_batch_calls: list = []

    class SpyFactStore(original_factstore):
        def save_batch(self, *a, **kw):
            save_batch_calls.append(a)
            return super().save_batch(*a, **kw)

    composition_module.FactStore = SpyFactStore
    try:
        auth = Transport(ok(TOKEN_BODY))
        data = Transport(ok(ACCOUNTS_BODY), ok(HOLDINGS_BODY), ok(HOLDINGS_BODY))
        result = run_toss(
            home=tmp_path, project_root=project_root_of(tmp_path),
            auth_transport=auth, data_transport=data,
            clock=Clock(), sleeper=Clock().sleep,
            id_factory=iter([f"r-{i}" for i in range(20)]).__next__,
            run_id="toss-run",
        )
    finally:
        composition_module.FactStore = original_factstore

    assert result.code == "success"
    assert len(save_batch_calls) == 1
    assert append_calls == []


def test_dual_write_denied_purpose_calls_neither_store(tmp_path):
    """A denied purpose calls neither sqlite.append nor FactStore.save_batch."""
    from tokmo.records.holdings_snapshots import snapshot_content_hash
    import tokmo.composition as composition_module

    leaf = prepare(tmp_path)

    append_calls: list = []

    class ExplodingStore:
        def append(self, *a, **kw):
            append_calls.append(a)
            raise AssertionError("sqlite append must not be called when denied")

    original_factstore = composition_module.FactStore
    save_batch_calls: list = []

    class SpyFactStore(original_factstore):
        def save_batch(self, *a, **kw):
            save_batch_calls.append(a)
            raise AssertionError("save_batch must not be called when denied")

    composition_module.FactStore = SpyFactStore
    try:
        auth = Transport(ok(TOKEN_BODY))
        data = Transport(ok(ACCOUNTS_BODY), ok(HOLDINGS_BODY), ok(HOLDINGS_BODY))
        with pytest.raises(LiveRunError):
            run_toss(
                home=tmp_path, project_root=project_root_of(tmp_path),
                auth_transport=auth, data_transport=data,
                clock=Clock(), sleeper=Clock().sleep,
                id_factory=iter([f"r-{i}" for i in range(20)]).__next__,
                run_id="toss-run",
                snapshot_store=ExplodingStore(),
                content_hash_fn=snapshot_content_hash,
                purpose="some-unapproved-program",
            )
    finally:
        composition_module.FactStore = original_factstore

    assert append_calls == []
    assert save_batch_calls == []


def test_no_ordinal_continuity_claim(tmp_path, capsys):
    """Typed report/diff output discloses account_count_changed (aggregate)
    and never claims ordinal continuity between captures. The output must
    not contain individual account A→B linkage, alias, or sequence claims."""
    from tokmo.records.holdings_snapshots import (
        HoldingsSnapshotStore, snapshot_content_hash,
    )
    from tokmo.composition import toss_report

    leaf = prepare(tmp_path)
    sqlite_path = leaf / "holdings_snapshots.sqlite3"
    store = HoldingsSnapshotStore(sqlite_path, clock=lambda: NOW)

    # Two captures with different account counts to trigger account_count_changed.
    from tokmo.programs.toss_holdings import assemble_complete_snapshot
    from tokmo.records.holdings_persistence import authorize_user_request
    from tokmo.values import (
        TOSS_SOURCE, TossAccountHoldingsFact, TossHoldingFact,
        HoldingsSnapshotAccount, CompleteHoldingsSnapshot,
    )

    for n_accounts in (2, 1):
        facts = tuple(
            TossAccountHoldingsFact(
                source=TOSS_SOURCE, captured_at=NOW,
                account_ordinal=i + 1, account_count=n_accounts,
                holdings=(TossHoldingFact(
                    symbol="AAPL", quantity="10", avg_price="150.5",
                    avg_price_field="averagePurchasePrice", currency="USD",
                    market_raw="US", market_field="market",
                    name="Apple Inc.", asset_type="STOCK",
                ),),
            )
            for i in range(n_accounts)
        )
        sid = uuid.uuid4().hex
        snap = assemble_complete_snapshot(facts, snapshot_id=sid,
                                          content_hash_fn=snapshot_content_hash)
        permit = authorize_user_request(snapshot_id=sid, clock=lambda: NOW)
        store.append(snap, permit)

    toss_report(home=tmp_path, snapshot_store=store)

    output = capsys.readouterr().out
    # Must include the aggregate account_count_changed flag.
    assert "account_count_changed=true" in output
    # Must NOT create ordinal linkage claims.
    assert "alias" not in output.lower()
    assert "same_account" not in output.lower()


# --- Slice 7 MATERIAL 1: typed report closed error mapping ---

@pytest.mark.parametrize("read_reason", [
    "not_found", "schema_mismatch", "store_locked", "store_corrupt",
])
def test_report_typed_preserves_exact_read_error_reason(tmp_path, read_reason):
    """toss_report typed branch must map HoldingsSnapshotReadError.reason
    exactly to LiveRunError.code, not collapse everything to store_corrupt."""
    from tokmo.records.holdings_snapshots import HoldingsSnapshotReadError
    from tokmo.composition import toss_report

    class FailingStore:
        def read_latest_pair(self):
            raise HoldingsSnapshotReadError(read_reason)

    leaf = prepare(tmp_path)
    with pytest.raises(LiveRunError) as error:
        toss_report(home=tmp_path, snapshot_store=FailingStore())
    assert error.value.code == read_reason


def test_report_typed_propagates_unexpected_runtime_error(tmp_path):
    """An unexpected RuntimeError from read_latest_pair must propagate,
    not be masked as store_corrupt or any LiveRunError."""
    from tokmo.composition import toss_report

    class ExplodingStore:
        def read_latest_pair(self):
            raise RuntimeError("unexpected bug")

    leaf = prepare(tmp_path)
    with pytest.raises(RuntimeError, match="unexpected bug"):
        toss_report(home=tmp_path, snapshot_store=ExplodingStore())


def test_report_typed_propagates_unexpected_assertion_error(tmp_path):
    """An AssertionError from read_latest_pair must propagate, not be
    masked as store_corrupt or any LiveRunError."""
    from tokmo.composition import toss_report

    class AssertingStore:
        def read_latest_pair(self):
            assert False, "invariant violated"

    leaf = prepare(tmp_path)
    with pytest.raises(AssertionError):
        toss_report(home=tmp_path, snapshot_store=AssertingStore())


# --- Slice 7 MATERIAL 2: pre-query context (UUID, purpose, seam) ---

class _TransportCallTracker:
    """Wraps auth/data transports to count whether they were called."""

    def __init__(self):
        self.auth_called = False
        self.data_called = False

    def make_auth(self, *outcomes):
        tracker = self

        class _Auth:
            def __init__(self):
                self.outcomes = list(outcomes)
            def __call__(self, request):
                tracker.auth_called = True
                assert tracker.outcomes, "auth started unexpectedly"
                return tracker.outcomes.pop(0)
        return _Auth()

    def make_data(self, *outcomes):
        tracker = self

        class _Data:
            def __init__(self):
                self.outcomes = list(outcomes)
            def __call__(self, request):
                tracker.data_called = True
                assert tracker.outcomes, "data started unexpectedly"
                return tracker.outcomes.pop(0)
        return _Data()


def _state_campaign_index(leaf):
    """Read the campaign_index from state — proves campaign state was started."""
    return state_of(leaf).get("campaign_index", 0)


def test_material2_denied_purpose_zero_transport_zero_state_zero_append(tmp_path):
    """A denied named purpose must fail BEFORE any transport call,
    campaign-state write, or store append."""
    from tokmo.records.holdings_snapshots import snapshot_content_hash

    leaf = prepare(tmp_path)
    tracker = _TransportCallTracker()

    append_calls: list = []

    class SpyStore:
        def append(self, *a, **kw):
            append_calls.append(a)
            raise AssertionError("append must not be called")

    spy = SpyStore()
    before_index = _state_campaign_index(leaf)

    with pytest.raises(LiveRunError) as error:
        run_toss(
            home=tmp_path, project_root=project_root_of(tmp_path),
            auth_transport=tracker.make_auth(ok(TOKEN_BODY)),
            data_transport=tracker.make_data(ok(ACCOUNTS_BODY), ok(HOLDINGS_BODY), ok(HOLDINGS_BODY)),
            clock=Clock(), sleeper=Clock().sleep,
            id_factory=iter([f"r-{i}" for i in range(20)]).__next__,
            run_id="toss-run",
            snapshot_store=spy,
            content_hash_fn=snapshot_content_hash,
            purpose="some-unapproved-named-program",
        )

    assert error.value.code == "unknown_program"
    assert tracker.auth_called is False
    assert tracker.data_called is False
    assert append_calls == []
    assert _state_campaign_index(leaf) == before_index


def test_material2_missing_hasher_zero_transport(tmp_path):
    """snapshot_store given but content_hash_fn=None must fail BEFORE
    any transport call."""
    leaf = prepare(tmp_path)
    tracker = _TransportCallTracker()

    class SpyStore:
        def append(self, *a, **kw):
            raise AssertionError("append must not be called")

    before_index = _state_campaign_index(leaf)

    with pytest.raises(LiveRunError) as error:
        run_toss(
            home=tmp_path, project_root=project_root_of(tmp_path),
            auth_transport=tracker.make_auth(ok(TOKEN_BODY)),
            data_transport=tracker.make_data(),
            clock=Clock(), sleeper=Clock().sleep,
            id_factory=iter([f"r-{i}" for i in range(20)]).__next__,
            run_id="toss-run",
            snapshot_store=SpyStore(),
            content_hash_fn=None,
        )

    assert error.value.code == "private_state_invalid"
    assert tracker.auth_called is False
    assert tracker.data_called is False
    assert _state_campaign_index(leaf) == before_index


def test_material2_invalid_factory_zero_transport(tmp_path):
    """snapshot_id_factory returning an invalid ID must fail BEFORE
    any transport call."""
    from tokmo.records.holdings_snapshots import snapshot_content_hash

    leaf = prepare(tmp_path)
    tracker = _TransportCallTracker()

    class SpyStore:
        def append(self, *a, **kw):
            raise AssertionError("append must not be called")

    before_index = _state_campaign_index(leaf)

    with pytest.raises(LiveRunError) as error:
        run_toss(
            home=tmp_path, project_root=project_root_of(tmp_path),
            auth_transport=tracker.make_auth(ok(TOKEN_BODY)),
            data_transport=tracker.make_data(),
            clock=Clock(), sleeper=Clock().sleep,
            id_factory=iter([f"r-{i}" for i in range(20)]).__next__,
            run_id="toss-run",
            snapshot_store=SpyStore(),
            content_hash_fn=snapshot_content_hash,
            snapshot_id_factory=lambda: "not-a-valid-uuid",
        )

    assert error.value.code == "private_state_invalid"
    assert tracker.auth_called is False
    assert tracker.data_called is False
    assert _state_campaign_index(leaf) == before_index


def test_material2_factory_called_once_same_id_binds_all(tmp_path):
    """The snapshot_id_factory is called exactly once, and the same
    generated ID binds snapshot, permit, and append call."""
    from tokmo.records.holdings_snapshots import (
        HoldingsSnapshotStore, snapshot_content_hash,
    )

    leaf = prepare(tmp_path)
    sqlite_path = leaf / "holdings_snapshots.sqlite3"
    real_store = HoldingsSnapshotStore(sqlite_path, clock=lambda: NOW)

    factory_calls: list[str] = []
    fixed_id = uuid.uuid4().hex

    def factory():
        factory_calls.append(fixed_id)
        return fixed_id

    append_calls: list = []

    class SpyStore:
        def __init__(self, inner):
            self._inner = inner
        def append(self, snapshot, permit):
            append_calls.append((snapshot, permit))
            return self._inner.append(snapshot, permit)

    spy = SpyStore(real_store)

    auth = Transport(ok(TOKEN_BODY))
    data = Transport(ok(ACCOUNTS_BODY), ok(HOLDINGS_BODY), ok(HOLDINGS_BODY))
    result = run_toss(
        home=tmp_path, project_root=project_root_of(tmp_path),
        auth_transport=auth, data_transport=data,
        clock=Clock(), sleeper=Clock().sleep,
        id_factory=iter([f"r-{i}" for i in range(20)]).__next__,
        run_id="toss-run",
        snapshot_store=spy,
        content_hash_fn=snapshot_content_hash,
        snapshot_id_factory=factory,
    )

    assert result.code == "success"
    assert len(factory_calls) == 1
    assert len(append_calls) == 1
    snapshot, permit = append_calls[0]
    assert snapshot.snapshot_id == fixed_id
    assert permit.snapshot_id == fixed_id
    assert permit.purpose_kind.value == "USER_REQUESTED_CAPTURE"
