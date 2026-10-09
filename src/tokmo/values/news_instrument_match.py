"""Exact corporate identity and bounded ETF token-coverage matching.

SDS 2026-09-01 A1 §O/§P as repaired by A3 §V6 (decisions 11·12):

- Operating companies match exactly — registered identity/alias or the
  normalized full official legal name. No fuzzy arithmetic of any kind;
  group prefixes and partial name tokens can never establish a company.
- ETF/product names (first normalized token in the closed brand set) match
  through a deterministic brand-anchored span extracted from the headline,
  scored by reference-side weighted token coverage ≥ 0.8 with numeric
  preservation and a signal floor; a match is admissible only when exactly
  one instrument in the supplied universe satisfies it (fail-closed
  ``ETF_MATCH_AMBIGUOUS`` otherwise).

Pure values layer: closed tables + exact token equality only — no
Levenshtein, no raw substring scans, no imports above the values layer.
"""

import re
import unicodedata
from dataclasses import dataclass

ETF_BRAND_TOKENS_V1 = frozenset(
    {
        "sol",
        "kodex",
        "tiger",
        "ace",
        "rise",
        "plus",
        "hanaro",
        "kosef",
        "arirang",
        "kiwoom",
    }
)
ETF_GENERIC_TOKENS_V1 = frozenset({"etf", "tr", "액티브", "합성", "top"})
ETF_BRAND_TRANSLITERATIONS_V1 = {
    "솔": "sol",
    "코덱스": "kodex",
    "타이거": "tiger",
    "에이스": "ace",
    "키움": "kiwoom",
}
COMPANY_LEGAL_SUFFIXES_V1 = ("주식회사", "(주)", "㈜", "co.,ltd", "inc", "corp")
ETF_REFERENCE_MAX_TOKENS = 6

# Mirrors the discovery tokenizer's particle strip (values must stay a
# leaf layer, so the closed tuple is restated here; parity is test-pinned).
_PARTICLES = (
    "으로부터",
    "에서는",
    "에게서",
    "으로",
    "에서",
    "에게",
    "까지",
    "부터",
    "처럼",
    "보다",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "의",
    "에",
    "와",
    "과",
    "도",
    "로",
)
# Script boundaries AND letter↔digit boundaries split (§V6 N1): "top2" →
# ["top", "2"], "ai반도체" → ["ai", "반도체"].
_TOKEN = re.compile(r"[a-z]+|[0-9]+|[가-힣]+")


def _strip_particle(token):
    for suffix in _PARTICLES:
        if token.endswith(suffix) and len(token) > len(suffix) + 1:
            return token[: -len(suffix)]
    return token


def _tokens(text):
    normalized = unicodedata.normalize("NFKC", str(text)).casefold()
    return [_strip_particle(token) for token in _TOKEN.findall(normalized)]


def _canonical(token):
    return ETF_BRAND_TRANSLITERATIONS_V1.get(token, token)


def normalize_company_name(text):
    """NFKC casefold, whitespace collapse, closed legal-suffix strip."""
    normalized = " ".join(unicodedata.normalize("NFKC", str(text)).casefold().split())
    for suffix in COMPANY_LEGAL_SUFFIXES_V1:
        token = suffix.casefold()
        if normalized.startswith(token):
            normalized = normalized[len(token) :].strip()
        if normalized.endswith(token):
            normalized = normalized[: -len(token)].strip()
    return normalized


def company_name_exact_match(text, official_name):
    """Whole-name equality only (§O.1-2) — never partial, never fuzzy."""
    left = normalize_company_name(text)
    return bool(left) and left == normalize_company_name(official_name)


def is_etf_name(official_name):
    """§P.1 gate: the official name's first normalized token is a brand."""
    tokens = _tokens(official_name)
    return bool(tokens) and _canonical(tokens[0]) in ETF_BRAND_TOKENS_V1


@dataclass(frozen=True)
class EtfMatchOutcomeV1:
    matched: bool
    coverage: float
    reason: str


def _rejected(reason, coverage=0.0):
    return EtfMatchOutcomeV1(False, coverage, reason)


def match_etf_reference(headline, official_name):
    """Single-pair §P.4/§V6 arithmetic over the brand-anchored span.

    Span: from the first brand/transliteration token, up to
    ``ETF_REFERENCE_MAX_TOKENS`` consecutive tokens; a token enters the
    span only if it resolves to an official-name token, is GENERIC, or is
    numeric — anything else ends the span (tokens outside the span never
    touch the arithmetic). Weights: DISTINCTIVE/NUMERIC 2, BRAND 1,
    GENERIC 0; tokens are deduplicated."""
    if not is_etf_name(official_name):
        return _rejected("NOT_ETF_NAME")
    official = {_canonical(token) for token in _tokens(official_name)}
    official_numbers = {token for token in official if token.isdigit()}
    reference = [_canonical(token) for token in _tokens(headline)]
    anchor = next(
        (
            index
            for index, token in enumerate(reference)
            if token in ETF_BRAND_TOKENS_V1
        ),
        None,
    )
    if anchor is None:
        return _rejected("NO_BRAND_ANCHOR")
    span = []
    for token in reference[anchor : anchor + ETF_REFERENCE_MAX_TOKENS]:
        if not (token in official or token in ETF_GENERIC_TOKENS_V1 or token.isdigit()):
            break
        if token not in span:  # deduplicated (JA-17)
            span.append(token)
    scored = [token for token in span if token not in ETF_GENERIC_TOKENS_V1]

    def weight(token):
        return 1 if token in ETF_BRAND_TOKENS_V1 else 2

    total = sum(weight(token) for token in scored)
    matched = [token for token in scored if token in official]
    coverage = (sum(weight(token) for token in matched) / total) if total else 0.0
    # P.4-3 numeric preservation: every reference numeric must equal an
    # official numeric.
    if any(token.isdigit() and token not in official_numbers for token in scored):
        return _rejected("NUMERIC_MISMATCH", coverage)
    if coverage < 0.8:
        return _rejected("COVERAGE_BELOW_THRESHOLD", coverage)
    # P.4-2 signal floor: ≥2 resolved non-generic tokens, ≥1 of them
    # DISTINCTIVE or NUMERIC (brand alone or brand+generic never matches).
    if len(matched) < 2 or all(token in ETF_BRAND_TOKENS_V1 for token in matched):
        return _rejected("SIGNAL_FLOOR", coverage)
    return EtfMatchOutcomeV1(True, coverage, "MATCHED")


def resolve_etf_reference(headline, universe):
    """§P.4-4 uniqueness over ``{identity_key: official_name}``: exactly
    one satisfying instrument admits; two or more fail closed."""
    hits = sorted(
        {
            key
            for key, official_name in dict(universe).items()
            if match_etf_reference(headline, official_name).matched
        }
    )
    if not hits:
        return None, "NO_MATCH"
    if len(hits) > 1:
        return None, "ETF_MATCH_AMBIGUOUS"
    return hits[0], "MATCHED"
