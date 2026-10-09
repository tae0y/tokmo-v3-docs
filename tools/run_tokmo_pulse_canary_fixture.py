#!/usr/bin/env python3
"""Fixture CLI for the G2.5 bounded live canary (test/dev only).

This tool lives outside src/tokmo/ so it is not subject to the architecture
import seal. It hosts the fixture JSON→TossCurrentPriceFact composition that
was moved OUT of the production src module (m-2). The production CLI
(run_canary_cli in pulse_canary.py) is live-only and does not accept --fixture.

Usage:
    python tools/run_tokmo_pulse_canary_fixture.py \\
        --mode bounded-live-canary \\
        --market KR --symbol 005930 \\
        --root /path/to/isolated/evidence/root \\
        --project-root /path/to/tokmo-v3 \\
        --max-ticks 5 --max-provider-starts 10 \\
        --fixture /path/to/canary_fixture.json
"""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Ensure src/ is on the import path when run from the project root.
project_root = Path(__file__).resolve().parent.parent
src_path = project_root / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

from tokmo.programs.pulse_canary import (
    CANARY_MAX_PROVIDER_STARTS_DEFAULT,
    CANARY_MAX_PROVIDER_STARTS_LIMIT,
    CANARY_MAX_TICKS,
    CanaryConfigV1,
    CanaryDepsV1,
    _validate_evidence_root,
    _validate_root_disjoint,
    run_canary_session,
)
from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
from tokmo.values.market_observations import TossCurrentPriceFact
from tokmo.values.session_gate import Market


def build_fixture_deps(fixture_path_str: str, t0: datetime) -> "CanaryDepsV1 | None":
    """Build CanaryDepsV1 from a fixture JSON file (test/dev only).

    This function was moved out of src/tokmo/programs/pulse_canary.py (m-2)
    so that the production src module does not contain fixture-to-fact
    composition or import TossCurrentPriceFact for fixture purposes.

    Returns None if the fixture file cannot be read.
    """
    try:
        fixture = json.loads(Path(fixture_path_str).read_text())
    except (OSError, ValueError):
        return None

    def token_fetch():
        return fixture.get("token", "fixture-token")

    def quotes_fetch(market_arg, symbols, *, bearer=None):
        table = fixture.get("quotes", {})
        facts = []
        failed = []
        for sym in symbols:
            raw = table.get(sym)
            if raw is None:
                failed.append(sym)
                continue
            ts = datetime.fromisoformat(raw["timestamp"].replace("Z", "+00:00"))
            fa = datetime.fromisoformat(raw["fetched_at"].replace("Z", "+00:00"))
            facts.append(
                TossCurrentPriceFact(
                    source="toss-current-price-v1",
                    symbol=sym,
                    last_price=raw["last_price"],
                    currency_raw=raw["currency"],
                    timestamp=ts,
                    fetched_at=fa,
                )
            )
        return PulseQuoteBatchOutcomeV1("success", tuple(facts), tuple(failed))

    def noop_sleep(target_utc):
        pass

    return CanaryDepsV1(
        clock=lambda: t0,
        token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=noop_sleep,
    )


def run_fixture_cli(argv: "list[str] | None" = None) -> int:
    """Fixture CLI entrypoint (test/dev only).

    Accepts --root (test-injectable) and --fixture. Does NOT require
    --canonical-root or --base-commit (those are live-only gates).
    """
    parser = argparse.ArgumentParser(
        prog="tokmo-pulse-canary-fixture",
        add_help=True,
    )
    parser.add_argument("--mode", default="bounded-live-canary")
    parser.add_argument("--market", default=None)
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--root", default=None)
    parser.add_argument("--canonical-root", default=None)
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--max-ticks", type=int, default=CANARY_MAX_TICKS)
    parser.add_argument(
        "--max-provider-starts", type=int, default=CANARY_MAX_PROVIDER_STARTS_DEFAULT
    )
    parser.add_argument("--fixture", default=None)
    parser.add_argument(
        "--t0", default=None, help="t0 UTC ISO timestamp (default: now)"
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 1

    def refuse(reason: str) -> int:
        print(f"tokmo-pulse-canary-fixture refusal={reason}", file=sys.stderr)
        return 1

    if args.mode != "bounded-live-canary":
        return refuse("unsupported_mode")

    if not args.market or not args.symbol or not args.root:
        return refuse("market_symbol_root_required")

    try:
        market = Market(args.market)
    except ValueError:
        return refuse("invalid_market")

    if args.max_ticks != CANARY_MAX_TICKS:
        return refuse(f"max_ticks_must_be_{CANARY_MAX_TICKS}")
    if args.max_provider_starts > CANARY_MAX_PROVIDER_STARTS_LIMIT:
        return refuse(f"max_provider_starts_exceeds_{CANARY_MAX_PROVIDER_STARTS_LIMIT}")
    if args.max_provider_starts <= 0:
        return refuse("max_provider_starts_must_be_positive")

    try:
        config = CanaryConfigV1(
            market=market,
            symbol=args.symbol,
            max_ticks=args.max_ticks,
            max_provider_starts=args.max_provider_starts,
        )
    except ValueError as error:
        return refuse(f"invalid_config:{error}".replace(" ", "_"))

    # Determine t0
    if args.t0:
        try:
            t0 = datetime.fromisoformat(args.t0.replace("Z", "+00:00"))
        except ValueError:
            return refuse("invalid_t0")
    else:
        epoch = time.time()
        t0_epoch = int(epoch) - (int(epoch) % 60) + 60
        t0 = datetime.fromtimestamp(t0_epoch, tz=UTC)

    if t0.microsecond != 0 or t0.second % 60 != 0:
        return refuse("t0_must_be_whole_second_aligned_to_60s_grid")

    canonical_root = Path(args.canonical_root) if args.canonical_root else None
    evidence_root = Path(args.root)

    if not args.fixture:
        return refuse("fixture_required_for_fixture_cli")

    try:
        ev_validated = _validate_evidence_root(evidence_root)
        _validate_root_disjoint(ev_validated, canonical_root)
    except ValueError as e:
        return refuse(f"root_validation:{e}".replace(" ", "_"))

    deps = build_fixture_deps(args.fixture, t0)
    if deps is None:
        return refuse("fixture_unreadable")

    clock_fn = lambda: t0

    result = run_canary_session(
        root=evidence_root,
        config=config,
        deps=deps,
        t0=t0,
        canonical_root=canonical_root,
    )

    summary = {
        "session_id": result.session_id,
        "completed_ticks": result.completed_ticks,
        "total_provider_starts": result.total_provider_starts,
        "stop_reason": result.stop_reason,
        "market": config.market.value,
        "symbol": config.symbol,
        "max_ticks": config.max_ticks,
        "max_provider_starts": config.max_provider_starts,
    }
    print(json.dumps(summary, sort_keys=True))

    if result.stop_reason == "all_ticks_complete" and result.is_fresh:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(run_fixture_cli(sys.argv[1:]))
