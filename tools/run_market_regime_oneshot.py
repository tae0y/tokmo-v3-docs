"""Manual operator CLI for the bounded targeted one-shot (G2, §15.4).

This is the ambient edge for exactly one operator-invoked targeted
market-regime analysis: it reads the real wall clock, builds the real
production deps (zero provider calls at construction; ``.env.toss`` is read
and never logged), and calls the operator seam once against the canonical
operational root. No scheduler, cron, or recurring registration of any kind.

Usage: .venv/bin/python tools/run_market_regime_oneshot.py \
    --market KR --symbol 329180 --requested-by user:2026-08-10-329180 \
    [--root ~/.local/share/tokmo-v3]

stdout carries one machine-readable JSON evidence line; stderr carries
refusals. Exit 0 = ok; 1 = skipped/* (wire-zero refusal or dedup, attempt
not consumed) or preflight refusal; 2 = failed/* (consumed failure,
``failed/invalid_target`` included). The JSON ``status``/``reason`` is the
authoritative record.
"""

import argparse
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

_TOOL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = _TOOL_DIR.parent
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tokmo.composition import build_production_cadence_deps
from tokmo.programs.market_regime_shadow import (
    _ANALYSIS_SYMBOL,
    TargetedRegimeTargetV1,
)
from tokmo.programs.market_regime_targeted_oneshot import (
    run_market_regime_targeted_oneshot,
)
from tokmo.runtime.config import ConfigError
from tokmo.values.production_cadence import CADENCE_MODE_LIVE_SHADOW
from tokmo.values.session_gate import Market

# The canonical operational root the recurring cadence runs against.
DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "var" / "data"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _refuse(reason: str) -> int:
    print(f"run-market-regime-oneshot refusal={reason}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run-market-regime-oneshot", add_help=True)
    parser.add_argument(
        "--market", required=True, choices=[market.value for market in Market]
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--requested-by", dest="requested_by", required=True)
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 1
    if not args.requested_by.strip():
        return _refuse("requested_by_required")
    # Target validation before any side effect: the seam's own closed symbol
    # grammar, checked here so an unservable target builds no deps and
    # touches no store. The symbol is otherwise passed through unnormalized.
    if _ANALYSIS_SYMBOL.fullmatch(args.symbol) is None:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "invalid_target",
                    "market": args.market,
                    "symbol": args.symbol[:64],
                },
                sort_keys=True,
            )
        )
        return 2
    target = TargetedRegimeTargetV1(
        Market(args.market),
        args.symbol,
        args.requested_by,
    )
    root = Path(args.root).expanduser()
    try:
        deps = build_production_cadence_deps(
            project_root=PROJECT_ROOT,
            clock=_utc_now,
            shadow_root=root,
            mode=CADENCE_MODE_LIVE_SHADOW,
            pension_composition_enabled=False,
        )
    except (ConfigError, OSError, ValueError):
        return _refuse("credential_preflight_failed")
    try:
        evidence = run_market_regime_targeted_oneshot(
            root=root,
            deps=deps,
            target=target,
            observed_now_utc=_utc_now(),
            invocation_id=secrets.token_hex(16),
        )
    except Exception as error:
        # Masked failure: the type name only, never a message or value.
        masked = type(error).__name__
        print(f"run-market-regime-oneshot failure={masked}", file=sys.stderr)
        print(
            json.dumps(
                {"status": "failed", "reason": f"exception_{masked}"}, sort_keys=True
            )
        )
        return 2
    print(json.dumps(evidence, sort_keys=True))
    if evidence["status"] == "ok":
        return 0
    if evidence["status"] == "skipped":
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
