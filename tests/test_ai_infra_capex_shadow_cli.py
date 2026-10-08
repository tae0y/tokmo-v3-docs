import importlib.util, json
from datetime import datetime, timezone
from pathlib import Path


P=Path(__file__).parents[1]/"tools/run_ai_infra_capex_shadow.py"
spec=importlib.util.spec_from_file_location("run_ai_infra_capex_shadow",P); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


def test_cli_silent_success_failure_and_preflight(tmp_path,monkeypatch,capsys):
    cfg=tmp_path/"config.json"; cfg.write_text("{}")
    monkeypatch.setattr(m,"invoke",lambda value:0)
    assert m.main(["--config",str(cfg)])==0
    monkeypatch.setattr(m,"invoke",lambda value:1)
    assert m.main(["--config",str(cfg)])==1
    assert m.main(["--preflight"])==0
    assert capsys.readouterr()==("","")


def test_cli_bad_args_and_config_are_silent(tmp_path,capsys):
    bad=tmp_path/"bad"; bad.write_text("{")
    assert m.main(["--config",str(bad)])==1
    assert m.main([])==1
    assert capsys.readouterr()==("","")


def test_cli_missing_config_value_is_silent(capsys):
    assert m.main(["--config"])==1
    assert capsys.readouterr()==("","")
