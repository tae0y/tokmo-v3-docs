import json, stat
from tokmo.records.macro_runtime_receipts import MacroRuntimeReceiptStore

def test_reserve_settle_duplicate_unknown_and_modes(tmp_path):
    s=MacroRuntimeReceiptStore(tmp_path); r=s.reserve("a"*64,{"completed_session":"2026-08-07"})
    assert r["state"]=="RESERVED"
    assert stat.S_IMODE(tmp_path.stat().st_mode)==0o700
    s.reconcile_unknown(); assert s.read_receipt("a"*64)["state"]=="UNKNOWN"
    try: s.reserve("a"*64,{})
    except ValueError: pass
    else: assert False
    s.reserve("b"*64,{}); s.settle("b"*64,"SUCCEEDED",{"starts":4})
    assert s.read_receipt("b"*64)["starts"]==4
