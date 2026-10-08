"""U.S. Treasury official daily par-yield facts."""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from xml.etree import ElementTree

from tokmo.values import YieldCurveFact

SOURCE = "us_treasury_daily_par_yield_curve"
ENDPOINT = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
_DATA_NS = "http://schemas.microsoft.com/ado/2007/08/dataservices"
Transport = Callable[[str, float], bytes]


def _decimal(text: str | None) -> Decimal | None:
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def parse_yield_curve_xml(xml_text: str, *, observed_at: datetime) -> YieldCurveFact:
    root = ElementTree.fromstring(xml_text)
    rows = []
    for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
        raw_date = entry.findtext(f".//{{{_DATA_NS}}}NEW_DATE")
        if raw_date:
            rows.append((raw_date, entry))
    if not rows:
        raise ValueError("treasury_feed_empty")
    raw_date, latest = max(rows, key=lambda item: item[0])
    return YieldCurveFact(
        source=SOURCE,
        market="US",
        rate_date=datetime.fromisoformat(raw_date.replace("Z", "+00:00")).date(),
        two_year_percent=_decimal(latest.findtext(f".//{{{_DATA_NS}}}BC_2YEAR")),
        ten_year_percent=_decimal(latest.findtext(f".//{{{_DATA_NS}}}BC_10YEAR")),
        observed_at=observed_at,
    )


def get_yield_curve(*, year: int, month: int, transport: Transport,
                    clock: Callable[[], datetime]) -> YieldCurveFact:
    if year < 1990 or not 1 <= month <= 12:
        raise ValueError("invalid_treasury_month")
    query = urlencode({"data": "daily_treasury_yield_curve",
                       "field_tdr_date_value_month": f"{year:04d}{month:02d}"})
    body = transport(f"{ENDPOINT}?{query}", 10.0)
    return parse_yield_curve_xml(body.decode("utf-8"), observed_at=clock())
