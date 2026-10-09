"""Quant contextual decision analysis (SDS 2026-09-03 v0.1, first vertical).

Projects one rendered decision card's existing context (report factors,
entry zone, latest quote, holdings/weights, the exact-matched OPEN
conditional orders, the structure-only playbook synthesis) into a small
JSON-safe *evidence pack*, computes the price/order/zone relations with
Decimal, hands the pack to an injected analysis callable (default OFF: zero
model calls), and validates the closed JSON reply: required sections, every
``evidence_ids`` entry resolves to a pack id, relation claims connect at
least two ids, no numeric reach probability (none is modelled here), no
private marker, no order instruction. Any failure yields ``None`` so the
deterministic card stays byte-identical.

External search is a pair of injected pure seams (``search``/``fetch``)
used only for gaps that can change the judgment: no gap → zero calls;
otherwise ≤3 queries, ≤5 primary fetches, retry 0, early exit once the
gaps close. Price/holdings/orders are never routed to the web. Nothing here
opens a file, a network connection or a store, and no model or provider
output is persisted.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from tokmo.programs.quant_telegram_card import (
    STANCE_LABELS,
    CardContextV1,
    _judgment,
    _private,
    _trend,
    entry_zone_condition_checks,
    format_price,
    playbook_agreement,
)
from tokmo.programs.quant_telegram_card import (
    _pct as display_pct,
)
from tokmo.values.quant_discovery import DecisionCardV1, FactorStatus

PACK_SCHEMA = "quant-contextual-evidence-v1"
MAX_QUERIES, MAX_FETCHES, RETRIES = 3, 5, 0
MAX_ITEMS, MAX_STATEMENT_CHARS, MAX_SUMMARY_CHARS, NEWS_MAX_AGE_DAYS = 5, 240, 400, 14
CONTEXT_KINDS = frozenset(
    {
        "playbook_current",
        "user_plan",
        "trade_reason",
        "news",
        "disclosure",
        "source_unavailable",
    }
)
_RECENT_KINDS = frozenset({"news", "disclosure"})
# playbook-derived kinds: prose summaries must carry no digits at all —
# dates/prices/quantities/amounts are private in these context summaries
_TEXTUAL_KINDS = frozenset({"playbook_current", "user_plan", "trade_reason"})
_CONTEXT_ITEM_DIGITS = re.compile(r"\d")
# bounded provenance suffix ("... [ref]") collectors may append to
# ``source_label``; kept in the pack's ``source`` metadata, stripped from
# the user-facing ``label`` (rendered citations show labels only)
_REF_SUFFIX_RE = re.compile(r" \[[^\]]*\]$")
FACTOR_LABELS = {
    "ret_5": "5일 수익률",
    "ret_20": "20일 수익률",
    "ret_60": "60일 수익률",
    "ret_120": "120일 수익률",
    "rsi_14": "RSI14",
    "realized_vol_63": "63일 변동성",
    "max_drawdown": "최대낙폭",
    "close_to_sma_20": "20일선 대비",
    "close_to_sma_60": "60일선 대비",
    "close_to_sma_120": "120일선 대비",
    "risk_adjusted_momentum": "위험조정 모멘텀",
    "macd_histogram": "MACD 히스토그램",
    "downside_vol_63": "연환산 63일 하방 변동성",
    "price_position_200": "200세션 가격범위 내 위치",
    "volume_change_20": "20일 거래량 변화",
    "trading_amount_change_20": "20일 거래대금 변화",
}
SEARCHABLE_GAPS = {
    "gap:news_disclosure": "최근 사건·공시",
    "gap:playbook_conflict": "플레이북 가설 1차 근거",
    "gap:one_sided": "반대 근거",
}
CONFIDENCE_LEVELS = ("낮음", "보통", "높음")
CONDITIONAL_VERDICTS = ("맞음", "조정 검토", "판단 불충분", "해당 없음")
LIST_SECTIONS = (
    "evidence_relations",
    "alternatives",
    "counterevidence",
    "assumptions",
    "falsifiers",
    "change_conditions",
)
REQUIRED_KEYS = frozenset(
    ("symbol", "summary", "conditional_order_assessment", "confidence") + LIST_SECTIONS
)
_PERCENT_REACH = re.compile(
    r"(\d+(?:\.\d+)?\s*%\s*(?:도달|가능성|확률))|((?:도달|가능성)\s*\d+(?:\.\d+)?\s*%)"
)
_NUMERIC_PROBABILITY = re.compile(
    r"(?:확률|probability|likelihood)\s*(?:은|는|이|가|:)?\s*\d+(?:\.\d+)?\s*(?:%|퍼센트)?"
    r"|(?:odds)\s*\d+(?:\.\d+)?(?:\s*[:/]\s*\d+(?:\.\d+)?)?",
    re.IGNORECASE,
)
_ALLOWED_ANALYSIS_NUMBER_LABELS = (
    "RSI14",
    "120일",
    "200일",
    "63일",
    "60일",
    "20일",
    "5일",
)
_ORDER_INSTRUCTIONS = (
    "주문 실행",
    "주문을 실행",
    "주문 제출",
    "주문을 제출",
    "주문 넣",
    "주문을 넣",
    "자동 주문",
    "주문을 변경",
    "주문 변경해",
    "주문을 취소",
    "취소하세요",
    "매수하세요",
    "매도하세요",
    "매수하십시오",
    "매도하십시오",
    "place order",
    "execute order",
    "submit order",
    "cancel order",
)
_UNKNOWN_ORDER_INFERENCES = (
    "매수",
    "매도",
    "매입",
    "처분",
    "청산",
    "이익 실현",
    "이익실현",
    "차익 실현",
    "차익실현",
    "익절",
    "손절",
    "손실 제한",
    "손실제한",
)


class AnalysisValidationError(ValueError):
    """Closed-output contract violation; the code is the first argument."""


@dataclass(frozen=True)
class ContextItemV1:
    """Narrow optional context (playbook current/plan/trade reason, local news,
    disclosure) supplied by the caller; production binding is a later step.
    Playbook-derived summaries (``playbook_current``/``user_plan``/
    ``trade_reason``) must carry no digits — dates, prices, quantities and
    amounts are scrubbed by the collectors and rejected here; NEWS titles
    and disclosure report names may carry their own digits. A bounded
    non-secret canonical source reference (e.g. NEWS ``news_document_id``,
    DART ``rcept_no``) may ride ``source_label`` as traceable evidence
    metadata — pack ``label``/``source`` fields only, never user-facing
    rendered analysis text."""

    kind: str
    summary: str
    observed_on: str | None = None
    source_label: str = "로컬"
    available_at: str | None = None
    published_at: str | None = None


@dataclass(frozen=True)
class ContextualAnalysisV1:
    symbol: str
    summary: str
    evidence_relations: tuple
    conditional_order_assessment: dict | None
    alternatives: tuple
    counterevidence: tuple
    assumptions: tuple
    falsifiers: tuple
    change_conditions: tuple
    confidence: dict
    labels: dict  # historical: short label; current: resolved label/display/asof
    display_version: int = 1


# --- evidence pack ---------------------------------------------------------------


def _pct(value: Decimal | None) -> str | None:
    return (
        None
        if value is None
        else str((value * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    )


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else f"{value.normalize():f}"


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    return None if denominator <= 0 else numerator / denominator - 1


def _zone_position(price: Decimal, zone) -> dict:
    if price > zone.upper:
        position = "above_upper"
    elif price < zone.invalidation:
        position = "below_invalidation"
    elif price < zone.lower:
        position = "below_lower"
    else:
        position = "inside"
    return {
        "position": position,
        "to_upper_pct": _pct(_ratio(price, zone.upper)),
        "to_lower_pct": _pct(_ratio(price, zone.lower)),
        "to_invalidation_pct": _pct(_ratio(price, zone.invalidation)),
    }


def _check_context_item(item) -> ContextItemV1:
    if type(item) is not ContextItemV1 or item.kind not in CONTEXT_KINDS:
        raise ValueError("context_item_shape")
    if (
        not item.summary.strip()
        or len(item.summary) > MAX_SUMMARY_CHARS
        or _private(item.summary)
    ):
        raise ValueError("context_item_summary")
    if item.kind in _TEXTUAL_KINDS and _CONTEXT_ITEM_DIGITS.search(item.summary):
        raise ValueError("context_item_summary")
    return item


def _parse_date(text) -> date | None:
    try:
        return date.fromisoformat(text) if isinstance(text, str) else None
    except ValueError:
        return None


def build_evidence_pack(
    card: DecisionCardV1,
    context: CardContextV1,
    *,
    completed_session_date: str,
    standings: dict | None = None,
    context_items=(),
) -> dict:
    """Bounded JSON-safe projection of one card; raises on any private marker."""
    if context.symbol != card.symbol:
        raise ValueError("context_symbol_mismatch")
    currency = "KRW" if card.market.value == "KR" else "USD"
    evidence: list = []
    gaps: list = []
    quote, zone = context.quote, card.entry_zone
    if quote is not None:
        evidence.append(
            {
                "id": "price",
                "kind": "price",
                "label": "기록 가격"
                if context.quote_age_valid
                else "기록 가격 (참고 전용)",
                "age_valid": context.quote_age_valid,
                "validity_reason": context.quote_validity_reason,
                "value": _dec(quote.price),
                "currency": quote.currency,
                "observed_at": None
                if quote.observed_at is None
                else quote.observed_at.isoformat(),
                "source": quote.source_label,
            }
        )
    else:
        gaps.append(
            {
                "id": "gap:quote",
                "label": "현재가 없음",
                "detail": context.quote_unavailable_reason or "시세 근거 없음",
            }
        )
    if zone is not None:
        evidence.append(
            {
                "id": "session_close",
                "kind": "price",
                "label": "완료 세션 종가",
                "value": _dec(zone.latest_close),
                "currency": currency,
                "date": completed_session_date,
            }
        )
    for factor in card.factors:
        if factor.status is FactorStatus.AVAILABLE:
            evidence.append(
                {
                    "id": f"f:{factor.name}",
                    "kind": "factor",
                    "label": FACTOR_LABELS.get(factor.name, factor.name),
                    "value": _dec(factor.raw_value),
                    "orientation": factor.orientation.value,
                    "asof": completed_session_date,
                }
            )
    if zone is not None:
        evidence.append(
            {
                "id": "zone",
                "kind": "zone",
                "label": "관찰구간",
                "lower": _dec(zone.lower),
                "upper": _dec(zone.upper),
                "invalidation": _dec(zone.invalidation),
                "setup_kind": zone.setup_kind,
                "evidence": list(zone.evidence),
                "expiry_completed_sessions": zone.expiry_completed_sessions,
                "confidence": zone.confidence,
                "asof": completed_session_date,
            }
        )
        if quote is not None:
            evidence.append(
                {
                    "id": "rel:price_zone",
                    "kind": "relation",
                    "label": "현재가↔관찰구간",
                    **_zone_position(quote.price, zone),
                }
            )
    else:
        gaps.append(
            {
                "id": "gap:zone",
                "label": "관찰구간 없음",
                "detail": "리포트 entry_zone 없음",
                "checks": list(entry_zone_condition_checks(card)),
            }
        )
    if standings is not None and card.symbol in standings.get("headline", {}):
        ordinal, sample = standings["headline"][card.symbol]
        evidence.append(
            {
                "id": "standing",
                "kind": "standing",
                "label": "상대순위",
                "ordinal": ordinal,
                "sample": sample,
                "scope": "리포트 카드 한정",
            }
        )
    if not context.holdings_available:
        gaps.append(
            {
                "id": "gap:holdings",
                "label": "보유 미확인",
                "detail": "보유 스토어 미확인",
            }
        )
    for index, holding in enumerate(context.holdings, 1):
        weight = holding.weight
        evidence.append(
            {
                "id": f"holding:{index}",
                "kind": "holding",
                "label": f"보유({holding.scope_label})",
                "scope_label": holding.scope_label,
                "quantity": holding.quantity,
                "avg_price": holding.avg_price,
                "market_weight_pct": None
                if weight is None
                else _pct(weight.market_weight),
                "market_weight_reason": None
                if weight is None
                else weight.market_reason,
                "total_weight_pct": None
                if weight is None
                else _pct(weight.total_weight),
                "total_weight_reason": None if weight is None else weight.total_reason,
            }
        )
    conditional = context.conditional
    status = "NOT_READ" if conditional is None else conditional.status
    orders = (
        ()
        if status != "ACTIVE"
        else tuple(
            order for order in conditional.orders if order.side in ("BUY", "SELL")
        )
    )
    for index, order in enumerate(orders, 1):
        evidence.append(
            {
                "id": f"order:{index}",
                "kind": "conditional_order",
                "label": f"조건주문{index}",
                "scope_label": order.scope_label,
                "order_kind": order.order_kind,
                "order_type": order.order_type,
                "quantity": order.quantity,
                "leg_type": order.leg_type,
                "leg_status": order.leg_status,
                "trigger_price": _dec(order.trigger_price),
                "order_price": _dec(order.order_price),
                "expire_date": order.expire_date,
                "side": order.side.lower(),
            }
        )
        relation = {
            "id": f"rel:order:{index}",
            "kind": "relation",
            "label": f"조건주문{index}↔현재가·구간",
            "trigger_vs_price_pct": None,
            "trigger_minus_price": None,
            "zone": None,
            "order_vs_trigger_pct": None,
        }
        if order.trigger_price is not None:
            if quote is not None:
                relation["trigger_vs_price_pct"] = _pct(
                    _ratio(order.trigger_price, quote.price)
                )
                relation["trigger_minus_price"] = _dec(
                    order.trigger_price - quote.price
                )
            if zone is not None:
                relation["zone"] = _zone_position(order.trigger_price, zone)
            if order.order_price is not None:
                relation["order_vs_trigger_pct"] = _pct(
                    _ratio(order.order_price, order.trigger_price)
                )
        evidence.append(relation)
    trend_kind = _trend(card)
    playbook = context.playbook
    if playbook is not None:
        verdict, reason, _impact = playbook_agreement(playbook, trend_kind)
        evidence.append(
            {
                "id": "playbook",
                "kind": "playbook",
                "label": "플레이북",
                "scope_label": playbook.scope_label,
                "has_user_thesis": playbook.has_user_thesis,
                "has_hermes_current": playbook.has_hermes_current,
                "layer3_entries": playbook.layer3_entries,
                "latest_trade_side": playbook.latest_trade_side,
                "stance": playbook.stance,
                "stance_label": STANCE_LABELS.get(playbook.stance, "미분류"),
                "quant_trend": trend_kind,
                "quant_agreement": verdict,
                "agreement_reason": reason,
            }
        )
    else:
        gaps.append(
            {
                "id": "gap:playbook",
                "label": "플레이북 없음",
                "detail": context.playbook_unavailable_reason or "플레이북 문서 없음",
            }
        )
    session = _parse_date(completed_session_date)
    recent = False
    for index, item in enumerate(map(_check_context_item, context_items), 1):
        evidence.append(
            {
                "id": f"ctx:{index}",
                "kind": item.kind,
                "label": f"맥락{index}({_REF_SUFFIX_RE.sub('', item.source_label)})",
                "summary": item.summary,
                "observed_on": item.observed_on,
                "source": item.source_label,
            }
        )
        observed = _parse_date(item.observed_on)
        if (
            item.kind in _RECENT_KINDS
            and observed is not None
            and session is not None
            and 0 <= (session - observed).days <= NEWS_MAX_AGE_DAYS
        ):
            recent = True
    if not recent:
        unavailable = [
            item
            for item in context_items
            if getattr(item, "kind", None) == "source_unavailable"
        ]
        detail = (
            f"완료 세션 기준 {NEWS_MAX_AGE_DAYS}일 내 로컬 NEWS/공시 근거 없음"
            if not unavailable
            else f"로컬 NEWS/공시 스토어 확인 불가({', '.join(sorted({item.summary for item in unavailable}))}) — "
            "근거 부재가 아니라 가용성 문제"
        )
        gaps.append(
            {
                "id": "gap:news_disclosure",
                "label": "최근 사건·공시 근거 없음",
                "detail": detail,
            }
        )
    if (
        playbook is not None
        and playbook_agreement(playbook, trend_kind)[0] in ("상충", "주의")
        and not recent
    ):
        gaps.append(
            {
                "id": "gap:playbook_conflict",
                "label": "플레이북↔Quant 상충 1차 근거 없음",
                "detail": "플레이북 기조와 Quant 추세가 갈리나 최근 1차 근거 없음",
            }
        )
    judgment = _judgment(card, context, standings or {"headline": {}})
    if not judgment["grounds"] or not judgment["counter"]:
        gaps.append(
            {
                "id": "gap:one_sided",
                "label": "근거 편중",
                "detail": "지지 근거 또는 반대 근거가 비어 판단이 한쪽으로 치우침",
            }
        )
    pack = {
        "schema": PACK_SCHEMA,
        "symbol": card.symbol,
        "market": card.market.value,
        "name": card.name,
        "completed_session_date": completed_session_date,
        "currency": currency,
        "evidence": evidence,
        "gaps": gaps,
        "external": {
            "queries": 0,
            "fetches": 0,
            "retries": RETRIES,
            "gaps_closed": [],
            "gaps_open": [],
        },
    }
    if _private(repr(pack)):
        raise ValueError("evidence_pack_privacy")
    return pack


# --- local-first external search seams ------------------------------------------------


def gather_external_evidence(
    pack: dict, *, search=None, fetch=None, budget=None
) -> dict:
    """Return a new pack: ≤3 queries / ≤5 primary fetches / retry 0, only for
    searchable gaps; unbound seams or no gaps → zero calls and gaps kept as-is."""
    open_gaps = [gap["id"] for gap in pack["gaps"] if gap["id"] in SEARCHABLE_GAPS]
    external = dict(pack["external"], gaps_open=list(open_gaps), gaps_closed=[])
    budget = {"queries": 0, "fetches": 0} if budget is None else budget
    if set(budget) != {"queries", "fetches"}:
        raise ValueError("external_budget_shape")
    evidence = list(pack["evidence"])
    if search is None or fetch is None or not open_gaps:
        return dict(pack, external=external)
    for gap_id in open_gaps:
        if budget["queries"] >= MAX_QUERIES or budget["fetches"] >= MAX_FETCHES:
            break
        external["queries"] += 1
        budget["queries"] += 1
        try:
            candidates = list(
                search(
                    {
                        "gap": gap_id,
                        "query": f"{pack['name']} ({pack['symbol']}) {SEARCHABLE_GAPS[gap_id]}",
                    }
                )
            )
        except Exception:
            continue  # retry 0: the gap stays open and is reported as such
        for candidate in candidates:
            if not candidate.get("primary") or budget["fetches"] >= MAX_FETCHES:
                continue
            external["fetches"] += 1
            budget["fetches"] += 1
            try:
                summary = fetch(candidate["url"])
            except Exception:
                continue
            if (
                not isinstance(summary, str)
                or not summary.strip()
                or _private(summary)
                or _private(str(candidate.get("url")))
            ):
                continue
            index = 1 + sum(1 for item in evidence if item["id"].startswith("ext:"))
            evidence.append(
                {
                    "id": f"ext:{index}",
                    "kind": "external",
                    "label": f"외부{index}(1차 출처)",
                    "summary": summary[:MAX_SUMMARY_CHARS],
                    "url": str(candidate["url"]),
                    "published_on": candidate.get("published_on"),
                    "gap": gap_id,
                }
            )
            external["gaps_closed"].append(gap_id)
            external["gaps_open"].remove(gap_id)
            break
    return dict(pack, evidence=evidence, external=external)


# --- closed-output validation ----------------------------------------------------------


def _check_text(text) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_STATEMENT_CHARS:
        raise AnalysisValidationError("statement_text")
    if _NUMERIC_PROBABILITY.search(text) or _PERCENT_REACH.search(text):
        raise AnalysisValidationError("unmodelled_probability")
    if _private(text):
        raise AnalysisValidationError("privacy")
    scrubbed = text
    for label in _ALLOWED_ANALYSIS_NUMBER_LABELS:
        scrubbed = scrubbed.replace(label, "")
    if re.search(r"\d", scrubbed):
        raise AnalysisValidationError("free_text_number")
    if any(marker in text.lower() for marker in _ORDER_INSTRUCTIONS):
        raise AnalysisValidationError("order_instruction")
    return text.strip()


def _statement(
    entry,
    ids: set,
    *,
    minimum_ids: int = 1,
    extra_keys: frozenset = frozenset(),
    unknown_order_ids: frozenset = frozenset(),
) -> dict:
    if (
        not isinstance(entry, dict)
        or set(entry) != {"statement", "evidence_ids"} | extra_keys
    ):
        raise AnalysisValidationError("statement_shape")
    text, cited = _check_text(entry["statement"]), entry["evidence_ids"]
    if (
        not isinstance(cited, list)
        or any(not isinstance(item, str) for item in cited)
        or len(set(cited)) != len(cited)
        or len(cited) < minimum_ids
    ):
        raise AnalysisValidationError("unsupported_claim")
    if any(item not in ids for item in cited):
        raise AnalysisValidationError("unknown_evidence_id")
    if unknown_order_ids.intersection(cited) and any(
        marker in text.lower() for marker in _UNKNOWN_ORDER_INFERENCES
    ):
        raise AnalysisValidationError("conditional_side_inferred")
    return {"statement": text, "evidence_ids": list(cited)}


def _display_labels(pack: dict) -> dict:
    """Resolve units from supplied fields; relation percentages are already percent.

    No prose parsing, future target or new strategy calculation lives here.
    Quote time is deliberately independent of the completed metric session.
    """
    session, currency = pack["completed_session_date"], pack["currency"]
    labels = {}
    for item in (*pack["evidence"], *pack["gaps"]):
        key, kind = item["id"], item.get("kind")
        label, display, asof = (
            item["label"],
            "",
            item.get("asof", item.get("observed_on", session)),
        )
        price = lambda value: format_price(
            Decimal(value), item.get("currency", currency)
        )
        if kind == "price":
            display = price(item["value"])
            asof = (
                (item.get("observed_at") or "가격 관측시각 미기록")
                if key == "price"
                else item.get("date", session)
            )
        elif kind == "factor" and item.get("value") is not None:
            value = Decimal(item["value"])
            if key == "f:rsi_14":
                display = f"{value.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}점"
            elif key.startswith(("f:ret_", "f:close_to_sma_")) or key in (
                "f:realized_vol_63",
                "f:downside_vol_63",
                "f:max_drawdown",
                "f:price_position_200",
                "f:volume_change_20",
                "f:trading_amount_change_20",
            ):
                display = display_pct(
                    value,
                    signed=key
                    not in (
                        "f:realized_vol_63",
                        "f:downside_vol_63",
                        "f:price_position_200",
                    ),
                )
            elif key == "f:macd_histogram":
                display = price(item["value"])
            else:
                display = item["value"] + (
                    "배" if key == "f:risk_adjusted_momentum" else " (단위 미기록)"
                )
            label = {
                "f:realized_vol_63": "연환산 63일 변동성",
                "f:max_drawdown": "과거 최대낙폭",
            }.get(key, label)
        elif kind == "zone":
            display = f"{price(item['lower'])}~{price(item['upper'])} · 무효화 {price(item['invalidation'])} · 유효 {item['expiry_completed_sessions']}완료 세션"
        elif key == "rel:price_zone":
            # quote / upper - 1; this is NOT the upside to reach upper.
            label = "기록 가격의 구간 상단 대비"
            display = item["to_upper_pct"] + "%"
            quote = next(row for row in pack["evidence"] if row["id"] == "price")
            asof = f"구간 {session} / 가격 {quote.get('observed_at') or '가격 관측시각 미기록'}"
        elif key.startswith("rel:order:"):
            display = " · ".join(
                part
                for part in (
                    "발동가의 기록 가격 대비 " + item["trigger_vs_price_pct"] + "%"
                    if item.get("trigger_vs_price_pct") is not None
                    else "",
                    "발동가−기록 가격 " + price(item["trigger_minus_price"])
                    if item.get("trigger_minus_price") is not None
                    else "",
                    "주문가의 발동가 대비 " + item["order_vs_trigger_pct"] + "%"
                    if item.get("order_vs_trigger_pct") is not None
                    else "",
                )
                if part
            )
            quote = next((row for row in pack["evidence"] if row["id"] == "price"), {})
            asof = f"가격 {quote.get('observed_at') or '가격 관측시각 미기록'} / 주문 관측시각 미기록"
        elif kind == "conditional_order":
            label = "확인된 " + item["side"].upper() + " 주문"
            display = " · ".join(
                part
                for part in (
                    "발동 " + price(item["trigger_price"])
                    if item.get("trigger_price")
                    else "",
                    "주문 " + price(item["order_price"])
                    if item.get("order_price")
                    else "",
                    str(item["quantity"]) + "주",
                    "만료 " + str(item["expire_date"])
                    if item.get("expire_date")
                    else "",
                )
                if part
            )
            asof = "주문 관측시각 미기록"
        elif kind == "holding":
            label = "보유 현황"
            display = f"{item.get('quantity', '미기록')}주 · 평단 {item.get('avg_price', '미기록')}"
            asof = "보유 관측시각 미기록"
        elif kind == "standing":
            label, display = "이번 리포트의 비교 대상", f"{item['sample']}종목"
        labels[key] = {
            "label": label,
            "display": display,
            "asof": asof or "기준시각 미기록",
        }
    return labels


def validate_analysis(output, pack: dict) -> ContextualAnalysisV1:
    if not isinstance(output, dict) or set(output) != REQUIRED_KEYS:
        raise AnalysisValidationError("required_sections")
    if output["symbol"] != pack["symbol"]:
        raise AnalysisValidationError("symbol_mismatch")
    ids = {item["id"] for item in pack["evidence"]} | {
        gap["id"] for gap in pack["gaps"]
    }
    unknown_order_ids = frozenset(
        candidate
        for item in pack["evidence"]
        if item["kind"] == "conditional_order" and item.get("side") == "unknown"
        for candidate in (item["id"], "rel:" + item["id"])
    )
    sections = {}
    for name in LIST_SECTIONS:
        entries = output[name]
        minimum = 2 if name == "evidence_relations" else 1
        if (
            not isinstance(entries, list)
            or len(entries) > MAX_ITEMS
            or (not entries and name != "assumptions")
        ):
            raise AnalysisValidationError(f"section_{name}")
        sections[name] = tuple(
            _statement(
                entry, ids, minimum_ids=minimum, unknown_order_ids=unknown_order_ids
            )
            for entry in entries
        )
    summary = _check_text(output["summary"])
    if (
        unknown_order_ids
        and "조건주문" in summary
        and any(marker in summary.lower() for marker in _UNKNOWN_ORDER_INFERENCES)
    ):
        raise AnalysisValidationError("conditional_side_inferred")
    assessment = output["conditional_order_assessment"]
    assessed = None
    if assessment is not None:
        if (
            not isinstance(assessment, dict)
            or assessment.get("verdict") not in CONDITIONAL_VERDICTS
        ):
            raise AnalysisValidationError("conditional_shape")
        assessed = _statement(assessment, ids, extra_keys=frozenset({"verdict"}))
        cited = set(assessed["evidence_ids"])
        orders = {
            item["id"]
            for item in pack["evidence"]
            if item["kind"] == "conditional_order"
            and item.get("side") in ("buy", "sell")
        }
        support = {
            item["id"]
            for item in pack["evidence"]
            if item["kind"] in ("factor", "zone", "user_plan", "playbook_current")
        }
        if not orders:
            raise AnalysisValidationError("conditional_invented")
        if not cited.intersection(orders) or not cited.intersection(support):
            raise AnalysisValidationError("conditional_uncited")
        assessed = {"verdict": assessment["verdict"], **assessed}
    observable = {
        item["id"]
        for item in pack["evidence"]
        if item["kind"] in ("factor", "zone")
        and (item["kind"] == "zone" or item.get("value") is not None)
    }
    if "zone" in observable:
        observable.add("rel:price_zone")
    if not any(
        observable.intersection(entry["evidence_ids"])
        for entry in sections["change_conditions"]
    ):
        raise AnalysisValidationError("observable_reversal")
    confidence = output["confidence"]
    if (
        not isinstance(confidence, dict)
        or confidence.get("level") not in CONFIDENCE_LEVELS
    ):
        raise AnalysisValidationError("confidence_shape")
    reason = _statement(
        confidence,
        ids,
        extra_keys=frozenset({"level"}),
        unknown_order_ids=unknown_order_ids,
    )
    return ContextualAnalysisV1(
        pack["symbol"],
        summary,
        sections["evidence_relations"],
        assessed,
        sections["alternatives"],
        sections["counterevidence"],
        sections["assumptions"],
        sections["falsifiers"],
        sections["change_conditions"],
        {"level": confidence["level"], **reason},
        _display_labels(pack),
        2,
    )


def run_contextual_analysis(pack: dict, *, analysis=None) -> tuple:
    """(ContextualAnalysisV1 | None, reason). ``analysis`` None → OFF, zero calls."""
    if analysis is None:
        return None, "analysis_off"
    try:
        output = analysis(pack)
    except Exception as error:
        return None, f"model_failure:{type(error).__name__}"
    try:
        return validate_analysis(output, pack), "validated"
    except AnalysisValidationError as error:
        return None, f"invalid_output:{error.args[0]}"


# --- provider-neutral analyzer adapter (2026-09-04 vertical) ------------------------------
# Smallest adapter around an INJECTED transport callable. No provider name, auth,
# route selection, or router: the caller (tests, a later reviewed binding) supplies
# ``transport(request) -> str``. The adapter serializes only the already
# privacy-checked evidence pack, uses a fixed system/task contract, parses exactly
# one JSON object, and routes it through ``validate_analysis``. Raw provider output
# is never persisted or logged; failures reject only that card.

ANALYZER_REQUEST_SCHEMA_VERSION = 1
ANALYZER_MODEL_PURPOSE = "quant-contextual-decision-analysis-v1"
ANALYZER_SYSTEM_CONTRACT = (
    "너는 개인 투자 증거의 관계를 해석하는 분석 보조다. 입력은 종목별 evidence pack뿐이다. "
    "지표를 다시 나열하지 말고 최소 두 증거의 지지/충돌 관계를 설명하고, 조건주문·플레이북·"
    "보유상태와의 정합을 평가하며, 반대 근거·가정·반증조건·판단 변경조건·확신도와 데이터 공백을 "
    "밝힌다. 모든 문장은 evidence pack의 evidence id만 인용한다. 승인된 지표명 외 숫자를 "
    "자유서술에 넣지 않고, 계산하지 않은 도달확률은 금지하며 정성 표현만 쓴다. "
    "출력은 정확히 하나의 JSON 객체다."
)


def _citation_map(pack: dict) -> dict:
    ids = [item["id"] for section in ("evidence", "gaps") for item in pack[section]]
    if any(not isinstance(item, str) for item in ids) or len(set(ids)) != len(ids):
        raise ValueError("ambiguous_evidence_ids")
    width = max(2, len(str(len(ids))))
    mapping = {key: f"E{index:0{width}d}" for index, key in enumerate(sorted(ids), 1)}
    if set(mapping).intersection(mapping.values()):
        raise ValueError("ambiguous_evidence_ids")
    return mapping


ALARM_WRITING_STYLE = """
[톡모 알람용 경량 한국어 윤문]
사용자에게 보여주는 제목과 설명에만 적용한다. 기존 분석·발송 기준과 출력 형식은 유지하며, 내용을 바꾸지 않고 읽기 쉽게 쓴다. 각 경로의 필수 항목·길이·수치 표시·인용·개인정보 규칙이 우선이며, 새 필드나 별도 윤문 호출을 추가하지 않는다.
- 친근한 “-어/-했어”체를 쓴다. 명사 나열과 보고서식 표현은 쉬운 동사로 풀고, 여러 뜻이 겹친 긴 문장은 자연스럽게 나눈다. 이미 명확한 문장은 억지로 고치지 않는다.
- 핵심 판단이나 중요한 변화를 먼저 말한다. 근거와 내 투자에 주는 의미가 이어지게 쓰되, 모든 알람을 같은 문단 수나 순서에 억지로 맞추지 않는다.
- 제목은 대상과 핵심 변화가 드러나게 쓴다. 막연한 관심 유도보다 무엇이 달라졌는지로 읽을 이유를 만든다. 본문보다 강하게 단정하거나 과장·공포·매수 기대를 덧붙이지 않는다. 원문 기사 제목을 직접 인용할 때는 바꾸지 않는다.
- 같은 결론과 근거를 여러 문단에서 반복하지 않는다. 불필요한 예고와 상투적인 맺음말은 덜어낸다. 다만 서로 다른 이유나 중요한 조건까지 중복으로 보고 지우지는 않는다.
- 확인된 사실과 그에 대한 해석을 구분한다. 수치, 날짜, 고유명사, 직접 인용, 출처와 판단의 의미를 보존한다. 보도를 확정 사실로, 가능성을 확신으로, 주문 의향이나 제출을 체결로 바꾸지 않는다.
- 후속 알람은 앞서 알린 내용에서 무엇이 달라졌고 판단에 어떤 차이가 생겼는지 선명하게 쓴다. 뒤늦게 확인한 과거 자료를 방금 발생한 변화처럼 표현하지 않는다. 매매 의견이 그대로여도 위험이나 기회에 대한 평가가 달라졌다면 그 차이를 설명한다.
- 확인된 내용과 의미를 먼저 설명하고, 결론을 제한하는 불확실성은 해당 문장 가까이에 붙인다. “확인되지 않았어”를 반복하거나 막연히 “지켜봐야 해”로 끝내지 말고, 주어진 다음 확인 조건을 구체적으로 풀어 쓴다. 없는 근거나 조건은 만들지 않는다.
- 필드 안의 문장은 평문으로 쓴다. 강조용 마크다운이나 장식용 제목·목록 기호를 추가하지 않는다. 제목과 한줄 요약은 한 줄로 쓰고, 메시지의 문단·빈 줄·강조는 기존 표시 프로그램에 맡긴다. 직접 인용·고유명사·코드·식별자에 원래 있는 기호는 그대로 보존한다. 가운뎃점·줄표·긴 괄호를 과하게 쓰지 않는다. 기존 필수 항목, 출처 표기, JSON 구조와 식별자는 바꾸지 않는다.
출력 전에 의미가 달라지거나 중요한 조건이 빠지지 않았는지 짧게 점검한다. 윤문 과정·평가 등급은 알람에 표시하지 않고 완성된 내용만 출력한다.
"""


def build_analyzer_request(pack: dict) -> dict:
    """Deterministic, provider-neutral request over the pack alone.

    Keys are fixed: schema/version identity, model-purpose, the fixed system
    contract, and the task contract (JSON-only output) carrying the exact
    structural contract ``validate_analysis`` enforces — one deterministic
    JSON-safe mapping derived from the module constants and this pack's ids
    (no second validator, schema, store or router). No account/order ids,
    paths, credentials, raw provider payloads, or cohort data are added — the
    pack is already the privacy-bounded projection.
    """
    mapping = _citation_map(pack)
    allowed_ids = list(mapping.values())
    sections = {
        name: {
            "statement_keys": ["statement", "evidence_ids"],
            "min_items": 1,
            "max_items": MAX_ITEMS,
            "may_be_empty": name == "assumptions",
        }
        for name in LIST_SECTIONS
    }
    order_ids = [
        item["id"] for item in pack["evidence"] if item["kind"] == "conditional_order"
    ]
    relation_order_ids = [
        item["id"]
        for item in pack["evidence"]
        if item["kind"] == "relation" and item["id"].startswith("rel:order:")
    ]
    conditional_semantics = {
        "order_evidence_present": bool(order_ids),
        "default": None,
        "must_cite_at_least_one_of": [mapping[item] for item in order_ids],
        "allowed_verdicts": list(CONDITIONAL_VERDICTS),
    }
    wire_pack = deepcopy(pack)
    for section in ("evidence", "gaps"):
        for item in wire_pack[section]:
            item["id"] = mapping[item["id"]]
            if item.get("kind") == "external" and "gap" in item:
                item["gap"] = mapping[item["gap"]]
    for key in ("gaps_open", "gaps_closed"):
        if key in wire_pack.get("external", {}):
            wire_pack["external"][key] = [
                mapping[item] for item in wire_pack["external"][key]
            ]
    return {
        "schema_version": ANALYZER_REQUEST_SCHEMA_VERSION,
        "model_purpose": ANALYZER_MODEL_PURPOSE,
        "system": ANALYZER_SYSTEM_CONTRACT + ALARM_WRITING_STYLE,
        "task": {
            "instruction": "evidence pack을 해석하고 출력 계약에 맞는 하나의 JSON 객체로 답하라.",
            "output_contract": {
                "type": "json_object",
                "required_keys": sorted(REQUIRED_KEYS),
                "no_extra_keys": True,
                "symbol": pack["symbol"],
                "symbol_must_equal_pack": True,
                "sections": sections,
                "conditional_order_assessment": {
                    "statement_keys": ["verdict", "statement", "evidence_ids"],
                    "nullable": True,
                    "verdict_enum": list(CONDITIONAL_VERDICTS),
                },
                "confidence": {
                    "statement_keys": ["level", "statement", "evidence_ids"],
                    "level_enum": list(CONFIDENCE_LEVELS),
                },
                "citations": {
                    "must_cite_pack_ids": True,
                    "allowed_ids": allowed_ids,
                    "elements_are_strings": True,
                    "unique": True,
                    "min_per_statement": 1,
                    "min_per_relation": 2,
                },
                "statement_text": {
                    "non_empty": True,
                    "max_chars": MAX_STATEMENT_CHARS,
                    "no_numeric_probability": True,
                    "no_percent_reach": True,
                    "no_digits_except_labels": list(_ALLOWED_ANALYSIS_NUMBER_LABELS),
                    "no_private_markers": True,
                    "no_order_instructions": list(_ORDER_INSTRUCTIONS),
                },
                "statement_text_max_chars": MAX_STATEMENT_CHARS,
                "conditional_semantics": conditional_semantics,
            },
        },
        "evidence_pack": wire_pack,
    }


def analyzer_adapter(transport):
    """Wrap ``transport(request) -> str`` into the controller's ``analysis(pack)``
    seam. One call per invocation (the controller bounds one per admissible
    card); any transport exception, non-JSON, non-object, or schema/citation
    failure propagates as the seam's existing rejection reasons."""

    def analysis(pack: dict):
        reverse = {value: key for key, value in _citation_map(pack).items()}
        prefix = "!unknown:"
        while any(key.startswith(prefix) for key in reverse.values()):
            prefix = "!" + prefix
        raw = transport(build_analyzer_request(pack))
        output = json.loads(raw)
        if not isinstance(output, dict):
            raise ValueError("output_not_object")
        for name in (*LIST_SECTIONS, "conditional_order_assessment", "confidence"):
            entries = output.get(name)
            entries = entries if name in LIST_SECTIONS else [entries]
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and isinstance(
                    entry.get("evidence_ids"), list
                ):
                    entry["evidence_ids"] = [
                        reverse.get(item, prefix + item)
                        if isinstance(item, str)
                        else item
                        for item in entry["evidence_ids"]
                    ]
        return output

    return analysis
