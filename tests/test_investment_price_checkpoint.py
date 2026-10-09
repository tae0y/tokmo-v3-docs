"""Approved conversation checkpoint; all paths, quotes, models and sends are fake."""

import json
import sqlite3
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tokmo.records.investment_followup import InvestmentFollowupStore

NOW = datetime(2026, 9, 9, 1, tzinfo=UTC)
END = "2026-10-10T00:00:00+09:00"


def approved():
    return dict(
        raw_text="집중을 줄일 뜻은 있지만 추가 매도는 기다린다.",
        interpretation=dict(
            action="HOLD",
            phase="DECIDED",
            account="synthetic-retirement",
            scope="retirement",
            keys=["KR:000001"],
            summary="추가 축소 재검토",
            reason_quotes=["집중을 줄일 뜻은 있지만 추가 매도는 기다린다."],
            condition_quotes=[],
            suggested_checks=[],
            supersedes=None,
        ),
        checkpoint=dict(
            key="KR:000001",
            name="합성 혼합 ETF",
            direction="<=",
            currency="KRW",
            threshold="13610",
            starts_at=NOW.isoformat(),
            expires_at=END,
            question="유지할지 추가로 일부 축소할지 다시 판단해 주세요.",
            recipient="TOKMO_GROUP",
        ),
    )


@pytest.fixture
def store(tmp_path):
    value = InvestmentFollowupStore(tmp_path / "followup.sqlite3")
    yield value
    value.close()


def register(store, data=None, source="synthetic-conversation:message-1"):
    from tokmo.programs.investment_price_checkpoint import register_checkpoint

    return register_checkpoint(
        store,
        data or approved(),
        source_id=source,
        at=NOW,
        references=["synthetic-conversation"],
    )


def test_typed_registration_read_cancel_and_source_idempotency(store):
    c = register(store)
    assert c["source_id"] == "synthetic-conversation:message-1"
    assert c["raw_text"] == approved()["raw_text"]
    assert c["references"] == ["synthetic-conversation"]
    assert register(store) == c
    state = store.checkpoint_state(c, at=NOW)
    assert state["status"] == "NOT_CHECKED" and state["expires_at"] == END
    assert state["last_checked_at"] is None and state["cadence_seconds"] == 300
    changed = approved()
    changed["checkpoint"]["threshold"] = "13000"
    with pytest.raises(ValueError):
        register(store, changed)
    store.cancel_checkpoint(c["id"], at=NOW, reason="사용자 취소")
    assert store.checkpoint_state(c, at=NOW)["status"] == "CANCELLED"
    assert register(store) == c  # Replay never rearms a cancelled source.
    assert not store.checkpoint_open(c["id"], at=NOW)
    assert (
        store.checkpoint_state(c, at=datetime.fromisoformat(END))["status"]
        == "CANCELLED"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("threshold", "NaN"),
        ("threshold", "Infinity"),
        ("threshold", "0"),
        ("threshold", "-1"),
        ("threshold", True),
        ("direction", "="),
        ("currency", "USD"),
        ("recipient", "elsewhere"),
        ("starts_at", "2026-09-09T10:00:00"),
        ("expires_at", NOW.isoformat()),
        ("question", ""),
        ("key", "US:000001"),
    ],
)
def test_invalid_typed_registration_never_creates_choice(store, field, value):
    data = approved()
    data["checkpoint"][field] = value
    with pytest.raises(ValueError):
        register(store, data)
    assert store.choices(at=NOW) == []


def test_cli_register_list_cancel_without_model_or_snapshot(
    tmp_path, monkeypatch, capsys
):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import run_investment_followup as cli

    monkeypatch.setattr(
        cli, "QuantModel", lambda **kw: pytest.fail("no model at intake")
    )
    config = tmp_path / "approved.json"
    config.write_text(json.dumps(approved()))
    args = [
        "--state",
        str(tmp_path / "state.sqlite3"),
        "--data-root",
        str(tmp_path),
        "--at",
        NOW.isoformat(),
    ]
    assert (
        cli.main(
            [
                "register-checkpoint",
                *args,
                "--checkpoint-file",
                str(config),
                "--source-id",
                "synthetic:1",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["checkpoint"]["status"] == "NOT_CHECKED"
    cid = result["choice"]["id"]
    assert cli.main(["list", *args]) == 0
    assert json.loads(capsys.readouterr().out)["checkpoints"][0]["expires_at"] == END
    assert (
        cli.main(["cancel-checkpoint", *args, "--choice-id", cid, "--reason", "취소"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["checkpoint"]["status"] == "CANCELLED"


def market(root, *, at=NOW, regular=True):
    from zoneinfo import ZoneInfo

    from tokmo.records.calendar_cache import CalendarCacheStore
    from tokmo.values.toss_market_calendar import (
        CalendarSession,
        IntegratedHour,
        KrMarketDay,
    )

    local = at.astimezone(ZoneInfo("Asia/Seoul"))
    session = CalendarSession(
        local.replace(hour=9, minute=0), local.replace(hour=15, minute=30)
    )
    (root / "ops/calendar_cache.json").unlink(missing_ok=True)
    CalendarCacheStore(root / "ops/calendar_cache.json", clock=lambda: at).write(
        KrMarketDay(
            local.date(), IntegratedHour(regular_market=session if regular else None)
        ),
        None,
    )


def quote(root, price="13610", *, at=NOW, **changes):
    path = root / "stores/latest_quotes.sqlite3"
    path.parent.mkdir(exist_ok=True)
    row = dict(
        source="toss-current-price-v1",
        symbol="000001",
        last_price=price,
        currency_raw="KRW",
        provider_timestamp=at.isoformat(),
        fetched_at=at.isoformat(),
        stored_at=at.isoformat(),
        schema_version=1,
    )
    row.update(changes)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS latest_quotes (source, symbol, last_price, currency_raw, provider_timestamp, fetched_at, stored_at, schema_version)"
        )
        conn.execute("DELETE FROM latest_quotes")
        conn.execute(
            "INSERT INTO latest_quotes VALUES(?,?,?,?,?,?,?,?)", tuple(row.values())
        )


def hold(prompt):
    return dict(
        change={
            "kind": "CONTEXT",
            "before": "추가 축소는 기다린다.",
            "after": "집중 완화 의도는 남지만 현재 자료로 추가 축소를 확정하기 어렵다.",
            "meaning": "자료를 더 확인한다.",
            "sources": [],
        },
        material_change=True,
        notification_reason="요청한 조건의 도달 여부와 선택 이유를 점검한다.",
        effect="MAINTAIN",
        business="UNCERTAIN",
        market_risk="UNKNOWN",
        notify=False,
        urgency="NORMAL",
        pending_action="KEEP",
        comparison="집중 완화 의도는 남지만 현재 자료로 추가 축소를 확정하기 어렵다.",
        new_facts=[],
        next_checks=["최신 보유와 상품 구성을 확인한다"],
        analysis=None,
    )


def tick(store, root, *, at=NOW, model=hold, **kw):
    from tokmo.programs.investment_followup_worker import run_tick

    return run_tick(
        store,
        news_path=root / "stores/news_episodes.sqlite3",
        data_root=root,
        snapshot=kw.pop("snapshot", None),
        at=at,
        model=model,
        **kw,
    )


@pytest.mark.parametrize(
    "price,hit", [("13615", False), ("13610", True), ("13605", True)]
)
def test_threshold_to_real_review_and_receipt_even_hold(store, tmp_path, price, hit):
    from tokmo.programs.investment_followup import deliver_pending

    c = register(store)
    market(tmp_path)
    quote(tmp_path, price)
    calls = []

    def model(prompt):
        calls.append(json.loads(prompt.split("\nINPUT\n", 1)[1]))
        assert store.watermark("checkpoint:" + c["id"])["observation"]["price"] == price
        return hold(prompt)

    result = tick(store, tmp_path, model=model)
    assert len(calls) == int(hit) and len(result["reviewed"]) == int(hit)
    assert result["checkpoints"][0]["last_checked_at"] == NOW.isoformat()
    assert store.watermark("checkpoint:" + c["id"])["last_check_status"] == (
        "HIT" if hit else "MONITORING"
    )
    if hit:
        packet = calls[0]
        assert packet["trigger"]["question"] == c["checkpoint"]["question"]
        assert packet["choice"]["raw_text"] == c["raw_text"]
        assert packet["quant"] == {}  # No invented holdings or current account facts.
        assert result["checkpoints"][0]["delivery_state"] == "PENDING"
        bodies = []
        assert (
            deliver_pending(
                store, at=NOW, sender=lambda body: bodies.append(body) or "701"
            )[0]["status"]
            == "SENT"
        )
        assert all(
            s in bodies[0]
            for s in ["13610", price, "10:00", c["raw_text"], "집중 완화", "자료"]
        )
        assert store.checkpoint_state(c, at=NOW)["status"] == "AWAITING_USER"
        assert store.checkpoint_state(c, at=NOW)["delivered_at"] == NOW.isoformat()
    later = NOW + timedelta(minutes=5)
    tick(
        store,
        tmp_path,
        at=later,
        model=lambda _: pytest.fail("same unresolved checkpoint repeated"),
    )
    assert store.checkpoint_state(c, at=later)["last_checked_at"] == later.isoformat()


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"currency_raw": "USD"}, "QUOTE_INVALID"),
        ({"symbol": "000002"}, "QUOTE_MISSING"),
        ({"source": "unofficial"}, "QUOTE_MISSING"),
        ({"provider_timestamp": None}, "QUOTE_INVALID"),
        ({"provider_timestamp": "2026-09-09T10:00:00"}, "QUOTE_INVALID"),
        (
            {"provider_timestamp": (NOW - timedelta(minutes=11)).isoformat()},
            "QUOTE_STALE",
        ),
        ({"fetched_at": (NOW - timedelta(minutes=11)).isoformat()}, "QUOTE_STALE"),
        (
            {
                "provider_timestamp": (
                    NOW + timedelta(seconds=5, microseconds=1)
                ).isoformat()
            },
            "QUOTE_FUTURE",
        ),
        ({"fetched_at": (NOW + timedelta(seconds=1)).isoformat()}, "QUOTE_FUTURE"),
        ({"fetched_at": (NOW + timedelta(microseconds=1)).isoformat()}, "QUOTE_FUTURE"),
        (
            {"fetched_at": (NOW - timedelta(seconds=5, microseconds=1)).isoformat()},
            "QUOTE_INVALID",
        ),
        ({"last_price": "NaN"}, "QUOTE_INVALID"),
        ({"last_price": "0"}, "QUOTE_INVALID"),
    ],
)
def test_bad_quote_never_triggers_and_exposes_status(store, tmp_path, changes, status):
    c = register(store)
    market(tmp_path)
    quote(tmp_path, **changes)
    result = tick(store, tmp_path, model=lambda _: pytest.fail("invalid quote model"))
    assert result["checkpoints"][0]["status"] == status
    assert any(
        e["stage"] == "price_checkpoint" and e["error"] == status
        for e in result["errors"]
    )
    assert "observation" not in store.watermark("checkpoint:" + c["id"])


@pytest.mark.parametrize("skew_seconds,worker_delay", [(1, 60), (1, 0), (5, 0)])
def test_provider_clock_skew_reaches_hit_and_review(
    store, tmp_path, skew_seconds, worker_delay
):
    from decimal import Decimal
    from hashlib import sha256

    from tokmo.programs.investment_price_checkpoint import read_quote
    from tokmo.values.quant_decisions import DecisionSnapshot, Instrument

    c = register(store)
    market(tmp_path)
    observed = NOW + timedelta(seconds=skew_seconds)
    at = NOW + timedelta(seconds=worker_delay)
    quote(tmp_path, provider_timestamp=observed.isoformat())
    path = tmp_path / "stores/latest_quotes.sqlite3"
    before = sha256(path.read_bytes()).hexdigest()
    status, raw = read_quote(tmp_path, c["checkpoint"], at=at)
    assert status == "OK"
    snapshot = DecisionSnapshot(
        NOW,
        (Instrument("000001", "KR", "합성", "KRW", "etf", Decimal(99999), NOW),),
        (),
        (),
    )
    result = tick(store, tmp_path, at=at, snapshot=snapshot)
    state = store.watermark("checkpoint:" + c["id"])
    assert state["last_check_status"] == "HIT" and state["status"] == "AWAITING_USER"
    assert len(result["reviewed"]) == 1 and not result["errors"]
    observation = state["observation"]
    assert observation == raw
    assert (
        observation["price"] == "13610"
        and observation["provider_timestamp"] == observed.isoformat()
    )
    assert (
        observation["fetched_at"] == NOW.isoformat()
        and observation["observed_at"] == at.isoformat()
    )
    packet = store.latest_review(c["id"], at=at)["input"]
    assert packet["trigger"]["observation"] == observation
    assert packet["quant"]["KR:000001"]["price_at"] == observed.isoformat()
    assert sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("boundary", ["session", "checkpoint"])
def test_provider_clock_skew_does_not_extend_session_or_checkpoint(
    store, tmp_path, boundary
):
    data = approved()
    market(tmp_path)
    end = (
        NOW.replace(hour=6, minute=30)
        if boundary == "session"
        else NOW + timedelta(seconds=5)
    )
    if boundary == "checkpoint":
        data["checkpoint"]["expires_at"] = end.isoformat()
    c = register(store, data)
    at = end - timedelta(seconds=1)
    quote(tmp_path, at=at, provider_timestamp=end.isoformat())
    result = tick(
        store, tmp_path, at=at, model=lambda _: pytest.fail("skew crossed boundary")
    )
    status = "QUOTE_OUTSIDE_SESSION" if boundary == "session" else "QUOTE_FUTURE"
    assert result["checkpoints"][0]["status"] == status
    assert any(e["error"] == status for e in result["errors"])
    assert "observation" not in store.watermark("checkpoint:" + c["id"])
    result = tick(
        store, tmp_path, at=end, model=lambda _: pytest.fail("closed or expired review")
    )
    assert result["checkpoints"][0]["status"] == (
        "WAITING_SESSION" if boundary == "session" else "EXPIRED"
    )


def test_closed_missing_and_expiry_are_distinct_and_no_create(store, tmp_path):
    c = register(store)
    market(tmp_path, regular=False)
    assert tick(store, tmp_path)["checkpoints"][0]["status"] == "WAITING_SESSION"
    assert not (tmp_path / "stores/latest_quotes.sqlite3").exists()
    market(tmp_path)
    assert tick(store, tmp_path)["checkpoints"][0]["status"] == "QUOTE_MISSING"
    quote(tmp_path, provider_timestamp="2026-09-09T08:59:59+09:00")
    assert tick(store, tmp_path)["checkpoints"][0]["status"] == "QUOTE_OUTSIDE_SESSION"
    end = datetime.fromisoformat(END)
    quote(tmp_path, at=end)
    market(tmp_path, at=end)
    result = tick(store, tmp_path, at=end, model=lambda _: pytest.fail("expired model"))
    assert result["checkpoints"][0]["status"] == "EXPIRED"
    assert not store.checkpoint_open(c["id"], at=end)
    assert store.checkpoint_open(c["id"], at=end - timedelta(microseconds=1))


def test_failed_model_retries_bounded_on_rebound_and_restart(store, tmp_path):
    c = register(store)
    market(tmp_path)
    quote(tmp_path)
    calls = []

    def broken(prompt):
        calls.append(prompt)
        raise TimeoutError("synthetic provider failure")

    result = tick(store, tmp_path, model=broken)
    assert result["checkpoints"][0]["status"] == "REVIEW_FAILED"
    assert result["checkpoints"][0]["failure"]["tries"] == 1
    tick(store, tmp_path, at=NOW + timedelta(seconds=30), model=broken)
    assert len(calls) == 1
    later = NOW + timedelta(minutes=5)
    quote(tmp_path, "14000", at=later)
    reopened = InvestmentFollowupStore(tmp_path / "followup.sqlite3")
    try:

        def recovered(prompt):
            packet = json.loads(prompt.split("\nINPUT\n", 1)[1])
            assert packet["trigger"]["observation"]["price"] == "13610"
            assert packet["trigger"]["observation"]["observed_at"] == NOW.isoformat()
            return hold(prompt)

        result = tick(reopened, tmp_path, at=later, model=recovered)
        assert len(result["reviewed"]) == 1 and not result["errors"]
        assert result["checkpoints"][0]["status"] == "AWAITING_USER"
        tick(
            reopened,
            tmp_path,
            at=later + timedelta(minutes=5),
            model=lambda _: pytest.fail("duplicate"),
        )
        assert (
            reopened.connection.execute(
                "SELECT count(*) FROM investment_reviews"
            ).fetchone()[0]
            == 1
        )
    finally:
        reopened.close()


def test_returned_model_replay_after_crash_does_not_call_again(
    store, tmp_path, monkeypatch
):
    c = register(store)
    market(tmp_path)
    quote(tmp_path)
    real_save = store.save_review

    def crash(_):
        raise SystemExit("synthetic process crash after model journal")

    monkeypatch.setattr(store, "save_review", crash)
    with pytest.raises(SystemExit):
        tick(store, tmp_path)
    monkeypatch.setattr(store, "save_review", real_save)
    later = NOW + timedelta(minutes=5)
    quote(tmp_path, "14000", at=later)
    result = tick(
        store,
        tmp_path,
        at=later,
        model=lambda _: pytest.fail("returned model called twice"),
    )
    assert len(result["reviewed"]) == 1
    # Simulate crash after save_review but before observation-review linkage.
    state = store.watermark("checkpoint:" + c["id"])
    state.pop("review_id")
    state["status"] = "PENDING_REVIEW"
    store.set_watermark("checkpoint:" + c["id"], state)
    tick(
        store,
        tmp_path,
        at=later + timedelta(minutes=5),
        model=lambda _: pytest.fail("saved review called twice"),
    )
    assert (
        store.connection.execute("SELECT count(*) FROM investment_reviews").fetchone()[
            0
        ]
        == 1
    )


@pytest.mark.parametrize("terminal", ["expired", "cancelled", "clock_expired"])
def test_pending_delivery_and_claim_block_terminal_checkpoint(
    store, tmp_path, terminal
):
    from tokmo.programs.investment_followup import deliver_pending

    c = register(store)
    market(tmp_path)
    quote(tmp_path)
    tick(store, tmp_path)
    end = datetime.fromisoformat(END)
    at = end if terminal == "expired" else NOW
    if terminal == "cancelled":
        store.cancel_checkpoint(c["id"], at=NOW, reason="취소")
    instants = iter([NOW, end])
    assert (
        deliver_pending(
            store,
            at=(lambda: next(instants)) if terminal == "clock_expired" else at,
            sender=lambda _: pytest.fail("terminal delivery"),
        )
        == []
    )
    if terminal != "clock_expired":
        assert not store.pending(at=at)
        assert not store.claim(store.latest_review(c["id"], at=at)["id"], at=at)


def test_unknown_receipt_never_resends_and_expired_requeue_rejected(store, tmp_path):
    from tokmo.programs.investment_followup import deliver_pending

    c = register(store)
    market(tmp_path)
    quote(tmp_path)
    tick(store, tmp_path)
    result = deliver_pending(store, at=NOW, sender=lambda _: None)
    assert result[0]["status"] == "UNKNOWN"
    assert store.checkpoint_state(c, at=NOW)["delivered_at"] is None
    assert (
        deliver_pending(
            store,
            at=NOW + timedelta(minutes=5),
            sender=lambda _: pytest.fail("unknown resend"),
        )
        == []
    )
    with pytest.raises(ValueError):
        store.resolve_delivery(
            result[0]["id"],
            action="requeue",
            reason="미전달 확인",
            at=datetime.fromisoformat(END),
            confirmed_not_sent=True,
        )


def test_checkpoint_runs_before_blocked_news_and_pending_ordinary_batch(
    store, tmp_path
):
    c = register(store)
    market(tmp_path)
    quote(tmp_path, "13615")
    ordinary = deepcopy(approved()["interpretation"])
    other = store.record_choice(
        raw_text=approved()["raw_text"],
        interpretation=ordinary,
        source_id="ordinary",
        at=NOW,
    )
    store.set_watermark(
        "pending_batch",
        dict(
            events=[],
            tasks=[dict(choice_id=c["id"], event_ids=[], reason="PERIODIC")],
            done=[],
            source_status="OK",
            created_at=NOW.isoformat(),
        ),
    )
    assert not tick(
        store, tmp_path, model=lambda _: pytest.fail("premature checkpoint")
    )["reviewed"]
    event = dict(id="bad-news", updated_at=NOW.isoformat(), decision={}, evidence=[])
    with sqlite3.connect(tmp_path / "stores/news_episodes.sqlite3") as db:
        db.execute("CREATE TABLE news_decision_events(state TEXT)")
        db.execute("INSERT INTO news_decision_events VALUES(?)", (json.dumps(event),))
    from tokmo.programs.investment_followup_worker import event_version

    store.set_watermark(
        "blocked_match_events",
        {event["id"]: dict(version=event_version(event), attempt="1")},
    )
    quote(tmp_path)
    result = tick(store, tmp_path)
    assert c["id"] in {r["choice_id"] for r in result["reviewed"]}
    assert other["id"] in {r["choice_id"] for r in result["reviewed"]}
    assert any(e["stage"] == "match" for e in result["errors"])


def test_direct_quote_overrides_old_supplement_and_numeric_validator_stays_strict(
    store, tmp_path
):
    from decimal import Decimal

    from tokmo.values.quant_decisions import DecisionSnapshot, Instrument

    register(store)
    market(tmp_path)
    quote(tmp_path)
    snapshot = DecisionSnapshot(
        NOW,
        (Instrument("000001", "KR", "합성", "KRW", "etf", Decimal(99999), NOW),),
        (),
        (),
    )
    calls = []

    def model(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            packet = json.loads(prompt.split("\nINPUT\n", 1)[1])
            assert packet["quant"]["KR:000001"]["price"] == "13610"
            answer = hold(prompt)
            answer["change"]["after"] = "13610원 기준이다."
            return answer
        assert "숫자 없이" in prompt
        return hold(prompt)

    assert len(tick(store, tmp_path, snapshot=snapshot, model=model)["reviewed"]) == 1
    assert len(calls) == 2


def test_cli_tick_still_checks_when_local_analysis_snapshot_is_broken(
    tmp_path, monkeypatch, capsys
):
    import run_investment_followup as cli

    db = tmp_path / "followup.sqlite3"
    store = InvestmentFollowupStore(db)
    register(store)
    store.close()
    market(tmp_path)
    quote(tmp_path)
    monkeypatch.setattr(cli, "QuantModel", lambda **kw: hold)

    def bad_snapshot(*a, **kw):
        raise ValueError("synthetic corrupt supplement")

    monkeypatch.setattr(cli, "load_local_snapshot", bad_snapshot)
    assert (
        cli.main(
            [
                "tick",
                "--state",
                str(db),
                "--data-root",
                str(tmp_path),
                "--at",
                NOW.isoformat(),
            ]
        )
        == 2
    )
    result = json.loads(capsys.readouterr().out)
    assert len(result["reviewed"]) == 1
    assert result["checkpoints"][0]["status"] == "AWAITING_USER"
    assert result["errors"][0]["stage"] == "snapshot"


def test_retry_exhaustion_stays_failed_until_explicit_attempt(store, tmp_path):
    register(store)
    market(tmp_path)
    quote(tmp_path)
    calls = []

    def broken(prompt):
        calls.append(prompt)
        raise TimeoutError()

    for minutes in (0, 5, 10, 15, 20):
        at = NOW + timedelta(minutes=minutes)
        quote(tmp_path, "14000" if minutes else "13610", at=at)
        result = tick(store, tmp_path, at=at, model=broken)
    assert len(calls) == 3 and result["checkpoints"][0]["status"] == "REVIEW_FAILED"
    assert result["checkpoints"][0]["failure"]["retry_at"] is None
    assert len(tick(store, tmp_path, at=at, attempt="operator-retry")["reviewed"]) == 1


def test_expired_failed_observation_never_reanalyses(store, tmp_path):
    register(store)
    market(tmp_path)
    quote(tmp_path)

    def broken(_):
        raise TimeoutError()

    tick(store, tmp_path, model=broken)
    result = tick(
        store,
        tmp_path,
        at=datetime.fromisoformat(END),
        model=lambda _: pytest.fail("expired pending review"),
    )
    assert result["checkpoints"][0]["status"] == "EXPIRED"
    assert result["checkpoints"][0]["observation"]["price"] == "13610"


def test_regular_boundaries_timezone_and_readonly_bytes(store, tmp_path):
    from hashlib import sha256

    from tokmo.programs.investment_price_checkpoint import read_quote

    cp = approved()["checkpoint"]
    cp["starts_at"] = NOW.replace(hour=0).isoformat()
    market(tmp_path, at=NOW.replace(hour=0))
    quote(
        tmp_path, at=NOW.replace(hour=0), provider_timestamp="2026-09-09T09:00:00+09:00"
    )
    path = tmp_path / "stores/latest_quotes.sqlite3"
    before = sha256(path.read_bytes()).hexdigest()
    assert read_quote(tmp_path, cp, at=NOW.replace(hour=0))[0] == "OK"
    assert (
        read_quote(tmp_path, cp, at=NOW.replace(hour=6, minute=30))[0]
        == "WAITING_SESSION"
    )
    assert sha256(path.read_bytes()).hexdigest() == before
    quote(tmp_path, at=NOW - timedelta(minutes=10))
    assert read_quote(tmp_path, cp, at=NOW)[0] == "OK"
    assert (
        read_quote(tmp_path, cp, at=NOW + timedelta(microseconds=1))[0] == "QUOTE_STALE"
    )


def test_model_hold_action_is_delivered_with_current_holdings(store, tmp_path):
    from decimal import Decimal

    from tokmo.programs.investment_followup import deliver_pending
    from tokmo.values.quant_decisions import DecisionSnapshot, Instrument, Position

    c = register(store)
    market(tmp_path)
    quote(tmp_path)
    snapshot = DecisionSnapshot(
        NOW,
        (Instrument("000001", "KR", "합성", "KRW", "etf", Decimal(99999), NOW),),
        (
            Position(
                "synthetic-retirement",
                "retirement",
                "KR:000001",
                Decimal(7),
                Decimal(12000),
                observed_at=NOW,
            ),
        ),
        (),
    )

    def model(prompt):
        answer = hold(prompt)
        answer["analysis"] = dict(
            key="KR:000001",
            action="HOLD",
            option_id=None,
            summary="현재 보유를 유지하며 집중을 살핀다.",
            reasons=[
                dict(text="추가 축소 판단에 필요한 자료를 확인한다.", sources=["price"])
            ],
            counterevidence="집중 위험이 남는다.",
            change_conditions=["상품 구성 확인"],
            thesis_status="uncertain",
        )
        return answer

    tick(store, tmp_path, snapshot=snapshot, model=model)
    r = store.latest_review(c["id"], at=NOW)
    assert r["input"]["quant"]["KR:000001"]["holdings"][0]["quantity"] == "7"
    assert (
        r["assessment"]["analysis"]["action"] == "HOLD" and r["selected_option"] is None
    )
    bodies = []
    deliver_pending(store, at=NOW, sender=lambda b: bodies.append(b) or "702")
    assert "현재 보유를 유지" in bodies[0] and "13610" in bodies[0]


def test_checkpoint_reassessment_can_cite_actual_snapshot_facts(store, tmp_path):
    from decimal import Decimal

    from tokmo.values.quant_decisions import DecisionSnapshot, Evidence, Instrument

    register(store)
    market(tmp_path)
    quote(tmp_path)
    earlier = NOW - timedelta(minutes=1)
    fact = Evidence(
        "product-fact",
        "etf_profile",
        "상품 구성에서 업종 중복을 확인했다.",
        earlier,
        "synthetic official facts",
        "retirement",
    )
    snapshot = DecisionSnapshot(
        earlier,
        (
            Instrument(
                "000001",
                "KR",
                "합성",
                "KRW",
                "etf",
                Decimal(99999),
                earlier,
                evidence=(fact,),
            ),
        ),
        (),
        (),
    )

    def model(prompt):
        packet = json.loads(prompt.split("\nINPUT\n", 1)[1])
        assert packet["quant"]["KR:000001"]["price_age_hours"] == 0
        answer = hold(prompt)
        answer["new_facts"] = [dict(text=fact.text, sources=[fact.id])]
        return answer

    result = tick(store, tmp_path, snapshot=snapshot, model=model)
    assert len(result["reviewed"]) == 1 and not result["errors"]
