import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

BASE = Path(__file__).resolve().parents[1] / "tools"
PROJECT = Path(__file__).resolve().parents[1]
RUNTIME = (
    Path(__file__).resolve().parents[1]
)  # canonical project tree (retired news-shadow-src runtime)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_news_factory_connects_guarded_runner_without_changing_glm(monkeypatch):
    monkeypatch.syspath_prepend(str(RUNTIME / "src"))
    monkeypatch.syspath_prepend(str(PROJECT / "tools"))
    monkeypatch.syspath_prepend(str(BASE))
    import subscription_json_model

    seen = {}

    def stop(prompt, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop before model")

    monkeypatch.setattr(subscription_json_model, "infer", stop)
    models = load("news_models_candidate", BASE / "news_decision_models.py")
    screen, analyze = models.build_models(
        screen_model="glm-5.3",
        analysis_model="codex:gpt-5.6-sol",
        analysis_reasoning_effort="high",
        timeout=120,
    )
    assert screen.model == "glm-5.3"
    with pytest.raises(RuntimeError, match="stop before model"):
        analyze({"task": "fixture", "context": {}, "events": [], "evidence": []})
    assert seen["runner"].__module__ == "news_subscription_model"
    assert seen["model"] == "gpt-5.6-sol" and seen["effort"] == "high"


@pytest.mark.parametrize("denied", [False, True])
def test_bridge_uses_preflight_then_run_and_preserves_cli_error_handling(
    tmp_path, monkeypatch, denied
):
    monkeypatch.syspath_prepend(str(RUNTIME / "src"))
    monkeypatch.syspath_prepend(str(BASE))
    bridge = load(
        "news_subscription_model", PROJECT / "tools/news_subscription_model.py"
    )
    called = []

    def invoke(argv, **kwargs):
        command = argv[2]
        called.append(command)
        request = json.loads(Path(argv[argv.index("--request") + 1]).read_text())
        assert request["purpose"] == "tokmo-news-confirmation"
        assert request["model"] == "gpt-5.6-sol" and request["timeout_seconds"] == 120
        assert Path(request["prompt_file"]).read_text() == "harmless fixture"
        out = Path(request["output_dir"])
        if command == "preflight":
            if denied:
                return SimpleNamespace(
                    returncode=2,
                    stdout=json.dumps({"issues": [{"code": "QUOTA_EXHAUSTED"}]}),
                    stderr="",
                )
            out.mkdir()
            (out / "READY.json").write_text("{}")
        else:
            assert command == "run"
            (out / "RESULT.json").write_text(
                json.dumps(
                    {
                        "model_api_started": "TRUE",
                        "external_cli_result": "SUCCEEDED",
                        "route_admission": "ADMITTED",
                        "model": "gpt-5.6-sol",
                        "fallback_used": False,
                    }
                )
            )
            (out / "child.stdout").write_text('{"type":"turn.completed"}\n')
            (out / "child.stderr").write_text("")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(bridge.subprocess, "run", invoke)
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}')
    argv = [
        "codex",
        "exec",
        "--model",
        "gpt-5.6-sol",
        "-c",
        'model_reasoning_effort="high"',
        "--output-schema",
        str(schema),
        "-",
    ]
    if denied:
        with pytest.raises(Exception) as error:
            bridge.run_news(
                argv,
                "harmless fixture",
                str(tmp_path),
                120,
                diagnostic_path=tmp_path / "diagnostics/analyze-last-error.json",
            )
        assert error.value.category == "QUOTA" and called == ["preflight"]
    else:
        assert "turn.completed" in bridge.run_news(
            argv,
            "harmless fixture",
            str(tmp_path),
            120,
            diagnostic_path=tmp_path / "diagnostics/analyze-last-error.json",
        )
        assert called == ["preflight", "run"]
        assert (
            json.loads(
                (tmp_path / "diagnostics/analyze-last-admission.json").read_text()
            )["external_cli_result"]
            == "SUCCEEDED"
        )
