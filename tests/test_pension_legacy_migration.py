"""Tests for V2 MD → V3 pension migration adapter."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tokmo.programs.pension_legacy_migration import (
    _PENSION_SYMBOL_MAP,
    PensionTradeRecordV1,
    migrate_legacy_md,
)
from tokmo.values.pension_holdings import PensionHoldingsRevisionV1

_KST = ZoneInfo("Asia/Seoul")

# Minimal MD fixture mimicking the real V2 file structure
_MD_FIXTURE = """\
# 퇴직연금 거래내역

- 작성일: 2026-07-15
- 원본 시트: Sheet1

## 요약

- 거래 기간: 2025-11-28 ~ 2026-07-13
- 총 거래 건수: 3건
- 현재 보유 상품: 2종목

### 상품별 현재 보유 현황

| 상품 | 현재 보유 수량 |
|---|---:|
| KODEX 200 | 101 |
| SOL AI반도체TOP2플러스 | 67 |

## 거래 이력

| 날짜 | 상품 | 거래유형 | 금액 | 잔액(종목별 수량) | 수량 | 단가 | 거래매체 |
|---|---|---|---:|---:|---:|---:|---|
| 2025-11-28 | KODEX 200 | ETF매수 | 5,570,000원 | 100 | 100 | 55,700원 | 미래에셋증권 통합앱 |
| 2025-11-28 | KODEX 200 | ETF매수 | 55,880원 | 101 | 1 | 55,880원 | 미래에셋증권 통합앱 |
| 2026-07-13 | SOL AI반도체TOP2플러스 | ETF매수 | 1,259,600원 | 67 | 67 | 18,800원 | 미래에셋증권 통합앱 |
"""


@pytest.fixture
def md_path(tmp_path: Path) -> Path:
    p = tmp_path / "retirement_pension_history.md"
    p.write_text(_MD_FIXTURE, encoding="utf-8")
    return p


def test_migration_produces_revision_and_trades(md_path: Path):
    validated = datetime(2026, 8, 5, 12, 0, tzinfo=_KST)
    revision, trades = migrate_legacy_md(md_path, validated_at=validated)

    assert isinstance(revision, PensionHoldingsRevisionV1)
    assert revision.holding_count == 2
    assert revision.liquidation_flag == 0
    assert len(trades) == 3


def test_holdings_have_correct_symbols(md_path: Path):
    validated = datetime(2026, 8, 5, 12, 0, tzinfo=_KST)
    revision, _ = migrate_legacy_md(md_path, validated_at=validated)

    symbols = {h.symbol for h in revision.holdings}
    assert "069500" in symbols  # KODEX 200
    assert "0167A0" in symbols  # SOL AI반도체TOP2플러스


def test_avg_price_computed_from_buy_history(md_path: Path):
    validated = datetime(2026, 8, 5, 12, 0, tzinfo=_KST)
    revision, _ = migrate_legacy_md(md_path, validated_at=validated)

    kodex = next(h for h in revision.holdings if h.symbol == "069500")
    # (5,570,000 + 55,880) / 101 = 55701.78... → quantize to integer
    assert kodex.avg_price in ("55701", "55702")  # depends on rounding mode


def test_source_as_of_is_last_trade_date(md_path: Path):
    validated = datetime(2026, 8, 5, 12, 0, tzinfo=_KST)
    revision, _ = migrate_legacy_md(md_path, validated_at=validated)

    assert revision.source_as_of == datetime(2026, 7, 13, tzinfo=_KST)


def test_trade_records_typed_correctly(md_path: Path):
    validated = datetime(2026, 8, 5, 12, 0, tzinfo=_KST)
    _, trades = migrate_legacy_md(md_path, validated_at=validated)

    assert all(isinstance(t, PensionTradeRecordV1) for t in trades)
    assert trades[0].trade_type == "BUY"
    assert trades[0].trade_quantity == 100


def test_revision_id_is_hex32(md_path: Path):
    validated = datetime(2026, 8, 5, 12, 0, tzinfo=_KST)
    revision, _ = migrate_legacy_md(md_path, validated_at=validated)

    assert len(revision.revision_id) == 32
    assert all(c in "0123456789abcdef" for c in revision.revision_id)


def test_holding_item_uses_correct_currency_and_market(md_path: Path):
    validated = datetime(2026, 8, 5, 12, 0, tzinfo=_KST)
    revision, _ = migrate_legacy_md(md_path, validated_at=validated)

    for h in revision.holdings:
        assert h.currency == "KRW"
        assert h.market_raw == "KR"
        assert h.market_field == "market"


def test_all_currently_held_products_have_symbol_map():
    """Verify all 7 V2 held products are in the symbol map."""
    held = [
        "KODEX 200",
        "KODEX 현대차로보틱스밸류체인TOP3플러스",
        "PLUS 미국S&P500(H)",
        "RISE 삼성전자SK하이닉스채권혼합50",
        "SOL AI반도체TOP2플러스",
        "SOL 화장품TOP3플러스",
        "TIGER 미국AI빅테크10",
    ]
    for name in held:
        assert name in _PENSION_SYMBOL_MAP, f"missing symbol map for: {name}"
