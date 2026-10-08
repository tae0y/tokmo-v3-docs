import importlib.util, json, os, stat, subprocess, sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

P = Path(__file__).parents[1] / "tools" / "macro_canary.py"
spec = importlib.util.spec_from_file_location("macro_canary", P)
mc = importlib.util.module_from_spec(spec); spec.loader.exec_module(mc)

def deps(tmp_path): return mc.CampaignStoreDepsV1.for_test(tmp_path)
def session_stores():
    class H:
        def read(self,market,day): return {"session":"regular","local_date":day}
    class C:
        def read(self):
            session=type("S",(),{"end":"2026-08-09T07:00:00Z"})(); day=type("D",(),{"trade_date":"2026-08-09","sessions":{"regularMarket":session}})()
            return type("Cache",(),{"kr":day})()
    return H(),C()

def test_cap_inherited_fail_unknown_and_eleventh_pre_io(tmp_path):
    d = deps(tmp_path)
    purposes = list(mc.CampaignLedgerV1.PURPOSES)[:10]
    for i, purpose in enumerate(purposes):
        a = mc._reserve_campaign_start(d, purpose, "TOKEN" if "TOKEN" in purpose else "STOCK_CANDLES",
            "0"*64, f"{i:064x}", "1"*40)
        mc._settle_attempt(d, a["ordinal"], "SUCCEEDED", "success")
    with pytest.raises(ValueError): mc._reserve_campaign_start(d, "RESIDUAL_PROXY_P2", "STOCK_CANDLES", "1"*64, "f"*64, "2"*40)
    ledger=json.loads(d.ledger.read_text()); assert ledger["reserved_total"] == 10

def test_reconcile_crash_is_unknown_and_private(tmp_path):
    d=deps(tmp_path); a=mc._reserve_campaign_start(d,"INITIAL_TOKEN","TOKEN","0"*64,"a"*64,"1"*40)
    mc._reconcile_campaign_unknown(d)
    ledger=json.loads(d.ledger.read_text()); assert ledger["attempts"][0]["state"] == "UNKNOWN"
    assert stat.S_IMODE(d.ledger.stat().st_mode) == 0o600
    with pytest.raises(ValueError): mc._reserve_campaign_start(d,"INITIAL_TOKEN","TOKEN","0"*64,"b"*64,"2"*40)

def test_fixed_identity_and_live_path_has_no_override(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    d=mc.CampaignStoreDepsV1.for_live()
    assert d.root == tmp_path/".local/share/tokmo-v3/macro-canary"/mc.CampaignLedgerV1.CAMPAIGN_KEY
    assert mc.main(["--help"]) == 0
    assert mc.main(["--root", str(tmp_path)]) != 0

def test_residual_admission_closed_and_sufficient_refuses(tmp_path):
    d=deps(tmp_path)
    mc.admit_residual(d, mc.ResidualPurposeV1("RESIDUAL_KOSPI_P1","HISTORICAL_SHAPE","KOSPI"))
    with pytest.raises(ValueError): mc.admit_residual(d, mc.ResidualPurposeV1("RESIDUAL_KOSPI_P1","HISTORICAL_SHAPE","KOSPI"))
    ledger=json.loads(d.ledger.read_text()); ledger["status"]="SUFFICIENT"; mc._write_private_atomic(d.ledger, ledger)
    with pytest.raises(ValueError): mc.admit_residual(d, mc.ResidualPurposeV1("RESIDUAL_PROXY_P1","T1_WINDOW","PROXY"))

def test_campaign_uses_production_request_objects_and_masks(tmp_path):
    from tokmo.facts.toss import _TossTransportFailure
    seen=[]
    result=mc.run_macro_campaign(deps(tmp_path), invocation_id="a"*64, revision_sha="1"*40,
        credentials=("client","secret"), auth_transport=lambda req: seen.append(req) or type("R",(),{"body":b'{"access_token":"bearer","expires_in":60}'})(),
        read_transport=lambda req: seen.append(req) or _TossTransportFailure("connection_failure"), clock=lambda: datetime(2026,8,9,8,tzinfo=timezone.utc),session_authorities=session_stores())
    assert len(seen)==2 and type(seen[0]).__name__=="TossAuthRequest" and type(seen[1]).__name__=="TossReadRequest"
    text=json.dumps(result); assert all(v not in text for v in ("client","secret","bearer",str(tmp_path)))
    persisted="\n".join(path.read_text() for path in deps(tmp_path).root.rglob("*.json"))
    assert all(value not in persisted for value in ("client","secret","bearer",str(tmp_path),"https://","accountSeq","nextBefore","2026-08-07T06:30:00Z"))

def test_exact_initial_success_sequence_and_four_start_cap(tmp_path):
    from tokmo.facts.toss import _TossTransportSuccess
    fixtures=Path(__file__).parent/"fixtures/macro_canary"
    def complete_history(name,key):
        body=json.loads((fixtures/name).read_text()); template=body["result"][key][0]; rows=[]
        for offset in range(60):
            day=date(2026,8,7)-timedelta(days=offset); row=dict(template)
            if key=="candles": row["timestamp"]=f"{day.isoformat()}T06:30:00Z"
            else: row.update(date=day.isoformat(),updatedAt=f"{day.isoformat()}T07:00:00Z")
            rows.append(row)
        body["result"][key]=rows
        body["result"]["nextBefore" if key=="candles" else "nextUntil"]=None
        return json.dumps(body).encode()
    bodies=iter((complete_history("kospi_page_current_v1.json","candles"),complete_history("investor_page_current_v1.json","records"),complete_history("proxy_page_current_v1.json","candles")))
    seen=[]
    result=mc.run_macro_campaign(deps(tmp_path),invocation_id="c"*64,revision_sha="2"*40,
      credentials=("client","secret"),auth_transport=lambda req:_TossTransportSuccess(b'{"access_token":"bearer","expires_in":60}'),
      read_transport=lambda req: seen.append(req.operation.value) or _TossTransportSuccess(next(bodies)),clock=lambda:datetime(2026,8,9,8,tzinfo=timezone.utc),session_authorities=session_stores())
    assert seen==["INDICATOR_CANDLES","INDICATOR_INVESTOR_TRADING","STOCK_CANDLES"]
    assert result["network_starts"]==4
    assert result["pack_digest"] and result["continuity"]=="CALENDAR_COMPLETE"
    assert result["status"]=="SUFFICIENT"
    assert result["firing_rate_status"]=="FIRING_RATE_UNPROVEN" and result["spec_digest"]!="0"*64
    assert all(result[key] for key in ("lifecycle_digest","source_digest","request_manifest_digest","before_invariant_digest","after_invariant_digest"))

def test_initial_session_authority_failure_refuses_before_reservation_and_io(tmp_path):
    starts=[]
    with pytest.raises(ValueError): mc.run_macro_campaign(deps(tmp_path),invocation_id="a"*64,revision_sha="1"*40,credentials=("c","s"),auth_transport=lambda req:starts.append(req),read_transport=lambda req:starts.append(req),clock=lambda:datetime(2026,8,9,8,tzinfo=timezone.utc),session_authorities=(type("H",(),{"read":lambda *a:None})(),type("C",(),{"read":lambda *a:None})()))
    assert starts==[]

def test_analysis_exception_propagates_without_reclassifying_settled_wire(monkeypatch,tmp_path):
    from tokmo.facts.toss import _TossTransportSuccess
    import tokmo.programs.macro_canary_adapter as adapter
    fixture=Path(__file__).parent/"fixtures/macro_canary"; bodies=iter((fixture/"kospi_page_current_v1.json",fixture/"investor_page_current_v1.json",fixture/"proxy_page_current_v1.json"))
    monkeypatch.setattr(adapter,"build_macro_bundle",lambda *a,**k:(_ for _ in ()).throw(RuntimeError("analysis")))
    d=deps(tmp_path)
    with pytest.raises(RuntimeError): mc.run_macro_campaign(d,invocation_id="d"*64,revision_sha="2"*40,credentials=("c","s"),auth_transport=lambda req:_TossTransportSuccess(b'{"access_token":"token","expires_in":60}'),read_transport=lambda req:_TossTransportSuccess(next(bodies).read_bytes()),clock=lambda:datetime(2026,8,9,8,tzinfo=timezone.utc),session_authorities=session_stores())
    assert [a["state"] for a in json.loads(d.ledger.read_text())["attempts"]]==["SUCCEEDED"]*4

def test_architecture_exact_module_definitions_and_no_network_client():
    import ast
    tree=ast.parse(P.read_text()); names={n.name for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}
    assert names == {"CampaignLedgerV1","CampaignStoreDepsV1","ResidualPurposeV1","run_macro_campaign","admit_residual","main","_canonical_campaign_path","_reserve_campaign_start","_settle_attempt","_reconcile_campaign_unknown","_write_private_atomic","_masked_summary"}
    assert "requests" not in P.read_text() and "httpx" not in P.read_text()
    main_node=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=="main")
    main_text=ast.unparse(main_node)
    assert "CampaignStoreDepsV1.for_live()" in main_text and "for_test" not in main_text

def test_token_reacquisition_requires_closed_durable_prerequisites(tmp_path):
    d=deps(tmp_path)
    first=mc._reserve_campaign_start(d,"INITIAL_TOKEN","TOKEN","0"*64,"a"*64,"1"*40)
    mc._settle_attempt(d,first["ordinal"],"SUCCEEDED","success")
    mc.admit_residual(d,mc.ResidualPurposeV1("RESIDUAL_KOSPI_P1","HISTORICAL_SHAPE","KOSPI"))
    with pytest.raises(ValueError):
        mc.run_macro_campaign(d,action="reacquire",invocation_id="b"*64,revision_sha="2"*40,
          credentials=("c","s"),auth_transport=lambda req:None,read_transport=lambda req:None,
          clock=lambda:datetime.now(timezone.utc),cursor_retained=True)
    mc.run_macro_campaign(d,action="record_boundary",invocation_id="b"*64,revision_sha="2"*40,
      credentials=None,auth_transport=None,read_transport=None,clock=lambda:datetime.now(timezone.utc),cursor_retained=True)
    seen=[]
    reply=type("R",(),{"body":b'{"access_token":"new","expires_in":60}'})()
    one=mc.run_macro_campaign(d,action="reacquire",invocation_id="b"*64,revision_sha="2"*40,
      credentials=("c","s"),auth_transport=lambda req:seen.append(req) or reply,read_transport=None,
      clock=lambda:datetime.now(timezone.utc),cursor_retained=True)
    assert one["last_purpose"]=="REACQUIRED_TOKEN_P1" and len(seen)==1
    mc.run_macro_campaign(d,action="record_boundary",invocation_id="c"*64,revision_sha="3"*40,
      credentials=None,auth_transport=None,read_transport=None,clock=lambda:datetime.now(timezone.utc),cursor_retained=True)
    two=mc.run_macro_campaign(d,action="reacquire",invocation_id="c"*64,revision_sha="3"*40,
      credentials=("c","s"),auth_transport=lambda req:reply,read_transport=None,
      clock=lambda:datetime.now(timezone.utc),cursor_retained=True)
    assert two["last_purpose"]=="REACQUIRED_TOKEN_P2"
    with pytest.raises(ValueError):
        mc.run_macro_campaign(d,action="reacquire",invocation_id="d"*64,revision_sha="4"*40,
          credentials=("c","s"),auth_transport=lambda req:reply,read_transport=None,
          clock=lambda:datetime.now(timezone.utc),cursor_retained=True)

def test_reacquire_failed_or_lost_coordinator_cursor_refuses(tmp_path):
    d=deps(tmp_path); a=mc._reserve_campaign_start(d,"INITIAL_TOKEN","TOKEN","0"*64,"a"*64,"1"*40)
    mc._settle_attempt(d,a["ordinal"],"FAILED","closed")
    starts=[]
    with pytest.raises(ValueError): mc.run_macro_campaign(d,action="reacquire",invocation_id="b"*64,revision_sha="2"*40,credentials=("c","s"),auth_transport=lambda req:starts.append(1),read_transport=None,clock=lambda:datetime.now(timezone.utc),cursor_retained=False)
    assert starts==[]

def test_residual_uses_retained_typed_cursor_and_one_production_call(tmp_path):
    from tokmo.facts.toss import _TossTransportSuccess
    from tokmo.facts.toss_reads import TossReadOperation, build_read_request, parse_read_response
    current=(Path(__file__).parent/"fixtures/macro_canary/kospi_page_current_v1.json").read_bytes()
    older=(Path(__file__).parent/"fixtures/macro_canary/kospi_page_older_v1.json").read_bytes()
    req=build_read_request(TossReadOperation.INDICATOR_CANDLES,token="token",symbol="KOSPI",interval="1d",count=200)
    page=parse_read_response(req=req,body=current,fetched_at=datetime.now(timezone.utc)).value
    d=deps(tmp_path); tok=mc._reserve_campaign_start(d,"INITIAL_TOKEN","TOKEN","0"*64,"a"*64,"1"*40); mc._settle_attempt(d,tok["ordinal"],"SUCCEEDED","success")
    mc.admit_residual(d,mc.ResidualPurposeV1("RESIDUAL_KOSPI_P1","PAGE_BOUNDARY","KOSPI"))
    seen=[]
    investor_req=build_read_request(TossReadOperation.INDICATOR_INVESTOR_TRADING,token="token",symbol="KOSPI",interval="1d",count=100); investor=parse_read_response(req=investor_req,body=(Path(__file__).parent/"fixtures/macro_canary/investor_page_current_v1.json").read_bytes(),fetched_at=datetime.now(timezone.utc)).value
    proxy_req=build_read_request(TossReadOperation.STOCK_CANDLES,token="token",symbol="261240",interval="1d",count=200); proxy=parse_read_response(req=proxy_req,body=(Path(__file__).parent/"fixtures/macro_canary/proxy_page_current_v1.json").read_bytes(),fetched_at=datetime.now(timezone.utc)).value; pages={"KOSPI":[page],"INVESTOR":[investor],"PROXY":[proxy]}
    result=mc.run_macro_campaign(d,action="residual",purpose_code="RESIDUAL_KOSPI_P1",
      invocation_id="b"*64,revision_sha="2"*40,credentials=None,bearer="token",
      auth_transport=None,read_transport=lambda request:seen.append(request) or _TossTransportSuccess(older),
      clock=lambda:datetime(2026,8,9,8,tzinfo=timezone.utc),cursor=page.next_before,newer_page=page,campaign_pages=pages,session_authorities=session_stores())
    assert len(seen)==1 and seen[0].before is page.next_before
    assert result["last_purpose"]=="RESIDUAL_KOSPI_P1" and len(pages["KOSPI"])==2 and result["pack_digest"]
    assert all(result[key] for key in ("spec_digest","lifecycle_digest","source_digest","request_manifest_digest","before_invariant_digest","after_invariant_digest")) and result["request_cap"]==10
    with pytest.raises(ValueError): mc.run_macro_campaign(d,action="residual",purpose_code="RESIDUAL_KOSPI_P1",invocation_id="c"*64,revision_sha="3"*40,credentials=None,bearer="token",auth_transport=None,read_transport=lambda req:seen.append(req),clock=lambda:datetime.now(timezone.utc),cursor=page.next_before,newer_page=page)

def test_residual_hand_entered_or_cross_page_cursor_refuses_before_io(tmp_path):
    from datetime import timedelta
    from tokmo.facts.toss_reads import TossReadOperation, build_read_request, parse_read_response
    current=(Path(__file__).parent/"fixtures/macro_canary/kospi_page_current_v1.json").read_bytes(); req=build_read_request(TossReadOperation.INDICATOR_CANDLES,token="token",symbol="KOSPI",interval="1d",count=200); page=parse_read_response(req=req,body=current,fetched_at=datetime.now(timezone.utc)).value
    d=deps(tmp_path); tok=mc._reserve_campaign_start(d,"INITIAL_TOKEN","TOKEN","0"*64,"a"*64,"1"*40); mc._settle_attempt(d,tok["ordinal"],"SUCCEEDED","success"); mc.admit_residual(d,mc.ResidualPurposeV1("RESIDUAL_KOSPI_P1","PAGE_BOUNDARY","KOSPI")); starts=[]
    with pytest.raises(ValueError): mc.run_macro_campaign(d,action="residual",purpose_code="RESIDUAL_KOSPI_P1",invocation_id="b"*64,revision_sha="2"*40,credentials=None,bearer="token",auth_transport=None,read_transport=lambda req:starts.append(req),clock=lambda:datetime.now(timezone.utc),cursor=page.next_before-timedelta(days=1),newer_page=page)
    assert starts==[]

def test_residual_transport_failure_stops_campaign_and_independent_admission(tmp_path):
    from tokmo.facts.toss import _TossTransportFailure
    from tokmo.facts.toss_reads import TossReadOperation,build_read_request,parse_read_response
    req=build_read_request(TossReadOperation.INDICATOR_CANDLES,token="token",symbol="KOSPI",interval="1d",count=200); page=parse_read_response(req=req,body=(Path(__file__).parent/"fixtures/macro_canary/kospi_page_current_v1.json").read_bytes(),fetched_at=datetime.now(timezone.utc)).value
    d=deps(tmp_path); tok=mc._reserve_campaign_start(d,"INITIAL_TOKEN","TOKEN","0"*64,"a"*64,"1"*40); mc._settle_attempt(d,tok["ordinal"],"SUCCEEDED","success")
    for purpose,series in (("RESIDUAL_KOSPI_P1","KOSPI"),("RESIDUAL_PROXY_P1","PROXY")): mc.admit_residual(d,mc.ResidualPurposeV1(purpose,"PAGE_BOUNDARY",series))
    with pytest.raises(ValueError): mc.run_macro_campaign(d,action="residual",purpose_code="RESIDUAL_KOSPI_P1",invocation_id="b"*64,revision_sha="2"*40,credentials=None,bearer="token",auth_transport=None,read_transport=lambda req:_TossTransportFailure("http_429"),clock=lambda:datetime.now(timezone.utc),cursor=page.next_before,newer_page=page)
    assert json.loads(d.ledger.read_text())["status"]=="STOPPED"
    with pytest.raises(ValueError): mc._reserve_campaign_start(d,"RESIDUAL_PROXY_P1","STOCK_CANDLES","0"*64,"c"*64,"3"*40)

def test_multiprocess_cap_never_overbooks_and_reconciles_unknown(tmp_path):
    root=tmp_path/"shared"; code='''import importlib.util,sys\nfrom pathlib import Path\np=Path(sys.argv[1]); s=importlib.util.spec_from_file_location("mc",p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)\nd=m.CampaignStoreDepsV1.for_test(Path(sys.argv[2]))\ntry:m._reserve_campaign_start(d,sys.argv[3],"TOKEN" if "TOKEN" in sys.argv[3] else "STOCK_CANDLES","0"*64,sys.argv[4]*64,"1"*40);raise SystemExit(0)\nexcept ValueError:raise SystemExit(3)'''
    procs=[subprocess.Popen([sys.executable,"-c",code,str(P),str(root),purpose,hex(i+1)[2:]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) for i,purpose in enumerate(mc.CampaignLedgerV1.PURPOSES)]
    codes=[p.wait() for p in procs]
    d=mc.CampaignStoreDepsV1.for_test(root); ledger=json.loads(d.ledger.read_text())
    assert codes.count(0)==10 and ledger["reserved_total"]==10 and len({a["request_identity"] for a in ledger["attempts"]})==10
    assert mc._reconcile_campaign_unknown(d)==10 and all(a["state"]=="UNKNOWN" for a in json.loads(d.ledger.read_text())["attempts"])

def test_crash_before_replace_consumes_zero_after_replace_consumes_unknown(tmp_path,monkeypatch):
    d=deps(tmp_path); real=mc.os.replace
    monkeypatch.setattr(mc.os,"replace",lambda *a: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError): mc._reserve_campaign_start(d,"INITIAL_TOKEN","TOKEN","0"*64,"a"*64,"1"*40)
    assert not d.ledger.exists()
    monkeypatch.setattr(mc.os,"replace",real); real_fsync=mc.os.fsync; calls=iter((None,OSError("post replace")))
    def crash_parent(fd):
        value=next(calls); real_fsync(fd)
        if isinstance(value,Exception): raise value
    monkeypatch.setattr(mc.os,"fsync",crash_parent)
    with pytest.raises(OSError): mc._reserve_campaign_start(d,"INITIAL_TOKEN","TOKEN","0"*64,"a"*64,"1"*40)
    monkeypatch.setattr(mc.os,"fsync",real_fsync)
    assert d.ledger.exists() and mc._reconcile_campaign_unknown(d)==1

def test_live_path_symlink_and_legacy_ambiguity_refuse_pre_io(tmp_path,monkeypatch):
    monkeypatch.setattr(Path,"home",classmethod(lambda cls:tmp_path))
    base=tmp_path/".local/share/tokmo-v3"; base.mkdir(parents=True); os.chmod(base,0o700)
    legacy=base/"canary"; legacy.mkdir(); (legacy/"artifact").write_text(mc.CampaignLedgerV1.PURPOSE_ID)
    d=mc.CampaignStoreDepsV1.for_live()
    with pytest.raises(ValueError): mc._reserve_campaign_start(d,"INITIAL_TOKEN","TOKEN","0"*64,"a"*64,"1"*40)
    assert not d.ledger.exists()

@pytest.mark.parametrize("defect",("symlink","wrong_mode","orphan_artifact","multiple"))
def test_live_path_every_unsafe_shape_refuses_before_reservation(tmp_path,monkeypatch,defect):
    monkeypatch.setattr(Path,"home",classmethod(lambda cls:tmp_path)); base=tmp_path/".local/share/tokmo-v3"; base.mkdir(parents=True); os.chmod(base,0o700); parent=base/"macro-canary"
    if defect=="symlink": parent.symlink_to(tmp_path/"elsewhere",target_is_directory=True)
    else:
        parent.mkdir(); os.chmod(parent,0o755 if defect=="wrong_mode" else 0o700)
        if defect=="orphan_artifact": target=parent/mc.CampaignLedgerV1.CAMPAIGN_KEY; target.mkdir(); (target/"orphan").write_text("x")
        if defect=="multiple": other=parent/"other"; other.mkdir(); (other/"campaign.json").write_text("{}")
    d=mc.CampaignStoreDepsV1.for_live()
    with pytest.raises(ValueError): mc._reserve_campaign_start(d,"INITIAL_TOKEN","TOKEN","0"*64,"a"*64,"1"*40)
    assert not d.ledger.exists()

def test_cli_invocation_preflight_and_mask_all_outputs(tmp_path,capsys):
    secret="PRIVATE_SECRET_VALUE"; private=str(tmp_path)
    assert mc.main(["--invocation-id","short","--confirm-bounded-live-canary"])==1
    assert mc.main(["--invocation-id","a"*64,"--root",private,"--confirm-bounded-live-canary"])==1
    captured=capsys.readouterr(); assert secret not in captured.out+captured.err and private not in captured.out+captured.err
