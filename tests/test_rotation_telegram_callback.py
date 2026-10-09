import importlib.util
from io import StringIO
from pathlib import Path


def tool():
    path = Path(__file__).parents[1] / "tools/rotation_telegram_callback.py"
    spec = importlib.util.spec_from_file_location("rotation_callback_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_callback_without_configured_owner_does_not_read_or_write_order_state(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.delenv("TOKMO_ROTATION_OWNER_ID", raising=False)
    monkeypatch.setattr("sys.stdin", StringIO("{}"))
    assert tool().main(["--state-dir", str(tmp_path)]) == 2
    assert list(tmp_path.iterdir()) == []
    assert "owner_not_configured" in capsys.readouterr().out


def test_callback_owner_comes_from_gateway_environment_not_request(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.setenv("TOKMO_ROTATION_OWNER_ID", "7")
    monkeypatch.setattr("sys.stdin", StringIO('{"user_id":"8","owner_id":"8"}'))
    assert tool().main(["--state-dir", str(tmp_path)]) == 2
    assert list(tmp_path.iterdir()) == []
    assert "invalid_callback_payload" in capsys.readouterr().out


def test_deployed_bridge_check_requires_owner_and_policy_without_reading_callback(
    monkeypatch, capsys, tmp_path
):
    import json

    from test_rotation_analysis import policy

    module = tool()
    monkeypatch.setattr(module, "select_policy", lambda root: policy())
    monkeypatch.setattr(
        module,
        "resolve_callback",
        lambda **kw: (_ for _ in ()).throw(AssertionError("approval invoked")),
    )
    monkeypatch.setenv("TOKMO_ROTATION_OWNER_ID", "7")
    assert module.main(["--state-dir", str(tmp_path), "--check"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "READY"
    assert list(tmp_path.iterdir()) == []
    monkeypatch.delenv("TOKMO_ROTATION_OWNER_ID")
    assert module.main(["--state-dir", str(tmp_path), "--check"]) == 2
    assert "owner_not_configured" in capsys.readouterr().out
