"""Paused-first G1-C shadow installer.

Activation must run during KST [12,17) for V3 acceptance. A manual in-window
operational invocation consumes the hour identity; it is not a harmless probe.
"""
from __future__ import annotations
import argparse,json,math,os,plistlib,subprocess,sys,time
from collections import namedtuple
from datetime import datetime,timedelta,timezone
from pathlib import Path

PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT/"src") not in sys.path: sys.path.insert(0,str(PROJECT_ROOT/"src"))
from tokmo.programs.ai_infra_capex_shadow_runtime import BUNDLE_MEMBERS_V1,canonical_path_identity_digest_v1,runtime_bundle_digest_v1
from tokmo.values.ai_infra_capex import CLASSIFIER_TABLE_DIGEST_V1,SPEC_DIGEST_V1

AGENT_LABEL="com.woongs.tokmo-v3-ai-infra-capex-g1c-shadow"
REVIEWED_MEMBERS_V1=(*BUNDLE_MEMBERS_V1,"tools/install_ai_infra_capex_shadow_agent.py")
class _SilentParser(argparse.ArgumentParser):
    def error(self,message):raise ValueError("invalid arguments")
AgentPathsV1=namedtuple("AgentPathsV1","home project_root runtime_root wrapper config state_root receipt_root lock plist venv_python runner canonical_news")
def resolve_paths(home=None,project_root=None):
    home=Path.home() if home is None else Path(home); project=PROJECT_ROOT if project_root is None else Path(project_root); runtime=project/"var/runtime/ai-infra-capex-g1c-shadow"; state=(home/".local/share/tokmo-v3").resolve()/"ai-infra-capex-g1c-shadow"  # relocated (2026-09-24)
    return AgentPathsV1(home,project,runtime,runtime/"run_ai_infra_capex_shadow.zsh",runtime/"config.json",state,state/"receipts",state/"runtime.lock",home/"Library/LaunchAgents"/f"{AGENT_LABEL}.plist",project/".venv/bin/python",project/"tools/run_ai_infra_capex_shadow.py",(home/".local/share/tokmo-v3").resolve()/"news/external_news_v4.sqlite3")
def _fmt(value):return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
def _git_head(paths):return subprocess.run(["git","-C",str(paths.project_root),"rev-parse","HEAD"],capture_output=True,text=True,check=False)
def _reviewed_bytes(paths,expected_commit,relative):
    if type(relative) is not str or relative not in REVIEWED_MEMBERS_V1 or Path(relative).is_absolute() or ".." in Path(relative).parts:raise ValueError("reviewed member")
    return subprocess.run(["git","-C",str(paths.project_root),"show",f"{expected_commit}:./{relative}"],capture_output=True,check=False)
def _timezone_ok(name=None):
    if name is not None:return name=="Asia/Seoul"
    path=Path("/etc/localtime")
    return path.is_symlink() and path.resolve(strict=True).parts[-3:]==("zoneinfo","Asia","Seoul")
def _bundle(paths):return {name:paths.project_root/name for name in BUNDLE_MEMBERS_V1}
def _validate(paths,expected_commit,timezone_name=None,*,commit=True):
    if len(expected_commit)!=40 or not _timezone_ok(timezone_name):raise ValueError("preflight")
    if commit:
        result=_git_head(paths)
        if result.returncode or result.stdout.strip()!=expected_commit:raise ValueError("commit")
        for relative in REVIEWED_MEMBERS_V1:
            reviewed=_reviewed_bytes(paths,expected_commit,relative); current=(paths.project_root/relative).read_bytes()
            if reviewed.returncode or reviewed.stdout!=current:raise ValueError("reviewed bytes")
    for path,executable in ((paths.venv_python,True),(paths.runner,False)):
        resolved=path.resolve(strict=True)
        if not resolved.is_file() or resolved.stat().st_uid!=os.getuid() or (executable and not os.access(resolved,os.X_OK)):raise ValueError("executable")
    canonical_path_identity_digest_v1(paths.canonical_news)
    return runtime_bundle_digest_v1(_bundle(paths))
def _wrapper(paths):return f'#!/bin/zsh\nset -u\nexport TOKMO_G1C_LAUNCHD=1\nexport PYTHONPATH="{paths.project_root/"src"}"\nexec "{paths.venv_python}" "{paths.runner}" --config "{paths.config}"\n'
def _plist(paths):return plistlib.dumps({"Label":AGENT_LABEL,"ProgramArguments":[str(paths.wrapper)],"StartCalendarInterval":{"Minute":12},"StandardOutPath":"/dev/null","StandardErrorPath":"/dev/null","WorkingDirectory":str(paths.project_root),"EnvironmentVariables":{"TOKMO_G1C_LAUNCHD":"1"}})
def _config(paths,expected_commit,bundle):return {"schema":"tokmo.g1c.shadow-config.v1","enabled":False,"policy":"PAUSED","project_root":str(paths.project_root),"runner_path":str(paths.runner),"venv_python":str(paths.venv_python),"canonical_news_path":str(paths.canonical_news.resolve()),"canonical_path_identity_digest":canonical_path_identity_digest_v1(paths.canonical_news),"expected_commit":expected_commit,"spec_digest":SPEC_DIGEST_V1,"classifier_digest":CLASSIFIER_TABLE_DIGEST_V1,"runtime_bundle_digest":bundle,"cadence_version":"g1c-hourly-minute12-kst-v1","minute_kst":12,"activation_utc":None,"review_due_utc":None,"expiry_utc":None,"review_days":45,"expiry_days":120,"query_policy":"STRICT_RO_V1","http_start_cap":0,"model_start_cap":0,"retry_cap":0,"query_cap":1,"delivery_cap":0,"news_write_cap":0}
def _atomic(path,data,mode):
    if path.is_symlink() or (path.exists() and path.stat().st_uid!=os.getuid()):raise ValueError("unsafe artifact")
    tmp=path.with_name(f".{path.name}.{os.getpid()}.tmp"); fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,"wb",closefd=False) as out:out.write(data);out.flush();os.fsync(out.fileno())
        os.close(fd);fd=-1;os.chmod(tmp,mode);os.replace(tmp,path);parent=os.open(path.parent,os.O_RDONLY);os.fsync(parent);os.close(parent)
    finally:
        if fd>=0:os.close(fd)
        tmp.unlink(missing_ok=True)
def install(paths,*,expected_commit,dry_run,timezone_name=None):
    bundle=_validate(paths,expected_commit,timezone_name)
    artifacts=((paths.wrapper,_wrapper(paths).encode(),0o700),(paths.config,(json.dumps(_config(paths,expected_commit,bundle),sort_keys=True)+"\n").encode(),0o600),(paths.plist,_plist(paths),0o600))
    if dry_run:return [str(path) for path,_,_ in artifacts]
    if paths.plist.exists() and (paths.plist.is_symlink() or plistlib.loads(paths.plist.read_bytes()).get("Label")!=AGENT_LABEL):raise ValueError("unowned plist")
    for directory in (paths.runtime_root,paths.state_root,paths.receipt_root,paths.plist.parent):
        if directory.exists() and directory.is_symlink():raise ValueError("directory symlink")
        directory.mkdir(parents=True,exist_ok=True)
        if directory!=paths.plist.parent:os.chmod(directory,0o700)
    for path,data,mode in artifacts:_atomic(path,data,mode)
    return [str(x[0]) for x in artifacts]
def doctor(paths,expected_commit,*,offline=False,timezone_name=None):
    try:
        bundle=_validate(paths,expected_commit,timezone_name); cfg=json.loads(paths.config.read_text())
        if cfg["runtime_bundle_digest"]!=bundle or paths.wrapper.read_bytes()!=_wrapper(paths).encode() or plistlib.loads(paths.plist.read_bytes())!=plistlib.loads(_plist(paths)):return 1
        if not offline:
            result=subprocess.run(["/bin/launchctl","print",f"gui/{os.getuid()}/{AGENT_LABEL}"],capture_output=True,check=False)
            if result.returncode:return 1
        return 0
    except (OSError,ValueError,KeyError):return 1
def _write_config(paths,value):_atomic(paths.config,(json.dumps(value,sort_keys=True)+"\n").encode(),0o600)
def pause(paths):
    value=json.loads(paths.config.read_text());value.update(enabled=False,policy="PAUSED");_write_config(paths,value);return 0
def _activation_acceptance(paths,before,*,deadline_seconds=10,poll_interval=.1,monotonic=time.monotonic,sleeper=time.sleep):
    if type(deadline_seconds) not in (int,float) or not math.isfinite(deadline_seconds) or deadline_seconds<0 or type(poll_interval) not in (int,float) or not math.isfinite(poll_interval) or poll_interval<0:return False
    deadline=monotonic()+deadline_seconds; selected=None
    while True:
        created=sorted(set(paths.receipt_root.glob("*.json"))-before)
        if len(created)==1:
            if selected is None:selected=created[0]
            elif created[0]!=selected:return False
            try: row=json.loads(selected.read_text())
            except (OSError,ValueError):return False
            if type(row) is not dict:return False
            state=row.get("state")
            if state=="SUCCEEDED":return row.get("status")=="OK" and type(row.get("query_count")) is int and row["query_count"]==1 and all(type(row.get(k)) is int and row[k]==0 for k in ("http_starts","model_starts","delivery_count","news_write_count","retry_count"))
            if state!="RESERVED":return False
        if len(created)>1:return False
        if selected is not None and created!=[selected]:return False
        if monotonic()>=deadline:return False
        sleeper(poll_interval)
def enable_shadow(paths,expected_commit,*,clock=lambda:datetime.now(timezone.utc),launchctl=None,acceptance=_activation_acceptance,timezone_name=None):
    value=json.loads(paths.config.read_text()); first=value["activation_utc"] is None
    _validate(paths,expected_commit,timezone_name,commit=first)
    if value["expected_commit"]!=expected_commit:raise ValueError("commit")
    if value["activation_utc"] is None:
        now=clock().astimezone(timezone.utc);value.update(activation_utc=_fmt(now),review_due_utc=_fmt(now+timedelta(days=45)),expiry_utc=_fmt(now+timedelta(days=120)))
    value.update(enabled=True,policy="SHADOW");_write_config(paths,value);before=set(paths.receipt_root.glob("*.json"))
    call=launchctl or (lambda command:subprocess.run(command,check=False).returncode)
    domain=f"gui/{os.getuid()}"; bootstrap=call(["/bin/launchctl","bootstrap",domain,str(paths.plist)])
    kick=call(["/bin/launchctl","kickstart","-k",f"{domain}/{AGENT_LABEL}"]) if bootstrap==0 else bootstrap
    if kick!=0 or not acceptance(paths,before):
        call(["/bin/launchctl","bootout",f"{domain}/{AGENT_LABEL}"]);pause(paths);return 1
    return 0
def uninstall(paths):
    if paths.plist.exists() and plistlib.loads(paths.plist.read_bytes()).get("Label")!=AGENT_LABEL:raise ValueError("unowned")
    for path in (paths.plist,paths.wrapper,paths.config):
        if path.exists():
            if path.is_symlink() or path.stat().st_uid!=os.getuid():raise ValueError("unowned")
            path.unlink()
    return 0
def main(argv=None):
    parser=_SilentParser(add_help=False)
    for name in ("dry-run","install","doctor","pause","enable-shadow","uninstall"):parser.add_argument("--"+name,action="store_true")
    parser.add_argument("--offline",action="store_true");parser.add_argument("--home");parser.add_argument("--project-root");parser.add_argument("--expected-commit")
    try:args,unknown=parser.parse_known_args(argv)
    except (SystemExit,ValueError):return 1
    actions=(args.dry_run,args.install,args.doctor,args.pause,args.enable_shadow,args.uninstall)
    if unknown or sum(actions)!=1:return 1
    p=resolve_paths(args.home,args.project_root)
    try:
        if args.dry_run or args.install:install(p,expected_commit=args.expected_commit or "",dry_run=args.dry_run);return 0
        if args.doctor:return doctor(p,args.expected_commit or "",offline=args.offline)
        if args.pause:return pause(p)
        if args.enable_shadow:return enable_shadow(p,args.expected_commit or "")
        return uninstall(p)
    except Exception:return 1
if __name__=="__main__":raise SystemExit(main())
