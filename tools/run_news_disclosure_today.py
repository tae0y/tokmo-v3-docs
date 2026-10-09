#!/usr/bin/env python3
"""Refresh today's DART listing only; preserve the separate full collector."""

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from tokmo.facts.opendart_bulk_live import OpenDartBulkLiveTransport
from tokmo.opendart_bulk_composition import (
    _require_private_credential,
    _require_private_directory,
)
from tokmo.programs.news_decision_config import read_decision_config
from tokmo.programs.opendart_pilot import _collect_day, pilot_failure_diagnostics
from tokmo.records.opendart_disclosures import DisclosureStore
from tokmo.runtime.config import load_opendart_config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args(argv)
    started = time.monotonic()
    try:
        return _run(args, started)
    except Exception as error:
        print(
            json.dumps(
                dict(
                    source="DART_TODAY",
                    status="failed",
                    diagnostics=pilot_failure_diagnostics(
                        error,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        trace=uuid4().hex[:16],
                    ),
                )
            )
        )
        return 2


def _run(args, started):
    root = (args.home / ".local/share/tokmo-v3").resolve()
    if not read_decision_config(root).get("enabled"):
        return 0
    config_dir = args.home / ".config/tokmo-v3"
    _require_private_directory(config_dir)
    credential = config_dir / "opendart.env"
    _require_private_credential(credential)
    config = load_opendart_config(credential)
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Seoul")).date()
    store = DisclosureStore.open(root / "opendart-bulk.sqlite3")
    try:
        if not store.partitions_migrated():
            raise ValueError("DART partition setup missing")
        outcome = _collect_day(
            store,
            today,
            today=today,
            api_key=config.api_key,
            transport=OpenDartBulkLiveTransport(),
            now=now,
            clock=lambda: datetime.now(UTC),
            deadline=now + timedelta(minutes=2),
        )
        payload = dict(source="DART_TODAY", status=outcome)
        print(json.dumps(payload))
        return (
            0 if outcome in ("complete", "pending", "partial", "skipped_overlap") else 2
        )
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
