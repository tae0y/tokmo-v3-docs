from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokmo.programs.ai_infra_capex_shadow_runtime import (
    BUNDLE_MEMBERS_V1,
    ShadowRuntimeDepsV1,
    canonical_path_identity_digest_v1,
    runtime_bundle_digest_v1,
    run_ai_infra_capex_shadow_once_v1,
)


class MemoryReceipts:
    def __init__(self): self.rows={}; self.locked=False
    def acquire(self):
        if self.locked: return None
        self.locked=True; return object()
    def release(self, token): self.locked=False
    def read(self, identity): return self.rows.get(identity)
    def reserve(self, identity, fields): self.rows[identity]={"state":"RESERVED",**fields}
    def settle(self, identity, state, fields): self.rows[identity].update(state=state,**fields)
    def reconcile_unknown(self, identity): self.rows[identity]["state"]="UNKNOWN"


def config(tmp_path, now, *, policy="SHADOW", enabled=True):
    tmp_path.mkdir(parents=True,exist_ok=True)
    source=tmp_path/"news.sqlite3"; source.write_bytes(b"db"); source.chmod(0o600)
    bundle={name:(tmp_path/name.replace("/","_")) for name in BUNDLE_MEMBERS_V1}
    for path in bundle.values(): path.write_text(path.name)
    return {
        "schema":"tokmo.g1c.shadow-config.v1","enabled":enabled,"policy":policy,
        "project_root":str(tmp_path),"runner_path":str(tmp_path/"runner"),"venv_python":str(tmp_path/"python"),
        "canonical_news_path":str(source.resolve()),"canonical_path_identity_digest":canonical_path_identity_digest_v1(source),
        "expected_commit":"a"*40,"spec_digest":"d403c8444bffc7e5ca958e5293ee511488fdc7743c03c863b7d8ce9353844c15",
        "classifier_digest":"efef7c0fb6ae79a4fa89920ad93daf4d82462c60291e2a07fd8434850d4c3b31",
        "runtime_bundle_digest":runtime_bundle_digest_v1(bundle),"cadence_version":"g1c-hourly-minute12-kst-v1","minute_kst":12,
        "activation_utc":"2026-08-01T00:00:00.000000Z","review_due_utc":"2026-09-15T00:00:00.000000Z","expiry_utc":"2026-11-29T00:00:00.000000Z",
        "review_days":45,"expiry_days":120,"query_policy":"STRICT_RO_V1","http_start_cap":0,"model_start_cap":0,
        "retry_cap":0,"query_cap":1,"delivery_cap":0,"news_write_cap":0,
    },bundle


def deps(now, receipts, calls, bundle, *, scheduler=True,query_status="OK"):
    def query(path, query): calls.append((path,query)); return {"status":query_status,"query_digest":"1"*64,"result_digest":"2"*64,"source_snapshot_digest":"3"*64}
    def evaluate(**kwargs):
        assert kwargs["query_local_news"](kwargs["query"])["status"]=="OK"
        return {"sensor_state":"NEEDS_CONFIRMATION","pack_digest":"4"*64,"all_match_semantic_digest":"5"*64}
    return ShadowRuntimeDepsV1(clock=lambda:now,scheduler_authority=lambda: scheduler,query_local_news=query,evaluate=evaluate,receipts=receipts,bundle_paths=bundle)


@pytest.mark.parametrize("policy,enabled",[("PAUSED",False),("PAUSED",True)])
def test_paused_is_structural_query_zero(tmp_path,policy,enabled):
    now=datetime(2026,8,9,3,13,tzinfo=timezone.utc); calls=[]; receipts=MemoryReceipts(); cfg,bundle=config(tmp_path,now,policy=policy,enabled=enabled)
    assert run_ai_infra_capex_shadow_once_v1(cfg,deps(now,receipts,calls,bundle))==0
    assert calls==[] and receipts.rows=={}


def test_eligible_exact_query_one_cached_evaluator_and_duplicate(tmp_path):
    now=datetime(2026,8,9,3,13,tzinfo=timezone.utc); calls=[]; receipts=MemoryReceipts(); cfg,bundle=config(tmp_path,now)
    assert run_ai_infra_capex_shadow_once_v1(cfg,deps(now,receipts,calls,bundle))==0
    assert len(calls)==1 and next(iter(receipts.rows.values()))["state"]=="SUCCEEDED"
    before=dict(receipts.rows)
    assert run_ai_infra_capex_shadow_once_v1(cfg,deps(now,receipts,calls,bundle))!=0
    assert len(calls)==1 and receipts.rows==before


def test_out_of_window_is_failed_receipt_query_zero(tmp_path):
    now=datetime(2026,8,9,3,18,tzinfo=timezone.utc); calls=[]; receipts=MemoryReceipts(); cfg,bundle=config(tmp_path,now)
    assert run_ai_infra_capex_shadow_once_v1(cfg,deps(now,receipts,calls,bundle))!=0
    row=next(iter(receipts.rows.values()))
    assert calls==[] and row["state"]=="FAILED" and row["error_code"]=="WINDOW_INVALID"
    assert row["scheduled_at_utc"]=="2026-08-09T03:12:00.000000Z"


def test_bad_scheduler_authority_is_distinct_from_window(tmp_path):
    now=datetime(2026,8,9,3,13,tzinfo=timezone.utc); calls=[]; receipts=MemoryReceipts(); cfg,bundle=config(tmp_path,now)
    assert run_ai_infra_capex_shadow_once_v1(cfg,deps(now,receipts,calls,bundle,scheduler=False))!=0
    assert next(iter(receipts.rows.values()))["error_code"]=="SCHEDULER_AUTHORITY_INVALID" and calls==[]


def test_non_ok_query_is_one_query_failed_not_success(tmp_path):
    now=datetime(2026,8,9,3,13,tzinfo=timezone.utc); calls=[]; receipts=MemoryReceipts(); cfg,bundle=config(tmp_path,now)
    assert run_ai_infra_capex_shadow_once_v1(cfg,deps(now,receipts,calls,bundle,query_status="BUSY_OR_LOCKED"))!=0
    row=next(iter(receipts.rows.values()))
    assert len(calls)==1 and row["state"]=="FAILED" and row["error_code"]=="QUERY_NON_OK"


def test_review_due_continues_one_query_and_marks_due(tmp_path):
    now=datetime(2026,9,15,3,13,tzinfo=timezone.utc); calls=[]; receipts=MemoryReceipts(); cfg,bundle=config(tmp_path,now)
    assert run_ai_infra_capex_shadow_once_v1(cfg,deps(now,receipts,calls,bundle))==0
    row=next(iter(receipts.rows.values()))
    assert len(calls)==1 and row["lifecycle_status"]=="REVIEW_DUE" and row["review_due"] is True


def test_bundle_drift_bad_authority_lock_and_expiry_are_query_zero(tmp_path):
    now=datetime(2026,8,9,3,13,tzinfo=timezone.utc)
    for case in ("bundle","authority","lock","expiry"):
        calls=[]; receipts=MemoryReceipts(); cfg,bundle=config(tmp_path/case,now)
        scheduler=True
        if case=="bundle": next(iter(bundle.values())).write_text("changed")
        if case=="authority": scheduler=False
        if case=="lock": receipts.locked=True
        if case=="expiry": cfg["expiry_utc"]="2026-08-09T03:13:00.000000Z"
        assert run_ai_infra_capex_shadow_once_v1(cfg,deps(now,receipts,calls,bundle,scheduler=scheduler))!=0
        assert calls==[]


def test_path_digest_excludes_device_and_bundle_order(tmp_path):
    path=tmp_path/"db"; path.write_bytes(b"x"); path.chmod(0o600)
    import hashlib,json
    info=path.stat(); pre={"inode":info.st_ino,"mode":384,"nlink":1,"path":str(path.resolve()),"uid":info.st_uid}
    assert canonical_path_identity_digest_v1(path)==hashlib.sha256(json.dumps(pre,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    assert "st_dev" not in Path(__file__).parents[1].joinpath("src/tokmo/programs/ai_infra_capex_shadow_runtime.py").read_text()
    assert BUNDLE_MEMBERS_V1==tuple(sorted(BUNDLE_MEMBERS_V1))


@pytest.mark.parametrize("field,value",[("activation_utc",1),("review_due_utc",False),("expiry_utc",[])])
def test_timestamp_config_types_fail_before_query(tmp_path,field,value):
    now=datetime(2026,8,9,3,13,tzinfo=timezone.utc); calls=[]; receipts=MemoryReceipts(); cfg,bundle=config(tmp_path,now); cfg[field]=value
    assert run_ai_infra_capex_shadow_once_v1(cfg,deps(now,receipts,calls,bundle))!=0
    assert calls==[] and receipts.rows=={}
