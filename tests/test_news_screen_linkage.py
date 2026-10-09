"""Public RCA fixtures: contract/wiring only, never a model-quality verdict."""

from copy import deepcopy

import pytest
from test_news_context_efficiency import event
from test_news_decision_engine import (
    Engine,
    NewsDecisionStore,
    decision,
    observation,
    timestamp,
)

from tokmo.programs.news_decision_engine import validate_screen

AT = timestamp("2026-09-29T10:35:00+09:00")
OLD = "event-562dbe19e32100eebcf574c8"
NEW = "news:b36a9dceb8484c13a566fce3d846c805:1"


def test_screen_generation_schema_requires_review():
    from news_decision_models import response_schema

    candidate = response_schema("screen")["properties"]["candidates"]["items"]
    assert {"event_id", "comparison_event_ids", "event_reason"} <= set(
        candidate["required"]
    )
    assert candidate["properties"]["event_reason"] == {"type": "string"}


def public_case():
    old = event(0)
    source = dict(
        observation(
            "news:24efc9dec4a740588ed3119e6aeaba6d:1",
            timestamp("2026-09-28T16:15:54.591864+09:00"),
        ),
        title="삼전·닉스, 5%대 급락…외국인·기관, 동반 '팔자'(종합)",
        published_at="2026-09-28T15:57:36+09:00",
    )
    old.update(
        id=OLD,
        evidence=[source],
        updated_at=source["observed_at"],
        last_delivery="NOT_SENT",
    )
    old["decision"].update(
        title="삼성전자·SK하이닉스 급락 후 새 시장 관측 없음",
        evidence_ids=[source["id"]],
    )
    old["context_memory"].update(
        phase="28일 반도체 급락의 매도 주체가 보강된 뒤 새 시장 관측을 기다리는 국면",
        source_ids=[source["id"]],
    )
    new = dict(
        observation(NEW, timestamp("2026-09-29T07:21:27.745864+09:00")),
        title="SK Hynix Stock Falls 5.1% as Rubin Memory Moves Into View",
        publisher="Yahoo",
        text="The display confirms HBM4 and SOCAMM2 integration, but customer volume and pricing remain undisclosed.",
        published_at="2026-09-29T05:42:23+09:00",
        time_interpretation="SOURCE_NATIVE",
    )
    return old, new


@pytest.mark.parametrize("mode", ["compare", "same", "unrelated", "failed"])
def test_missing_review_correction_and_selected_context(tmp_path, mode):
    old, new = public_case()
    if mode == "unrelated":
        new.update(
            title="다른 국가의 신규 수입 규제 발표",
            text="반도체 시세와 별개의 정책 사건",
        )
    store = NewsDecisionStore(tmp_path / "state.db")
    seen = []
    analyses = []
    store.save_event(old)
    for n in range(1, 14):
        other = event(n)
        other["decision"]["title"] = new["title"]
        store.save_event(other)
    broken = dict(candidates=[dict(evidence_ids=[NEW], event_id=None)], ignored=[])

    def screen(request):
        seen.append(deepcopy(request))
        if "correction" not in request or mode == "failed":
            return deepcopy(broken)
        assert request["correction"]["previous_response"] == broken
        return dict(
            candidates=[
                dict(
                    evidence_ids=[NEW],
                    event_id=OLD if mode == "same" else None,
                    comparison_event_ids=[OLD] if mode == "compare" else [],
                    event_reason={
                        "compare": "급락과 전시 보도의 관계를 비교하되 별개 사건으로 유지",
                        "same": "28일 급락을 다시 다루는 같은 사건",
                        "unrelated": "제공 목록의 반도체 시세와 별개의 국가 정책",
                    }[mode],
                )
            ],
            ignored=[],
        )

    engine = Engine(
        store,
        screen,
        lambda req: analyses.append(req) or decision([NEW], material_change=None),
        context_mode="efficient",
    )
    try:
        result = engine.tick([new], AT)
        assert len(seen) == 2 and result["corrections"][0]["status"] == (
            "FAILED" if mode == "failed" else "OK"
        )
        row = next(r for r in seen[0]["events"] if r["event_id"] == OLD)
        assert row["detail_included"] is False and row["last_sent_at"] is None
        assert (
            row["source_time_context"][0]["published_at"]
            == old["evidence"][0]["published_at"]
        )
        assert (
            row["phase"] == old["context_memory"]["phase"]
            and not seen[0]["sent_bodies"]
        )
        if mode == "failed":
            assert (
                not analyses
                and not result["alerts"]
                and result["input_states"] == {"ERROR": 1}
            )
            assert (
                store.observation(NEW)["text"] == new["text"]
                and len(store.events()) == 14
            )
            store.retry_errors([NEW])
            assert store.pending()[0]["id"] == NEW
        else:
            assert (
                not result["failures"]
                and len(analyses) == 1
                and len(result["alerts"]) == 1
            )
            request = analyses[0]
            assert [r["event_id"] for r in request["continuity"]["related_events"]] == (
                [OLD] if mode == "compare" else []
            )
            if mode == "compare":
                assert (
                    request["continuity"]["related_events"][0]["source_time_context"]
                    == row["source_time_context"]
                )
            assert [r["id"] for r in request["events"]] == (
                [OLD] if mode == "same" else []
            )
            assert request["comparison"]["coverage"]["baseline_applicable"] is False
            assert not request["comparison"]["other_events"] and result[
                "input_states"
            ] == {"DONE": 1}
    finally:
        store.close()


@pytest.mark.parametrize(
    "patch",
    [
        dict(),
        dict(event_reason="", comparison_event_ids=[]),
        dict(event_reason="검토", comparison_event_ids=None),
        dict(event_reason="검토", comparison_event_ids=False),
        dict(event_reason="검토", comparison_event_ids=["absent"]),
        dict(event_reason="검토", comparison_event_ids=[{}]),
        dict(event_reason="검토", comparison_event_ids=[], event_id="absent"),
    ],
)
def test_incomplete_or_invalid_review_is_rejected(patch):
    with pytest.raises(ValueError, match="screen"):
        validate_screen(
            dict(
                candidates=[dict(evidence_ids=[NEW], event_id=None) | patch], ignored=[]
            ),
            {NEW},
            {OLD},
            {},
        )


@pytest.mark.parametrize(
    "candidates",
    [
        [],
        [
            dict(
                evidence_ids=[NEW],
                event_id=OLD,
                comparison_event_ids=[],
                event_reason="같은 사건",
            )
        ],
    ],
)
def test_invalid_ignored_event(candidates):
    with pytest.raises(ValueError, match="event"):
        validate_screen(
            dict(
                candidates=candidates,
                ignored=[dict(evidence_id=NEW, event_id="absent", reason="반복")],
            ),
            {NEW},
            {OLD},
            {},
        )


def test_unknown_publication_stays_unknown(tmp_path):
    old, new = public_case()
    old["evidence"][0].pop("published_at")
    store = NewsDecisionStore(tmp_path / "s.db")
    try:
        request = Engine(store, None, None)._screen_request([new], [old])
        source = request["events"][0]["source_time_context"][0]
        assert source["published_at"] is None and timestamp(
            source["observed_at"]
        ) == timestamp(old["evidence"][0]["observed_at"])
        assert "occurred_at" not in source and "event_time" not in source
    finally:
        store.close()
