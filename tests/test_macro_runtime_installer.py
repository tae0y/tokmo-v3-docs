import importlib.util, json, os, plistlib, stat
from pathlib import Path
import pytest

P=Path(__file__).parents[1]/"tools/install_macro_runtime_agent.py"
spec=importlib.util.spec_from_file_location("install_macro_runtime_agent",P); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

def paths(tmp_path):
    project=tmp_path/"project"; (project/".venv/bin").mkdir(parents=True); (project/"tools").mkdir()
    py=project/".venv/bin/python"; py.write_text("x"); py.chmod(0o700)
    run=project/"tools/run_macro_runtime.py"; run.write_text("x")
    return m.resolve_paths(tmp_path/"home",project)

def permit_environment(monkeypatch):
    def run(command,**kwargs):
        return type("R",(),{"returncode":0,"stdout":b"1"*40+b"\n"})()
    monkeypatch.setattr(m.subprocess,"run",run)

def test_fixed_surface_paused_install_and_uninstall_preserves_state(tmp_path,monkeypatch):
    p=paths(tmp_path); permit_environment(monkeypatch)
    out=m.install(p,expected_commit="1"*40,dry_run=False)
    cfg=json.loads(p.config.read_text()); assert cfg["enabled"] is False and cfg["policy"]=="PAUSED"
    pl=plistlib.loads(p.plist.read_bytes()); assert pl["Label"]==m.AGENT_LABEL and pl["StartCalendarInterval"]=={"Hour":16,"Minute":10}
    assert pl["StandardOutPath"]==pl["StandardErrorPath"]=="/dev/null" and "exec >>" in p.wrapper.read_text()
    assert stat.S_IMODE(p.config.stat().st_mode)==0o600
    p.state_root.mkdir(parents=True,exist_ok=True); marker=p.state_root/"keep"; marker.write_text("x")
    m.uninstall(p); assert marker.exists()

def test_dry_run_and_offline_doctor_never_launchctl(tmp_path,monkeypatch):
    p=paths(tmp_path); calls=[]; permit_environment(monkeypatch)
    assert m.install(p,expected_commit="1"*40,dry_run=True)
    monkeypatch.setattr(m,"subprocess",type("S",(),{"run":staticmethod(lambda *a,**k: calls.append(a) or type("R",(),{"returncode":1,"stdout":b""})())})())
    m.doctor(p,offline=True); assert not any("launchctl" in str(x) for x in calls)

def test_cli_dry_run_injected_paths(tmp_path,monkeypatch):
    p=paths(tmp_path); permit_environment(monkeypatch)
    assert m.main(["--dry-run","--home",str(p.home),"--project-root",str(p.project_root),"--expected-commit","1"*40])==0

def test_install_refuses_non_kst_commit_mismatch_and_symlink(tmp_path,monkeypatch):
    p=paths(tmp_path)
    monkeypatch.setattr(m.subprocess,"run",lambda command,**kwargs:type("R",(),{"returncode":0,"stdout":b"1"*40+b"\n"})())
    bad=tmp_path/"UTC"; bad.write_text("zone")
    try: m.install(p,expected_commit="1"*40,dry_run=False,timezone_path=bad)
    except ValueError: pass
    else: assert False

def test_timezone_probe_is_privilege_free_and_fail_closed(tmp_path,monkeypatch):
    p=paths(tmp_path); zone=tmp_path/"zoneinfo/Asia/Seoul"; zone.parent.mkdir(parents=True); zone.write_text("zone"); local=tmp_path/"localtime"; local.symlink_to(zone)
    calls=[]
    def run(command,**kwargs):
        calls.append(command); return type("R",(),{"returncode":0,"stdout":b"1"*40+b"\n"})()
    monkeypatch.setattr(m.subprocess,"run",run)
    assert m.install(p,expected_commit="1"*40,dry_run=True,timezone_path=local)
    assert not any("systemsetup" in " ".join(command) for command in calls)
    bad=tmp_path/"UTC"; bad.write_text("zone"); local.unlink(); local.symlink_to(bad)
    with pytest.raises(ValueError): m.install(p,expected_commit="1"*40,dry_run=True,timezone_path=local)
    permit_environment(monkeypatch); p.venv_python.unlink(); p.venv_python.symlink_to("/bin/echo")
    try: m.install(p,expected_commit="1"*40,dry_run=False)
    except ValueError: pass
    else: assert False

def test_owned_venv_interpreter_symlink_to_owned_executable_is_valid(tmp_path,monkeypatch):
    p=paths(tmp_path); target=tmp_path/"python3.12"; target.write_text("x"); target.chmod(0o700)
    p.venv_python.unlink(); p.venv_python.symlink_to(target); permit_environment(monkeypatch)
    assert m.install(p,expected_commit="1"*40,dry_run=True)

def test_install_atomic_private_artifacts_log_rotation_and_no_48h_touch(tmp_path,monkeypatch):
    p=paths(tmp_path); old48=p.home/".hermes/runtime/tokmo-v3/48h-shadow/keep"; old48.parent.mkdir(parents=True); old48.write_text("same")
    p.stdout_log.parent.mkdir(parents=True); p.stdout_log.write_bytes(b"x"*(1024*1024+1))
    permit_environment(monkeypatch)
    m.install(p,expected_commit="1"*40,dry_run=False)
    assert old48.read_text()=="same" and p.stdout_log.with_suffix(".log.1").exists()
    assert not list(p.runtime_root.glob("*.tmp*"))

def test_online_doctor_only_readonly_print(tmp_path,monkeypatch):
    p=paths(tmp_path); p.runtime_root.mkdir(parents=True); p.config.write_text(json.dumps({"policy":"PAUSED"}))
    calls=[]; monkeypatch.setattr(m.subprocess,"run",lambda command,**kwargs:calls.append(command) or type("R",(),{"returncode":0,"stdout":b""})())
    m.doctor(p,offline=False)
    assert calls[-1]==["/bin/launchctl","print",f"gui/{os.getuid()}/{m.AGENT_LABEL}"]
    forbidden={"bootstrap","bootout","kickstart","enable","disable","load","unload"}
    assert not forbidden.intersection(calls[0])

def test_installer_cli_enable_pause_roundtrip_and_launchd_zero(tmp_path,monkeypatch):
    p=paths(tmp_path); permit_environment(monkeypatch); m.install(p,expected_commit="1"*40,dry_run=False); calls=[]
    def run(command,**kwargs): calls.append(command); return type("R",(),{"returncode":0,"stdout":b"1"*40+b"\n"})()
    monkeypatch.setattr(m.subprocess,"run",run)
    assert m.main(["--enable-shadow","--home",str(p.home),"--project-root",str(p.project_root),"--expected-commit","1"*40])==0
    assert json.loads(p.config.read_text())["policy"]=="SHADOW"
    assert m.main(["--pause","--home",str(p.home),"--project-root",str(p.project_root),"--expected-commit","1"*40])==0
    assert json.loads(p.config.read_text())["policy"]=="PAUSED"
    assert len(calls)==1 and calls[0][:3]==["git","-C",str(p.project_root)]
    assert not any("launchctl" in " ".join(command) for command in calls)

def test_enable_shadow_preserves_fred_enrichment(tmp_path,monkeypatch):
    p=paths(tmp_path); permit_environment(monkeypatch)
    m.install(p,expected_commit="1"*40,dry_run=False)
    value=json.loads(p.config.read_text()); value["fred_enrichment"]=True
    p.config.write_text(json.dumps(value,sort_keys=True)+"\n")
    assert m.main(["--enable-shadow","--home",str(p.home),"--project-root",str(p.project_root),"--expected-commit","1"*40])==0
    enabled=json.loads(p.config.read_text())
    assert enabled["enabled"] is True and enabled["policy"]=="SHADOW"
    assert enabled["fred_enrichment"] is True

def test_enable_shadow_revalidates_runtime_executable_before_arming(tmp_path,monkeypatch):
    p=paths(tmp_path); permit_environment(monkeypatch)
    m.install(p,expected_commit="1"*40,dry_run=False)
    outside=tmp_path/"replacement-runner"; outside.write_text("x")
    p.runner.unlink(); p.runner.symlink_to(outside)
    calls=[]
    def run(command,**kwargs):
        calls.append((command,kwargs))
        return type("R",(),{"returncode":0,"stdout":b"1"*40+b"\n"})()
    monkeypatch.setattr(m.subprocess,"run",run)
    assert m.main(["--enable-shadow","--home",str(p.home),"--project-root",str(p.project_root),"--expected-commit","1"*40])==1
    assert json.loads(p.config.read_text())["policy"]=="PAUSED"
    assert not any("launchctl" in " ".join(command) for command,_ in calls)

def test_enable_calls_activation_preconditions_but_pause_bypasses_them(tmp_path,monkeypatch):
    p=paths(tmp_path); permit_environment(monkeypatch)
    m.install(p,expected_commit="1"*40,dry_run=False)
    checked=[]
    monkeypatch.setattr(m,"validate_activation_preconditions",
                        lambda actual: checked.append(actual))
    assert m.main(["--enable-shadow","--home",str(p.home),"--project-root",str(p.project_root),"--expected-commit","1"*40])==0
    assert checked==[p]
    checked.clear()
    assert m.main(["--pause","--home",str(p.home),"--project-root",str(p.project_root)])==0
    assert checked==[]

def test_pause_and_repin_recover_without_reinstall(tmp_path,monkeypatch):
    p=paths(tmp_path); permit_environment(monkeypatch)
    m.install(p,expected_commit="1"*40,dry_run=False)
    value=json.loads(p.config.read_text()); value.update(enabled=True,policy="SHADOW",fred_enrichment=True,expected_candidate_sha256="0"*64)
    p.config.write_text(json.dumps(value,sort_keys=True)+"\n")
    assert m.main(["--pause","--home",str(p.home),"--project-root",str(p.project_root)])==0
    paused=json.loads(p.config.read_text())
    assert paused["policy"]=="PAUSED" and paused["fred_enrichment"] is True
    assert m.main(["--repin","--home",str(p.home),"--project-root",str(p.project_root),"--expected-commit","1"*40])==0
    repinned=json.loads(p.config.read_text())
    assert repinned["policy"]=="PAUSED" and repinned["fred_enrichment"] is True
    assert repinned["expected_candidate_sha256"] != "0"*64


# --- v0.3: FRED config keys + fail-safe reinstall reset (D3/M4) ---

def test_generate_config_has_inert_fred_keys(tmp_path,monkeypatch):
    p=paths(tmp_path); permit_environment(monkeypatch)
    cfg=m.generate_config(p,expected_commit="1"*40)
    assert cfg["fred_enrichment"] is False
    assert isinstance(cfg["fred_root"],str) and "fred-ondemand" in cfg["fred_root"]
    assert isinstance(cfg["fred_credential"],str) and "fred.env" in cfg["fred_credential"]

def test_install_produces_fred_keys_paused(tmp_path,monkeypatch):
    p=paths(tmp_path); permit_environment(monkeypatch)
    m.install(p,expected_commit="1"*40,dry_run=False)
    cfg=json.loads(p.config.read_text())
    assert cfg["fred_enrichment"] is False
    assert cfg["enabled"] is False and cfg["policy"]=="PAUSED"

def test_reinstall_resets_to_paused_fred_disabled(tmp_path,monkeypatch):
    """D3: --install intentionally resets the entire config."""
    p=paths(tmp_path); permit_environment(monkeypatch)
    m.install(p,expected_commit="1"*40,dry_run=False)
    # Simulate operator enabling FRED + shadow
    cfg=json.loads(p.config.read_text())
    cfg.update(enabled=True,policy="SHADOW",fred_enrichment=True)
    p.config.write_text(json.dumps(cfg,sort_keys=True)+"\n")
    assert json.loads(p.config.read_text())["fred_enrichment"] is True
    # Reinstall resets everything (stub returns matching commit)
    def run2(command,**kwargs): return type("R",(),{"returncode":0,"stdout":b"2"*40+b"\n"})()
    monkeypatch.setattr(m.subprocess,"run",run2)
    m.install(p,expected_commit="2"*40,dry_run=False)
    cfg2=json.loads(p.config.read_text())
    assert cfg2["enabled"] is False
    assert cfg2["policy"]=="PAUSED"
    assert cfg2["fred_enrichment"] is False
    assert cfg2["expected_commit"]=="2"*40
