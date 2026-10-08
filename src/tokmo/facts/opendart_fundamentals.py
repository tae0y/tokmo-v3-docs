"""OpenDART fundamentals typed contracts (G1A, KR PAUSED).

SDS §4.4: KR cohort is PAUSED until official KOSPI/KOSDAQ classifier
contract, as-of effective semantics, and listing/sector material are
resolved by independent review. provider starts = 0.

This module provides the typed contract surface and a paused result
builder. No endpoints are invented, no network calls are made. When KR
is unpaused in a future SDS delta, the adapter will be populated with
the official fnlttSinglAcnt contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from tokmo.values.fundamental_materials import OfficialFundamentalMaterialV1

SOURCE = "opendart_fundamentals"
OPENDART_FUNDAMENTALS_STATUS = "PAUSED"
OPENDART_FUNDAMENTALS_PAUSED_REASON = "kr_classifier_unresolved"


@dataclass(frozen=True)
class OpenDartFundamentalsResult:
    """KR fundamentals parse result: always paused/unavailable in G1A."""

    materials: tuple[OfficialFundamentalMaterialV1, ...] | None
    unavailable_reason: str | None

    _STATUS: ClassVar[str] = OPENDART_FUNDAMENTALS_STATUS


def build_paused_result() -> OpenDartFundamentalsResult:
    """Build a PAUSED result for KR fundamentals.

    This is the only result this module produces in G1A. KR is PAUSED
    with provider starts = 0.
    """
    return OpenDartFundamentalsResult(
        materials=None,
        unavailable_reason=OPENDART_FUNDAMENTALS_PAUSED_REASON,
    )
