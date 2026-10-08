"""The news worker reads var/data/models.json, builds chains, and reports a switch in its health notice."""
from test_quant_repeat_latency import routing_fixture
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import sys

from tokmo.programs.model_chain import ModelChain
from tokmo.records.news_decision_store import timestamp

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
spec = importlib.util.spec_from_file_location("decision_worker_registry", TOOLS / "run_news_decision_worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
import news_decision_models as models

T = timestamp("2026-09-25T10:06:00+09:00")


def run(root, capsys, *extra, **kw):
    code = worker.main(["--root", str(root), "--now", T.isoformat(), *extra], **kw)
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_worker_passes_registry_chains_and_state_path_to_the_builder(tmp_path, capsys):
    (tmp_path / "news_decision.json").write_text(json.dumps({"enabled": True, "cutoff": "2026-09-01T09:00:00+09:00",
        "screen_model": "glm-5.3-flash", "analysis_model": "glm-5.3", "first_pass_model": "glm-5.3"}))
    (tmp_path / "models.json").write_text(json.dumps({"schema": "tokmo-models-v1", "news": {
        "screen": ["glm-5.3", "codex:gpt-5.6-terra@low"], "confirm": {"model": "codex:gpt-5.6-sol", "effort": "high", "transport": "direct"}}}))
    seen = {}
    never = lambda request: (_ for _ in ()).throw(AssertionError("model called"))
    def build(**kw):
        seen.update(kw)
        return never, never
    code, report = run(tmp_path, capsys, build=build)
    assert report["status"] == "DEGRADED"      # sources unavailable in the empty root, as in the other worker tests
    assert [str(s) for s in seen["screen_model"]] == ["glm-5.3", "codex:gpt-5.6-terra@low"]
    assert [str(s) for s in seen["analysis_model"]] == ["codex:gpt-5.6-sol@high"] and seen["analysis_model"][0].transport == "direct"
    assert seen["chain_state"] == tmp_path / "news/decision-worker/model-chain.json" and callable(seen["clock"])
    assert callable(seen["quota_reset"]) and seen["quota_reset"]("zai", T) is None      # no Hermes snapshot in this root -> unknown


def test_invalid_registry_is_an_exception_pass_not_a_silent_default(tmp_path, capsys):
    (tmp_path / "news_decision.json").write_text(json.dumps({"enabled": True, "cutoff": "2026-09-01T09:00:00+09:00"}))
    (tmp_path / "models.json").write_text(json.dumps({"schema": "tokmo-models-v1", "news": {"screen": "openai:gpt-5"}}))
    code, report = run(tmp_path, capsys, build=lambda **kw: (None, None))
    assert code == 1 and report["status"] == "EXCEPTION"
    latest = json.loads((tmp_path / "news/decision-worker/latest.json").read_text())
    assert "unknown provider" in latest["health"]["exception"]


def test_unknown_model_id_is_rejected_before_any_call_even_from_legacy_keys(tmp_path, capsys, monkeypatch):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(json.dumps({"models": [{"slug": "gpt-5.6-sol", "visibility": "list"},
                                                                          {"slug": "gpt-reserve", "visibility": "hide"}]}))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    (tmp_path / "news_decision.json").write_text(json.dumps({"enabled": True, "cutoff": "2026-09-01T09:00:00+09:00",
                                                             "analysis_model": "codex:gpt-5-6-sol"}))     # typo in the legacy key
    never = lambda **kw: (_ for _ in ()).throw(AssertionError("models were built"))
    code, report = run(tmp_path, capsys, build=never)
    assert code == 1 and report["status"] == "EXCEPTION"
    latest = json.loads((tmp_path / "news/decision-worker/latest.json").read_text())
    assert "unknown model id: news.confirm: codex:gpt-5-6-sol" in latest["health"]["exception"]
    # A hidden catalog entry is unknown too; a listed one passes.
    (tmp_path / "models.json").write_text(json.dumps({"schema": "tokmo-models-v1", "news": {"confirm": "codex:gpt-reserve"}}))
    code, report = run(tmp_path, capsys, build=never)
    assert report["status"] == "EXCEPTION"
    (tmp_path / "models.json").write_text(json.dumps({"schema": "tokmo-models-v1", "news": {"confirm": "codex:gpt-5.6-sol"}}))
    code, report = run(tmp_path, capsys, build=lambda **kw: (never, never))
    assert report["status"] == "DEGRADED"


def test_builder_returns_plain_model_for_one_spec_and_a_chain_for_several(tmp_path):
    screen, analyze = models.build_models(screen_model=["glm-5.3", "codex:gpt-5.6-terra@low"], analysis_model="glm-5.3",
                                          chain_state=tmp_path / "chain.json", clock=lambda: T, diagnostic_root=tmp_path)
    assert isinstance(screen, ModelChain) and screen.stage == "screen" and screen.model == "chain:screen[glm-5.3>codex:gpt-5.6-terra]"
    assert [m.model for m in screen.members] == ["glm-5.3", "codex:gpt-5.6-terra"]
    assert screen.members[1].reasoning_effort == "low" and screen.members[1].provider == "codex"
    assert screen.members[1].diagnostic_path == tmp_path / "screen-fallback1-last-error.json"
    assert isinstance(analyze, models.DecisionModel) and analyze.provider == "zai"
    first = models.build_first_pass_model(["glm-5.3", "claude:claude-haiku-4-5-20251001"], chain_state=tmp_path / "chain.json", clock=lambda: T)
    assert isinstance(first, ModelChain) and first.stage == "analyze-first" and first.members[1].transport == "direct"
    assert first.members[1].provider == "claude" and first.members[1].reasoning_effort == "medium"


def test_confirm_stage_uses_the_hermes_launcher_only_for_the_admitted_pair_and_only_when_installed(tmp_path, monkeypatch):
    import pytest
    from tokmo.values.model_registry import ModelRegistryError
    monkeypatch.setattr(models, "HERMES_LAUNCHER", tmp_path / "missing")
    _, direct = models.build_models(analysis_model="codex:gpt-5.6-sol", analysis_reasoning_effort="high")
    assert direct.transport == "direct"
    (tmp_path / "launcher").write_text("")
    monkeypatch.setattr(models, "HERMES_LAUNCHER", tmp_path / "launcher")
    _, via_hermes = models.build_models(analysis_model="codex:gpt-5.6-sol", analysis_reasoning_effort="high")
    assert via_hermes.transport == "hermes"
    _, forced = models.build_models(analysis_model=[{"model": "codex:gpt-5.6-sol", "effort": "high", "transport": "direct"}])
    assert forced.transport == "direct"
    # Anything the launcher would refuse (other model, other effort, another provider) runs directly ...
    for spec, effort in (("codex:gpt-5.6-terra@low", "high"), ("codex:gpt-5.6-sol", "medium"), ("claude:claude-haiku-4-5-20251001", "high")):
        _, member = models.build_models(analysis_model=spec, analysis_reasoning_effort=effort)
        assert member.transport == "direct", spec
    _, mixed = models.build_models(analysis_model=["codex:gpt-5.6-sol@high", "claude:claude-sonnet-5"], chain_state=tmp_path / "c.json", clock=lambda: T)
    assert [m.transport for m in mixed.members] == ["hermes", "direct"]
    # ... and asking for the launcher explicitly on such a pair is a configuration error, never a runtime ADMISSION stop.
    with pytest.raises(ModelRegistryError, match="transport hermes admits only codex:gpt-5.6-sol@high"):
        models.build_models(analysis_model=[{"model": "codex:gpt-5.6-terra", "effort": "low", "transport": "hermes"}])
    first = models.build_first_pass_model("codex:gpt-5.6-sol@high")
    assert first.transport == "direct"      # the first pass never goes through the launcher


def test_a_due_retry_without_a_call_is_not_reported_as_recovery(tmp_path, capsys):
    """The worker builds a real chain whose preferred member failed earlier; time passing alone keeps
    the switch item (no RECOVERY notice) until the preferred member actually answers."""
    (tmp_path / "news_decision.json").write_text(json.dumps({"enabled": True, "cutoff": "2026-09-01T09:00:00+09:00"}))
    (tmp_path / "models.json").write_text(json.dumps({"schema": "tokmo-models-v1", "news": {"screen": ["glm-5.3", "codex:gpt-5.6-terra@low"]}}))
    state = tmp_path / "news/decision-worker/model-chain.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"screen": {"glm-5.3": {"category": "TRANSIENT", "attempts": 1, "since": "2026-09-25T09:50:00+09:00",
                                                        "failed_at": "2026-09-25T09:50:00+09:00", "retry_at": "2026-09-25T09:55:00+09:00"}}}))
    never = lambda request: (_ for _ in ()).throw(AssertionError("model called"))
    def build(**kw):
        return models.build_chain(kw["screen_model"], "screen", label="screen", chain_state=kw["chain_state"], clock=kw["clock"]), never
    code, first = run(tmp_path, capsys, build=build)          # T = 10:06, retry was due at 09:55, nothing was called
    assert first["status"] == "DEGRADED" and "screen 모델 전환: glm-5.3 대신 codex:gpt-5.6-terra 사용 중 (TRANSIENT · 다음 요청에서 glm-5.3 복귀 확인)" in first["notice"]
    latest = json.loads((tmp_path / "news/decision-worker/latest.json").read_text())
    assert latest["health"]["model_switches"][0]["retry_due"] is True and latest["model_chain"][0]["active"] == "codex:gpt-5.6-terra"
    # The config never set model_wait_enabled: the chain turns it on so held inputs can resume, and says so.
    assert latest["model_wait_enabled"] is True and latest["model_wait_reason"] == "enabled for the configured model chain"
    code, later = worker.main(["--root", str(tmp_path), "--now", (T + timedelta(hours=1)).isoformat()], build=build), json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "복구" not in later.get("notice", "") and json.loads(state.read_text())["screen"]["glm-5.3"]["category"] == "TRANSIENT"


def test_worker_holds_new_inputs_while_every_member_waits_and_resumes_them_without_wait_config(tmp_path, monkeypatch):
    """Reviewer flow, worker path, config without model_wait_enabled: all members fail -> input WAITING;
    a new input while all wait -> held (no call, no journal row); at the retry time both complete."""
    from subscription_json_model import SubscriptionInferenceError
    from test_news_decision_engine import decision
    from tokmo.programs.news_decision_engine import RECORDED_TIME
    from tokmo.records.news_decision_store import NewsDecisionStore

    class Member:
        def __init__(self, model, failures=()):
            self.model, self.failures, self.calls = model, list(failures), 0
            self.reasoning_effort, self.max_output_tokens, self.policy_fingerprint = 'low', 4096, 'stable'
        def __call__(self, request):
            self.calls += 1
            if self.failures:
                raise SubscriptionInferenceError(self.failures.pop(0), exit_code=1)
            return dict(decision([r['id'] for r in request['evidence']]), _model=self.model)

    now = {'at': timestamp('2026-09-25T10:00:00+09:00')}
    pending = {'rows': []}
    def news(*args, **kwargs):
        rows, pending['rows'] = pending['rows'], []
        return {"status": "OK", "observations": rows, "next_since": now['at'].isoformat()}
    monkeypatch.setattr(worker, "read_news_observations", news)
    for name in ("read_disclosure_observations", "read_sec_observations"):
        monkeypatch.setattr(worker, name, lambda *a, **kw: {"status": "OK", "observations": [], "next_since": now['at'].isoformat()})
    row = lambda key: {"id": key, "observed_at": now['at'].isoformat(), "title": key, "text": "본문", "source_kind": "NEWS", "publisher": "p", "url": "https://example.invalid/" + key}
    screen = lambda request: {"candidates": [{"evidence_ids": [e["id"]]} for e in request["evidence"]], "ignored": []}
    a, b = Member('glm-5.3', ['TRANSIENT']), Member('codex:gpt-5.6-terra', ['TRANSIENT'])
    chain = ModelChain([a, b], stage='analyze', state_path=tmp_path / 'news/decision-worker/model-chain.json', clock=lambda: now['at'])
    config = {"enabled": True, "cutoff": "2026-09-01T09:00:00+09:00"}        # no model_wait_enabled at all
    store = NewsDecisionStore(tmp_path / 'stores/news_episodes.sqlite3')
    journal = lambda: store.connection.execute("SELECT count(*), coalesce(sum(tokens),0) FROM news_decision_calls WHERE stage='analyze'").fetchone()
    try:
        pending['rows'] = [row('a')]
        report = worker.run_once(tmp_path, config, now=now['at'], screen=screen, analyze=chain, clock=RECORDED_TIME)
        assert report['model_wait_enabled'] is True and report['model_wait_reason'] == 'enabled for the configured model chain'
        assert store.counts() == {'WAITING': 1} and a.calls == b.calls == 1 and journal()[0] == 2     # one row per attempt
        before = journal()
        now['at'] = timestamp('2026-09-25T10:01:00+09:00')
        pending['rows'] = [row('b')]
        report = worker.run_once(tmp_path, config, now=now['at'], screen=screen, analyze=chain, clock=RECORDED_TIME)
        assert store.counts() == {'WAITING': 2} and a.calls == b.calls == 1 and journal() == before
        deferred = report['ticks'][0]['model_deferred'][0]          # whole-chain wait (or the chain hold) kept it, no row either way
        assert deferred['model'] == chain.model and deferred['retry_at'] == '2026-09-25T10:05:00+09:00'
        now['at'] = timestamp('2026-09-25T10:05:00+09:00')
        report = worker.run_once(tmp_path, config, now=now['at'], screen=screen, analyze=chain, clock=RECORDED_TIME)
        assert store.counts() == {'DONE': 2} and a.calls == 3 and b.calls == 1 and store.controls('model_wait:') == []
        assert journal()[0] == 4
    finally:
        store.close()


def test_check_api_keys_counts_only_selected_models(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("check_api_keys_test", TOOLS / "check_api_keys.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    data = tmp_path / "data"
    data.mkdir()
    (data / "news_decision.json").write_text(json.dumps({"enabled": True, "screen_model": "glm-5.3", "analysis_model": "glm-5.3"}))
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(json.dumps({"models": [{"slug": "gpt-5.6-terra", "visibility": "list"}]}))
    monkeypatch.setattr(tool, "DATA", data)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    report = tool.model_report()
    assert report["providers"] == ["zai"] and report["problems"] == []
    quant = [r for r in report["rows"] if r["module"] == "quant"]
    assert quant and quant[0]["selected"] is False and quant[0]["unknown"] == []
    import quant_decision_models
    route = quant_decision_models.ROUTING_PATH
    route.write_text(route.read_text().replace("gpt-6-astra", "synthetic-astra-99"))
    quant = next(r for r in tool.model_report()["rows"] if r["module"] == "quant")
    assert "codex:synthetic-astra-99" in quant["models"] and quant["unknown"] == []
    (data / "models.json").write_text(json.dumps({"schema": "tokmo-models-v1", "news": {"screen": "codex:gpt-6-astra"}}))
    report = tool.model_report()
    assert any("news.screen: unknown model id codex:gpt-6-astra" in p for p in report["problems"])


def test_health_notice_reports_a_switch_and_its_recovery():
    switched = {"status": "DEGRADED", "health": {"input_errors": [], "review_errors": [], "unavailable_sources": [],
                "model_switches": [{"stage": "screen", "preferred": "glm-5.3", "active": "codex:gpt-5.6-terra",
                                    "category": "QUOTA", "retry_at": "2026-09-25T16:00:00+09:00"}]}}
    state, notice = worker.health_notice(switched, None, T)
    assert "screen 모델 전환: glm-5.3 대신 codex:gpt-5.6-terra 사용 중 (QUOTA · 09-25 16:00 이후 glm-5.3 복귀 시도)" in notice
    assert state["items"] == ["model_switch:screen:codex:gpt-5.6-terra"]
    state, again = worker.health_notice(switched, state, T + timedelta(hours=1))
    assert again is None
    recovered = {"status": "OK", "health": {"input_errors": [], "review_errors": [], "unavailable_sources": [], "model_switches": []}}
    state, back = worker.health_notice(recovered, state, T + timedelta(hours=6))
    assert "복구" in back and state["items"] == []
