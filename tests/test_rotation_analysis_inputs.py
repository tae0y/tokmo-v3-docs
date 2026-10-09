import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest
from test_rotation_analysis import NOW, policy

from tokmo.records.order_command_journal import OrderCommandJournal
from tokmo.records.rotation_analysis_inputs import read_rotation_context


def saved_state(tmp_path, held=True, config=None):
    config = config or policy()
    journal = OrderCommandJournal(tmp_path / "orders.sqlite3")
    journal.initialize_experiment(
        config.experiment_id, mode="live", envelope=config.envelope()
    )
    if held:
        journal.apply_experiment_fill(
            config.experiment_id,
            mode="live",
            fill_id="fill-1",
            symbol=config.symbol,
            side="BUY",
            quantity=D(1),
            amount=D(372500),
            costs=D(0),
            observed_at=NOW - timedelta(days=1),
        )
    (tmp_path / "settlements").mkdir()
    (tmp_path / "settlements" / f"{config.market}-20260916.json").write_text(
        json.dumps(
            dict(
                market=config.market,
                day="20260916",
                at="2026-09-16T08:40:00+09:00",
                results=[dict(experiment_id="rotation-test", status="OK")],
            )
        )
    )
    return journal


def test_settlement_market_follows_policy(tmp_path):
    config = replace(policy(), market="US", symbol="SAMPLE")
    saved_state(tmp_path, config=config)
    state = read_rotation_context(
        tmp_path, config, now=NOW, regular_open_at=NOW.replace(hour=0, minute=0)
    )
    assert state["quantity"] == "1" and state["cost_basis"] == "372500"


def read(tmp_path):
    return read_rotation_context(
        tmp_path,
        policy(),
        now=NOW,
        regular_open_at=NOW.replace(hour=0, minute=0),
        last_closed_at=NOW.replace(hour=11, minute=0) - timedelta(days=1),
    )


def test_read_only_adapter_reuses_lot_basis_without_journal_writes(tmp_path):
    saved_state(tmp_path)
    path = tmp_path / "orders.sqlite3"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    state = read(tmp_path)
    assert state["quantity"] == "1" and state["cost_basis"] == "372500"
    assert state["cash"] == "77500" and state["open_commands"] == []
    assert before == hashlib.sha256(path.read_bytes()).hexdigest()


def test_no_journal_does_not_create_an_empty_book(tmp_path):
    with pytest.raises((ValueError, OSError, sqlite3.Error)):
        read(tmp_path)
    assert not (tmp_path / "orders.sqlite3").exists()


def test_next_cycle_uses_its_fill_instead_of_previous_cycle(tmp_path):
    journal = saved_state(tmp_path)
    journal.apply_experiment_fill(
        policy().experiment_id,
        mode="live",
        fill_id="sell-1",
        symbol="005380",
        side="SELL",
        quantity=D(1),
        amount=D(384000),
        costs=D(800),
        observed_at=NOW - timedelta(hours=20),
    )
    journal.apply_experiment_fill(
        policy().experiment_id,
        mode="live",
        fill_id="buy-2",
        symbol="005380",
        side="BUY",
        quantity=D(1),
        amount=D(360000),
        costs=D(0),
        observed_at=NOW - timedelta(hours=19),
    )
    assert read(tmp_path)["cost_basis"] == "360000"


def test_failed_latest_settlement_does_not_reuse_an_older_success(tmp_path):
    saved_state(tmp_path)
    path = tmp_path / "settlements/KR-20260916.json"
    data = json.loads(path.read_text())
    data["results"][0]["status"] = "HALT"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="settlement_failed"):
        read(tmp_path)


def test_fill_after_settlement_is_unresolved(tmp_path):
    journal = saved_state(tmp_path, False)
    journal.apply_experiment_fill(
        policy().experiment_id,
        mode="live",
        fill_id="later",
        symbol="005380",
        side="BUY",
        quantity=D(1),
        amount=D(372500),
        costs=D(0),
        observed_at=NOW - timedelta(minutes=2),
    )
    with pytest.raises(ValueError, match="inventory_changed_after_settlement"):
        read(tmp_path)


@pytest.mark.parametrize("market_code", ["KR", "US"])
def test_calendar_is_read_without_opening_a_writable_store(tmp_path, market_code):
    from tokmo.records.calendar_lookahead import (
        CalendarLookaheadStoreV1,
        LookaheadDayV1,
        LookaheadSessionV1,
        Market,
    )
    from tokmo.records.rotation_analysis_inputs import saved_market_day

    path = tmp_path / "ops/calendar_lookahead.sqlite3"
    market = Market(market_code)
    day = LookaheadDayV1(
        market,
        "2026-09-16",
        "2026-09-16",
        (
            LookaheadSessionV1(
                "regularMarket",
                NOW.replace(hour=0, minute=0),
                NOW.replace(hour=6, minute=30),
            ),
        ),
        NOW - timedelta(hours=1),
        f"calendar:{market_code}:2026-09-16:0",
    )
    CalendarLookaheadStoreV1(path, clock=lambda: NOW).replace_market_days(
        market, (day,)
    )
    before = path.read_bytes()
    result = saved_market_day(tmp_path, now=NOW, market=market_code)
    assert result["trade_date"] == "2026-09-16" and result["last_closed_at"] is None
    assert path.read_bytes() == before


def closed_calendar(tmp_path, now=NOW):
    """Existing official calendar stores only; no account/credential fixture."""
    from tokmo.records.calendar_cache import CalendarCacheStore
    from tokmo.records.calendar_lookahead import (
        CalendarLookaheadStoreV1,
        LookaheadDayV1,
        LookaheadSessionV1,
        Market,
    )
    from tokmo.records.regular_session_history import _SCHEMA
    from tokmo.values.toss_market_calendar import KrMarketDay

    ops = tmp_path / "ops"
    ops.mkdir(parents=True)
    CalendarCacheStore(ops / "calendar_cache.json", clock=lambda: now).write(
        KrMarketDay(now.date(), None), None
    )
    with sqlite3.connect(ops / "regular_session_history.sqlite3") as db:
        db.executescript(_SCHEMA)
        db.execute(
            "INSERT INTO regular_session_history VALUES(?,?,?,?,?)",
            (
                "KR",
                now.date().isoformat(),
                "no_regular",
                f"calendar:KR:{now.date()}:0",
                now.isoformat(),
            ),
        )
    next_day = now + timedelta(days=1)
    day = LookaheadDayV1(
        Market.KR,
        next_day.date().isoformat(),
        next_day.date().isoformat(),
        (
            LookaheadSessionV1(
                "regularMarket",
                next_day.replace(hour=0, minute=0),
                next_day.replace(hour=6, minute=30),
            ),
        ),
        now,
        f"calendar:KR:{now.date()}:0",
    )
    CalendarLookaheadStoreV1(
        ops / "calendar_lookahead.sqlite3", clock=lambda: now
    ).replace_market_days(Market.KR, (day,))
    return ops
