"""Legacy retirement_pension_history.md → typed pension data adapter.

This is a ONE-TIME migration tool that reads the legacy markdown ledger and produces:
1. A PensionHoldingsRevisionV1 from the current holdings summary table.
2. A typed trade record tuple from the full transaction history.

Average prices are computed from the trade ledger (buy amount / buy quantity per symbol),
matching the legacy projection logic. Only currently-held symbols (quantity > 0) are included
in the holdings revision.

Trade records capture the full history for delta verification and realized P&L tracking.

No network, no production DB, no side effects. Pure function producing typed dataclasses.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from tokmo.values.current_holdings import HoldingItemV1
from tokmo.values.pension_holdings import (
    PENSION_HOLDINGS_SCHEMA_VERSION,
    PENSION_PARSER_VERSION,
    PensionHoldingsRevisionV1,
    pension_semantic_digest,
    pension_reconciliation_digest,
)

_KST = ZoneInfo("Asia/Seoul")

# Product name → (symbol_code, currency, market_raw, market_field)
# These are ETF instruments traded on KRX (KR market, KRW currency).
# Symbol codes are the standard KRX short codes for each ETF.
_PENSION_SYMBOL_MAP: dict[str, tuple[str, str, str, str]] = {
    "KODEX 200": ("069500", "KRW", "KR", "market"),
    # 442300/457540 were wrong KRX short codes (Toss price API returned
    # contract_mismatch — verified live read-only 2026-08-05). Corrected to the
    # live-verified current Toss identities: 0204D0 and 269540.
    "KODEX 현대차로보틱스밸류체인TOP3플러스": ("0204D0", "KRW", "KR", "market"),
    "PLUS 미국S&P500(H)": ("269540", "KRW", "KR", "market"),  # KRX listed, KRW denominated
    # 423160/431190 were also wrong, but unlike the pair above they are live
    # codes for other instruments, so the price API answered instead of
    # rejecting the batch and the error stayed invisible for two months.
    # Corrected against the operator's own broker view and a live read-only
    # probe on 2026-08-06: 0162Z0 = 12,475 and 490090 = 16,520.
    "RISE 삼성전자SK하이닉스채권혼합50": ("0162Z0", "KRW", "KR", "market"),
    "SOL AI반도체TOP2플러스": ("0167A0", "KRW", "KR", "market"),
    "SOL 화장품TOP3플러스": ("0008T0", "KRW", "KR", "market"),
    "TIGER 미국AI빅테크10": ("490090", "KRW", "KR", "market"),
    # Historical (no longer held) — for trade record completeness
    "TIGER 200": ("102900", "KRW", "KR", "market"),
    "KODEX 단기채권": ("277170", "KRW", "KR", "market"),
    "TIGER 코스피고배당": ("114800", "KRW", "KR", "market"),
    "TIGER 미국나스닥바이오": ("425840", "KRW", "KR", "market"),
    "TIGER 반도체": ("368990", "KRW", "KR", "market"),
    "ACE 차이나AI빅테크TOP2+액티브": ("429400", "KRW", "KR", "market"),
    "TIGER 인도니프티50": ("308620", "KRW", "KR", "market"),
    "PLUS 고배당주채권혼합": ("358910", "KRW", "KR", "market"),
    "SOL 미국배당미국채혼합50": ("431490", "KRW", "KR", "market"),
    "KODEX CD금리액티브(합성)": ("415670", "KRW", "KR", "market"),
    "KODEX 미국30년국채액티브(H)": ("455200", "KRW", "KR", "market"),
}


@dataclass(frozen=True)
class PensionTradeRecordV1:
    """Typed trade record for pension transaction history."""
    trade_date: datetime
    product_name: str
    trade_type: str   # normalized "BUY" or "SELL"
    transaction_type_raw: str  # raw cell value, e.g. "매수", "매도정정1"
    amount: Decimal    # total trade amount in KRW
    balance_quantity: int  # running balance after this trade
    trade_quantity: int
    unit_price: Decimal
    trade_medium: str  # e.g. "미래에셋증권 통합앱"


def _parse_krw_amount(text: str) -> Decimal:
    """Parse '5,570,000원' → Decimal('5570000')."""
    cleaned = text.replace(",", "").replace("원", "").strip()
    return Decimal(cleaned)


def _classify_trade_type(raw: str) -> str:
    """Map Korean trade type to BUY/SELL."""
    if "매수" in raw or "정정" in raw:
        # 매수정정* → BUY (direction of correction follows the name)
        return "BUY" if "매수" in raw else "SELL"
    if "매도" in raw:
        return "SELL"
    raise ValueError(f"unknown trade type: {raw}")


def _parse_holdings_table(lines: list[str]) -> dict[str, int]:
    """Parse '상품별 현재 보유 현황' table → {product_name: quantity}."""
    result: dict[str, int] = {}
    in_holdings = False
    for line in lines:
        stripped = line.strip()
        if "상품별 현재 보유 현황" in stripped:
            in_holdings = True
            continue
        if in_holdings:
            if not stripped.startswith("|"):
                if result:
                    break  # table ended
                continue
            cells = [c.strip() for c in stripped[1:-1].split("|")]
            if len(cells) >= 2 and cells[0] != "상품" and not cells[0].startswith("---"):
                try:
                    qty = int(cells[1].replace(",", ""))
                    result[cells[0]] = qty
                except (ValueError, IndexError):
                    pass
    return result


def _parse_trade_history(lines: list[str]) -> list[PensionTradeRecordV1]:
    """Parse '거래 이력' table → list of typed trade records."""
    trades: list[PensionTradeRecordV1] = []
    in_trades = False
    for line in lines:
        stripped = line.strip()
        if "거래 이력" in stripped:
            in_trades = True
            continue
        if in_trades:
            if not stripped.startswith("|"):
                if trades:
                    break  # table ended
                continue
            cells = [c.strip() for c in stripped[1:-1].split("|")]
            if len(cells) >= 8 and cells[0] != "날짜" and not cells[0].startswith("---"):
                try:
                    trade_date = datetime.strptime(cells[0], "%Y-%m-%d").replace(tzinfo=_KST)
                    product_name = cells[1]
                    trade_type = _classify_trade_type(cells[2])
                    amount = _parse_krw_amount(cells[3])
                    balance = int(cells[4].replace(",", ""))
                    qty = int(cells[5].replace(",", ""))
                    unit_price = _parse_krw_amount(cells[6])
                    medium = cells[7]
                    trades.append(PensionTradeRecordV1(
                        trade_date=trade_date,
                        product_name=product_name,
                        trade_type=trade_type,
                        transaction_type_raw=cells[2],
                        amount=amount,
                        balance_quantity=balance,
                        trade_quantity=qty,
                        unit_price=unit_price,
                        trade_medium=medium,
                    ))
                except (ValueError, IndexError, InvalidOperation):
                    pass
    return trades


def _compute_avg_prices(trades: list[PensionTradeRecordV1]) -> dict[str, Decimal]:
    """Compute weighted average buy price per product from trade history."""
    buy_totals: dict[str, Decimal] = {}
    buy_quantities: dict[str, int] = {}
    for t in trades:
        if t.trade_type == "BUY":
            buy_totals[t.product_name] = buy_totals.get(t.product_name, Decimal("0")) + t.amount
            buy_quantities[t.product_name] = buy_quantities.get(t.product_name, 0) + t.trade_quantity
    result: dict[str, Decimal] = {}
    for name, total in buy_totals.items():
        qty = buy_quantities.get(name, 0)
        if qty > 0:
            result[name] = (total / Decimal(qty)).quantize(Decimal("1"))
        else:
            result[name] = Decimal("0")
    return result


def migrate_legacy_md(
    md_path: Path,
    *,
    validated_at: datetime,
) -> tuple[PensionHoldingsRevisionV1, tuple[PensionTradeRecordV1, ...]]:
    """Read legacy retirement_pension_history.md and produce typed data.

    Returns:
        (holdings_revision, trade_records)

    The holdings revision is derived from the "current holdings" table,
    with average prices computed from the full buy history.
    Trade records capture every transaction for audit/delta verification.
    """
    text = md_path.read_text(encoding="utf-8")
    raw_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    lines = text.splitlines()

    # Parse holdings and trades
    holdings_map = _parse_holdings_table(lines)
    trades = _parse_trade_history(lines)

    if not holdings_map:
        raise ValueError("no current holdings found in markdown")

    if not trades:
        raise ValueError("no trade history found in markdown")

    # Compute average buy prices
    avg_prices = _compute_avg_prices(trades)

    # Build HoldingItemV1 tuple for currently-held symbols
    holding_items: list[HoldingItemV1] = []
    holdings_with_names: list[tuple[HoldingItemV1, str]] = []
    for name, qty in sorted(holdings_map.items(), key=lambda kv: kv[0]):
        if qty <= 0:
            continue
        symbol_info = _PENSION_SYMBOL_MAP.get(name)
        if symbol_info is None:
            raise ValueError(f"unknown product name: {name}")
        symbol, currency, market_raw, market_field = symbol_info
        avg_price = avg_prices.get(name, Decimal("0"))
        item = HoldingItemV1(
            symbol=symbol,
            quantity=str(qty),
            avg_price=str(avg_price),
            currency=currency,
            market_raw=market_raw,
            market_field=market_field,
        )
        holding_items.append(item)
        holdings_with_names.append((item, name))

    holdings_tuple = tuple(holding_items)
    semantic = pension_semantic_digest(holdings_tuple)
    reconciliation = pension_reconciliation_digest(tuple(holdings_with_names))

    # source_as_of = last trade date
    last_trade_date = max(t.trade_date for t in trades)

    revision = PensionHoldingsRevisionV1(
        revision_id=hashlib.sha256(
            f"legacy-migration:{raw_sha[:32]}".encode()
        ).hexdigest()[:32],
        schema_version=PENSION_HOLDINGS_SCHEMA_VERSION,
        source_content_sha256=raw_sha,
        parser_version=PENSION_PARSER_VERSION,
        sheet_schema_version=1,
        holdings=holdings_tuple,
        source_as_of=last_trade_date,
        uploaded_at=validated_at,
        validated_at=validated_at,
        semantic_digest=semantic,
        reconciliation_digest=reconciliation,
        holding_count=len(holdings_tuple),
        liquidation_flag=0,
    )

    return revision, tuple(trades)
