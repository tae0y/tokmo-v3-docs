from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_pulse_cadence_token_coordination import T1, _deps

from tokmo.facts.toss_reads import TossReadOperation as Op
from tokmo.programs.integrated_pulse import run_pulse_once
from tokmo.programs.quant_input_capture import ExistingTokenReader


def test_optional_quant_capture_receives_normal_tick_token_without_new_auth(tmp_path):
    calls = []
    received = []

    def capture(**kwargs):
        received.append(kwargs)
        return {"failures": []}

    deps = replace(
        _deps(tmp_path, T1, lambda _: True, calls), quant_input_capture=capture
    )
    result = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=T1,
        observed_now_utc=T1,
        deps=deps,
    )
    assert calls.count("token") == 1 and calls.count("quotes") == 1
    assert callable(received[0].pop("wire_recorder"))
    assert received == [
        {"existing_token": "t", "now": T1, "deadline": T1 + timedelta(seconds=35)}
    ]
    assert result.reason is None and result.wire_counts["TOKEN"] == 1


def test_capture_failure_preserves_normal_quote_cycle(tmp_path):
    calls = []

    def capture(**kwargs):
        raise RuntimeError("expired token or unavailable extra data")

    deps = replace(
        _deps(tmp_path, T1, lambda _: True, calls), quant_input_capture=capture
    )
    result = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=T1,
        observed_now_utc=T1,
        deps=deps,
    )
    assert (
        calls.count("token") == 1
        and calls.count("quotes") == 1
        and result.reason is None
    )
    assert any(
        s["stage"] == "quant_inputs" and s["status"] == "skipped" for s in result.stages
    )


def test_closed_tick_does_not_authenticate_just_for_quant(tmp_path):
    calls = []
    deps = replace(
        _deps(tmp_path, T1, lambda _: True, calls),
        quotes_provider_enabled=False,
        quant_input_capture=lambda **kw: calls.append("capture"),
    )
    run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=T1,
        observed_now_utc=T1,
        deps=deps,
    )
    assert "token" not in calls and "capture" not in calls


def test_time_budget_does_not_start_request_that_can_overrun_next_tick():
    calls = []
    reader = ExistingTokenReader(
        token="existing-token",
        refs=SimpleNamespace(reveal=lambda _: "123"),
        transport=lambda req: calls.append(req),
        clock=lambda: T1,
        deadline=T1 + timedelta(seconds=5),
    )
    with pytest.raises(RuntimeError, match="enough time"):
        reader(Op.BUYING_POWER, account="personal-a", currency="KRW")
    assert not calls and not reader.calls
