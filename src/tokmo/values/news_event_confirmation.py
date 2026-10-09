"""Closed confirmation/event-type vocabularies and the deterministic
confirmation derivation (SDS 2026-09-01 §D.1–§D.3/§E.1).

Decisions 7·8·9: the four confirmation states are preserved without
collapse; ``OFFICIAL_CONFIRMED`` exists ONLY through a deterministic
official primary-evidence ref (never a model claim, never repeated media
coverage); the model contributes bounded claims that the derivation applies
in the downward direction only.
"""

import re
import unicodedata
from dataclasses import dataclass

CONFIRMATION_STATES_V1 = frozenset(
    {
        "OFFICIAL_CONFIRMED",  # §D.3 official primary-evidence ref only
        "MULTI_MEDIA_UNCONFIRMED",  # FACT-role independent sources >= 2, no ref
        "SINGLE_MEDIA_UNCONFIRMED",  # exactly one FACT-role source
        "ANALYSIS_OPINION",  # OPINION-role dominant / no factual source
    }
)

CONFIRMATION_LABELS_KO = {
    "OFFICIAL_CONFIRMED": "공식 확인(공시·1차 출처)",
    "MULTI_MEDIA_UNCONFIRMED": "복수 언론 보도, 공식 확인 전",
    "SINGLE_MEDIA_UNCONFIRMED": "단일 언론 보도, 미확인",
    "ANALYSIS_OPINION": "분석·의견·전망",
}

EVENT_TYPES_V1 = frozenset(
    {
        "CORPORATE_ACTION_DECIDED",  # 결의·결정·체결된 자본/지배구조 행위
        "CORPORATE_ACTION_PROPOSED",  # 검토·추진·계획 보도
        "EARNINGS_GUIDANCE",  # 실적/가이던스 발표·쇼크
        "DIVIDEND_ROUTINE",  # 정기·예상부합 배당
        "DIVIDEND_CHANGE",  # 배당 정책/금액 변경
        "MACRO_POLICY_EMERGENCY",  # 중앙은행·정부 긴급조치
        "WAR_MARKET_DISRUPTION",  # 전쟁·시장 폐쇄·대형 신용사건
        "CONSUMER_MILESTONE",  # 앱 회원·가입자·다운로드류 지표
        "ANALYST_OPINION",  # 애널리스트 의견·목표가·여력 추측
        "RUMOR_SINGLE_CLAIM",  # 미확인 단독 주장
        "OTHER",
    }
)

SURPRISE_CLAIMS_V1 = frozenset({"ROUTINE", "SURPRISE", "UNKNOWN"})

# Relevance closed vocabulary (A1 §Q.2, decisions 13·14): the ONLY axis a
# model claim may promote — and only with citations resolving into the
# supplied public evidence keys.
RELEVANCE_CLAIMS_V1 = frozenset({"NONE", "INDIRECT_MATERIAL", "THEMATIC_MATERIAL"})

# Official-report-name substring patterns (§D.3): applied to the OpenDART
# ``report_nm`` form name ONLY — never to article titles.
CORP_ACTION_PATTERNS_V1 = {
    "BUYBACK_RETIREMENT": ("자기주식소각", "주식소각"),
    "BUYBACK": ("자기주식취득",),
    "DIVIDEND_DECISION": ("현금ㆍ현물배당결정", "배당결정"),
    "CAPITAL_INCREASE": ("유상증자결정", "무상증자결정"),
    "MERGER_ACQUISITION": ("영업양수도", "합병", "주식양수도"),
}

# Article↔disclosure correlation (A2 §Z3, decision 18): event types that
# may promote at all, and the action families each admits. Every other
# event type maps to the empty set — no official promotion, no query.
EVENT_ACTION_FAMILIES_V1 = {
    "CORPORATE_ACTION_DECIDED": frozenset(
        {
            "BUYBACK_RETIREMENT",
            "BUYBACK",
            "DIVIDEND_DECISION",
            "CAPITAL_INCREASE",
            "MERGER_ACQUISITION",
        }
    ),
    "DIVIDEND_CHANGE": frozenset({"DIVIDEND_DECISION"}),
    "DIVIDEND_ROUTINE": frozenset({"DIVIDEND_DECISION"}),
}

# Article action anchors (§Z3-2): closed headline-token vocabulary applied
# to STORED titles only — deterministic, never model output. Korean tokens
# attach at a token head (소각 matches 소각결의, never 대소각).
ARTICLE_ACTION_TOKENS_V1 = {
    "BUYBACK_RETIREMENT": ("소각",),
    "BUYBACK": ("자사주", "자기주식"),
    "DIVIDEND_DECISION": ("배당",),
    "CAPITAL_INCREASE": ("증자",),
    "MERGER_ACQUISITION": ("합병", "인수", "양수도"),
}

_ACTION_TOKEN = re.compile(r"[0-9a-z]+|[가-힣]+")


def _action_tokens(text):
    return _ACTION_TOKEN.findall(unicodedata.normalize("NFKC", str(text)).casefold())


def derive_article_action_families(headlines):
    """Deterministic action families observed in the stored headline
    titles (§Z3-2): a family qualifies when any of its anchor tokens
    matches a headline token exactly or at a Korean token head."""
    families = set()
    tokens = [token for headline in headlines for token in _action_tokens(headline)]
    for family, anchors in ARTICLE_ACTION_TOKENS_V1.items():
        for anchor in anchors:
            if any(token == anchor or token.startswith(anchor) for token in tokens):
                families.add(family)
                break
    return frozenset(families)


@dataclass(frozen=True)
class OfficialEvidenceRefV1:
    """Deterministic official primary-evidence ref — code-owned; a model
    output can never construct one (§D.3)."""

    source: str  # "opendart" (v1 only)
    ref_id: str  # rcept_no (14 digits)
    report_name: str  # report_nm as stored
    stock_code: str  # 6 digits
    receipt_date: str  # YYYY-MM-DD
    matched_action: str  # CORP_ACTION_PATTERNS_V1 key


_CONFIRMATION_RANK = {
    "ANALYSIS_OPINION": 0,
    "SINGLE_MEDIA_UNCONFIRMED": 1,
    "MULTI_MEDIA_UNCONFIRMED": 2,
    "OFFICIAL_CONFIRMED": 3,
}


def derive_relevance(direct_holding, claim, citations, evidence):
    """Pure relevance derivation (§Q.3): DIRECT is identity-owned; a model
    claim promotes only when in-vocabulary AND cited with keys that resolve
    into the public evidence; everything else is NONE. Never an input to
    the event class or immediate eligibility."""
    if direct_holding:
        return "DIRECT"
    cited = tuple(citations or ())
    if (
        claim in ("INDIRECT_MATERIAL", "THEMATIC_MATERIAL")
        and cited
        and set(cited) <= set(evidence)
    ):
        return claim
    return "NONE"


def derive_confirmation(official_ref, fact_n, opinion_n, model_claim):
    """Pure confirmation-state derivation (§E.1).

    The official ref is the ONLY promotion path; the FACT-role independent
    source count sets the base; a valid model claim applies downward only —
    an OFFICIAL claim without a ref is ignored (거짓수리 2·4)."""
    if official_ref is not None:
        return "OFFICIAL_CONFIRMED"
    if fact_n >= 2:
        base = "MULTI_MEDIA_UNCONFIRMED"
    elif fact_n == 1:
        base = "SINGLE_MEDIA_UNCONFIRMED"
    else:
        base = "ANALYSIS_OPINION"
    if (
        model_claim in _CONFIRMATION_RANK
        and _CONFIRMATION_RANK[model_claim] < _CONFIRMATION_RANK[base]
    ):
        return model_claim
    return base
