"""Bounded, read-only coverage evidence from the existing polling receipts."""

import json
from datetime import UTC, timedelta
from pathlib import Path

from tokmo.programs.news_poll_policy import NewsGroup, news_tick_id
from tokmo.values.news_collection_diagnostics import collection_complete


def recent_complete_polls(receipts, *, now):
    """Latest successful scheduled poll per source in the last hour.

    A successful source in an otherwise failed group counts. A partially
    persisted runner, a conflict, or only some successful company targets do
    not. EMPTY and unchanged duplicates are normal successful collections.
    This reads a fixed number of known slots, never the whole receipt history.
    """
    latest = {}
    minute = now.astimezone(UTC).replace(second=0, microsecond=0)
    for offset in range(61):
        scheduled = minute - timedelta(minutes=offset)
        for group in NewsGroup:
            for fast in (False, True):
                try:
                    tick = news_tick_id(group, scheduled, fast_primary=fast)
                except ValueError:
                    continue
                try:
                    payload = json.loads((Path(receipts) / f"{tick}.json").read_text())
                    if payload["tick_id"] != tick:
                        continue
                    grouped = {}
                    for entry in payload["sources"]:
                        grouped.setdefault(entry["source"], []).append(entry)
                    for source, entries in grouped.items():
                        if source in latest:
                            continue
                        if all(
                            collection_complete(entry) and entry.get("conflict", 0) == 0
                            for entry in entries
                        ):
                            latest[source] = scheduled
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    # An unreadable receipt is not proof of successful coverage.
                    continue
    return latest
