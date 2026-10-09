"""Small optional capture jobs fed by an existing collector's GET reader.

This module has no authentication API or credential input. Scheduling stays
with the current collector. Saved results carry their original observation
times; a partial refresh never renews old data timestamps.
"""

import json
from decimal import Decimal
from pathlib import Path

from tokmo.facts.toss_reads import (
    REQUEST_TIMEOUT_SECONDS,
    build_read_request,
    query_read,
)
from tokmo.programs.quant_decision_calculations import plain
from tokmo.programs.quant_decision_inputs import (
    HistoryRequestError,
    capture_market_data,
    capture_order_info,
    validate_history_request,
    verified_price_history,
)
from tokmo.records.quant_decision_reports import atomic_private


def read_existing_account_refs(path, *, clock):
    """Reuse the validated mapping reader without its legacy DB setup path."""
    import sqlite3

    from tokmo.records.account_native_refs import AccountNativeRefStoreV1

    class ReadOnlyRefs(AccountNativeRefStoreV1):
        def _connect(self):
            return sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0
            )

    return ReadOnlyRefs(Path(path), clock=clock).read_refs()


class ExistingTokenReader:
    """Read-only adapter bound to one supplied bearer; never renews it."""

    def __init__(
        self,
        *,
        token,
        refs,
        transport,
        clock,
        max_calls=4,
        deadline=None,
        wire_recorder=None,
    ):
        if not isinstance(token, str) or not token:
            raise ValueError("existing token required")
        if max_calls < 1:
            raise ValueError("positive call budget required")
        self._token = token
        self._refs = refs
        self._transport = transport
        self.clock = clock
        self.max_calls = max_calls
        self.calls = []
        self.auth_failed = False
        self.deadline = deadline
        self.rate_limited = False
        self.wire_recorder = wire_recorder

    def __call__(self, operation, *, account=None, **kwargs):
        if self.auth_failed:
            raise RuntimeError("expired existing token; no renewal")
        if self.rate_limited:
            raise RuntimeError("http_429")
        if len(self.calls) >= self.max_calls:
            raise RuntimeError("capture job call budget reached")
        if (
            self.deadline is not None
            and (self.deadline - self.clock()).total_seconds()
            < REQUEST_TIMEOUT_SECONDS + 1
        ):
            raise RuntimeError("not enough time for optional capture before next tick")
        if operation == "CURRENT_PRICES":
            from tokmo.facts.toss_prices import (
                build_prices_request,
                query_current_prices,
            )
            from tokmo.facts.toss_prices_live import TossPricesLiveTransport

            build_prices_request(token=self._token, symbols=kwargs["symbols"])
            call = {"operation": "CURRENT_PRICES", "status": "STARTED"}
            if self.wire_recorder:
                self.wire_recorder("QUANT_READ", f"{len(self.calls):064x}")
            self.calls.append(call)
            result = query_current_prices(
                token=self._token,
                symbols=kwargs["symbols"],
                transport=TossPricesLiveTransport(),
                clock=self.clock,
            )
        else:
            request = build_read_request(
                operation,
                token=self._token,
                account_seq=self._refs.reveal(account) if account else None,
                **kwargs,
            )
            call = {"operation": operation.value, "status": "STARTED"}
            if self.wire_recorder:
                self.wire_recorder("QUANT_READ", f"{len(self.calls):064x}")
            self.calls.append(call)
            result = query_read(request, transport=self._transport, clock=self.clock)
        call["status"] = result.code
        if result.retry_after is not None:
            call["retry_after"] = result.retry_after
        if result.code in {"auth_rejection", "forbidden"}:
            self.auth_failed = True
        if result.code == "http_429":
            self.rate_limited = True
        if result.code != "success":
            raise RuntimeError(result.code)
        return result.facts if operation == "CURRENT_PRICES" else result.value


def capture_job(
    fetch,
    *,
    data,
    path,
    job,
    now,
    offset=0,
    batch=3,
    history_start=None,
    history_sessions=None,
    history_reason=None,
):
    if (
        job
        not in {"funds", "orders", "sellable", "candles", "quotes", "investor_flows"}
        or offset < 0
        or batch < 1
    ):
        raise ValueError("invalid capture job")
    personal = [p for p in data["positions"] if p["scope"] == "personal"]
    # A fully sold account still needs its buying power. Do not derive the
    # account inventory solely from its remaining stock positions.
    accounts = sorted(
        {p["account"] for p in personal}
        | {a["account"] for a in data.get("accounts", []) if a["scope"] == "personal"}
    )
    if job == "investor_flows":
        from tokmo.programs.quant_investor_flows import capture_flows

        result = capture_flows(fetch, targets=data["flow_targets"][:batch], now=now)
    elif job == "quotes":
        result = {"instruments": [], "failures": []}
        selected = {i["symbol"]: i for i in data["instruments"][:200]}
        try:
            for value in fetch("CURRENT_PRICES", symbols=tuple(selected)):
                item = selected[value.symbol]
                if (
                    value.currency_raw != item["currency"]
                    or Decimal(value.last_price) <= 0
                ):
                    raise ValueError("invalid current price")
                result["instruments"].append(
                    dict(
                        symbol=value.symbol,
                        market=item["market"],
                        price=value.last_price,
                        price_at=(value.timestamp or value.fetched_at).isoformat(),
                        sources=["Toss GET /api/v1/prices · 퀀트 시세 보충"],
                    )
                )
        except Exception as error:
            result["failures"].append(
                dict(operation="CURRENT_PRICES", reason=type(error).__name__)
            )
    elif job == "candles":
        result = capture_market_data(
            fetch,
            instruments=data["instruments"][offset : offset + batch],
            now=now,
            completed_session=data.get("completed_session"),
            session_closes=data.get("session_closes"),
            history_start=history_start,
            history_sessions=history_sessions,
            history_reason=history_reason,
        )
    else:
        sections = {
            "funds": ("cash", "fx"),
            "orders": ("fees", "orders"),
            "sellable": ("sellable",),
        }[job]
        result = capture_order_info(
            fetch,
            accounts=accounts,
            positions=personal[offset : offset + batch],
            now=now,
            sections=sections,
        )
    path = Path(path)
    cache = json.loads(path.read_text()) if path.exists() else {}
    if job == "investor_flows":
        flows = cache.setdefault("investor_flows", {})
        flows.update(result["investor_flows"])
        for failure in result["failures"]:
            old = flows.setdefault(failure["key"], {})
            # Keep the last good observation, including its original time.
            old["error"] = failure["reason"]

    def merge(field, identity):
        values = {identity(v): v for v in cache.get(field, [])}
        for row in result.get(field, []):
            key = identity(row)
            old = values.get(key, {})
            if (
                field == "instruments"
                and old.get("bars")
                and row.get("metrics", {}).get("price_history", {}).get("stop_reason")
                == "FETCH_FAILED"
            ):
                continue  # Preserve last good bars/times; jobs records this failed attempt.
            if field == "instruments" and old.get("bars") and row.get("bars"):
                prior = {b["date"]: b for b in old["bars"]}
                older, newer = verified_price_history(old), verified_price_history(row)
                retained = old.get("metrics", {}).get("price_history")
                retained = older or (
                    retained.get("cache_coverage")
                    if isinstance(retained, dict)
                    else None
                )
                if isinstance(retained, dict):
                    row["metrics"]["price_history"]["cache_coverage"] = dict(retained)
                prior.update({b["date"]: b for b in row["bars"]})
                row["bars"] = [prior[d] for d in sorted(prior)]
                # Retention does not certify coverage. Only overlapping verified
                # spans certify their union; the current short request stays intact.
                if (
                    older
                    and newer
                    and newer["actual_start"] <= older["actual_end"]
                    and newer["actual_end"] >= older["actual_start"]
                ):
                    coverage = dict(older if older.get("requested_start") else newer)
                    starts = [
                        h["requested_start"]
                        for h in (older, newer)
                        if h.get("requested_start")
                    ]
                    coverage.update(
                        requested_start=min(starts) if starts else None,
                        requested_sessions=None if starts else len(prior),
                        requested_end=row["bars"][-1]["date"],
                        actual_start=row["bars"][0]["date"],
                        actual_end=row["bars"][-1]["date"],
                        actual_count=len(prior),
                    )
                    row["metrics"]["price_history"]["cache_coverage"] = coverage
            values[key] = dict(old, **row)
            if field == "instruments":
                values[key]["metrics"] = dict(
                    old.get("metrics", {}), **row.get("metrics", {})
                )
        cache[field] = list(values.values())

    if job == "funds":
        merge("cash", lambda r: (r["account"], r["currency"]))
        if result.get("fx"):
            cache["fx"] = result["fx"]
    if job == "sellable":
        merge("positions", lambda r: (r["account"], r["key"]))
    if job in {"candles", "quotes"}:
        merge("instruments", lambda r: (r["market"], r["symbol"]))
    if job == "orders":
        cache["fees"] = dict(cache.get("fees", {}), **result["fees"])
        # Empty successful lists remove completed orders; failed scans remain
        # explicitly unavailable, never represented as no open orders.
        cache["orders"] = [
            o for o in cache.get("orders", []) if o.get("account") not in accounts
        ] + result["orders"]
        cache["orders_observed_at"] = now.isoformat()
    cache["captured_at"] = now.isoformat()
    cache.setdefault("jobs", {})[job + ":" + str(offset)] = {
        "at": now.isoformat(),
        "offset": offset,
        "batch": batch,
        "failures": result.get("failures", []),
        "calls": getattr(fetch, "calls", []),
        "authentication_issued": 0,
    }
    atomic_private(path, plain(cache))
    return {
        "job": job,
        "path": str(path),
        "failures": result.get("failures", []),
        "calls": getattr(fetch, "calls", []),
        "authentication_issued": 0,
    }


def requested_capture_consumer(*, project_root, data_root, clock):
    """Use the normal Pulse token for queued jobs or small price references.

    The existing Pulse calls this only when enabled and a normal tick has
    acquired a bearer. Explicit Quant jobs take priority; otherwise refresh
    missing previous-session closes for NEWS without running Quant analysis.
    """
    from tokmo.facts.toss_reads_live import TossReadsLiveTransport
    from tokmo.programs.quant_decision_inputs import load_local_snapshot

    root = Path(data_root) / "ops/quant-inputs"

    def consume(*, existing_token, now, deadline, wire_recorder=None):
        request_path = root / "request.json"
        request = json.loads(request_path.read_text()) if request_path.exists() else {}
        jobs = request.get("jobs", [])
        if not jobs:
            from tokmo.programs.price_reference import capture_price_references

            reader = ExistingTokenReader(
                token=existing_token,
                refs=None,
                transport=TossReadsLiveTransport(),
                clock=clock,
                max_calls=3,
                deadline=deadline,
                wire_recorder=wire_recorder,
            )
            prices = capture_price_references(data_root, reader, now=now)
            if reader.calls or prices.get("failures"):
                return prices
            from tokmo.programs.quant_input_refresh import refresh_due_inputs

            return refresh_due_inputs(
                project_root=project_root,
                data_root=data_root,
                existing_token=existing_token,
                clock=clock,
                now=now,
                deadline=deadline,
                wire_recorder=wire_recorder,
            )
        job = jobs[0]
        data = load_local_snapshot(
            project_root,
            data_root,
            as_of=now,
            include_context=False,
            pension_analysis=request.get("pension_analysis"),
        )
        if job["kind"] == "candles":
            try:
                validate_history_request(
                    history_start=job.get("history_start"),
                    history_sessions=job.get("history_sessions"),
                    instruments=[job["instrument"]]
                    if "instrument" in job
                    else data["instruments"],
                    now=now,
                    completed_session=data.get("completed_session"),
                )
            except HistoryRequestError as error:
                result = dict(
                    job="candles",
                    calls=[],
                    authentication_issued=0,
                    failures=[dict(operation="STOCK_CANDLES", reason=str(error))],
                )
                request.setdefault("history", []).append(
                    dict(result, at=now.isoformat())
                )
                request.setdefault("failed", []).append(jobs.pop(0))
                atomic_private(request_path, request)
                return result
            if "instrument" not in job:
                # Freeze a legacy three-symbol slice before splitting it;
                # merely reducing batch=3 to 1 would silently drop two names.
                offset = job.get("offset", 0)
                selected = data["instruments"][offset : offset + job.get("batch", 3)]
                if selected:
                    jobs[:1] = [
                        dict(
                            job,
                            offset=0,
                            instrument={k: i[k] for k in ("market", "symbol")},
                        )
                        for i in selected
                    ]
                    job = jobs[0]
            if "instrument" in job:
                data["instruments"] = [job["instrument"]]
                job["offset"] = 0
        if job.get("account"):
            data["positions"] = [
                p for p in data["positions"] if p["account"] == job["account"]
            ]
            data["accounts"] = [
                a for a in data.get("accounts", []) if a["account"] == job["account"]
            ]
        refs = (
            read_existing_account_refs(
                Path(data_root) / "ops/account_native_refs.sqlite3", clock=clock
            )
            if job["kind"] not in {"investor_flows", "candles", "quotes"}
            else None
        )
        reader = ExistingTokenReader(
            token=existing_token,
            refs=refs,
            transport=TossReadsLiveTransport(),
            clock=clock,
            max_calls=4,
            deadline=deadline,
            wire_recorder=wire_recorder,
        )
        result = capture_job(
            reader,
            data=data,
            path=root / "latest.json",
            job=job["kind"],
            now=now,
            offset=job.get("offset", 0),
            batch=1 if job["kind"] == "candles" else 3,
            history_start=job.get("history_start"),
            history_sessions=job.get("history_sessions"),
            history_reason=job.get("history_reason"),
        )
        job["attempts"] = job.get("attempts", 0) + (1 if reader.calls else 0)
        if not reader.calls and result["failures"]:
            job["deferred_ticks"] = job.get("deferred_ticks", 0) + 1
        request.setdefault("history", []).append(dict(result, at=now.isoformat()))
        if not result["failures"] or job["attempts"] >= 3:
            completed = request.setdefault(
                "failed" if result["failures"] else "completed", []
            )
            completed.append(jobs.pop(0))
        atomic_private(request_path, request)
        return result

    return consume
