"""Input reduction and call reuse are independently switchable and falsifiable."""

from copy import deepcopy
from datetime import timedelta

import pytest
from test_news_decision_engine import decision, observation, screen

from tokmo.programs.news_decision_engine import RECORDED_TIME, NewsDecisionEngine
from tokmo.programs.news_incremental_review import (
    dependency_context,
    dependent_keys,
    expand_prior_delta,
    prior_delta,
    review_dependencies,
)
from tokmo.records.news_decision_store import NewsDecisionStore, encoded, kst, timestamp

AT = timestamp("2026-09-09T10:00:00+09:00")


def context():
    return dict(
        holdings=[
            dict(key="KR:1", name="회사1", quantity="1", account="a"),
            dict(key="KR:2", name="회사2", quantity="2", account="a"),
        ],
        instruments=[dict(key="KR:1"), dict(key="KR:2")],
        portfolio_status="FRESH",
        investment_followup=dict(
            status="OK",
            choices=[
                dict(
                    id="choice1",
                    keys=["KR:1"],
                    reason_quotes=["이번 분기 양산 예정, 실패하면 비중 재검토"],
                    summary="최대 10억 조건부 투자",
                )
            ],
            delivered_reviews=[],
        ),
    )


@pytest.mark.parametrize(
    "change", ["same", "reason", "unit", "removed", "reordered", "duplicate", "escaped"]
)
def test_prior_view_is_exact_reconstructable_and_never_larger(change):
    old = context()
    old["long"] = "원래 계획은 조건부이며 실제 실적이 아님. " * 80
    new = deepcopy(old)
    if change == "reason":
        new["investment_followup"]["choices"][0]["reason_quotes"] = ["계획 철회"]
    elif change == "unit":
        new["investment_followup"]["choices"][0]["summary"] = "10억 달러 확정"
    elif change == "removed":
        new["investment_followup"]["choices"] = []
    elif change == "reordered":
        new["holdings"].reverse()
    elif change == "duplicate":
        new["holdings"].append(deepcopy(new["holdings"][0]))
    elif change == "escaped":
        old["a~/b"] = new["a~/b"] = {"text": "긴 설명" * 80}
        new["long"] = "변화"
    before = deepcopy(old), deepcopy(new)
    small = prior_delta(old, new)
    assert expand_prior_delta(small, new) == old
    assert len(encoded(small)) <= len(encoded(old))
    assert (old, new) == before


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"all_portfolio": False, "keys": ["UNKNOWN"]},
        {"all_portfolio": False, "keys": "KR:1"},
    ],
)
def test_unknown_dependencies_fall_back_to_broad_review(bad):
    assert review_dependencies(bad, context()) == dict(all_portfolio=True, keys=[])


@pytest.mark.parametrize(
    "change",
    ["source_date", "weights", "stale", "order", "duplicate", "market", "removed"],
)
def test_etf_history_preserves_changes_without_repeating_unchanged_composition(change):
    old = {
        "etf_context": [
            dict(
                market="KR",
                symbol="ETF001",
                source_date="2026-09-15",
                components={
                    "rows": [
                        dict(code=str(i), name="구성종목" + str(i), weight_pct="1.0")
                        for i in range(30)
                    ]
                },
                nav={"at": "2026-09-15T15:30:00+09:00", "status": "FRESH"},
            ),
            dict(
                market="KR",
                symbol="ETF002",
                source_date="2026-09-15",
                note="구성 미확인" * 20,
            ),
        ]
    }
    new = deepcopy(old)
    new["etf_context"][0]["nav"]["at"] = "2026-09-16T15:30:00+09:00"
    if change == "source_date":
        new["etf_context"][0]["source_date"] = "2026-09-16"
    elif change == "weights":
        new["etf_context"][0]["components"]["rows"][0]["weight_pct"] = "3.0"
    elif change == "stale":
        new["etf_context"][0]["nav"]["status"] = "STALE"
    elif change == "order":
        new["etf_context"].reverse()
    elif change == "duplicate":
        new["etf_context"].append(deepcopy(new["etf_context"][0]))
    elif change == "market":
        new["etf_context"][0]["market"] = "US"
    elif change == "removed":
        new["etf_context"].pop(0)
    view = prior_delta(old, new)
    assert expand_prior_delta(view, new) == old
    assert len(encoded(view)) <= len(encoded(old))
    if change in {"source_date", "stale", "order"}:
        assert len(encoded(view)) < len(encoded(old)) * 0.4
    if change == "duplicate":
        assert view["etf_context"][0] == old["etf_context"][0]


def setup(tmp_path, *, scoped=True, broad=False, mode="incremental"):
    data = context()
    calls = []

    def analyze(request):
        calls.append(deepcopy(request))
        return decision(
            [r["id"] for r in request["evidence"]],
            notify=False,
            change="NONE",
            urgency="NORMAL",
            review_dependencies=dict(
                all_portfolio=broad, keys=[] if broad else ["KR:1"]
            ),
            followup_scope=dict(all_choices=False, choice_ids=["choice1"]),
            recheck=dict(
                after=kst(AT + timedelta(hours=18)), reason="업무 발표 시각 확인"
            ),
        )

    analyze.model = "recorded"
    analyze.policy_fingerprint = "stable"
    store = NewsDecisionStore(tmp_path / "s.sqlite3")
    engine = NewsDecisionEngine(
        store,
        screen,
        analyze,
        clock=RECORDED_TIME,
        context_reader=lambda at: deepcopy(data),
        context_mode=mode,
        reuse_unchanged_reviews=True,
        scoped_review_reuse=scoped,
    )
    result = engine.tick([observation("a", AT)], AT)
    assert not result["failures"]
    return store, engine, calls, data


@pytest.mark.parametrize(
    "change", ["unrelated_quantity", "unrelated_choice", "unrelated_report"]
)
def test_irrelevant_changes_reuse_but_never_rewrite_original_assessment(
    tmp_path, change
):
    store, engine, calls, data = setup(tmp_path)
    old = deepcopy(store.events()[0])
    try:
        if change == "unrelated_quantity":
            data["holdings"][1]["quantity"] = "3"
        elif change == "unrelated_choice":
            data["investment_followup"]["choices"].append(
                dict(id="c2", keys=["KR:2"], summary="증가")
            )
        else:
            data["investment_followup"]["delivered_reviews"].append(
                dict(id="r2", choice_id="c2", summary="추가 검토")
            )
        # A process restart must not depend on a stale in-memory portfolio.
        if hasattr(engine, "_display_context"):
            del engine._display_context
        result = engine.tick([], AT + timedelta(hours=6))
        assert (
            not result["failures"] and result["calls"] == 0 and result["reused_reviews"]
        )
        assert len(calls) == 1 and store.events()[0]["updated_at"] == old["updated_at"]
        assert store.events()[0]["context_snapshot"] == old["context_snapshot"]
    finally:
        store.close()


@pytest.mark.parametrize(
    "change",
    [
        "quantity",
        "reason",
        "new_asset",
        "new_watch",
        "health",
        "global_choice",
        "related_report",
        "source",
    ],
)
def test_relevant_changes_and_unknown_new_exposure_still_reassess(tmp_path, change):
    store, engine, calls, data = setup(tmp_path)
    try:
        if change == "quantity":
            data["holdings"][0]["quantity"] = "3"
        elif change == "reason":
            data["investment_followup"]["choices"][0]["summary"] = "핵심 가설 변경"
        elif change == "new_asset":
            data["holdings"].append(dict(key="KR:3", quantity="1"))
        elif change == "new_watch":
            data["instruments"].append(dict(key="KR:3"))
        elif change == "health":
            data["portfolio_status"] = "STALE"
        elif change == "global_choice":
            data["investment_followup"]["choices"].append(
                dict(id="cash", keys=[], summary="현금 대기")
            )
        elif change == "related_report":
            data["investment_followup"]["delivered_reviews"].append(
                dict(id="r1", choice_id="choice1", summary="새 후속 결과")
            )
        else:
            e = store.events()[0]
            e["evidence"][0]["text"] = "동일 문서 정정: 최대 금액이며 확정 아님"
            store.save_event(e)
        result = engine.tick([], AT + timedelta(hours=6))
        assert not result["failures"] and result["calls"] == 1 and len(calls) == 2
        assert calls[-1]["review_trigger"]["reasons"]
        assert "basis" not in calls[-1]["review_trigger"]["recheck_plan"]
    finally:
        store.close()


@pytest.mark.parametrize("scoped,broad", [(False, False), (True, True)])
def test_input_only_mode_and_broad_events_keep_existing_rechecks(
    tmp_path, scoped, broad
):
    store, engine, calls, data = setup(tmp_path, scoped=scoped, broad=broad)
    try:
        data["holdings"][1]["quantity"] = "10"
        result = engine.tick([], AT + timedelta(hours=6))
        assert not result["failures"] and len(calls) == 2
    finally:
        store.close()


def test_etf_membership_broadens_relevant_holdings_without_claiming_weight():
    data = context()
    data["holdings"].append(dict(key="KR:ETF001", quantity="10"))
    data["etf_context"] = [
        dict(
            symbol="ETF001",
            components=dict(rows=[dict(code="1", market=None)], coverage="PARTIAL"),
        )
    ]
    e = dict(
        review_dependencies=dict(all_portfolio=False, keys=["KR:1"]),
        followup_scope=dict(all_choices=False, choice_ids=[]),
    )
    assert dependent_keys(e, data) == {"KR:1", "KR:ETF001"}
    old = dependency_context(e, data)
    data["holdings"][-1]["quantity"] = "20"
    assert dependency_context(e, data) != old


@pytest.mark.parametrize(
    "kind", ["deadline", "new_shock", "morning", "evening", "recovery"]
)
def test_scoping_does_not_delay_new_shocks_deadlines_or_delivery_reviews(
    tmp_path, kind
):
    store, engine, calls, data = setup(tmp_path)
    try:
        when = AT + timedelta(hours=18 if kind == "deadline" else 6)
        batch = []
        if kind == "new_shock":
            batch = [observation("shock", when)]
        elif kind in {"morning", "evening", "recovery"}:
            e = store.events()[0]
            e["recovery_review_at" if kind == "recovery" else "held_until"] = kst(when)
            if kind == "evening":
                e["held_kind"] = "EVENING_HEADLINES"
            store.save_event(e)
        if hasattr(engine, "_display_context"):
            del engine._display_context
        result = engine.tick(batch, when)
        assert not result["failures"] and len(calls) == 2
    finally:
        store.close()


def test_incremental_schema_is_opt_in():
    from news_decision_models import response_schema

    assert "review_dependencies" not in response_schema("analyze")["properties"]
    assert (
        "review_dependencies"
        in response_schema("analyze", incremental=True)["required"]
    )
    assert response_schema("screen") == response_schema("screen", incremental=True)


def test_same_replay_fixture_compares_input_only_and_scoped_reuse(tmp_path):
    import json
    from pathlib import Path

    from replay_news_alerts import replay

    case = json.loads(
        (
            Path(__file__).parent
            / "fixtures/news_alert_cases/synthetic-incremental-dependencies.json"
        ).read_text()
    )
    reports = []
    for name, mode, scope in [
        ("A", "efficient", False),
        ("B", "incremental", False),
        ("C", "incremental", True),
    ]:
        reports.append(
            replay(
                case,
                tmp_path / (name + ".db"),
                context_mode=mode,
                reuse_unchanged_reviews=True,
                scoped_review_reuse=scope,
            )
        )
    assert all(r["passed"] for r in reports)
    assert [r["calls"] for r in reports] == [5, 5, 4]
    assert (
        reports[2]["timeline"][-1]["result"]["calls"] == 1
    )  # actual deadline still reviewed
    assert (
        reports[2]["timeline"][2]["result"]["calls"] == 1
    )  # related quantity change still reviewed
