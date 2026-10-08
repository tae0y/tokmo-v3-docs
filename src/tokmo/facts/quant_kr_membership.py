"""Pure KR membership facts for the quant market-profiles lane.

Cohort predicate over personal Toss holdings, the one-start STOCKS plan, the
typed ``StockInfo`` admission gates, and the provenance digest. Everything
here is pure computation over typed values — no transport, no parser, no
store. The production-pinned token vocabulary contains only tokens grounded
in the in-repo contract fixtures; the ETF security-type token and any wider
market/status tokens are SDS_GAP-1/2 and stay fail-closed (refuse, never
guess) until pinned at G3 from the private canary artifact manifest.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from re import fullmatch
from types import MappingProxyType
from typing import Mapping
from zoneinfo import ZoneInfo

from tokmo.values.account_scope_patterns import ACCOUNT_SCOPE_RE, PENSION_SCOPE_RE
from tokmo.values.cadence_policy import validate_cadence_operation
from tokmo.values.current_holdings import (
    CurrentHoldingsV1,
    canonical_holdings_bytes,
    classify_market,
)
from tokmo.values.material_pack import canonical_encode
from tokmo.values.quant_multifactor_core import MarketV1
from tokmo.values.quant_profiles import KR_QUANT_PROFILE_V1
from tokmo.values.session_gate import MARKET_TIMEZONE, Market
from tokmo.values.toss_reference import KrMarketDetail, StockInfo


MAX_OBSERVATION_AGE = timedelta(minutes=30)
_KR_MARKET_TIMEZONE = ZoneInfo(MARKET_TIMEZONE[Market.KR])
_SYMBOL_RE = r"[A-Z0-9._-]{1,32}"
_KR_MARKETS = frozenset({MarketV1.KR_KOSPI, MarketV1.KR_KOSDAQ})


@dataclass(frozen=True, slots=True)
class KrMembershipVocabularyV1:
    """Closed KR token vocabulary; every gate refuses unseen tokens."""

    active_status_tokens: frozenset[str]
    stock_security_type_token: str
    etf_security_type_tokens: frozenset[str]
    market_tokens: Mapping[str, MarketV1]

    def __post_init__(self) -> None:
        if (
            type(self.stock_security_type_token) is not str
            or not self.stock_security_type_token
            or self.stock_security_type_token in self.etf_security_type_tokens
        ):
            raise ValueError("security_type_tokens")
        if not isinstance(self.market_tokens, Mapping) or any(
            market not in _KR_MARKETS for market in self.market_tokens.values()
        ):
            raise ValueError("market_tokens")
        object.__setattr__(
            self, "market_tokens", MappingProxyType(dict(self.market_tokens)),
        )


# Production pin: exactly the tokens observed in the in-repo contract shape
# (tests/test_toss_reads_contract.py — market="KOSPI", security_type="STOCK",
# status="ACTIVE"). SDS_GAP-1: no ETF security-type token is pinned yet, so
# ETF members refuse fail-closed. SDS_GAP-2: no ETF market-token map beyond
# the observed "KOSPI" is pinned yet.
KR_MEMBERSHIP_VOCABULARY_V1 = KrMembershipVocabularyV1(
    active_status_tokens=frozenset({"ACTIVE"}),
    stock_security_type_token="STOCK",
    etf_security_type_tokens=frozenset(),
    market_tokens=MappingProxyType({"KOSPI": MarketV1.KR_KOSPI}),
)


@dataclass(frozen=True, slots=True)
class KrCohortBindingV1:
    expected_symbols: tuple[str, ...]
    membership_holdings_digest: str
    membership_as_of: date
    holdings_bytes: bytes


@dataclass(frozen=True, slots=True)
class KrAdmittedMemberV1:
    symbol: str
    isin_code: str
    market: MarketV1
    market_token: str
    security_type: str
    is_common_share: bool
    status: str
    currency: str
    list_date: date | None
    krx_trading_suspended: bool


@dataclass(frozen=True, slots=True)
class KrAdmissionResultV1:
    admitted: tuple[KrAdmittedMemberV1, ...]
    refusals: tuple[tuple[str, str], ...]


def _kr_market_date(instant: datetime) -> date:
    return instant.astimezone(_KR_MARKET_TIMEZONE).date()


def describe_kr_personal_membership_cohort(
    holdings: CurrentHoldingsV1,
    evaluation_as_of: datetime,
) -> KrCohortBindingV1:
    """Personal Toss KR cohort with the same scope rule as the US lane."""
    if evaluation_as_of.tzinfo is None or evaluation_as_of.utcoffset() is None:
        raise ValueError("evaluation_as_of")
    if holdings.captured_at > evaluation_as_of:
        raise ValueError("holdings_future")
    if evaluation_as_of - holdings.captured_at > MAX_OBSERVATION_AGE:
        raise ValueError("holdings_stale")
    if _kr_market_date(holdings.captured_at) != _kr_market_date(evaluation_as_of):
        raise ValueError("holdings_market_date")
    symbol_markets: dict[str, object] = {}
    for account in holdings.accounts:
        scope = account.account_scope
        if ACCOUNT_SCOPE_RE.fullmatch(scope) is not None:
            include = True
        elif PENSION_SCOPE_RE.fullmatch(scope) is not None:
            include = False
        else:
            raise ValueError("account_scope")
        if not include:
            continue
        for item in account.holdings:
            market = classify_market(item.market_raw, item.market_field)
            if market is None:
                raise ValueError("market_unknown")
            prior = symbol_markets.get(item.symbol)
            if prior is not None and prior is not market:
                raise ValueError(f"market_conflict:{item.symbol}")
            symbol_markets[item.symbol] = market
    expected_symbols = tuple(
        sorted(
            symbol for symbol, market in symbol_markets.items()
            if market is Market.KR
        )
    )
    holdings_bytes = canonical_holdings_bytes(holdings)
    return KrCohortBindingV1(
        expected_symbols=expected_symbols,
        membership_holdings_digest=sha256(holdings_bytes).hexdigest(),
        membership_as_of=_kr_market_date(evaluation_as_of),
        holdings_bytes=holdings_bytes,
    )


def predict_kr_personal_cohort(
    prior_holdings: CurrentHoldingsV1 | None,
) -> tuple[str, ...]:
    if not isinstance(prior_holdings, CurrentHoldingsV1):
        return ()
    try:
        return describe_kr_personal_membership_cohort(
            prior_holdings, prior_holdings.captured_at
        ).expected_symbols
    except (AttributeError, TypeError, ValueError):
        return ()


def kr_membership_native_identity(session_date: date) -> str:
    if type(session_date) is not date or not 2007 <= session_date.year <= 9999:
        raise ValueError("session_date")
    return f"membership:toss-stocks:{session_date.isoformat()}"


def kr_membership_operation_plan(
    symbols: tuple[str, ...], session_date: date,
) -> tuple[tuple[str, str], ...]:
    """Exactly one STOCKS batch start; a cohort beyond the KR profile maximum
    is refused here, before any wire could be planned."""
    if (
        type(symbols) is not tuple
        or not 1 <= len(symbols) <= KR_QUANT_PROFILE_V1.cohort_max
        or len(set(symbols)) != len(symbols)
        or any(fullmatch(_SYMBOL_RE, symbol) is None for symbol in symbols)
    ):
        raise ValueError("symbols")
    pair = ("MEMBERSHIP_TOSS_STOCKS", kr_membership_native_identity(session_date))
    if validate_cadence_operation(*pair) != "MEMBERSHIP":
        raise ValueError("membership operation plan")
    return (pair,)


def verify_kr_cohort_echo(
    infos: tuple[StockInfo, ...], expected_symbols: tuple[str, ...],
) -> None:
    returned = tuple(info.symbol for info in infos)
    if (
        len(set(returned)) != len(returned)
        or set(returned) != set(expected_symbols)
    ):
        raise ValueError("cohort_echo")


def _admit_member(
    info: StockInfo, vocabulary: KrMembershipVocabularyV1,
) -> KrAdmittedMemberV1 | str:
    if info.status not in vocabulary.active_status_tokens:
        return "status_not_active"
    if isinstance(info.delist_date, date):
        return "delisted"
    if info.currency != "KRW":
        return "currency_not_krw"
    if type(info.isin_code) is not str or not info.isin_code.startswith("KR"):
        return "isin_not_kr"
    market = vocabulary.market_tokens.get(info.market)
    if market is None:
        return "market_token_unknown"
    if info.security_type == vocabulary.stock_security_type_token:
        if info.is_common_share is not True:
            return "not_common_share"
    elif info.security_type not in vocabulary.etf_security_type_tokens:
        return "security_type_unknown"
    detail = info.korean_market_detail
    if not isinstance(detail, KrMarketDetail):
        return "market_detail_missing"
    if detail.krx_trading_suspended is not False:
        return "krx_trading_suspended"
    return KrAdmittedMemberV1(
        symbol=info.symbol,
        isin_code=info.isin_code,
        market=market,
        market_token=info.market,
        security_type=info.security_type,
        is_common_share=info.is_common_share,
        status=info.status,
        currency=info.currency,
        list_date=info.list_date if isinstance(info.list_date, date) else None,
        krx_trading_suspended=False,
    )


def admit_kr_membership(
    infos: tuple[StockInfo, ...],
    *,
    expected_symbols: tuple[str, ...],
    vocabulary: KrMembershipVocabularyV1 = KR_MEMBERSHIP_VOCABULARY_V1,
) -> KrAdmissionResultV1:
    """Typed fail-closed admission of one exact-echo STOCKS response."""
    if not isinstance(vocabulary, KrMembershipVocabularyV1):
        raise ValueError("vocabulary")
    if not all(isinstance(info, StockInfo) for info in infos):
        raise ValueError("stock_info")
    verify_kr_cohort_echo(infos, expected_symbols)
    admitted = []
    refusals = []
    for info in sorted(infos, key=lambda item: item.symbol):
        outcome = _admit_member(info, vocabulary)
        if isinstance(outcome, KrAdmittedMemberV1):
            admitted.append(outcome)
        else:
            refusals.append((info.symbol, outcome))
    return KrAdmissionResultV1(tuple(admitted), tuple(refusals))


def _member_payload(member: KrAdmittedMemberV1) -> dict:
    return {
        "symbol": member.symbol,
        "isin_code": member.isin_code,
        "market": member.market_token,
        "security_type": member.security_type,
        "is_common_share": member.is_common_share,
        "status": member.status,
        "currency": member.currency,
        "list_date": (
            None if member.list_date is None else member.list_date.isoformat()
        ),
        "delist_date": None,
        "krx_trading_suspended": member.krx_trading_suspended,
    }


def kr_provenance_digest(
    *,
    members: tuple[KrAdmittedMemberV1, ...],
    request_identity: str,
    observed_at: datetime,
) -> str:
    """sha256 over the canonical typed admission fields plus request identity
    and ``observed_at`` — never over prose (§5.2)."""
    if type(request_identity) is not str or not request_identity:
        raise ValueError("request_identity")
    if (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        raise ValueError("observed_at")
    payload = {
        "schema_version": "kr-membership-provenance-v1",
        "request_identity": request_identity,
        "observed_at": observed_at,
        "members": [
            {**_member_payload(member), "market_v1": member.market.value}
            for member in members
        ],
    }
    return sha256(canonical_encode(payload)).hexdigest()


def kr_material_members(
    members: tuple[KrAdmittedMemberV1, ...],
) -> tuple[dict, ...]:
    """Material member records (§9), sorted by symbol, codec-shaped."""
    rows = tuple(
        _member_payload(member)
        for member in sorted(members, key=lambda item: item.symbol)
    )
    return rows
