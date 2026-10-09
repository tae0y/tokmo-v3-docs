"""Actual extra evidence and relevant choices must drive repeat model calls."""

from copy import deepcopy
from datetime import timedelta

import pytest
from test_news_context_efficiency import AT, event
from test_news_decision_engine import decision, observation, screen

from tokmo.programs.news_decision_engine import RECORDED_TIME, NewsDecisionEngine
from tokmo.records.news_decision_store import NewsDecisionStore, kst, timestamp


@pytest.mark.parametrize("invalid_first", [False, True])
def test_no_additional_source_does_not_trigger_fake_restoration(
    tmp_path, invalid_first
):
    seen = []

    def analyze(request):
        seen.append(deepcopy(request))
        return decision(
            ["unknown"] if invalid_first and len(seen) == 1 else ["a"],
            notify=False,
            change="NONE",
            source_requests=["a"],
        )

    store = NewsDecisionStore(tmp_path / "state.db")
    try:
        engine = NewsDecisionEngine(
            store, screen, analyze, clock=RECORDED_TIME, context_mode="efficient"
        )
        result = engine.tick([observation("a", AT)], AT)
        assert not result["failures"]
        assert len(seen) == (2 if invalid_first else 1)
        assert seen[0]["source_availability"]["expandable_ids"] == []
        assert result["source_restore_noops"][0]["source_ids"] == ["a"]
        assert store.events()[0]["source_restore_noop_count"] == 1
        if invalid_first:
            assert (
                seen[1]["correction"]["reason"] != "requested_original_sources_restored"
            )
            assert seen[1]["correction"]["restored_source_ids"] == []
    finally:
        store.close()


def context():
    return dict(
        portfolio_status="FRESH",
        cash_status="CASH_AVAILABLE",
        holdings=[dict(key="KR:1", name="기업 A", quantity="1")],
        investment_followup=dict(
            status="OK",
            choices=[
                dict(id="a-choice", keys=["KR:1"], reason_quotes=["사업 확장"]),
                dict(id="b-choice", keys=["US:OTHER"], reason_quotes=["시장 성장"]),
            ],
            delivered_reviews=[
                dict(id="a-report", choice_id="a-choice", summary="A 이전 판단"),
                dict(id="b-report", choice_id="b-choice", summary="B 이전 판단"),
            ],
        ),
    )


def review_setup(tmp_path, scope, *, next_scope=None, hours=18):
    state, seen = context(), []
    store = NewsDecisionStore(tmp_path / "state.db")

    def analyze(request):
        seen.append(deepcopy(request))
        value = next_scope if next_scope is not None and len(seen) > 1 else scope
        return decision(
            [r["id"] for r in request["evidence"]],
            notify=False,
            change="NONE",
            followup_scope=value,
            recheck=dict(
                after=kst(timestamp(request["now"]) + timedelta(hours=hours)),
                reason="다음 확인 기한 전까지 같은 근거 재사용",
            ),
        )

    engine = NewsDecisionEngine(
        store,
        screen,
        analyze,
        clock=RECORDED_TIME,
        context_mode="efficient",
        reuse_unchanged_reviews=True,
        context_reader=lambda at: dict(deepcopy(state), observed_at=kst(at)),
    )
    assert not engine.tick([observation("a", AT)], AT)["failures"]
    return store, engine, seen, state


@pytest.mark.parametrize("ids", [[], ["a-choice"]])
def test_unrelated_report_change_reuses_but_relevant_report_change_rechecks(
    tmp_path, ids
):
    store, engine, seen, state = review_setup(
        tmp_path, dict(all_choices=False, choice_ids=ids)
    )
    try:
        state["investment_followup"]["delivered_reviews"][1]["summary"] = (
            "관계없는 B의 새 판단"
        )
        result = engine.tick([], AT + timedelta(hours=6))
        assert result["calls"] == 0 and result["reused_reviews"] and len(seen) == 1
        # Restart does not widen or lose the stored scope.
        store.close()
        store = NewsDecisionStore(tmp_path / "state.db")
        engine.store = store
        state["investment_followup"]["delivered_reviews"][0]["summary"] = "A의 새 판단"
        result = engine.tick([], AT + timedelta(hours=12))
        assert not result["failures"]
        if ids:
            assert len(seen) == 2
            assert result["review_reuse_misses"][0]["reasons"] == [
                "FOLLOWUP_REPORT_CHANGED"
            ]
            assert [
                r["choice_id"]
                for r in seen[-1]["context"]["investment_followup"]["delivered_reviews"]
            ] == ids
            assert len(seen[-1]["context"]["investment_followup"]["choices"]) == 2
        else:
            assert result["calls"] == 0
    finally:
        store.close()


@pytest.mark.parametrize(
    "scope",
    [
        None,
        {"all_choices": True, "choice_ids": []},
        {"all_choices": False, "choice_ids": ["unknown"]},
    ],
)
def test_marketwide_or_unknown_scope_keeps_all_followup_reports(tmp_path, scope):
    store, engine, seen, state = review_setup(tmp_path, scope)
    try:
        state["investment_followup"]["delivered_reviews"][1]["summary"] = (
            "새로운 시장 위험"
        )
        result = engine.tick([], AT + timedelta(hours=6))
        assert not result["failures"] and len(seen) == 2
        assert len(seen[-1]["context"]["investment_followup"]["delivered_reviews"]) == 2
    finally:
        store.close()


@pytest.mark.parametrize(
    "change", ["quantity", "choice", "cash", "stale", "new_evidence"]
)
def test_changed_user_basis_or_new_evidence_restores_broad_view(tmp_path, change):
    store, engine, seen, state = review_setup(
        tmp_path, dict(all_choices=False, choice_ids=[])
    )
    try:
        rows = []
        if change == "quantity":
            state["holdings"][0]["quantity"] = "2"
        elif change == "choice":
            state["investment_followup"]["choices"][1]["reason_quotes"] = [
                "다른 선택 이유"
            ]
        elif change == "cash":
            state["cash_status"] = "INVESTED"
        elif change == "stale":
            state["portfolio_status"] = "STALE"
        else:
            rows = [observation("risk", AT + timedelta(hours=6))]
        result = engine.tick(rows, AT + timedelta(hours=6))
        assert not result["failures"] and len(seen) == 2
        assert len(seen[-1]["context"]["investment_followup"]["delivered_reviews"]) == 2
        if not rows:
            assert result["review_reuse_misses"][0]["reasons"] == ["PORTFOLIO_CHANGED"]
        if change == "choice":
            snapshot = seen[-1]["events"][0]["context_snapshot"]
            assert snapshot["holdings"] == {"same_content_as": "context.holdings"}
            assert snapshot["investment_followup"]["choices"][1]["reason_quotes"] == [
                "시장 성장"
            ]
            assert seen[-1]["context"]["investment_followup"]["choices"][1][
                "reason_quotes"
            ] == ["다른 선택 이유"]
    finally:
        store.close()


def test_timed_review_can_expand_its_dependencies_with_existing_second_call(tmp_path):
    store, engine, seen, state = review_setup(
        tmp_path,
        dict(all_choices=False, choice_ids=["a-choice"]),
        next_scope=dict(all_choices=True, choice_ids=[]),
        hours=6,
    )
    try:
        result = engine.tick([], AT + timedelta(hours=6))
        assert not result["failures"] and len(seen) == 3
        assert len(seen[1]["context"]["investment_followup"]["delivered_reviews"]) == 1
        assert len(seen[2]["context"]["investment_followup"]["delivered_reviews"]) == 2
        assert seen[2]["correction"]["reason"] == "followup_context_expanded"
        assert seen[2]["correction"]["restored_source_ids"] == []
        assert result["review_reuse_misses"][0]["reasons"] == ["DEADLINE_REACHED"]
    finally:
        store.close()


def test_related_review_timestamp_does_not_invalidate_but_new_facts_do(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    seen = []
    other = event(999)
    store.save_event(other)

    def select(request):
        return dict(
            candidates=[
                dict(
                    event_id=None,
                    evidence_ids=["a"],
                    comparison_event_ids=[other["id"]],
                    event_reason="관련 별개 사건 비교",
                )
            ],
            ignored=[],
        )

    def analyze(request):
        seen.append(request)
        return decision(
            ["a"],
            notify=False,
            change="NONE",
            recheck=dict(after=kst(AT + timedelta(hours=18)), reason="확인 일정"),
        )

    try:
        engine = NewsDecisionEngine(
            store,
            select,
            analyze,
            clock=RECORDED_TIME,
            context_mode="efficient",
            reuse_unchanged_reviews=True,
        )
        assert not engine.tick([observation("a", AT)], AT)["failures"]
        other["updated_at"] = kst(AT + timedelta(hours=1))
        other["context_memory"]["reviewed_at"] = other["updated_at"]
        store.save_event(other)
        result = engine.tick([], AT + timedelta(hours=6))
        assert result["calls"] == 0 and result["reused_reviews"]
        other["context_memory"]["summary"] = "공급 차질이 새로 확인됨"
        store.save_event(other)
        result = engine.tick([], AT + timedelta(hours=12))
        assert not result["failures"] and len(seen) == 2
        assert result["review_reuse_misses"][0]["reasons"] == ["RELATED_EVENT_CHANGED"]
        assert (
            seen[-1]["continuity"]["related_events"][0]["memory"]["summary"]
            == "공급 차질이 새로 확인됨"
        )
    finally:
        store.close()


def test_legacy_plan_gets_one_fresh_review_and_misses_are_reported(tmp_path):
    from report_news_decisions import report

    store, engine, seen, state = review_setup(
        tmp_path, dict(all_choices=False, choice_ids=[])
    )
    try:
        old = store.events()[0]
        old["review_reuse_plan"].pop("basis")
        store.save_event(old)
        result = engine.tick([], AT + timedelta(hours=6))
        assert len(seen) == 2 and not result["failures"]
        assert result["review_reuse_misses"][0]["reasons"] == ["LEGACY_PLAN"]
        summary = report(tmp_path / "state.db", since=kst(AT))
        assert summary["reuse_cumulative"]["misses"] == {"LEGACY_PLAN": 1}
        assert "기간 집계" in summary["reuse_cumulative"]["note"]
    finally:
        store.close()
