"""Dated ETF valuation and local look-through calculations, compact model input."""

import json
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from tokmo.records.atomic_json import read_atomic_json


def read_document(root, name):
    try:
        return json.loads(
            read_atomic_json(Path(root) / "kis" / name, max_bytes=3_000_000)
        )
    except (OSError, ValueError):
        return None


def portfolio_context(root, symbol, *, at):
    d = read_document(root, "issuer-components-" + symbol + ".json")
    if not d or d.get("symbol") != symbol or d.get("schema") != "etf-issuer-v1":
        return None
    try:
        received = datetime.fromisoformat(d["received_at"])
        source = datetime.fromisoformat(d["source_date"]).replace(
            tzinfo=ZoneInfo("Asia/Seoul")
        )
        rows = d["rows"]
        if received > at or source > at or not rows or len(rows) > 2000:
            return None
        total = sum((Decimal(r["weight_pct"]) for r in rows), Decimal(0))
        if not total.is_finite() or not Decimal(95) <= total <= Decimal(105):
            return None
        if any(not Decimal(r["weight_pct"]).is_finite() for r in rows):
            return None
        stale = at - source > timedelta(days=7)
        return dict(
            d,
            status="STALE" if stale else "DATED",
            whole_fund_weights_usable=bool(d.get("whole_fund_weights_usable"))
            and not stale,
            note="운용사 공시 구성 비중 기준 추정. 공시 이후 가격·리밸런싱과 재간접 ETF 내부 구성에 따라 실제 노출은 달라질 수 있음.",
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


def dated_nav_context(root, symbol, *, at):
    daily = read_document(root, "nav_daily-" + symbol + ".json")
    if (
        not daily
        or daily.get("symbol") != symbol
        or daily.get("operation") != "nav_daily"
    ):
        return None
    try:
        if datetime.fromisoformat(daily["received_at"]) > at:
            return None
        today = at.astimezone(ZoneInfo("Asia/Seoul")).date()
        rows = [
            r
            for r in daily["rows"]
            if datetime.fromisoformat(r["source_date"]).date() <= today
        ]
        row = max(rows, key=lambda r: r["source_date"])
        nav, price = Decimal(row["nav"]), Decimal(row["price"])
        if not nav.is_finite() or not price.is_finite() or min(nav, price) <= 0:
            return None
        gap = str(((price / nav - 1) * 100).quantize(Decimal(".01")))
        result = dict(
            status="DATED_REFERENCE",
            source="KIS",
            source_date=row["source_date"],
            source_at=None,
            source_time=None,
            date_basis="KIS_DAILY_SOURCE_DATE",
            received_at=daily["received_at"],
            nav=str(nav),
            provider_price=str(price),
            provider_gap_pct=row.get("provider_gap_pct"),
            current_gap_pct=gap,
            usable_for_valuation=True,
            usable_for_price_decision=False,
            note="같은 일별 응답의 가격·NAV로 계산한 괴리율. 장중 현재가와 전일 NAV를 섞지 않음.",
        )
        minute = read_document(root, "nav-" + symbol + ".json")
        if (
            minute
            and minute.get("symbol") == symbol
            and datetime.fromisoformat(minute["received_at"]) <= at
        ):
            # Attach a date only with an explicitly dated matching observation.
            # If reads straddle a changing tick, use the dated daily result.
            latest = max(
                minute.get("rows", []), key=lambda r: r["source_time"], default=None
            )
            if (
                latest
                and Decimal(latest["nav"]) == nav
                and Decimal(latest["price"]) == price
            ):
                source_at = datetime.fromisoformat(
                    row["source_date"] + "T" + latest["source_time"]
                ).replace(tzinfo=ZoneInfo("Asia/Seoul"))
                if source_at <= at and source_at <= datetime.fromisoformat(
                    minute["received_at"]
                ):
                    result.update(
                        source_at=source_at.isoformat(),
                        source_time=latest["source_time"],
                        date_basis="KIS_DAILY_DATE_AND_MATCHING_TIME_SERIES",
                        received_at=minute["received_at"],
                        observation_age_minutes=round(
                            (at - source_at).total_seconds() / 60, 1
                        ),
                        usable_for_price_decision=at - source_at
                        <= timedelta(minutes=5),
                    )
                    if result["usable_for_price_decision"]:
                        result["status"] = "INTRADAY"
        if (today - datetime.fromisoformat(row["source_date"]).date()).days > 7:
            result.update(
                status="STALE",
                usable_for_valuation=False,
                usable_for_price_decision=False,
            )
        return result
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


def compact_components(data, *, keys=(), limit=10):
    result = {
        k: v
        for k, v in data.items()
        if k not in {"rows", "received_at", "schema", "symbol"}
    }
    rows = data.get("rows", [])

    def weight(r):
        return abs(Decimal(r.get("weight_pct") or "0"))

    selected = sorted(rows, key=weight, reverse=True)[:limit]
    selected_keys = {r.get("key") or r.get("code") for r in selected}
    for r in rows:
        key = r.get("key")
        if key in keys and key not in selected_keys:
            selected.append(r)
            selected_keys.add(key)
    result["rows"] = [
        {
            k: r.get(k)
            for k in ("key", "code", "name", "weight_pct", "asset_kind")
            if r.get(k) is not None
        }
        for r in selected
    ]
    classes = defaultdict(Decimal)
    for r in rows:
        if r.get("weight_pct") is not None:
            classes[r.get("asset_kind", "unknown")] += Decimal(r["weight_pct"])
    result["asset_weights_pct"] = {
        k: str(v.quantize(Decimal(".01"))) for k, v in classes.items()
    }
    result["full_row_count"] = len(rows)
    result["rows_truncated"] = len(selected) < len(rows)
    result["selection_note"] = (
        "전체 구성은 로컬 계산에 사용. 주요 비중과 관련 보유주만 표시."
    )
    return result


def portfolio_exposure(snapshot, *, scope, focus_key, related_keys=()):
    """Currency-aware one-level estimates. No invented look-through for funds."""
    instruments = {i.key: i for i in snapshot.instruments}
    by_key = defaultdict(lambda: dict(direct=Decimal(0), indirect=Decimal(0), via=[]))
    etfs = []
    total = Decimal(0)
    for p in snapshot.positions:
        if (scope is not None and p.scope != scope) or p.quantity <= 0:
            continue
        i = instruments.get(p.key)
        if i is None or i.price is None:
            continue
        value = p.quantity * i.price
        if i.currency == "USD":
            if snapshot.fx is None:
                continue
            value *= Decimal(str(snapshot.fx["rate"]))
        total += value
        by_key[p.key]["direct"] += value
        e = next(
            (
                e
                for e in i.evidence
                if e.kind == "etf_components"
                and e.metadata.get("whole_fund_weights_usable")
            ),
            None,
        )
        if e is None:
            continue
        price_at = getattr(i, "price_at", None)
        etfs.append(
            dict(
                key=p.key,
                name=i.name,
                value_krw=str(value),
                source_date=e.metadata.get("source_date"),
                price_at=price_at.isoformat() if price_at is not None else None,
            )
        )
        for r in e.metadata.get("rows", []):
            key = r.get("key")
            if not key or r.get("weight_pct") is None:
                continue
            amount = value * Decimal(r["weight_pct"]) / 100
            by_key[key]["indirect"] += amount
            by_key[key]["name"] = r["name"]
            if amount:
                by_key[key]["via"].append(
                    dict(
                        etf=p.key,
                        weight_pct=r["weight_pct"],
                        amount_krw=str(amount.quantize(Decimal(1))),
                        source_date=e.metadata.get("source_date"),
                    )
                )
    if not etfs:
        return None
    relevant = {focus_key, *related_keys} | {
        k for k, v in by_key.items() if v["direct"] and v["indirect"]
    }
    relevant.update(
        k
        for k, v in sorted(
            by_key.items(), key=lambda kv: kv[1]["indirect"], reverse=True
        )[:8]
    )
    result = []
    for key in relevant:
        v = by_key[key]
        if not v["indirect"]:
            continue
        amount = v["direct"] + v["indirect"]
        result.append(
            dict(
                key=key,
                name=v.get("name", key),
                direct_krw=str(v["direct"].quantize(Decimal(1))),
                via_etf_krw=str(v["indirect"].quantize(Decimal(1))),
                combined_krw=str(amount.quantize(Decimal(1))),
                pct_of_priced_holdings=str(
                    (amount / total * 100).quantize(Decimal(".01"))
                )
                if total
                else None,
                via=v["via"],
            )
        )
    return dict(
        basis="운용사 구성 비중 × 보유 ETF 평가금액의 추정치",
        denominator="해당 범위의 가격이 확인된 보유자산 평가액. 현금 제외.",
        priced_holdings_krw=str(total.quantize(Decimal(1))),
        etfs=etfs,
        overlaps=sorted(result, key=lambda r: Decimal(r["combined_krw"]), reverse=True),
        limitation="ETF 안의 다른 ETF는 한 단계 노출로 표시. 펀드와 그 내부 자산을 중복 합산하지 않음. 자료 기준일·가격 갱신시각 차이 존재.",
    )
