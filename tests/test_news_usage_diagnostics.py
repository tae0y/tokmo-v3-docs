"""Behavior parity, failure isolation, usage attribution and restart-safe diagnostics."""

import json
import os
from copy import deepcopy
from datetime import timedelta

import pytest
from test_news_decision_engine import decision, observation, screen
from test_news_incremental_review import context
from test_news_product_gate import gate_world  # pytest fixture

from tokmo.programs.news_decision_engine import RECORDED_TIME, NewsDecisionEngine
from tokmo.programs.news_usage_diagnostics import (
    BASELINE_KEY,
    UsageDiagnostics,
    baseline,
    changes,
    observe_call,
    usage_observation,
)
from tokmo.programs.news_usage_report import usage_report
from tokmo.records.news_decision_store import NewsDecisionStore, encoded, kst, timestamp

AT = timestamp("2026-09-15T10:00:00+09:00")
USAGE = dict(
    prompt_tokens=100,
    completion_tokens=20,
    total_tokens=120,
    prompt_tokens_details=dict(cached_tokens=50),
    completion_tokens_details=dict(reasoning_tokens=5),
)


def records(path):
    return [
        json.loads(line)
        for f in sorted(path.glob("*.jsonl"))
        for line in f.read_text().splitlines()
    ]


def run_case(path, enabled, change, *, broken=False):
    path.mkdir()
    data = context()
    # Neither the raw account identifier nor reason content may be copied to new logs.
    data["holdings"][0]["account"] = "PRIVATE-ACCOUNT-123456"
    data["investment_followup"]["choices"][0]["summary"] = "PRIVATE-REASON-SENTINEL"
    calls = []

    def analyze(q):
        calls.append(deepcopy(q))
        return decision(
            [e["id"] for e in q["evidence"]],
            notify=False,
            open=True,
            change="NONE",
            urgency="NORMAL",
            review_dependencies=dict(all_portfolio=False, keys=["KR:1"]),
            followup_scope=dict(all_choices=False, choice_ids=["choice1"]),
            recheck=dict(
                after=kst(AT + timedelta(hours=18)), reason="지정된 발표 확인"
            ),
            _usage=USAGE,
            _model="test",
        )

    analyze.model = "test"
    analyze.policy_fingerprint = "stable"

    def screening(q):
        calls.append(deepcopy(q))
        return dict(screen(q), _usage=USAGE, _model="test")

    recorder = (
        UsageDiagnostics(path / "diagnostics", now=AT, purpose="verification")
        if enabled
        else None
    )
    if broken:
        recorder.directory.write_text("cannot mkdir here")
    store = NewsDecisionStore(path / "db.sqlite3")
    engine = NewsDecisionEngine(
        store,
        screening,
        analyze,
        clock=RECORDED_TIME,
        context_mode="incremental",
        context_reader=lambda now: deepcopy(data),
        reuse_unchanged_reviews=True,
        scoped_review_reuse=True,
        diagnostics=recorder,
    )
    first = engine.tick([observation("a", AT)], AT)
    assert not first["failures"]
    before = deepcopy(store.events()[0])
    basis = engine._review_basis(before)
    when = AT + timedelta(hours=6)
    if change == "quantity":
        data["holdings"][0]["quantity"] = "3"
    elif change == "reason":
        data["investment_followup"]["choices"][0]["summary"] = "new thesis"
    elif change == "health":
        data["portfolio_status"] = "STALE"
    elif change == "unrelated":
        data["holdings"][1]["quantity"] = "7"
    elif change == "timestamps":
        data["holdings"][0]["observed_at"] = kst(when)
        data["holdings"].reverse()
    elif change == "deadline":
        when = AT + timedelta(hours=18)
    elif change == "missing_baseline":
        e = store.events()[0]
        e.pop(BASELINE_KEY, None)
        store.save_event(e)
    # Restart the engine to prove persisted baselines work, not an in-memory prior value.
    engine = NewsDecisionEngine(
        store,
        screening,
        analyze,
        clock=RECORDED_TIME,
        context_mode="incremental",
        context_reader=lambda now: deepcopy(data),
        reuse_unchanged_reviews=True,
        scoped_review_reuse=True,
        diagnostics=recorder,
    )
    result = engine.tick(
        [observation("b", when)] if change == "new_input" else [], when
    )
    assert not result["failures"]
    final = deepcopy(store.events()[0])
    final.pop(BASELINE_KEY, None)
    rows = [
        dict(zip(["id", "request", "response"], r))
        for r in store.connection.execute(
            "select id,request,response from news_decision_calls order by rowid"
        )
    ]
    store.close()
    if recorder:
        recorder.safe("finish", dict(status="OK"))
    return dict(
        requests=calls,
        first=first,
        result=result,
        event=final,
        rows=rows,
        basis=basis,
        recorder=recorder,
        before=before,
    )


@pytest.mark.parametrize(
    "change",
    [
        "same",
        "quantity",
        "reason",
        "health",
        "unrelated",
        "timestamps",
        "deadline",
        "missing_baseline",
        "new_input",
    ],
)
def test_logging_does_not_change_requests_ids_reuse_or_proposals(tmp_path, change):
    off = run_case(tmp_path / "off", False, change)
    on = run_case(tmp_path / "on", True, change)
    for k in ["requests", "first", "result", "event", "rows", "basis"]:
        assert on[k] == off[k], k
    assert on["recorder"].errors == 0
    rs = records(tmp_path / "on" / "diagnostics")
    gates = [r for r in rs if r["kind"] == "review_gate"]
    assert len(gates) == (0 if change == "new_input" else 1)
    if change == "quantity":
        assert any(p.endswith("/quantity") for p in gates[0]["changes"]["fields"])
        assert gates[0]["changes"]["root_changed"]
    if change in {"same", "timestamps", "unrelated"}:
        assert gates[0]["outcome"] == "REUSED" and gates[0]["changes"]["fields"] == []
        assert on["event"]["updated_at"] == on["before"]["updated_at"]
    if change == "deadline":
        assert gates[0]["reasons"] == ["DEADLINE_REACHED"] and gates[0]["recheck_at"]
    if change == "missing_baseline":
        assert gates[0]["changes"]["status"] == "BASELINE_UNAVAILABLE"
    text = "\n".join(encoded(r) for r in rs)
    assert (
        "PRIVATE-ACCOUNT-123456" not in text and "PRIVATE-REASON-SENTINEL" not in text
    )
    assert all(
        os.stat(f).st_mode & 0o777 == 0o600
        for f in (tmp_path / "on" / "diagnostics").glob("*")
    )


def test_logging_failure_does_not_change_financial_behavior(tmp_path):
    off = run_case(tmp_path / "off", False, "quantity")
    on = run_case(tmp_path / "broken", True, "quantity", broken=True)
    for k in ["requests", "first", "result", "event", "rows", "basis"]:
        assert on[k] == off[k]
    assert on["recorder"].status()["coverage"] == "incomplete"


def test_diagnostic_baseline_missing_schema_change_and_field_overflow():
    data = context()
    event = dict(
        review_dependencies=dict(all_portfolio=False, keys=["KR:1"]),
        followup_scope=dict(all_choices=False, choice_ids=[]),
    )
    old = baseline(event, data, True)
    reordered = deepcopy(data)
    reordered["holdings"].reverse()
    assert old == baseline(event, reordered, True)
    assert changes(None, old)["status"] == "BASELINE_UNAVAILABLE"
    oldschema = dict(old, schema_version=0)
    assert changes(oldschema, old)["status"] == "DIAGNOSTIC_SCHEMA_CHANGED"
    large = context()
    large["metadata"] = {str(i): str(i) for i in range(2000)}
    assert baseline(event, large, True)["omitted"] > 0


@pytest.mark.parametrize("invalid", [True, False])
def test_final_prompt_size_and_usage_before_decode_failure(
    tmp_path, monkeypatch, invalid
):
    import subscription_json_model
    from news_decision_models import DecisionModel

    seen = []

    def infer(prompt, **kwargs):
        seen.append((prompt, kwargs))
        return ("not json" if invalid else '{"ok":true}'), dict(
            _usage=USAGE, _model="test", _elapsed_seconds=0.2
        )

    monkeypatch.setattr(subscription_json_model, "infer", infer)
    q = {"task": "analyze", "evidence": [], "context": {"holdings": []}, "now": kst(AT)}
    model = DecisionModel("codex:test", "analyze", timeout=1)

    def invoke():
        if invalid:
            assert model(q)["invalid_model_reason"] == "invalid_json"
        else:
            assert model(q)["ok"] is True

    invoke()
    recorder = UsageDiagnostics(tmp_path, now=AT, purpose="verification")
    with observe_call(recorder, "call1", "analyze", q, kst(AT)):
        invoke()
    assert seen[0] == seen[1] and recorder.errors == 0
    rs = records(tmp_path)
    prompt = next(r for r in rs if r["kind"] == "prompt")
    assert prompt["size"]["chars"] == len(seen[1][0])
    assert prompt["size"]["utf8_bytes"] == len(seen[1][0].encode())
    assert (
        next(r for r in rs if r["kind"] == "provider_received")["usage"]["total_tokens"]
        == 120
    )


def test_usage_join_missing_failed_duplicates_conflict_and_partial_tail(tmp_path):
    r = UsageDiagnostics(tmp_path, now=AT, purpose="recovery")
    r.safe("evaluation", "analyze", "event1")
    with observe_call(r, "failed", "analyze", {"evidence": []}, kst(AT)):
        usage_observation({"_usage": USAGE})
    with observe_call(r, "ok", "analyze", {"evidence": []}, kst(AT)):
        usage_observation({"_usage": USAGE})
    r.safe("finish", {"status": "OK"})
    f = next(tmp_path.glob("*.jsonl"))
    text = f.read_text()
    f.write_text(text + text + "{truncated")
    calls = [
        dict(id="failed", stage="analyze", status="ERROR", response=None),
        dict(
            id="ok",
            stage="analyze",
            status="OK",
            response=json.dumps({"_usage": USAGE}),
        ),
        dict(id="unknown", stage="analyze", status="ERROR", response=None),
    ]
    report = usage_report(tmp_path, calls, since=AT, until=AT + timedelta(hours=1))
    assert (
        report["totals"]["total_tokens"] == 240
        and report["totals"]["supplemental_tokens"] == 120
    )
    assert (
        report["totals"]["usage_unknown_calls"] == 1 and report["malformed_lines"] == 1
    )
    assert (
        report["totals"]["missing_call_diagnostics"] == 1
        and report["totals"]["usage_conflicts"] == 0
    )
    f.write_text(text)
    with observe_call(r, "ok", "analyze", {"evidence": []}, kst(AT)):
        usage_observation(
            {"_usage": dict(prompt_tokens=101, completion_tokens=20, total_tokens=121)}
        )
    assert usage_report(tmp_path, calls, since=AT)["totals"]["usage_conflicts"] == 1


def test_reuse_no_new_provider_and_correction_parent(tmp_path):
    from tokmo.programs.news_usage_diagnostics import usage_view

    assert (
        usage_view(dict(prompt_tokens=100, completion_tokens=20, total_tokens=121))
        is None
    )
    r = UsageDiagnostics(tmp_path, now=AT)
    r.safe("evaluation", "screen")
    with observe_call(r, "first", "screen", {"evidence": []}, kst(AT)):
        pass
    with observe_call(
        r,
        "second",
        "screen",
        {"evidence": [], "correction": {"reason": "format"}},
        kst(AT),
    ):
        pass
    starts = [x for x in records(tmp_path) if x["kind"] == "call_started"]
    assert starts[0]["parent_call_id"] is None
    assert (
        starts[1]["parent_call_id"] == "first"
        and starts[0]["evaluation_id"] == starts[1]["evaluation_id"]
    )


def test_actual_engine_correction_and_cached_call_keep_one_evaluation(tmp_path):
    recorder = UsageDiagnostics(tmp_path / "logs", now=AT)
    store = NewsDecisionStore(tmp_path / "state.db")
    calls = []

    def analyze(q):
        calls.append(q)
        result = (
            decision()
            if q.get("correction")
            else {
                "invalid_model_reason": "invalid_json",
                "invalid_model_response": "broken",
            }
        )
        return dict(result, _usage=USAGE, _model="fixture")

    engine = NewsDecisionEngine(
        store, screen, analyze, clock=RECORDED_TIME, diagnostics=recorder
    )
    result = engine.tick([observation(at=AT)], AT)
    assert not result["failures"] and len(result["alerts"]) == 1 and len(calls) == 2
    rows = records(tmp_path / "logs")
    starts = [
        r for r in rows if r["kind"] == "call_started" and r["stage"] == "analyze"
    ]
    assert starts[1]["parent_call_id"] == starts[0]["call_id"]
    assert starts[1]["evaluation_id"] == starts[0]["evaluation_id"]
    done = [
        r for r in rows if r["kind"] == "evaluation_finished" and r["task"] == "analyze"
    ]
    assert (
        len(done) == 1
        and done[0]["accepted"]
        and done[0]["proposal_ids"] == [result["alerts"][0]["id"]]
    )
    first = next(
        r
        for r in rows
        if r["kind"] == "call_finished" and r["call_id"] == starts[0]["call_id"]
    )
    assert first["decoded"] is False and first["decode_error"] == "invalid_json"
    # Re-read an already successful call: no additional callback or charged usage record.
    request = dict(calls[-1])
    request.pop("now", None)
    previous_count = len(calls)
    engine._call("analyze", request, {"calls": 0}, context=engine._context)
    assert len(calls) == previous_count
    assert records(tmp_path / "logs")[-1]["kind"] == "call_reused"
    store.close()


@pytest.mark.parametrize("broken", [False, True])
def test_worker_wires_switch_and_exposes_coverage_without_health_noise(
    tmp_path, monkeypatch, broken
):
    from test_news_decision_worker import worker

    import tokmo.kis_composition

    monkeypatch.setattr(
        tokmo.kis_composition,
        "refresh_kis_etf_inputs",
        lambda **kw: {"status": "fixture"},
    )
    empty = lambda *a, **kw: dict(status="OK", observations=[], next_since=kst(AT))
    for name in (
        "read_news_observations",
        "read_disclosure_observations",
        "read_sec_observations",
    ):
        monkeypatch.setattr(worker, name, empty)
    config = dict(enabled=True, cutoff=kst(AT))
    no_call = lambda q: pytest.fail("No extra model calls for logging")
    results = []
    for enabled in (False, True):
        root = tmp_path / str(enabled)
        path = root / "news/decision-worker/usage-diagnostics"
        if enabled and broken:
            path.parent.mkdir(parents=True)
            path.write_text("blocked")
        result = worker.run_once(
            root,
            dict(config, usage_diagnostics_enabled=enabled),
            now=AT,
            screen=no_call,
            analyze=no_call,
            clock=RECORDED_TIME,
            diagnostic_purpose="verification",
            diagnostic_task_id="unit-test",
        )
        results.append(result)
        if enabled:
            assert result["usage_diagnostics"]["coverage"] == (
                "incomplete" if broken else "complete"
            )
            if not broken:
                rs = records(path)
                assert (
                    rs[0]["kind"] == "run_started"
                    and rs[0]["purpose"] == "verification"
                )
                assert all(rs[0]["code"]["files"].values())
                assert rs[-1]["kind"] == "run_finished" and rs[-1]["work"]["calls"] == 0
        else:
            assert not path.exists() and "usage_diagnostics" not in result
    for key in ("status", "health", "input_states", "ticks", "delivery"):
        assert results[0][key] == results[1][key]


def test_partial_usage_details_do_not_double_count_or_hide_known_total(tmp_path):
    from tokmo.programs.news_usage_diagnostics import usage_view

    assert (
        usage_view(dict(USAGE, prompt_tokens_details="malformed"))["total_tokens"]
        == 120
    )
    r = UsageDiagnostics(tmp_path, now=AT)
    q = {"evidence": []}
    with observe_call(r, "a", "screen", q, kst(AT)):
        usage_observation({"_usage": USAGE})
        usage_observation(
            {"_usage": dict(prompt_tokens=100, completion_tokens=20, total_tokens=120)}
        )
        usage_observation({"_usage": {"total_tokens": "unusable"}})
    result = usage_report(
        tmp_path,
        [dict(id="a", stage="screen", status="ERROR", response=None)],
        since=AT,
    )
    assert (
        result["totals"]["total_tokens"] == 120
        and result["totals"]["usage_conflicts"] == 0
    )
    assert result["totals"]["cached_input_tokens"] == 50


def test_large_table_keeps_changed_field_without_per_cell_growth():
    ctx = {
        "holdings": [
            {"key": f"KR:{i}", "account": "private", "quantity": 1} for i in range(500)
        ]
    }
    old = baseline({}, ctx, False)
    ctx["holdings"].reverse()
    assert baseline({}, ctx, False) == old
    ctx["holdings"][0]["quantity"] = 2
    diff = changes(old, baseline({}, ctx, False))
    assert diff["fields"] == ["/holdings/*/quantity"] and diff["root_changed"]
    assert old["omitted"] == 0 and len(encoded(old)) < 2000


def test_glm_screen_measures_sent_short_ids_and_has_no_separate_schema(
    tmp_path, monkeypatch, gate_world
):
    import news_decision_models as models

    monkeypatch.setenv("ZAI_API_KEY", "fixture-only")
    seen = []

    def fake(prompt, **kwargs):
        seen.append(prompt)
        kwargs["opener"].usage = USAGE
        return '{"candidates":[],"ignored":[]}'

    monkeypatch.setattr(models, "zai_chat_once", fake)
    model = models.DecisionModel(
        "glm-5.3", "screen", diagnostic_path=tmp_path / "screen-last-error.json"
    )
    q = dict(
        task="screen",
        evidence=[observation("very-long-document-identifier", AT)],
        events=[],
        now=kst(AT),
    )
    model(q)
    r = UsageDiagnostics(tmp_path, now=AT)
    with observe_call(r, "call", "screen", q, kst(AT)):
        model(q)
    prompt = next(x for x in records(tmp_path) if x["kind"] == "prompt")
    assert seen[0] == seen[1] and prompt["size"]["chars"] == len(seen[1])
    sent = json.loads(seen[1].split("INPUT_JSON:\n")[1])
    assert sent["evidence"][0]["id"] != q["evidence"][0]["id"]
    assert prompt["areas"]["evidence"]["chars"] == len(
        json.dumps(sent["evidence"], ensure_ascii=False)
    )
    assert prompt["schema_size"] is None
