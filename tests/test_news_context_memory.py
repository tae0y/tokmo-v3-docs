"""Chronological context, receipts, original-source recovery and small input views."""

import json
from copy import deepcopy
from datetime import timedelta

from test_news_decision_engine import decision, followup, observation, screen

from tokmo.programs.news_alert_delivery import run_alert_delivery
from tokmo.programs.news_context_memory import (
    compact_portfolio,
    numeric_progress,
    update_memory,
)
from tokmo.programs.news_decision_engine import RECORDED_TIME, NewsDecisionEngine
from tokmo.programs.news_decision_outbox import queue_proposals, reconcile_deliveries
from tokmo.records.news_decision_store import NewsDecisionStore, kst, timestamp
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import ProducerRole

AT = timestamp("2026-09-08T10:00:00+09:00")


def memory(ids, phase="진행 중", summary="공급 비용 상승이 이어짐"):
    return dict(
        topic="산업 공급 여건",
        phase=phase,
        summary=summary,
        watch="공급 정상화 여부",
        source_ids=ids,
    )


def deliver(engine, ops, at):
    queued = queue_proposals(
        engine, ops, ProducerRole.HERMES, cutoff=AT, activation_enabled=True, test=True
    )
    for n in range(queued["queued"]):
        run_alert_delivery(
            ops_kernel=ops,
            role=ProducerRole.HERMES,
            now=at,
            telegram_send=lambda *a, **kw: "987654",
            keyboard_resolver=lambda _: None,
            delivery_binder=lambda *a, **kw: None,
        )
    reconcile_deliveries(engine, ops)


def test_unsent_updates_preserve_delivered_anchor_across_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = NewsDecisionStore(path)
    ops = OpsKernelStore(tmp_path / "ops.sqlite3", stable_phase_ids=frozenset())
    seen = []

    def analyze(request):
        seen.append(deepcopy(request))
        key = request["evidence"][-1]["id"]
        out = decision([key]) if key == "a" else followup([key])
        if key in ("b", "c"):
            out.update(notify=False, change="NONE", material_change=None)
        out["context_update"] = memory([key], phase=key)
        return out

    try:
        engine = NewsDecisionEngine(store, screen, analyze, clock=RECORDED_TIME)
        for n, key in enumerate("abc"):
            row = observation(key, AT + timedelta(minutes=n))
            row["measurements"] = [
                dict(
                    metric="비용지수",
                    unit="point",
                    value=100 + n * 10,
                    measured_at=kst(AT + timedelta(minutes=n)),
                )
            ]
            result = engine.tick([row], AT + timedelta(minutes=n))
            assert not result["failures"]
            deliver(engine, ops, AT + timedelta(minutes=n))
        event = store.events()[0]
        assert event["context_memory"]["phase"] == "c"
        assert event["last_sent"]["context_memory"]["phase"] == "a"
        store.close()
        store = NewsDecisionStore(path)
        engine = NewsDecisionEngine(store, screen, analyze, clock=RECORDED_TIME)
        row = observation("d", AT + timedelta(minutes=3))
        row["measurements"] = [
            dict(
                metric="비용지수",
                unit="point",
                value=140,
                measured_at=kst(AT + timedelta(minutes=3)),
            )
        ]
        result = engine.tick([row], AT + timedelta(minutes=3))
        assert not result["failures"]
        request = seen[-1]
        assert request["events"][0]["last_sent"]["context_memory"]["phase"] == "a"
        assert request["events"][0]["context_memory"]["phase"] == "c"
        progress = request["continuity"]["numeric_progress"][0]
        assert (
            progress["baseline_source_id"] == "a"
            and progress["change_percent"] == "40.0"
        )
        deliver(engine, ops, AT + timedelta(minutes=3))
        assert store.events()[0]["last_sent"]["context_memory"]["phase"] == "d"
    finally:
        store.close()
        ops.close()


def test_bad_auxiliary_memory_cannot_block_first_warning(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.sqlite3")
    try:
        model = lambda _: decision(context_update=memory(["absent"]))
        result = NewsDecisionEngine(store, screen, model, clock=RECORDED_TIME).tick(
            [observation(at=AT)], AT
        )
        assert len(result["alerts"]) == 1 and not result["failures"]
        assert result["memory_issues"][0]["reason"] == "INVALID_SOURCE_IDS"
        assert "context_memory" not in result["alerts"][0]
    finally:
        store.close()


def test_screen_can_keep_observation_without_analysis_or_moving_anchor(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.sqlite3")
    calls = []

    def select(request):
        if not request["events"]:
            return screen(request)
        return {
            "candidates": [],
            "ignored": [
                {
                    "evidence_id": request["evidence"][0]["id"],
                    "event_id": request["events"][0]["event_id"],
                    "reason": "기존 범위의 작은 변동",
                }
            ],
        }

    def analyze(request):
        calls.append(request)
        return decision(context_update=memory(["a"]))

    try:
        engine = NewsDecisionEngine(store, select, analyze, clock=RECORDED_TIME)
        engine.tick([observation("a", AT)], AT)
        before = deepcopy(store.events()[0])
        result = engine.tick(
            [observation("b", AT + timedelta(minutes=1))], AT + timedelta(minutes=1)
        )
        event = store.events()[0]
        assert (
            not result["failures"]
            and result["context_only_inputs"] == ["b"]
            and len(calls) == 1
        )
        assert (
            event["context_memory"] == before["context_memory"]
            and event["last_alert"] == before["last_alert"]
        )
        assert {e["id"] for e in event["evidence"]} == {"a", "b"}
        assert event["context_observations"]["observed_through"] == kst(
            AT + timedelta(minutes=1)
        )
    finally:
        store.close()


def test_missing_update_does_not_make_old_memory_fresh():
    old = {"context_memory": dict(memory(["a"]), reviewed_at=kst(AT))}
    saved, issue = update_memory(
        None, old, [observation(at=AT)], at=AT + timedelta(days=10)
    )
    assert issue == "NOT_PROVIDED" and saved["reviewed_at"] == kst(AT)
    saved["summary"] = "other"
    assert old["context_memory"]["summary"] != "other"


def test_original_source_can_be_restored_once_without_mutating_archive(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.sqlite3")
    seen = []
    original = observation(at=AT)
    original["text"] = "초기 설명 " * 400 + "원문 끝의 반대 조건"

    def analyze(request):
        seen.append(deepcopy(request))
        if len(seen) == 1:
            return decision(context_update=memory(["a"]))
        out = followup(["b"], change="CORRECTION", context_update=memory(["a", "b"]))
        if len(seen) == 2:
            out["source_requests"] = ["a"]
        return out

    try:
        engine = NewsDecisionEngine(store, screen, analyze, clock=RECORDED_TIME)
        engine.tick([original], AT)
        result = engine.tick(
            [observation("b", AT + timedelta(minutes=1))], AT + timedelta(minutes=1)
        )
        assert not result["failures"] and len(seen) == 3
        assert "원문 끝" not in seen[1]["evidence"][0]["text"]
        assert "원문 끝" in seen[2]["evidence"][0]["text"]
        assert store.events()[0]["evidence"][0]["text"] == original["text"]
    finally:
        store.close()


def test_related_unsent_event_is_context_without_merging_identity(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.sqlite3")
    seen = []

    def select(request):
        key = request["evidence"][0]["id"]
        return {
            "candidates": [
                {
                    "evidence_ids": [key],
                    "event_id": None,
                    "event_reason": "미발송 별개 사건과 비교",
                    "comparison_event_ids": [request["events"][0]["event_id"]]
                    if request["events"]
                    else [],
                }
            ],
            "ignored": [],
        }

    def analyze(request):
        seen.append(request)
        return decision(
            [request["evidence"][-1]["id"]],
            notify=False,
            context_update=memory([request["evidence"][-1]["id"]]),
        )

    try:
        engine = NewsDecisionEngine(store, select, analyze, clock=RECORDED_TIME)
        engine.tick([observation("a", AT)], AT)
        result = engine.tick(
            [observation("b", AT + timedelta(minutes=1))], AT + timedelta(minutes=1)
        )
        assert not result["failures"] and len(store.events()) == 2
        related = seen[-1]["continuity"]["related_events"]
        assert len(related) == 1 and related[0]["last_sent"] is None
        assert related[0]["memory"]["summary"] == "공급 비용 상승이 이어짐"
    finally:
        store.close()


def test_compact_identity_keeps_all_holdings_and_etf_kind_without_price_inputs():
    original = {
        "cash_status": "CASH_AVAILABLE",
        "holdings": [
            {
                "key": "KR:a",
                "name": "HD현대중공업",
                "kind": "stock",
                "scope": "personal",
                "account": "domestic",
                "quantity": "2",
                "avg_price": "123",
                "observed_at": kst(AT),
            }
        ],
        "instruments": [
            {
                "key": "KR:a",
                "name": "HD현대중공업",
                "symbol": "a",
                "kind": "stock",
                "identity_source": "capture",
                "identity_at": kst(AT),
            },
            {
                "key": "KR:b",
                "name": "채권혼합ETF",
                "symbol": "b",
                "kind": "etf",
                "identity_source": "capture",
                "identity_at": kst(AT),
            },
        ],
    }
    result = compact_portfolio(original)
    assert result["holdings"][0]["name"] == "HD현대중공업"
    assert result["instruments"][1]["kind"] == "etf"
    assert result["cash_status"] == "CASH_AVAILABLE"
    assert (
        "quantity" not in result["holdings"][0]
        and original["holdings"][0]["quantity"] == "2"
    )
    assert len(json.dumps(result)) < len(json.dumps(original))


def test_numeric_series_rejects_future_conflict_and_keeps_units_separate():
    a = observation("a", AT)
    b = observation("b", AT + timedelta(hours=1))
    a["measurements"] = [
        dict(metric="지수", unit="point", value="100", measured_at=kst(AT))
    ]
    b["measurements"] = [
        dict(
            metric="지수",
            unit="point",
            value="120",
            measured_at=kst(AT + timedelta(hours=1)),
        )
    ]
    assert numeric_progress([a, b], baseline_ids={"a"}, at=AT) == []
    assert (
        numeric_progress([a, b], baseline_ids={"a"}, at=AT + timedelta(hours=1))[0][
            "change_percent"
        ]
        == "20.0"
    )
    b["measurements"][0]["unit"] = "USD"
    assert (
        numeric_progress([a, b], baseline_ids={"a"}, at=AT + timedelta(hours=1)) == []
    )
    b["measurements"][0]["unit"] = "point"
    b["measurements"][0]["measured_at"] = kst(AT)
    assert (
        numeric_progress([a, b], baseline_ids={"a"}, at=AT + timedelta(hours=1)) == []
    )


def test_delayed_receipts_keep_each_proposals_own_memory(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.sqlite3")
    ops = OpsKernelStore(tmp_path / "ops.sqlite3", stable_phase_ids=frozenset())

    def analyze(request):
        key = request["evidence"][-1]["id"]
        return (decision([key]) if key == "a" else followup([key])) | {
            "context_update": memory([key], phase=key)
        }

    try:
        engine = NewsDecisionEngine(store, screen, analyze, clock=RECORDED_TIME)
        engine.tick([observation("a", AT)], AT)
        queue_proposals(
            engine,
            ops,
            ProducerRole.HERMES,
            cutoff=AT,
            activation_enabled=True,
            test=True,
        )
        engine.tick(
            [observation("b", AT + timedelta(minutes=1))], AT + timedelta(minutes=1)
        )
        queue_proposals(
            engine,
            ops,
            ProducerRole.HERMES,
            cutoff=AT,
            activation_enabled=True,
            test=True,
        )
        for _ in range(2):
            run_alert_delivery(
                ops_kernel=ops,
                role=ProducerRole.HERMES,
                now=AT + timedelta(minutes=2),
                telegram_send=lambda *a, **kw: "987654",
                keyboard_resolver=lambda _: None,
                delivery_binder=lambda *a, **kw: None,
            )
        reconcile_deliveries(engine, ops)
        event = store.events()[0]
        assert {
            (row["evidence_ids"][0], row["context_memory"]["phase"])
            for row in event["delivered_history"]
        } == {("a", "a"), ("b", "b")}
        assert event["last_sent"]["context_memory"]["phase"] == "b"
    finally:
        store.close()
        ops.close()


def test_choice_periodic_review_keeps_dated_news_context_after_restart(tmp_path):
    from test_investment_followup import choice, response

    from tokmo.programs.investment_followup_worker import run_tick
    from tokmo.records.investment_followup import InvestmentFollowupStore

    news = NewsDecisionStore(tmp_path / "news.sqlite3")
    news.save_event(
        {
            "id": "event-a",
            "updated_at": kst(AT),
            "decision": decision(["a"]),
            "evidence": [observation("a", AT)],
            "context_memory": dict(
                memory(["a"]), reviewed_at=kst(AT), source_observed_through=kst(AT)
            ),
        }
    )
    news.close()
    path = tmp_path / "choices.sqlite3"
    store = InvestmentFollowupStore(path)
    seen = []
    c = choice(store, at=AT)

    def match(prompt):
        return {
            "matches": [
                {
                    "choice_id": c["id"],
                    "event_ids": ["event-a"],
                    "reason": "시장 위험",
                    "assessment": "REVIEW",
                }
            ],
            "unmatched_reason": "없음",
        }

    def analyze(prompt):
        seen.append(json.loads(prompt.split("\nINPUT\n")[-1]))
        return response(
            effect="MAINTAIN", notify=False, pending_action="KEEP", new_facts=[]
        )

    try:
        args = dict(
            news_path=tmp_path / "news.sqlite3",
            snapshot=None,
            model=analyze,
            screen_model=match,
        )
        assert not run_tick(store, at=AT, **args)["errors"]
        store.close()
        store = InvestmentFollowupStore(path)
        assert not run_tick(store, at=AT + timedelta(hours=6), **args)["errors"]
        retained = seen[-1]["trigger"]["news_context"][0]["memory"]
        assert retained["source_observed_through"] == kst(AT)
        assert retained["summary"] == "공급 비용 상승이 이어짐"
    finally:
        store.close()


def test_morning_digest_preserves_each_events_sources_and_memory(tmp_path):
    from tokmo.programs.news_decision_engine import digest_alerts

    store = NewsDecisionStore(tmp_path / "state.sqlite3")
    ops = OpsKernelStore(tmp_path / "ops.sqlite3", stable_phase_ids=frozenset())
    alerts = [
        {
            "id": "alert-" + key,
            "event_id": "event-" + key,
            "evidence_ids": [key],
            "created_at": kst(AT),
            "body": "요약 " + key,
            "context_memory": memory([key], phase=key),
        }
        for key in "ab"
    ]
    digest = digest_alerts(alerts)
    try:
        for key in "ab":
            store.save_event(
                {
                    "id": "event-" + key,
                    "evidence": [observation(key, AT)],
                    "decision": decision([key]),
                    "updated_at": kst(AT),
                    "last_alert": digest,
                    "pending_alerts": [digest],
                }
            )
        engine = NewsDecisionEngine(store, None, None, clock=RECORDED_TIME)
        deliver(engine, ops, AT)
        assert {
            (
                e["last_sent"]["evidence_ids"][0],
                e["last_sent"]["context_memory"]["phase"],
            )
            for e in store.events()
        } == {("a", "a"), ("b", "b")}
    finally:
        store.close()
        ops.close()
