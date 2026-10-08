"""Immutable, bounded fundamental material value types.

OfficialFundamentalMaterialV1 is the source-neutral immutable container for
official annual fundamental data (SEC companyfacts or OpenDART fnlttSinglAcnt).
It carries 6 canonical metrics with typed selection evidence, revision
provenance, and a composite SHA-256 source-ref digest.

Annual 10-K/FY only. Quarterly sums, arithmetic reconstruction, fuzzy name
matching, and currency conversion are never performed at this layer. Missing
or ambiguous metrics are carried as typed MetricEvidence (AMBIGUOUS/UNAVAILABLE)
with value/concept = None, NOT dropped to bare None on the material.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import ClassVar


class AccountingRegime(str, Enum):
    US_GAAP = "US_GAAP"
    K_IFRS = "K_IFRS"


class StatementScope(str, Enum):
    CONSOLIDATED = "CONSOLIDATED"
    NON_CONSOLIDATED = "NON_CONSOLIDATED"


class MetricSelection(str, Enum):
    """How a metric value was selected from source facts."""

    EXACT = "exact"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"


# SDS §5.4: the exact policy version for date-only available-at normalization.
DATE_ONLY_POLICY_VERSION = "official-date-only-next-completed-session-v1"

# C3: the session-history source identity that the date-only resolver
# contract must carry. Production parsing must bind a verified source,
# not accept an arbitrary callable that happens to return a datetime.
DATE_ONLY_SESSION_HISTORY_SOURCE = "nysearca-next-completed-session"

# SDS §6.4: provenance digest prefixes.
FUNDAMENTAL_SOURCE_DIGEST_PREFIX = "qfundv1:sha256:"
FUNDAMENTAL_SOURCE_REF_MAX_LEN = 80

SEC_FUNDAMENTAL_SOURCE = "sec_companyfacts_fundamentals"
OPENDART_FUNDAMENTAL_SOURCE = "opendart_fundamentals"


@dataclass(frozen=True, slots=True)
class AvailableAtResolution:
    """C3: typed result of the date-only available-at resolution contract.

    DATE_ONLY_POLICY_VERSION cannot be stamped merely because an arbitrary
    callable returned a datetime. The resolver must produce a typed result
    carrying:
    - available_at: the resolved datetime (next completed regular session close)
    - policy_version: must equal DATE_ONLY_POLICY_VERSION
    - session_history_source: verified identity of the session-history source

    Production parsing requires this exact contract. Tests may use a typed
    fake that implements this contract — not an arbitrary lambda.
    """

    available_at: datetime
    policy_version: str
    session_history_source: str

    def __post_init__(self) -> None:
        if self.policy_version != DATE_ONLY_POLICY_VERSION:
            raise ValueError("available_at_resolution_policy_version_mismatch")
        if self.session_history_source != DATE_ONLY_SESSION_HISTORY_SOURCE:
            raise ValueError("available_at_resolution_session_history_source_mismatch")
        if (
            not isinstance(self.available_at, datetime)
            or self.available_at.tzinfo is None
            or self.available_at.utcoffset() is None
        ):
            raise ValueError("available_at_resolution_requires_aware_datetime")


@dataclass(frozen=True, slots=True)
class MetricEvidence:
    """One canonical metric's source evidence and value.

    When selection is AMBIGUOUS or UNAVAILABLE, value and source_concept_id
    must be None — the metric is present as typed unavailable evidence without
    a numeric value.
    """

    value: Decimal | None
    source_concept_id: str | None
    unit: str
    selection: MetricSelection

    def __post_init__(self) -> None:
        if self.selection in (MetricSelection.AMBIGUOUS, MetricSelection.UNAVAILABLE):
            if self.value is not None:
                raise ValueError("ambiguous_unavailable_must_have_none_value")
            if self.source_concept_id is not None:
                raise ValueError("ambiguous_unavailable_must_have_none_concept")


@dataclass(frozen=True, slots=True)
class OfficialFundamentalMaterialV1:
    """Immutable, bounded official fundamental material for one fiscal year.

    Annual-only. Each of the 6 canonical metrics carries its own typed evidence,
    allowing per-metric unavailability (AMBIGUOUS/UNAVAILABLE) without rejecting
    the entire material.
    """

    # Identity
    source: str
    entity_id: str
    canonical_symbol: str
    market: str
    accounting_regime: AccountingRegime
    statement_scope: StatementScope

    # Period (annual only)
    fiscal_year: int
    period_start: date | None
    period_end: date

    # Filing
    filing_id: str
    filed_at_raw: str
    available_at: datetime
    revision_of: str | None

    # Taxonomy
    taxonomy_namespace: str
    taxonomy_version: str
    mapping_version: str

    # 6 canonical metrics (None = not present in source at all;
    # MetricEvidence with UNAVAILABLE = present but unavailable)
    revenue_ttm: MetricEvidence | None
    net_income_ttm: MetricEvidence | None
    gross_profit_ttm: MetricEvidence | None
    total_equity: MetricEvidence | None
    total_assets: MetricEvidence | None
    diluted_shares_outstanding: MetricEvidence | None

    # Source verification
    source_request_identity_digest: str
    raw_body_sha256: str
    observed_at: datetime

    # M6: as_of for available_at <= as_of enforcement
    as_of: datetime

    # C3: verified session-history source identity from the typed resolver.
    # Binds the available_at resolution proof into the material digest.
    available_at_session_history_source: str = ""

    # Lazily computed source_ref, cached via object.__setattr__ in property.
    _source_ref: str | None = None

    _METRIC_NAMES: ClassVar[tuple[str, ...]] = (
        "revenue_ttm",
        "net_income_ttm",
        "gross_profit_ttm",
        "total_equity",
        "total_assets",
        "diluted_shares_outstanding",
    )

    def __post_init__(self) -> None:
        ref = self._compute_source_ref()
        if len(ref) >= FUNDAMENTAL_SOURCE_REF_MAX_LEN:
            raise ValueError("source_ref_exceeds_bound")
        object.__setattr__(self, "_source_ref", ref)

    @property
    def source_ref(self) -> str:
        return self._source_ref  # type: ignore[return-value]

    def _compute_source_ref(self) -> str:
        """SDS §6.4: composite SHA-256 digest binding all provenance fields.

        payload binds: source, entity_id, fiscal_year, period_start, period_end,
        filing_id, filed_at_raw, revision_of, taxonomy/mapping version,
        date_only_policy, market, statement_scope, available_at, as_of,
        and per-metric (concept_id, unit, selection, value) evidence.

        M2 fix: metric values are included in the digest so that two materials
        with different values but identical metadata produce different source_refs.

        C3 fix: the session_history_source from the typed resolver is bound
        into the digest so that available_at cannot be stamped without a
        verified session-history source identity.

        m3 fix: filed_at_raw is bound into the digest so that tampering with
        the raw filing date is detected on store readback.
        """
        parts: list[str] = [
            self.source,
            self.entity_id,
            str(self.fiscal_year),
            self.period_start.isoformat() if self.period_start else "",
            self.period_end.isoformat(),
            self.filing_id,
            self.filed_at_raw,
            self.revision_of or "",
            f"{self.taxonomy_namespace}:{self.taxonomy_version}:{self.mapping_version}",
            DATE_ONLY_POLICY_VERSION,
            self.market,
            self.statement_scope.value,
            self.available_at.isoformat(),
            self.available_at_session_history_source,
            self.as_of.isoformat(),
        ]
        for name in self._METRIC_NAMES:
            ev = getattr(self, name)
            if ev is None:
                parts.append(f"{name}::absent")
            elif ev.selection == MetricSelection.UNAVAILABLE:
                parts.append(f"{name}::unavailable")
            elif ev.selection == MetricSelection.AMBIGUOUS:
                parts.append(f"{name}::ambiguous")
            else:
                # M2: include value in digest for EXACT metrics.
                parts.append(
                    f"{name}:{ev.source_concept_id or ''}:{ev.unit}:{ev.selection.value}:{ev.value}"
                )
        payload = "|".join(parts)
        digest = hashlib.sha256(payload.encode("ascii")).hexdigest()
        return FUNDAMENTAL_SOURCE_DIGEST_PREFIX + digest
