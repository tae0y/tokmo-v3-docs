"""Refresh held ETFs in small batches on existing NEWS/Quant entry points."""

import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tokmo.programs.etf_portfolio_context import read_document
from tokmo.records.atomic_json import write_atomic_json
from tokmo.records.kis_state import KisState


def held_etfs(root, config, *, at):
    from tokmo.programs.quant_decision_inputs import load_local_snapshot

    followup = Path(root) / "investment_followup.json"
    settings = json.loads(followup.read_text()) if followup.is_file() else {}
    supplement = [
        Path(root) / "ops/quant-inputs/latest.json",
        *settings.get("supplements", []),
    ]
    supplement = [p for p in supplement if Path(p).is_file()]
    data = load_local_snapshot(
        config["credential_project_root"],
        root,
        as_of=at,
        supplement=supplement,
        pension_analysis=settings.get("pension_analysis"),
        include_context=False,
    )
    owned = {p["key"] for p in data["positions"] if float(p["quantity"]) > 0}
    return sorted(
        {
            i["symbol"]
            for i in data["instruments"]
            if i["kind"] == "etf"
            and i["market"] == "KR"
            and "KR:" + i["symbol"] in owned
        }
    )


def refresh_portfolios(root, config, *, at, client_factory, portfolio_fetch=None):
    root = Path(root)
    now = at if isinstance(at, datetime) else datetime.fromisoformat(at)
    local = now.astimezone(ZoneInfo("Asia/Seoul"))
    state = KisState(root / "kis")
    result = dict(
        status="CACHED", requests=0, auth_requests=0, issuer_requests=0, outcomes=[]
    )
    with state.locked("refresh"):
        symbols = held_etfs(root, config, at=now)
        write_atomic_json(
            root / "kis/targets.json",
            json.dumps(dict(symbols=symbols, as_of=now.isoformat())).encode(),
            max_bytes=32768,
        )
        saved = read_document(root, "portfolio-refresh.json") or {"attempts": {}}
        attempts = saved["attempts"]
        due = []
        market_window = local.weekday() < 5 and time(9) <= local.time().replace(
            tzinfo=None
        ) <= time(20, 1)
        for symbol in symbols:
            comp = read_document(root, "issuer-components-" + symbol + ".json")
            nav = read_document(root, "nav_daily-" + symbol + ".json")
            comp_due = comp is None or (
                market_window and comp["source_date"] < local.date().isoformat()
            )
            previous = attempts.get(symbol, {})
            if comp_due and previous.get("components_date") == local.date().isoformat():
                comp_due = False
            nav_due = nav is None or (
                market_window
                and (now - datetime.fromisoformat(nav["received_at"]))
                >= timedelta(minutes=5)
            )
            # One close capture after the polling window; retain dated data over
            # weekends. An empty cache can be initialized outside market hours.
            if (
                nav
                and local.weekday() < 5
                and local.time().replace(tzinfo=None) > time(20, 1)
            ):
                closing_capture = local.replace(
                    hour=20, minute=1, second=0, microsecond=0
                )
                if datetime.fromisoformat(nav["received_at"]) < closing_capture:
                    nav_due = True
            last = previous.get("nav_at")
            if last and now - datetime.fromisoformat(last) < timedelta(minutes=5):
                nav_due = False
            if comp_due or nav_due:
                due.append((previous.get("nav_at", ""), symbol, comp_due, nav_due))
        if not due:
            return dict(result, targets=len(symbols))
        # Rotate oldest attempts first so one unsupported ETF cannot starve others.
        batch = sorted(due)[:3]
        client = client_factory(
            project_root=Path(config["credential_project_root"]),
            state_root=root / "kis",
            max_requests=1 + 2 * len(batch),
        )
        for _, symbol, comp_due, nav_due in batch:
            previous = attempts.setdefault(symbol, {})
            if comp_due:
                previous["components_date"] = local.date().isoformat()
            if nav_due:
                previous["nav_at"] = now.isoformat()
            write_atomic_json(
                root / "kis/portfolio-refresh.json",
                json.dumps(saved).encode(),
                max_bytes=32768,
            )
            outcome = dict(symbol=symbol)
            if comp_due:
                try:
                    if portfolio_fetch is None:
                        raise ValueError("issuer transport not connected")
                    fact, raw = portfolio_fetch(symbol, at=local)
                    result["issuer_requests"] += fact.pop("read_requests", 1)
                    write_atomic_json(
                        root / "kis" / f"issuer-components-{symbol}.json",
                        json.dumps(fact, ensure_ascii=False).encode(),
                        max_bytes=3_000_000,
                    )
                    write_atomic_json(
                        root / "kis" / f"issuer-source-{symbol}.json",
                        json.dumps(
                            dict(
                                source_url=fact["source_url"],
                                received_at=fact["received_at"],
                                body=raw,
                            ),
                            ensure_ascii=False,
                        ).encode(),
                        max_bytes=3_000_000,
                    )
                    outcome["components"] = "UPDATED"
                except Exception as error:
                    outcome["components"] = "FAILED:" + type(error).__name__
            if nav_due:
                try:
                    client.fetch("nav_daily", symbol)
                    client.fetch("nav", symbol)
                    outcome["nav"] = "UPDATED"
                except Exception as error:
                    outcome["nav"] = "FAILED:" + getattr(
                        error, "code", type(error).__name__
                    )
            result["outcomes"].append(outcome)
        result.update(
            status="UPDATED"
            if all(
                not str(v).startswith("FAILED:")
                for o in result["outcomes"]
                for v in o.values()
            )
            else "DEGRADED",
            requests=client.requests,
            auth_requests=client.auth_requests,
            targets=len(symbols),
            pending=max(0, len(due) - len(batch)),
        )
        saved["last_run"] = dict(result, at=now.isoformat())
        write_atomic_json(
            root / "kis/portfolio-refresh.json",
            json.dumps(saved).encode(),
            max_bytes=32768,
        )
    return result
