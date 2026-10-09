"""One bounded refresh shared by existing NEWS and Quant entrypoints.

Each due operation is claimed before I/O, including skipped work after a
failure. Neither concurrent minute ticks nor repeated questions reset it.
The polling window is not exchange-session or source-date verification.
"""

import re
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from tokmo.programs.etf_context import COMPONENT_MAX_AGE, NAV_MAX_AGE, read_etf_context
from tokmo.records.kis_context import read_kis_integration
from tokmo.records.kis_state import KisState
from tokmo.values.kis_etf import KisError


def _time(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("time")
    return result


def refresh_etf_inputs(root, *, at, client_factory, portfolio_fetch=None):
    result = dict(status="DISABLED", requests=0, auth_requests=0)
    client = None
    try:
        config = read_kis_integration(root)
        if config is None:
            return result
        if (
            isinstance(config, dict)
            and config.get("enabled")
            and config.get("auto_holdings") is True
        ):
            from tokmo.programs.etf_portfolio_refresh import refresh_portfolios

            return refresh_portfolios(
                root,
                config,
                at=at,
                client_factory=client_factory,
                portfolio_fetch=portfolio_fetch,
            )
        if (
            not isinstance(config, dict)
            or set(config) != {"enabled", "symbols", "credential_project_root"}
            or type(config["enabled"]) is not bool
        ):
            raise KisError("integration_config")
        if not config["enabled"]:
            return result
        symbols = config["symbols"]
        if (
            not isinstance(symbols, list)
            or not 1 <= len(symbols) <= 2
            or len(set(symbols)) != len(symbols)
            or any(
                not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9]{6}", s)
                for s in symbols
            )
        ):
            raise KisError("integration_symbols")
        if (
            not isinstance(config["credential_project_root"], str)
            or not Path(config["credential_project_root"]).is_absolute()
        ):
            raise KisError("credential_path")
        now = _time(at)
        local = now.astimezone(ZoneInfo("Asia/Seoul"))
        if local.weekday() >= 5 or not time(9) <= local.time().replace(
            tzinfo=None
        ) < time(15, 31):
            return dict(result, status="OUTSIDE_POLL_WINDOW")
        state = KisState(Path(root) / "kis")
        with state.locked("refresh"):
            saved = state.read_refresh_state()
            if (
                not isinstance(saved, dict)
                or not isinstance(saved.get("attempts"), dict)
                or any(
                    type(saved.get(k)) is not int or saved[k] < 0
                    for k in ("total_requests", "total_auth_requests")
                )
            ):
                raise KisError("refresh_state_invalid")
            due = []
            for symbol in symbols:
                context = read_etf_context(root, symbol, at=now)
                for operation, interval in [
                    ("components", COMPONENT_MAX_AGE),
                    ("nav", NAV_MAX_AGE),
                ]:
                    key = f"{operation}:{symbol}"
                    previous = [
                        saved["attempts"].get(key),
                        context[operation].get("received_at"),
                    ]
                    dates = [_time(v) for v in previous if v is not None]
                    if dates and max(dates) > now:
                        raise KisError("refresh_future_state")
                    if dates and now - max(dates) < interval:
                        continue
                    due.append((operation, symbol))
            if not due:
                return dict(result, status="CACHED")
            # Persist all claims before token construction/network. If one fails,
            # the remaining due requests wait until their normal next interval.
            for operation, symbol in due:
                saved["attempts"][f"{operation}:{symbol}"] = now.isoformat()
            saved["last_run"] = dict(
                result, status="STARTED", at=now.isoformat(), due=len(due)
            )
            state.save_refresh_state(saved)
            try:
                client = client_factory(
                    project_root=Path(config["credential_project_root"]),
                    state_root=Path(root) / "kis",
                    max_requests=1 + len(due),
                )
                outcomes = []
                for operation, symbol in due:
                    fact = client.fetch(operation, symbol)
                    outcomes.append(
                        dict(operation=operation, symbol=symbol, status=fact["status"])
                    )
                result.update(status="UPDATED", outcomes=outcomes)
            except Exception as error:
                result.update(
                    status="DEGRADED",
                    reason=error.code
                    if isinstance(error, KisError)
                    else "refresh_failed",
                )
            finally:
                if client is not None:
                    result.update(
                        requests=client.requests, auth_requests=client.auth_requests
                    )
                saved["total_requests"] += result["requests"]
                saved["total_auth_requests"] += result["auth_requests"]
                saved["last_run"] = dict(
                    result, at=now.isoformat(), request_cap=1 + len(due)
                )
                state.save_refresh_state(saved)
            return result
    except Exception as error:
        return dict(
            result,
            status="BUSY"
            if isinstance(error, KisError) and error.code == "connection_busy"
            else "DEGRADED",
            reason=error.code
            if isinstance(error, KisError)
            else "integration_state_invalid",
        )
