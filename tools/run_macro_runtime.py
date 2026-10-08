"""Network-zero controls and the sole composition seam for macro runtime."""
import argparse, hashlib, json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT/"src") not in sys.path: sys.path.insert(0,str(PROJECT_ROOT/"src"))

SESSION_AUTHORITY_REASONS = frozenset({
    "aware now required",
    "session authority unsafe",
    "calendar authority unavailable",
    "regular session unavailable",
    "regular session incomplete",
    "history authority mismatch",
})
RUNTIME_CANDIDATE_PATHS = (
    "src/tokmo/programs/macro_fred_inputs.py",
    "src/tokmo/values/fred_query.py",
    "tools/install_macro_runtime_agent.py",
    "tools/run_macro_runtime.py",
)

def build_runtime_deps(config):
    from tokmo.composition import build_macro_runtime_deps
    closed=dict(config)
    if closed.get("enabled") and closed.get("policy")=="SHADOW":
        from tokmo.programs.macro_canary_adapter import resolve_completed_session
        from tokmo.records.calendar_cache import CalendarCacheStore
        from tokmo.records.regular_session_history import RegularSessionHistoryStore
        from tokmo.runtime.config import load_toss_config
        now=datetime.now(timezone.utc); ops=Path.home()/".local/share/tokmo-v3/ops"
        history=RegularSessionHistoryStore(ops/"regular_session_history.sqlite3",clock=lambda:now)
        cache=CalendarCacheStore(ops/"calendar_cache.json",clock=lambda:now)
        try: completed=resolve_completed_session(history,cache,now)
        except (OSError,ValueError) as error:
            completed=None
            reason=(str(error) if type(error) is ValueError
                    and str(error) in SESSION_AUTHORITY_REASONS
                    else "session authority unclassified")
            print("session-authority: "+reason,file=sys.stderr)
        closed.update(scheduled_at=now,completed_session=completed,clock=lambda:datetime.now(timezone.utc),session_authorities=(history,cache))
        if completed is not None:
            toss=load_toss_config(PROJECT_ROOT/".env.toss"); closed["credentials"]=(toss.client_id,toss.client_secret)
        if closed.get("fred_enrichment") is True and completed is not None:
            from tokmo.facts.fred_live import FredLiveTransport, build_live_opener, load_fred_api_key
            from tokmo.fred_query_composition import build_macro_fred_caller_v1, build_production_fred_query_deps_v1
            from tokmo.records.fred_schedule_sidecar import SIDECAR_NAME
            fred_root=Path(closed["fred_root"])
            credential=Path(closed["fred_credential"])
            closed["fred_caller"]=build_macro_fred_caller_v1(
                build_production_fred_query_deps_v1(
                    root=fred_root, sidecar_path=fred_root / SIDECAR_NAME,
                    transport=FredLiveTransport(opener=build_live_opener()),
                    api_key_provider=lambda: load_fred_api_key(credential),
                    clock=closed["clock"]))
    return build_macro_runtime_deps(closed)

def invoke_macro_runtime(deps):
    from tokmo.composition import macro_runtime_main
    return macro_runtime_main(deps)

def main(argv=None):
    parser=argparse.ArgumentParser(prog="tokmo-macro-runtime")
    parser.add_argument("--config"); parser.add_argument("--preflight",action="store_true"); parser.add_argument("--paused",action="store_true")
    try: args,unknown=parser.parse_known_args(argv)
    except SystemExit as error: return 0 if error.code==0 else 1
    if unknown: return 1
    if args.preflight: return 0
    if not args.config: return 1
    try: config=json.loads(Path(args.config).read_text())
    except (OSError,ValueError): return 1
    if args.paused or not config.get("enabled") or config.get("policy")=="PAUSED": return 0
    try:
        expected=config.get("expected_commit")
        expected_candidate=config.get("expected_candidate_sha256")
        if (not isinstance(expected,str) or len(expected)!=40
                or not isinstance(expected_candidate,str) or len(expected_candidate)!=64):
            print("commit-pin: mismatch",file=sys.stderr)
            return 0
        scoped=subprocess.run(
            ["git","-C",str(PROJECT_ROOT),"diff","--binary",expected,"--",
             *RUNTIME_CANDIDATE_PATHS], capture_output=True,
            check=False,timeout=5)
        observed=hashlib.sha256(scoped.stdout).hexdigest()
        if scoped.returncode!=0 or observed!=expected_candidate:
            print("commit-pin: mismatch",file=sys.stderr)
            return 0
    except (OSError,subprocess.TimeoutExpired,TypeError):
        print("commit-pin: unavailable",file=sys.stderr)
        return 0
    try: return invoke_macro_runtime(build_runtime_deps(config))
    except Exception: return 1

if __name__=="__main__": raise SystemExit(main())
