"""Regression for 09-08: empty non-match and repeated scheduler failure notices."""

import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta

import pytest
import run_investment_followup_cron as cron
from test_investment_followup import (
    NOW,
    choice,
    response,
    review,
    store,  # pytest fixture
)

from tokmo.programs.investment_followup_worker import run_tick, validate_matches


def test_empty_nonmatch_preserves_valid_review_and_consumes_batch(store, tmp_path):
    a = choice(store)
    b = choice(store, "other")
    review(store, a)
    review(store, b)  # Neither choice is due for a periodic review.
    event = {
        "id": "news-a",
        "updated_at": NOW.isoformat(),
        "decision": {"title": "supply update", "evidence_ids": ["source-a"]},
        "evidence": [
            {
                "id": "source-a",
                "observed_at": NOW.isoformat(),
                "title": "供給",
                "text": "공급 변화",
            }
        ],
    }
    db = tmp_path / "news.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE news_decision_events(state TEXT)")
        conn.execute("INSERT INTO news_decision_events VALUES(?)", (json.dumps(event),))
    screens = []
    reviews = []

    def match(prompt):
        screens.append(prompt)
        return {
            "matches": [
                {
                    "choice_id": a["id"],
                    "event_ids": ["news-a"],
                    "reason": "공급 전망 점검",
                    "assessment": "REVIEW",
                },
                {"choice_id": b["id"], "event_ids": [], "reason": None},
            ],
            "unmatched_reason": "두 번째 선택에 관련된 사건은 없음",
        }

    def analyze(prompt):
        reviews.append(prompt)
        return response(
            notify=False, effect="MAINTAIN", pending_action="KEEP", new_facts=[]
        )

    args = dict(news_path=db, snapshot=None, model=analyze, screen_model=match)
    result = run_tick(store, at=NOW + timedelta(minutes=1), **args)
    assert not result["errors"] and len(result["reviewed"]) == 1
    assert result["reviewed"][0]["choice_id"] == a["id"]
    assert store.watermark("news_versions").get("news-a")
    run_tick(store, at=NOW + timedelta(minutes=6), **args)
    assert len(screens) == len(reviews) == 1
    assert not store.watermark("blocked_match_events")


@pytest.mark.parametrize("ids", [["missing"], None, "event", [None]])
def test_nonmatch_normalization_does_not_accept_bad_references(ids):
    with pytest.raises(ValueError, match="사건 id 오류"):
        validate_matches(
            {
                "matches": [{"choice_id": "a", "event_ids": ids, "reason": "x"}],
                "unmatched_reason": "없음",
            },
            {"a"},
            {"event"},
        )


def test_unknown_choice_is_invalid_even_with_empty_events():
    with pytest.raises(ValueError, match="선택 id"):
        validate_matches(
            {
                "matches": [{"choice_id": "missing", "event_ids": []}],
                "unmatched_reason": "없음",
            },
            {"a"},
            {"event"},
        )


def test_cron_retains_failure_silently_at_night_then_notifies_once_at_eight(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "investment_followup.json").write_text(json.dumps({"enabled": True}))
    now = datetime.fromisoformat("2026-09-08T22:00:00+09:00")

    class Clock:
        @staticmethod
        def now(tz):
            return now.astimezone(tz)

    monkeypatch.setattr(cron, "datetime", Clock)
    errors = [
        {
            "stage": "match",
            "error": "RETRY_EXHAUSTED_OR_INVALID",
            "event_ids": ["a", "b"],
        }
    ]

    def run(_):
        print(
            json.dumps(
                {"errors": deepcopy(errors), "reviewed": [{"id": "other-success"}]}
            )
        )
        return 2 if errors else 0

    monkeypatch.setattr(cron, "run", run)
    args = ["--root", str(tmp_path)]
    health_path = tmp_path / "ops/investment-followup/health.json"
    for _ in range(15):
        assert cron.main(args) == 0
        assert capsys.readouterr().out == ""
        now += timedelta(minutes=5)
    health = json.loads(health_path.read_text())
    assert health["status"] == "DEGRADED" and health["worker_exit_code"] == 2
    assert health["errors"] == errors and health["reviews"] == 1
    assert health["notice_suppressed"] == "quiet_hours"
    now = datetime.fromisoformat("2026-09-09T07:59:00+09:00")
    assert cron.main(args) == 0 and capsys.readouterr().out == ""
    now += timedelta(minutes=1)
    assert (
        cron.main(args) == 0 and "뉴스와 투자 선택 연결 보류" in capsys.readouterr().out
    )
    errors[0]["event_ids"] = ["b"]
    errors[0]["failed_at"] = now.isoformat()
    assert cron.main(args) == 0 and capsys.readouterr().out == ""
    errors.clear()
    assert cron.main(args) == 0 and capsys.readouterr().out == ""
    now += timedelta(minutes=10)
    assert cron.main(args) == 0 and "복구 확인" in capsys.readouterr().out
    assert cron.main(args) == 0 and capsys.readouterr().out == ""
    assert json.loads(health_path.read_text())["status"] == "OK"


def test_caught_worker_exception_stays_visible_in_health(tmp_path, monkeypatch, capsys):
    (tmp_path / "investment_followup.json").write_text(json.dumps({"enabled": True}))

    def broken(_):
        raise RuntimeError("do not send raw provider detail")

    monkeypatch.setattr(cron, "run", broken)
    assert cron.main(["--root", str(tmp_path)]) == 0
    health = json.loads((tmp_path / "ops/investment-followup/health.json").read_text())
    assert health["status"] == "DEGRADED"
    assert health["errors"] == [{"stage": "worker", "error": "RuntimeError"}]
    assert "provider detail" not in capsys.readouterr().out


def test_valid_duplicate_choice_rows_are_combined_without_dropping_review_evidence():
    raw = {
        "matches": [
            {"choice_id": "c", "event_ids": [], "reason": None},
            {
                "choice_id": "c",
                "event_ids": ["a"],
                "reason": "배경 누적",
                "assessment": "RECORD",
            },
            {
                "choice_id": "c",
                "event_ids": ["b", "a"],
                "reason": "보유 이유 재검토",
                "assessment": "REVIEW",
            },
        ],
        "unmatched_reason": "그 외 없음",
    }
    matches = validate_matches(raw, {"c"}, {"a", "b"})
    assert len(matches) == 1 and matches[0]["event_ids"] == ["a", "b"]
    assert matches[0]["assessment"] == "REVIEW"
    assert (
        "배경 누적" in matches[0]["reason"]
        and "보유 이유 재검토" in matches[0]["reason"]
    )
    raw["matches"][-1]["event_ids"].append("not_supplied")
    with pytest.raises(ValueError, match="사건 id 오류"):
        validate_matches(raw, {"c"}, {"a", "b"})


def test_match_context_keeps_historical_facts_but_not_selectable_event_handles(store):
    from tokmo.programs.followup_bundling import match_choice_context

    c = choice(store)
    event = {
        "id": "old-event",
        "updated_at": NOW.isoformat(),
        "decision": {"title": "과거 공급 관측", "evidence_ids": ["source-old"]},
        "evidence": [
            {
                "id": "source-old",
                "observed_at": NOW.isoformat(),
                "text": "조건부 공급 전망",
            }
        ],
    }
    key = "followup_bundle:" + c["id"]
    store.set_watermark(key, {"events": {"old-event": event}, "reasons": ["누적 위험"]})
    context = match_choice_context(store, c, NOW)
    assert context["role"] == "COMPARISON_ONLY"
    assert "event_ids" not in context["accumulating"]
    fact = context["accumulating"]["facts"][0]
    assert "event_id" not in fact and fact["facts"][0]["text"] == "조건부 공급 전망"
    assert (
        fact["facts"][0]["id"] == "source-old"
        and fact["facts"][0]["available_at"] == NOW.isoformat()
    )
    assert store.watermark(key)["events"]["old-event"] == event


def test_health_does_not_repeat_as_same_unresolved_errors_appear_and_disappear():
    now = datetime.fromisoformat("2026-09-13T21:00:00+09:00")
    match = {
        "stage": "match",
        "error": "RETRY_EXHAUSTED_OR_INVALID",
        "event_ids": ["a"],
    }
    delivery = {
        "stage": "delivery",
        "error": "RECEIPT_UNCONFIRMED",
        "deliveries": [{"id": "d", "status": "UNKNOWN"}],
    }

    def tick(previous, errors):
        health = {"status": "DEGRADED" if errors else "OK", "errors": errors}
        return health, cron.health_notice(health, previous, {}, now)

    h, message = tick({}, [match, delivery])
    assert (
        "뉴스와 투자 선택 연결 보류 1건" in message
        and "알림 발송 확인 필요 1건" in message
    )
    h, message = tick(
        h,
        [match, delivery, {"stage": "match", "error": "ValueError", "choice_id": "c"}],
    )
    assert message is None
    h, message = tick(h, [match])
    assert message is None
    h, message = tick(h, [match, delivery])
    assert message is None
    snapshot = {"stage": "snapshot", "error": "ValueError"}
    h, message = tick(h, [match, delivery, snapshot])
    assert "입력자료" in message
    h, message = tick(h, [match])
    assert message is None
    h, message = tick(h, [match, snapshot])
    assert message is None
    h, message = tick(h, [])
    assert message is None
    now += timedelta(minutes=10)
    h, message = tick(h, [])
    assert "복구 확인" in message
    h, message = tick(h, [match])
    assert message is not None  # A new incident after recovery.


def test_legacy_notification_is_migrated_without_resending_and_auth_failure_is_new():
    now = datetime.fromisoformat("2026-09-13T21:00:00+09:00")
    error = {"stage": "match", "error": "RETRY_EXHAUSTED_OR_INVALID", "choice_id": None}
    previous = {
        "notification": {
            "status": "DEGRADED",
            "signature": json.dumps(["DEGRADED", [json.dumps(error)]]),
        }
    }
    health = {
        "status": "DEGRADED",
        "errors": [{"stage": "match", "error": "ValueError"}],
    }
    assert cron.health_notice(health, previous, {}, now) is None
    health = {"status": "DEGRADED", "errors": [dict(error, http_status=401)]}
    assert "인증" in cron.health_notice(health, previous, {}, now)


def test_opening_quote_wait_resolves_without_problem_or_recovery_notice():
    now = datetime.fromisoformat("2026-09-14T09:00:00+09:00")
    h = {
        "status": "DEGRADED",
        "errors": [
            {
                "stage": "price_checkpoint",
                "choice_id": "c",
                "error": "QUOTE_OUTSIDE_SESSION",
            }
        ],
    }
    assert cron.health_notice(h, {}, {}, now) is None
    good = {"status": "OK", "errors": []}
    assert cron.health_notice(good, h, {}, now + timedelta(minutes=5)) is None
    assert good["monitor"] == {}


def test_real_quote_gap_and_unstable_recovery_keep_one_incident():
    now = datetime.fromisoformat("2026-09-14T09:00:00+09:00")
    previous = {}

    def tick(minutes, failed):
        nonlocal previous
        h = {
            "status": "DEGRADED" if failed else "OK",
            "errors": [
                {
                    "stage": "price_checkpoint",
                    "choice_id": "c",
                    "subject": "ETF 가격 감시",
                    "error": "QUOTE_STALE",
                }
            ]
            if failed
            else [],
        }
        msg = cron.health_notice(h, previous, {}, now + timedelta(minutes=minutes))
        previous = h
        return msg

    assert tick(0, True) is None
    message = tick(10, True)
    assert (
        "ETF 가격 감시" in message
        and "원인:" in message
        and "가격 조건 도달 판단" in message
    )
    assert tick(15, False) is None
    assert tick(20, True) is None
    assert tick(25, False) is None
    assert tick(30, False) is None
    assert "복구 확인" in tick(35, False)
    assert tick(40, False) is None


def test_plan_placeholder_and_validation_are_one_specific_actionable_notice():
    now = datetime.fromisoformat("2026-09-14T09:00:00+09:00")
    errors = [
        {
            "stage": "plan",
            "choice_id": "c",
            "error": "REVIEW_PENDING_RETRY",
            "subject": "SOL 예약 분석",
        },
        {
            "choice_id": "c",
            "error": "AnalysisValidationError",
            "cause": "number_validation",
            "value": "17,200원",
            "subject": "SOL 예약 분석",
        },
    ]
    h = {"status": "DEGRADED", "errors": errors}
    msg = cron.health_notice(h, {}, {}, now)
    assert (
        msg.count("SOL 예약 분석") == 1
        and "17,200원" in msg
        and "영향:" in msg
        and "조치:" in msg
    )
    h2 = {"status": "DEGRADED", "errors": errors[:1]}
    assert cron.health_notice(h2, h, {}, now + timedelta(minutes=5)) is None
    h3 = {"status": "DEGRADED", "errors": errors}
    assert cron.health_notice(h3, h2, {}, now + timedelta(minutes=10)) is None


def test_system_bot_route_is_silent_to_hermes_and_records_confirmed_receipt(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "investment_followup.json").write_text(
        json.dumps({"enabled": True, "health_destination": "SYSTEM_ALERT"})
    )
    now = datetime.fromisoformat("2026-09-14T10:00:00+09:00")

    class Clock:
        @staticmethod
        def now(tz):
            return now.astimezone(tz)

    monkeypatch.setattr(cron, "datetime", Clock)

    def run(_):
        print(
            json.dumps(
                {
                    "errors": [
                        {
                            "stage": "plan",
                            "choice_id": "c",
                            "error": "AnalysisValidationError",
                        }
                    ]
                }
            )
        )
        return 2

    monkeypatch.setattr(cron, "run", run)
    sent = []
    monkeypatch.setattr(
        cron, "send_health_notice", lambda body: sent.append(body) or "812"
    )
    assert cron.main(["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "" and len(sent) == 1
    h = json.loads((tmp_path / "ops/investment-followup/health.json").read_text())
    assert (
        h["health_delivery"]["receipt"] == "812"
        and h["health_delivery"]["destination"] == "tokmoALERT_BOT"
    )
    assert cron.main(["--root", str(tmp_path)]) == 0 and len(sent) == 1


def test_failed_system_send_is_not_acknowledged_and_retries_are_bounded():
    now = datetime.fromisoformat("2026-09-14T10:00:00+09:00")
    previous = {}
    calls = []

    def fail(body):
        calls.append(body)
        raise TimeoutError("do not expose credentials")

    for minute in (0, 5, 10, 20, 30, 40):
        at = now + timedelta(minutes=minute)
        h = {
            "status": "DEGRADED",
            "errors": [{"stage": "worker", "error": "RuntimeError"}],
        }
        message = cron.health_notice(h, previous, {}, at)
        assert message
        cron.deliver_health(h, previous, message, at, fail)
        assert h["notification"] == {} and h["health_delivery"]["status"] == "FAILED"
        assert "credentials" not in json.dumps(h)
        previous = h
    assert len(calls) == 3


def test_model_execution_diagnostics_only_keep_known_safe_details():
    from tokmo.programs.followup_health import error_info, issue_text

    details = error_info(RuntimeError("subscription CLI failed (exit 1)"))
    assert details == {"cause": "model_execution", "exit_code": 1}
    assert "모델 실행기" in issue_text(dict(error="RuntimeError", **details))[1]
    assert error_info(RuntimeError("private provider request token=secret")) == {}
