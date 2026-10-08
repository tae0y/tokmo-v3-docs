from datetime import datetime, timezone
from decimal import Decimal

from tokmo.facts.treasury import parse_yield_curve_xml

XML = '''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices"
      xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
  <entry><content type="application/xml"><m:properties>
    <d:NEW_DATE>2026-07-24T00:00:00</d:NEW_DATE>
    <d:BC_2YEAR>3.85</d:BC_2YEAR>
    <d:BC_10YEAR>4.40</d:BC_10YEAR>
  </m:properties></content></entry>
</feed>'''


def test_parses_latest_two_and_ten_year_yields_as_typed_fact():
    observed = datetime(2026, 7, 27, 1, 0, tzinfo=timezone.utc)
    fact = parse_yield_curve_xml(XML, observed_at=observed)

    assert fact.source == "us_treasury_daily_par_yield_curve"
    assert fact.market == "US"
    assert fact.rate_date.isoformat() == "2026-07-24"
    assert fact.two_year_percent == Decimal("3.85")
    assert fact.ten_year_percent == Decimal("4.40")
    assert fact.spread_percentage_points == Decimal("0.55")
    assert fact.observed_at == observed
