"""Quant Telegram decision-card renderer and one-attempt canary delivery.

Product contract 2026-09-03 (decision-card output contract v0.1, D1-D3)
plus the cycle-1 user feedback (provider ids 81/82) repaired for cycle 2:
one message per market, user-approved mobile line breaks, evidence-only
content. Every section is either grounded in an exact existing record
(report card factors, report entry zone, latest-quote store, current
holdings store, Git-tracked watchlist, playbook document STRUCTURE, the
one-page OPEN conditional-order read) or rendered as an explicit
unavailable label with its reason.

Cycle-2 rules plus the 2026-09-03 header/table amendment: the header is
the compact three lines "📊 퀀트 <market> / <completed session date> /
카드 N종목" — the report identity carries only a session DATE, so no
time-of-day is ever fabricated; the current-price section carries the
price only (its observation time/source line is not rendered); the
5/20/120 returns are a two-row "label / label / label" over
"value / value / value" table meant for Telegram monospace: the rows are
wrapped in ``` fences that the senders convert into a Telegram ``pre``
entity (``telegram_pre_message``), each label centred over its value by
CJK display width (at most 0.5 cell off); the OPEN conditional-order
section follows the holdings section directly; raw playbook text and file
paths never reach Telegram — only a rule-based synthesis (absence per
layer, stance classification, agreement with the Quant evidence, decision
impact) labelled 헤르 해석(1층); holdings weights are quantity × valid
latest quote over the exact denominator "stock holdings of the same
account and market" (cash, bonds, funds excluded) and a cross-market
STOCK total only with a valid FX envelope; conditional orders are shown
from the injected read only, never inferred.

Mobile revision 2026-09-04 (user-visible contract): the holdings heading
is ``보유 현황 - 개인계좌`` and carries personal-account positions only
(the retirement/pension book is outside Quant holdings and is never mixed
in — the controller filters it before building ``CardContextV1``); the
held line is ``보유 : 4주 / 평단 : 1,250,000원`` (USD for US); the weight
line is ``비중 : 국내 60.9%`` / ``비중 : 미국 …%`` with no denominator prose;
no account-total (or stock-total) weight line exists at all — a true
account-total percentage needs the complete same-instant personal-account
assets (stocks, cash, bonds, funds, …) valued in one currency with valid
USD/KRW FX and explicit denominator semantics, none of which the card has,
so the KR+US stock ratio ``compute_weights`` still derives is never
rendered and its unavailability reason survives only in
``unavailable_sections`` (see the SDS); the conditional heading is
``조건주문 현황`` with the compact per-order block ``[n]`` / ``발동가`` +
``주문가`` (combined as ``발동/주문가`` when equal) / ``거래구분`` from a
provider-side field only, else ``확인 불가 · N주`` / ``만료일`` — no scope
label, leg type/status, price gap or zone relation — grouped BUY ≤2 /
SELL ≤2 / unknown ≤2 in input order with the hidden remainder summarised
once as ``외 N건..``; the playbook heading is plain ``플레이북`` and never
prints ``있음`` (absence per layer only); the null entry-zone wording
distinguishes "connected, setup conditions not met", "connected,
ATR14/support series not reproducible from the report" and "indicator
missing" — never a blanket 자료 미연결; every revised user-visible line is
kept within ``MOBILE_LINE_CELLS`` (44 display cells) by splitting at its
own `` · `` / `` — `` seams or spaces (``wrap_mobile``), never by
rewording evidence strings.

Nothing here touches a provider, an order, an account write, a clock, or
the process environment; transports are injected as plain callables.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from tokmo.programs.execution_dialogue import _kst
from tokmo.programs.portfolio_calculations import (
    FX_MAX_AGE_SECONDS_V1,
    fx_content_hash_v1,
)
from tokmo.values.instrument_display import display_or_symbol
from tokmo.values.news_payload import contains_private_markers
from tokmo.values.portfolio_calculations import FxEvidenceEnvelopeV1
from tokmo.values.quant_discovery import (
    DecisionCardV1,
    FactorOrientation,
    FactorStatus,
    QuantRunReportV1,
    QuantUnavailableCode,
)

MARKETS = ("KR", "US")
MESSAGE_CHAR_CAP = 4096
RETURN_SEPARATOR = " / "
PRE_FENCE = "```"
PROVISIONAL_LABEL = "잠정 · 규칙 기반 · AI 분석 미연결"
PROVISIONAL_LABEL_ANALYSED = "잠정 · 규칙 기반 · AI 맥락 분석 아래"
ANALYSIS_LABEL = "맥락 분석 (AI · 근거 연결 검증됨)"
HERMES_SYNTHESIS_LABEL = "헤르 해석(1층)"
HOLDINGS_HEADING = "보유 현황 - 개인계좌"
CONDITIONAL_HEADING = "조건주문 현황"
MARKET_WEIGHT_LABELS = {"KR": "국내", "US": "미국"}
ORDER_SIDES = ("BUY", "SELL")
CONDITIONAL_SIDE_CAP = 2
MOBILE_LINE_CELLS = 44  # iPhone 15 Pro Telegram line, CJK = 2 cells
WEIGHT_QUOTE_MAX_AGE = timedelta(days=7)
FX_SOURCE = "toss-exchange-rate-v1"
LOW_SAMPLE_THRESHOLD = 10
STANDING_FACTORS = (
    ("ret_120", "120일 모멘텀"),
    ("risk_adjusted_momentum", "위험조정 모멘텀"),
    ("realized_vol_63", "변동성 안정성"),
    ("max_drawdown", "최대낙폭 방어력"),
)
HERMES_START, HERMES_END = (
    "<!-- HERMES_CURRENT_START -->",
    "<!-- HERMES_CURRENT_END -->",
)
THESIS_START, THESIS_END = "<!-- USER_THESIS_START -->", "<!-- USER_THESIS_END -->"
STANCE_LABELS = {"ACCUMULATE": "매수·확대", "HOLD": "보유 유지", "REDUCE": "축소·정리"}
_STANCE_WORDS = {
    "ACCUMULATE": (
        "추가 매수",
        "추가매수",
        "비중 확대",
        "분할 매수",
        "분할매수",
        "저가 매수",
        "매수 관점",
        "확대",
    ),
    "HOLD": (
        "보유 유지",
        "장기 보유",
        "장기보유",
        "홀딩",
        "안 팔",
        "안팔",
        "계속 보유",
        "유지",
    ),
    "REDUCE": (
        "전량 매도",
        "비중 축소",
        "부분매도",
        "부분 매도",
        "익절",
        "손절",
        "정리",
        "축소",
    ),
}
_ANCHOR_RE = re.compile(r'<a id="entry-([^"]+)"></a>')
_SUMMARY_RE = re.compile(r"- `trade_summary`: `(BUY|SELL) ")
_EXTRA_PRIVATE = ("/users/", "account", "credential", "private_holding")
_SECTION_QUOTE = "현재가"
_SECTION_HOLDINGS = "보유 현황"
_SECTION_PLAYBOOK = "플레이북"
_SECTION_ZONE = "진입구간"
_SECTION_STANDINGS = "상대순위"
_SECTION_JUDGMENT = "투자판단"
_SECTION_CONDITIONAL = "조건주문"
_SECTION_ANALYSIS = "맥락 분석"
_SIDE_KR = {"BUY": "매수", "SELL": "매도"}


# --- context values supplied by the controller ------------------------------


@dataclass(frozen=True)
class QuoteContextV1:
    price: Decimal
    currency: str
    observed_at: datetime | None
    source_label: str


@dataclass(frozen=True)
class WeightContextV1:
    """Weights over exact, labelled denominators; None + reason when not computable."""

    market_weight: Decimal | None
    market_label: str | None
    market_reason: str | None
    total_weight: Decimal | None
    total_reason: str | None


@dataclass(frozen=True)
class HoldingContextV1:
    scope_label: str
    quantity: str
    avg_price: str
    weight: WeightContextV1 | None = None


@dataclass(frozen=True)
class PlaybookSynthesisV1:
    """Structure-only view of an instrument playbook; never carries text or paths."""

    scope_label: str
    has_user_thesis: bool
    has_hermes_current: bool
    layer3_entries: int
    latest_trade_side: str | None
    stance: str | None


@dataclass(frozen=True)
class ConditionalOrderContextV1:
    """One conditional-order leg. Side provenance is explicit leg orderSide,
    compatible legacy SINGLE top-level side, or documented OCO/OTO semantics;
    it is never derived from holdings, prices, or the trigger/leg type."""

    scope_label: str
    order_kind: str
    order_type: str
    quantity: str
    leg_type: str
    leg_status: str
    trigger_price: Decimal | None
    order_price: Decimal | None
    expire_date: str | None
    side: str | None = None

    def __post_init__(self):
        if self.side is not None and self.side not in ORDER_SIDES:
            raise ValueError("conditional order side must be BUY, SELL or None")


@dataclass(frozen=True)
class ConditionalContextV1:
    """ACTIVE (orders found), NONE (every mapped account read fully, none for this
    symbol), or UNAVAILABLE (any account unread: never inferred as absent)."""

    status: str
    reason: str | None = None
    orders: tuple = ()


@dataclass(frozen=True)
class CardContextV1:
    symbol: str
    quote: QuoteContextV1 | None = None
    quote_unavailable_reason: str | None = None
    holdings: tuple = ()
    holdings_available: bool = False
    watchlisted: bool | None = None
    playbook: PlaybookSynthesisV1 | None = None
    playbook_unavailable_reason: str | None = None
    conditional: ConditionalContextV1 | None = None
    report_verified: bool = False
    quote_age_valid: bool = False
    quote_validity_reason: str | None = None


@dataclass(frozen=True)
class MarketMessageV1:
    market: str
    completed_session_date: str
    identity_sha256: str
    report_sha256: str
    parts: tuple
    grounded_sections: tuple
    unavailable_sections: tuple


@dataclass(frozen=True)
class PositionV1:
    """One stock holding line with its (possibly unavailable) valid quote.
    ``scope_key`` is the exact account scope (one denominator per account)."""

    scope_key: str
    scope_label: str
    market: str
    symbol: str
    quantity: Decimal
    price: Decimal | None
    currency: str
    price_reason: str | None = None


# --- formatting helpers -----------------------------------------------------


def _pct(value: Decimal | None, *, signed: bool = True) -> str:
    if value is None:
        return "산출 불가"
    scaled = (value * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    text = f"{scaled:,.1f}%"
    return f"+{text}" if signed and scaled > 0 else text


def _chars(text: str) -> int:
    """Telegram length unit: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def _display_width(text: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text
    )


def _center_display(text: str, width: int) -> str:
    padding = max(0, width - _display_width(text))
    left = padding // 2
    return " " * left + text + " " * (padding - left)


def _wrap_words(text: str, width: int) -> list:
    lines: list = []
    current = ""
    for word in text.split(" "):
        candidate = word if not current else f"{current} {word}"
        if current and _display_width(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    return lines + [current] if current else lines


def wrap_mobile(text: str, width: int = MOBILE_LINE_CELLS) -> list:
    """Deterministic phone-width split of one rendered line: unchanged when it
    fits; otherwise packed at its own `` · `` seams, and a seamless overlong
    chunk is broken at spaces. Bytes are only moved across line breaks —
    no word is ever reworded or dropped."""
    if _display_width(text) <= width:
        return [text]
    chunks: list = []
    current = None
    for part in text.split(" · "):
        candidate = part if current is None else f"{current} · {part}"
        if current is not None and _display_width(candidate) > width:
            chunks.append(current)
            current = part
        else:
            current = candidate
    chunks.append(current)
    return [line for chunk in chunks for line in _wrap_words(chunk, width)]


def _returns_table(card: DecisionCardV1) -> tuple[str, str]:
    """Two rows for a monospace block: ``5일 / 20일 / 120일`` centred over
    ``-4.9% / -0.7% / +70.1%`` by CJK display width."""
    labels = ("5일", "20일", "120일")
    values = tuple(_pct(_factor(card, name)) for name in ("ret_5", "ret_20", "ret_120"))
    widths = tuple(
        max(_display_width(label), _display_width(value))
        for label, value in zip(labels, values)
    )
    label_row = RETURN_SEPARATOR.join(
        _center_display(label, width) for label, width in zip(labels, widths)
    ).rstrip()
    value_row = RETURN_SEPARATOR.join(
        _center_display(value, width) for value, width in zip(values, widths)
    ).rstrip()
    return label_row, value_row


def telegram_pre_message(text: str) -> tuple:
    """(plain text without ``` fence lines, Telegram ``pre`` entities).

    The renderer marks monospace blocks with ``` fence lines; the live
    sender posts plain text (no parse mode), so the fences are removed here
    and each block becomes one ``pre`` entity with UTF-16 offsets. An
    unbalanced fence is a renderer defect and fails closed."""
    lines = text.split("\n")
    kept: list = []
    entities: list = []
    inside = False
    start = None
    for line in lines:
        if line == PRE_FENCE:
            if not inside:
                inside = True
                start = _chars("\n".join(kept)) + (1 if kept else 0)
                continue
            body = "\n".join(kept)
            length = _chars(body) - start
            if length <= 0:
                raise ValueError("empty_pre_block")
            entities.append({"type": "pre", "offset": start, "length": length})
            inside = False
            continue
        kept.append(line)
    if inside:
        raise ValueError("unbalanced_pre_fence")
    return "\n".join(kept), tuple(entities)


def format_price(value: Decimal, currency: str) -> str:
    if currency == "KRW":
        return f"{int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP)):,}원"
    if currency == "USD":
        return f"${value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"
    return f"{value.normalize():f} {currency}"


_price = format_price


def _currency(market: str) -> str:
    return "KRW" if market == "KR" else "USD"


def _factor(card: DecisionCardV1, name: str) -> Decimal | None:
    for factor in card.factors:
        if factor.name == name:
            return factor.raw_value if factor.status is FactorStatus.AVAILABLE else None
    return None


ZONE_RSI_LOWER, ZONE_RSI_UPPER = (
    Decimal(45),
    Decimal(65),
)  # quant_discovery_entry TREND_PULLBACK contract


def entry_zone_condition_checks(card: DecisionCardV1) -> tuple:
    """Deterministic re-check of the TREND_PULLBACK factor conditions from the
    report card alone: ``(label, passed, detail)`` per condition, ``passed``
    None when the factor is unavailable. Mirrors ``build_reference_entry_zone``
    (close > SMA20 > SMA60 and 45 ≤ RSI14 ≤ 65) using the card's ratio
    factors ``close/SMA − 1``: SMA20 > SMA60 ⇔ close_to_sma_60 > close_to_sma_20
    (both ratios share the same close). The ATR/support conditions need the
    price series and are NOT re-derived here."""
    sma_20, sma_60, rsi = (
        _factor(card, "close_to_sma_20"),
        _factor(card, "close_to_sma_60"),
        _factor(card, "rsi_14"),
    )
    checks = []
    if sma_20 is None:
        checks.append(("종가>SMA20", None, "20일선 대비 지표 없음"))
    else:
        checks.append(("종가>SMA20", sma_20 > 0, f"20일선 대비 {_pct(sma_20)}"))
    if sma_20 is None or sma_60 is None:
        checks.append(("SMA20>SMA60", None, "20일선/60일선 대비 지표 없음"))
    else:
        checks.append(
            (
                "SMA20>SMA60",
                sma_60 > sma_20,
                f"60일선 대비 {_pct(sma_60)} vs 20일선 대비 {_pct(sma_20)}",
            )
        )
    if rsi is None:
        checks.append(
            (f"RSI14 {ZONE_RSI_LOWER}~{ZONE_RSI_UPPER}", None, "RSI14 지표 없음")
        )
    else:
        checks.append(
            (
                f"RSI14 {ZONE_RSI_LOWER}~{ZONE_RSI_UPPER}",
                ZONE_RSI_LOWER <= rsi <= ZONE_RSI_UPPER,
                f"RSI14 {rsi.quantize(Decimal('0.1'))}",
            )
        )
    return tuple(checks)


def _zone_unavailable_lines(card: DecisionCardV1) -> tuple:
    """(lines, verdict) for a null report entry zone. The head lines name
    which of three distinct situations holds — an indicator itself is
    missing (판정 불가; INSUFFICIENT_PRICE_HISTORY suffix when the report
    says so) which takes precedence over everything else; the indicator
    data is connected but a deterministic TREND_PULLBACK factor condition
    failed; or the factor conditions hold but the ATR14/support step needs
    the price series the report does not carry — followed by every failed,
    missing and met condition with its actual comparison values. Never a
    manufactured zone, a softened rule or a blanket "자료 미연결".
    ``verdict`` is "미성립" or "판정 불가"."""
    checks = entry_zone_condition_checks(card)
    failed = [
        f"{label} 불충족 · {detail}"
        for label, passed, detail in checks
        if passed is False
    ]
    unknown = [
        f"{label} 판정 불가 · {detail}"
        for label, passed, detail in checks
        if passed is None
    ]
    met = [
        f"{label} 충족 · {detail}" for label, passed, detail in checks if passed is True
    ]
    # a missing required factor wins the verdict: "지표 연결됨" may only be claimed
    # when every required indicator is present (review M1, 2026-09-04)
    if unknown:
        verdict, situation = "판정 불가", "지표 없음"
        if QuantUnavailableCode.INSUFFICIENT_PRICE_HISTORY in card.unavailable_codes:
            situation += " · 가격 이력 부족"
    elif failed:
        verdict, situation = "미성립", "지표 연결됨 · 추세눌림 조건 불충족"
    else:
        verdict, situation = "미성립", "지표 조건 충족"
    lines = [f"관찰구간 : {verdict}"]
    lines += [
        line
        for item in (situation, *failed, *unknown, *met)
        for line in wrap_mobile(item)
    ]
    if not failed and not unknown:
        lines.append("ATR14/지지선 : 시계열 미확보 · 리포트 밖")
    return lines, verdict


def _rsi_label(value: Decimal) -> str:
    if value >= 70:
        return "단기 과열권"
    if value <= 30:
        return "과매도권"
    return "중립권"


def _vol_label(value: Decimal) -> str:
    if value >= Decimal("0.8"):
        return "매우 높음"
    if value >= Decimal("0.5"):
        return "높음"
    if value >= Decimal("0.3"):
        return "보통"
    return "낮음"


def _private(text: str) -> bool:
    lowered = text.lower()
    return contains_private_markers(text) or any(
        marker in lowered for marker in _EXTRA_PRIVATE
    )


def extract_playbook_layers(text: str) -> tuple:
    """(2층 사용자의 원문, 1층 헤르의 생각) raw blocks; None when a block is absent."""

    def between(start: str, end: str):
        if text.count(start) != 1 or text.count(end) != 1:
            return None
        return text.split(start, 1)[1].split(end, 1)[0]

    return between(THESIS_START, THESIS_END), between(HERMES_START, HERMES_END)


def _layer_present(block) -> bool:
    if block is None:
        return False
    lines = [line.strip() for line in block.replace("\r\n", "\n").split("\n")]
    return any(
        line and not line.startswith(("## ", "> ", "- `", "<!--")) for line in lines
    )


def classify_stance(text: str) -> str | None:
    """Deterministic keyword classification; None when no clear majority."""
    scores = {
        stance: sum(text.count(word) for word in words)
        for stance, words in _STANCE_WORDS.items()
    }
    best = max(scores.values())
    if best == 0:
        return None
    winners = [stance for stance, score in scores.items() if score == best]
    return winners[0] if len(winners) == 1 else None


def synthesize_playbook(text: str, *, scope_label: str) -> PlaybookSynthesisV1:
    """Structure-only synthesis of one instrument document (no text leaves here)."""
    thesis, hermes = extract_playbook_layers(text)
    entries = len(_ANCHOR_RE.findall(text))
    sides = _SUMMARY_RE.findall(text)
    stance = classify_stance("\n".join(block for block in (hermes, thesis) if block))
    return PlaybookSynthesisV1(
        scope_label,
        _layer_present(thesis),
        _layer_present(hermes),
        entries,
        sides[0] if sides else None,
        stance,
    )


# --- weights -------------------------------------------------------------------


def fx_usd_krw_from_evidence(envelope, *, now: datetime) -> tuple:
    """(rate, None) for a valid canonical Toss USD/KRW envelope, else (None, reason)."""
    if envelope is None:
        return None, "USD/KRW 환율 근거 없음"
    if type(envelope) is not FxEvidenceEnvelopeV1:
        return None, "환율 근거 형식 불일치"
    try:
        rate = Decimal(envelope.rate)
    except (InvalidOperation, TypeError, ValueError):
        return None, "환율 값 불량"
    if (
        envelope.schema_version != 1
        or envelope.source != FX_SOURCE
        or envelope.base_currency != "USD"
        or envelope.quote_currency != "KRW"
        or not rate.is_finite()
        or rate <= 0
        or envelope.max_age_seconds != FX_MAX_AGE_SECONDS_V1
    ):
        return None, "환율 근거 계약 불일치"
    try:
        if fx_content_hash_v1(envelope) != envelope.content_hash:
            return None, "환율 근거 해시 불일치"
    except Exception:
        return None, "환율 근거 해시 불일치"
    now_utc = now.astimezone(envelope.observed_at.tzinfo)
    if not (
        envelope.valid_from <= now_utc <= envelope.valid_until
    ) or now_utc - envelope.observed_at > timedelta(seconds=FX_MAX_AGE_SECONDS_V1):
        return None, "환율 근거 유효기간 경과"
    return rate, None


def compute_weights(
    positions: tuple, *, fx_usd_krw: Decimal | None, fx_reason: str | None
) -> dict:
    """{(scope_key, market, symbol): WeightContextV1} over exact labelled denominators.

    Market weight: this position's valuation over the sum of ALL stock
    positions of the same account scope and market; unavailable when any of
    them lacks a valid quote. Total weight: over the account's KR+US stock
    valuation in KRW, only with a valid FX rate; a KRW-only account needs no FX."""
    by_market: dict = {}
    for position in positions:
        by_market.setdefault((position.scope_key, position.market), []).append(position)
    result = {}
    for position in positions:
        group = by_market[(position.scope_key, position.market)]
        valuation = (
            None if position.price is None else position.quantity * position.price
        )
        market_label = f"{position.scope_label} {position.market} 주식"
        if valuation is None:
            market = (None, market_label, position.price_reason or "현재가 미확인")
        elif any(other.price is None for other in group):
            missing = sum(1 for other in group if other.price is None)
            market = (None, market_label, f"같은 시장 보유 {missing}종목 시세 미확인")
        else:
            denominator = sum(other.quantity * other.price for other in group)
            market = (
                valuation / denominator if denominator > 0 else None,
                market_label,
                None if denominator > 0 else "평가액 0",
            )
        scope_positions = [
            other for other in positions if other.scope_key == position.scope_key
        ]
        if valuation is None:
            total = (None, position.price_reason or "현재가 미확인")
        elif any(other.price is None for other in scope_positions):
            total = (None, "계좌 보유 종목 일부 시세 미확인")
        else:
            needs_fx = any(other.currency == "USD" for other in scope_positions)
            if needs_fx and fx_usd_krw is None:
                total = (None, fx_reason or "USD/KRW 환율 근거 없음")
            else:

                def krw(other):
                    value = other.quantity * other.price
                    return value if other.currency == "KRW" else value * fx_usd_krw

                denominator = sum(krw(other) for other in scope_positions)
                total = (
                    krw(position) / denominator if denominator > 0 else None,
                    None if denominator > 0 else "평가액 0",
                )
        result[(position.scope_key, position.market, position.symbol)] = (
            WeightContextV1(market[0], market[1], market[2], total[0], total[1])
        )
    return result


# --- standings --------------------------------------------------------------


def compute_standings(cards: tuple) -> dict:
    """Competition ordinals per factor over the supplied cards only."""
    per_factor: dict = {}
    for name, _label in STANDING_FACTORS:
        orientation = None
        values = {}
        for card in cards:
            for factor in card.factors:
                if factor.name == name:
                    orientation = factor.orientation
                    if factor.status is FactorStatus.AVAILABLE:
                        values[card.symbol] = factor.raw_value
        sample = len(values)
        standings = {}
        for symbol, value in values.items():
            if orientation is FactorOrientation.LOWER_IS_BETTER:
                better = sum(1 for other in values.values() if other < value)
            else:
                better = sum(1 for other in values.values() if other > value)
            standings[symbol] = (better + 1, sample)
        per_factor[name] = standings
    headline = {}
    means = {}
    for card in cards:
        ordinals = [
            per_factor[name][card.symbol][0]
            for name, _ in STANDING_FACTORS
            if card.symbol in per_factor[name]
        ]
        if ordinals:
            means[card.symbol] = Decimal(sum(ordinals)) / Decimal(len(ordinals))
    for symbol, mean in means.items():
        headline[symbol] = (
            1 + sum(1 for other in means.values() if other < mean),
            len(means),
        )
    return {"factors": per_factor, "headline": headline, "sample_size": len(cards)}


# --- judgment (deterministic, provisional) ----------------------------------


def _trend(card: DecisionCardV1) -> str:
    ret_120, sma_120 = _factor(card, "ret_120"), _factor(card, "close_to_sma_120")
    if ret_120 is None:
        return "unknown"
    if ret_120 > 0 and (sma_120 is None or sma_120 > 0):
        return "up"
    if ret_120 < 0:
        return "down"
    return "mixed"


def _judgment(
    card: DecisionCardV1,
    context: CardContextV1,
    standings: dict,
    *,
    analysed: bool = False,
) -> dict:
    ret_20, ret_120 = _factor(card, "ret_20"), _factor(card, "ret_120")
    sma_20, sma_60, sma_120 = (
        _factor(card, "close_to_sma_20"),
        _factor(card, "close_to_sma_60"),
        _factor(card, "close_to_sma_120"),
    )
    rsi, vol, drawdown = (
        _factor(card, "rsi_14"),
        _factor(card, "realized_vol_63"),
        _factor(card, "max_drawdown"),
    )
    grounds, counter = [], []
    if ret_120 is not None:
        (grounds if ret_120 > 0 else counter).append(f"120일 수익률 {_pct(ret_120)}")
    if sma_120 is not None:
        (grounds if sma_120 > 0 else counter).append(f"120일선 대비 {_pct(sma_120)}")
    if sma_60 is not None:
        (grounds if sma_60 > 0 else counter).append(f"60일선 대비 {_pct(sma_60)}")
    if ret_20 is not None:
        (grounds if ret_20 > 0 else counter).append(f"20일 수익률 {_pct(ret_20)}")
    if rsi is not None:
        label = _rsi_label(rsi)
        (counter if label == "단기 과열권" else grounds).append(
            f"RSI14 {rsi.quantize(Decimal('0.1'))} ({label})"
        )
    if vol is not None:
        label = _vol_label(vol)
        (counter if label in ("높음", "매우 높음") else grounds).append(
            f"63일 변동성 {_pct(vol, signed=False)} ({label})"
        )
    if drawdown is not None:
        (counter if drawdown <= Decimal("-0.3") else grounds).append(
            f"최대낙폭 {_pct(drawdown)}"
        )

    trend_kind = _trend(card)
    short_down = (ret_20 is not None and ret_20 < 0) or (
        sma_20 is not None and sma_20 < 0
    )
    overheated = rsi is not None and rsi >= 70
    heavy_vol = vol is not None and vol >= Decimal("0.5")
    deep_drawdown = drawdown is not None and drawdown <= Decimal("-0.3")

    if trend_kind == "up" and short_down:
        trend = "중장기 상승 추세는 유지되지만 단기 조정 국면입니다."
    elif trend_kind == "up":
        trend = "중장기 상승 추세가 확인됩니다."
    elif trend_kind == "down":
        trend = "중장기 하락 추세로 신규 진입 근거가 약합니다."
    elif trend_kind == "unknown":
        trend = "추세 판단에 필요한 120일 수익률이 없습니다."
    else:
        trend = "추세 방향이 혼재되어 방향성 근거가 약합니다."
    burdens = [
        name
        for flag, name in (
            (overheated, "과열"),
            (heavy_vol, "변동성"),
            (deep_drawdown, "낙폭"),
        )
        if flag
    ]
    burden = (
        ("·".join(burdens) + " 부담이 큽니다.")
        if burdens
        else "가격 부담 지표는 뚜렷하지 않습니다."
    )

    zone = card.entry_zone
    if zone is None:
        stance = "관찰구간이 산출되지 않아 현재 가격에서의 신규 진입 손익비를 평가할 수 없습니다."
    elif context.quote is not None and context.quote.price > zone.upper:
        stance = "현재가가 관찰구간 위에 있어 신규 추격매수의 손익비는 불리해 보입니다."
    elif context.quote is not None and context.quote.price < zone.invalidation:
        stance = "현재가가 무효화 기준 아래에 있어 이번 관찰구간은 유효하지 않습니다."
    else:
        stance = "완료 세션 기준 관찰구간이 성립해 확인 신호를 기다리는 구간입니다."

    held = bool(context.holdings)
    if zone is None:
        new_entry = "대기 (관찰구간 산출 불가)"
    elif context.quote is None:
        new_entry = "대기 (현재가 미확인)"
    elif context.quote.price > zone.upper:
        new_entry = "대기 (관찰구간 상단 위)"
    elif context.quote.price < zone.invalidation:
        new_entry = "진입구간 없음 (무효화 기준 하회)"
    else:
        new_entry = "관찰구간 내 · 확인 신호 대기"
    existing = (
        "플레이북의 익절·비중상한 점검"
        if held and context.playbook is not None
        else "보유 리스크 점검 (플레이북 없음)"
        if held
        else "해당 없음 (미보유)"
    )
    sample = standings["headline"].get(card.symbol, (None, 0))[1]
    ai_note = "AI 맥락 분석 아래" if analysed else "AI 분석 미연결"
    return {
        "summary": (trend, burden, stance),
        "grounds": tuple(grounds),
        "counter": tuple(counter),
        "confidence": f"낮음 — 비교군 {sample}종목 · 규칙 기반 · {ai_note}",
        "change": "관찰구간 성립/무효화 확정, 비교군 확장"
        + ("" if analysed else ", AI 분석 연결")
        + " 시 재판단",
        "actions": (
            ("신규 매수", new_entry),
            ("기존 보유", existing),
            (
                "재검토",
                "다음 완료 세션 리포트"
                + (" · 플레이북 조건 재확인" if context.playbook else ""),
            ),
        ),
    }


# --- playbook synthesis rendering ------------------------------------------------

# Fixed rule sentences, each phrase ≤ MOBILE_LINE_CELLS once split at " — ";
# "중장기"/"플레이북은" are implied by the trend summary and the section heading.
_AGREEMENT = {
    ("ACCUMULATE", "up"): ("일치", "Quant 상승 근거가 매수·확대 기조를 지지"),
    ("HOLD", "up"): ("일치", "Quant 상승 근거가 보유 유지 기조를 지지"),
    ("REDUCE", "down"): ("일치", "Quant 하락 근거가 축소·정리 기조를 지지"),
    ("ACCUMULATE", "down"): ("상충", "매수·확대 기조이나 Quant는 하락 근거"),
    ("HOLD", "down"): ("주의", "보유 유지 기조이나 Quant는 하락 근거 — 재점검 필요"),
    ("REDUCE", "up"): ("상충", "축소·정리 기조이나 Quant는 상승 근거"),
}
_IMPACT = {
    "일치": "현재 판단을 지지 — 플레이북 조건(익절·비중상한) 재확인 후 유지",
    "상충": "플레이북과 Quant 근거가 갈림 — 플레이북 재검토 또는 해석 정정 필요",
    "주의": "보유 유지 근거 약화 — 무효화 조건과 플레이북 손절 규칙 재확인",
}


def playbook_agreement(synthesis: PlaybookSynthesisV1 | None, trend_kind: str) -> tuple:
    """(verdict, reason, impact) — deterministic, from stance × Quant trend only."""
    if synthesis is None:
        return ("해당 없음", "플레이북 없음", "플레이북 없이 Quant 근거만으로 판단")
    if synthesis.stance is None:
        return (
            "대조 불가",
            "기조 미분류 · 매수/보유/축소 키워드 불명확",
            "영향 판단 보류 — 플레이북 기조 명시 필요",
        )
    if trend_kind in ("mixed", "unknown"):
        return (
            "대조 보류",
            "Quant 추세 근거 혼재/부재",
            "영향 판단 보류 — 다음 완료 세션 리포트 대기",
        )
    verdict, reason = _AGREEMENT[(synthesis.stance, trend_kind)]
    return verdict, reason, _IMPACT[verdict]


def _playbook_lines(context: CardContextV1, trend_kind: str) -> list:
    """Absence is printed per layer (``없음``); presence never is. The stance
    classified from the document structure is shown under 헤르 해석(1층)."""
    synthesis = context.playbook
    if synthesis is None:
        reason = context.playbook_unavailable_reason or "플레이북 문서 없음"
        return [
            _SECTION_PLAYBOOK,
            "없음" if reason == "플레이북 문서 없음" else f"확인 불가 · {reason}",
        ]
    stance = STANCE_LABELS.get(synthesis.stance)
    lines = [
        _SECTION_PLAYBOOK
    ]  # plain heading: the account is already named by the holdings heading
    if synthesis.has_hermes_current:
        lines.append(
            f"{HERMES_SYNTHESIS_LABEL}: "
            + (f"{stance} 기조" if stance else "기조 미분류")
        )
    else:
        lines.append(f"{HERMES_SYNTHESIS_LABEL}: 없음")
        if stance:
            lines.append(f"기조(2층 기준) : {stance}")
    if not synthesis.has_user_thesis:
        lines.append("사용자 원문(2층): 없음")
    if synthesis.layer3_entries:
        latest = _SIDE_KR.get(synthesis.latest_trade_side)
        lines.append(
            f"매매사유 : {synthesis.layer3_entries}건"
            + (f" · 최근 {latest}" if latest else "")
        )
    else:
        lines.append("매매사유 : 없음")
    verdict, reason, impact = playbook_agreement(synthesis, trend_kind)
    # the fixed agreement/impact sentences break at their dash into phone-width lines
    tail = [
        f"Quant 대조 : {verdict}",
        *reason.split(" — "),
        *f"판단 영향 : {impact}".split(" — "),
    ]
    return lines + [line for item in tail for line in wrap_mobile(item)]


# --- conditional-order rendering -----------------------------------------------


def grouped_conditional_orders(orders: tuple) -> tuple:
    """(shown, hidden_count): BUY ≤ cap, then SELL ≤ cap, then side-unknown
    ≤ cap, each group in the read's own order; deterministic."""
    groups = {
        side: [order for order in orders if order.side == side]
        for side in (*ORDER_SIDES, None)
    }
    shown = [
        order
        for side in (*ORDER_SIDES, None)
        for order in groups[side][:CONDITIONAL_SIDE_CAP]
    ]
    return tuple(shown), len(orders) - len(shown)


def _order_lines(index: int, order: ConditionalOrderContextV1, currency: str) -> list:
    """Compact block: index, prices, 거래구분, 만료일. No scope label, leg
    type/status, price gap or zone relation (user contract 2026-09-04)."""
    lines = [f"[{index}]"]
    trigger, price = order.trigger_price, order.order_price
    if trigger is not None and price is not None and trigger == price:
        lines.append(f"발동/주문가 : {_price(trigger, currency)}")
    else:
        lines.append(
            "발동가 : 미상"
            if trigger is None
            else f"발동가 : {_price(trigger, currency)}"
        )
        if price is not None:
            lines.append(f"주문가 : {_price(price, currency)}")
        else:
            lines.append(
                "주문가 : 시장가" if order.order_type == "MARKET" else "주문가 : 미상"
            )
    side = _SIDE_KR.get(order.side)
    lines.append(
        f"거래구분 : {side} {order.quantity}주"
        if side
        else f"거래구분 : 확인 불가 · {order.quantity}주"
    )
    lines.append(
        "만료일 : 미상"
        if order.expire_date is None
        else f"만료일 : {order.expire_date}"
    )
    return lines


def _conditional_lines(
    card: DecisionCardV1, context: CardContextV1, currency: str, unavailable: list
) -> list:
    lines = [CONDITIONAL_HEADING]
    conditional = context.conditional
    if conditional is None or conditional.status == "UNAVAILABLE":
        reason = (
            "조회 미실행"
            if conditional is None
            else (conditional.reason or "확인 불가")
        )
        lines.append(f"확인 불가 · {reason}")
        unavailable.append((f"{card.symbol}:{_SECTION_CONDITIONAL}", reason))
        return lines
    if conditional.status == "NONE":
        lines.append("없음 · OPEN 조회 완료")
        return lines
    shown, hidden = grouped_conditional_orders(conditional.orders)
    for index, order in enumerate(shown, 1):
        lines += _order_lines(index, order, currency)
    if hidden:
        lines.append(f"외 {hidden}건..")
    if conditional.reason:
        lines += wrap_mobile(f"※ {conditional.reason}")
    return lines


# --- holdings rendering ---------------------------------------------------------


def _holdings_lines(
    card: DecisionCardV1,
    context: CardContextV1,
    market: str,
    grounded: set,
    unavailable: list,
) -> list:
    """Personal-account positions only (the controller never passes retirement
    positions). Market weight over personal stock of the same market. There is
    no account-total line: the KR+US stock ratio is not an account total, so
    it is never rendered; its unavailability reason is kept for the receipt."""
    lines = [HOLDINGS_HEADING]
    if not context.holdings_available:
        lines.append("산출 불가 · 보유 스토어 미확인")
        unavailable.append(
            (f"{card.symbol}:{_SECTION_HOLDINGS}", "holdings store unavailable")
        )
        return lines
    if not context.holdings:
        lines.append("미보유")
        grounded.add(_SECTION_HOLDINGS)
        return lines
    label = MARKET_WEIGHT_LABELS.get(market, market)
    for holding in context.holdings:
        lines.append(f"보유 : {holding.quantity}주 / 평단 : {holding.avg_price}")
        weight = holding.weight
        if weight is None:
            lines.append(f"비중 : {label} 산출 불가 · 평가액 미연결")
            unavailable.append((f"{card.symbol}:비중", "weights not wired"))
            continue
        if weight.market_weight is None:
            # the exact evidence reason on its own line(s), split only at its own seams
            lines += [
                f"비중 : {label} 산출 불가",
                *wrap_mobile(weight.market_reason or "사유 미상"),
            ]
            unavailable.append(
                (f"{card.symbol}:비중", weight.market_reason or "unavailable")
            )
        else:
            lines.append(f"비중 : {label} {_pct(weight.market_weight, signed=False)}")
            grounded.add("비중")
        if weight.total_weight is None:
            unavailable.append(
                (f"{card.symbol}:계좌 전체 비중", weight.total_reason or "unavailable")
            )
    grounded.add(_SECTION_HOLDINGS)
    return lines


# --- validated contextual analysis rendering -------------------------------------


def _analysis_lines(analysis) -> list:
    """Concise ``맥락 분석`` from an already-validated ContextualAnalysisV1 only;
    every statement carries the labels of the evidence it connects."""

    def cite(entry) -> str:
        return (
            " ("
            + ", ".join(analysis.labels[item] for item in entry["evidence_ids"])
            + ")"
        )

    def bullets(title, entries) -> list:
        return [title] + [f"• {entry['statement']}{cite(entry)}" for entry in entries]

    assessment, confidence = analysis.conditional_order_assessment, analysis.confidence
    lines = [ANALYSIS_LABEL, analysis.summary, ""]
    lines += bullets("근거 관계", analysis.evidence_relations)
    lines += [
        f"조건주문 평가: {assessment['verdict']} — {assessment['statement']}{cite(assessment)}"
    ]
    lines += bullets("선택지", analysis.alternatives)
    lines += bullets("반대 근거", analysis.counterevidence)
    if analysis.assumptions:
        lines += bullets("가정", analysis.assumptions)
    lines += bullets("반증 조건", analysis.falsifiers)
    lines += [
        f"확신도: {confidence['level']} — {confidence['statement']}{cite(confidence)}"
    ]
    lines += bullets("판단 변경 조건", analysis.change_conditions)
    return lines


def _actionable_analysis_lines(analysis, action_lines=()) -> list:
    """Grounded values adjacent to each judgment; no generated text is trimmed."""

    def grounds(entry):
        lines = []
        for key in entry["evidence_ids"]:
            item = analysis.labels[key]
            asof = re.sub(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
                lambda match: _kst(datetime.fromisoformat(match[0])) + " KST",
                item["asof"],
            )
            # Keep the value and its date together, even for long factor labels.
            lines += wrap_mobile("근거: " + item["label"])
            lines += wrap_mobile(
                " · ".join(value for value in (item["display"], asof) if value)
            )
        return lines

    conclusion_grounds = grounds(analysis.evidence_relations[0])

    def statements(entries, *, dedupe_conclusion=False):
        lines = []
        for entry in entries:
            evidence = grounds(entry)
            lines += wrap_mobile(entry["statement"])
            if not dedupe_conclusion or evidence != conclusion_grounds:
                lines += evidence
        return lines

    lines = [
        "투자판단",
        *wrap_mobile(analysis.summary),
        *conclusion_grounds,
        "",
        "왜 이렇게 보나",
        *statements(analysis.evidence_relations, dedupe_conclusion=True),
        *statements(analysis.counterevidence, dedupe_conclusion=True),
        "",
        "어떻게 대응하나",
        *statements(analysis.alternatives),
        *action_lines,
    ]
    if analysis.conditional_order_assessment is not None:
        lines += statements((analysis.conditional_order_assessment,))
    reversals = list(analysis.change_conditions)
    for entry in analysis.falsifiers:
        if entry not in reversals:
            reversals.append(entry)
    lines += ["", "판단을 바꿀 때", *statements(reversals)]
    return lines


def _zone_action_lines(card, context, asof):
    """Reference-session actions, using the existing position calculation only.

    Report verification and the quote-age gate bound usable data. Neither
    establishes a current tick or counts elapsed sessions to renew a zone.
    """
    from tokmo.programs.quant_contextual_analysis import _zone_position

    zone = card.entry_zone
    if zone is None:
        checks = entry_zone_condition_checks(card)
        failed = [detail for _, passed, detail in checks if passed is False]
        unknown = [detail for _, passed, detail in checks if passed is None]
        lines = []
        if failed:
            lines += ["현재 추세눌림 조건은 충족하지 않습니다.", *failed]
        if unknown:
            lines += ["추세눌림 판단 자료가 부족합니다.", *unknown]
        if not failed and not unknown:
            lines += [
                "기본 지표 조건은 충족하지만 구간이 없는 이유는 이 리포트만으로 확인되지 않습니다."
            ]
        return [
            *lines,
            f"지표 기준 세션 {asof}",
            "매수 확인 신호는 아직 계산하지 않습니다.",
        ]
    currency = _currency(card.market.value)
    bounds = f"{_price(zone.lower, currency)}~{_price(zone.upper, currency)}"
    invalidation = _price(zone.invalidation, currency)
    use_quote = context.quote is not None and context.quote_age_valid
    basis = "기록 가격은" if use_quote else "기준 세션 종가는"
    price = context.quote.price if use_quote else zone.latest_close
    position = _zone_position(price, zone)["position"]
    if position == "above_upper":
        action = f"{basis} 관찰구간 {bounds} 위입니다. 추격을 보류하고 구간 재진입 여부를 확인합니다."
    elif position == "below_invalidation":
        action = f"{basis} 무효화 {invalidation} 아래입니다. 관찰구간 {bounds}의 매수 판단을 재검토합니다."
    elif position == "inside":
        action = f"{basis} 관찰구간 {bounds} 안입니다. 기준 세션의 구간 안에서 매수를 검토할 수 있습니다."
    elif position == "below_lower":
        action = f"{basis} 관찰구간 {bounds} 하단 아래입니다. 구간 회복 여부를 확인한 뒤 매수를 재검토합니다."
    else:
        action = f"관찰구간 {bounds}은 기준 세션의 참고값입니다. 가격과 구간의 위치 확인 후 매수를 검토합니다."
    validity = (
        "최신 저장 리포트 확인 · 가격 관측 7일 이내"
        if context.report_verified and context.quote_age_valid
        else "과거/참고 조건 · "
        + (context.quote_validity_reason or "자료 최신성 미확인")
    )
    return [
        action,
        f"무효화 {invalidation} 아래에서는 이 구간의 매수 근거가 유지되지 않습니다.",
        f"구간 기준 {asof} · 유효 {zone.expiry_completed_sessions}완료 세션",
        validity + " · 실시간 가격·구간 잔여 유효기간 미확인 · 매수 확인 신호 미계산.",
    ]


def _actionable_card_block(
    card, context, standings, grounded, unavailable, analysis, *, asof=None
):
    if analysis is None:
        from tokmo.programs.quant_contextual_analysis import (
            _display_labels,
            build_evidence_pack,
        )

        labels = _display_labels(
            build_evidence_pack(
                card, context, completed_session_date=asof or "세션 미기록"
            )
        )
    else:
        labels = analysis.labels
    session = asof or next(
        (value["asof"] for key, value in labels.items() if key.startswith("f:")),
        "세션 미기록",
    )
    lines = ["• " + display_or_symbol(card.market.value, card.symbol, card.name)]
    tags = ["보유 종목"] if context.holdings_available and context.holdings else []
    if context.watchlisted is True:
        tags.append("관심종목")
    if tags:
        lines.append(" · ".join(tags))
    action = [
        line
        for item in _zone_action_lines(card, context, session)
        for line in wrap_mobile(item)
    ]
    if analysis is None:
        judgment = _judgment(card, context, standings)
        lines += ["", "투자판단", "규칙 기반 · AI 분석 미연결", judgment["summary"][0]]
        if judgment["grounds"]:
            lines += [
                "근거: " + " · ".join(judgment["grounds"]),
                f"지표 기준 세션 {session}",
            ]
        if judgment["counter"]:
            lines += ["반대 근거: " + " · ".join(judgment["counter"])]
        lines += ["", "어떻게 대응하나", *action]
        grounded.add(_SECTION_JUDGMENT)
    else:
        lines += ["", *_actionable_analysis_lines(analysis, action)]
        grounded.update((_SECTION_ANALYSIS, _SECTION_JUDGMENT))
    lines += ["", "지표 요약"]
    quote = context.quote
    if quote is not None:
        lines += [
            f"기록 가격 {_price(quote.price, quote.currency)}",
            "가격 관측 " + _kst(quote.observed_at) + " KST"
            if quote.observed_at
            else "가격 관측시각 미기록",
        ]
    else:
        lines.append("기록 가격 없음")
    if card.entry_zone is not None:
        lines.append(
            f"기준 세션 종가 {_price(card.entry_zone.latest_close, _currency(card.market.value))}"
        )
    lines += ["수익률", PRE_FENCE, *_returns_table(card), PRE_FENCE]
    if _factor(card, "ret_60") is not None:
        lines.append(f"60일 수익률 {_pct(_factor(card, 'ret_60'))}")
    for key in ("f:rsi_14", "f:realized_vol_63", "f:max_drawdown"):
        item = labels.get(key)
        if item:
            lines.append(f"{item['label']} {item['display']}")
    lines.append(f"지표 기준 세션 {session}")
    if context.holdings_available and context.holdings:
        holdings = _holdings_lines(
            card, context, card.market.value, grounded, unavailable
        )
        holdings[0] = "보유 현황"
        lines += [
            "",
            *holdings,
            f"비중 기준: 각 보유계좌의 {MARKET_WEIGHT_LABELS.get(card.market.value, card.market.value)} 주식 평가액",
        ]
    sample = standings.get("headline", {}).get(card.symbol)
    if sample:
        lines += ["", f"이번 리포트의 비교 대상 {sample[1]}종목"]
    grounded.add("지표")
    if quote is not None:
        grounded.add(_SECTION_QUOTE)
    else:
        unavailable.append(
            (
                f"{card.symbol}:{_SECTION_QUOTE}",
                context.quote_unavailable_reason or "시세 근거 없음",
            )
        )
    if card.entry_zone is not None:
        grounded.add(_SECTION_ZONE)
        unavailable.append(
            (f"{card.symbol}:확인 후 진입구간", "confirmation rule not defined")
        )
    else:
        unavailable.append(
            (f"{card.symbol}:{_SECTION_ZONE}", "report entry_zone is null")
        )
    if context.playbook is None:
        unavailable.append(
            (
                f"{card.symbol}:{_SECTION_PLAYBOOK}",
                context.playbook_unavailable_reason or "플레이북 문서 없음",
            )
        )
    # Keep the existing monospace table intact; wrap prose by display cells.
    return "\n".join(
        line for item in lines for line in (wrap_mobile(item) if item else [""])
    )


# --- card rendering ---------------------------------------------------------


def _card_block(
    card: DecisionCardV1,
    context: CardContextV1,
    standings: dict,
    grounded: set,
    unavailable: list,
    analysis=None,
    *,
    asof=None,
) -> str:
    if analysis is None or analysis.display_version == 2:
        return _actionable_card_block(
            card, context, standings, grounded, unavailable, analysis, asof=asof
        )
    market = card.market.value
    currency = _currency(market)
    lines = ["• " + display_or_symbol(market, card.symbol, card.name), ""]

    # selection rationale (report product + local membership, never invented)
    membership = []
    if context.holdings_available:
        membership.append("보유 종목" if context.holdings else "미보유")
    else:
        membership.append("보유 여부 미확인")
    if context.watchlisted is None:
        membership.append("관심종목 미확인")
    else:
        membership.append("관심종목" if context.watchlisted else "관심종목 아님")
    lines += [
        "선정 근거",
        "완료 세션 거래대금 상위 후보 (MARKET_DISCOVERY 리포트)",
        " · ".join(membership),
        "",
    ]
    grounded.add("선정 근거")

    # current price: the price only (no separate observation time/source
    # line — the only time shown is the truthful session date in the header)
    lines.append(_SECTION_QUOTE)
    if context.quote is not None:
        lines.append(_price(context.quote.price, context.quote.currency))
        grounded.add(_SECTION_QUOTE)
    else:
        reason = context.quote_unavailable_reason or "시세 근거 없음"
        lines.append(f"산출 불가 — {reason}")
        unavailable.append((f"{card.symbol}:{_SECTION_QUOTE}", reason))
    if card.entry_zone is not None:
        lines.append(f"완료 세션 종가 {_price(card.entry_zone.latest_close, currency)}")
    lines.append("")

    # Slash-separated horizons, each label centred over its value; the fence
    # lines become one Telegram ``pre`` entity at the send boundary.
    return_labels, return_values = _returns_table(card)
    lines += ["수익률", PRE_FENCE, return_labels, return_values, PRE_FENCE, ""]
    rsi = _factor(card, "rsi_14")
    lines += [
        "RSI14",
        "산출 불가"
        if rsi is None
        else f"{rsi.quantize(Decimal('0.1'))} — {_rsi_label(rsi)}",
        "",
    ]
    vol = _factor(card, "realized_vol_63")
    lines += [
        "63일 변동성",
        "산출 불가"
        if vol is None
        else f"{_pct(vol, signed=False)} — {_vol_label(vol)}",
        "",
    ]
    lines += ["최대낙폭", _pct(_factor(card, "max_drawdown")), ""]
    grounded.add("지표")

    # standings (reduced universe = cards in this report)
    headline = standings["headline"].get(card.symbol)
    lines.append(_SECTION_STANDINGS)
    if headline is None:
        lines.append("산출 불가 — 비교 가능한 지표 없음")
        unavailable.append(
            (f"{card.symbol}:{_SECTION_STANDINGS}", "no comparable factor")
        )
    else:
        lines.append(f"비교 대상 {headline[1]}종목 중 {headline[0]}위")
        lines.append("※ 리포트 카드 한정 비교 (보유∪관심 전체 아님)")
        if headline[1] < LOW_SAMPLE_THRESHOLD:
            lines.append(f"※ 표본 부족 ({headline[1]}종목) — 저신뢰")
        lines.append("")
        for name, label in STANDING_FACTORS:
            standing = standings["factors"][name].get(card.symbol)
            lines.append(
                f"• {label}: "
                + ("산출 불가" if standing is None else f"{standing[0]}위")
            )
        grounded.add(_SECTION_STANDINGS)
    lines.append("")

    # interpretation from ordinals only
    lines.append("해석")
    if headline is None:
        lines.append("비교 지표가 없어 해석을 생략합니다.")
    else:
        momentum = standings["factors"]["ret_120"].get(card.symbol)
        stability = standings["factors"]["realized_vol_63"].get(card.symbol)
        defense = standings["factors"]["max_drawdown"].get(card.symbol)

        def tier(standing):
            if standing is None:
                return "산출 불가"
            ordinal, sample = standing
            return (
                "최상위"
                if ordinal == 1
                else ("하위권" if ordinal == sample else "중위권")
            )

        lines.append(f"상승 강도는 비교군 {tier(momentum)},")
        lines.append(
            f"가격 안정성은 {tier(stability)}, 낙폭 방어력은 {tier(defense)}입니다."
        )
    lines.append("")

    # holdings context (personal account) + weights over exact denominators
    lines += _holdings_lines(card, context, market, grounded, unavailable)
    lines.append("")

    # conditional orders (injected one-page OPEN read; never inferred) —
    # directly after holdings, before playbook / entry zone / judgment
    lines += _conditional_lines(card, context, currency, unavailable)
    if context.conditional is not None and context.conditional.status != "UNAVAILABLE":
        grounded.add(_SECTION_CONDITIONAL)
    lines.append("")

    # playbook: structure-only synthesis, labelled; never raw text or paths
    trend_kind = _trend(card)
    lines += _playbook_lines(context, trend_kind)
    if context.playbook is None:
        unavailable.append(
            (
                f"{card.symbol}:{_SECTION_PLAYBOOK}",
                context.playbook_unavailable_reason or "플레이북 문서 없음",
            )
        )
    else:
        grounded.add(_SECTION_PLAYBOOK)
    lines.append("")

    # zones
    zone = card.entry_zone
    lines.append(_SECTION_ZONE)
    if zone is None:
        zone_lines, verdict = _zone_unavailable_lines(card)
        lines += zone_lines
        lines += ["", "무효화 조건", f"산출 불가 · 관찰구간 {verdict}"]
        unavailable.append(
            (f"{card.symbol}:{_SECTION_ZONE}", "report entry_zone is null")
        )
    else:
        lines.append(
            f"신규 진입 관찰구간: {_price(zone.lower, currency)}~{_price(zone.upper, currency)}"
        )
        lines.append(
            f"근거: {zone.setup_kind} · "
            + ", ".join(zone.evidence)
            + f" · 유효 {zone.expiry_completed_sessions}세션"
        )
        if context.quote is not None:
            gap = context.quote.price / zone.upper - 1
            if gap > 0:
                lines.append(
                    f"현재가는 관찰구간 상단보다 {_pct(gap, signed=False)} 높습니다."
                )
            elif context.quote.price < zone.lower:
                lines.append(
                    f"현재가는 관찰구간 하단보다 {_pct(-(context.quote.price / zone.lower - 1), signed=False)} 낮습니다."
                )
            else:
                lines.append("현재가는 관찰구간 안에 있습니다.")
        lines.append("확인 후 진입구간: 산출 불가 — 확인 규칙 미정의")
        lines += [
            "",
            "무효화 조건",
            f"{_price(zone.invalidation, currency)} 하향 이탈 또는",
            "핵심 실적 가설 훼손",
        ]
        grounded.add(_SECTION_ZONE)
        unavailable.append(
            (f"{card.symbol}:확인 후 진입구간", "confirmation rule not defined")
        )
    lines.append("")

    # judgment (deterministic); the validated AI analysis, when present, follows it
    analysed = analysis is not None and analysis.symbol == card.symbol
    judgment = _judgment(card, context, standings, analysed=analysed)
    lines.append(
        f"{_SECTION_JUDGMENT} ({PROVISIONAL_LABEL_ANALYSED if analysed else PROVISIONAL_LABEL})"
    )
    lines += list(judgment["summary"])
    lines.append("근거: " + (", ".join(judgment["grounds"]) or "없음"))
    lines.append("반대 근거: " + (", ".join(judgment["counter"]) or "없음"))
    lines.append(f"신뢰도: {judgment['confidence']}")
    lines.append(f"변경 조건: {judgment['change']}")
    lines.append("")
    lines.append("행동 제안")
    lines += [f"• {name}: {value}" for name, value in judgment["actions"]]
    grounded.add(_SECTION_JUDGMENT)
    if analysed:
        lines += [""] + _analysis_lines(analysis)
        grounded.add(_SECTION_ANALYSIS)
    return "\n".join(lines)


def _header(report: QuantRunReportV1) -> str:
    """Compact header: market, completed-session date, card count.

    The report identity records only the completed session DATE (no run or
    session time-of-day exists in the report or its receipt), so the second
    line keeps that truthful granularity instead of inventing a time. The
    report digest, delivery label and order-write disclaimer stay out of the
    visible text (the digest lives in the receipt / group marker)."""
    identity = report.identity
    session = (
        identity.completed_session_date.isoformat()
        if identity.completed_session_date
        else "세션 미상"
    )
    return "\n".join(
        (f"📊 퀀트 {identity.market.value}", session, f"카드 {len(report.cards)}종목")
    )


def render_market_message(
    report: QuantRunReportV1,
    contexts,
    *,
    report_sha256: str,
    analyses: dict | None = None,
) -> MarketMessageV1:
    """One market → one or more ≤4096-char parts split only at card boundaries.
    ``analyses`` maps symbol → validated ContextualAnalysisV1; absent/None keeps
    the deterministic card byte-identical."""
    market = report.identity.market.value
    if any(card.market.value != market for card in report.cards):
        raise ValueError("cards must belong to the report market")
    by_symbol = {context.symbol: context for context in contexts}
    grounded: set = set()
    unavailable: list = []
    header = _header(report)
    blocks = []
    standings = compute_standings(report.cards)
    for card in sorted(report.cards, key=lambda item: item.symbol):
        context = by_symbol.get(card.symbol, CardContextV1(card.symbol))
        card_grounded: set = set()
        card_unavailable: list = []
        analysis = (analyses or {}).get(card.symbol)
        block = _card_block(
            card,
            context,
            standings,
            card_grounded,
            card_unavailable,
            analysis,
            asof=(
                report.identity.completed_session_date.isoformat()
                if report.identity.completed_session_date
                else "세션 미기록"
            ),
        )
        if (
            analysis is not None
            and analysis.display_version == 1
            and _chars(header + "\n\n" + block) > MESSAGE_CHAR_CAP
        ):
            card_grounded, card_unavailable = set(), []
            block = _card_block(
                card, context, standings, card_grounded, card_unavailable, None
            )
        blocks.append(block)
        grounded.update(card_grounded)
        unavailable.extend(card_unavailable)
    if not blocks:
        codes = ", ".join(code.value for code in report.unavailable_codes) or "없음"
        blocks.append(f"카드 없음 — 리포트 unavailable: {codes}")
        unavailable.append(("market:cards", "report has no cards"))
    if (
        any(
            item is not None and item.display_version == 2
            for item in (analyses or {}).values()
        )
        and _chars(header + "\n\n" + "\n\n".join(blocks)) > MESSAGE_CHAR_CAP
    ):
        raise ValueError("market payload exceeds message cap")
    parts: list = []
    current = header
    for block in blocks:
        candidate = current + "\n\n" + block
        if _chars(candidate) <= MESSAGE_CHAR_CAP:
            current = candidate
            continue
        if current != header:
            parts.append(current)
        current = header + "\n\n" + block
        if _chars(current) > MESSAGE_CHAR_CAP:
            raise ValueError("card block exceeds message cap")
    parts.append(current)
    for part in parts:
        if _private(part):
            raise ValueError("render_privacy")
    session = (
        report.identity.completed_session_date.isoformat()
        if report.identity.completed_session_date
        else ""
    )
    return MarketMessageV1(
        market,
        session,
        report.identity.identity_sha256,
        report_sha256,
        tuple(parts),
        tuple(sorted(grounded)),
        tuple(unavailable),
    )


def render_decision_cards(
    reports: dict, contexts: dict, analyses: dict | None = None
) -> tuple:
    """Exactly one MarketMessageV1 per market in MARKETS order; a missing
    report renders an explicit 'no report' message rather than nothing."""
    messages = []
    analyses = analyses or {}
    for market in MARKETS:
        located = reports.get(market)
        if located is None:
            text = "\n".join(
                (
                    f"📊 퀀트 {market}",
                    "리포트 없음 — 검증된 완료 세션 리포트가 없습니다",
                )
            )
            messages.append(
                MarketMessageV1(
                    market,
                    "",
                    "",
                    "",
                    (text,),
                    (),
                    (("market:report", "no verified report"),),
                )
            )
            continue
        report, digest = located
        messages.append(
            render_market_message(
                report,
                contexts.get(market, ()),
                report_sha256=digest,
                analyses=analyses.get(market),
            )
        )
    return tuple(messages)


# --- one-attempt canary delivery -------------------------------------------

NO_RESEND_STATUSES = frozenset({"UNKNOWN", "RESERVED"})


def run_quant_telegram_card_canary(
    *,
    activation: dict,
    receipts,
    activation_id: str,
    messages: tuple,
    telegram_send,
    now: datetime,
    override_report_dedupe: bool = False,
) -> dict:
    """Exactly-one bounded send per market of the rendered market messages.

    Markets are independent (live defect 2026-09-03): each market/report
    digest gets its own one attempt, and a FAILED/UNKNOWN part stops only
    the remaining parts of that same market. The durable reservation is
    written BEFORE the first transport start. SENT, FAILED, UNKNOWN
    (timeout / settle failure after a possible send) all consume that
    market's attempt; a digest whose transport may already have started is
    deduped on any later activation. UNKNOWN and unsettled RESERVED are
    permanent: no override can resend them. A SKIPPED part never started
    transport, so its report stays eligible for a later bounded recovery.
    """
    result = {
        "off": None,
        "reserved": 0,
        "sent": 0,
        "failed": 0,
        "unknown": 0,
        "skipped": 0,
        "deduped": {},
        "parts": [],
    }
    if activation.get("mode") != "CANARY":
        result["off"] = activation.get("reason") or "mode"
        return result
    if activation.get("activation_id") != activation_id:
        result["off"] = "activation_mismatch"
        return result
    if receipts.read(activation_id) is not None:
        result["off"] = "cap_consumed"
        return result
    eligible = []
    for message in messages:
        prior = (
            receipts.report_attempted(message.report_sha256)
            if message.report_sha256
            else None
        )
        if prior is not None:
            prior_id, prior_status = prior
            if not override_report_dedupe or prior_status in NO_RESEND_STATUSES:
                result["deduped"][message.market] = f"{prior_id}:{prior_status}"
                continue
        eligible.append(message)
    if not eligible:
        result["off"] = "report_already_attempted"
        return result
    plan = [
        (message.market, f"{message.market}:{index}", text)
        for message in eligible
        for index, text in enumerate(message.parts, 1)
    ]
    digests = {message.market: message.report_sha256 for message in eligible}
    if not receipts.reserve(
        activation_id,
        report_digests=digests,
        part_keys=tuple(key for _, key, _ in plan),
        now=now,
        deduped=dict(result["deduped"]),
    ):
        result["off"] = "cap_consumed"
        return result
    result["reserved"] = 1
    stopped_markets: set = set()
    for market, key, text in plan:
        if market in stopped_markets:
            receipts.settle(
                activation_id, key, "SKIPPED", now=now, reason="aborted_after_failure"
            )
            result["skipped"] += 1
            result["parts"].append((key, "SKIPPED"))
            continue
        try:
            provider_message_id = telegram_send(text)
        except TimeoutError:
            receipts.settle(
                activation_id, key, "UNKNOWN", now=now, reason="provider_timeout"
            )
            result["unknown"] += 1
            result["parts"].append((key, "UNKNOWN"))
            stopped_markets.add(market)
            continue
        except Exception:
            receipts.settle(
                activation_id, key, "FAILED", now=now, reason="provider_failure"
            )
            result["failed"] += 1
            result["parts"].append((key, "FAILED"))
            stopped_markets.add(market)
            continue
        try:
            receipts.settle(
                activation_id,
                key,
                "SENT",
                now=now,
                provider_message_id=str(provider_message_id),
            )
        except Exception:
            try:
                receipts.settle(
                    activation_id,
                    key,
                    "UNKNOWN",
                    now=now,
                    reason="settle_failed_after_delivery",
                )
            except Exception:
                pass
            result["unknown"] += 1
            result["parts"].append((key, "UNKNOWN"))
            stopped_markets.add(market)
            continue
        result["sent"] += 1
        result["parts"].append((key, "SENT"))
    return result
