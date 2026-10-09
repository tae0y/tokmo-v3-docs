"""Private analysis files, independent of the runner's order/approval journal."""

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from tokmo.records.durability import fsync_directory
from tokmo.records.quant_decision_reports import atomic_private


class RotationAnalysisStore:
    def __init__(self, root):
        self.root = Path(root).absolute()
        if any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise ValueError("analysis_path_symlink")

    def path(self, policy, day):
        return self.root / (day.isoformat() + "-" + policy.experiment_id + ".json")

    def read(self, policy, day):
        path = self.path(policy, day)
        if path.is_symlink():
            raise ValueError("analysis_path_symlink")
        return json.loads(path.read_text()) if path.exists() else None

    def write(self, policy, day, document):
        path = self.path(policy, day)
        if path.is_symlink():
            raise ValueError("analysis_path_symlink")
        atomic_private(path, document)
        fsync_directory(path.parent)

    @contextmanager
    def lock(self, policy, day):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.path(policy, day).with_suffix(".lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            os.close(fd)

    def record_delivery(self, policy, day, receipt):
        atomic_private(self.path(policy, day).with_suffix(".delivery.json"), receipt)

    def delivery_receipt(self, policy, day):
        path = self.path(policy, day).with_suffix(".delivery.json")
        return json.loads(path.read_text()) if path.exists() else None
