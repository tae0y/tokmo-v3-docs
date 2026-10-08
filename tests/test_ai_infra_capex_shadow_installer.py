import importlib.util, json, os, plistlib, stat
from datetime import datetime, timezone
from pathlib import Path

import pytest


P=Path(__file__).parents[1]/"tools/install_ai_infra_capex_shadow_agent.py"
spec=importlib.util.spec_from_file_location("install_ai_infra_capex_shadow_agent",P); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


def paths(tmp_path):
    project=tmp_path/"project"; (project/".venv/bin").mkdir(parents=True); (project/"tools").mkdir(); (project/"src/tokmo/programs").mkdir(parents=True); (project/"src/tokmo/records").mkdir(); (project/"src/tokmo/values").mkdir()
    for rel in m.BUNDLE_MEMBERS_V1:
        path=project/rel; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(rel)
    (project/"tools/install_ai_infra_capex_shadow_agent.py").write_bytes(P.read_bytes())
    py=project/".venv/bin/python"; py.write_text("x"); py.chmod(0o700)
    db=tmp_path/"home/.local/share/tokmo-v3/news/external_news_v4.sqlite3"; db.parent.mkdir(parents=True); db.write_bytes(b"db"); db.chmod(0o600)
    return m.resolve_paths(tmp_path/"home",project)


def runner(command,**kwargs):
    if command[:2]==["git","-C"] and command[3]=="rev-parse": return type("R",(),{"returncode":0,"stdout":"a"*40+"\n"})()
    if command[:2]==["git","-C"] and command[3]=="show":
        rel=command[4].split(":",1)[1].removeprefix("./"); return type("R",(),{"returncode":0,"stdout":(Path(command[2])/rel).read_bytes()})()
    return type("R",(),{"returncode":0,"stdout":""})()


def test_paused_install_plist_modes_and_no_launchctl(tmp_path,monkeypatch):
    p=paths(tmp_path); calls=[]
    monkeypatch.setattr(m.subprocess,"run",lambda command,**kwargs:calls.append(command) or runner(command,**kwargs))
    m.install(p,expected_commit="a"*40,dry_run=False,timezone_name="Asia/Seoul")
    cfg=json.loads(p.config.read_text()); assert cfg["policy"]=="PAUSED" and cfg["activation_utc"] is None
    assert len(cfg["runtime_bundle_digest"])==64
    pl=plistlib.loads(p.plist.read_bytes()); assert pl["Label"]==m.AGENT_LABEL and pl["StartCalendarInterval"]=={"Minute":12}
    assert pl["StandardOutPath"]==pl["StandardErrorPath"]=="/dev/null"
    assert stat.S_IMODE(p.config.stat().st_mode)==0o600 and stat.S_IMODE(p.wrapper.stat().st_mode)==0o700
    assert not any("launchctl" in " ".join(c) for c in calls)


def test_enable_timestamps_once_failure_pauses_and_no_live_calls(tmp_path,monkeypatch):
    p=paths(tmp_path); monkeypatch.setattr(m.subprocess,"run",runner); m.install(p,expected_commit="a"*40,dry_run=False,timezone_name="Asia/Seoul")
    calls=[]; now=datetime(2026,8,9,4,0,tzinfo=timezone.utc)
    assert m.enable_shadow(p,"a"*40,clock=lambda:now,launchctl=lambda command:calls.append(command) or 1,timezone_name="Asia/Seoul")==1
    cfg=json.loads(p.config.read_text()); assert cfg["policy"]=="PAUSED" and cfg["activation_utc"]=="2026-08-09T04:00:00.000000Z"
    assert cfg["review_due_utc"]=="2026-09-23T04:00:00.000000Z" and cfg["expiry_utc"]=="2026-12-07T04:00:00.000000Z"
    first=(cfg["activation_utc"],cfg["review_due_utc"],cfg["expiry_utc"])
    assert m.enable_shadow(p,"a"*40,clock=lambda:datetime(2026,8,10,tzinfo=timezone.utc),launchctl=lambda command:0,acceptance=lambda paths,before:True,timezone_name="Asia/Seoul")==0
    cfg=json.loads(p.config.read_text()); assert (cfg["activation_utc"],cfg["review_due_utc"],cfg["expiry_utc"])==first


def test_kickstart_without_exact_success_receipt_pauses(tmp_path,monkeypatch):
    p=paths(tmp_path); monkeypatch.setattr(m.subprocess,"run",runner); m.install(p,expected_commit="a"*40,dry_run=False,timezone_name="Asia/Seoul")
    assert m.enable_shadow(p,"a"*40,clock=lambda:datetime(2026,8,9,4,tzinfo=timezone.utc),launchctl=lambda command:0,acceptance=lambda paths,before:False,timezone_name="Asia/Seoul")==1
    assert json.loads(p.config.read_text())["policy"]=="PAUSED"


def _receipt(state="SUCCEEDED",**overrides):
    row={"state":state,"status":"OK","query_count":1,"http_starts":0,"model_starts":0,"delivery_count":0,"news_write_count":0,"retry_count":0}
    row.update(overrides); return row


def _atomic_receipt(path,row):
    tmp=path.with_name("."+path.name+".tmp"); tmp.write_text(json.dumps(row)); tmp.chmod(0o600); tmp.replace(path)


def test_activation_acceptance_waits_for_reserved_to_settle(tmp_path):
    p=paths(tmp_path); p.receipt_root.mkdir(parents=True); p.receipt_root.chmod(0o700); receipt=p.receipt_root/("a"*64+".json"); stage=0; now=0.0
    def monotonic():return now
    def sleeper(interval):
        nonlocal stage,now
        stage+=1; now+=interval
        _atomic_receipt(receipt,_receipt(state="RESERVED",status=None) if stage==1 else _receipt())
    assert m._activation_acceptance(p,set(),deadline_seconds=1,poll_interval=.1,monotonic=monotonic,sleeper=sleeper) is True
    assert stage==2


def test_actual_activation_acceptance_exact_success_and_failures(tmp_path):
    p=paths(tmp_path); p.receipt_root.mkdir(parents=True)
    good=p.receipt_root/"a.json"; good.write_text(json.dumps(_receipt()))
    assert m._activation_acceptance(p,set(),deadline_seconds=0,poll_interval=0,monotonic=lambda:0,sleeper=lambda _:None) is True
    good.write_text(json.dumps(_receipt(state="FAILED")))
    assert m._activation_acceptance(p,set(),deadline_seconds=0,poll_interval=0,monotonic=lambda:0,sleeper=lambda _:None) is False
    good.write_text("{")
    assert m._activation_acceptance(p,set(),deadline_seconds=0,poll_interval=0,monotonic=lambda:0,sleeper=lambda _:None) is False
    good.unlink()
    assert m._activation_acceptance(p,set(),deadline_seconds=0,poll_interval=0,monotonic=lambda:0,sleeper=lambda _:None) is False


@pytest.mark.parametrize("state",["FAILED","UNKNOWN","EXPIRED"])
def test_actual_activation_acceptance_rejects_terminal_non_success(tmp_path,state):
    p=paths(tmp_path); p.receipt_root.mkdir(parents=True); (p.receipt_root/"a.json").write_text(json.dumps(_receipt(state=state)))
    assert m._activation_acceptance(p,set(),deadline_seconds=1,poll_interval=0,monotonic=lambda:0,sleeper=lambda _:None) is False


def test_actual_activation_acceptance_reserved_never_settles_by_deadline(tmp_path):
    p=paths(tmp_path); p.receipt_root.mkdir(parents=True); (p.receipt_root/"a.json").write_text(json.dumps(_receipt(state="RESERVED",status=None)))
    ticks=iter((0.0,0.0,1.0))
    assert m._activation_acceptance(p,set(),deadline_seconds=1,poll_interval=.1,monotonic=lambda:next(ticks),sleeper=lambda _:None) is False


@pytest.mark.parametrize("counter",["query_count","http_starts","model_starts","delivery_count","news_write_count","retry_count"])
@pytest.mark.parametrize("bad",[None,True,False])
def test_actual_activation_acceptance_rejects_missing_or_bool_counter(tmp_path,counter,bad):
    p=paths(tmp_path); p.receipt_root.mkdir(parents=True); row=_receipt()
    if bad is None:row.pop(counter)
    else:row[counter]=bad
    (p.receipt_root/"a.json").write_text(json.dumps(row))
    assert m._activation_acceptance(p,set(),deadline_seconds=0,poll_interval=0,monotonic=lambda:0,sleeper=lambda _:None) is False


def test_activation_acceptance_fails_if_reserved_path_set_changes(tmp_path):
    p=paths(tmp_path); p.receipt_root.mkdir(parents=True); first=p.receipt_root/"a.json"; second=p.receipt_root/"b.json"; _atomic_receipt(first,_receipt(state="RESERVED",status=None)); now=0.0
    def monotonic():return now
    def sleeper(interval):
        nonlocal now
        now+=interval; first.unlink(); _atomic_receipt(second,_receipt())
    assert m._activation_acceptance(p,set(),deadline_seconds=1,poll_interval=.1,monotonic=monotonic,sleeper=sleeper) is False


@pytest.mark.parametrize("counter",["http_starts","model_starts","delivery_count","news_write_count","retry_count"])
def test_actual_activation_acceptance_rejects_each_nonzero_cap(tmp_path,counter):
    p=paths(tmp_path); p.receipt_root.mkdir(parents=True); (p.receipt_root/"a.json").write_text(json.dumps(_receipt(**{counter:1})))
    assert m._activation_acceptance(p,set(),deadline_seconds=0,poll_interval=0,monotonic=lambda:0,sleeper=lambda _:None) is False


@pytest.mark.parametrize("query_count",[0,2])
def test_actual_activation_acceptance_rejects_wrong_integer_query_count(tmp_path,query_count):
    p=paths(tmp_path); p.receipt_root.mkdir(parents=True); (p.receipt_root/"a.json").write_text(json.dumps(_receipt(query_count=query_count)))
    assert m._activation_acceptance(p,set(),deadline_seconds=0,poll_interval=0,monotonic=lambda:0,sleeper=lambda _:None) is False


@pytest.mark.parametrize("field",["deadline_seconds","poll_interval"])
@pytest.mark.parametrize("value",[float("nan"),float("inf"),float("-inf")])
def test_actual_activation_acceptance_rejects_nonfinite_timing(field,value,tmp_path):
    p=paths(tmp_path); p.receipt_root.mkdir(parents=True); kwargs={field:value}
    assert m._activation_acceptance(p,set(),monotonic=lambda:0,sleeper=lambda _:None,**kwargs) is False


def test_actual_activation_acceptance_rejects_two_receipts_and_unknown(tmp_path):
    p=paths(tmp_path); p.receipt_root.mkdir(parents=True)
    (p.receipt_root/"a.json").write_text(json.dumps(_receipt())); (p.receipt_root/"b.json").write_text(json.dumps(_receipt()))
    assert m._activation_acceptance(p,set(),deadline_seconds=0,poll_interval=0,monotonic=lambda:0,sleeper=lambda _:None) is False
    (p.receipt_root/"b.json").unlink(); (p.receipt_root/"a.json").write_text(json.dumps(_receipt(status="UNKNOWN")))
    assert m._activation_acceptance(p,set(),deadline_seconds=0,poll_interval=0,monotonic=lambda:0,sleeper=lambda _:None) is False


def test_reviewed_bytes_mismatch_fails_install_preflight(tmp_path,monkeypatch):
    p=paths(tmp_path); monkeypatch.setattr(m.subprocess,"run",runner); m.install(p,expected_commit="a"*40,dry_run=False,timezone_name="Asia/Seoul")
    def mismatch(command,**kwargs):
        if command[3]=="rev-parse":return type("R",(),{"returncode":0,"stdout":"a"*40+"\n"})()
        rel=command[4].split(":",1)[1].removeprefix("./")
        data=b"not-reviewed-bytes" if rel==m.BUNDLE_MEMBERS_V1[0] else (Path(command[2])/rel).read_bytes()
        return type("R",(),{"returncode":0,"stdout":data})()
    monkeypatch.setattr(m.subprocess,"run",mismatch)
    with pytest.raises(ValueError,match="reviewed bytes"):
        m.install(p,expected_commit="a"*40,dry_run=True,timezone_name="Asia/Seoul")
    assert m.doctor(p,"a"*40,offline=True,timezone_name="Asia/Seoul")==1
    with pytest.raises(ValueError,match="reviewed bytes"):
        m.enable_shadow(p,"a"*40,launchctl=lambda command:0,acceptance=lambda paths,before:True,timezone_name="Asia/Seoul")


def test_reviewed_bytes_real_git_prefix_relative_for_every_member():
    root=Path(__file__).parents[1]; p=m.resolve_paths(root/"unused-home",root)
    import subprocess
    head=subprocess.check_output(["git","rev-parse","HEAD"],cwd=root,text=True).strip()
    for relative in m.REVIEWED_MEMBERS_V1:
        reviewed=m._reviewed_bytes(p,head,relative)
        modified=subprocess.run(["git","diff","--quiet","--",relative],cwd=root,check=False).returncode!=0
        expected=subprocess.check_output(["git","cat-file","blob",f"{head}:./{relative}"],cwd=root) if modified else (root/relative).read_bytes()
        assert reviewed.returncode==0 and reviewed.stdout==expected


@pytest.mark.parametrize("unsafe",["/absolute.py","../escape.py","src/../escape.py","not-allowed.py"])
def test_reviewed_bytes_rejects_unsafe_or_unlisted_name(tmp_path,monkeypatch,unsafe):
    p=paths(tmp_path); calls=[]; monkeypatch.setattr(m.subprocess,"run",lambda *a,**k:calls.append(a) or None)
    with pytest.raises(ValueError,match="reviewed member"):
        m._reviewed_bytes(p,"a"*40,unsafe)
    assert calls==[]


def test_activation_operational_caution_is_module_help():
    help_text=m.__doc__ or ""
    assert "KST [12,17)" in help_text and "consumes the hour identity" in help_text


def test_installer_malformed_args_are_silent(capsys):
    assert m.main(["--expected-commit"])==1
    assert capsys.readouterr()==("","")


def test_commit_mismatch_unsafe_db_doctor_and_uninstall_preserves_receipts(tmp_path,monkeypatch):
    p=paths(tmp_path); monkeypatch.setattr(m.subprocess,"run",runner); m.install(p,expected_commit="a"*40,dry_run=False,timezone_name="Asia/Seoul")
    assert m.doctor(p,"a"*40,offline=True,timezone_name="Asia/Seoul")==0
    marker=p.receipt_root/"keep.json"; marker.parent.mkdir(parents=True,exist_ok=True); marker.write_text("x")
    m.uninstall(p); assert marker.exists()
    monkeypatch.setattr(m.subprocess,"run",lambda *a,**k:type("R",(),{"returncode":0,"stdout":"b"*40+"\n"})())
    with pytest.raises(ValueError): m.install(paths(tmp_path/"two"),expected_commit="a"*40,dry_run=True,timezone_name="Asia/Seoul")


def test_exact_eight_architecture_and_forbidden_graph():
    root=Path(__file__).parents[1]
    expected={
      "src/tokmo/programs/ai_infra_capex_shadow_runtime.py","src/tokmo/records/ai_infra_capex_shadow_receipts.py",
      "tools/run_ai_infra_capex_shadow.py","tools/install_ai_infra_capex_shadow_agent.py",
      "tests/test_ai_infra_capex_shadow_runtime.py","tests/test_ai_infra_capex_shadow_receipts.py",
      "tests/test_ai_infra_capex_shadow_cli.py","tests/test_ai_infra_capex_shadow_installer.py"}
    import subprocess
    status=set(subprocess.check_output(["git","status","--short"],cwd=root,text=True).splitlines())
    created={line[3:] for line in status if line.startswith("?? ") and line[3:] in expected}
    # Historical seal: the G1-C shadow lane closed at 9e610b3.  Later reviewed
    # modules must not make this exact-eight proof compare against moving HEAD.
    shadow_closure="9e610b3f563329b0d512622e4b6a1e129e4be90b"
    assert subprocess.run(["git","merge-base","--is-ancestor",shadow_closure,"HEAD"],cwd=root,check=False).returncode==0
    committed={p for p in subprocess.check_output(["git","diff","--relative","--name-only",f"ecacbd6a250a1f4daf815d74702269cba863cef0..{shadow_closure}"],cwd=root,text=True).splitlines() if not p.startswith(".ai/")}
    working={p for p in subprocess.check_output(["git","diff","--relative","--name-only","--",*sorted(expected)],cwd=root,text=True).splitlines() if not p.startswith(".ai/")}
    assert created|committed|working==expected
    for rel in {x for x in expected if not x.startswith("tests/")}:
        text=(root/rel).read_text().lower()
        assert not any(word in text for word in ("requests", "urllib.request", "telegram", "create_order", "account_seq"))
