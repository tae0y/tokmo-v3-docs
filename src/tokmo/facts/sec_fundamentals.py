"""SEC companyfacts fundamental parser (G1A).

Parses the official SEC companyfacts JSON response into immutable
OfficialFundamentalMaterialV1 objects. Implements:

- Exact versioned concept registry for the 6 canonical metrics.
- Annual FY 10-K filtering only (quarterly facts are ignored).
- Ambiguity detection: multiple concepts for the same metric in the same
  fiscal period → metric AMBIGUOUS (carried as typed evidence, not dropped).
- Amendment/revision resolution: 10-K/A with strictly-later filed date
  supersedes the original 10-K; the original accession becomes revision_of.
  Ordinary 10-K comparison filings (different fy, same value) never produce
  revision_of — only explicit 10-K/A or uniquely proven amendment chain.
- Date-only available-at policy seam (resolver injected, not hardcoded here).

Envelope semantics (B1 fix): companyfacts ``fy``/``fp`` describe the *filing's*
fiscal period, not the value's coverage period. A single FY2025 10-K carries
the current year AND comparison years, all tagged ``fy=2025, fp=FY``. We group
annual values by ``(start, end)`` period identity, not filing ``fy``.

No external API calls. No retry, no redirect follow. No arithmetic
reconstruction, no currency conversion.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from tokmo.facts.eia import LIVE_TRANSPORT_FAILURE_CODES, LiveAttemptOutcome
from tokmo.facts.sec import REQUEST_TIMEOUT_SECONDS as SEC_TIMEOUT
from tokmo.values.fundamental_materials import (
    AccountingRegime,
    DATE_ONLY_POLICY_VERSION,
    DATE_ONLY_SESSION_HISTORY_SOURCE,
    AvailableAtResolution,
    MetricEvidence,
    MetricSelection,
    OfficialFundamentalMaterialV1,
    SEC_FUNDAMENTAL_SOURCE,
    StatementScope,
)

SOURCE = SEC_FUNDAMENTAL_SOURCE
COMPANYFACTS_ENDPOINT_ID = "sec_companyfacts_fundamentals"
COMPANYFACTS_HOST = "data.sec.gov"
COMPANYFACTS_PATH = "/api/xbrl/companyfacts/CIK##########.json"
COMPANYFACTS_REQUEST_TIMEOUT_SECONDS = SEC_TIMEOUT  # 10.0

# Canary measured Apple companyfacts body = 3,789,099 bytes (3.79 MB).
# Reviewed bounded headroom: 5 MB accommodates larger multi-concept entities
# without unbounded risk. Identity content-encoding only.
MAX_COMPANYFACTS_BODY_BYTES = 5 * 1024 * 1024

TAXONOMY_NAMESPACE = "us-gaap"
# m1 fix: the companyfacts endpoint serves XBRL facts under the us-gaap
# namespace but the body does NOT carry a per-fact taxonomy version stamp.
# Stamping a hardcoded year ("2024") is a false taxonomy vintage claim,
# and putting the endpoint identity in the version field is also false.
# The truthful value is "unknown" — the companyfacts JSON does not expose
# which taxonomy version a given fact was tagged with. The namespace +
# mapping_version still bind the contract identity correctly.
TAXONOMY_VERSION = "unknown"
MAPPING_VERSION = "v1"

# Exact versioned concept registry: metric -> ordered candidate concepts.
CONCEPT_REGISTRY: dict[str, tuple[str, ...]] = {
    "revenue_ttm": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
    ),
    "net_income_ttm": ("NetIncomeLoss",),
    "gross_profit_ttm": ("GrossProfit",),
    "total_equity": ("StockholdersEquity",),
    "total_assets": ("Assets",),
    "diluted_shares_outstanding": (
        "WeightedAverageNumberOfDilutedSharesOutstanding",
    ),
}

# Alternative concepts that may co-occur with primary -> ambiguity check.
AMBIGUITY_CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue_ttm": (
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
    ),
}

METRIC_UNITS: dict[str, str] = {
    "revenue_ttm": "USD",
    "net_income_ttm": "USD",
    "gross_profit_ttm": "USD",
    "total_equity": "USD",
    "total_assets": "USD",
    "diluted_shares_outstanding": "shares",
}

_ALL_METRIC_NAMES = tuple(CONCEPT_REGISTRY.keys())

# C1: bounded real fiscal-year length between adjacent period ends.
# 364 = 52-week fiscal year; 365 = non-leap calendar; 366 = leap calendar;
# 371 = 53-week fiscal year. The 1-day slack on each side (363..372) tolerates
# off-by-one edge dates while rejecting semiannual (~180d) and 18-month (~550d).
_MIN_FISCAL_YEAR_DAYS = 363
_MAX_FISCAL_YEAR_DAYS = 372


@dataclass(frozen=True)
class SECCompanyFactsRequest:
    endpoint_id: str
    cik: str
    timeout_seconds: float
    user_agent: str = field(repr=False)


@dataclass(frozen=True)
class SECCompanyFactsTransportSuccess:
    body: bytes = field(repr=False)
    status: int = 200


@dataclass(frozen=True)
class SECCompanyFactsTransportFailure:
    code: str
    retry_after: int | None = None

    def __post_init__(self) -> None:
        if self.code not in LIVE_TRANSPORT_FAILURE_CODES:
            raise ValueError("live_transport_code_outside_contract")
        if self.retry_after is not None and self.code != "http_429":
            raise ValueError("retry_after_requires_http_429")


@dataclass(frozen=True)
class SECCompanyFactsResult:
    materials: tuple[OfficialFundamentalMaterialV1, ...] | None
    unavailable_reason: str | None
    attempt: LiveAttemptOutcome | None
    raw_body_sha256: str | None


SecTransport = Callable[
    [SECCompanyFactsRequest], "SECCompanyFactsTransportSuccess | SECCompanyFactsTransportFailure"
]
Clock = Callable[[], datetime]
AvailableAtResolver = Callable[[str], "AvailableAtResolution"]

_FAILURE_REASONS = {
    "timeout": "transport_error",
    "connection_failure": "transport_error",
    "http_5xx": "transport_error",
    "http_429": "rate_limited",
    "auth_rejection": "contract_mismatch",
    "bad_request": "contract_mismatch",
    "redirect": "transport_error",
    "body_too_large": "transport_error",
    "unsupported_content_encoding": "transport_error",
    "protocol_failure": "transport_error",
}


def build_companyfacts_request(*, cik: str, user_agent: str) -> SECCompanyFactsRequest:
    return SECCompanyFactsRequest(
        endpoint_id=COMPANYFACTS_ENDPOINT_ID,
        cik=cik,
        timeout_seconds=COMPANYFACTS_REQUEST_TIMEOUT_SECONDS,
        user_agent=user_agent,
    )


# -- Internal data carrier for one extracted annual fact --


@dataclass(frozen=True)
class _AnnualFactData:
    """One annual fact with provenance, keyed by period (start, end)."""

    value: int
    start: str | None
    end: str
    accn: str
    filed: str
    form: str
    concept_id: str
    fy: int
    frame: str | None = None
    predecessor_accn: str | None = None


def parse_companyfacts_response(
    body: bytes,
    *,
    cik: str,
    canonical_symbol: str,
    observed_at: datetime,
    available_at_resolver: AvailableAtResolver,
    market: str,
    statement_scope: StatementScope,
    as_of: datetime,
) -> SECCompanyFactsResult:
    """Parse SEC companyfacts JSON -> OfficialFundamentalMaterialV1 list.

    ``market`` and ``statement_scope`` must be verified by the caller from
    official inputs; this parser does not guess them.

    ``as_of`` is the as-of datetime; the parser enforces
    ``available_at <= as_of`` for every material produced.
    """
    raw_body_sha256 = hashlib.sha256(body).hexdigest()

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _unavailable("parse_error", raw_body_sha256)

    if not isinstance(payload, dict):
        return _unavailable("contract_mismatch", raw_body_sha256)

    facts = payload.get("facts")
    if not isinstance(facts, dict):
        return _unavailable("contract_mismatch", raw_body_sha256)

    us_gaap = facts.get(TAXONOMY_NAMESPACE)
    if not isinstance(us_gaap, dict):
        return _unavailable("contract_mismatch", raw_body_sha256)

    entity_name = payload.get("entityName")
    if not isinstance(entity_name, str) or not entity_name:
        return _unavailable("contract_mismatch", raw_body_sha256)

    # Extract metrics grouped by fiscal period end date, not filing fy.
    # M1 fix: primary and alternative facts are preserved separately with
    # full provenance; ambiguity is evaluated at accession scope downstream.
    primary_facts, alternative_facts = _extract_annual_metrics(us_gaap)
    if not primary_facts:
        return _unavailable("no_published_data", raw_body_sha256)

    # Determine available fiscal periods from the data (keyed by end date).
    all_ends: set[str] = set()
    for metric_ends in primary_facts.values():
        for end_key in metric_ends:
            all_ends.add(end_key)

    if not all_ends:
        return _unavailable("no_published_data", raw_body_sha256)

    # Sort periods by end date descending to find current and prior.
    sorted_ends = sorted(all_ends, reverse=True)

    source_request_identity_digest = hashlib.sha256(
        f"sec_companyfacts:{cik}".encode("ascii")
    ).hexdigest()

    # M2 fix: SDS §5.2 — current is the "latest admissible annual revision"
    # where available_at <= as_of. We evaluate candidate period ends
    # newest→oldest under the typed resolver and select the newest whose
    # available_at <= as_of. A future latest filing must not erase older
    # PIT material: if the newest period is inadmissible, we fall back to
    # the next-oldest period instead of returning no_published_data.
    #
    # The prior is then only the exact adjacent prior of the selected
    # current (not the raw sorted_ends[1] which may belong to an
    # inadmissible newer period).
    current_end: str | None = None
    for candidate_end in sorted_ends:
        try:
            candidate_material = _build_material(
                primary_facts=primary_facts,
                alternative_facts=alternative_facts,
                period_end_key=candidate_end,
                cik=cik,
                canonical_symbol=canonical_symbol,
                observed_at=observed_at,
                available_at_resolver=available_at_resolver,
                raw_body_sha256=raw_body_sha256,
                source_request_identity_digest=source_request_identity_digest,
                market=market,
                statement_scope=statement_scope,
                as_of=as_of,
            )
        except _MissingFiledDateError:
            # Missing/conflicting/malformed — try next candidate.
            continue
        # First admissible candidate (newest→oldest) is the current.
        current_end = candidate_end
        current_material = candidate_material
        break

    if current_end is None:
        # No period end produced an admissible material.
        return _unavailable("no_published_data", raw_body_sha256)

    materials = [current_material]

    # Prior: the exact adjacent prior of the SELECTED current period.
    # Walk sorted_ends from the current_end position downward to find the
    # first period that is consecutive (363..372 days) with current_end.
    current_end_date: date | None
    try:
        current_end_date = date.fromisoformat(current_end)
    except ValueError:
        current_end_date = None

    if current_end_date is not None:
        # Find candidate priors: period ends that come before current_end.
        prior_candidates = [
            e for e in sorted_ends
            if e < current_end
        ]
        for prior_end in prior_candidates:
            try:
                prior_end_date = date.fromisoformat(prior_end)
            except ValueError:
                continue
            days_diff = (current_end_date - prior_end_date).days
            if not (_MIN_FISCAL_YEAR_DAYS <= days_diff <= _MAX_FISCAL_YEAR_DAYS):
                continue
            # Found the adjacent prior. Build it.
            try:
                prior_material = _build_material(
                    primary_facts=primary_facts,
                    alternative_facts=alternative_facts,
                    period_end_key=prior_end,
                    cik=cik,
                    canonical_symbol=canonical_symbol,
                    observed_at=observed_at,
                    available_at_resolver=available_at_resolver,
                    raw_body_sha256=raw_body_sha256,
                    source_request_identity_digest=source_request_identity_digest,
                    market=market,
                    statement_scope=statement_scope,
                    as_of=as_of,
                )
            except _MissingFiledDateError:
                prior_material = None
            if prior_material is not None:
                materials.append(prior_material)
            break  # only the first consecutive prior

    return SECCompanyFactsResult(
        materials=tuple(materials),
        unavailable_reason=None,
        attempt=None,
        raw_body_sha256=raw_body_sha256,
    )


def _unavailable(reason: str, raw_body_sha256: str) -> SECCompanyFactsResult:
    return SECCompanyFactsResult(
        materials=None,
        unavailable_reason=reason,
        attempt=None,
        raw_body_sha256=raw_body_sha256,
    )


def _extract_annual_metrics(
    us_gaap: dict,
) -> tuple[
    dict[str, dict[str, list[_AnnualFactData]]],
    dict[str, dict[str, list[_AnnualFactData]]],
]:
    """Extract annual FY 10-K facts for each canonical metric.

    Groups facts by fiscal period END date — NOT by filing ``fy``.
    See module docstring for envelope semantics rationale.

    Returns a tuple of:
    1. primary_facts: metric -> {end_date -> [primary concept facts]}.
       ALL primary facts are preserved so the caller can resolve
       admissibility newest→oldest by filed date.
    2. alternative_facts: metric -> {end_date -> [alternative concept facts]}.
       ALL alternative (ambiguity) concept facts are preserved so the
       caller can evaluate ambiguity at the exact accession scope.

    M1 fix (reclosure): ambiguity is NOT pre-computed globally. Instead,
    primary and alternative facts are preserved separately with full
    provenance (accession, filed, form). ``_build_material_from_accn``
    evaluates ambiguity per target accession: a primary fact is AMBIGUOUS
    only when the SAME accession also has an alternative concept fact for
    the exact same (start, end). A future amendment's alternative concept
    cannot retroactively contaminate an earlier accession's primary fact.
    """
    primary_result: dict[str, dict[str, list[_AnnualFactData]]] = {}
    alternative_result: dict[str, dict[str, list[_AnnualFactData]]] = {}

    for metric_name in _ALL_METRIC_NAMES:
        primary_concepts = CONCEPT_REGISTRY[metric_name]
        ambiguity_concepts = AMBIGUITY_CONCEPTS.get(metric_name, ())
        expected_unit = METRIC_UNITS[metric_name]

        # Collect primary facts per end_key.
        primary_data: dict[str, list[_AnnualFactData]] = {}
        for concept_id in primary_concepts:
            concept_data = _get_concept_annual_facts(us_gaap, concept_id, expected_unit)
            for end_key, fact_list in concept_data.items():
                primary_data.setdefault(end_key, []).extend(fact_list)

        # Collect alternative (ambiguity) facts per end_key.
        alt_data: dict[str, list[_AnnualFactData]] = {}
        for concept_id in ambiguity_concepts:
            concept_data = _get_concept_annual_facts(us_gaap, concept_id, expected_unit)
            for end_key, fact_list in concept_data.items():
                alt_data.setdefault(end_key, []).extend(fact_list)

        if primary_data:
            primary_result[metric_name] = primary_data
        if alt_data:
            alternative_result[metric_name] = alt_data

    return primary_result, alternative_result


def _get_concept_annual_facts(
    us_gaap: dict,
    concept_id: str,
    expected_unit: str,
) -> dict[str, list[_AnnualFactData]]:
    """Extract annual FY facts from one concept, grouped by period end date.

    Each unique ``end`` date maps to a list of candidate facts (from
    different filings: original 10-K, 10-K/A amendments, or comparison
    re-statements in later filings). The caller resolves which candidate
    to use based on admissibility and revision rules.
    """
    concept = us_gaap.get(concept_id)
    if not isinstance(concept, dict):
        return {}

    units = concept.get("units")
    if not isinstance(units, dict):
        return {}

    unit_facts = units.get(expected_unit)
    if not isinstance(unit_facts, list):
        return {}

    grouped: dict[str, list[_AnnualFactData]] = {}
    for raw_fact in unit_facts:
        if not isinstance(raw_fact, dict):
            continue
        fp = raw_fact.get("fp")
        form = raw_fact.get("form")
        if fp != "FY":
            continue
        if form not in ("10-K", "10-K/A"):
            continue

        fy = raw_fact.get("fy")
        val = raw_fact.get("val")
        end = raw_fact.get("end")
        accn = raw_fact.get("accn")
        filed = raw_fact.get("filed")
        start = raw_fact.get("start")
        frame = raw_fact.get("frame")

        if not isinstance(fy, int):
            continue
        # m7: reject float values; accept int only.
        if not isinstance(val, int) or isinstance(val, bool):
            continue
        if not isinstance(end, str) or not end:
            continue
        if not isinstance(accn, str) or not accn:
            continue
        if not isinstance(filed, str) or not filed:
            continue
        if start is not None and not isinstance(start, str):
            continue

        fact_data = _AnnualFactData(
            value=val,
            start=start,
            end=end,
            accn=accn,
            filed=filed,
            form=form,
            concept_id=concept_id,
            fy=fy,
            frame=frame if isinstance(frame, str) else None,
        )
        grouped.setdefault(end, []).append(fact_data)

    # M2 fix: return ALL candidates per period end so the caller can try
    # them newest→oldest by filed date under as_of admissibility. The old
    # code called _resolve_revision here which picked a single resolved
    # fact per end, losing the ability to fall back to an older filing
    # when the latest filing's available_at > as_of.
    return grouped


def _resolve_revision(
    fact_list: list[_AnnualFactData],
) -> _AnnualFactData | None:
    """Resolve multiple facts for the same fiscal period (start, end).

    SDS §5.3: ``revision_of`` is populated ONLY when the latest fact is an
    explicit ``10-K/A`` amendment (or a uniquely proven amendment chain where
    same entity/period/scope/concept has a strictly-later filed date with a
    different form-family). Ordinary ``10-K`` comparison filings — where a
    later annual filing restates the same period value with the same form —
    never produce ``revision_of``.

    Resolution rules:
    1. Single fact: use it directly, no revision_of.
    2. Multiple facts, latest is 10-K/A and earliest is 10-K (or 10-K/A):
       use the latest value; revision_of = earliest accession.
       This is an explicit amendment chain.
    3. Multiple facts, latest is ordinary 10-K (different filing fy):
       Use the latest-filed fact. No revision_of — this is a comparison
       restatement, not an amendment.
    4. Multiple facts with same filed date and different values: ambiguous,
       return None (typed unavailable downstream).
    """
    if not fact_list:
        return None
    if len(fact_list) == 1:
        return fact_list[0]

    # Sort by filed date descending; tie-break by form (10-K/A after 10-K).
    sorted_facts = sorted(
        fact_list,
        key=lambda f: (f.filed, 1 if f.form == "10-K/A" else 0),
        reverse=True,
    )

    latest = sorted_facts[0]

    # Check for ambiguity: same filed date + same form + different values.
    latest_group = [
        f for f in sorted_facts
        if f.filed == latest.filed and f.form == latest.form
    ]
    if len(latest_group) > 1:
        values = {f.value for f in latest_group}
        if len(values) > 1:
            return None  # ambiguous: different values, same date/form

    # revision_of only for explicit 10-K/A amendment (B2 fix).
    if latest.form == "10-K/A":
        # Find the predecessor: the earliest 10-K (non-amendment) for this period.
        predecessors = [
            f for f in sorted_facts
            if f.form == "10-K" and f.accn != latest.accn
        ]
        if predecessors:
            predecessor = min(predecessors, key=lambda f: f.filed)
            return _AnnualFactData(
                value=latest.value,
                start=latest.start,
                end=latest.end,
                accn=latest.accn,
                filed=latest.filed,
                form=latest.form,
                concept_id=latest.concept_id,
                fy=latest.fy,
                frame=latest.frame,
                predecessor_accn=predecessor.accn,
            )
        # 10-K/A without a predecessor 10-K in the data: use it without revision_of.
        return latest

    # Latest is ordinary 10-K: use it, no revision_of.
    # This correctly handles comparison re-statements across filings.
    return latest


def _build_material(
    *,
    primary_facts: dict[str, dict[str, list[_AnnualFactData]]],
    alternative_facts: dict[str, dict[str, list[_AnnualFactData]]],
    period_end_key: str,
    cik: str,
    canonical_symbol: str,
    observed_at: datetime,
    available_at_resolver: AvailableAtResolver,
    raw_body_sha256: str,
    source_request_identity_digest: str,
    market: str,
    statement_scope: StatementScope,
    as_of: datetime,
) -> OfficialFundamentalMaterialV1:
    """Build one OfficialFundamentalMaterialV1 for a specific fiscal period.

    M2 fix: enumerate all distinct accessions that have primary data for
    this period. Sort by filed date descending and try each. The first
    admissible (available_at <= as_of) accession wins.
    """
    end_raw = period_end_key
    try:
        period_end = date.fromisoformat(end_raw)
    except ValueError:
        raise _MissingFiledDateError()

    # Enumerate accessions from primary facts for this period.
    candidate_facts_by_accn: dict[str, list[_AnnualFactData]] = {}
    for metric_name in _ALL_METRIC_NAMES:
        data = primary_facts.get(metric_name)
        if data and period_end_key in data:
            for fact in data[period_end_key]:
                candidate_facts_by_accn.setdefault(fact.accn, []).append(fact)

    if not candidate_facts_by_accn:
        raise _MissingFiledDateError()

    accn_filed_dates: dict[str, str] = {}
    for accn, facts in candidate_facts_by_accn.items():
        accn_filed_dates[accn] = max(f.filed for f in facts)
    sorted_accns = sorted(
        candidate_facts_by_accn.keys(),
        key=lambda a: accn_filed_dates[a],
        reverse=True,
    )

    last_error: _MissingFiledDateError | None = None
    for accn in sorted_accns:
        try:
            return _build_material_from_accn(
                primary_facts=primary_facts,
                alternative_facts=alternative_facts,
                period_end_key=period_end_key,
                period_end=period_end,
                cik=cik,
                canonical_symbol=canonical_symbol,
                observed_at=observed_at,
                available_at_resolver=available_at_resolver,
                raw_body_sha256=raw_body_sha256,
                source_request_identity_digest=source_request_identity_digest,
                market=market,
                statement_scope=statement_scope,
                as_of=as_of,
                target_accn=accn,
            )
        except _MissingFiledDateError as exc:
            last_error = exc
            continue

    raise last_error if last_error else _MissingFiledDateError()


def _build_material_from_accn(
    *,
    primary_facts: dict[str, dict[str, list[_AnnualFactData]]],
    alternative_facts: dict[str, dict[str, list[_AnnualFactData]]],
    period_end_key: str,
    period_end: date,
    cik: str,
    canonical_symbol: str,
    observed_at: datetime,
    available_at_resolver: AvailableAtResolver,
    raw_body_sha256: str,
    source_request_identity_digest: str,
    market: str,
    statement_scope: StatementScope,
    as_of: datetime,
    target_accn: str,
) -> OfficialFundamentalMaterialV1:
    """Build material from a specific accession for the given period.

    M1 fix (reclosure): ambiguity is evaluated at accession scope. A
    primary fact for ``target_accn`` is AMBIGUOUS only when the SAME
    accession also has an alternative concept fact for the exact same
    (start, end). A future accession's alternative concept cannot
    contaminate this accession's primary.

    M2 fix (reclosure): ALL evidence used by the selected accession —
    whether EXACT or AMBIGUOUS — participates in coherent
    accn/filed/form-family/fy/start/end identity checks before material
    construction. Any conflict makes this accession unavailable.
    """
    filing_id = ""
    filed_at_raw = ""
    revision_of: str | None = None
    metric_evidence: dict[str, MetricEvidence | None] = {}
    period_start: date | None = None
    # M2: coherent identity tracked across ALL evidence (EXACT + AMBIGUOUS).
    coherent_filed: str | None = None
    coherent_form: str | None = None
    coherent_fy: int | None = None

    for metric_name in _ALL_METRIC_NAMES:
        # Get primary facts for this metric/period from target accession.
        prim_data = primary_facts.get(metric_name)
        prim_candidates: list[_AnnualFactData] = (
            prim_data.get(period_end_key, []) if prim_data else []
        )
        prim_matching = [f for f in prim_candidates if f.accn == target_accn]

        # M1: check accession-scoped ambiguity. A primary fact is ambiguous
        # when the SAME accession also has an alternative concept fact for
        # the exact same (start, end) period.
        alt_data = alternative_facts.get(metric_name)
        alt_matching: list[_AnnualFactData] = []
        if alt_data and period_end_key in alt_data:
            alt_matching = [
                f for f in alt_data[period_end_key] if f.accn == target_accn
            ]

        # Determine the evidence fact (primary or ambiguous) and check
        # coherence for ALL evidence.
        evidence_fact: _AnnualFactData | None = None
        is_ambiguous = False
        # M1/M2 reclosure: ALL alt facts that share the exact (start, end)
        # with the primary are ambiguity evidence and must participate in
        # coherence + as_of checks alongside the primary.
        alt_evidence: list[_AnnualFactData] = []

        if prim_matching:
            fact = prim_matching[0] if len(prim_matching) == 1 else _resolve_revision(prim_matching)
            if fact is None:
                metric_evidence[metric_name] = MetricEvidence(
                    value=None, source_concept_id=None,
                    unit=METRIC_UNITS[metric_name],
                    selection=MetricSelection.UNAVAILABLE,
                )
                continue

            # M1 accession-scoped ambiguity: check if the SAME accession has
            # an alternative concept for the exact same (start, end).
            alt_evidence = [
                alt_f for alt_f in alt_matching
                if alt_f.start == fact.start and alt_f.end == fact.end
            ]
            if alt_evidence:
                is_ambiguous = True
            evidence_fact = fact
        else:
            # No primary fact from this accession.
            metric_evidence[metric_name] = MetricEvidence(
                value=None, source_concept_id=None,
                unit=METRIC_UNITS[metric_name],
                selection=MetricSelection.UNAVAILABLE,
            )
            continue

        if evidence_fact is None:
            raise _MissingFiledDateError()

        # M2 coherent identity: ALL evidence (EXACT or AMBIGUOUS) must
        # share filed, form-family, and fy within the same accession.
        # When ambiguous, both the primary fact AND every alt fact that
        # caused the ambiguity are evidence — they must ALL be coherent.
        all_evidence = [evidence_fact] + alt_evidence
        for ev in all_evidence:
            if coherent_filed is None:
                coherent_filed = ev.filed
                coherent_form = ev.form
                coherent_fy = ev.fy
            else:
                if (
                    ev.filed != coherent_filed
                    or ev.form != coherent_form
                    or ev.fy != coherent_fy
                ):
                    raise _MissingFiledDateError()

            # M2 period_start coherence: ALL evidence must agree on start.
            if ev.start:
                try:
                    parsed_start = date.fromisoformat(ev.start)
                except ValueError:
                    raise _MissingFiledDateError()
                if period_start is None:
                    period_start = parsed_start
                elif period_start != parsed_start:
                    raise _MissingFiledDateError()

        # M2: when ambiguous, verify EVERY alt evidence fact's available_at
        # <= as_of. A future-dated alt that hasn't been admitted yet must
        # reject the whole accession — it cannot bypass as_of by hiding
        # behind the primary's earlier filed date.
        # M2 reclosure: the resolver may raise ValueError/TypeError on
        # malformed filed strings (e.g. date.fromisoformat). Convert these
        # to _MissingFiledDateError so the accession is rejected cleanly
        # and the caller falls back to older admissible accession/period,
        # matching the exception policy of the main evidence resolver below.
        if is_ambiguous:
            for alt_ev in alt_evidence:
                try:
                    alt_resolution = available_at_resolver(alt_ev.filed)
                except (ValueError, TypeError):
                    raise _MissingFiledDateError()
                if not isinstance(alt_resolution, AvailableAtResolution):
                    raise _MissingFiledDateError()
                if alt_resolution.available_at > as_of:
                    raise _MissingFiledDateError()

        # M2 + §5.3: revision_of from cross-accession resolver.
        resolved = _resolve_revision(prim_candidates)
        if (
            resolved is not None
            and resolved.accn == target_accn
            and resolved.predecessor_accn
            and revision_of is None
        ):
            revision_of = resolved.predecessor_accn

        if is_ambiguous:
            metric_evidence[metric_name] = MetricEvidence(
                value=None, source_concept_id=None,
                unit=METRIC_UNITS[metric_name],
                selection=MetricSelection.AMBIGUOUS,
            )
        else:
            metric_evidence[metric_name] = MetricEvidence(
                value=Decimal(evidence_fact.value),
                source_concept_id=evidence_fact.concept_id,
                unit=METRIC_UNITS[metric_name],
                selection=MetricSelection.EXACT,
            )

        if not filing_id:
            filing_id = evidence_fact.accn
            filed_at_raw = evidence_fact.filed

    if not filed_at_raw:
        raise _MissingFiledDateError()

    # C3: typed resolver check. Match alt resolver exception policy:
    # convert ValueError/TypeError to _MissingFiledDateError.
    try:
        resolution = available_at_resolver(filed_at_raw)
    except (ValueError, TypeError):
        raise _MissingFiledDateError()
    if not isinstance(resolution, AvailableAtResolution):
        raise _MissingFiledDateError()
    available_at = resolution.available_at

    # M2: enforce available_at <= as_of.
    if available_at > as_of:
        raise _MissingFiledDateError()

    fiscal_year = period_end.year

    return OfficialFundamentalMaterialV1(
        source=SOURCE,
        entity_id=cik,
        canonical_symbol=canonical_symbol,
        market=market,
        accounting_regime=AccountingRegime.US_GAAP,
        statement_scope=statement_scope,
        fiscal_year=fiscal_year,
        period_start=period_start,
        period_end=period_end,
        filing_id=filing_id,
        filed_at_raw=filed_at_raw,
        available_at=available_at,
        revision_of=revision_of,
        taxonomy_namespace=TAXONOMY_NAMESPACE,
        taxonomy_version=TAXONOMY_VERSION,
        mapping_version=MAPPING_VERSION,
        revenue_ttm=metric_evidence.get("revenue_ttm"),
        net_income_ttm=metric_evidence.get("net_income_ttm"),
        gross_profit_ttm=metric_evidence.get("gross_profit_ttm"),
        total_equity=metric_evidence.get("total_equity"),
        total_assets=metric_evidence.get("total_assets"),
        diluted_shares_outstanding=metric_evidence.get("diluted_shares_outstanding"),
        source_request_identity_digest=source_request_identity_digest,
        raw_body_sha256=raw_body_sha256,
        observed_at=observed_at,
        as_of=as_of,
        available_at_session_history_source=resolution.session_history_source,
    )


class _MissingFiledDateError(Exception):
    """Internal: no metric produced a filed date for this fiscal period."""
