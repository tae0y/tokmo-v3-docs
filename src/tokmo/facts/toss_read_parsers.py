"""Operation-dispatched envelope parsing and schema-specific atomic parsers
for the 20 remaining Toss reads.

No I/O, no semantic calculations, no derived status, no positivity checks,
no canonicalization, and no rank ordering. Every array-bearing endpoint
fails atomically: one bad item invalidates the entire result.
"""

import json
import re
from datetime import date

from tokmo.values import (
    _INSTITUTION_BREAKDOWN_KEYS,
    MISSING,
    CalendarSession,
    Candle,
    CandlePageResponse,
    ConditionalOrderCondition,
    ConditionalOrderDetailResponse,
    ExchangeRateResponse,
    InstitutionTradingBreakdown,
    IntegratedHour,
    InvestorTradingAmount,
    InvestorTradingRecord,
    InvestorTradingResponse,
    KrMarketCalendarResponse,
    KrMarketDay,
    KrMarketDetail,
    MarketIndicatorCandle,
    MarketIndicatorCandlePageResponse,
    MarketIndicatorPrice,
    Opt,
    Order,
    OrderbookEntry,
    OrderbookResponse,
    OrderExecution,
    PaginatedConditionalOrderResponse,
    PaginatedOrderResponse,
    PriceLimitResponse,
    RankingItem,
    RankingPrice,
    RankingResponse,
    RawStr,
    StockInfo,
    StockWarning,
    Trade,
    UsMarketCalendarResponse,
    UsMarketDay,
    _AwareDateTime,
    _Decimal30,
    _StrictDate,
)
from tokmo.values.toss_investor_trading import (
    InvestorTradingVolume,
    StockInvestorTradingRecord,
    StockInvestorTradingResponse,
)
from tokmo.values.toss_order_info import (
    BuyingPowerResponse,
    Commission,
    SellableQuantityResponse,
)

__all__ = ["parse_envelope"]


# =========================================================================
# Envelope loading
# =========================================================================


def _load_envelope(body: bytes):
    """Return the ``result`` value if the body is a valid envelope, else
    a sentinel ``_FAIL`` for parse errors and ``_WRONG`` for contract errors.
    """
    if not isinstance(body, (bytes, bytearray)):
        return _WRONG
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, UnicodeError):
        return _FAIL
    if not isinstance(payload, dict):
        return _WRONG
    if "result" not in payload:
        return _WRONG
    return payload["result"]


class _Fail:
    pass


class _Wrong:
    pass


_FAIL = _Fail()
_WRONG = _Wrong()


# =========================================================================
# Common field extraction helpers
# =========================================================================


def _req_str(obj: dict, key: str) -> str | None:
    """Required string that must be a non-empty ``str``."""
    v = obj.get(key)
    if not isinstance(v, str) or isinstance(v, bool) or not v:
        return None
    return v


def _req_decimal30(obj: dict, key: str) -> str | None:
    return _Decimal30.parse(obj.get(key))


def _req_datetime(obj: dict, key: str) -> object | None:
    return _AwareDateTime.parse(obj.get(key))


def _req_date(obj: dict, key: str) -> object | None:
    return _StrictDate.parse(obj.get(key))


def _req_int_nonbool(obj: dict, key: str) -> int | None:
    v = obj.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def _req_bool(obj: dict, key: str) -> bool | None:
    v = obj.get(key)
    if not isinstance(v, bool):
        return None
    return v


def _req_nullable_decimal30(obj: dict, key: str):
    """Required nullable decimal: the key must be present; None / value."""
    if key not in obj:
        return _WRONG
    v = obj[key]
    if v is None:
        return None
    parsed = _Decimal30.parse(v)
    return parsed if parsed is not None else _WRONG


def _req_nullable_datetime(obj: dict, key: str):
    if key not in obj:
        return _WRONG
    v = obj[key]
    if v is None:
        return None
    parsed = _AwareDateTime.parse(v)
    return parsed if parsed is not None else _WRONG


def _req_nullable_date(obj: dict, key: str):
    if key not in obj:
        return _WRONG
    v = obj[key]
    if v is None:
        return None
    parsed = _StrictDate.parse(v)
    return parsed if parsed is not None else _WRONG


def _opt_nullable_decimal30(obj: dict, key: str):
    """Optional nullable decimal: MISSING / None / value."""
    if key not in obj:
        return MISSING
    v = obj[key]
    if v is None:
        return None
    parsed = _Decimal30.parse(v)
    return parsed if parsed is not None else _WRONG


def _opt_nullable_datetime(obj: dict, key: str):
    if key not in obj:
        return MISSING
    v = obj[key]
    if v is None:
        return None
    parsed = _AwareDateTime.parse(v)
    return parsed if parsed is not None else _WRONG


def _opt_nullable_date(obj: dict, key: str):
    if key not in obj:
        return MISSING
    v = obj[key]
    if v is None:
        return None
    parsed = _StrictDate.parse(v)
    return parsed if parsed is not None else _WRONG


def _opt_nullable_raw(obj: dict, key: str):
    if key not in obj:
        return MISSING
    v = obj[key]
    if v is None:
        return None
    parsed = RawStr.parse(v)
    return parsed if parsed is not None else _WRONG


def _opt_nullable_str_no_max(obj: dict, key: str):
    """Optional nullable bounded string (cursor): MISSING / None / value."""
    if key not in obj:
        return MISSING
    v = obj[key]
    if v is None:
        return None
    if isinstance(v, str) and 1 <= len(v) <= 4096:
        return v
    return _WRONG


# =========================================================================
# Dispatch
# =========================================================================


def parse_envelope(body: bytes, operation: str, *, requested_symbols=None, status=None):
    """Dispatch to the schema-specific parser for ``operation``.

    Returns the typed value on success, ``_FAIL`` for invalid JSON/bytes,
    or ``_WRONG`` for contract mismatch.
    """
    result = _load_envelope(body)
    if result is _FAIL:
        return _FAIL
    if result is _WRONG:
        return _WRONG
    try:
        return _dispatch(
            operation, result, requested_symbols=requested_symbols, status=status
        )
    except Exception:
        return _WRONG


# =========================================================================
# Bundle 1 parsers
# =========================================================================


def _parse_orderbook(result) -> object:
    if not isinstance(result, dict):
        return _WRONG
    currency = _req_str(result, "currency")
    if currency is None:
        return _WRONG
    ts = _opt_nullable_datetime(result, "timestamp")
    if ts is _WRONG:
        return _WRONG
    asks = _parse_orderbook_entries(result.get("asks"))
    if asks is _WRONG:
        return _WRONG
    bids = _parse_orderbook_entries(result.get("bids"))
    if bids is _WRONG:
        return _WRONG
    return OrderbookResponse(currency=currency, asks=asks, bids=bids, timestamp=ts)


def _parse_orderbook_entries(raw) -> tuple[OrderbookEntry, ...] | object:
    if not isinstance(raw, list):
        return _WRONG
    entries = []
    for item in raw:
        if not isinstance(item, dict):
            return _WRONG
        price = _req_decimal30(item, "price")
        if price is None:
            return _WRONG
        volume = _req_decimal30(item, "volume")
        if volume is None:
            return _WRONG
        entries.append(OrderbookEntry(price=price, volume=volume))
    return tuple(entries)


def _parse_trades(result) -> object:
    if not isinstance(result, list):
        return _WRONG
    trades = []
    for item in result:
        if not isinstance(item, dict):
            return _WRONG
        price = _req_decimal30(item, "price")
        volume = _req_decimal30(item, "volume")
        ts = _req_datetime(item, "timestamp")
        currency = _req_str(item, "currency")
        if price is None or volume is None or ts is None or currency is None:
            return _WRONG
        trades.append(
            Trade(price=price, volume=volume, timestamp=ts, currency=currency)
        )
    return tuple(trades)


def _parse_price_limits(result) -> object:
    if not isinstance(result, dict):
        return _WRONG
    ts = _req_datetime(result, "timestamp")
    currency = _req_str(result, "currency")
    if ts is None or currency is None:
        return _WRONG
    upper = _opt_nullable_decimal30(result, "upperLimitPrice")
    lower = _opt_nullable_decimal30(result, "lowerLimitPrice")
    if upper is _WRONG or lower is _WRONG:
        return _WRONG
    return PriceLimitResponse(
        timestamp=ts,
        currency=currency,
        upper_limit_price=upper,
        lower_limit_price=lower,
    )


def _parse_stock_candles(result) -> object:
    if not isinstance(result, dict):
        return _WRONG
    candles = _parse_candles(result.get("candles"))
    if candles is _WRONG:
        return _WRONG
    nb = _opt_nullable_datetime(result, "nextBefore")
    if nb is _WRONG:
        return _WRONG
    return CandlePageResponse(candles=candles, next_before=nb)


def _parse_candles(raw) -> tuple[Candle, ...] | object:
    if not isinstance(raw, list):
        return _WRONG
    candles = []
    for item in raw:
        if not isinstance(item, dict):
            return _WRONG
        ts = _req_datetime(item, "timestamp")
        op = _req_decimal30(item, "openPrice")
        hp = _req_decimal30(item, "highPrice")
        lp = _req_decimal30(item, "lowPrice")
        cp = _req_decimal30(item, "closePrice")
        vol = _req_decimal30(item, "volume")
        cur = _req_str(item, "currency")
        if any(x is None for x in (ts, op, hp, lp, cp, vol, cur)):
            return _WRONG
        candles.append(
            Candle(
                timestamp=ts,
                open_price=op,
                high_price=hp,
                low_price=lp,
                close_price=cp,
                volume=vol,
                currency=cur,
            )
        )
    return tuple(candles)


def _parse_stocks(result, requested_symbols) -> object:
    if not isinstance(result, list):
        return _WRONG
    infos = []
    for item in result:
        if not isinstance(item, dict):
            return _WRONG
        si = _parse_stock_info(item)
        if si is _WRONG:
            return _WRONG
        infos.append(si)
    returned = tuple(si.symbol for si in infos)
    if len(set(returned)) != len(returned):
        return _WRONG
    if set(returned) != set(requested_symbols):
        return _WRONG
    return tuple(infos)


def _parse_stock_info(item: dict) -> StockInfo | object:
    symbol = _req_str(item, "symbol")
    name = _req_str(item, "name")
    english_name = _req_str(item, "englishName")
    isin = _req_str(item, "isinCode")
    market = _req_str(item, "market")
    sec_type = _req_str(item, "securityType")
    is_common = _req_bool(item, "isCommonShare")
    status = _req_str(item, "status")
    currency = _req_str(item, "currency")
    shares = _req_decimal30(item, "sharesOutstanding")
    if any(
        x is None
        for x in (
            symbol,
            name,
            english_name,
            isin,
            market,
            sec_type,
            is_common,
            status,
            currency,
            shares,
        )
    ):
        return _WRONG
    list_date = _opt_nullable_date(item, "listDate")
    delist_date = _opt_nullable_date(item, "delistDate")
    leverage = _opt_nullable_decimal30(item, "leverageFactor")
    if any(x is _WRONG for x in (list_date, delist_date, leverage)):
        return _WRONG
    kr_detail = MISSING
    if "koreanMarketDetail" in item:
        kmd = item["koreanMarketDetail"]
        if kmd is None:
            kr_detail = None
        else:
            parsed = _parse_kr_market_detail(kmd)
            if parsed is _WRONG:
                return _WRONG
            kr_detail = parsed
    return StockInfo(
        symbol=symbol,
        name=name,
        english_name=english_name,
        isin_code=isin,
        market=market,
        security_type=sec_type,
        is_common_share=is_common,
        status=status,
        currency=currency,
        shares_outstanding=shares,
        list_date=list_date,
        delist_date=delist_date,
        leverage_factor=leverage,
        korean_market_detail=kr_detail,
    )


def _parse_kr_market_detail(raw) -> KrMarketDetail | object:
    if not isinstance(raw, dict):
        return _WRONG
    liq = _req_bool(raw, "liquidationTrading")
    nxt = _req_bool(raw, "nxtSupported")
    krx_susp = _req_bool(raw, "krxTradingSuspended")
    if any(x is None for x in (liq, nxt, krx_susp)):
        return _WRONG
    nxt_susp = _opt_nullable_date(raw, "nxtTradingSuspended")
    # nxtTradingSuspended is boolean|null, not date — re-parse as bool|null
    if "nxtTradingSuspended" in raw:
        v = raw["nxtTradingSuspended"]
        if v is None:
            nxt_susp = None
        elif isinstance(v, bool):
            nxt_susp = v
        else:
            return _WRONG
    else:
        nxt_susp = MISSING
    return KrMarketDetail(
        liquidation_trading=liq,
        nxt_supported=nxt,
        krx_trading_suspended=krx_susp,
        nxt_trading_suspended=nxt_susp,
    )


def _parse_stock_warnings(result) -> object:
    if not isinstance(result, list):
        return _WRONG
    warnings = []
    for item in result:
        if not isinstance(item, dict):
            return _WRONG
        wt = _req_str(item, "warningType")
        if wt is None:
            return _WRONG
        exchange = _opt_nullable_raw(item, "exchange")
        start_date = _opt_nullable_date(item, "startDate")
        end_date = _opt_nullable_date(item, "endDate")
        if any(x is _WRONG for x in (exchange, start_date, end_date)):
            return _WRONG
        warnings.append(
            StockWarning(
                warning_type=wt,
                exchange=exchange,
                start_date=start_date,
                end_date=end_date,
            )
        )
    return tuple(warnings)


def _parse_exchange_rate(result) -> object:
    if not isinstance(result, dict):
        return _WRONG
    base = _req_str(result, "baseCurrency")
    quote = _req_str(result, "quoteCurrency")
    rate = _req_decimal30(result, "rate")
    mid = _req_decimal30(result, "midRate")
    bp = _req_decimal30(result, "basisPoint")
    rct = _req_str(result, "rateChangeType")
    vf = _req_datetime(result, "validFrom")
    vu = _req_datetime(result, "validUntil")
    if any(x is None for x in (base, quote, rate, mid, bp, rct, vf, vu)):
        return _WRONG
    return ExchangeRateResponse(
        base_currency=base,
        quote_currency=quote,
        rate=rate,
        mid_rate=mid,
        basis_point=bp,
        rate_change_type=rct,
        valid_from=vf,
        valid_until=vu,
    )


def _parse_kr_calendar(result) -> object:
    if not isinstance(result, dict):
        return _WRONG
    today = _parse_kr_market_day(result.get("today"))
    prev = _parse_kr_market_day(result.get("previousBusinessDay"))
    nxt = _parse_kr_market_day(result.get("nextBusinessDay"))
    if any(x is _WRONG for x in (today, prev, nxt)):
        return _WRONG
    return KrMarketCalendarResponse(
        today=today, previous_business_day=prev, next_business_day=nxt
    )


def _parse_kr_market_day(raw) -> KrMarketDay | object:
    if not isinstance(raw, dict):
        return _WRONG
    d = _req_date(raw, "date")
    if d is None:
        return _WRONG
    integrated = MISSING
    if "integrated" in raw:
        ival = raw["integrated"]
        if ival is None:
            integrated = None
        else:
            parsed = _parse_integrated_hour(ival)
            if parsed is _WRONG:
                return _WRONG
            integrated = parsed
    return KrMarketDay(date=d, integrated=integrated)


def _parse_integrated_hour(raw) -> IntegratedHour | object:
    if not isinstance(raw, dict):
        return _WRONG
    if not set(raw).issubset({"preMarket", "regularMarket", "afterMarket"}):
        return _WRONG
    pm = _parse_optional_session(raw, "preMarket")
    rm = _parse_optional_session(raw, "regularMarket")
    am = _parse_optional_session(raw, "afterMarket")
    if any(x is _WRONG for x in (pm, rm, am)):
        return _WRONG
    return IntegratedHour(pre_market=pm, regular_market=rm, after_market=am)


def _parse_optional_session(raw: dict, key: str):
    if key not in raw:
        return MISSING
    v = raw[key]
    if v is None:
        return None
    return _parse_session(v)


def _parse_session(raw) -> CalendarSession | object:
    if not isinstance(raw, dict):
        return _WRONG
    st = _req_datetime(raw, "startTime")
    et = _req_datetime(raw, "endTime")
    if st is None or et is None:
        return _WRONG
    spat = _opt_nullable_datetime(raw, "singlePriceAuctionStartTime")
    epat = _opt_nullable_datetime(raw, "singlePriceAuctionEndTime")
    if spat is _WRONG or epat is _WRONG:
        return _WRONG
    return CalendarSession(
        start_time=st,
        end_time=et,
        single_price_auction_start_time=spat,
        single_price_auction_end_time=epat,
    )


def _parse_us_calendar(result) -> object:
    if not isinstance(result, dict):
        return _WRONG
    today = _parse_us_market_day(result.get("today"))
    prev = _parse_us_market_day(result.get("previousBusinessDay"))
    nxt = _parse_us_market_day(result.get("nextBusinessDay"))
    if any(x is _WRONG for x in (today, prev, nxt)):
        return _WRONG
    return UsMarketCalendarResponse(
        today=today, previous_business_day=prev, next_business_day=nxt
    )


def _parse_us_market_day(raw) -> UsMarketDay | object:
    if not isinstance(raw, dict):
        return _WRONG
    known_sessions = {"dayMarket", "preMarket", "regularMarket", "afterMarket"}
    # Live market-day payloads carry metadata not shown in the OpenAPI example.
    # Accept unrelated metadata, but fail closed on a newly named Market field
    # so a future tradable session cannot disappear silently.
    if any(key.endswith("Market") and key not in known_sessions for key in raw):
        return _WRONG
    d = _req_date(raw, "date")
    if d is None:
        return _WRONG
    dm = _parse_optional_session(raw, "dayMarket")
    pm = _parse_optional_session(raw, "preMarket")
    rm = _parse_optional_session(raw, "regularMarket")
    am = _parse_optional_session(raw, "afterMarket")
    if any(x is _WRONG for x in (dm, pm, rm, am)):
        return _WRONG
    return UsMarketDay(
        date=d, day_market=dm, pre_market=pm, regular_market=rm, after_market=am
    )


# =========================================================================
# Bundle 2 parsers
# =========================================================================


def _parse_rankings(result) -> object:
    if not isinstance(result, dict):
        return _WRONG
    ranked_at = _opt_nullable_datetime(result, "rankedAt")
    if ranked_at is _WRONG:
        return _WRONG
    raw_list = result.get("rankings")
    if not isinstance(raw_list, list):
        return _WRONG
    items = []
    for item in raw_list:
        if not isinstance(item, dict):
            return _WRONG
        ri = _parse_ranking_item(item)
        if ri is _WRONG:
            return _WRONG
        items.append(ri)
    return RankingResponse(rankings=tuple(items), ranked_at=ranked_at)


def _parse_ranking_item(item: dict) -> RankingItem | object:
    rank = _req_int_nonbool(item, "rank")
    symbol = _req_str(item, "symbol")
    currency = _req_str(item, "currency")
    tv = _req_decimal30(item, "tradingVolume")
    ta = _req_decimal30(item, "tradingAmount")
    if any(x is None for x in (rank, symbol, currency, tv, ta)):
        return _WRONG
    price_raw = item.get("price")
    if not isinstance(price_raw, dict):
        return _WRONG
    last_p = _req_decimal30(price_raw, "lastPrice")
    base_p = _req_decimal30(price_raw, "basePrice")
    if last_p is None or base_p is None:
        return _WRONG
    change = _opt_nullable_decimal30(price_raw, "changeRate")
    if change is _WRONG:
        return _WRONG
    return RankingItem(
        rank=rank,
        symbol=symbol,
        currency=currency,
        price=RankingPrice(last_price=last_p, base_price=base_p, change_rate=change),
        trading_volume=tv,
        trading_amount=ta,
    )


def _parse_indicator_prices(result, requested_symbols) -> object:
    if not isinstance(result, list):
        return _WRONG
    items = []
    for item in result:
        if not isinstance(item, dict):
            return _WRONG
        symbol = _req_str(item, "symbol")
        last_price = _req_decimal30(item, "lastPrice")
        if symbol is None or last_price is None:
            return _WRONG
        ts = _opt_nullable_datetime(item, "timestamp")
        if ts is _WRONG:
            return _WRONG
        items.append(
            MarketIndicatorPrice(symbol=symbol, last_price=last_price, timestamp=ts)
        )
    returned = tuple(mi.symbol for mi in items)
    if len(set(returned)) != len(returned):
        return _WRONG
    if set(returned) != set(requested_symbols):
        return _WRONG
    return tuple(items)


def _parse_indicator_candles(result) -> object:
    if not isinstance(result, dict):
        return _WRONG
    candles_raw = result.get("candles")
    if not isinstance(candles_raw, list):
        return _WRONG
    candles = []
    for item in candles_raw:
        if not isinstance(item, dict):
            return _WRONG
        ts = _req_datetime(item, "timestamp")
        op = _req_decimal30(item, "openPrice")
        hp = _req_decimal30(item, "highPrice")
        lp = _req_decimal30(item, "lowPrice")
        cp = _req_decimal30(item, "closePrice")
        vol = _req_decimal30(item, "volume")
        if any(x is None for x in (ts, op, hp, lp, cp, vol)):
            return _WRONG
        candles.append(
            MarketIndicatorCandle(
                timestamp=ts,
                open_price=op,
                high_price=hp,
                low_price=lp,
                close_price=cp,
                volume=vol,
            )
        )
    nb = _opt_nullable_datetime(result, "nextBefore")
    if nb is _WRONG:
        return _WRONG
    return MarketIndicatorCandlePageResponse(candles=tuple(candles), next_before=nb)


def _parse_stock_investor_trading(result) -> object:
    if not isinstance(result, dict) or not isinstance(result.get("records"), list):
        return _WRONG

    def volume(raw):
        if not isinstance(raw, dict):
            return _WRONG
        values = [raw.get(k) for k in ("buyVolume", "sellVolume", "netBuyVolume")]
        if any(
            not isinstance(v, str) or len(v) > 30 or re.fullmatch(r"-?\d+", v) is None
            for v in values
        ):
            return _WRONG
        return InvestorTradingVolume(*values)

    records = []
    for row in result["records"]:
        if not isinstance(row, dict):
            return _WRONG
        day, at = _req_date(row, "date"), _req_datetime(row, "updatedAt")
        personal = row.get("individual", MISSING)
        if personal is not None and personal is not MISSING:
            personal = volume(personal)
        foreign, institution = (
            volume(row.get("foreigner")),
            volume(row.get("institution")),
        )
        if (
            day is None
            or at is None
            or any(v is _WRONG for v in (personal, foreign, institution))
        ):
            return _WRONG
        records.append(
            StockInvestorTradingRecord(day, at, personal, foreign, institution)
        )
    cursor = _opt_nullable_date(result, "nextUntil")
    if cursor is _WRONG:
        return _WRONG
    return StockInvestorTradingResponse(tuple(records), cursor)


def _parse_investor_trading(result) -> object:
    if not isinstance(result, dict):
        return _WRONG
    records_raw = result.get("records")
    if not isinstance(records_raw, list):
        return _WRONG
    records = []
    for item in records_raw:
        rec = _parse_investor_trading_record(item)
        if rec is _WRONG:
            return _WRONG
        records.append(rec)
    nu = _opt_nullable_date(result, "nextUntil")
    if nu is _WRONG:
        return _WRONG
    return InvestorTradingResponse(records=tuple(records), next_until=nu)


def _parse_investor_trading_record(item) -> InvestorTradingRecord | object:
    if not isinstance(item, dict):
        return _WRONG
    d = _req_date(item, "date")
    ua = _req_datetime(item, "updatedAt")
    if d is None or ua is None:
        return _WRONG
    ind = _parse_investor_amount(item.get("individual"))
    fr = _parse_investor_amount(item.get("foreigner"))
    inst_raw = item.get("institution")
    if not isinstance(inst_raw, dict):
        return _WRONG
    inst_buy = _req_decimal30(inst_raw, "buyAmount")
    inst_sell = _req_decimal30(inst_raw, "sellAmount")
    if inst_buy is None or inst_sell is None:
        return _WRONG
    breakdown_raw = inst_raw.get("breakdown")
    if not isinstance(breakdown_raw, dict):
        return _WRONG
    breakdown = _parse_institution_breakdown(breakdown_raw)
    if breakdown is _WRONG:
        return _WRONG
    oc = _parse_investor_amount(item.get("otherCorporation"))
    if any(x is _WRONG for x in (ind, fr, oc)):
        return _WRONG
    return InvestorTradingRecord(
        date=d,
        updated_at=ua,
        individual=ind,
        foreigner=fr,
        institution_buy_amount=inst_buy,
        institution_sell_amount=inst_sell,
        institution_breakdown=breakdown,
        other_corporation=oc,
    )


def _parse_investor_amount(raw) -> InvestorTradingAmount | object:
    if not isinstance(raw, dict):
        return _WRONG
    buy = _req_decimal30(raw, "buyAmount")
    sell = _req_decimal30(raw, "sellAmount")
    if buy is None or sell is None:
        return _WRONG
    return InvestorTradingAmount(buy_amount=buy, sell_amount=sell)


def _parse_institution_breakdown(raw: dict) -> InstitutionTradingBreakdown | object:
    amounts = {}
    for key in _INSTITUTION_BREAKDOWN_KEYS:
        if key not in raw:
            return _WRONG
        amt = _parse_investor_amount(raw[key])
        if amt is _WRONG:
            return _WRONG
        amounts[key] = amt
    return InstitutionTradingBreakdown(
        financial_investment=amounts["financialInvestment"],
        insurance=amounts["insurance"],
        trust=amounts["trust"],
        private_equity_fund=amounts["privateEquityFund"],
        bank=amounts["bank"],
        other_financial_institution=amounts["otherFinancialInstitution"],
        pension_fund=amounts["pensionFund"],
    )


# =========================================================================
# Bundle 3 parsers
# =========================================================================


def _parse_orders(result, status) -> object:
    if not isinstance(result, dict):
        return _WRONG
    orders_raw = result.get("orders")
    if not isinstance(orders_raw, list):
        return _WRONG
    orders = []
    for item in orders_raw:
        order = _parse_order(item)
        if order is _WRONG:
            return _WRONG
        orders.append(order)
    next_cursor_raw = Opt.from_json(result, "nextCursor")
    has_next_raw = result.get("hasNext")
    if not isinstance(has_next_raw, bool):
        return _WRONG
    # Determine next_cursor state. PaginatedOrderResponse requires the
    # nextCursor key (nullable): an absent key is a contract mismatch.
    if next_cursor_raw is MISSING:
        return _WRONG
    if next_cursor_raw is None:
        next_cursor = None
        cursor_present = False
    else:
        if not isinstance(next_cursor_raw, str) or not next_cursor_raw:
            return _WRONG
        next_cursor = next_cursor_raw
        cursor_present = True
    # Pagination invariants
    if status == "OPEN":
        if cursor_present or has_next_raw:
            return _WRONG
    else:  # CLOSED
        if has_next_raw != cursor_present:
            return _WRONG
    return PaginatedOrderResponse(
        orders=tuple(orders), next_cursor=next_cursor, has_next=has_next_raw
    )


def _parse_order(item) -> Order | object:
    if not isinstance(item, dict):
        return _WRONG
    order_id = _req_str(item, "orderId")
    symbol = _req_str(item, "symbol")
    side = _req_str(item, "side")
    ot = _req_str(item, "orderType")
    tif = _req_str(item, "timeInForce")
    status = _req_str(item, "status")
    qty = _req_decimal30(item, "quantity")
    currency = _req_str(item, "currency")
    ordered = _req_datetime(item, "orderedAt")
    if any(
        x is None
        for x in (order_id, symbol, side, ot, tif, status, qty, currency, ordered)
    ):
        return _WRONG
    price = _opt_nullable_decimal30(item, "price")
    oa = _opt_nullable_decimal30(item, "orderAmount")
    canceled = _opt_nullable_datetime(item, "canceledAt")
    if any(x is _WRONG for x in (price, oa, canceled)):
        return _WRONG
    exec_raw = item.get("execution")
    if not isinstance(exec_raw, dict):
        return _WRONG
    execution = _parse_execution(exec_raw)
    if execution is _WRONG:
        return _WRONG
    return Order(
        order_id=order_id,
        symbol=symbol,
        side=side,
        order_type=ot,
        time_in_force=tif,
        status=status,
        quantity=qty,
        currency=currency,
        ordered_at=ordered,
        execution=execution,
        price=price,
        order_amount=oa,
        canceled_at=canceled,
    )


def _parse_execution(raw: dict) -> OrderExecution | object:
    # All seven OrderExecution keys are canonically required; the six
    # nullable values may be None but the keys must be present.
    fq = _req_decimal30(raw, "filledQuantity")
    if fq is None:
        return _WRONG
    afp = _req_nullable_decimal30(raw, "averageFilledPrice")
    fa = _req_nullable_decimal30(raw, "filledAmount")
    comm = _req_nullable_decimal30(raw, "commission")
    tax = _req_nullable_decimal30(raw, "tax")
    fat = _req_nullable_datetime(raw, "filledAt")
    sd = _req_nullable_date(raw, "settlementDate")
    if any(x is _WRONG for x in (afp, fa, comm, tax, fat, sd)):
        return _WRONG
    return OrderExecution(
        filled_quantity=fq,
        average_filled_price=afp,
        filled_amount=fa,
        commission=comm,
        tax=tax,
        filled_at=fat,
        settlement_date=sd,
    )


def _parse_order_detail(result) -> object:
    if not isinstance(result, dict):
        return _WRONG
    return _parse_order(result)


def _parse_conditional_orders(result, status) -> object:
    if not isinstance(result, dict):
        return _WRONG
    co_raw = result.get("conditionalOrders")
    if not isinstance(co_raw, list):
        return _WRONG
    items = []
    for item in co_raw:
        detail = _parse_conditional_order_detail_obj(item)
        if detail is _WRONG:
            return _WRONG
        items.append(detail)
    has_next_raw = result.get("hasNext")
    if not isinstance(has_next_raw, bool):
        return _WRONG
    next_cursor_raw = Opt.from_json(result, "nextCursor")
    # Determine cursor state for hasNext invariant
    if next_cursor_raw is MISSING:
        next_cursor = MISSING
        cursor_present_non_null = False
    elif next_cursor_raw is None:
        next_cursor = None
        cursor_present_non_null = False
    else:
        if not isinstance(next_cursor_raw, str) or not next_cursor_raw:
            return _WRONG
        next_cursor = next_cursor_raw
        cursor_present_non_null = True
    # hasNext ↔ (nextCursor present and non-null)
    if has_next_raw != cursor_present_non_null:
        return _WRONG
    return PaginatedConditionalOrderResponse(
        conditional_orders=tuple(items), has_next=has_next_raw, next_cursor=next_cursor
    )


def _parse_conditional_order_detail(result) -> object:
    if not isinstance(result, dict):
        return _WRONG
    return _parse_conditional_order_detail_obj(result)


def _parse_conditional_order_detail_obj(
    item,
) -> ConditionalOrderDetailResponse | object:
    if not isinstance(item, dict):
        return _WRONG
    coid = _req_str(item, "conditionalOrderId")
    typ = _req_str(item, "type")
    status = _req_str(item, "status")
    symbol = _req_str(item, "symbol")
    market = _req_str(item, "market")
    qty = _req_decimal30(item, "quantity")
    ot = _req_str(item, "orderType")
    created = _req_datetime(item, "createdAt")
    if any(x is None for x in (coid, typ, status, symbol, market, qty, ot, created)):
        return _WRONG
    expire = _opt_nullable_date(item, "expireDate")
    if expire is _WRONG:
        return _WRONG
    first_raw = item.get("first")
    if not isinstance(first_raw, dict):
        return _WRONG
    first = _parse_conditional_condition(first_raw)
    if first is _WRONG:
        return _WRONG
    second = MISSING
    if "second" in item:
        sv = item["second"]
        if sv is None:
            second = None
        else:
            parsed = _parse_conditional_condition(sv)
            if parsed is _WRONG:
                return _WRONG
            second = parsed
    return ConditionalOrderDetailResponse(
        conditional_order_id=coid,
        type=typ,
        status=status,
        symbol=symbol,
        market=market,
        quantity=qty,
        order_type=ot,
        first=first,
        created_at=created,
        expire_date=expire,
        second=second,
    )


def _parse_conditional_condition(raw: dict) -> ConditionalOrderCondition | object:
    if not isinstance(raw, dict):
        return _WRONG
    typ = _req_str(raw, "type")
    status = _req_str(raw, "status")
    if typ is None or status is None:
        return _WRONG
    side = Opt.from_json(raw, "orderSide")
    if side is not MISSING and side is not None and side not in ("BUY", "SELL"):
        return _WRONG
    tp = _opt_nullable_decimal30(raw, "triggerPrice")
    tpr = _opt_nullable_decimal30(raw, "targetProfitRate")
    op = _opt_nullable_decimal30(raw, "orderPrice")
    toi = _opt_nullable_raw(raw, "triggeredOrderId")
    if any(x is _WRONG for x in (tp, tpr, op, toi)):
        return _WRONG
    return ConditionalOrderCondition(
        type=typ,
        status=status,
        order_side=side,
        trigger_price=tp,
        target_profit_rate=tpr,
        order_price=op,
        triggered_order_id=toi,
    )


# =========================================================================
# Dispatch table
# =========================================================================


def _parse_buying_power(result):
    if not isinstance(result, dict):
        return _WRONG
    currency = _req_str(result, "currency")
    amount = _req_decimal30(result, "cashBuyingPower")
    return (
        BuyingPowerResponse(currency, amount)
        if currency and amount is not None
        else _WRONG
    )


def _parse_sellable_quantity(result):
    if not isinstance(result, dict):
        return _WRONG
    amount = _req_decimal30(result, "sellableQuantity")
    return SellableQuantityResponse(amount) if amount is not None else _WRONG


def _parse_commissions(result):
    if not isinstance(result, list):
        return _WRONG
    rows = []
    for item in result:
        if not isinstance(item, dict):
            return _WRONG
        market = _req_str(item, "marketCountry")
        rate = _req_decimal30(item, "commissionRate")
        if not market or rate is None:
            return _WRONG
        try:
            start = (
                date.fromisoformat(item["startDate"])
                if item.get("startDate") is not None
                else None
            )
            end = (
                date.fromisoformat(item["endDate"])
                if item.get("endDate") is not None
                else None
            )
        except (ValueError, TypeError):
            return _WRONG
        rows.append(Commission(market, rate, start, end))
    return tuple(rows)


def _dispatch(operation: str, result, *, requested_symbols=None, status=None):
    """Dispatch to the schema-specific parser for ``operation``.

    Returns the typed value on success, ``_FAIL`` for invalid JSON/bytes,
    or ``_WRONG`` for contract mismatch.
    """
    simple = {
        "BUYING_POWER": _parse_buying_power,
        "SELLABLE_QUANTITY": _parse_sellable_quantity,
        "COMMISSIONS": _parse_commissions,
        "ORDERBOOK": _parse_orderbook,
        "TRADES": _parse_trades,
        "PRICE_LIMITS": _parse_price_limits,
        "STOCK_CANDLES": _parse_stock_candles,
        "STOCK_WARNINGS": _parse_stock_warnings,
        "EXCHANGE_RATE": _parse_exchange_rate,
        "KR_MARKET_CALENDAR": _parse_kr_calendar,
        "US_MARKET_CALENDAR": _parse_us_calendar,
        "RANKINGS": _parse_rankings,
        "INDICATOR_CANDLES": _parse_indicator_candles,
        "INDICATOR_INVESTOR_TRADING": _parse_investor_trading,
        "STOCK_INVESTOR_TRADING": _parse_stock_investor_trading,
        "ORDER_DETAIL": _parse_order_detail,
        "CONDITIONAL_ORDER_DETAIL": _parse_conditional_order_detail,
    }
    if operation in simple:
        return simple[operation](result)
    if operation == "STOCKS":
        return _parse_stocks(result, requested_symbols)
    if operation == "INDICATOR_PRICES":
        return _parse_indicator_prices(result, requested_symbols)
    if operation == "ORDERS":
        return _parse_orders(result, status)
    if operation == "CONDITIONAL_ORDERS":
        return _parse_conditional_orders(result, status)
    return _WRONG
