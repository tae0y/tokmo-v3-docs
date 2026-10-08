"""Private atomic receipts and one nonblocking lock for the G1-C shadow lane."""
from __future__ import annotations

import fcntl
import json
import os
import re
import stat
from pathlib import Path

_KEYS={
 "schema","tick_identity","state","status","error_code","phase","cadence_version","utc_hour","scheduled_at_utc","started_at_utc","finished_at_utc","duration_ms","policy","lifecycle_status","review_due","query_count","http_starts","model_starts","delivery_count","news_write_count","retry_count","database_validation_row_count","temporal_candidate_count","temporal_candidate_normalized_bytes","total_matched_rows","total_matched_distinct_stories","matched_distinct_source_count","returned_count","returned_distinct_story_count","truncated_rows","truncated_bytes","future_source_skew_count","future_availability_count","sensor_state","evidence_count","contradiction_count","missing_need_count","query_digest","result_digest","source_snapshot_digest","all_match_semantic_digest","pack_digest","spec_digest","classifier_digest","source_path_identity_digest"
}
_STATES={"RESERVED","SUCCEEDED","FAILED","UNKNOWN","EXPIRED"}
_STATUS={"OK","QUERY_FAILED","BLOCKED","EXPIRED","UNKNOWN"}
_PHASE={"RESERVED","PREFLIGHT","QUERY","EVALUATE","SETTLE"}
_ERROR={"NONE","CONFIG_INVALID","DIGEST_MISMATCH","PATH_IDENTITY_MISMATCH","PATH_METADATA_UNSAFE","SCHEDULER_AUTHORITY_INVALID","WINDOW_INVALID","LOCK_BUSY","DUPLICATE_TICK","ORPHANED_RESERVATION","QUERY_NON_OK","QUERY_EXCEPTION","QUERY_COUNT_VIOLATION","EVALUATION_INVALID","PRIVACY_REJECTED","RECEIPT_IO_FAILED","RUNTIME_BUNDLE_MISMATCH","EXPIRED"}

def _validate(value):
    if set(value)-_KEYS: raise ValueError("receipt key")
    for key,item in value.items():
        if item is None or type(item) in (bool,int):
            if type(item) is int and item<0: raise ValueError("negative receipt count")
            continue
        if type(item) is not str or len(item)>128: raise ValueError("receipt scalar")
        if key.endswith("digest") or key=="tick_identity":
            if re.fullmatch(r"[0-9a-f]{64}",item) is None: raise ValueError("receipt digest")
        elif "/" in item or "\\" in item or "://" in item or any(word in item.lower() for word in ("token=","secret=","traceback")): raise ValueError("receipt privacy")
    if value.get("state") not in _STATES: raise ValueError("receipt state")
    if value.get("status") is not None and value["status"] not in _STATUS: raise ValueError("receipt status")
    if value.get("phase") is not None and value["phase"] not in _PHASE: raise ValueError("receipt phase")
    if value.get("error_code") is not None and value["error_code"] not in _ERROR: raise ValueError("receipt error")
    if value.get("policy") is not None and value["policy"] not in ("PAUSED","SHADOW"): raise ValueError("receipt policy")
    if value.get("lifecycle_status") is not None and value["lifecycle_status"] not in ("SHADOW","ACTIVE","REVIEW_DUE","DORMANT","RETIRED","EXPIRED"): raise ValueError("receipt lifecycle")

class ShadowReceiptStoreV1:
    def __init__(self,root,lock):
        self.root=Path(root); self.lock=Path(lock)
        for directory in (self.root,self.lock.parent):
            if directory.exists() and directory.is_symlink(): raise ValueError("unsafe receipt root")
            directory.mkdir(parents=True,exist_ok=True); os.chmod(directory,0o700)
        if self.lock.exists() and (self.lock.is_symlink() or self.lock.stat().st_uid!=os.getuid()): raise ValueError("unsafe lock")
    def acquire(self):
        fd=os.open(self.lock,os.O_RDWR|os.O_CREAT,0o600)
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: os.close(fd); return None
        return fd
    def release(self,token):
        fcntl.flock(token,fcntl.LOCK_UN); os.close(token)
    def _write(self,path,value):
        _validate(value); data=(json.dumps(value,sort_keys=True,separators=(",",":"))+"\n").encode()
        tmp=path.with_name(f".{path.name}.{os.getpid()}.tmp"); fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,"wb",closefd=False) as out: out.write(data); out.flush(); os.fsync(out.fileno())
            os.close(fd); fd=-1; os.replace(tmp,path); parent=os.open(self.root,os.O_RDONLY); os.fsync(parent); os.close(parent)
        finally:
            if fd>=0: os.close(fd)
            tmp.unlink(missing_ok=True)
    def read(self,identity):
        path=self.root/f"{identity}.json"
        if not path.exists(): return None
        if path.is_symlink() or path.stat().st_uid!=os.getuid() or stat.S_IMODE(path.stat().st_mode)!=0o600: raise ValueError("unsafe receipt")
        value=json.loads(path.read_text()); _validate(value)
        if value.get("tick_identity")!=identity or value.get("state") not in _STATES: raise ValueError("receipt identity")
        return value
    def reserve(self,identity,fields):
        if re.fullmatch(r"[0-9a-f]{64}",identity) is None or self.read(identity) is not None: raise ValueError("terminal identity")
        value={"schema":"tokmo.g1c.shadow-receipt.v1","tick_identity":identity,"state":"RESERVED","status":None,"phase":"RESERVED",**fields}; self._write(self.root/f"{identity}.json",value); return value
    def settle(self,identity,state,fields):
        if state not in _STATES-{"RESERVED"}: raise ValueError("terminal state")
        value=self.read(identity)
        if value is None or value["state"]!="RESERVED": raise ValueError("not reserved")
        value={key:None for key in _KEYS}; value.update(self.read(identity) or {}); value.update(fields,state=state,schema="tokmo.g1c.shadow-receipt.v1",tick_identity=identity)
        if set(value)!=_KEYS: raise ValueError("terminal receipt shape")
        self._write(self.root/f"{identity}.json",value); return value
    def reconcile_unknown(self,identity): return self.settle(identity,"UNKNOWN",{"status":"UNKNOWN","error_code":"ORPHANED_RESERVATION"})
