"""Thin programs seam: record a completed disclosure review.

This function does not fetch data, infer review time, classify alert
importance, or call delivery code. It forwards the fact and caller-supplied
reviewed_at to the records store.
"""

from datetime import datetime

from tokmo.records.disclosure_reviews import (
    DisclosureReviewReceipt,
    DisclosureReviewStore,
)
from tokmo.values import OpenDartDisclosureFact, SecFilingFact

__all__ = ["record_completed_disclosure_review"]


def record_completed_disclosure_review(
    fact: SecFilingFact | OpenDartDisclosureFact,
    *,
    reviewed_at: datetime,
    store: DisclosureReviewStore,
) -> DisclosureReviewReceipt:
    return store.append_review(fact, reviewed_at=reviewed_at)
