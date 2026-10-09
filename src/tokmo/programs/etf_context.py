"""Shared issuer weights and dated NAV, with partial KIS evidence as fallback."""

import re
from copy import deepcopy
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from tokmo.records.kis_context import read_kis_fact, read_kis_integration

COMPONENT_MAX_AGE = timedelta(hours=24)
NAV_MAX_AGE = timedelta(minutes=5)


def _instant(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("timezone")
    return result


def _number(value, *, positive=False):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value, re.ASCII)
        or len(value) > 40
    ):
        raise ValueError("number")
    result = Decimal(value)
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError("number")
    return str(result)


def _fact(root, operation, symbol, at):
    raw, status = read_kis_fact(root, operation, symbol)
    if raw is None:
        return None, {"status": status}
    try:
        if any(
            raw.get(k) != v
            for k, v in dict(
                schema="kis-etf-v1",
                source="KIS",
                operation=operation,
                symbol=symbol,
                market="KR",
                currency="KRW",
            ).items()
        ):
            raise ValueError("identity")
        received = _instant(raw["received_at"])
        if received > at:
            return None, {"status": "UNAVAILABLE_AT_THIS_TIME"}
        rows = raw["rows"]
        if (
            not isinstance(rows, list)
            or len(rows) > 2000
            or any(not isinstance(r, dict) for r in rows)
        ):
            raise ValueError("rows")
        if raw.get("status") not in {"OK", "NO_DATA"} or (
            raw["status"] == "NO_DATA"
        ) != (not rows):
            raise ValueError("status")
        age = at - received
        return raw, dict(
            status="NO_DATA"
            if not rows
            else "STALE"
            if age > (COMPONENT_MAX_AGE if operation == "components" else NAV_MAX_AGE)
            else "REFERENCE_ONLY",
            received_at=received.isoformat(),
            source_date=None,
            freshness="UNVERIFIED",
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        return None, {"status": "INVALID"}


def read_etf_context(root, symbol, *, at):
    at = _instant(at)
    comp, components = _fact(root, "components", symbol, at)
    components.update(coverage="UNVERIFIED", whole_fund_weights_usable=False, rows=[])
    if comp is not None:
        try:
            count = comp.get("reported_component_count")
            count = None if count is None else Decimal(_number(count))
            if count is not None and (count < 0 or count != count.to_integral_value()):
                raise ValueError("count")
            rows = []
            for row in comp["rows"]:
                code, name = row.get("component_code"), row.get("component_name")
                if code is not None and (not isinstance(code, str) or len(code) > 40):
                    raise ValueError("code")
                if name is not None and (
                    not isinstance(name, str)
                    or len(name) > 160
                    or any(ord(c) < 32 for c in name)
                ):
                    raise ValueError("name")
                rows.append(dict(code=code, name=name, market=None))
            partial = (
                comp.get("coverage") == "PARTIAL"
                or comp.get("more_available") is True
                or (count is not None and count > len(rows))
            )
            components.update(
                coverage="PARTIAL" if partial else "UNVERIFIED",
                reported_component_count=None if count is None else int(count),
                returned_component_count=len(rows),
                rows=rows[:100] if components["status"] == "REFERENCE_ONLY" else [],
                rows_truncated=len(rows) > 100,
                weight_basis="PROVIDER_UNVERIFIED",
                note="조회된 일부 구성의 연관성만 참고. 누락은 노출 없음이 아니며 전체 비중·중복 노출량 계산 불가.",
            )
        except (ValueError, TypeError, InvalidOperation):
            components = dict(
                status="INVALID",
                coverage="UNVERIFIED",
                whole_fund_weights_usable=False,
                rows=[],
            )
    raw, nav = _fact(root, "nav", symbol, at)
    nav.update(
        usable_for_price_decision=False,
        current_gap_pct=None,
        note="원천 날짜 미확인. 수집 시각은 기준일이 아니며 현재가와 비교해 할인·할증을 판단할 수 없음.",
    )
    if raw is not None and raw["rows"]:
        try:
            for row in raw["rows"]:
                if not isinstance(row.get("source_time"), str) or not re.fullmatch(
                    r"\d{2}:\d{2}:\d{2}", row["source_time"]
                ):
                    raise ValueError("time")
                time.fromisoformat(row["source_time"])
            row = max(raw["rows"], key=lambda r: r["source_time"])
            nav.update(
                source_time=row["source_time"],
                nav=_number(row["nav"], positive=True),
                provider_price=_number(row["price"], positive=True),
                provider_gap_pct=None
                if row.get("provider_gap_pct") is None
                else _number(row["provider_gap_pct"]),
            )
        except (KeyError, ValueError, TypeError, InvalidOperation):
            nav = dict(
                status="INVALID", usable_for_price_decision=False, current_gap_pct=None
            )
    from tokmo.programs.etf_portfolio_context import (
        dated_nav_context,
        portfolio_context,
    )

    components = portfolio_context(root, symbol, at=at) or components
    nav = dated_nav_context(root, symbol, at=at) or nav
    return dict(
        source="KIS+ISSUER" if components.get("schema") == "etf-issuer-v1" else "KIS",
        symbol=symbol,
        market="KR",
        components=components,
        nav=nav,
    )


def news_etf_context(context):
    """Stable membership/quality only; minute NAV and receipt churn are not news."""
    result = deepcopy(context)
    from tokmo.programs.etf_portfolio_context import compact_components

    if result["components"].get("whole_fund_weights_usable"):
        result["components"] = compact_components(result["components"])
    result["components"].pop("received_at", None)
    # NAV quality travels to NEWS; numerical quote samples are display-only.
    result["nav"] = {
        k: v
        for k, v in result["nav"].items()
        if k in {"usable_for_price_decision", "note"}
    }
    return result


def connected_etf_symbols(root):
    """Configured quote targets are references, never proof of holdings."""
    try:
        config = read_kis_integration(root)
        symbols = config.get("symbols", []) if isinstance(config, dict) else []
        if isinstance(config, dict) and config.get("auto_holdings"):
            from tokmo.programs.etf_portfolio_context import read_document

            targets = read_document(root, "targets.json")
            if targets is not None:
                symbols = targets.get("symbols", symbols)
        if (
            not isinstance(symbols, list)
            or len(symbols) > 200
            or any(
                not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9]{6}", s)
                for s in symbols
            )
        ):
            return ()
        return tuple(dict.fromkeys(symbols))
    except (OSError, ValueError, TypeError):
        return ()


def attach_news_etf_context(root, result, *, at):
    selected = {
        h.get("symbol")
        for h in result.get("holdings", [])
        if h.get("market_raw") == "KR"
    }
    selected.update(connected_etf_symbols(root))
    selected.update(
        r["symbol"]
        for r in result.get("instruments", [])
        if r.get("market") == "KR" and r.get("kind") == "etf"
    )
    contexts = []
    for symbol in sorted(
        s for s in selected if isinstance(s, str) and re.fullmatch(r"[A-Z0-9]{6}", s)
    ):
        context = read_etf_context(root, symbol, at=at)
        if all(context[k]["status"] == "MISSING" for k in ("components", "nav")):
            continue
        context["holding_status"] = (
            "CONFIRMED_IN_SNAPSHOT"
            if any(
                h.get("market_raw") == "KR" and h.get("symbol") == symbol
                for h in result.get("holdings", [])
            )
            else "NOT_CONFIRMED"
        )
        contexts.append(context)
    if contexts:
        result["etf_context"] = [news_etf_context(c) for c in contexts]
        result["_etf_context"] = contexts


def attach_quant_etf_context(root, instruments, *, at, held_keys):
    """Replace only this projection. KIS never changes prices, quantities or rank."""
    contexts = {}
    for item in instruments:
        item["evidence"] = [
            e for e in item.get("evidence", []) if not e["id"].startswith("kis-etf:")
        ]
        if item.get("market") != "KR" or not re.fullmatch(
            r"[A-Z0-9]{6}", item.get("symbol", "")
        ):
            continue
        context = read_etf_context(root, item["symbol"], at=at)
        if all(context[k]["status"] == "MISSING" for k in ("components", "nav")):
            continue
        contexts[item["symbol"]] = context
        for operation, kind in [("components", "etf_components"), ("nav", "etf_nav")]:
            data = context[operation]
            item["evidence"].append(
                dict(
                    id=f"kis-etf:{item['symbol']}:{operation}",
                    kind=kind,
                    text=("ETF 구성: " + data.get("note", "자료 " + data["status"]))
                    if operation == "components"
                    else "KIS NAV: " + data.get("note", "자료 " + data["status"]),
                    available_at=data.get("received_at", _instant(at).isoformat()),
                    source=(data["source"] + " 운용사 구성 공시")
                    if data.get("schema") == "etf-issuer-v1"
                    else "한국투자증권 ETF 시세",
                    metadata=deepcopy(data),
                )
            )
    for etf, context in contexts.items():
        if "KR:" + etf not in held_keys:
            continue
        for row in context["components"]["rows"]:
            # The provider does not identify component market. Bind only an
            # exact local KR code AND official display name; no US collision.
            verified = context["components"].get("whole_fund_weights_usable") is True
            matches = [
                i
                for i in instruments
                if (
                    (verified and i.get("market") + ":" + i["symbol"] == row.get("key"))
                    or (
                        not verified
                        and i.get("market") == "KR"
                        and i.get("symbol") == row["code"]
                        and row["name"]
                        and i.get("name") == row["name"]
                    )
                )
                and i.get("symbol") != etf
            ]
            for item in matches:
                item["evidence"].append(
                    dict(
                        id=f"kis-etf:{etf}:indirect",
                        kind="etf_indirect",
                        text=(
                            f"보유 ETF {etf}에 {row.get('weight_pct')}% 편입. {context['components'].get('source_date')} 운용사 공시 기준."
                            if verified
                            else f"보유 ETF {etf}의 조회된 구성에 포함. 원천 기준일·전체 비중 미확인으로 정량 중복 노출 계산 불가."
                        ),
                        available_at=context["components"]["received_at"],
                        source=context["components"].get(
                            "source", "한국투자증권 ETF 시세"
                        ),
                        metadata=dict(
                            via_etf="KR:" + etf,
                            component=item["market"] + ":" + item["symbol"],
                            coverage=context["components"]["coverage"],
                            weight_pct=row.get("weight_pct") if verified else None,
                            whole_fund_weights_usable=verified,
                            source_date=context["components"].get("source_date"),
                        ),
                    )
                )
