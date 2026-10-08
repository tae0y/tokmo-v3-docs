#!/usr/bin/env python3
"""Read-only local Quant preview progress; no credential, network, or delivery."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from tokmo.programs.quant_discovery_runtime import project_personal_symbols, project_pension_symbols
from tokmo.programs.quant_preview import build_local_preview
from tokmo.records.current_holdings_store import CurrentHoldingsStore
from tokmo.records.pension_revisions import PensionRegistryStore
from tokmo.values.quant_discovery import load_peer_registry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(__file__).resolve().parents[1] / "var" / "data"
DEFAULT_REPORT_ROOT = DATA_ROOT / "ops" / "quant-g2-preview"
LEGACY_REPORT_ROOT = DATA_ROOT / "ops" / "quant-bounded-canary" / "report-store"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Show local-only Quant preview readiness")
    parser.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT)
    parser.add_argument("--target-completed-sessions", type=int, default=10)
    parser.add_argument("--peer-review-max-age-days", type=int, default=90)
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    stores = DATA_ROOT / "stores"
    holdings = CurrentHoldingsStore(stores / "current_holdings.json", clock=lambda: now).read()
    pension = PensionRegistryStore(stores / "pension_registry.sqlite3", clock=lambda: now).read_active_revision_ro()
    personal = tuple((symbol, market.value) for symbol, market in project_personal_symbols(holdings))
    pension_symbols = tuple((symbol, market.value) for symbol, market in project_pension_symbols(pension).symbols)
    result = build_local_preview(
        report_root=args.report_root,
        registry=load_peer_registry(PROJECT_ROOT / "config" / "quant_peer_registry.json"),
        personal_symbols=personal, pension_symbols=pension_symbols, as_of=now.date(),
        target_completed_sessions=args.target_completed_sessions,
        peer_review_max_age_days=args.peer_review_max_age_days,
        legacy_report_root=LEGACY_REPORT_ROOT,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
