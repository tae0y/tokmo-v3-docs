import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime

import pytest

from tokmo.programs.market_regime_shadow import derive_analysis_targets
from tokmo.records.market_regime_shadow_state import MarketRegimeShadowStateStore
from tokmo.values.market_regime_core import AdjustmentMode
from tokmo.values.session_gate import Market


def test_targets_are_pure_canonical_deduped_and_uncapped():
    selected, unknown = derive_analysis_targets(
        holdings={Market.KR: ("005930", "005930")},
        watchlist_symbols={Market.KR: frozenset({"000660", "005930"})},
        market_scope=frozenset({Market.KR}),
        pension_excluded=frozenset(),
    )
    assert selected == {Market.KR: ("000660", "005930")}
    assert unknown == frozenset()


def test_shared_target_helper_preserves_exact_historical_quote_semantics():
    selected, unknown = derive_analysis_targets(
        holdings={Market.KR: ("hold", "PENSION")},
        watchlist_symbols={Market.KR: ("PENSION", "Watch", "bad symbol")},
        market_scope=frozenset({Market.KR}),
        pension_excluded=frozenset({"PENSION"}),
    )
    # Pension exclusion was holdings-only; watchlist case and invalid-but-
    # nonempty spelling passed unchanged and did not become incidents.
    assert selected == {Market.KR: ("PENSION", "Watch", "bad symbol", "hold")}
    assert unknown == frozenset()


def test_success_is_transactional_and_reused(tmp_path):
    store = MarketRegimeShadowStateStore(tmp_path / "shadow.sqlite3")
    now = datetime(2026, 8, 10, tzinfo=UTC)
    kwargs = dict(
        identity_digest="a" * 64,
        program_id="market-regime-core-v1",
        spec_digest="b" * 64,
        policy_version="p",
        market=Market.KR,
        symbol="005930",
        completed_session_date=date(2026, 8, 8),
        adjustment_mode=AdjustmentMode.ADJUSTED,
        request_identity="c" * 64,
        owner_invocation_id="owner",
        owner_started_at=now,
    )
    lease = store.acquire_identity_lease("a" * 64)
    assert lease is not None
    with lease:
        assert (
            store.claim(**kwargs, owner_lease_key=lease.key, identity_lease=lease).state
            == "owner"
        )
    store.succeed(
        "a" * 64,
        input_digest="d" * 64,
        pack_digest="e" * 64,
        fetched_at=now,
        evaluated_at=now,
        completed_session_date=date(2026, 8, 8),
        normalized_bar_count=200,
        result_payload={"regime": "RANGE"},
    )
    next_lease = store.acquire_identity_lease("a" * 64)
    with next_lease:
        reused = store.claim(
            **{**kwargs, "owner_invocation_id": "next"},
            owner_lease_key=next_lease.key,
            identity_lease=next_lease,
        )
    assert (reused.state, reused.reused_input_digest) == ("reused", "d" * 64)


def test_live_foreign_owner_is_protected_and_dead_owner_requires_repair(tmp_path):
    store = MarketRegimeShadowStateStore(tmp_path / "shadow.sqlite3")
    now = datetime(2026, 8, 10, tzinfo=UTC)
    kwargs = dict(
        identity_digest="f" * 64,
        program_id="market-regime-core-v1",
        spec_digest="b" * 64,
        policy_version="p",
        market=Market.KR,
        symbol="005930",
        completed_session_date=date(2026, 8, 8),
        adjustment_mode=AdjustmentMode.ADJUSTED,
        request_identity="c" * 64,
        owner_started_at=now,
    )
    owner_lease = store.acquire_identity_lease("f" * 64)
    assert owner_lease is not None
    assert (
        store.claim(
            **kwargs,
            owner_invocation_id="foreign",
            owner_lease_key=owner_lease.key,
            identity_lease=owner_lease,
        ).state
        == "owner"
    )
    assert store.acquire_identity_lease("f" * 64) is None
    owner_lease.close()
    recovery_lease = store.acquire_identity_lease("f" * 64)
    with recovery_lease:
        recovered = store.claim(
            **kwargs,
            owner_invocation_id="new",
            owner_lease_key=recovery_lease.key,
            identity_lease=recovery_lease,
        )
    assert recovered.state == "recovery_required"
    retry_lease = store.acquire_identity_lease("f" * 64)
    with retry_lease:
        assert (
            store.claim(
                **kwargs,
                owner_invocation_id="later",
                owner_lease_key=retry_lease.key,
                identity_lease=retry_lease,
            ).state
            == "failed"
        )


def test_fail_is_atomic_and_persists_exact_terminal_failure_on_reopen(tmp_path):
    path = tmp_path / "shadow.sqlite3"
    store = MarketRegimeShadowStateStore(path)
    now = datetime(2026, 8, 10, tzinfo=UTC)
    identity = "1" * 64
    kwargs = dict(
        identity_digest=identity,
        program_id="market-regime-core-v1",
        spec_digest="2" * 64,
        policy_version="p",
        market=Market.KR,
        symbol="005930",
        completed_session_date=date(2026, 8, 8),
        adjustment_mode=AdjustmentMode.ADJUSTED,
        request_identity="3" * 64,
        owner_invocation_id="owner",
        owner_started_at=now,
    )
    lease = store.acquire_identity_lease(identity)
    assert lease is not None
    with lease:
        assert (
            store.claim(**kwargs, owner_lease_key=lease.key, identity_lease=lease).state
            == "owner"
        )

    store.fail(identity, failure_code="X", finished_at=now)

    reopened = MarketRegimeShadowStateStore(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT state,failure_code FROM market_regime_shadow_claim "
            "WHERE identity_digest=?",
            (identity,),
        ).fetchone() == ("FAILED", "X")
    assert reopened.probe(identity).state == "failed"
    next_lease = reopened.acquire_identity_lease(identity)
    assert next_lease is not None
    with next_lease:
        assert (
            reopened.claim(
                **{**kwargs, "owner_invocation_id": "next"},
                owner_lease_key=next_lease.key,
                identity_lease=next_lease,
            ).state
            == "failed"
        )
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT state,failure_code FROM market_regime_shadow_claim "
            "WHERE identity_digest=?",
            (identity,),
        ).fetchone() == ("FAILED", "X")

    with pytest.raises(ValueError, match="claim not owned"):
        reopened.fail(identity, failure_code="RECOVERY_REQUIRED", finished_at=now)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT state,failure_code FROM market_regime_shadow_claim "
            "WHERE identity_digest=?",
            (identity,),
        ).fetchone() == ("FAILED", "X")


def test_fail_rolls_back_and_closes_when_commit_raises(monkeypatch, tmp_path):
    path = tmp_path / "shadow.sqlite3"
    store = MarketRegimeShadowStateStore(path)
    now = datetime(2026, 8, 10, tzinfo=UTC)
    identity = "4" * 64
    lease = store.acquire_identity_lease(identity)
    assert lease is not None
    with lease:
        assert (
            store.claim(
                identity_digest=identity,
                program_id="market-regime-core-v1",
                spec_digest="5" * 64,
                policy_version="p",
                market=Market.KR,
                symbol="005930",
                completed_session_date=date(2026, 8, 8),
                adjustment_mode=AdjustmentMode.ADJUSTED,
                request_identity="6" * 64,
                owner_invocation_id="owner",
                owner_started_at=now,
                owner_lease_key=lease.key,
                identity_lease=lease,
            ).state
            == "owner"
        )

    real_connect = sqlite3.connect

    class CommitFailureConnection:
        def __init__(self):
            self.connection = real_connect(path, isolation_level=None)
            self.closed = False

        @property
        def in_transaction(self):
            return self.connection.in_transaction

        def execute(self, statement, parameters=()):
            if statement == "COMMIT":
                raise sqlite3.OperationalError("injected commit failure")
            return self.connection.execute(statement, parameters)

        def close(self):
            self.connection.close()
            self.closed = True

    injected = CommitFailureConnection()
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: injected)
    with pytest.raises(sqlite3.OperationalError, match="injected commit failure"):
        store.fail(identity, failure_code="X", finished_at=now)
    assert injected.closed
    with closing(real_connect(path)) as connection:
        assert connection.execute(
            "SELECT state,failure_code,finished_at "
            "FROM market_regime_shadow_claim WHERE identity_digest=?",
            (identity,),
        ).fetchone() == ("CLAIMED", None, None)
