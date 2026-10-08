#!/usr/bin/env python3
"""Finite Stage3 FRED canonical CLI; live execution is mechanically blocked."""

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from tokmo.facts.fred_live import FredTransportOutcome
from tokmo.fred_composition import run_fred_operation
from tokmo.values.fred_macro import ExecutionMode, FredOperation, FredRequest


class _FixtureTransport:
    def __init__(self, body: bytes): self.body = body
    def __call__(self, request, api_key): return FredTransportOutcome("success", self.body)


def _request(operation: FredOperation, release_id: int | None) -> FredRequest:
    if operation is FredOperation.SERIES: return FredRequest(operation, series_id="PAYEMS")
    if operation is FredOperation.SERIES_RELEASE: return FredRequest(operation, series_id="PAYEMS")
    if operation is FredOperation.RELEASE_DATES: return FredRequest(operation, release_id=release_id, limit=10)
    if operation is FredOperation.SERIES_OBSERVATIONS:
        return FredRequest(operation, series_id="PAYEMS", output_type=4,
            realtime_start=date(2026,1,1), realtime_end=date(2026,1,2),
            observation_start=date(2025,12,1), observation_end=date(2025,12,31), limit=10)
    return FredRequest(operation, series_id="PAYEMS", realtime_start=date(2026,1,1),
                       realtime_end=date(2026,1,2), limit=10)


def run_cli(argv, *, wall_clock, monotonic_clock, api_key_loader) -> int:
    parser = argparse.ArgumentParser()
    if "--live" in argv:
        print(json.dumps({"code":"g_live_forbidden","G_LIVE_ALLOWED":"NO"}, sort_keys=True))
        return 2
    parser.add_argument("--operation", choices=[value.value for value in FredOperation], required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--release-id", type=int)
    args = parser.parse_args(argv)
    if args.root is None:
        print(json.dumps({"code":"explicit_root_required","G_LIVE_ALLOWED":"NO"}, sort_keys=True))
        return 2
    root = args.root
    body = args.fixture.read_bytes()
    monotonic_value = monotonic_clock()
    if type(monotonic_value) not in (int, float):
        print(json.dumps({"code":"clock_failure","G_LIVE_ALLOWED":"NO"}, sort_keys=True))
        return 1
    start_instant = wall_clock()
    outcome = run_fred_operation(root=root,
        request=_request(FredOperation(args.operation), args.release_id),
        api_key=api_key_loader(), transport=_FixtureTransport(body), clock=lambda: start_instant,
        execution_mode=ExecutionMode.FIXTURE, started_at=start_instant)
    print(json.dumps({"code":outcome.code, "ordinal":outcome.ordinal,
        "FIXTURE_ONLY":True, "STORE_VERIFIED":outcome.store_evidence == "STORE_VERIFIED",
        "LIVE_VERIFIED":False, "not_verified_against_provider":[1,2,3],
        "G_LIVE_ALLOWED":"NO"}, sort_keys=True))
    return 0 if outcome.code in ("durable_success", "already_attempted_fixture") else 1


def main() -> int:
    return run_cli(sys.argv[1:], wall_clock=lambda: datetime.now(timezone.utc),
                   monotonic_clock=time.monotonic, api_key_loader=lambda: "")


if __name__ == "__main__":
    raise SystemExit(main())
