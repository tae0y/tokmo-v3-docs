"""Time, restart, updates, and receipts for the existing NEWS lane's evening digest."""

import pytest
from test_news_decision_engine import decision, observation

from tokmo.programs.news_decision_engine import RECORDED_TIME, NewsDecisionEngine
from tokmo.programs.news_decision_outbox import queue_proposals, reconcile_deliveries
from tokmo.records.news_decision_store import NewsDecisionStore, timestamp
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import AlertStatus, ProducerRole

ROLE = ProducerRole.HERMES


def at(time, day="2026-09-09"):
    return timestamp(day + "T" + time + ":00+09:00")


def screening(request):
    # Identity in these tests is supplied by the fake screen, not a production keyword gate.
    return dict(
        candidates=[
            dict(
                evidence_ids=[e["id"]],
                event_id=None,
                comparison_event_ids=[],
                event_reason="각 입력은 별개의 사업 소식",
            )
            for e in request["evidence"]
        ],
        ignored=[],
    )


def analyze(request):
    item = request["evidence"][-1]
    return decision(
        [item["id"]],
        title=item["title"],
        facts=item["text"],
        urgency="NORMAL",
        portfolio_note="",
    )


def engine(store, **kw):
    return NewsDecisionEngine(
        store, screening, analyze, clock=RECORDED_TIME, headline_digest_hour=18, **kw
    )


def add(e, key, when):
    return e.tick(
        [
            dict(
                observation(key, when),
                title=key + " 새 사업 소식",
                text=key + " 실제 계약 체결",
            )
        ],
        when,
    )


def test_day_and_overnight_headlines_wait_then_form_one_durable_digest(tmp_path):
    path = tmp_path / "news.db"
    store = NewsDecisionStore(path)
    e = engine(store)
    assert not add(e, "b", at("23:00", "2026-09-08"))["alerts"]
    assert e.tick([], at("08:00"))["calls"] == 0
    assert not add(e, "a", at("10:00"))["alerts"]
    assert all(
        r["held_kind"] == "EVENING_HEADLINES"
        and timestamp(r["held_until"]) == at("18:00")
        for r in store.events()
    )
    assert not e.pending_alerts()
    assert e.tick([], at("17:59"))["calls"] == 0
    result = e.tick([], at("18:00"))
    assert not result["failures"] and len(result["alerts"]) == 1
    digest = result["alerts"][0]
    assert digest["kind"] == "EVENING_DIGEST" and len(digest["constituent_ids"]) == 2
    assert (
        digest["body"].startswith("[저녁 뉴스 모아보기 · 09-09]")
        and "a 새 사업" in digest["body"]
        and "b 새 사업" in digest["body"]
    )
    assert (
        "의미:" not in digest["body"]
        and "https://" not in digest["body"]
        and "보도 확인" not in digest["body"]
    )
    store.close()
    store = NewsDecisionStore(path)
    e = engine(store)
    assert [r["id"] for r in e.pending_alerts()] == [digest["id"]]
    assert not e.tick([], at("18:01"))["alerts"]
    assert not add(e, "c", at("18:02"))["alerts"]
    c = next(r for r in store.events() if r["decision"]["title"].startswith("c "))
    assert timestamp(c["held_until"]) == at("18:00", "2026-09-10")
    store.close()


def test_changed_or_invalidated_waiting_news_uses_latest_judgment(tmp_path):
    store = NewsDecisionStore(tmp_path / "news.db")
    e = engine(store)
    add(e, "a", at("10:00"))
    prior = store.events()[0]
    e.screen = lambda r: dict(
        candidates=[
            dict(
                evidence_ids=["b"],
                event_id=prior["id"],
                comparison_event_ids=[],
                event_reason="같은 사건의 변화",
            )
        ],
        ignored=[],
    )
    e.tick(
        [dict(observation("b", at("12:00")), title="변경된 실제 계약", text="새 조건")],
        at("12:00"),
    )
    assert timestamp(store.events()[0]["held_until"]) == at("18:00")
    result = e.tick([], at("18:00"))
    assert (
        "변경된 실제 계약" in result["alerts"][0]["body"]
        and "a 새 사업" not in result["alerts"][0]["body"]
    )
    store.close()


def test_notify_false_cancels_hold_without_marking_a_send(tmp_path):
    store = NewsDecisionStore(tmp_path / "news.db")
    e = engine(store)
    add(e, "a", at("10:00"))
    e.analyze = lambda r: decision(
        ["a"], notify=False, change="NONE", urgency="NORMAL", portfolio_note=""
    )
    assert not e.tick([], at("18:00"))["alerts"]
    event = store.events()[0]
    assert (
        event["held_until"] is None
        and not event.get("last_sent")
        and not e.pending_alerts()
    )
    assert e.tick([], at("18:01"))["calls"] == 0
    store.close()


@pytest.mark.parametrize(
    "extra",
    [
        dict(portfolio_note="보유 이유를 강화하는 실제 사업 변화"),
        dict(urgency="IMMEDIATE"),
        dict(change="CORRECTION"),
    ],
)
def test_personal_urgent_and_correction_are_not_held_for_digest(tmp_path, extra):
    store = NewsDecisionStore(tmp_path / "news.db")
    e = engine(store)
    e.analyze = lambda r: decision(["a"], urgency="NORMAL", portfolio_note="") | extra
    result = add(e, "a", at("10:00"))
    assert (
        len(result["alerts"]) == 1 and result["alerts"][0]["kind"] != "EVENING_DIGEST"
    )
    assert not store.events()[0].get("held_until")
    store.close()


def test_new_urgent_evidence_breaks_an_existing_evening_hold(tmp_path):
    store = NewsDecisionStore(tmp_path / "news.db")
    e = engine(store)
    add(e, "a", at("10:00"))
    prior = store.events()[0]
    e.screen = lambda r: dict(
        candidates=[
            dict(
                evidence_ids=["b"],
                event_id=prior["id"],
                comparison_event_ids=[],
                event_reason="같은 사건의 변화",
            )
        ],
        ignored=[],
    )
    e.analyze = lambda r: decision(["b"], urgency="IMMEDIATE", portfolio_note="")
    result = add(e, "b", at("11:00"))
    assert result["alerts"][0]["urgency"] == "IMMEDIATE" and not store.events()[0].get(
        "held_until"
    )
    store.close()


def test_downtime_rolls_unprepared_hold_to_next_evening_not_morning(tmp_path):
    store = NewsDecisionStore(tmp_path / "news.db")
    e = engine(store)
    add(e, "a", at("10:00"))
    assert not e.tick([], at("08:00", "2026-09-10"))["alerts"]
    assert timestamp(store.events()[0]["held_until"]) == at("18:00", "2026-09-10")
    assert (
        e.tick([], at("18:00", "2026-09-10"))["alerts"][0]["kind"] == "EVENING_DIGEST"
    )
    store.close()


def test_digest_outbox_receipts_reconcile_every_event_without_repeat(tmp_path):
    store = NewsDecisionStore(tmp_path / "news.db")
    ops = OpsKernelStore(tmp_path / "ops.db", stable_phase_ids=frozenset())
    e = engine(store)
    add(e, "a", at("10:00"))
    add(e, "b", at("11:00"))
    digest = e.tick([], at("18:00"))["alerts"][0]
    result = queue_proposals(e, ops, ROLE, cutoff=at("00:00"), activation_enabled=True)
    assert result["queued"] == 1 and not e.pending_alerts()
    assert (
        queue_proposals(e, ops, ROLE, cutoff=at("00:00"), activation_enabled=True)[
            "queued"
        ]
        == 0
    )
    # Even after a sender outage, an evening packet cannot spill into the next morning.
    assert ops.claim_pending_telegram(ROLE, at("08:00", "2026-09-10")) is None
    for event in store.events():
        assert not event.get("last_sent")
        assert (
            ops.read_outbox(event["delivery_refs"][digest["id"]][0]).status
            is AlertStatus.PENDING
        )
    outbox = ops.claim_pending_telegram(ROLE, at("18:00", "2026-09-10"))
    assert outbox.severity == "NEWS_DIGEST_18"
    ops.mark_sent(ROLE, outbox.alert_id, at("18:00", "2026-09-10"), "123")
    reconcile_deliveries(e, ops)
    for event in store.events():
        assert event["last_sent"]["alert_id"] == digest["id"]
        assert event["last_sent"]["evidence_ids"] == event["decision"]["evidence_ids"]
    assert ops.claim_pending_telegram(ROLE, at("18:01", "2026-09-10")) is None
    store.close()
    ops.close()


def test_waiting_digest_does_not_block_urgent_delivery(tmp_path):
    store = NewsDecisionStore(tmp_path / "news.db")
    ops = OpsKernelStore(tmp_path / "ops.db", stable_phase_ids=frozenset())
    e = engine(store)
    add(e, "a", at("10:00"))
    e.tick([], at("18:00"))
    queue_proposals(e, ops, ROLE, cutoff=at("00:00"), activation_enabled=True)
    e.analyze = lambda r: decision(["u"], urgency="IMMEDIATE", portfolio_note="")
    add(e, "u", at("08:00", "2026-09-10"))
    queue_proposals(e, ops, ROLE, cutoff=at("00:00"), activation_enabled=True)
    outbox = ops.claim_pending_telegram(ROLE, at("08:01", "2026-09-10"))
    assert outbox.severity == "NEWS_IMMEDIATE"
    store.close()
    ops.close()


def test_screen_gets_current_held_identities_but_not_quote_history(tmp_path):
    store = NewsDecisionStore(tmp_path / "news.db")
    seen = []
    e = engine(
        store,
        context_reader=lambda now: dict(
            holdings=[dict(key="KR:123456", name="보유 회사")],
            _market_context=dict(instruments=["large private price history"]),
        ),
    )

    def screen(request):
        seen.append(request)
        return dict(candidates=[], ignored=[dict(evidence_id="a", reason="통상 절차")])

    e.screen = screen
    add(e, "a", at("10:00"))
    assert seen[0]["context"]["holdings"][0]["key"] == "KR:123456"
    assert "_market_context" not in seen[0]["context"]
    store.close()


def test_worker_distinguishes_waiting_reassessment_from_ready_messages(tmp_path):
    from run_news_decision_worker import run_once

    store = NewsDecisionStore(tmp_path / "stores/news_episodes.sqlite3")
    e = engine(store)
    add(e, "a", at("10:00"))
    store.close()
    never = lambda request: (_ for _ in ()).throw(
        AssertionError("unexpected model call")
    )
    result = run_once(
        tmp_path,
        dict(enabled=True, cutoff=at("00:00").isoformat(), headline_digest_hour=18),
        now=at("11:00"),
        screen=never,
        analyze=never,
        clock=RECORDED_TIME,
    )
    assert result["pending_proposals"] == 0
    assert result["headline_digest"] == dict(
        hour=18,
        waiting_reassessment=1,
        next_due="2026-09-09T18:00:00+09:00",
        review_errors=0,
    )


def test_restart_after_one_reassessment_before_batch_commit(tmp_path):
    path = tmp_path / "news.db"
    store = NewsDecisionStore(path)
    e = engine(store)
    add(e, "a", at("10:00"))
    add(e, "b", at("11:00"))
    original = e._analyze
    calls = []

    def interrupt(*args, **kw):
        if calls:
            raise KeyboardInterrupt("simulated process stop")
        calls.append(1)
        return original(*args, **kw)

    e._analyze = interrupt
    with pytest.raises(KeyboardInterrupt):
        e.tick([], at("18:00"))
    store.close()
    store = NewsDecisionStore(path)
    e = engine(store)
    result = e.tick([], at("18:01"))
    assert (
        len(result["alerts"]) == 1 and len(result["alerts"][0]["constituent_ids"]) == 2
    )
    assert len(e.pending_alerts()) == 1
    store.close()


def test_multipart_receipts_do_not_confirm_unsent_parts_or_repeat_unknown(tmp_path):
    store = NewsDecisionStore(tmp_path / "news.db")
    ops = OpsKernelStore(tmp_path / "ops.db", stable_phase_ids=frozenset())
    e = engine(store)
    for n in range(6):
        item = dict(
            observation(str(n), at("10:00")),
            title="사업 계약 " + str(n) + " 긴 제목" * 150,
        )
        e.tick([item], at("10:00"))
    digest = e.tick([], at("18:00"))["alerts"][0]
    queued = queue_proposals(e, ops, ROLE, cutoff=at("00:00"), activation_enabled=True)[
        "queued"
    ]
    assert queued > 1
    first = ops.claim_pending_telegram(ROLE, at("18:01"))
    ops.mark_sent(ROLE, first.alert_id, at("18:01"), "101")
    reconcile_deliveries(e, ops)
    assert all(not row.get("last_sent") for row in store.events())
    second = ops.claim_pending_telegram(ROLE, at("18:02"))
    ops.mark_unknown(ROLE, second.alert_id, at("18:02"), "provider_timeout")
    reconcile_deliveries(e, ops)
    assert (
        queue_proposals(e, ops, ROLE, cutoff=at("00:00"), activation_enabled=True)[
            "queued"
        ]
        == 0
    )
    assert all(not row.get("last_sent") for row in store.events())
    assert ops.read_outbox(second.alert_id).status is AlertStatus.UNKNOWN
    store.close()
    ops.close()
