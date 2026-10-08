import json, os, stat

import pytest

from tokmo.records.ai_infra_capex_shadow_receipts import ShadowReceiptStoreV1


def test_atomic_private_transition_duplicate_and_unknown(tmp_path):
    store=ShadowReceiptStoreV1(tmp_path/"receipts",tmp_path/"runtime.lock")
    token=store.acquire(); assert token is not None
    identity="a"*64
    store.reserve(identity,{"tick_identity":identity,"error_code":"NONE"})
    store.settle(identity,"SUCCEEDED",{"status":"OK"})
    before=(tmp_path/"receipts"/f"{identity}.json").read_bytes()
    with pytest.raises(ValueError): store.reserve(identity,{})
    assert (tmp_path/"receipts"/f"{identity}.json").read_bytes()==before
    other="b"*64; store.reserve(other,{"tick_identity":other,"error_code":"NONE"}); store.reconcile_unknown(other)
    assert store.read(other)["state"]=="UNKNOWN"
    store.release(token)
    assert stat.S_IMODE((tmp_path/"receipts").stat().st_mode)==0o700
    assert stat.S_IMODE((tmp_path/"receipts"/f"{identity}.json").stat().st_mode)==0o600
    assert not list((tmp_path/"receipts").glob(".*.tmp"))


@pytest.mark.parametrize("bad",[
    {"raw_title":"secret"},{"message":"boom"},{"local_path":"/Users/x"},{"url":"https://x"},{"token":"abc"},
])
def test_privacy_and_unknown_keys_fail_before_write(tmp_path,bad):
    store=ShadowReceiptStoreV1(tmp_path/"receipts",tmp_path/"runtime.lock"); token=store.acquire(); identity="c"*64
    with pytest.raises(ValueError): store.reserve(identity,{"tick_identity":identity,**bad})
    assert store.read(identity) is None
    store.release(token)


def test_nonblocking_owned_lock(tmp_path):
    one=ShadowReceiptStoreV1(tmp_path/"receipts",tmp_path/"runtime.lock"); two=ShadowReceiptStoreV1(tmp_path/"receipts",tmp_path/"runtime.lock")
    token=one.acquire(); assert token is not None and two.acquire() is None
    one.release(token); assert two.acquire() is not None


def test_window_error_is_closed_enum(tmp_path):
    store=ShadowReceiptStoreV1(tmp_path/"receipts",tmp_path/"runtime.lock"); token=store.acquire(); identity="d"*64
    store.reserve(identity,{"error_code":"NONE"}); store.settle(identity,"FAILED",{"status":"BLOCKED","phase":"PREFLIGHT","error_code":"WINDOW_INVALID"})
    assert store.read(identity)["error_code"]=="WINDOW_INVALID"; store.release(token)
