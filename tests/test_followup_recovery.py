"""Recovery contracts: real local stores, fake transports/models, no live effects."""

import json
import sqlite3
from copy import deepcopy
from datetime import timedelta
from urllib.error import HTTPError

import pytest
from test_investment_followup import (
    NOW,
    choice,
    interpretation,
    response,
    review,
    store,  # pytest fixture
)

from tokmo.programs.investment_followup import deliver_pending, record_text
from tokmo.programs.investment_followup_worker import run_tick
from tokmo.programs.news_delivery_route import NewsRouteError
from tokmo.records.investment_followup import InvestmentFollowupStore


def unavailable(*args):
    raise NewsRouteError("missing route")


def test_route_failure_can_be_requeued_without_claiming_it_was_delivered(store):
    r = review(store, choice(store))
    assert deliver_pending(store, at=NOW, sender=unavailable)[0]["status"] == "FAILED"
    assert not store.uncertain_deliveries()
    store.resolve_delivery(r["id"], action="requeue", reason="route restored", at=NOW)
    assert (
        deliver_pending(store, at=NOW, sender=lambda _: "telegram-group:-123:4")[0][
            "status"
        ]
        == "SENT"
    )
    assert (
        store.watermark("delivery:" + r["id"])["resolutions"][0]["previous_state"]
        == "FAILED"
    )


def test_uncertain_transport_requires_investigation_but_is_resolvable(store):
    r = review(store, choice(store))

    def broken(_):
        raise ValueError("response JSON after request")

    assert deliver_pending(store, at=NOW, sender=broken)[0]["status"] == "UNKNOWN"
    with pytest.raises(ValueError):
        store.resolve_delivery(r["id"], action="requeue", reason="guess", at=NOW)
    assert not deliver_pending(
        store, at=NOW + timedelta(hours=1), sender=lambda _: pytest.fail("blind retry")
    )
    store.resolve_delivery(
        r["id"],
        action="sent",
        reason="found Telegram receipt",
        at=NOW,
        receipt="telegram-group:-123:5",
        delivered_at=NOW.isoformat(),
    )
    assert not store.delivery_issues()


def test_rate_limit_obeys_server_delay_survives_restart_and_stops_at_three(tmp_path):
    path = tmp_path / "s.db"
    s = InvestmentFollowupStore(path)
    r = review(s, choice(s))
    calls = []

    def limited(_):
        calls.append(1)
        raise HTTPError(
            "https://example.invalid", 429, "limited", {"Retry-After": "120"}, None
        )

    assert deliver_pending(s, at=NOW, sender=limited)[0]["status"] == "RETRY"
    s.close()
    s = InvestmentFollowupStore(path)
    assert not deliver_pending(s, at=NOW + timedelta(seconds=119), sender=limited)
    deliver_pending(s, at=NOW + timedelta(seconds=120), sender=limited)
    assert not deliver_pending(s, at=NOW + timedelta(seconds=419), sender=limited)
    assert (
        deliver_pending(s, at=NOW + timedelta(seconds=420), sender=limited)[0]["status"]
        == "FAILED"
    )
    assert not deliver_pending(s, at=NOW + timedelta(days=1), sender=limited)
    assert len(calls) == 3 and s.delivery_issues()[0]["tries"] == 3
    s.close()


def test_match_failure_does_not_block_periodic_review_or_lose_retried_news(
    store, tmp_path
):
    c = choice(store)
    db = tmp_path / "news.db"
    conn = sqlite3.connect(db)
    conn.execute("create table news_decision_events(state text)")
    event = {
        "id": "e",
        "updated_at": NOW.isoformat(),
        "decision": {"title": "war", "evidence_ids": ["war"]},
        "evidence": [
            {
                "id": "war",
                "observed_at": NOW.isoformat(),
                "title": "war",
                "text": "운송 차질",
            }
        ],
    }
    conn.execute("insert into news_decision_events values(?)", (json.dumps(event),))
    conn.commit()
    conn.close()
    screens = []
    reviews = []

    def matcher(_):
        screens.append(1)
        if len(screens) == 1:
            raise TimeoutError()
        return {
            "matches": [
                {
                    "choice_id": c["id"],
                    "event_ids": ["e"],
                    "reason": "시장 위험",
                    "assessment": "REVIEW",
                }
            ],
            "unmatched_reason": "없음",
        }

    def analyze(prompt):
        packet = json.loads(prompt.split("\nINPUT\n")[-1])
        reviews.append(packet)
        return response(
            effect="MAINTAIN", notify=False, pending_action="KEEP", new_facts=[]
        )

    args = dict(news_path=db, snapshot=None, model=analyze, screen_model=matcher)
    first = run_tick(store, at=NOW, **args)
    assert len(first["reviewed"]) == 1 and first["errors"][0]["stage"] == "match"
    assert store.watermark("news_versions") == {}
    run_tick(store, at=NOW + timedelta(seconds=59), **args)
    assert len(screens) == 1
    recovered = run_tick(store, at=NOW + timedelta(seconds=60), **args)
    assert len(recovered["reviewed"]) == 1 and not recovered["errors"]
    assert store.watermark("news_versions").get("e") and len(screens) == 2


def test_review_retry_has_cooldown_and_other_choice_continues(store, tmp_path):
    a = choice(store)
    b = choice(store, "other")
    calls = {a["id"]: 0, b["id"]: 0}

    def analyst(prompt):
        packet = json.loads(prompt.split("\nINPUT\n")[-1])
        key = packet["choice"]["id"]
        calls[key] += 1
        if key == a["id"] and calls[key] == 1:
            raise TimeoutError()
        return response(
            effect="MAINTAIN", notify=False, pending_action="KEEP", new_facts=[]
        )

    args = dict(news_path=tmp_path / "absent", snapshot=None, model=analyst)
    run_tick(store, at=NOW, **args)
    run_tick(store, at=NOW + timedelta(seconds=59), **args)
    assert calls == {a["id"]: 1, b["id"]: 1}
    result = run_tick(store, at=NOW + timedelta(seconds=60), **args)
    assert calls[a["id"]] == 2 and result["reviewed"][0]["choice_id"] == a["id"]
    assert store.watermark("failed_review:" + a["id"]) is None


CATALOG = {
    "accounts": [{"account": "personal-a", "scope": "personal"}],
    "instruments": {"KR:000001": "가상"},
}


def test_duplicate_changed_action_is_not_silently_swallowed_and_can_be_reclassified(
    store,
):
    old = choice(store)
    text = "장기 전망을 믿고 있지만 이제 일부 팔기로 했어"
    value = interpretation("TRIM")
    value["supersedes"] = old["id"]
    result = record_text(
        store,
        text=text,
        source_id="new",
        at=NOW,
        catalog=CATALOG,
        model=lambda _: {
            "duplicate_of": old["id"],
            "duplicate_reason": "same",
            "choice": deepcopy(value),
        },
    )
    assert result["choice"] is None and result["clarification"]
    assert (
        store.input_record("new")["raw_text"] == text
        and len(store.choices(at=NOW)) == 1
    )
    result = record_text(
        store,
        text=text,
        source_id="new",
        at=NOW,
        catalog=CATALOG,
        attempt="corrected",
        model=lambda _: {"choice": deepcopy(value)},
    )
    assert result["choice"]["interpretation"]["action"] == "TRIM"
    assert len(store.input_record("new")["history"]) == 2


def test_semantic_duplicate_keeps_original_receipt_without_new_choice(store):
    old = choice(store)
    text = "장기 전망을 믿고 계속 보유할래"
    result = record_text(
        store,
        text=text,
        source_id="repeat",
        at=NOW,
        catalog=CATALOG,
        model=lambda _: {
            "duplicate_of": old["id"],
            "duplicate_reason": "같은 보유 이유 유지",
            "choice": interpretation(),
        },
    )
    assert result["duplicate"] and len(store.choices(at=NOW)) == 1
    assert store.input_record("repeat")["raw_text"] == text


def test_unknown_instrument_returns_clarification_instead_of_traceback(store):
    value = interpretation()
    value["keys"] = ["KR:999999"]
    result = record_text(
        store,
        text="장기 전망을 믿고 보유",
        source_id="unknown",
        at=NOW,
        catalog=CATALOG,
        model=lambda _: {"choice": value},
    )
    assert result["choice"] is None and "내부 조회" in result["clarification"]


def test_known_rejection_does_not_make_later_uncertain_attempt_safe_to_resend(store):
    r = review(store, choice(store))

    def limited(_):
        raise HTTPError("https://example.invalid", 429, "limited", {}, None)

    deliver_pending(store, at=NOW, sender=limited)

    def lost(_):
        raise TimeoutError()

    deliver_pending(store, at=NOW + timedelta(seconds=60), sender=lost)
    with pytest.raises(ValueError, match="미전달"):
        store.resolve_delivery(
            r["id"],
            action="requeue",
            at=NOW + timedelta(seconds=61),
            reason="previous rejection",
        )


def test_failed_match_survives_outage_longer_than_news_scan_window(store, tmp_path):
    choice(store)
    db = tmp_path / "news.db"
    conn = sqlite3.connect(db)
    conn.execute("create table news_decision_events(state text)")
    event = {
        "id": "old",
        "updated_at": NOW.isoformat(),
        "decision": {"evidence_ids": ["war"]},
        "evidence": [],
    }
    conn.execute("insert into news_decision_events values(?)", (json.dumps(event),))
    conn.commit()
    conn.close()

    def unavailable(_):
        raise TimeoutError()

    def periodic(_):
        return response(
            effect="DATA_GAP", notify=False, pending_action="KEEP", new_facts=[]
        )

    run_tick(
        store,
        news_path=db,
        snapshot=None,
        at=NOW,
        model=periodic,
        screen_model=unavailable,
    )
    seen = []

    def match(prompt):
        seen.append(prompt)
        return {"matches": [], "unmatched_reason": "현재 시점 중요 변화 없음"}

    result = run_tick(
        store,
        news_path=db,
        snapshot=None,
        at=NOW + timedelta(days=3),
        model=periodic,
        screen_model=match,
    )
    assert len(seen) == 1 and "old" in seen[0] and not result["errors"]
    assert not store.watermark("match_backlog") and store.watermark(
        "news_versions"
    ).get("old")


def test_silent_replace_normalizes_without_pending_but_corrects_with_pending(store):
    c = choice(store)
    calls = []

    def silent(_):
        calls.append(1)
        return response(notify=False, pending_action="REPLACE")

    r = review(store, c, model=silent)
    assert r["assessment"]["pending_action"] == "KEEP" and len(calls) == 1
    review(store, c, at=NOW + timedelta(seconds=1), trigger="new")

    def corrected(prompt):
        calls.append(1)
        return response(
            notify=False,
            pending_action="CANCEL" if "형식/근거 오류" in prompt else "REPLACE",
        )

    r = review(
        store, c, at=NOW + timedelta(seconds=2), trigger="correction", model=corrected
    )
    assert r["assessment"]["pending_action"] == "CANCEL" and len(calls) == 3
    assert not store.pending(at=NOW + timedelta(seconds=2))
