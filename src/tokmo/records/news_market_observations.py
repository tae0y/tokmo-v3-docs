"""Read already-computed daily indicators. No refresh, trading or new store."""

import json
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from tokmo.records.news_decision_store import timestamp

# These are source-computed values, not numbers extracted by a language model.
METRICS = {
    "rsi14": "PERCENT",
    "rv20": "PERCENT",
    "drawdown": "RETURN",
    "range_position": "POSITION",
}


def read_market_observations(path, keys, *, at):
    keys = sorted(set(keys))
    if not keys:
        return {"status": "NO_TARGETS", "series": []}
    if not Path(path).is_file():
        return {"status": "MISSING", "series": []}
    now = timestamp(at)
    try:
        c = sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1
        )
        try:
            rows = c.execute(
                "SELECT c.market,c.symbol,r.identity_digest,r.fetched_at,r.evaluated_at,"
                "r.completed_session_date,r.result_payload FROM market_regime_shadow_result r "
                "JOIN market_regime_shadow_claim c ON c.identity_digest=r.identity_digest "
                "WHERE c.state='SUCCEEDED' AND c.market||':'||c.symbol IN ("
                + ",".join("?" for _ in keys)
                + ") "
                "AND julianday(r.fetched_at)<=julianday(?) AND julianday(r.evaluated_at)<=julianday(?) "
                "ORDER BY r.completed_session_date DESC,julianday(r.evaluated_at) DESC",
                keys + [now.isoformat(), now.isoformat()],
            ).fetchall()
        finally:
            c.close()
    except (sqlite3.Error, OSError):
        return {"status": "ERROR", "series": []}
    result = {}
    for market, symbol, identity, fetched, evaluated, session, payload in rows:
        key = market + ":" + symbol
        if key in result:
            continue
        try:
            value = json.loads(payload)
            if (
                value["symbol"] != symbol
                or value["kind"] != "CORE_ANALYZER"
                or value["as_of"] != session
                or value["last_session_date"] != session
                or session > now.date().isoformat()
            ):
                raise ValueError("identity/session")
            metrics = {}
            for component in value["components"]:
                for m in component["metrics"]:
                    name = m.get("name")
                    if name in METRICS and m.get("available") is True:
                        number = Decimal(str(m["value"]))
                        if (
                            m["unit"] != METRICS[name]
                            or not number.is_finite()
                            or name in metrics
                        ):
                            raise ValueError("invalid/conflicting measurement")
                        metrics[name] = {"value": str(number), "unit": m["unit"]}
            # This is an age guard, NOT a trading-calendar freshness assertion.
            old = (now.date() - date.fromisoformat(session)).days > 4
            result[key] = dict(
                key=key,
                status="AGE_LIMIT_EXCEEDED"
                if old
                else "DATED"
                if metrics
                else "NO_MEASUREMENTS",
                source_id="market-regime:" + identity,
                session=session,
                available_at=max(timestamp(fetched), timestamp(evaluated)).isoformat(),
                formula=value["formula_version"],
                adjustment_mode=value["adjustment_mode"],
                metrics=metrics,
            )
        except (ValueError, KeyError, TypeError, InvalidOperation):
            result[key] = dict(key=key, status="INVALID", metrics={})
    return dict(
        status="AVAILABLE",
        series=[result.get(k, dict(key=k, status="MISSING", metrics={})) for k in keys],
    )
