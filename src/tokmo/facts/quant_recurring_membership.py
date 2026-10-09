"""Pure membership planning helpers for the inactive Quant recurring lane."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from re import fullmatch

from tokmo.facts.market_regime_toss_input import candle_native_identity
from tokmo.values.cadence_policy import CadencePolicyV1, validate_cadence_operation
from tokmo.values.current_holdings import CurrentHoldingsV1
from tokmo.values.session_gate import Market

PER_START_BUDGET_SECONDS = 10.0
DEADLINE_MARGIN_SECONDS = 30

_ENDPOINT_OPERATIONS = {
    "nasdaqlisted": "MEMBERSHIP_NASDAQ_LISTED",
    "otherlisted": "MEMBERSHIP_OTHER_LISTED",
    "registry": "MEMBERSHIP_SEC_REGISTRY",
    "issuer": "MEMBERSHIP_SEC_ISSUER",
}


class EndpointKindV1(str, Enum):
    NASDAQ_LISTED = "NASDAQ_LISTED"
    OTHER_LISTED = "OTHER_LISTED"
    SEC_REGISTRY = "SEC_REGISTRY"
    SEC_SUBMISSIONS = "SEC_SUBMISSIONS"


@dataclass(frozen=True, slots=True)
class OfficialMembershipRequestV1:
    endpoint_kind: EndpointKindV1
    cik: str | None = None

    def __post_init__(self):
        if not isinstance(self.endpoint_kind, EndpointKindV1):
            raise ValueError("endpoint_kind")
        if self.endpoint_kind is EndpointKindV1.SEC_SUBMISSIONS:
            if (
                type(self.cik) is not str
                or len(self.cik) != 10
                or not self.cik.isascii()
                or not self.cik.isdigit()
            ):
                raise ValueError("cik")
        elif self.cik is not None:
            raise ValueError("cik")

    @property
    def url(self) -> str:
        if self.endpoint_kind is EndpointKindV1.SEC_SUBMISSIONS:
            return f"https://data.sec.gov/submissions/CIK{self.cik}.json"
        return {
            EndpointKindV1.NASDAQ_LISTED: (
                "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
            ),
            EndpointKindV1.OTHER_LISTED: (
                "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"
            ),
            EndpointKindV1.SEC_REGISTRY: (
                "https://www.sec.gov/files/company_tickers_exchange.json"
            ),
        }[self.endpoint_kind]


def predict_us_personal_cohort(
    prior_holdings: CurrentHoldingsV1 | None,
) -> tuple[str, ...]:
    if not isinstance(prior_holdings, CurrentHoldingsV1):
        return ()
    try:
        from tokmo.programs.quant_membership import (
            describe_us_personal_membership_cohort,
        )

        return describe_us_personal_membership_cohort(
            prior_holdings, prior_holdings.captured_at
        ).expected_symbols
    except (AttributeError, TypeError, ValueError):
        return ()


def _validate_session_date(session_date: date) -> None:
    if type(session_date) is not date or not 2007 <= session_date.year <= 9999:
        raise ValueError("session_date")


def _validate_symbols(symbols: tuple[str, ...]) -> None:
    if (
        type(symbols) is not tuple
        or not 1 <= len(symbols) <= 8
        or len(set(symbols)) != len(symbols)
        or any(fullmatch(r"[A-Z0-9._-]{1,32}", symbol) is None for symbol in symbols)
    ):
        raise ValueError("symbols")


def membership_native_identity(
    *,
    endpoint: str,
    session_date: date,
    symbol: str | None = None,
) -> str:
    _validate_session_date(session_date)
    if endpoint not in _ENDPOINT_OPERATIONS:
        raise ValueError("endpoint")
    if (endpoint == "issuer") != (symbol is not None):
        raise ValueError("endpoint")
    if symbol is not None and fullmatch(r"[A-Z0-9._-]{1,32}", symbol) is None:
        raise ValueError("symbols")
    suffix = "" if symbol is None else f":{symbol}"
    return f"membership:{endpoint}:{session_date.isoformat()}{suffix}"


def membership_operation_plan(
    symbols: tuple[str, ...],
    session_date: date,
) -> tuple[tuple[str, str], ...]:
    _validate_session_date(session_date)
    _validate_symbols(symbols)
    endpoints = (
        ("nasdaqlisted", None),
        ("otherlisted", None),
        ("registry", None),
        *(("issuer", symbol) for symbol in sorted(symbols)),
    )
    pairs = tuple(
        (
            _ENDPOINT_OPERATIONS[endpoint],
            membership_native_identity(
                endpoint=endpoint,
                session_date=session_date,
                symbol=symbol,
            ),
        )
        for endpoint, symbol in endpoints
    )
    for operation, native_identity in pairs:
        if validate_cadence_operation(operation, native_identity) != "MEMBERSHIP":
            raise ValueError("membership operation plan")
    return pairs


def quant_candle_native_identity(
    *,
    market: Market,
    symbol: str,
    session_date: date,
    page_index: int,
) -> str:
    return candle_native_identity("qt", market, symbol, session_date, page_index)


def sufficient_deadline(
    *,
    policy: CadencePolicyV1,
    scheduled_for_utc: datetime,
    observed_now_utc: datetime,
    planned_starts: int,
) -> bool:
    remaining = (
        policy.child_timeout_seconds
        - (observed_now_utc - scheduled_for_utc).total_seconds()
    )
    required = planned_starts * PER_START_BUDGET_SECONDS + DEADLINE_MARGIN_SECONDS
    return remaining >= required
