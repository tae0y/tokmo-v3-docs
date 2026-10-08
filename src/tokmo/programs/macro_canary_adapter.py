"""Shared production-DTO adapter for the bounded macro canary and shadow."""
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
from zoneinfo import ZoneInfo

from tokmo.values.macro_observations import (MacroCandleObservation,
    MacroDataCoverageV1, MacroInvestorObservation, MacroObservationSeriesV1,
    MacroSourceRefV1, compute_series_digest)
from tokmo.values.session_gate import Market
from tokmo.values.signal_program_lifecycle import ProgramKindV1,ProgramLifecycleSpecV1

_G1B_SPEC_DIGEST=hashlib.sha256(b"AI-MACRO-001|1.0.0|macro-divergence-v1|thresholds-closed-sds-2026-08-08|effective-2026-08-09").hexdigest()
_G1B_LIFECYCLE_SPEC=ProgramLifecycleSpecV1("AI-MACRO-001","1.0.0",ProgramKindV1.CORE_ANALYZER,datetime(2026,8,9,0,0,tzinfo=timezone.utc),datetime(2026,11,7,0,0,tzinfo=timezone.utc),None,90,_G1B_SPEC_DIGEST)


def normalize_macro_pages(series, pages, fetched_at, completed_session_date):
    if not isinstance(fetched_at, datetime) or fetched_at.tzinfo is None:
        raise ValueError("aware fetched_at required")
    if type(completed_session_date) is not date:
        raise ValueError("completed session required")
    kst = ZoneInfo("Asia/Seoul")
    rows = []
    for index, page in enumerate(pages):
        if index:
            validate_page_boundary(series, pages[index - 1], page)
        values = getattr(page, "records", getattr(page, "candles", ()))
        cursor = getattr(page, "next_until", getattr(page, "next_before", None))
        if not values and cursor is not None:
            raise ValueError("empty page with cursor")
        dates = []
        for value in values:
            day = value.date if hasattr(value, "date") else value.timestamp.astimezone(kst).date()
            dates.append(day)
        if len(dates) != len(set(dates)):
            raise ValueError("duplicate session date")
        if values and cursor is not None:
            minimum = min((v.date if hasattr(v, "date") else v.timestamp) for v in values)
            if type(cursor) is date:
                if cursor >= min(dates): raise ValueError("non-monotonic cursor")
            elif not isinstance(cursor, datetime) or cursor.tzinfo is None or cursor >= minimum:
                raise ValueError("non-monotonic cursor")
        for value, day in zip(values, dates):
            if day > completed_session_date:
                continue
            if hasattr(value, "date"):
                rows.append(MacroInvestorObservation(day, value.updated_at, fetched_at,
                    value.foreigner.buy_amount, value.foreigner.sell_amount,
                    value.individual.buy_amount, value.individual.sell_amount,
                    value.institution_buy_amount, value.institution_sell_amount))
            else:
                rows.append(MacroCandleObservation(day, value.timestamp, fetched_at,
                    value.open_price, value.high_price, value.low_price,
                    value.close_price, value.volume,
                    "KRW" if series == "KOSPI" else value.currency))
    rows.sort(key=lambda item: item.session_date)
    if len({r.session_date for r in rows}) != len(rows):
        raise ValueError("cross-page duplicate")
    return tuple(rows)


def validate_page_boundary(series, newer, older):
    def days(page):
        values = getattr(page, "records", getattr(page, "candles", ()))
        return tuple(v.date if hasattr(v, "date") else v.timestamp.astimezone(ZoneInfo("Asia/Seoul")).date() for v in values)
    nd, od = days(newer), days(older)
    if nd and od and max(od) >= min(nd):
        raise ValueError("page overlap")
    return True


def merge_macro_pages(series, pages, fetched_at, completed_session_date):
    return normalize_macro_pages(series, pages, fetched_at, completed_session_date)


def build_macro_bundle(kospi_pages, investor_pages, proxy_pages, *, fetched_at,
                       completed_session_date, as_of, calendar_dates=None):
    if not kospi_pages or not investor_pages or not proxy_pages:
        return None, "COVERAGE_CONTINUITY_UNPROVEN"
    k = merge_macro_pages("KOSPI", kospi_pages, fetched_at, completed_session_date)
    i = merge_macro_pages("INVESTOR", investor_pages, fetched_at, completed_session_date)
    p = merge_macro_pages("PROXY", proxy_pages, fetched_at, completed_session_date)
    continuity="COVERAGE_CONTINUITY_UNPROVEN"
    if (isinstance(calendar_dates,tuple) and len(calendar_dates)==2
            and type(calendar_dates[0]) is bool
            and isinstance(calendar_dates[1],frozenset)
            and all(type(day) is date for day in calendar_dates[1])
            and calendar_dates[0] is True):
        common={x.session_date for x in k}&{x.session_date for x in i}&{x.session_date for x in p}
        if calendar_dates[1]==common: continuity="CALENDAR_COMPLETE"
    refs = tuple(MacroSourceRefV1(op, sym, "1d", count, len(rows),
        rows[0].session_date if rows else None, rows[-1].session_date if rows else None,
        compute_series_digest(rows)) for op, sym, count, rows in (
        ("INDICATOR_CANDLES","KOSPI",200,k),
        ("INDICATOR_INVESTOR_TRADING","KOSPI",100,i),
        ("STOCK_CANDLES","261240",200,p)))
    return MacroObservationSeriesV1(k, p, i, as_of, refs), continuity


def compute_firing_rate_status(denominator, numerator, *, normative):
    if normative or not isinstance(denominator, int) or denominator <= 1:
        return "FIRING_RATE_UNPROVEN", None
    rate = Decimal(numerator) / Decimal(denominator)
    return "SHADOW_FORWARD_FIRING_RATE", format(rate, "f")


def resolve_completed_session(history_store, cache_store, now):
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("aware now required")
    import os,stat
    for store in (history_store,cache_store):
        path=getattr(store,"path",None)
        if path is not None:
            path=__import__("pathlib").Path(path)
            if path.is_symlink() or not path.is_file(): raise ValueError("session authority unsafe")
            info=path.stat()
            if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1: raise ValueError("session authority unsafe")
    local = now.astimezone(ZoneInfo("Asia/Seoul")).date()
    cache = cache_store.read()
    day = None if cache is None else cache.kr
    if day is None or day.trade_date != local.isoformat():
        raise ValueError("calendar authority unavailable")
    session = day.sessions.get("regularMarket")
    if session is None:
        raise ValueError("regular session unavailable")
    end = datetime.fromisoformat(session.end.replace("Z", "+00:00"))
    if end > now:
        raise ValueError("regular session incomplete")
    row = history_store.read(Market.KR, local)
    if row is None or row.get("session") != "regular" or row.get("local_date") != local:
        raise ValueError("history authority mismatch")
    return local
