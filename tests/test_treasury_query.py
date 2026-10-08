from datetime import datetime, timezone

import pytest

from tokmo.facts.treasury import get_yield_curve

XML = b'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices" xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"><entry><content><m:properties><d:NEW_DATE>2026-07-24T00:00:00</d:NEW_DATE><d:BC_2YEAR>3.85</d:BC_2YEAR><d:BC_10YEAR>4.40</d:BC_10YEAR></m:properties></content></entry></feed>'''


def test_query_uses_official_month_parameter_and_one_transport_call():
    calls = []
    def transport(url: str, timeout: float) -> bytes:
        calls.append((url, timeout))
        return XML

    fact = get_yield_curve(year=2026, month=7, transport=transport,
                           clock=lambda: datetime(2026, 7, 27, tzinfo=timezone.utc))

    assert len(calls) == 1
    assert "data=daily_treasury_yield_curve" in calls[0][0]
    assert "field_tdr_date_value_month=202607" in calls[0][0]
    assert calls[0][1] == 10.0
    assert fact.rate_date.isoformat() == "2026-07-24"


def test_transport_and_clock_are_mandatory_injections_with_no_live_default():
    with pytest.raises(TypeError):
        get_yield_curve(year=2026, month=7)
