from datetime import date
from decimal import Decimal

import pytest

from tokmo.values.fred_macro import (
    FredOperation, FredRequest, PageCompleteness, ReleaseClassification,
    canonical_fred_value, classification_flags,
)


def test_closed_requests_decimal_missing_and_classification_contract():
    request = FredRequest(
        operation=FredOperation.SERIES_OBSERVATIONS,
        series_id="PAYEMS", output_type=4,
        realtime_start=date(2026, 1, 1), realtime_end=date(2026, 1, 2),
        observation_start=date(2025, 12, 1), observation_end=date(2025, 12, 31),
        limit=10,
    )
    assert request.request_identity
    assert canonical_fred_value(".") == (None, "MISSING")
    assert canonical_fred_value("001.2300") == (Decimal("1.23"), "PRESENT")
    assert classification_flags(ReleaseClassification.INITIAL) == (True, False)
    assert classification_flags(ReleaseClassification.REVISION) == (False, True)
    assert classification_flags(ReleaseClassification.UNKNOWN) == (None, None)
    assert PageCompleteness.from_counts(0, 2, 2) is PageCompleteness.COMPLETE
    assert PageCompleteness.from_counts(0, 2, 3) is PageCompleteness.TRUNCATED
    with pytest.raises(ValueError, match="vintage_dates_output_policy"):
        FredRequest(operation=FredOperation.SERIES_OBSERVATIONS,
                    series_id="PAYEMS", output_type=4,
                    vintage_dates=(date(2026, 1, 1),))
    with pytest.raises(ValueError, match="realtime_vintage_exclusive"):
        FredRequest(operation=FredOperation.SERIES_OBSERVATIONS,
                    series_id="PAYEMS", output_type=2,
                    realtime_start=date(2026, 1, 1),
                    realtime_end=date(2026, 1, 2),
                    vintage_dates=(date(2026, 1, 1),))
