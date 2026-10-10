"""Real validator/retry flows with temporary stores; no live transport."""

import json
import stat
from datetime import timedelta

import pytest
import run_investment_followup_cron as cron
import subscription_json_model as adapter
from test_investment_plans import (
    NOW,
    register,
    run,
    store,  # pytest fixture
    unchanged,
)
from test_quant_decisions import response, sample_input

from tokmo.programs.followup_health import enrich_errors, error_info
from tokmo.programs.quant_decision_analysis import make_packet, validate_analysis
from tokmo.values.quant_decisions import parse_snapshot


@pytest.mark.parametrize("written", ["173만9,500원", "173만 9천500원", "1,739,500원"])
def test_full_mixed_korean_amount_uses_the_supplied_price(written):
    data = sample_input()
    data["instruments"][0]["price"] = "1739500"
    snapshot = parse_snapshot(data)
    packet = make_packet(snapshot, snapshot.instruments[0], "personal-a", "personal")
    raw = response(packet)
    raw["summary"] = "현재가는 " + written + "이야."
    assert validate_analysis(raw, packet)["summary"] == raw["summary"]


@pytest.mark.parametrize(
    "written", ["173만9,501원", "173만9,500달러", "173만9,500주", "9,500원"]
)
def test_mixed_amount_does_not_authorize_wrong_price_unit_or_suffix(written):
    data = sample_input()
    data["instruments"][0]["price"] = "1739500"
    snapshot = parse_snapshot(data)
    packet = make_packet(snapshot, snapshot.instruments[0], "personal-a", "personal")
    raw = response(packet)
    raw["summary"] = "현재가는 " + written + "이야."
    with pytest.raises(ValueError, match="unsupported monetary/share claim"):
        validate_analysis(raw, packet)


@pytest.mark.parametrize(
    "evidence,written",
    [
        ("1억2,000만원", "120,000,000원"),
        ("1억2천만원", "120,000,000원"),
        ("2조3,000억원", "2,300,000,000,000원"),
    ],
)
def test_compound_source_amount_preserves_full_magnitude(evidence, written):
    data = sample_input()
    data["instruments"][0]["evidence"][0]["text"] = "확정 계약 " + evidence
    snapshot = parse_snapshot(data)
    packet = make_packet(snapshot, snapshot.instruments[0], "personal-a", "personal")
    raw = response(packet)
    raw["summary"] = "확정 계약은 " + written + "이야."
    assert validate_analysis(raw, packet)


@pytest.mark.parametrize(
    "detail,category,retryable",
    [
        ("stream disconnected before completion", "TRANSIENT", True),
        ("usage_limit_reached", "QUOTA", False),
        ("invalid_api_key", "AUTH", False),
        ("context_length_exceeded", "REQUEST", False),
        ("unexpected exit", "EXECUTOR_UNKNOWN", True),
    ],
)
def test_cli_failure_keeps_private_diagnostics_and_safe_public_reason(
    tmp_path, monkeypatch, detail, category, retryable
):
    secret = "private-request-do-not-publish"

    class Process:
        returncode = 1

        def communicate(self, *args, **kw):
            return json.dumps(
                {"type": "turn.failed", "error": {"message": detail + " " + secret}}
            ), secret

    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *a, **kw: Process())
    path = tmp_path / "failure.json"
    with pytest.raises(adapter.SubscriptionInferenceError) as raised:
        adapter._run(
            ["codex"], "private prompt", str(tmp_path), 1, diagnostic_path=path
        )
    error = raised.value
    assert error.category == category and error.inference_retryable == retryable
    assert secret not in str(error) and secret not in json.dumps(error_info(error))
    assert secret in path.read_text() and stat.S_IMODE(path.stat().st_mode) == 0o600


def test_unknown_cli_exit_retries_once_and_completed_reservation_never_repeats(
    store, tmp_path
):
    c = register(store, tmp_path)["choice"]
    at = NOW + timedelta(minutes=5)
    calls = []

    def model(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            raise adapter.SubscriptionInferenceError("EXECUTOR_UNKNOWN", exit_code=1)
        return unchanged(prompt)

    first = run(store, tmp_path, at=at, model=model)
    assert first["errors"][0]["retry_at"]
    assert (
        run(store, tmp_path, at=at + timedelta(seconds=59), model=model)["errors"]
        and len(calls) == 1
    )
    assert (
        run(store, tmp_path, at=at + timedelta(minutes=5), model=model)["reviewed"]
        and len(calls) == 2
    )
    assert not run(store, tmp_path, at=at + timedelta(minutes=10), model=model)[
        "reviewed"
    ]
    assert len(calls) == 2 and store.watermark("plan:" + c["id"])["done"] == ["TIME"]
    journal = store.connection.execute(
        "select id,status from investment_calls"
    ).fetchall()
    assert len(journal) == 2 and {r[1] for r in journal} == {"ERROR", "OK"}


@pytest.mark.parametrize(
    "category,limit",
    [
        ("EXECUTOR_UNKNOWN", 2),
        ("TRANSIENT", 3),
        ("AUTH", 1),
        ("QUOTA", 1),
        ("REQUEST", 1),
    ],
)
def test_repeated_failure_is_bounded_across_scheduler_ticks(
    store, tmp_path, category, limit
):
    register(store, tmp_path)
    at = NOW + timedelta(minutes=5)
    calls = []

    def model(prompt):
        calls.append(prompt)
        raise adapter.SubscriptionInferenceError(category, exit_code=1)

    for minutes in (0, 5, 10, 15, 30, 60):
        run(store, tmp_path, at=at + timedelta(minutes=minutes), model=model)
    assert len(calls) == limit
    assert (
        store.connection.execute("select count(*) from investment_calls").fetchone()[0]
        == limit
    )


def test_later_followup_failure_does_not_label_finished_reservation_as_failed(
    store, tmp_path
):
    c = register(store, tmp_path)["choice"]
    at = NOW + timedelta(minutes=5)
    assert run(store, tmp_path, at=at)["reviewed"]
    issue = dict(
        choice_id=c["id"],
        error="RuntimeError",
        cause="model_execution",
        exit_code=1,
        review_kind="NEWS_FOLLOWUP",
        failed_at=(at + timedelta(hours=2)).isoformat(),
    )
    errors = enrich_errors(tmp_path, [issue])
    assert "scheduled_at" not in errors[0] and errors[0]["previous_schedule_completed"]
    notice = cron.health_notice(
        dict(status="DEGRADED", errors=errors), {}, {}, at + timedelta(hours=2)
    )
    assert (
        "새 뉴스에 따른 후속 분석" in notice and "예약 분석은 이미 완료됐어" in notice
    )
    assert "예약 시각:" not in notice and "분석 시작:" in notice


def test_unfinished_plan_still_shows_its_reservation(store, tmp_path):
    c = register(store, tmp_path)["choice"]
    error = enrich_errors(
        tmp_path, [dict(stage="plan", choice_id=c["id"], error="REVIEW_PENDING_RETRY")]
    )[0]
    assert (
        error["scheduled_at"] == c["plan"]["review_at"]
        and error["review_kind"] == "SCHEDULED"
    )
