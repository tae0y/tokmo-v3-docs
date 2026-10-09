"""One private JSON state, a Mac advisory writer lock and atomic durable replace."""

import fcntl
import json
from pathlib import Path

from tokmo.records.quant_decision_reports import atomic_private


class PaperStore:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "state.json"

    def read(self):
        if not self.path.is_file():
            raise ValueError("PAPER state not initialized")
        return json.loads(self.path.read_text())

    def transaction(self, change, *, initialize=False, commit_check=None):
        if not initialize and not self.path.is_file():
            raise ValueError("PAPER state not initialized")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / "writer.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("PAPER writer busy") from exc
            try:
                state = self.read() if self.path.exists() else None
                previous = json.dumps(state, sort_keys=True)
                new_state = change(state)
                if json.dumps(new_state, sort_keys=True) != previous:
                    atomic_private(self.path, new_state)
                    try:
                        if commit_check is not None and not commit_check():
                            raise ValueError("intent write missed its future boundary")
                    except Exception:
                        # Still holding the writer lock: no advance can consume
                        # an intent whose durable write crossed the boundary.
                        atomic_private(self.path, json.loads(previous))
                        raise
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        return new_state
