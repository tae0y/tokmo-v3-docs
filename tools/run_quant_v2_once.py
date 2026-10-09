#!/usr/bin/env python3
"""Local fixture-only one-shot; never reads credentials or production runtime."""

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

from tokmo.programs.quant_discovery_runtime import run_quant_once
from tokmo.values.quant_discovery import QuantMarketProfileV1


class _Reply:
    def __init__(self, body):
        self.body = json.dumps({"result": body}).encode()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args(argv)
    data = json.loads(args.fixture.read_text())
    replies = data["replies"]

    def transport(request):
        operation = request.operation.value
        if operation == "RANKINGS":
            key = f"RANKINGS:{request.market_country}"
        elif operation == "STOCKS":
            key = f"STOCKS:{','.join(request.symbols)}"
        else:
            key = f"STOCK_CANDLES:{request.symbol}"
        return _Reply(replies[key])

    deps = SimpleNamespace(
        root=Path(data["root"]),
        token=data["token"],
        transport=transport,
        clock=lambda: datetime.now(UTC),
        profiles=(QuantMarketProfileV1.KR,),
        completed_sessions={
            QuantMarketProfileV1.KR: date.fromisoformat(data["completed_session"])
        },
        profile_revision=data["profile_revision"],
        report_revision=data["report_revision"],
        shortlist_cap=data["shortlist_cap"],
        candle_start_cap=data["candle_start_cap"],
        public_symbols=(),
        personal_symbols=(),
        pension_revision=None,
        reviewed_peers=(),
    )
    result = run_quant_once(deps=deps, now=datetime.now(UTC))
    receipt = result.receipt
    print(
        json.dumps(
            {
                "identity_sha256": receipt.identity.identity_sha256,
                "outcome": receipt.outcome,
                "operation_counts": receipt.operation_counts,
                "report_sha256": receipt.report_sha256,
                "delivery_state": receipt.delivery_state,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
