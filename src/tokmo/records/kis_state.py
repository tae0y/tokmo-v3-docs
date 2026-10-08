"""Private atomic JSON state using existing persistence mechanics, no DB."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat

from tokmo.records.atomic_json import read_atomic_json, write_atomic_json
from tokmo.values.kis_etf import KisError


class KisState:
    def __init__(self, root):
        self.root = Path(root)
        if any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise KisError("state_unsafe")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.stat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
            raise KisError("state_unsafe")

    @contextmanager
    def locked(self, name="connection"):
        if name not in {"connection", "refresh"}:
            raise KisError("lock_identity")
        fd = os.open(self.root / f"{name}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
                raise KisError("lock_unsafe")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise KisError("connection_busy") from None
            yield
        finally:
            os.close(fd)

    def read_token(self):
        path = self.root / "token.json"
        if not path.exists() and not path.is_symlink():
            return None
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise KisError("token_cache_unsafe")
        try:
            return json.loads(read_atomic_json(path, max_bytes=32768))
        except (ValueError, OSError):
            raise KisError("token_cache_invalid") from None

    def save_token(self, document):
        write_atomic_json(self.root / "token.json", json.dumps(document, allow_nan=False).encode(), max_bytes=32768)

    def save_fact(self, fact):
        operation, symbol = fact["operation"], fact["symbol"]
        if operation not in {"components", "nav", "nav_daily"} or not re.fullmatch(r"[A-Z0-9]{6}", symbol):
            raise KisError("fact_identity")
        write_atomic_json(self.root / f"{operation}-{symbol}.json",
                          json.dumps(fact, ensure_ascii=False, allow_nan=False).encode(), max_bytes=2097152)

    def save_quote_source(self, operation, symbol, payload):
        """Retain successful public quote bodies, never auth/error bodies."""
        if operation not in {"components", "nav", "nav_daily"} or not re.fullmatch(r"[A-Z0-9]{6}", symbol):
            raise KisError("source_identity")
        data = json.loads(payload)
        if type(data) is not dict or data.get("rt_cd") != "0":
            raise KisError("source_not_quote_success")
        write_atomic_json(self.root / f"source-{operation}-{symbol}.json", payload, max_bytes=1048576)


    def read_refresh_state(self):
        try:
            return json.loads(read_atomic_json(self.root / "refresh.json", max_bytes=32768))
        except FileNotFoundError:
            return {"attempts": {}, "total_requests": 0, "total_auth_requests": 0}
        except (ValueError, OSError):
            raise KisError("refresh_state_invalid") from None

    def save_refresh_state(self, document):
        write_atomic_json(self.root / "refresh.json", json.dumps(document, allow_nan=False).encode(), max_bytes=32768)
