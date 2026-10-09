#!/usr/bin/env python3
"""Internal-first source guard for Tokmo financial research.

The first broad financial external lookup in a session/scope is blocked once to
prompt internal-source reuse.  Repeating that exact external lookup is allowed
without a receipt or second approval.  Direct third-party quote endpoints remain
hard-blocked because they bypass the normal source-selection flow.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

WEB_TOOLS = {
    "web_search",
    "web_extract",
    "mcp__zai_web_search__web_search_prime",
}
BROWSER_TOOLS = {"computer_use"}
FINANCE_RE = re.compile(
    r"(?:금융|재무|주식|증권|종목|시세|현재가|가격|환율|보유|평단|주문|체결|거래|공시|"
    r"시장|지수|KOSPI|KOSDAQ|KRX|NYSE|NASDAQ|OpenDART|XBRL|CFS|OFS|ETF|"
    r"finance|financial|stock|equity|security|ticker|quote|price|exchange.?rate|"
    r"holdings?|average.?cost|orders?|fills?|trades?|market|index|filing|sector|"
    r"[0-9]{6}|[0-9]{4}[A-Z][0-9])",
    re.IGNORECASE,
)
UPPER_FINANCE_TOKEN_RE = re.compile(r"\b(?:KR|US|API|CFS|OFS|XBRL|ETF|FX)\b")
FX_RE = re.compile(
    r"(?:환율|exchange.?rate|USD\s*[/_-]?\s*KRW|KRW\s*[/_-]?\s*USD|\bFX\b)",
    re.IGNORECASE,
)
HOLDINGS_RE = re.compile(
    r"(?:보유수량|보유|수량|평단|평균단가|holdings?|average.?cost)", re.IGNORECASE
)
ORDER_TRADE_RE = re.compile(
    r"(?:주문|체결|orders?|fills?|거래내역|trade.?history)", re.IGNORECASE
)
CALENDAR_RE = re.compile(
    r"(?:시장.*(?:마감|개장|캘린더)|market.*(?:close|open|calendar)|휴장)",
    re.IGNORECASE,
)
INDEX_RE = re.compile(
    r"(?:KOSPI|KOSDAQ|S&P\s*500|NASDAQ|다우|시장지수|주가지수|market.?index)",
    re.IGNORECASE,
)
IDENTITY_RE = re.compile(r"(?:종목명|종목코드|티커|identity|isin)", re.IGNORECASE)
US_RE = re.compile(r"(?:미국|US(?:A)?\b|NYSE|NASDAQ)", re.IGNORECASE)
THIRD_PARTY_FINANCE_RE = re.compile(
    r"(?:finance\.naver\.com|polling\.finance\.naver\.com|query[12]?\.finance\.yahoo\.com|"
    r"alphavantage|finnhub|polygon\.io|stooq|investing\.com)",
    re.IGNORECASE,
)
OFFICIAL_TOSS_RE = re.compile(
    r"(?:openapi\.tossinvest\.com|TossReadsLiveTransport|\.env\.toss)", re.IGNORECASE
)
STATE_DIR = Path.home() / ".hermes" / "state" / "tokmo-source-internal-first"


def _text(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value)


def _internal_first_already_prompted(
    text: str, *, tool_name: str, session_id: str
) -> bool:
    """Return True after the first prompt for the exact session/tool/input scope."""
    if not session_id:
        return False
    key = hashlib.sha256(
        (session_id + "\0" + tool_name + "\0" + text).encode()
    ).hexdigest()
    path = STATE_DIR / f"{key}.seen"
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists():
            return True
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        return False
    except FileExistsError:
        return True
    except OSError:
        # Fail closed for the first pass if the advisory state cannot be stored.
        return False


def _guidance(text: str) -> tuple[str, str]:
    """Return the detected broker fact class and its lowest-cost Toss-first route."""
    if FX_RE.search(text):
        return (
            "EXCHANGE_RATE",
            "Toss EXCHANGE_RATE GET /api/v1/exchange-rate "
            "(baseCurrency=USD, quoteCurrency=KRW); then use official web sources only for residual macro research",
        )
    if HOLDINGS_RE.search(text):
        return (
            "HOLDINGS_AVERAGE_COST",
            "fresh Toss-origin account canonical store first; otherwise use the official Toss account read path",
        )
    if ORDER_TRADE_RE.search(text):
        return (
            "ORDERS_TRADES",
            "Toss-origin ledger first; otherwise use the official Toss orders/trades read path",
        )
    if CALENDAR_RE.search(text):
        operation = "US_MARKET_CALENDAR" if US_RE.search(text) else "KR_MARKET_CALENDAR"
        country = "US" if operation.startswith("US_") else "KR"
        return (
            "MARKET_CALENDAR",
            f"Toss {operation} GET /api/v1/market-calendar/{country}",
        )
    if INDEX_RE.search(text):
        return (
            "MARKET_INDEX",
            "Toss INDICATOR_PRICES GET /api/v1/market-indicators/prices with the canonical indicator symbol",
        )
    if IDENTITY_RE.search(text):
        return (
            "INSTRUMENT_IDENTITY",
            "Toss STOCKS GET /api/v1/stocks with the exact symbol",
        )
    return (
        "CURRENT_QUOTE",
        "fresh Toss-origin quote store first; otherwise Toss TRADES(count=1) for current traded price",
    )


def decide(payload: dict) -> dict | None:
    if payload.get("hook_event_name") != "pre_tool_call":
        return None
    tool = payload.get("tool_name") or ""
    args = payload.get("tool_input")
    if not isinstance(args, dict):
        return None
    text = _text(args)

    candidate = False
    hard_block = False
    if tool in WEB_TOOLS or tool in BROWSER_TOOLS:
        candidate = bool(FINANCE_RE.search(text) or UPPER_FINANCE_TOKEN_RE.search(text))
    elif tool == "terminal":
        candidate = bool(THIRD_PARTY_FINANCE_RE.search(text))
        hard_block = candidate
        if OFFICIAL_TOSS_RE.search(text):
            candidate = False
    elif tool == "execute_code":
        candidate = bool(THIRD_PARTY_FINANCE_RE.search(text))
        hard_block = candidate

    if not candidate:
        return None
    if not hard_block and _internal_first_already_prompted(
        text, tool_name=tool, session_id=str(payload.get("session_id") or "")
    ):
        return None
    fact_class, next_action = _guidance(text)
    return {
        "action": "block",
        "message": (
            "TOKMO_INTERNAL_FIRST: "
            f"detected_fact={fact_class}; "
            "reason=use available internal sources and canonical stores before external financial research; "
            f"next_action={next_action}; "
            + (
                "fallback=direct third-party quote endpoint bypass remains blocked."
                if hard_block
                else "fallback=if internal sources are absent or limited, first report "
                "local_checked=<stores/files/queries and freshness>; "
                "unresolved_gap=<specific missing or conflicting fact>; "
                "why_external_can_change_answer=<material reason>. Then retry the same external request; "
                "it will be allowed without another block, extra proof, or per-query approval."
            )
        ),
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    result = decide(payload)
    if result is not None:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
