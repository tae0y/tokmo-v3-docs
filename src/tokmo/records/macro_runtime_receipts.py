"""Private atomic JSON receipts for the macro shadow lane."""
import fcntl
import json
import os
from pathlib import Path


class MacroRuntimeReceiptStore:
    def __init__(self, root):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True); os.chmod(self.root, 0o700)
        self.lock = self.root / "receipts.lock"

    def reserve(self, identity, fields):
        def write(path,value):
            data=(json.dumps(value,sort_keys=True,separators=(",",":"))+"\n").encode(); tmp=path.with_name(f".{path.name}.{os.getpid()}.tmp"); descriptor=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            try:
                with os.fdopen(descriptor,"wb",closefd=False) as out: out.write(data); out.flush(); os.fsync(out.fileno())
                os.close(descriptor); descriptor=-1; os.replace(tmp,path); parent=os.open(self.root,os.O_RDONLY); os.fsync(parent); os.close(parent)
            finally:
                if descriptor>=0: os.close(descriptor)
                tmp.unlink(missing_ok=True)
        if not isinstance(identity, str) or len(identity) != 64: raise ValueError("invalid identity")
        fd=os.open(self.lock,os.O_RDWR|os.O_CREAT,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX); path=self.root/f"{identity}.json"
            if path.exists(): raise ValueError("terminal identity")
            value={"schema":"tokmo.macro.runtime-receipt.v1","identity":identity,"state":"RESERVED",**dict(fields)}
            write(path,value); return value
        finally: os.close(fd)

    def settle(self, identity, state, fields):
        def write(path,value):
            data=(json.dumps(value,sort_keys=True,separators=(",",":"))+"\n").encode(); tmp=path.with_name(f".{path.name}.{os.getpid()}.tmp"); descriptor=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            try:
                with os.fdopen(descriptor,"wb",closefd=False) as out: out.write(data); out.flush(); os.fsync(out.fileno())
                os.close(descriptor); descriptor=-1; os.replace(tmp,path); parent=os.open(self.root,os.O_RDONLY); os.fsync(parent); os.close(parent)
            finally:
                if descriptor>=0: os.close(descriptor)
                tmp.unlink(missing_ok=True)
        if state not in ("SUCCEEDED","FAILED","UNKNOWN","SKIPPED"): raise ValueError("invalid state")
        descriptor=os.open(self.lock,os.O_RDWR|os.O_CREAT,0o600)
        try:
            fcntl.flock(descriptor,fcntl.LOCK_EX); value=self.read_receipt(identity)
            if value is None or value["state"] != "RESERVED": raise ValueError("not reserved")
            value.update(dict(fields)); value["state"]=state; write(self.root/f"{identity}.json",value); return value
        finally: os.close(descriptor)

    def read_receipt(self, identity):
        path=self.root/f"{identity}.json"
        if not path.exists(): return None
        value=json.loads(path.read_text())
        if value.get("identity") != identity: raise ValueError("receipt mismatch")
        return value

    def reconcile_unknown(self):
        def write(path,value):
            data=(json.dumps(value,sort_keys=True,separators=(",",":"))+"\n").encode(); tmp=path.with_name(f".{path.name}.{os.getpid()}.tmp"); descriptor=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            try:
                with os.fdopen(descriptor,"wb",closefd=False) as out: out.write(data); out.flush(); os.fsync(out.fileno())
                os.close(descriptor); descriptor=-1; os.replace(tmp,path); parent=os.open(self.root,os.O_RDONLY); os.fsync(parent); os.close(parent)
            finally:
                if descriptor>=0: os.close(descriptor)
                tmp.unlink(missing_ok=True)
        descriptor=os.open(self.lock,os.O_RDWR|os.O_CREAT,0o600)
        try:
            fcntl.flock(descriptor,fcntl.LOCK_EX)
            for path in self.root.glob("*.json"):
                value=json.loads(path.read_text())
                if value.get("state")=="RESERVED": value["state"]="UNKNOWN"; write(path,value)
        finally: os.close(descriptor)
