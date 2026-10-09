"""Optional quota-reset lookup from the Hermes quota snapshot (model-quota-latest.json).

The snapshot is refreshed twice a day by a Hermes job and lists, per provider, the usage windows
(``label``, ``remaining`` in percentage points, ``reset_at`` as an ISO instant with offset, so the
provider's own clock zone, e.g. Hong Kong for Z.AI, never matters). A chain member that failed
with QUOTA asks ``reset(provider, at)``: the earliest future reset among the *exhausted* windows,
or None when the file is missing, stale, unreadable, or names no exhausted window. None means
"unknown" and the caller keeps its flat probe interval. Installs without Hermes simply never
have the file.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from tokmo.records.news_decision_store import timestamp

SNAPSHOT_MAX_AGE = timedelta(
    hours=24
)  # older observations say nothing about the current block
EXHAUSTED_REMAINING = 0.5  # percentage points at or below which a window is the block


def quota_reset_reader(path):
    """Build ``reset(provider, at) -> aware datetime | None`` over the snapshot at ``path``.

    The file is read on every call so a fresher snapshot is picked up by the next failure."""
    path = Path(path)

    def reset(provider, at):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            observed = timestamp(payload["observed_at"])
            windows = payload["providers"][provider]["windows"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not isinstance(windows, list) or not (
            at - SNAPSHOT_MAX_AGE <= observed <= at + timedelta(minutes=5)
        ):
            return None
        candidates = []
        for window in windows:
            if not isinstance(window, dict):
                continue
            try:
                remaining = float(window["remaining"])
                when = timestamp(window["reset_at"]) if window.get("reset_at") else None
            except (KeyError, TypeError, ValueError):
                continue
            if when is not None and remaining <= EXHAUSTED_REMAINING and when > at:
                candidates.append(when)
        return min(candidates) if candidates else None

    return reset
