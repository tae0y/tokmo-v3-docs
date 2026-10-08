"""Market-local calendar request dates and typed fetch identity (SDS §3-§4).

Every derivation starts from one aware UTC instant. KR owns Asia/Seoul and US
owns America/New_York; ``utc_now().date()`` is never a market trade date.
"""

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from tokmo.values._primitives import MISSING
from tokmo.values.ops_kernel import CalendarStartReceiptV1
from tokmo.values.session_gate import MARKET_TIMEZONE, Market
from tokmo.values.toss_market_calendar import CalendarSession, KrMarketDay, UsMarketDay


class CalendarRefreshError(ValueError):
    """A fetched calendar result failed its typed identity contract."""


class CalendarIdentityError(CalendarRefreshError):
    """Returned market/day type does not match the requested market."""


class CalendarDateError(CalendarRefreshError):
    """Returned trade date does not equal the requested market-local date."""


class CalendarBoundaryError(CalendarRefreshError):
    """Regular-session boundaries are naive, malformed, or inverted."""


def market_local_date(market: Market, now_utc: datetime) -> date:
    if not isinstance(market, Market):
        raise ValueError("unknown market")
    if not isinstance(now_utc, datetime) or now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now must be an aware datetime")
    return now_utc.astimezone(ZoneInfo(MARKET_TIMEZONE[market])).date()


def _regular_session(day, market: Market):
    if market is Market.KR:
        integrated = day.integrated
        if integrated is None or integrated is MISSING:
            return None
        session = integrated.regular_market
    else:
        session = day.regular_market
    if session is None or session is MISSING:
        return None
    if not isinstance(session, CalendarSession):
        raise CalendarBoundaryError("calendar regular session structure changed")
    return session


@dataclass(frozen=True)
class CalendarFetchResult:
    """Closed typed identity of one market calendar fetch (SDS §4 step 4).

    Carries the OpsKernel STARTED calendar receipt (M5): the durable request
    identity that admitted this wire attempt. A result without a matching
    STARTED receipt cannot exist, so the cache commit is structurally bound
    to the admission identity.
    """

    market: Market
    requested_local_date: date
    day: KrMarketDay | UsMarketDay
    receipt: CalendarStartReceiptV1

    def __post_init__(self):
        if not isinstance(self.market, Market):
            raise CalendarIdentityError("unknown market")
        expected_type = KrMarketDay if self.market is Market.KR else UsMarketDay
        if type(self.day) is not expected_type:
            raise CalendarIdentityError("returned market day type mismatch")
        if type(self.requested_local_date) is not date:
            raise CalendarDateError("requested local date must be a date")
        if type(self.day.date) is not date or self.day.date != self.requested_local_date:
            raise CalendarDateError("returned trade date mismatch")
        if not isinstance(self.receipt, CalendarStartReceiptV1):
            raise CalendarIdentityError("missing STARTED calendar receipt")
        if self.receipt.market is not self.market:
            raise CalendarIdentityError("receipt market mismatch")
        if self.receipt.market_local_date != self.requested_local_date:
            raise CalendarDateError("receipt local date mismatch")
        if self.receipt.state != "STARTED":
            raise CalendarIdentityError("calendar receipt not STARTED")
        session = _regular_session(self.day, self.market)
        if session is None:
            return
        for boundary in (session.start_time, session.end_time):
            if (not isinstance(boundary, datetime) or boundary.tzinfo is None
                    or boundary.utcoffset() is None):
                raise CalendarBoundaryError("regular session boundaries must be aware")
        if session.start_time >= session.end_time:
            raise CalendarBoundaryError("regular session must have positive duration")
