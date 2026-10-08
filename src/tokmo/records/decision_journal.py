"""Append-only private decision JSONL leaf."""

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from tokmo.records.durability import fsync_directory
from tokmo.values import DecisionRecord


class DecisionJournal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: DecisionRecord) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        line = (json.dumps(_as_json_row(record), separators=(",", ":")) + "\n").encode()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        fsync_directory(self.path.parent)


def _as_json_row(record: DecisionRecord) -> dict[str, str | None]:
    row = asdict(record)
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in row.items()
    }
