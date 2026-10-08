#!/usr/bin/env python3
"""Bounded operator CLI for the finite OpenDART bulk pilot."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tokmo.opendart_bulk_composition import execute_opendart_bulk
from tokmo.values.news_collection_diagnostics import sanitize_diagnostics
from tokmo.programs.opendart_pilot import pilot_failure_diagnostics

_FAILURE_OUTCOMES = frozenset({"failed", "overflow", "incomplete", "skipped_overlap", "partial", "conflict"})


def failure_details(raw):
    """Forward only the optional closed diagnostic extension to wrappers."""
    try:
        payload = json.loads(raw)
        result = {"diagnostics": sanitize_diagnostics(payload["diagnostics"])}
        if type(payload.get("outcome")) is str and payload["outcome"] in _FAILURE_OUTCOMES:
            result["outcome"] = payload["outcome"]
        return result
    except (ValueError, KeyError, TypeError):
        return {}


def parser():
    value = argparse.ArgumentParser(prog="opendart-bulk")
    value.add_argument("command", choices=("preflight", "baseline", "incremental",
                                           "status", "integrity", "pilot-tick",
                                           "operate", "migrate", "backfill-seed",
                                           "prune", "enrich-init",
                                           "enrich-status", "enrich-pause",
                                           "enrich-resume", "enrich-set-cap"))
    value.add_argument("--home", required=True, type=Path)
    value.add_argument("--expected-preflight-id")
    value.add_argument("--expected-planned-pages", type=int)
    return value


def main(argv=None, *, emit=print):
    args = parser().parse_args(argv)
    started = time.monotonic()
    try:
        result = execute_opendart_bulk(
            home=args.home, command=args.command,
            expected_preflight_id=args.expected_preflight_id,
            expected_planned_pages=args.expected_planned_pages,
            now=datetime.now(timezone.utc))
    except Exception as error:
        emit(json.dumps({"status": "failed", "diagnostics": pilot_failure_diagnostics(
            error, elapsed_ms=int((time.monotonic() - started) * 1000),
            trace=uuid4().hex[:16])}, separators=(",", ":")))
        return 2
    if args.command in {"pilot-tick", "operate"} and result["status"] != "complete":
        outcome = result["status"] if result["status"] in _FAILURE_OUTCOMES else "failed"
        diag = pilot_failure_diagnostics(RuntimeError(),
            elapsed_ms=int((time.monotonic() - started) * 1000), trace=uuid4().hex[:16])
        if outcome in {"incomplete", "partial", "conflict"}:
            diag["cause"] = "INCOMPLETE_STORAGE"
        counts = [result.get(key) for key in ("new", "duplicate", "conflict", "revision")]
        if all(type(value) is int and 0 <= value <= 2**60 for value in counts):
            diag["persisted"] = sum(counts)
        emit(json.dumps({"status": "failed", "outcome": outcome, "diagnostics": diag},
                        sort_keys=True, separators=(",", ":")))
        return 2
    if args.command != "pilot-tick":
        emit(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
