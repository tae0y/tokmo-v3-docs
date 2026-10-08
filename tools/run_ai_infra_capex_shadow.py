"""Production-only composition root for the G1-C shadow runtime."""
from __future__ import annotations
import argparse,json,os,subprocess,sys
from datetime import datetime,timezone
from pathlib import Path

PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT/"src") not in sys.path: sys.path.insert(0,str(PROJECT_ROOT/"src"))
class _SilentParser(argparse.ArgumentParser):
    def error(self,message): raise ValueError("invalid arguments")

def build_deps(config):
    from tokmo.programs.ai_infra_capex_allocation import evaluate_ai_infra_capex_allocation_v1
    from tokmo.programs.ai_infra_capex_shadow_runtime import BUNDLE_MEMBERS_V1,ShadowRuntimeDepsV1
    from tokmo.records.ai_infra_capex_shadow_receipts import ShadowReceiptStoreV1
    from tokmo.records.external_news_query import query_local_news_v1
    root=Path.home()/".local/share/tokmo-v3/ai-infra-capex-g1c-shadow"
    def authority():
        if os.environ.get("TOKMO_G1C_LAUNCHD")!="1" or os.getppid()!=1:return False
        result=subprocess.run(["/bin/launchctl","print",f"gui/{os.getuid()}/com.woongs.tokmo-v3-ai-infra-capex-g1c-shadow"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
        return result.returncode==0
    return ShadowRuntimeDepsV1(clock=lambda:datetime.now(timezone.utc),scheduler_authority=authority,query_local_news=query_local_news_v1,evaluate=evaluate_ai_infra_capex_allocation_v1,receipts=ShadowReceiptStoreV1(root/"receipts",root/"runtime.lock"),bundle_paths={name:PROJECT_ROOT/name for name in BUNDLE_MEMBERS_V1})
def invoke(config):
    from tokmo.programs.ai_infra_capex_shadow_runtime import run_ai_infra_capex_shadow_once_v1
    return run_ai_infra_capex_shadow_once_v1(config,build_deps(config))
def main(argv=None):
    parser=_SilentParser(add_help=False); parser.add_argument("--config"); parser.add_argument("--preflight",action="store_true")
    try: args,unknown=parser.parse_known_args(argv)
    except (SystemExit,ValueError):return 1
    if unknown:return 1
    if args.preflight:return 0
    if not args.config:return 1
    try: value=json.loads(Path(args.config).read_text())
    except (OSError,ValueError):return 1
    try:return 0 if invoke(value)==0 else 1
    except Exception:return 1
if __name__=="__main__": raise SystemExit(main())
