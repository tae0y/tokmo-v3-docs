"""Recent closing prices for display, independent of Quant analysis.

Uses the existing broker calendar and authenticated Pulse GET reader. A small
JSON cache holds ten completed bars per quoted symbol; no new daemon or auth.
"""

import json
import sqlite3
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from tokmo.records.quant_decision_reports import atomic_private


def instant(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp requires timezone")
    return result


def positive(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def read_cache(root):
    try:
        value = json.loads((Path(root) / "stores/price_reference.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _rows(path, query):
    if not path.is_file():
        return []
    try:
        connection = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1
        )
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute(query)]
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return []


def read_calendar(root, *, at, cached=None):
    root, at = Path(root), instant(at)
    cached = read_cache(root) if cached is None else cached
    days = {}
    for row in cached.get("calendar_days", []):
        try:
            if instant(row["fetched_at"]) <= at:
                days[(row["market"], row["trade_date"])] = row
        except (KeyError, TypeError, ValueError):
            continue
    for row in _rows(
        root / "ops/calendar_lookahead.sqlite3",
        "SELECT payload_json FROM calendar_lookahead",
    ):
        try:
            day = json.loads(row["payload_json"])
            if instant(day["fetched_at"]) <= at:
                days[(day["market"], day["trade_date"])] = day
        except (KeyError, TypeError, ValueError):
            continue
    history = {"KR": {}, "US": {}}
    for row in _rows(
        root / "ops/regular_session_history.sqlite3",
        "SELECT market,local_date,session,captured_at FROM regular_session_history",
    ):
        try:
            if row["market"] in history and instant(row["captured_at"]) <= at:
                history[row["market"]][row["local_date"]] = row["session"] == "regular"
        except (KeyError, TypeError, ValueError):
            continue
    for (market, trade_date), day in days.items():
        if market in history:
            history[market][trade_date] = any(
                s["kind"] == "regularMarket" for s in day["sessions"]
            )
    return dict(days=list(days.values()), history=history)


def quote_session(calendar, market, price_at):
    """Broker trade date includes US day trading before New York midnight."""
    when = instant(price_at)
    matches = []
    for day in calendar.get("days", []):
        if day["market"] != market:
            continue
        if any(
            instant(s["start"]) <= when <= instant(s["end"]) for s in day["sessions"]
        ):
            matches.append(day["trade_date"])
    if not matches:
        return dict(trade_date=None, previous_date=None)
    trade_date = max(matches)
    history = calendar.get("history", {}).get(market, {})
    candidate = date.fromisoformat(trade_date)
    for _ in range(30):
        candidate -= timedelta(days=1)
        status = history.get(candidate.isoformat())
        if status is True:
            return dict(trade_date=trade_date, previous_date=candidate.isoformat())
        if status is None:
            break  # Missing calendar evidence is not a confirmed holiday.
    return dict(trade_date=trade_date, previous_date=None)


def comparison_reference(*, market, price_at, currency, bars, calendar, at):
    session = quote_session(calendar, market, price_at)
    trade_date, expected = session["trade_date"], session["previous_date"]
    # Legacy observations can still show their actual dated comparison while
    # the calendar is unavailable. Never label this fallback as a normal day.
    upper = (
        trade_date
        or instant(price_at)
        .astimezone(ZoneInfo("Asia/Seoul" if market == "KR" else "America/New_York"))
        .date()
        .isoformat()
    )
    available = []
    for bar in bars:
        try:
            if (
                bar["date"] < upper
                and instant(bar["available_at"]) <= instant(at)
                and instant(bar["available_at"]) <= instant(price_at)
                and bar.get("currency", currency) == currency
                and positive(bar.get("close"))
            ):
                available.append(bar)
        except (KeyError, ValueError, TypeError):
            continue
    reference = max(available, key=lambda b: b["date"]) if available else None
    status = (
        "OK"
        if reference and reference["date"] == expected
        else "STALE"
        if expected and reference
        else "MISSING"
        if expected
        else "CALENDAR_UNKNOWN"
    )
    return dict(session, status=status, bar=reference)


def capture_price_references(root, fetch, *, now):
    """At most three GETs per normal Pulse; successful dates are reused.

    A failed symbol/date gets at most three attempts, five/fifteen minutes
    apart. No auth issuance, queues, orders, model calls, or message delivery.
    """
    from tokmo.facts.toss_reads import TossReadOperation as Op

    root, now = Path(root), instant(now)
    cache = read_cache(root)
    before = json.dumps(cache, sort_keys=True)
    calendar = read_calendar(root, at=now, cached=cache)
    cutoff = (now - timedelta(days=14)).date().isoformat()
    cache["calendar_days"] = sorted(
        (d for d in calendar["days"] if d["trade_date"] >= cutoff),
        key=lambda d: (d["market"], d["trade_date"]),
    )
    records = cache.setdefault("instruments", {})
    failures, processed = [], 0
    quotes = _rows(
        root / "stores/latest_quotes.sqlite3",
        "SELECT symbol,currency_raw,provider_timestamp,fetched_at FROM latest_quotes "
        "WHERE source='toss-current-price-v1' ORDER BY symbol",
    )
    if cache.get("rate_limit_until") and instant(cache["rate_limit_until"]) > now:
        quotes = []
    for quote in quotes:
        market = {"KRW": "KR", "USD": "US"}.get(quote["currency_raw"])
        observed = quote["provider_timestamp"] or quote["fetched_at"]
        if not market or not observed:
            continue
        if not timedelta(0) <= now - instant(observed) <= timedelta(days=4):
            continue
        basis = quote_session(calendar, market, observed)
        if not basis["previous_date"]:
            continue
        key = market + ":" + quote["symbol"]
        record = records.get(key, {})
        reference = comparison_reference(
            market=market,
            price_at=observed,
            currency=quote["currency_raw"],
            bars=record.get("bars", []),
            calendar=calendar,
            at=now,
        )
        if reference["status"] == "OK":
            continue
        target = basis["trade_date"] + ":" + basis["previous_date"]
        attempt = (
            record.get("attempt", {})
            if record.get("attempt", {}).get("target") == target
            else {}
        )
        if attempt.get("count", 0) >= 3 or (
            attempt.get("next_at") and instant(attempt["next_at"]) > now
        ):
            continue
        if processed >= 3 or getattr(fetch, "auth_failed", False):
            break
        calls_before = len(getattr(fetch, "calls", []))
        zone = ZoneInfo("Asia/Seoul" if market == "KR" else "America/New_York")
        boundary = datetime.combine(
            date.fromisoformat(basis["trade_date"]), time(), tzinfo=zone
        )
        try:
            page = fetch(
                Op.STOCK_CANDLES,
                symbol=quote["symbol"],
                interval="1d",
                count=10,
                adjusted=True,
                before=boundary,
            )
            bars = []
            for candle in page.candles:
                day = candle.timestamp.astimezone(zone).date()
                # All requested dates precede the quote's trade date. Normal
                # closing times are conservative for an earlier holiday close.
                closed = datetime.combine(
                    day, time(15, 30) if market == "KR" else time(16), tzinfo=zone
                )
                if (
                    day.isoformat() < basis["trade_date"]
                    and closed <= instant(observed)
                    and candle.currency == quote["currency_raw"]
                    and positive(candle.close_price)
                ):
                    bars.append(
                        dict(
                            date=day.isoformat(),
                            close=candle.close_price,
                            currency=candle.currency,
                            available_at=closed.isoformat(),
                            adjusted=True,
                        )
                    )
            # Preserve already captured dates if a partial provider response
            # omits them. Keep only the most recent ten completed bars.
            merged = {b["date"]: b for b in record.get("bars", [])}
            merged.update({b["date"]: b for b in bars})
            record = dict(
                record,
                bars=sorted(merged.values(), key=lambda b: b["date"])[-10:],
                captured_at=now.isoformat(),
                source="Toss GET /api/v1/candles",
            )
            check = comparison_reference(
                market=market,
                price_at=observed,
                currency=quote["currency_raw"],
                bars=record["bars"],
                calendar=calendar,
                at=now,
            )
            error = (
                None
                if check["status"] == "OK"
                else "previous_session_close_not_returned"
            )
        except Exception as exc:
            if hasattr(fetch, "calls") and len(fetch.calls) == calls_before:
                break  # No time/call budget left; this was not an API attempt.
            error = (
                str(exc)
                if str(exc) in ("auth_rejection", "forbidden", "http_429", "timeout")
                else type(exc).__name__
            )
        processed += 1
        count = attempt.get("count", 0) + 1
        record["attempt"] = dict(
            target=target,
            count=count,
            at=now.isoformat(),
            error=error,
            next_at=(now + timedelta(minutes=5 if count == 1 else 15)).isoformat()
            if error and count < 3
            else None,
        )
        records[key] = record
        if error:
            failures.append(dict(key=key, reason=error))
        if error == "http_429":
            calls = getattr(fetch, "calls", [])
            seconds = calls[-1].get("retry_after", 60) if calls else 60
            cache["rate_limit_until"] = (
                now + timedelta(seconds=max(60, seconds))
            ).isoformat()
            break
    if json.dumps(cache, sort_keys=True) != before:
        atomic_private(root / "stores/price_reference.json", cache)
    return dict(
        job="price_references",
        skipped=processed == 0,
        failures=failures,
        calls=getattr(fetch, "calls", []),
        authentication_issued=0,
    )
