from datetime import date, datetime, timezone
import json
from pathlib import Path
from tokmo.programs.macro_runtime import (MacroRuntimePolicyV1, MacroRuntimeTriggerV1,
    eligible_completed_session, macro_dedupe_identity, run_macro_runtime_once)
from tokmo.records.macro_runtime_receipts import MacroRuntimeReceiptStore

NOW=datetime(2026,8,9,8,tzinfo=timezone.utc)
def test_paused_closed_replay_and_duplicate_start_zero(tmp_path):
    starts=[]; store=MacroRuntimeReceiptStore(tmp_path)
    for policy, trigger in [(MacroRuntimePolicyV1(False,"PAUSED"),MacroRuntimeTriggerV1(NOW,None)),
                            (MacroRuntimePolicyV1(True,"SHADOW"),MacroRuntimeTriggerV1(NOW,None)),
                            (MacroRuntimePolicyV1(True,"SHADOW"),MacroRuntimeTriggerV1(NOW,date(2026,8,7),replay=True))]:
        assert run_macro_runtime_once(policy,trigger,store,lambda: starts.append(1))["starts"]==0
    assert starts==[]

def test_post_close_four_then_duplicate_zero(tmp_path):
    store=MacroRuntimeReceiptStore(tmp_path); calls=[]
    p=MacroRuntimePolicyV1(True,"SHADOW"); t=MacroRuntimeTriggerV1(NOW,date(2026,8,7))
    one=run_macro_runtime_once(p,t,store,lambda: calls.append(1) or {"starts":4,"status":"FIRING_RATE_UNPROVEN"})
    two=run_macro_runtime_once(p,t,store,lambda: calls.append(1))
    assert one["starts"]==4 and two["starts"]==0 and len(calls)==1
    assert macro_dedupe_identity(t.completed_session)==one["identity"]

def test_replay_reads_terminal_receipt_reproduces_digest_and_starts_zero(tmp_path):
    store=MacroRuntimeReceiptStore(tmp_path); p=MacroRuntimePolicyV1(True,"SHADOW"); session=date(2026,8,7)
    live=run_macro_runtime_once(p,MacroRuntimeTriggerV1(NOW,session),store,lambda:{"starts":4,"status":"FIRING_RATE_UNPROVEN","evidence_digest":"a"*64})
    replay=run_macro_runtime_once(p,MacroRuntimeTriggerV1(NOW,session,replay=True),store,lambda:(_ for _ in ()).throw(AssertionError()))
    assert replay["starts"]==0 and replay["evidence_digest"]=="a"*64 and live["evidence_digest"]==replay["evidence_digest"]

def test_eligibility_requires_completed_not_future():
    assert eligible_completed_session(date(2026,8,7),NOW)
    assert not eligible_completed_session(date(2026,8,10),NOW)

def test_failure_receipt_is_terminal_and_never_rearms(tmp_path):
    store=MacroRuntimeReceiptStore(tmp_path); calls=[]
    p=MacroRuntimePolicyV1(True,"SHADOW"); t=MacroRuntimeTriggerV1(NOW,date(2026,8,7))
    assert run_macro_runtime_once(p,t,store,lambda: calls.append(1) or (_ for _ in ()).throw(RuntimeError()))["status"]=="FAILED"
    assert run_macro_runtime_once(p,t,store,lambda: calls.append(1))["starts"]==0 and calls==[1]

def test_global_pause_after_material_failure_blocks_later_session(tmp_path):
    store=MacroRuntimeReceiptStore(tmp_path); p=MacroRuntimePolicyV1(True,"SHADOW"); calls=[]
    run_macro_runtime_once(p,MacroRuntimeTriggerV1(NOW,date(2026,8,7)),store,lambda:{"starts":2,"status":"FAILED"})
    result=run_macro_runtime_once(p,MacroRuntimeTriggerV1(NOW,date(2026,8,8)),store,lambda:calls.append(1))
    assert result["status"]=="GLOBAL_PAUSED" and result["starts"]==0 and calls==[]

def test_composition_enabled_path_uses_bounded_injected_production_seam(tmp_path):
    from tokmo.composition import build_macro_runtime_deps, macro_runtime_main
    calls=[]
    config={"enabled":True,"policy":"SHADOW","root":str(tmp_path),"scheduled_at":NOW,
      "completed_session":date(2026,8,7),"evaluate":lambda: calls.append("evaluate") or {"starts":4,"status":"FIRING_RATE_UNPROVEN"}}
    assert macro_runtime_main(build_macro_runtime_deps(config))==0
    assert calls==["evaluate"]
    assert macro_runtime_main(build_macro_runtime_deps(config))==0 and calls==["evaluate"]

def test_composition_all_zero_start_gates_do_not_evaluate(tmp_path):
    from tokmo.composition import build_macro_runtime_deps, macro_runtime_main
    for enabled,policy,session,replay in ((False,"PAUSED",date(2026,8,7),False),(True,"SHADOW",None,False),(True,"SHADOW",date(2026,8,7),True)):
        calls=[]; cfg={"enabled":enabled,"policy":policy,"root":str(tmp_path/f"{enabled}{session}{replay}"),"scheduled_at":NOW,"completed_session":session,"replay":replay,"evaluate":lambda:calls.append(1)}
        assert macro_runtime_main(build_macro_runtime_deps(cfg))==0 and calls==[]

def test_skipped_produces_no_receipt(tmp_path):
    """B4: SKIPPED returns before reserve, leaving the receipt root empty."""
    from tokmo.composition import build_macro_runtime_deps, macro_runtime_main
    root = tmp_path / "skipped_root"
    cfg = {"enabled":True,"policy":"SHADOW","root":str(root),
           "scheduled_at":NOW,"completed_session":None,
           "evaluate":lambda:(_ for _ in ()).throw(AssertionError())}
    assert macro_runtime_main(build_macro_runtime_deps(cfg)) == 0
    assert not list(root.glob("*.json"))

def test_composition_evaluator_uses_exact_production_auth_read_parser_sequence(tmp_path):
    from pathlib import Path
    from tokmo.composition import build_macro_runtime_deps, macro_runtime_main
    from tokmo.facts.toss import _TossTransportSuccess
    fixture=Path(__file__).parent/"fixtures/macro_canary"; bodies=iter((fixture/"kospi_page_current_v1.json",fixture/"investor_page_current_v1.json",fixture/"proxy_page_current_v1.json"))
    seen=[]
    def auth(request): seen.append(type(request).__name__); return _TossTransportSuccess(b'{"access_token":"token","expires_in":60}')
    def read(request): seen.append(request.operation.value); return _TossTransportSuccess(next(bodies).read_bytes())
    class H:
        def read(self,market,day): return {"session":"regular","local_date":day}
    cfg={"enabled":True,"policy":"SHADOW","root":str(tmp_path),"scheduled_at":NOW,"completed_session":date(2026,8,7),"credentials":("client","secret"),"auth_transport":auth,"read_transport":read,"clock":lambda:NOW,"session_authorities":(H(),object())}
    assert macro_runtime_main(build_macro_runtime_deps(cfg))==0
    assert seen==["TossAuthRequest","INDICATOR_CANDLES","INDICATOR_INVESTOR_TRADING","STOCK_CANDLES"]
    from tokmo.programs.macro_canary_adapter import _G1B_LIFECYCLE_SPEC
    receipts=list(tmp_path.glob("*.json")); receipt=json.loads(receipts[0].read_text()); assert len(receipts)==1 and receipt["evidence_digest"]
    assert receipt["spec_digest"]==_G1B_LIFECYCLE_SPEC.spec_digest and receipt["spec_digest"]!="0"*64

def test_runtime_unequal_pages_continuity_partial_succeeds_without_global_pause(tmp_path):
    from pathlib import Path
    from tokmo.composition import build_macro_runtime_deps,macro_runtime_main
    from tokmo.facts.toss import _TossTransportSuccess
    fixture=Path(__file__).parent/"fixtures/macro_canary"
    def combined(current,older,extra=False):
        first=json.loads((fixture/current).read_text()); old=json.loads((fixture/older).read_text()); first["result"]["candles"]+=old["result"]["candles"]
        if extra:
            row=dict(old["result"]["candles"][0]); row["timestamp"]="2026-08-04T06:30:00Z"; first["result"]["candles"].append(row)
        first["result"]["nextBefore"]=None; return json.dumps(first).encode()
    bodies=iter((combined("kospi_page_current_v1.json","kospi_page_older_v1.json"),(fixture/"investor_page_current_v1.json").read_bytes(),combined("proxy_page_current_v1.json","proxy_page_older_v1.json",True))); seen=[]
    class H:
        def read(self,market,day): return {"session":"regular","local_date":day} if day>=date(2026,8,6) else None
    cfg={"enabled":True,"policy":"SHADOW","root":str(tmp_path),"scheduled_at":NOW,"completed_session":date(2026,8,7),"credentials":("c","s"),"auth_transport":lambda req:_TossTransportSuccess(b'{"access_token":"token","expires_in":60}'),"read_transport":lambda req:_TossTransportSuccess((lambda body: body if isinstance(body,bytes) else body.read_bytes())(next(bodies))),"clock":lambda:NOW,"session_authorities":(H(),object())}
    macro_runtime_main(build_macro_runtime_deps(cfg)); receipts=[json.loads(p.read_text()) for p in tmp_path.glob("*.json")]
    assert len(receipts)==1 and receipts[0]["state"]=="SUCCEEDED" and receipts[0]["calibration_status"]=="FIRING_RATE_UNPROVEN"
    assert not any(r.get("reason","").startswith("MATERIAL") for r in receipts)

def test_second_session_extends_forward_rate_and_receipt_contract(tmp_path):
    store=MacroRuntimeReceiptStore(tmp_path); p=MacroRuntimePolicyV1(True,"SHADOW")
    evidence={"starts":4,"status":"FIRING_RATE_UNPROVEN","evidence_digest":"a"*64,"pack_digest":"a"*64,"source_digest":"b"*64,"spec_digest":"c"*64,"lifecycle_digest":"d"*64,"request_manifest_digest":"e"*64,"request_cap":4,"fired":False}
    run_macro_runtime_once(p,MacroRuntimeTriggerV1(datetime(2026,8,9,8,tzinfo=timezone.utc),date(2026,8,7)),store,lambda:evidence)
    run_macro_runtime_once(p,MacroRuntimeTriggerV1(datetime(2026,8,10,8,tzinfo=timezone.utc),date(2026,8,8)),store,lambda:{**evidence,"fired":True})
    rows=[json.loads(x.read_text()) for x in tmp_path.glob("*.json")]
    latest=next(r for r in rows if r.get("completed_session")=="2026-08-08")
    assert latest["forward_denominator"]==2 and latest["forward_numerator"]==1 and latest["forward_status"]=="SHADOW_FORWARD_FIRING_RATE"
    assert all(latest[key] for key in ("pack_digest","source_digest","spec_digest","lifecycle_digest","request_manifest_digest")) and latest["request_cap"]==4


# --- AR-11/AR-22/M-3/M-4: FRED fields flow into the receipt but starts stays Toss-only ---

from tokmo.fred_query_composition import MACRO_FRED_RECEIPT_FIELDS


def _fred_receipt(**kw):
    fields = dict.fromkeys(MACRO_FRED_RECEIPT_FIELDS, None)
    fields.update(fred_bls_starts=0, **kw)
    return fields


def test_ar10_fred_fields_in_macro_runtime_match_composition_constant():
    """The runtime receipt tuple must persist every canonical FRED field."""
    import ast
    source = (Path(__file__).parents[1] / "src/tokmo/programs/macro_runtime.py").read_text()
    tree = ast.parse(source)
    # Extract the for-key tuple from the source.
    for_node = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.For)
                    and any(isinstance(n, ast.Constant) and n.value == "pack_digest"
                            for n in ast.walk(node)))
    key_consts = {n.value for n in ast.walk(for_node.iter)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert set(MACRO_FRED_RECEIPT_FIELDS) <= key_consts


def test_ar11_fred_caller_through_real_composition_keeps_starts_4_and_succeeds(tmp_path):
    """M-3/AR-11: Drive FRED through the real composition assembly.
    ``starts`` must stay Toss-only (4) even when FRED fields are present,
    and the run must settle SUCCEEDED with no GLOBAL_PAUSE receipt."""
    from tokmo.composition import build_macro_runtime_deps, macro_runtime_main
    fred_calls = []
    def fred_caller():
        fred_calls.append(1)
        return _fred_receipt(fred_status="ENRICHED", fred_provider_starts=1,
                             fred_schedule_source="SIDECAR")
    config = {"enabled": True, "policy": "SHADOW", "root": str(tmp_path),
              "scheduled_at": NOW, "completed_session": date(2026, 8, 7),
              "evaluate": lambda: {"starts": 4, "status": "FIRING_RATE_UNPROVEN"},
              "fred_caller": fred_caller}
    assert macro_runtime_main(build_macro_runtime_deps(config)) == 0
    assert fred_calls == [1], "fred_caller was not invoked exactly once"
    receipt = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert receipt["state"] == "SUCCEEDED"
    assert receipt["starts"] == 4
    assert receipt["fred_status"] == "ENRICHED"
    assert receipt["fred_provider_starts"] == 1
    assert receipt["fred_bls_starts"] == 0
    # Non-vacuous global-pause assertion: scan ALL receipts in the root.
    rows = [json.loads(p.read_text()) for p in tmp_path.glob("*.json")]
    assert not any(r.get("reason", "").startswith("MATERIAL") for r in rows)


def test_recurring_caller_revision_aware_reuse_through_composition(tmp_path):
    """A real caller/cache reuse is ENRICHED with zero FRED starts in composition."""
    from datetime import timedelta
    from tokmo.composition import build_macro_runtime_deps, macro_runtime_main
    from tokmo.fred_query_composition import (
        FredFixtureTransportOutcomeV1, FredFixtureTransportV1,
        build_fixture_api_key_provider_v1, build_macro_fred_caller_v1,
        build_production_fred_query_deps_v1)
    from tokmo.records.fred_schedule_sidecar import (
        SIDECAR_NAME, build_sidecar_payload_v1, write_schedule_sidecar_v1)
    from tokmo.values.fred_query import SERIES_PAYEMS, build_fred_release_schedule_v1

    fred_root=tmp_path/"fred"; fred_root.mkdir(mode=0o700)
    official=datetime(2026,8,7,12,30,tzinfo=timezone.utc)
    provenance={"source":"offline-composition-test"}
    schedule=build_fred_release_schedule_v1(
        series_id=SERIES_PAYEMS,release_id=50,release_date=date(2026,8,7),
        official_release_at=official,schedule_provenance=provenance)
    payload=build_sidecar_payload_v1(
        schedule=schedule,release_id=50,release_date=date(2026,8,7),
        official_release_at=official,provenance=provenance)
    write_schedule_sidecar_v1(fred_root/SIDECAR_NAME,payload)
    body=json.dumps({"realtime_start":"1776-07-04","realtime_end":"9999-12-31",
        "observation_start":"2026-05-01","observation_end":"2026-07-31",
        "units":"lin","output_type":4,"file_type":"json",
        "order_by":"observation_date","sort_order":"asc","count":3,"offset":0,
        "limit":3,"observations":[
            {"realtime_start":"2026-06-05","realtime_end":"9999-12-31","date":"2026-05-01","value":"150800"},
            {"realtime_start":"2026-07-02","realtime_end":"9999-12-31","date":"2026-06-01","value":"150900"},
            {"realtime_start":"2026-08-07","realtime_end":"9999-12-31","date":"2026-07-01","value":"151000"}]}).encode()
    seed_transport=FredFixtureTransportV1(outcomes=(FredFixtureTransportOutcomeV1("success",body),))
    seed_deps=build_production_fred_query_deps_v1(
        root=fred_root,sidecar_path=fred_root/SIDECAR_NAME,transport=seed_transport,
        api_key_provider=build_fixture_api_key_provider_v1(),clock=lambda:NOW)
    assert build_macro_fred_caller_v1(seed_deps)()["fred_status"]=="ENRICHED"

    later=NOW+timedelta(hours=24)
    reuse_transport=FredFixtureTransportV1(outcomes=())
    reuse_deps=build_production_fred_query_deps_v1(
        root=fred_root,sidecar_path=fred_root/SIDECAR_NAME,transport=reuse_transport,
        api_key_provider=build_fixture_api_key_provider_v1(),clock=lambda:later)
    runtime_root=tmp_path/"runtime"
    config={"enabled":True,"policy":"SHADOW","root":str(runtime_root),
            "scheduled_at":later,"completed_session":date(2026,8,8),
            "evaluate":lambda:{"starts":4,"status":"FIRING_RATE_UNPROVEN"},
            "fred_caller":build_macro_fred_caller_v1(reuse_deps)}
    assert macro_runtime_main(build_macro_runtime_deps(config))==0
    receipt=json.loads(next(runtime_root.glob("*.json")).read_text())
    assert receipt["state"]=="SUCCEEDED" and receipt["starts"]==4
    assert receipt["fred_status"]=="ENRICHED"
    assert receipt["fred_coverage"]=="USED"
    assert receipt["fred_due_state"]=="NOT_DUE_REVISION_CURRENT"
    assert receipt["fred_provider_starts"]==0 and reuse_transport.requests==[]


def test_failed_base_does_not_invoke_fred_caller(tmp_path):
    """M-3: A FAILED base evaluate must NOT invoke the FRED caller."""
    from tokmo.composition import build_macro_runtime_deps, macro_runtime_main
    fred_calls = []
    def fred_caller():
        fred_calls.append(1)
        return _fred_receipt(fred_status="OMITTED")
    config = {"enabled": True, "policy": "SHADOW", "root": str(tmp_path),
              "scheduled_at": NOW, "completed_session": date(2026, 8, 7),
              "evaluate": lambda: {"starts": 1, "status": "FAILED"},
              "fred_caller": fred_caller}
    macro_runtime_main(build_macro_runtime_deps(config))
    assert fred_calls == [], "fred_caller was invoked on a FAILED base"


def test_ar22_folding_fred_starts_into_starts_fails_through_composition(tmp_path):
    """M-3/AR-22 mutation-survivor: if a mutation folds fred_provider_starts
    into starts, the composition-level evaluate returns starts=5, and
    run_macro_runtime_once must raise the cap, settle FAILED, and write a
    GLOBAL_PAUSE receipt. This is the real composition path, not a hand-
    constructed starts=5."""
    from tokmo.composition import build_macro_runtime_deps, macro_runtime_main
    def fred_caller():
        return _fred_receipt(fred_status="ENRICHED", fred_provider_starts=1)
    config = {"enabled": True, "policy": "SHADOW", "root": str(tmp_path),
              "scheduled_at": NOW, "completed_session": date(2026, 8, 7),
              "evaluate": lambda: {"starts": 4, "status": "FIRING_RATE_UNPROVEN"},
              "fred_caller": fred_caller}
    deps = build_macro_runtime_deps(config)
    # Simulate the mutation: patch the wrapped evaluate to fold starts.
    original = deps["config"]["evaluate"]
    def mutated():
        result = original()
        result["starts"] += result.get("fred_provider_starts", 0)
        return result
    deps["config"]["evaluate"] = mutated
    macro_runtime_main(deps)
    rows = [json.loads(p.read_text()) for p in tmp_path.glob("*.json")]
    # The mutation produces starts=5 → ValueError("start cap") → FAILED.
    assert any(r.get("state") == "FAILED" for r in rows)
    assert any(r.get("reason", "").startswith("MATERIAL") for r in rows)
