"""Failure diagnostics never change the order decision or replay a closed day."""

import json
from pathlib import Path

import pytest
from test_rotation_analysis import DEADLINE, NOW, context, policy, report, snapshot
from test_rotation_quant_entry import request_snapshot

from tokmo.programs.rotation_analysis import (
    RotationAnalysisError,
    run_rotation_analysis,
)
from tokmo.programs.rotation_analysis_delivery import make_notifier
from tokmo.programs.rotation_quant_entry import ExistingQuantAnalyzer
from tokmo.records.rotation_analysis import RotationAnalysisStore


def failed_analyzer(tmp_path, errors, *, malformed=False, wrong_key=False, code=2):
    def launch(args, *, deadline):
        value = report()
        value["purpose"] = args[args.index("--purpose") + 1]
        value["results"][0].update(status="FAILED", analysis=None, errors=errors)
        if wrong_key:
            value["results"][0]["key"] = "KR:000660"
        out = Path(args[args.index("--output") + 1]) / (value["report_id"] + ".json")
        out.write_text("{broken" if malformed else json.dumps(value))
        return code

    return ExistingQuantAnalyzer(
        python="/unused", entry=tmp_path / "entry", root=tmp_path / "run", launch=launch
    )


@pytest.mark.parametrize(
    "upstream,expected",
    [
        (
            "model_call:QUANT_PRODUCT_SCOPE_NOT_APPROVED",
            "QUANT_PRODUCT_SCOPE_NOT_APPROVED",
        ),
        ("model_call:QUANT_ROUTE_OR_QUOTA_BLOCKED", "QUANT_ROUTE_OR_QUOTA_BLOCKED"),
        ("model_call:QUANT_CALL_BUDGET_EXHAUSTED", "QUANT_CALL_BUDGET_EXHAUSTED"),
        ("model_call:SubscriptionInferenceError", "quant_model_failed"),
        ("invalid_response:unknown source SECRET_TOKEN", "quant_validation_failed"),
        ("model_call:QUANT_SECRET_TOKEN", "quant_entry_failed"),
    ],
)
def test_nonzero_quant_preserves_only_closed_failure_codes(
    tmp_path, upstream, expected
):
    with pytest.raises(RotationAnalysisError, match="^" + expected + "$"):
        failed_analyzer(tmp_path, [upstream])(request_snapshot(), DEADLINE)


@pytest.mark.parametrize("options", [{"malformed": True}, {"wrong_key": True}])
def test_invalid_failure_report_is_not_used_as_diagnostic_authority(tmp_path, options):
    with pytest.raises(RotationAnalysisError, match="^quant_entry_failed$"):
        failed_analyzer(
            tmp_path, ["model_call:QUANT_PRODUCT_SCOPE_NOT_APPROVED"], **options
        )(request_snapshot(), DEADLINE)


def test_failure_survives_runner_and_closed_day_is_never_replayed(tmp_path):
    analyzer = failed_analyzer(
        tmp_path, ["model_call:QUANT_PRODUCT_SCOPE_NOT_APPROVED"]
    )
    store = RotationAnalysisStore(tmp_path / "decisions")
    sent = []
    notify = make_notifier(
        lambda text, reply_markup=None: (
            sent.append((text, reply_markup)) or "telegram-group:-99:42"
        )
    )
    result = run_rotation_analysis(
        policy=policy(),
        store=store,
        load_inputs=lambda: (snapshot(), context()),
        analyze=analyzer,
        clock=lambda: NOW,
        notify=notify,
    )
    assert result["status"] == "FAILED" and result["decision"] is None
    assert result["failure_code"] == "QUANT_PRODUCT_SCOPE_NOT_APPROVED"
    assert result["failure_reason"] == "QUANT_PRODUCT_SCOPE_NOT_APPROVED"
    text, buttons = sent[0]
    assert (
        text.count("원인:") == 1
        and "쿼터" in text
        and "수리" in text
        and "영향:" in text
        and "조치:" in text
    )
    assert "호출 전" in text and buttons is None
    assert "ValueError" not in text

    def unexpected(*a, **k):
        pytest.fail("terminal day must not run again")

    again = run_rotation_analysis(
        policy=policy(),
        store=store,
        load_inputs=unexpected,
        analyze=unexpected,
        clock=lambda: NOW,
        notify=unexpected,
    )
    assert again == result and len(sent) == 1


@pytest.mark.parametrize(
    "code,phrase",
    [
        ("QUANT_ROUTE_OR_QUOTA_BLOCKED", "소진으로 단정할 수 없어"),
        ("QUANT_CALL_BUDGET_EXHAUSTED", "이번 분석의 호출 횟수"),
        ("quant_validation_failed", "결과 검증"),
        ("ValueError", "원인 미확정"),
    ],
)
def test_failure_notice_does_not_call_every_error_a_quota_wait(code, phrase):
    sent = []
    doc = dict(
        status="FAILED",
        trade_date="2026-09-23",
        failure_code=code,
        failure_reason="SECRET_TOKEN",
        analysis_summary="원인: SECRET_TOKEN",
        decision=None,
    )
    make_notifier(
        lambda text, reply_markup=None: sent.append(text) or "telegram-group:-99:42"
    )(doc)
    assert phrase in sent[0] and "SECRET_TOKEN" not in sent[0]
    assert sent[0].count("원인:") == 1 and "조치:" in sent[0]
