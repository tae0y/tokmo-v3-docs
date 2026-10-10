"""Choice-level accumulation, important changes, durability and readable output."""

import json
import sqlite3
from copy import deepcopy
from datetime import timedelta

import pytest
from test_investment_followup import (
    NOW,
    choice,
    response,
    review,
    store,  # pytest fixture
)

from tokmo.programs.followup_bundling import choice_context
from tokmo.programs.investment_followup import deliver_pending, render_followup
from tokmo.programs.investment_followup_worker import run_tick
from tokmo.records.investment_followup import InvestmentFollowupStore


def news(path, key, at, text="같은 국면의 추가 설명"):
    conn = sqlite3.connect(path)
    conn.execute("create table if not exists news_decision_events(state text)")
    conn.execute(
        "insert into news_decision_events values(?)",
        (
            json.dumps(
                {
                    "id": key,
                    "updated_at": at.isoformat(),
                    "decision": {"title": text, "evidence_ids": [key]},
                    "evidence": [
                        {
                            "id": key,
                            "title": text,
                            "text": text,
                            "observed_at": at.isoformat(),
                        }
                    ],
                }
            ),
        ),
    )
    conn.commit()
    conn.close()


def router(timing="RECORD", seen=None):
    def run(prompt):
        packet = json.loads(prompt.rsplit("\n", 1)[-1])
        if seen is not None:
            seen.append(packet)
        return {
            "matches": [
                {
                    "choice_id": c["id"],
                    "event_ids": [e["id"] for e in packet["events"]],
                    "reason": "선택에 영향을 주는 새로운 근거",
                    "assessment": timing,
                }
                for c in packet["choices"]
            ],
            "unmatched_reason": "나머지 없음",
        }

    return run


def analyst(seen):
    def run(prompt):
        packet = json.loads(prompt.split("\nINPUT\n", 1)[1])
        seen.append(packet)
        return response(
            notify=False, material_change=False, pending_action="KEEP", new_facts=[]
        )

    return run


def baseline(store):
    c = choice(store)
    review(store, c, raw=response(notify=False, pending_action="KEEP"))
    return c


def test_context_is_recorded_without_arbitrary_hour_delay_then_changes_review_union(
    store, tmp_path
):
    c = baseline(store)
    path = tmp_path / "news.db"
    seen = []
    news(path, "one", NOW)
    args = dict(
        news_path=path, snapshot=None, model=analyst(seen), screen_model=router()
    )
    first = run_tick(store, at=NOW, **args)
    assert not first["reviewed"] and first["record_only_choices"] == 1
    news(path, "two", NOW + timedelta(minutes=40))
    run_tick(store, at=NOW + timedelta(minutes=40), **args)
    run_tick(store, at=NOW + timedelta(minutes=60), **args)
    assert not seen  # No hourly wake-up just because time passed.
    news(path, "three", NOW + timedelta(minutes=65))
    result = run_tick(
        store,
        at=NOW + timedelta(minutes=65),
        **{**args, "screen_model": router("REVIEW")},
    )
    assert len(result["reviewed"]) == 1 and {e["id"] for e in seen[0]["evidence"]} == {
        "one",
        "two",
        "three",
    }
    assert not store.watermark("followup_bundle:" + c["id"])


def test_first_assessment_is_not_delayed_even_if_router_records_context(
    store, tmp_path
):
    choice(store)
    path = tmp_path / "news.db"
    seen = []
    news(path, "first", NOW)
    result = run_tick(
        store,
        news_path=path,
        snapshot=None,
        at=NOW,
        model=analyst(seen),
        screen_model=router(),
    )
    assert len(result["reviewed"]) == 1 and seen[0]["evidence"][0]["id"] == "first"


def test_important_change_reviews_now_with_previously_accumulated_evidence(
    store, tmp_path
):
    baseline(store)
    path = tmp_path / "news.db"
    seen = []
    news(path, "background", NOW)
    args = dict(news_path=path, snapshot=None, model=analyst(seen))
    run_tick(store, at=NOW, screen_model=router(), **args)
    news(
        path,
        "major-change",
        NOW + timedelta(minutes=5),
        "장기 사건에서 갑자기 중요한 변화",
    )
    result = run_tick(
        store, at=NOW + timedelta(minutes=5), screen_model=router("REVIEW"), **args
    )
    assert len(result["reviewed"]) == 1
    assert {e["id"] for e in seen[0]["evidence"]} == {"background", "major-change"}


def test_retained_facts_survive_restart_and_existing_periodic_review(store, tmp_path):
    c = baseline(store)
    path = tmp_path / "news.db"
    seen = []
    news(path, "old", NOW)
    args = dict(
        news_path=path, snapshot=None, model=analyst(seen), screen_model=router()
    )
    run_tick(store, at=NOW, **args)
    # A new connection sees facts, not just opaque IDs/short routing reasons.
    reopened = InvestmentFollowupStore(tmp_path / "choices.sqlite3")
    context = choice_context(reopened, c, NOW)
    assert (
        context["accumulating"]["facts"][0]["facts"][0]["title"]
        == "같은 국면의 추가 설명"
    )
    path.unlink()
    result = run_tick(reopened, at=NOW + timedelta(hours=6), **args)
    assert result["reviewed"] and seen[0]["evidence"][0]["id"] == "old"
    reopened.close()


@pytest.mark.parametrize("hour", [12, 13, 14])
def test_analysis_does_not_inherit_quiet_hours(store, tmp_path, hour):
    baseline(store)
    at = NOW.replace(hour=hour, minute=30)
    path = tmp_path / "news.db"
    news(path, "source", at)

    def model(prompt):
        p = json.loads(prompt.split("\nINPUT\n")[1])
        return response(
            new_facts=[{"text": "새로운 위험이 확인됐다.", "sources": ["source"]}]
        )

    result = run_tick(
        store,
        news_path=path,
        snapshot=None,
        at=at,
        model=model,
        screen_model=router("REVIEW"),
    )
    assert len(result["reviewed"]) == 1
    pending = store.pending(at=at)
    assert bool(pending) == (
        hour == 12
    )  # 21:30 sends normally; 22:30/23:30 waits to send only.


def test_old_hour_bundle_is_drained_without_waiting_for_its_due_at(store, tmp_path):
    c = baseline(store)
    seen = []
    event = {
        "id": "legacy",
        "updated_at": NOW.isoformat(),
        "decision": {},
        "evidence": [
            {"id": "old", "title": "old", "text": "old", "observed_at": NOW.isoformat()}
        ],
    }
    store.set_watermark(
        "followup_bundle:" + c["id"],
        {
            "first_at": NOW.isoformat(),
            "due_at": (NOW + timedelta(hours=11)).isoformat(),
            "events": {"legacy": event},
            "reasons": ["old classification"],
        },
    )
    result = run_tick(
        store,
        news_path=tmp_path / "missing",
        snapshot=None,
        at=NOW,
        model=analyst(seen),
    )
    assert result["reviewed"] and seen[0]["evidence"][0]["id"] == "old"
    assert not store.watermark("followup_bundle:" + c["id"])


def test_failed_review_uses_existing_bounded_retry_journal(store, tmp_path):
    c = baseline(store)
    path = tmp_path / "news.db"
    news(path, "source", NOW)
    seen = []

    def broken(_):
        raise TimeoutError()

    args = dict(news_path=path, snapshot=None, screen_model=router("REVIEW"))
    result = run_tick(store, at=NOW, model=broken, **args)
    assert result["errors"] and store.watermark("failed_review:" + c["id"])
    assert not store.watermark("followup_bundle:" + c["id"])
    assert not run_tick(
        store, at=NOW + timedelta(seconds=30), model=analyst(seen), **args
    )["reviewed"]
    result = run_tick(
        store, at=NOW + timedelta(seconds=60), model=analyst(seen), **args
    )
    assert result["reviewed"] and seen[-1]["evidence"][0]["id"] == "source"


def test_router_receives_actual_delivery_and_accumulating_context(store, tmp_path):
    c = choice(store)
    first = review(store, c)
    deliver_pending(store, at=NOW, sender=lambda _: "local:1")
    path = tmp_path / "news.db"
    news(path, "one", NOW)
    screens = []
    run_tick(
        store,
        news_path=path,
        snapshot=None,
        at=NOW,
        model=analyst([]),
        screen_model=router(seen=screens),
    )
    ctx = screens[0]["choices"][0]["followup_context"]
    assert ctx["last_delivered"]["id"] == first["id"]
    assert choice_context(store, c, NOW)["accumulating"]["event_ids"] == ["one"]


def test_explicit_context_only_judgment_does_not_notify_despite_boolean_conflict(store):
    c = choice(store)
    r = review(store, c, raw=response(material_change=False, notify=True))
    assert not r["assessment"]["notify"] and not store.pending(at=NOW)


def test_same_action_can_still_notify_important_relief_twice_same_hour(store):
    c = choice(store)
    review(store, c)
    deliver_pending(store, at=NOW, sender=lambda _: "local:first")
    r = review(
        store,
        c,
        at=NOW + timedelta(minutes=5),
        trigger="relief",
        raw=response(
            market_risk="RELIEVED",
            notification_reason="이전 경고의 핵심 근거가 공식적으로 해소됐다.",
        ),
    )
    assert r["assessment"]["notify"]
    assert deliver_pending(
        store, at=NOW + timedelta(minutes=5), sender=lambda _: "local:second"
    )


def test_render_preserves_whole_negation_and_url_with_paragraphs(store):
    c = choice(store)
    long = (
        "새로 확보한 자료의 집계 기간과 관측 범위가 명확하지 않고 이전 발표와의 연결도 확인되지 않아 "
        + ("해석에 필요한 조건을 먼저 살피면서 " * 12)
        + "현재까지 감소가 지속된다고 단정할 수 없어."
    )
    url = "https://example.invalid/" + ("a" * 400)
    r = review(
        store,
        c,
        ev=[
            {"id": "war", "text": long, "source": url, "available_at": NOW.isoformat()}
        ],
        raw=response(new_facts=[{"text": long, "sources": ["war"]}]),
    )
    text = render_followup(r)
    assert text.startswith("퀸트 정기 분석 알림 · ")
    assert "\n\n지금 판단\n" in text and "\n\n다음 확인\n• " in text
    assert long in text and url in text and "…" not in text


def test_oversized_optional_facts_are_omitted_as_whole_sentences(store):
    r = review(store, choice(store))
    r = deepcopy(r)
    r["assessment"]["new_facts"] = [
        {"text": str(i) + "긴 설명 " * 190 + "단정하지 않는다.", "sources": ["war"]}
        for i in range(5)
    ]
    text = render_followup(r)
    assert len(text.encode("utf-16-be")) // 2 <= 3800
    assert "일부 상세 근거는 생략" in text
    for f in r["assessment"]["new_facts"]:
        assert f["text"] in text or f["text"][:50] not in text


def test_telegram_buttons_preserve_full_urls_without_body_clutter():
    from tokmo.programs.investment_followup import followup_telegram_message

    url = "https://example.invalid/" + ("x" * 400)
    text, buttons = followup_telegram_message(
        "지금 판단\n결론이야.\n\n출처: " + url + "\n\n출처: 공식 발표"
    )
    assert text == "지금 판단\n결론이야.\n\n출처: 공식 발표"
    assert buttons == {"inline_keyboard": [[{"text": "원문 1", "url": url}]]}


def test_no_source_button_for_non_url_or_untrusted_scheme():
    from tokmo.programs.investment_followup import followup_telegram_message

    body = "지금 판단\n결론이야.\n\n출처: file:///private/example"
    assert followup_telegram_message(body) == (body, None)


def test_delivery_cli_sends_compact_body_with_original_link_buttons(
    store, tmp_path, monkeypatch, capsys
):
    from types import SimpleNamespace

    import run_investment_followup as cli

    import tokmo.programs.news_delivery_route as route

    c = choice(store)
    url = "https://example.invalid/original"
    review(
        store,
        c,
        ev=[
            {
                "id": "war",
                "text": "전쟁 장기화",
                "source": url,
                "available_at": NOW.isoformat(),
            }
        ],
    )
    sent = []

    def routed(root, fallback, transport):
        def send(body, reply_markup=None):
            sent.append((body, reply_markup))
            return "telegram-group:-123:501"

        return send

    class Clock:
        @staticmethod
        def now(tz):
            return NOW

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(route, "build_news_sender", routed)
    args = SimpleNamespace(
        state=tmp_path / "choices.sqlite3",
        command="deliver",
        send=True,
        data_root=tmp_path,
        delivery_until=None,
        output=None,
    )
    assert cli.execute(args, NOW) == 0
    assert url not in sent[0][0] and sent[0][1]["inline_keyboard"][0][0]["url"] == url
    assert json.loads(capsys.readouterr().out)["delivery"][0]["status"] == "SENT"


def test_no_change_verdict_overrides_conflicting_notify_flags_and_commentary(store):
    c = choice(store)
    raw = response()
    raw["change"] = {
        "kind": "UNCHANGED",
        "before": "기존 위험을 관찰한다.",
        "after": "기존 위험과 대응 판단에 변화가 없다.",
        "meaning": "출처만 추가됐으므로 내부 기록한다.",
        "sources": ["war"],
    }
    raw["comparison"] = "새로운 위험이 있으니 반드시 알린다."
    r = review(store, c, raw=raw)
    assert r["assessment"]["notify"] is False
    assert r["assessment"]["material_change"] is False
    assert r["assessment"]["comparison"] == raw["change"]["after"]
    assert not store.pending(at=NOW)


def test_real_change_does_not_depend_on_a_separate_notify_boolean(store):
    raw = response()
    raw.pop("notify")
    raw.pop("material_change")
    raw.pop("notification_reason")
    r = review(store, choice(store), raw=raw)
    assert r["assessment"]["notify"] and r["assessment"]["material_change"]
    assert r["assessment"]["notification_reason"] == raw["change"]["meaning"]


def test_change_fields_must_reference_supplied_evidence(store):
    raw = response()
    raw["change"]["sources"] = ["invented-source"]
    with pytest.raises(ValueError, match="실제 제공한 근거"):
        review(store, choice(store), raw=raw)


def test_multiple_ready_episodes_get_one_choice_review_in_the_same_tick(
    store, tmp_path
):
    baseline(store)
    path = tmp_path / "news.db"
    seen = []
    for eid in ("source-one", "source-two", "source-three"):
        news(path, eid, NOW)
    r = run_tick(
        store,
        news_path=path,
        snapshot=None,
        at=NOW,
        model=analyst(seen),
        screen_model=router("REVIEW"),
    )
    assert len(r["reviewed"]) == 1 and len(seen) == 1
    assert {e["id"] for e in seen[0]["evidence"]} == {
        "source-one",
        "source-two",
        "source-three",
    }


def test_a_silent_verdict_moves_the_screens_baseline_to_the_facts_it_judged(store):
    from tokmo.programs.investment_followup_worker import MATCH_INSTRUCTION

    c = baseline(store)  # a context-only review over evidence 'war', never delivered
    current = choice_context(store, c, NOW)["current_assessment"]
    assert current["judged_evidence_ids"] == ["war"] and current["delivered_at"] is None
    # The matcher is told to anchor on that judgment, not on what was last delivered.
    assert (
        "judged_evidence_ids" in MATCH_INSTRUCTION
        and "마지막 전달 상태부터" not in MATCH_INSTRUCTION
    )


def test_the_screen_must_state_every_verdict_explicitly(store, tmp_path):
    from tokmo.programs.investment_followup_worker import validate_matches

    with pytest.raises(ValueError, match="assessment"):
        validate_matches(
            {
                "matches": [{"choice_id": "c1", "event_ids": ["e1"], "reason": "근거"}],
                "unmatched_reason": "없음",
            },
            {"c1"},
            {"e1"},
        )
    # A router that forgets the verdict is a failed match, never a silent review.
    c = baseline(store)
    path = tmp_path / "news.db"
    seen = []
    news(path, "one", NOW)

    def forgetful(prompt):
        try:
            packet = json.loads(prompt.rsplit("\n", 1)[-1])
            ids = [e["id"] for e in packet["events"]]
        except (ValueError, KeyError, TypeError):
            ids = ["one"]
        return {
            "matches": [{"choice_id": c["id"], "event_ids": ids, "reason": "근거"}],
            "unmatched_reason": "없음",
        }

    result = run_tick(
        store,
        news_path=path,
        snapshot=None,
        model=analyst(seen),
        screen_model=forgetful,
        at=NOW,
    )
    assert (
        not result["reviewed"]
        and not seen
        and any(e.get("stage") == "match" for e in result["errors"])
    )


def test_a_completed_review_settles_the_carried_over_demand(store):
    from tokmo.programs.followup_bundling import acknowledge_bundle, bundle_matches

    c = baseline(store)
    key = "followup_bundle:" + c["id"]
    store.set_watermark(
        key,
        {
            "mode": "choice-evidence-state",
            "first_at": NOW.isoformat(),
            "reasons": ["r"],
            "review_required": True,
            "events": {"a": {"id": "a"}, "b": {"id": "b"}},
        },
    )
    acknowledge_bundle(store, {"choice_id": c["id"], "event_ids": ["a"]})
    left = store.watermark(key)
    assert list(left["events"]) == ["b"] and left["review_required"] is False
    # With the demand settled, a mere RECORD on the leftover fact does not re-run the review.
    tasks, _, waiting = bundle_matches(
        store,
        [c],
        [
            {
                "choice_id": c["id"],
                "event_ids": ["b"],
                "reason": "배경",
                "assessment": "RECORD",
            }
        ],
        [{"id": "b", "updated_at": NOW.isoformat()}],
        at=NOW + timedelta(minutes=5),
        attempt="1",
    )
    assert tasks == [] and waiting == [c["id"]]


def test_screen_context_carries_only_the_most_recent_unjudged_facts(store):
    from tokmo.programs.followup_bundling import MAX_SCREEN_EVENTS

    c = baseline(store)
    key = "followup_bundle:" + c["id"]
    events = {
        f"e{i}": {
            "id": f"e{i}",
            "updated_at": (NOW + timedelta(minutes=i)).isoformat(),
            "decision": {"evidence_ids": []},
            "evidence": [
                {
                    "id": f"e{i}",
                    "title": "t",
                    "text": "x" * 900,
                    "observed_at": NOW.isoformat(),
                }
            ],
        }
        for i in range(MAX_SCREEN_EVENTS + 2)
    }
    store.set_watermark(
        key,
        {
            "mode": "choice-evidence-state",
            "first_at": NOW.isoformat(),
            "reasons": [],
            "events": events,
        },
    )
    acc = choice_context(store, c, NOW + timedelta(hours=1))["accumulating"]
    assert len(acc["facts"]) == MAX_SCREEN_EVENTS and acc["omitted_events"] == 2
    assert (
        acc["facts"][-1]["event_id"] == f"e{MAX_SCREEN_EVENTS + 1}"
        and len(acc["facts"][0]["facts"][0]["text"]) == 400
    )
    assert (
        len(acc["event_ids"]) == MAX_SCREEN_EVENTS + 2
    )  # the bundle itself still holds every fact


def test_review_evidence_keeps_cited_and_recent_rows_within_bounds():
    from tokmo.programs.investment_followup_worker import event_evidence

    rows = [
        {
            "id": f"r{i}",
            "title": "t",
            "text": "y" * 3000,
            "observed_at": NOW.isoformat(),
        }
        for i in range(9)
    ]
    out = event_evidence(
        [{"id": "ev", "decision": {"evidence_ids": ["r0"]}, "evidence": rows}],
        at=NOW + timedelta(minutes=1),
    )
    assert [e["id"] for e in out] == ["r0", "r4", "r5", "r6", "r7", "r8"]
    assert all(len(e["text"]) == 2500 and e["text_truncated"] for e in out)


def test_first_pass_settles_context_only_reviews_without_the_primary_model(
    store, tmp_path
):
    c = baseline(store)
    path = tmp_path / "news.db"
    cheap = []
    primary = []
    news(path, "one", NOW)

    def quiet(prompt):
        # A genuinely context-only verdict: nothing to notify, judgment maintained.
        cheap.append(prompt)
        return response(
            notify=False,
            material_change=False,
            pending_action="KEEP",
            new_facts=[],
            effect="MAINTAIN",
            business="SUPPORTED",
            market_risk="WITHIN_EXPECTED",
        )

    result = run_tick(
        store,
        news_path=path,
        snapshot=None,
        model=analyst(primary),
        first_pass_model=quiet,
        screen_model=router("REVIEW"),
        at=NOW,
    )
    assert (
        len(result["reviewed"]) == 1
        and result["reviewed"][0]["review_tier"] == "first_pass"
    )
    assert len(cheap) == 1 and primary == []  # the expensive model never saw this one
    saved = store.latest_review(c["id"], at=NOW)
    assert (
        saved["review_tier"] == "first_pass"
        and saved["first_pass"] is None
        and saved["assessment"]["notify"] is False
    )


def test_first_pass_material_verdict_is_confirmed_by_the_primary_model(store, tmp_path):
    c = baseline(store)
    path = tmp_path / "news.db"
    cheap = []
    primary = []
    news(path, "one", NOW)

    def alarmed(prompt):
        cheap.append(prompt)
        return response(
            notify=True,
            material_change=True,
            pending_action="REPLACE",
            new_facts=[dict(text="운송 차질이 확인됐다.", sources=["one"])],
        )

    def calm(prompt):
        primary.append(prompt)
        return response(
            notify=False, material_change=False, pending_action="KEEP", new_facts=[]
        )

    result = run_tick(
        store,
        news_path=path,
        snapshot=None,
        model=calm,
        first_pass_model=alarmed,
        screen_model=router("REVIEW"),
        at=NOW,
    )
    assert (
        len(cheap) == 1
        and len(primary) == 1
        and result["reviewed"][0]["review_tier"] == "escalated"
    )
    saved = store.latest_review(c["id"], at=NOW)
    # The primary's verdict is the review of record; the first pass is kept only as an audit note.
    assert (
        saved["assessment"]["notify"] is False
        and saved["first_pass"]["change"] == "MATERIAL"
        and saved["first_pass"]["notify"] is True
    )
    assert (
        store.connection.execute(
            "select count(*) from investment_reviews where choice_id=?", (c["id"],)
        ).fetchone()[0]
        == 2
    )  # baseline + this one


def test_news_driven_review_carries_no_quant_packet_even_with_a_snapshot(
    store, tmp_path
):
    from test_investment_followup import BASE, parse_snapshot

    snapshot = parse_snapshot(json.loads(BASE.read_text()))
    c = baseline(store)
    path = tmp_path / "news.db"
    seen = []
    news(path, "one", NOW)
    run_tick(
        store,
        news_path=path,
        snapshot=snapshot,
        model=analyst(seen),
        screen_model=router("REVIEW"),
        at=NOW,
    )
    assert (
        len(seen) == 1 and seen[0]["quant"] == {}
    )  # evidence only; no account figures
    saved = store.latest_review(c["id"], at=NOW)
    assert saved["input"]["quant"] == {} and saved["assessment"]["analysis"] is None
