#!/usr/bin/env python3
"""Read non-ETF pension totals; no registry or holdings changes."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tokmo.programs.quant_pension_summary import read_pension_summary
from tokmo.records.quant_decision_reports import atomic_private


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workbook", type=Path, required=True)
    p.add_argument(
        "--classification",
        type=Path,
        required=True,
        help="JSON mapping exact product names to etf/cash_like/bond/fund/other",
    )
    p.add_argument("--account", required=True)
    p.add_argument(
        "--source-as-of",
        required=True,
        help="Confirmed workbook valuation date/time with timezone",
    )
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args(argv)
    result = read_pension_summary(
        a.workbook,
        json.loads(a.classification.read_text()),
        account=a.account,
        source_as_of=a.source_as_of,
    )
    atomic_private(a.output, result)
    print(
        json.dumps(
            {"output": str(a.output), "available_cash_inferred": False},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
