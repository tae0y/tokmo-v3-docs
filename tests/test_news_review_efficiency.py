"""Small chronological counterexamples; all providers and state are isolated."""

from copy import deepcopy
from datetime import timedelta

import pytest
from subscription_json_model import SubscriptionInferenceError, _failure
from test_news_decision_engine import decision, observation, screen
from test_news_incremental_review import AT, context

from tokmo.programs.news_decision_engine import RECORDED_TIME, NewsDecisionEngine
from tokmo.records.news_decision_store import NewsDecisionStore, kst


def system(tmp_path, kind="NEW_FACTS", **flags):
    store = NewsDecisionStore(tmp_path / "state.db")
    data = context()
    data["etf_context"] = [
        dict(
            symbol="ETF1",
            market="KR",
            components=dict(rows=[dict(code="1")], source_date="2026-09-08"),
        ),
        dict(
            symbol="ETF2",
            market="KR",
            components=dict(rows=[dict(code="2")], source_date="2026-09-08"),
        ),
    ]
    calls = []

    def analyze(request):
        calls.append(deepcopy(request))
        return decision(
            [r["id"] for r in request["evidence"]],
            notify=False,
            change="NONE",
            urgency="NORMAL",
            review_dependencies=dict(all_portfolio=False, keys=["KR:1"]),
            followup_scope=dict(all_choices=False, choice_ids=["choice1"]),
            recheck=dict(
                after=kst(AT + timedelta(hours=6)),
                reason="새 사실 없으면 기다림",
                kind=kind,
            ),
        )

    analyze.model = "astra"
    analyze.policy_fingerprint = "stable"
    engine = NewsDecisionEngine(
        store,
        screen,
        analyze,
        clock=RECORDED_TIME,
        context_reader=lambda at: deepcopy(data),
        context_mode="incremental",
        reuse_unchanged_reviews=True,
        scoped_review_reuse=True,
        typed_review_enabled=True,
        event_detail_enabled=True,
        **flags,
    )
    return store, engine, calls, data


@pytest.mark.parametrize(
    "kind,expected",
    [("NEW_FACTS", 0), ("DEADLINE", 1), ("PERSISTENCE", 1), ("SAFETY", 1), (None, 1)],
)
def test_real_deadline_and_persistence_survive_cache_expiry(tmp_path, kind, expected):
    store, engine, calls, _ = system(tmp_path, kind)
    try:
        engine.tick([observation("a", AT)], AT)
        before = deepcopy(store.events()[0])
        report = engine.tick([], AT + timedelta(hours=6))
        assert not report["failures"] and report["calls"] == expected
        if not expected:
            after = store.events()[0]
            for field in (
                "updated_at",
                "context_snapshot",
                "decision",
                "last_sent",
                "market_anchor",
            ):
                assert after.get(field) == before.get(field)
            assert after["review_reuse_plan"]["safety_at"] == kst(
                AT + timedelta(hours=24)
            )
    finally:
        store.close()


@pytest.mark.parametrize(
    "change",
    [
        "safety",
        "related_quantity",
        "new_asset",
        "new_evidence",
        "source_edit",
        "health",
        "related_choice",
        "held",
    ],
)
def test_new_facts_wait_never_masks_meaningful_change(tmp_path, change):
    store, engine, calls, data = system(tmp_path)
    try:
        engine.tick([observation("a", AT)], AT)
        at = AT + timedelta(hours=24 if change == "safety" else 6)
        batch = []
        event = store.events()[0]
        if change == "related_quantity":
            data["holdings"][0]["quantity"] = "5"
        elif change == "new_asset":
            data["holdings"].append(dict(key="KR:3", quantity="2"))
        elif change == "new_evidence":
            batch = [observation("war", at)]
        elif change == "source_edit":
            event["evidence"][0]["text"] = "조건부 최대 투자액으로 정정"
            store.save_event(event)
        elif change == "health":
            data["portfolio_status"] = "STALE"
        elif change == "related_choice":
            data["investment_followup"]["choices"][0]["summary"] = "가설 철회"
        elif change == "held":
            event["held_until"] = kst(at)
            store.save_event(event)
        report = engine.tick(batch, at)
        assert not report["failures"] and len(calls) == 2
        if change in {"new_evidence", "new_asset", "held"}:
            assert "detail_scope" not in calls[-1]["context"]
    finally:
        store.close()


def test_unrelated_etf_change_is_not_sent_but_related_source_dates_are(tmp_path):
    store, engine, calls, data = system(tmp_path)
    try:
        engine.tick([observation("a", AT)], AT)
        data["etf_context"][1]["components"]["source_date"] = "2026-09-09"
        report = engine.tick([], AT + timedelta(hours=6))
        assert report["calls"] == 0
        data["etf_context"][0]["components"]["source_date"] = "2026-09-09"
        report = engine.tick([], AT + timedelta(hours=12))
        assert report["calls"] == 1 and not report["failures"]
        sent = calls[-1]["context"]
        assert [r["symbol"] for r in sent["etf_context"]] == ["ETF1"]
        assert len(sent["holdings"]) == 2  # identity discovery survives scoping
        assert sent["etf_context"][0]["components"]["source_date"] == "2026-09-09"
        assert (
            sent["investment_followup"]["choices"][0]["reason_quotes"]
            == data["investment_followup"]["choices"][0]["reason_quotes"]
        )
    finally:
        store.close()


def test_model_can_expand_scope_within_one_existing_correction(tmp_path):
    store, engine, calls, data = system(tmp_path)
    try:
        engine.tick([observation("a", AT)], AT)
        old = engine.analyze

        def wider(request):
            result = old(request)
            result["review_dependencies"] = dict(all_portfolio=True, keys=[])
            return result

        wider.model = old.model
        wider.policy_fingerprint = old.policy_fingerprint
        engine.analyze = wider
        data["holdings"][0]["quantity"] = "5"
        report = engine.tick([], AT + timedelta(hours=6))
        assert not report["failures"] and report["calls"] == 2
        assert calls[-2]["context"]["detail_scope"]["omitted_etfs"] == 1
        assert len(calls[-1]["context"]["etf_context"]) == 2
        assert report["corrections"][0]["reason"] == "portfolio_context_expanded"
    finally:
        store.close()


@pytest.mark.parametrize(
    "kind,retired", [("NEW_FACTS", True), ("DEADLINE", False), (None, False)]
)
def test_retiring_an_attempt_preserves_the_assessment_and_event_watch(
    tmp_path, kind, retired
):
    store, engine, calls, data = system(tmp_path, kind)
    try:
        engine.tick([observation("a", AT)], AT)
        old = store.events()[0]
        old["review_error"] = "older failed attempt"
        store.save_event(old)
        report = engine.tick([], AT + timedelta(hours=6))
        event = store.events()[0]
        assert bool(report.get("retired_review_failures")) == retired
        assert (
            event["updated_at"] == old["updated_at"]
            and event["decision"] == old["decision"]
        )
        assert (
            event["context_snapshot"] == old["context_snapshot"]
            and event["decision"]["open"]
        )
        if retired:
            assert event["review_error"] is None
            assert (
                event["retired_review_failures"][0]["error"] == "older failed attempt"
            )
    finally:
        store.close()


def test_capacity_keeps_screening_outcome_across_restart_and_resumes_once(tmp_path):
    store, engine, calls, data = system(tmp_path, model_wait_enabled=True)
    old = engine.analyze

    def fail(request):
        raise SubscriptionInferenceError("TRANSIENT", exit_code=1)

    fail.model = "astra"
    engine.analyze = fail
    report = engine.tick([observation("a", AT)], AT)
    assert report["calls"] == 2 and not report["failures"]
    assert store.counts() == {"WAITING": 1} and len(store.deferred_candidates()) == 1
    assert len(store.controls("model_wait:")) == 1
    store.close()
    # A new worker with no in-memory state must respect the persisted cooldown.
    store = NewsDecisionStore(tmp_path / "state.db")
    from tokmo.programs.news_model_wait import ModelWait

    engine.store = store
    engine.model_wait = ModelWait(store)
    engine.analyze = old
    try:
        early = engine.tick([], AT + timedelta(minutes=4))
        assert early["calls"] == 0 and store.counts() == {"WAITING": 1}
        resumed = engine.tick([], AT + timedelta(minutes=5))
        assert (
            resumed["calls"] == 1
            and not resumed["failures"]
            and store.counts() == {"DONE": 1}
        )
        assert store.controls("model_wait:") == [] and store.deferred_candidates() == []
        assert len(calls) == 1
    finally:
        store.close()


def test_transient_recovery_is_bounded_for_whole_model_not_per_news(tmp_path):
    from tokmo.programs.news_model_wait import ModelWait

    store, engine, _, _ = system(tmp_path, model_wait_enabled=True)
    invoked = []

    def fail(request):
        invoked.append(request)
        raise SubscriptionInferenceError("TRANSIENT")

    fail.model = "astra"
    engine.analyze = fail
    engine.screen = lambda r: dict(
        candidates=[dict(event_id=None, evidence_ids=[v["id"]]) for v in r["evidence"]],
        ignored=[],
    )
    try:
        first = engine.tick([observation(str(i), AT) for i in range(3)], AT)
        assert (
            len(invoked) == 1
            and store.counts() == {"WAITING": 3}
            and not first["failures"]
        )
        for minute in (5, 20, 100):
            engine.model_wait = ModelWait(store)
            engine.tick([], AT + timedelta(minutes=minute))
        assert len(invoked) == 3
        state = store.controls("model_wait:")[0]
        assert state["manual_required"] is True and state["retry_at"] is None
        assert store.counts() == {"WAITING": 3}
        # A manual-required wait probes once per 6h interval (no operator reset needed); the failed probe re-arms it.
        engine.model_wait = ModelWait(store)
        engine.tick([], AT + timedelta(minutes=1000))
        assert len(invoked) == 4 and store.counts() == {"WAITING": 3}
        state = store.controls("model_wait:")[0]
        assert state["manual_required"] is True and state["failed_at"] == kst(
            AT + timedelta(minutes=1000)
        )
    finally:
        store.close()


def test_codex_usage_limit_text_is_a_quota_outage():
    error = _failure(
        '{"type":"error","message":"You\'ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again later."}\n',
        "",
        1,
        None,
    )
    assert error.category == "QUOTA"


def test_three_unclassified_executor_failures_become_a_whole_model_wait(tmp_path):
    from tokmo.programs.news_model_wait import ModelWait

    store, engine, calls, _ = system(tmp_path, model_wait_enabled=True)
    old = engine.analyze

    def fail(request):
        raise SubscriptionInferenceError("EXECUTOR_UNKNOWN", exit_code=1)

    fail.model = "astra"
    engine.analyze = fail
    engine.screen = lambda r: dict(
        candidates=[dict(event_id=None, evidence_ids=[v["id"]]) for v in r["evidence"]],
        ignored=[],
    )
    try:
        report = engine.tick([observation(str(i), AT) for i in range(3)], AT)
        # Two per-item errors as before; the third failure in a row opens a bounded wait instead of a third error.
        assert (
            store.counts() == {"ERROR": 2, "WAITING": 1}
            and len(store.controls("model_wait:")) == 1
        )
        assert len(report["failures"]) == 2
        engine.analyze = old
        engine.model_wait = ModelWait(store)
        resumed = engine.tick([], AT + timedelta(minutes=5))
        assert resumed["calls"] == 1 and store.counts() == {"ERROR": 2, "DONE": 1}
        assert (
            store.controls("model_wait:") == []
            and store.controls("model_unknown:") == []
        )
    finally:
        store.close()


def test_quota_wait_recovers_by_itself_after_the_probe_interval(tmp_path):
    from tokmo.programs.news_model_wait import ModelWait

    store, engine, calls, _ = system(tmp_path, model_wait_enabled=True)
    old = engine.analyze

    def fail(request):
        raise SubscriptionInferenceError("QUOTA", exit_code=1)

    fail.model = "astra"
    engine.analyze = fail
    try:
        engine.tick([observation("a", AT)], AT)
        assert (
            store.counts() == {"WAITING": 1}
            and store.controls("model_wait:")[0]["manual_required"] is True
        )
        engine.analyze = old
        engine.model_wait = ModelWait(store)
        assert engine.tick([], AT + timedelta(hours=5))[
            "calls"
        ] == 0 and store.counts() == {"WAITING": 1}
        engine.model_wait = ModelWait(store)
        recovered = engine.tick([], AT + timedelta(hours=6))
        assert (
            recovered["calls"] == 1
            and store.counts() == {"DONE": 1}
            and store.controls("model_wait:") == []
        )
    finally:
        store.close()


def test_resume_compares_events_delivered_after_original_classification(tmp_path):
    from tokmo.programs.news_model_wait import ModelWait

    store, engine, calls, _ = system(tmp_path, model_wait_enabled=True)
    old = engine.analyze

    def fail(request):
        raise SubscriptionInferenceError("TRANSIENT")

    fail.model = "astra"
    engine.analyze = fail
    try:
        engine.tick([observation("a", AT)], AT)
        store.save_event(
            dict(
                id="newer-event",
                updated_at=kst(AT + timedelta(minutes=1)),
                decision=decision(["newer"]),
                evidence=[observation("newer", AT + timedelta(minutes=1))],
                last_sent=dict(
                    alert_id="delivered-newer",
                    delivered_at=kst(AT + timedelta(minutes=2)),
                    body="모델 대기 중 다른 자료로 이미 전달한 내용",
                    evidence_ids=["newer"],
                ),
            )
        )
        engine.analyze = old
        engine.model_wait = ModelWait(store)
        result = engine.tick([], AT + timedelta(minutes=5))
        assert result["calls"] == 1 and not result["failures"]
        assert (
            calls[-1]["comparison"]["other_events"][0]["alert_id"] == "delivered-newer"
        )
        assert calls[-1]["model_wait_recovery"]["classified_at"] == kst(AT)
    finally:
        store.close()


@pytest.mark.parametrize("category", ["QUOTA", "AUTH", "EXECUTOR_UNKNOWN", "REQUEST"])
def test_quota_auth_and_unknown_are_not_transient_retry_loops(tmp_path, category):
    from tokmo.programs.news_model_wait import ModelWait

    store, engine, _, _ = system(tmp_path, model_wait_enabled=True)
    invoked = []

    def fail(request):
        invoked.append(request)
        raise SubscriptionInferenceError(category)

    fail.model = "astra"
    engine.analyze = fail
    try:
        engine.tick([observation("a", AT)], AT)
        engine.model_wait = ModelWait(store)
        engine.tick([], AT + timedelta(hours=5))
        assert len(invoked) == 1  # no retry loop inside the probe interval
        engine.model_wait = ModelWait(store)
        engine.tick([], AT + timedelta(days=1))
        # A whole-model wait probes once per 6h; a single unclassified/request error is a per-item error with no wait.
        assert len(invoked) == (2 if category in {"QUOTA", "AUTH"} else 1)
        assert store.counts() == (
            {"WAITING": 1} if category in {"QUOTA", "AUTH"} else {"ERROR": 1}
        )
    finally:
        store.close()


def test_screen_failure_does_not_poison_new_inputs_or_loop(tmp_path):
    store, engine, _, _ = system(tmp_path, model_wait_enabled=True)

    def fail(request):
        raise SubscriptionInferenceError("TRANSIENT")

    fail.model = "glm-5.3"
    engine.screen = fail
    try:
        report = engine.tick([observation("a", AT)], AT)
        assert (
            report["calls"] == 1
            and not report["failures"]
            and store.counts() == {"READY": 1}
        )
        report = engine.tick(
            [observation("b", AT + timedelta(minutes=10))], AT + timedelta(minutes=10)
        )
        assert report["calls"] == 0 and store.counts() == {
            "READY": 2
        }  # no same-pass retry even after 5min
    finally:
        store.close()


def test_cooldown_starts_when_slow_failure_finishes_not_when_call_started(tmp_path):
    store, engine, _, _ = system(tmp_path, model_wait_enabled=True)
    clock = [AT]
    engine.clock = lambda: clock[0]

    def slow_failure(request):
        clock[0] = AT + timedelta(minutes=10)
        raise TimeoutError("model timed out")

    slow_failure.model = "astra"
    engine.analyze = slow_failure
    try:
        engine.tick([observation("a", AT)], AT)
        state = store.controls("model_wait:")[0]
        assert state["failed_at"] == kst(AT + timedelta(minutes=10))
        assert state["retry_at"] == kst(AT + timedelta(minutes=15))
    finally:
        store.close()


def test_capacity_is_transport_failure_not_successful_text():
    assert (
        _failure(
            '{"type":"error","message":"Selected model is at capacity. Please try a different model."}',
            "",
            1,
            None,
        ).category
        == "TRANSIENT"
    )
    assert (
        _failure(
            '{"type":"item.completed","text":"Selected model is at capacity"}',
            "",
            1,
            None,
        ).category
        == "EXECUTOR_UNKNOWN"
    )


def test_success_receipt_prevents_reanalysis_after_transition_crash(tmp_path):
    store, engine, calls, _ = system(tmp_path, model_wait_enabled=True)
    try:
        engine.tick([observation("a", AT)], AT)
        # Simulate success committed but the caller's WAITING->DONE missed.
        store.mark(["a"], "READY")
        store.defer_candidate(
            dict(event_id=None, evidence_ids=["a"], comparison_event_ids=[]), AT
        )
        report = engine.tick([], AT + timedelta(minutes=1))
        assert (
            report["calls"] == 0 and len(calls) == 1 and store.counts() == {"DONE": 1}
        )
    finally:
        store.close()


def test_explicit_retry_after_contract_error_can_wait_again(tmp_path):
    store, engine, _, _ = system(tmp_path, model_wait_enabled=True)
    try:
        store.ingest([observation("a", AT)], AT)
        candidate = dict(event_id=None, evidence_ids=["a"], comparison_event_ids=[])
        store.defer_candidate(candidate, AT)
        store.finish_deferred(
            store.deferred_candidates()[0], "ERROR", "invalid response"
        )
        store.retry_errors(["a"])
        store.defer_candidate(candidate, AT + timedelta(minutes=1))
        assert (
            store.counts() == {"WAITING": 1} and len(store.deferred_candidates()) == 1
        )
    finally:
        store.close()


def test_exact_waiting_duplicate_can_close_but_changed_units_cannot(tmp_path):
    store, _, _, _ = system(tmp_path)
    try:
        a = observation("a", AT)
        b = dict(a, id="b")
        c = dict(a, id="c", text="단위가 달러로 정정")
        store.ingest([a, b, c], AT)
        store.mark(["a"], "DONE")
        store.defer_candidate(dict(event_id=None, evidence_ids=["b"]), AT)
        store.defer_candidate(dict(event_id=None, evidence_ids=["c"]), AT)
        assert store.close_duplicate_deferred(AT) == 1
        assert store.counts() == {"DONE": 1, "CLOSED": 1, "WAITING": 1}
        assert store.deferred_candidates()[0]["evidence_ids"] == ["c"]
        assert store.close_duplicate_deferred(AT) == 0
        assert (
            store.connection.execute(
                "SELECT reason FROM news_decision_input_closures"
            ).fetchone()[0]
            == "DUPLICATE_OF_PROCESSED"
        )
    finally:
        store.close()


def test_model_reset_is_explicit_audited_and_does_not_retry_individual_errors(tmp_path):
    from tokmo.programs.news_model_wait import ModelWait

    store, engine, _, _ = system(tmp_path, model_wait_enabled=True)

    def fail(request):
        raise SubscriptionInferenceError("QUOTA")

    fail.model = "astra"
    engine.analyze = fail
    try:
        engine.tick([observation("a", AT)], AT)
        store.ingest([observation("error", AT)], AT)
        store.mark(["error"], "ERROR", "invalid json")
        result = ModelWait(store).reset("astra", AT, "구독 사용량 회복 확인")
        assert result["model_calls"] == 0 and store.controls("model_wait:") == []
        assert len(store.controls("model_reset:")) == 1
        assert store.counts() == {"ERROR": 1, "WAITING": 1}
    finally:
        store.close()


def test_legacy_policy_bases_do_not_change_by_enabling_new_flags(tmp_path):
    store, engine, _, _ = system(tmp_path)
    try:
        engine.typed_review_enabled = engine.event_detail_enabled = False
        engine.tick([observation("a", AT)], AT)
        event = store.events()[0]
        before = engine._review_basis(event)
        engine.typed_review_enabled = engine.event_detail_enabled = True
        assert engine._review_basis(event) == before
        assert "kind" not in event["review_reuse_plan"]
    finally:
        store.close()


def test_glm_http_classification_keeps_malformed_response_out_of_retries():
    from tokmo.programs.news_model_wait import failure_category
    from tokmo.values.news_classifier import NewsClassifierFailure

    for status, kind, want in [
        (503, "http_error", "TRANSIENT"),
        (401, "http_error", "AUTH"),
        (None, "connection_error", "TRANSIENT"),
        (200, "malformed_response", None),
    ]:
        exc = NewsClassifierFailure("MODEL_UNAVAILABLE")
        exc.failure_diagnostics = dict(http_status=status, error_class=kind)
        assert failure_category(exc) == want


def test_new_schema_is_opt_in_and_does_not_invalidate_old_policy():
    from news_decision_models import response_schema

    old = response_schema("analyze", incremental=True)
    new = response_schema("analyze", incremental=True, typed=True)
    assert "kind" not in old["properties"]["recheck"]["anyOf"][0]["properties"]
    assert new["properties"]["recheck"]["anyOf"][0]["properties"]["kind"]["enum"] == [
        "NEW_FACTS",
        "DEADLINE",
        "PERSISTENCE",
        "SAFETY",
    ]
    assert response_schema("screen") == response_schema("screen", typed=True)
