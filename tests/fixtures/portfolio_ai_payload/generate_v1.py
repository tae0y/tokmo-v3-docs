#!/usr/bin/env python3
"""Generate/check canonical synthetic portfolio AI payload fixtures."""

import argparse
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from tokmo.values.analysis_contracts import AnalysisKind, MaterialKindV1
from tokmo.values.portfolio_ai_payload import (
    PortfolioCandidateSummaryV1,
    PortfolioEventCandidatePayloadV1,
    PortfolioPositionDetailV1,
    canonical_portfolio_payload_bytes_v1,
    compute_portfolio_payload_digest_v1,
)
from tokmo.values.portfolio_calculations import (
    FxStatus,
    PortfolioScope,
    PricingState,
    RatioState,
)
from tokmo.values.session_gate import Market

NOW = datetime(2026, 8, 4, tzinfo=UTC)
H64 = "a" * 64
ROOT = Path(__file__).parent


def make(scope, market, symbol, currency):
    usd = currency == "USD"
    detail = PortfolioPositionDetailV1(
        scope,
        market,
        symbol,
        currency,
        Decimal(2),
        Decimal(80),
        Decimal(100),
        Decimal(160),
        Decimal(200),
        Decimal(300000) if usd else None,
        Decimal(40),
        Decimal(60000) if usd else None,
        RatioState.DEFINED,
        Decimal("0.25"),
        PricingState.PRICED,
        Decimal(1),
        Decimal(1),
        Decimal(1),
        Decimal(1),
        Decimal(1),
        Decimal(1),
        NOW,
        NOW,
        H64 if usd else None,
    )
    summary = PortfolioCandidateSummaryV1(
        scope,
        FxStatus.AVAILABLE if usd else FxStatus.NOT_REQUIRED_SINGLE_KRW,
        Decimal(300000) if usd else Decimal(200),
        Decimal(60000) if usd else Decimal(40),
        RatioState.DEFINED,
        Decimal("0.25"),
        market,
        currency,
        1,
        1,
        0,
        Decimal(160),
        Decimal(160),
        Decimal(200),
        Decimal(300000) if usd else None,
        Decimal(40),
        Decimal(60000) if usd else None,
        RatioState.DEFINED,
        Decimal("0.25"),
        RatioState.DEFINED,
        Decimal(1),
        H64 if usd else None,
    )
    candidate = PortfolioEventCandidatePayloadV1(
        1,
        AnalysisKind.EVENT,
        f"synthetic-{scope.value}",
        f"delta-{scope.value}",
        MaterialKindV1.QUOTE_STATE,
        market,
        symbol,
        7,
        H64,
        "b" * 64,
        "event-admission-v1",
        "c" * 64,
        ("event_quote_threshold_move",),
        None,
        detail,
        summary,
        None,
        (),
        H64,
    )
    return replace(
        candidate, payload_digest=compute_portfolio_payload_digest_v1(candidate)
    )


def generated():
    rows = {
        "personal_v1.json": canonical_portfolio_payload_bytes_v1(
            make(PortfolioScope.PERSONAL, Market.KR, "005930", "KRW")
        ),
        "pension_v1.json": canonical_portfolio_payload_bytes_v1(
            make(PortfolioScope.PENSION, Market.US, "SYNUS1", "USD")
        ),
    }
    files = {}
    for name, raw in rows.items():
        payload = json.loads(raw)
        files[name] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "payload_digest": payload["payload_digest"],
        }
    manifest = {
        "domain": "tokmo-v3:portfolio-ai-payload-fixtures:v1",
        "files": files,
    }
    rows["manifest_v1.json"] = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return rows


def main():
    # PositionCalcV1 source-freshness fields are intentionally included by
    # dataclass serialization while the additive payload schema remains v1.
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--write", action="store_true")
    args = parser.parse_args()
    rows = generated()
    if args.write:
        for name, raw in rows.items():
            (ROOT / name).write_bytes(raw)
        return
    drift = [
        name
        for name, raw in rows.items()
        if not (ROOT / name).exists() or (ROOT / name).read_bytes() != raw
    ]
    if drift:
        raise SystemExit("fixture drift: " + ",".join(drift))


if __name__ == "__main__":
    main()
