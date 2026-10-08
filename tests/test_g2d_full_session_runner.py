"""G2-D full-session shadow runner — offline via scripted HTTPS + injected stubs.

G2-D extends G2-B (live *data* path) + G2-C (injected scripted GLM/SOL analysis)
across the whole KR regular session: the runner loops the existing production
seven-stage cadence (``_production_stage_deps``) tick by tick, self-terminating
on the real-time session gate, while every Toss response is scripted and the
GLM/SOL model slots are caller-injected stubs.  No socket, DNS, credential,
production database, Telegram, cron, or order-write surface is ever touched.

This suite is the RED→GREEN matrix for §11 T-N20…T-N23 (personal-Mac one-shot
pilot: no ``g2d_ticks`` table, single process, crash = INCOMPLETE with the
production invariant re-measured, no resume/replay, campaign ledger dedicated
to 2310, calendar counted apart, bounded in-process token re-acquisition of at
most ten TOTAL durable TOKEN calls, per-tick green-receipt gate, real model
provider calls = 0) plus the B-1/B-2/B-3/M-1/M-2/M-3 finding closures:

- B-1: the campaign ledger is injected with the real 2310 ceiling (not G2-B's
  286), proven by a 78-tick all-green session whose data starts exceed 286.
- B-2: every tick passes the G2-B-isomorphic green-receipt gate; a non-green
  tick is an explicit STOP, never a success terminus.
- B-3: a crash still re-measures the production fingerprint and records the
  masked exception type durably; KeyboardInterrupt/SystemExit are never eaten.
- M-1: an expired in-process bearer is re-acquired (durable ledger + day
  counter per call) up to ten TOTAL calls; the eleventh or any failure is the
  terminal ``token_lost_incomplete``.
- M-2: real ``g2d`` CLI routes exist with production-safe scripted stubs, and a
  zero-tick ``kr_not_open_*`` refusal does not seal the day.
- M-3: the report's numeric aggregates come from the analysis stage's explicit
  per-tick metric (subject-tick evidence), never from GLM projection rows.
"""

from datetime import date, datetime, timedelta, timezone
from email.message import Message
import ast
import inspect
import io
import json
import os
import socket
import sqlite3

import pytest

import urllib.parse
import urllib.request
import urllib.response

from tokmo import composition
from tokmo.composition import (
    _G2BCanaryLedger,
    build_g2d_live_deps,
    g2d_scripted_glm_call,
    g2d_scripted_sol_call,
)
from tokmo.programs import g2d_full_session_runner as g2d
from tokmo.programs import integrated_fake_cadence
from tokmo.programs.g2a_shadow_runner import G2AStopError, shadow_root
from tokmo.programs.g2d_full_session_runner import (
    G2D_CAMPAIGN_DATA_CEILING,
    G2D_MAX_TICKS,
    _D_STAGE_ORDER,
    _G2DInjectedModelCall,
    run_d_full_session,
)
from tokmo.values.ops_kernel import OpsKernelBudgetExceeded
from tokmo.values.session_gate import Market


# A wall instant on the KR regular-session day, aligned to the 300s grid so the
# first scheduled tick sits exactly at KR open.
START = datetime(2026, 8, 3, 1, 0, tzinfo=timezone.utc)
UTC_DAY = START.date()
# KR regular window spans exactly two 300s ticks: tick 0 (01:00) and tick 1
# (01:05) are open; tick 2 (01:10) is at/after the close, so the runner must
# self-terminate at tick 2 before any data provider.
KR_OPEN = START
KR_CLOSE = START + timedelta(seconds=600)
# The full-session shape §10.2 targets: 78 five-minute ticks (6.5 hours).
FULL_SESSION_TICKS = 78


class VirtualClock:
    """Compressed-replay virtual clock: time is fixed *within* a tick and only
    the runner's sleeper advances it to the next grid boundary.

    This keeps the 300s grid base deterministic (a plain read never drifts, so
    preflight's clock touch cannot round the base up) while making every tick's
    provider closures observe their own scheduled tick time — so ``_pinned_tick``
    tracks tick identity and each open tick fetches fresh live data, exactly as a
    real advancing wall clock would over a full session.
    """

    def __init__(self, start=START):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t = self.t + timedelta(seconds=seconds)


def _clock():
    return VirtualClock()


# ---------------------------------------------------------------------------
# Path-dispatching scripted HTTPS handler (robust to per-tick call counts).
# ---------------------------------------------------------------------------

def _JsonResponse(body: bytes):
    headers = Message()
    response = urllib.response.addinfourl(
        io.BytesIO(body), headers, "https://openapi.tossinvest.com/x", code=200)
    setattr(response, "msg", "OK")
    return response


def _token_body(expires_in: int = 3600) -> bytes:
    return json.dumps({
        "access_token": "a" * 40, "expires_in": expires_in,
        "token_type": "Bearer",
    }).encode()


def _kr_open_body(day: date) -> bytes:
    return json.dumps({"result": {
        "today": {"date": day.isoformat(), "integrated": {"regularMarket": {
            "startTime": f"{KR_OPEN.isoformat()}",
            "endTime": f"{KR_CLOSE.isoformat()}"}}},
        "previousBusinessDay": {"date": "2026-07-31"},
        "nextBusinessDay": {"date": "2026-08-04"},
    }}).encode()


def _kr_window_body(close_seconds: int):
    """A KR calendar whose regular window spans ``close_seconds`` from open."""
    def body(day: date) -> bytes:
        return json.dumps({"result": {
            "today": {"date": day.isoformat(), "integrated": {"regularMarket": {
                "startTime": f"{KR_OPEN.isoformat()}",
                "endTime": f"{(KR_OPEN + timedelta(seconds=close_seconds)).isoformat()}"}}},
            "previousBusinessDay": {"date": "2026-07-31"},
            "nextBusinessDay": {"date": "2026-08-04"},
        }}).encode()
    return body


def _kr_no_regular_body(day: date) -> bytes:
    """A KR holiday: the official calendar carries no regular session."""
    return json.dumps({"result": {
        "today": {"date": day.isoformat(), "integrated": None},
        "previousBusinessDay": {"date": "2026-07-31"},
        "nextBusinessDay": {"date": "2026-08-04"},
    }}).encode()


def _kr_session_body(day: date) -> bytes:
    # A past KR regular-session date for the trades window prefetch: any regular
    # window on the echoed date qualifies it as a session.
    return json.dumps({"result": {
        "today": {"date": day.isoformat(), "integrated": {"regularMarket": {
            "startTime": f"{day.isoformat()}T01:00:00+00:00",
            "endTime": f"{day.isoformat()}T06:30:00+00:00"}}},
        "previousBusinessDay": {"date": "2026-07-31"},
        "nextBusinessDay": {"date": "2026-08-04"},
    }}).encode()


def _us_closed_body(day: date) -> bytes:
    return json.dumps({"result": {
        "today": {"date": day.isoformat()},
        "previousBusinessDay": {"date": "2026-07-31"},
        "nextBusinessDay": {"date": "2026-08-04"},
    }}).encode()


def _accounts_body(*seqs: str) -> bytes:
    return json.dumps({"result": [{"accountSeq": seq} for seq in seqs]}).encode()


def _kr_holdings_body() -> bytes:
    return json.dumps({"result": {"items": [
        {"symbol": "005930", "quantity": "5", "averagePrice": "70000",
         "market": "KR", "currency": "KRW"},
    ]}}).encode()


def _kr_price_body() -> bytes:
    return json.dumps({"result": [
        {"symbol": "005930", "lastPrice": "70500", "currency": "KRW",
         "timestamp": f"{KR_OPEN.isoformat()}"},
    ]}).encode()


def _orders_empty_closed() -> bytes:
    return json.dumps({"result": {
        "orders": [], "nextCursor": None, "hasNext": False}}).encode()


def _dispatch_handler(wire, *, kr_calendar=_kr_open_body,
                      token_expires_in: int = 3600, holdings_body=None):
    """A scripted HTTPS handler that answers by request path (not by order).

    A whole KR-open session runs many ticks with cache-gated calendar/token, so
    a fixed queue would be brittle; dispatching on the path lets every tick's
    real call sequence resolve to a valid scripted body while ``wire`` records
    exactly which endpoints were actually hit and when.  ``holdings_body`` may
    be a callable of the running holdings-call count, so a test can turn the
    holdings path non-green mid-session deterministically.
    """
    holdings_calls = {"n": 0}

    def _body_for(method: str, path: str, query) -> bytes:
        if path == "/oauth2/token":
            return _token_body(token_expires_in)
        if path == "/api/v1/market-calendar/KR":
            day = date.fromisoformat(query.get("date", [UTC_DAY.isoformat()])[0])
            if day == UTC_DAY:
                return kr_calendar(day)
            return _kr_session_body(day)
        if path == "/api/v1/market-calendar/US":
            day = date.fromisoformat(query.get("date", [UTC_DAY.isoformat()])[0])
            return _us_closed_body(day)
        if path == "/api/v1/accounts":
            return _accounts_body("1001")
        if path == "/api/v1/holdings":
            holdings_calls["n"] += 1
            if holdings_body is not None:
                return holdings_body(holdings_calls["n"])
            return _kr_holdings_body()
        if path == "/api/v1/prices":
            return _kr_price_body()
        if path == "/api/v1/orders":
            return _orders_empty_closed()
        raise AssertionError(f"unscripted path: {path}")

    class _Scripted(urllib.request.HTTPSHandler):
        def https_open(self, request):
            parts = urllib.parse.urlsplit(request.full_url)
            query = urllib.parse.parse_qs(parts.query)
            wire.append((request.get_method(), parts.path))
            return _JsonResponse(_body_for(request.get_method(), parts.path, query))

    return _Scripted


# ---------------------------------------------------------------------------
# Injected scripted GLM/SOL stubs (no live model provider, ever).
# ---------------------------------------------------------------------------

def _glm_screen_all_none(body):
    return json.dumps({
        "schema_version": 1,
        "annotations": [{
            "candidate_id": row["candidate_id"], "relevance": "NONE",
            "materiality": "LOW", "urgency": "NONE",
            "evidence_unknowns": [], "summary": "screened",
        } for row in json.loads(body)["candidates"]],
    }, separators=(",", ":")).encode()


def _sol_never(body):
    return json.dumps({"schema_version": 1, "analyses": []},
                      separators=(",", ":")).encode()


def _paths(wire):
    return [entry[1] for entry in wire]


def _credential(tmp_path):
    cred = tmp_path / ".env.toss"
    cred.write_text("TOSS_CLIENT_ID=real-id\nTOSS_CLIENT_SECRET=real-secret\n")
    os.chmod(cred, 0o600)
    return cred


def _forbid_socket(monkeypatch, sink):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: sink.append(a))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: sink.append(a))


@pytest.fixture(autouse=True)
def _restore_socket():
    saved = (socket.getaddrinfo, socket.create_connection)
    yield
    socket.getaddrinfo, socket.create_connection = saved


def _run(tmp_path, monkeypatch, *, glm=_glm_screen_all_none, sol=_sol_never,
         clock=None, sleeper=None, wire=None, kr_calendar=_kr_open_body,
         campaign_data_ceiling=G2D_CAMPAIGN_DATA_CEILING, approved_rerun=False,
         token_expires_in=3600, holdings_body=None, metrics_sink=None):
    _credential(tmp_path)
    real_socket: list = []
    _forbid_socket(monkeypatch, real_socket)
    clock = clock or VirtualClock()
    # The default sleeper advances virtual time to the next grid boundary, so the
    # next tick's providers observe their own scheduled tick (fresh live data per
    # tick); a test may inject its own sleeper (e.g. to simulate a crash).
    if sleeper is None:
        sleeper = clock.advance if isinstance(clock, VirtualClock) else (lambda _s: None)
    if wire is None:
        wire = []
    deps = build_g2d_live_deps(
        project_root=tmp_path, clock=clock, glm_call=glm, sol_call=sol,
        https_handler_factory=_dispatch_handler(
            wire, kr_calendar=kr_calendar, token_expires_in=token_expires_in,
            holdings_body=holdings_body))
    report = run_d_full_session(
        home=tmp_path, utc_day=UTC_DAY, deps=deps, credential_path=None,
        clock=clock, sleeper=sleeper, open_handle_probe=lambda _p: 0,
        campaign_data_ceiling=campaign_data_ceiling, approved_rerun=approved_rerun,
        metrics_sink=metrics_sink)
    return report, wire, real_socket


def _read_state(tmp_path):
    return json.loads(
        (shadow_root(tmp_path, UTC_DAY) / "g2d_run_state.json").read_text())


# ---------------------------------------------------------------------------
# T-N20 (§10.3): self-termination on the after_close tick with data-provider 0.
# ---------------------------------------------------------------------------

def test_after_close_tick_self_terminates_before_any_data_provider(tmp_path, monkeypatch):
    report, wire, real_socket = _run(tmp_path, monkeypatch)

    # KR was open for exactly ticks 0 and 1; the third tick read the verdict as
    # after_close and the loop exited *before* the tick's data providers.
    assert report.stop_reason == "session_ended"
    assert report.incomplete is False
    assert report.tick_count == 2
    # B-2: both open ticks proved their data receipts green, and the receipt
    # evidence is carried in the report (no false-green shortcut).
    assert report.green_tick_count == 2
    assert report.receipt_counts.get("holdings:saved") == 2
    assert report.receipt_counts.get("quotes:saved") == 2
    assert report.receipt_counts.get("trades:saved") == 2
    # The live data path really ran while KR was open (holdings/quotes/trades),
    # so this is a genuine data run, not a no-op.
    data_paths = {"/api/v1/accounts", "/api/v1/holdings", "/api/v1/prices",
                  "/api/v1/orders"}
    assert any(path in data_paths for path in _paths(wire))
    # Corrected market-local behavior (G-1, cadence SDS §4): the session-gate
    # calendar is fetched once per market for its own local date on the cold
    # first tick (before any data GET) and reused from cache afterwards, so
    # the terminating after_close tick performs no wire at all — the final
    # wire entry is the last open tick's data call, with nothing after it.
    first_data = min(i for i, (_m, path) in enumerate(wire) if path in data_paths)
    cold_calendar = [
        i for i, (_m, path) in enumerate(wire[:first_data])
        if path.startswith("/api/v1/market-calendar/")
    ]
    assert len(cold_calendar) == 2  # one KR + one US cold local-date fetch
    assert wire[-1][1] in data_paths
    assert real_socket == []


def test_stage_order_is_the_full_seven_stage_cadence(tmp_path):
    assert _D_STAGE_ORDER == (
        "calendar_provider", "session_gate", "holdings_runner", "quotes_runner",
        "trades_runner", "barrier_runner", "analysis_runner",
    )


# ---------------------------------------------------------------------------
# Full-session shape: 78 ticks, all receipts green, ledger past G2-B's 286,
# bounded token re-acquisition, subject-tick numeric evidence (B-1/M-1/M-3).
# ---------------------------------------------------------------------------

def test_full_session_78_ticks_green_past_g2b_ledger_with_token_reacquisition(
        tmp_path, monkeypatch):
    report, wire, real_socket = _run(
        tmp_path, monkeypatch,
        kr_calendar=_kr_window_body(FULL_SESSION_TICKS * 300),
        token_expires_in=3600)

    # The whole KR session ran and self-terminated as a success terminus.
    assert report.stop_reason == "session_ended"
    assert report.incomplete is False
    assert report.tick_count == FULL_SESSION_TICKS
    # B-2: every single tick proved its three data receipts green.
    assert report.green_tick_count == FULL_SESSION_TICKS
    assert report.receipt_counts.get("holdings:saved") == FULL_SESSION_TICKS
    assert report.receipt_counts.get("quotes:saved") == FULL_SESSION_TICKS
    assert report.receipt_counts.get("trades:saved") == FULL_SESSION_TICKS
    assert not any(":failed" in key for key in report.receipt_counts)
    # B-1: the run's non-calendar starts exceed G2-B's 286 campaign ceiling —
    # under the reused 286 ledger this session could never have stayed green.
    assert report.provider_started_count > 286
    assert report.provider_started_count <= G2D_CAMPAIGN_DATA_CEILING
    # M-1: the 3600s bearer expired in-session and was re-acquired in-process:
    # ceil(78*300/3600) = 7 durable TOKEN calls, within the ten-call allowance.
    assert report.per_operation.get("TOKEN") == 7
    assert report.token_started_count == 7
    # M-3: the numeric aggregates are subject-tick evidence from the analysis
    # stage's explicit metric.  One subject observed over 78 ticks: the first
    # tick is DATA_INCOMPLETE (ring < 2), the remaining 77 are established.
    assert report.numeric_state_established_count == FULL_SESSION_TICKS - 1
    assert report.numeric_state_counts.get("DATA_INCOMPLETE") == 1
    assert sum(report.numeric_state_counts.values()) == FULL_SESSION_TICKS
    assert report.trigger_counts == {"FIRST_OBSERVATION": 1, "STATE_TRANSITION": 1}
    assert report.numeric_evidence_missing_tick_count == 0
    # The aggregates are decoupled from the GLM projection: the model was
    # called only on the two edge ticks, not once per established subject-tick.
    assert report.glm_calls < report.numeric_state_established_count
    # The calendar wire stays a separate bounded budget (<=70, M-4).
    calendar = sum(
        1 for path in _paths(wire)
        if path.startswith("/api/v1/market-calendar/"))
    assert calendar <= 70
    # Virtual full-session duration: the terminating tick sits one grid step
    # past the last open tick.
    assert report.duration_seconds == FULL_SESSION_TICKS * 300.0
    assert report.production_invariant_ok is True
    assert real_socket == []
    assert _read_state(tmp_path)["status"] == "COMPLETED"


def test_ledger_admits_worst_shape_to_exactly_2310_and_refuses_2311th():
    # The deterministic worst-shape equivalence for §10.5: ten TOKEN calls plus
    # one hundred ticks of exactly 23 data starts (1 accounts + 5 holdings +
    # 5 quote batches + 12 trades) fill the dedicated 2310 ledger exactly; the
    # 2311th admission is refused before any I/O.
    ledger = _G2BCanaryLedger(campaign_ceiling=G2D_CAMPAIGN_DATA_CEILING)
    for _ in range(10):
        ledger.admit_token()
    for tick in range(G2D_MAX_TICKS):
        ledger.begin_tick(tick + 1)
        ledger.admit_accounts()
        for _ in range(5):
            ledger.admit_account_holdings()
        for _ in range(5):
            ledger.admit_quote_batch()
        for _ in range(12):
            ledger.admit_trade()
    assert ledger.total == G2D_CAMPAIGN_DATA_CEILING == 2310
    ledger.begin_tick(G2D_MAX_TICKS + 1)
    with pytest.raises(OpsKernelBudgetExceeded):
        ledger.admit_accounts()
    # G2-B's own default stays the bounded 286 (its contract is untouched).
    g2b_ledger = _G2BCanaryLedger()
    for _ in range(286):
        g2b_ledger.admit_token()
    with pytest.raises(OpsKernelBudgetExceeded):
        g2b_ledger.admit_token()


# ---------------------------------------------------------------------------
# B-2: a non-green tick is an explicit STOP, never a success terminus.
# ---------------------------------------------------------------------------

def test_non_green_tick_stops_run_instead_of_reporting_success(tmp_path, monkeypatch):
    # The holdings path turns malformed from its third tick onward: the stage
    # writes a failed receipt and returns normally, and the per-tick green gate
    # must convert that into an explicit STOP.
    def holdings_body(count):
        if count >= 3:
            return b"{\"result\": \"garbage\"}"
        return _kr_holdings_body()

    report, _wire, _socket = _run(
        tmp_path, monkeypatch, kr_calendar=_kr_window_body(10 * 300),
        holdings_body=holdings_body)

    assert report.stop_reason == "data_tick_not_green"
    assert report.tick_count == 3
    assert report.green_tick_count == 2
    assert any(key.startswith("holdings:failed") for key in report.receipt_counts)
    state = _read_state(tmp_path)
    assert state["status"] == "STOPPED"
    assert state["stop_code"] == "data_tick_not_green"


# ---------------------------------------------------------------------------
# T-N21 (§10.4): single process, INCOMPLETE on crash/token-loss, no g2d_ticks
# table, no resume, same-day rerun refused without approval.
# ---------------------------------------------------------------------------

def test_crash_mid_session_ends_incomplete_without_propagating(tmp_path, monkeypatch):
    boom = {"ticks": 0}
    clock = VirtualClock()

    def _sleeper(seconds):
        # A crash injected between ticks: the second scheduled sleep raises, so
        # the single process dies mid-session after tick 0 committed its work.
        boom["ticks"] += 1
        if boom["ticks"] >= 2:
            raise RuntimeError("simulated crash")
        clock.advance(seconds)

    report, _wire, _socket = _run(
        tmp_path, monkeypatch, clock=clock, sleeper=_sleeper,
        kr_calendar=_kr_window_body(10 * 300))

    # No exception escaped: the run terminated itself as INCOMPLETE.
    assert report.incomplete is True
    assert report.stop_reason == "crashed"
    assert report.tick_count >= 1
    # B-3: the untouched production fingerprint was actually re-measured, and
    # the masked exception type is recorded durably and in the report.
    assert report.production_invariant_ok is True
    assert report.masked_exception_type == "RuntimeError"
    state = _read_state(tmp_path)
    assert state["status"] == "INCOMPLETE"
    assert state["exception_type"] == "RuntimeError"


def test_crash_path_reconciles_production_mutation(tmp_path, monkeypatch):
    # B-3: a crash that coincides with a production-state mutation must not
    # report production_invariant_ok=True — the crash path re-measures.
    production_lock = (
        tmp_path / ".local" / "share" / "tokmo-v3" / "ops" / "locks"
        / "integrated.lock")
    boom = {"ticks": 0}
    clock = VirtualClock()

    def _sleeper(seconds):
        boom["ticks"] += 1
        if boom["ticks"] >= 2:
            production_lock.parent.mkdir(parents=True, exist_ok=True)
            production_lock.write_text("")
            raise RuntimeError("simulated crash")
        clock.advance(seconds)

    report, _wire, _socket = _run(
        tmp_path, monkeypatch, clock=clock, sleeper=_sleeper,
        kr_calendar=_kr_window_body(10 * 300))

    assert report.incomplete is True
    assert report.production_invariant_ok is False
    assert report.stop_reason == "production_mutated"
    assert report.masked_exception_type == "RuntimeError"
    state = _read_state(tmp_path)
    assert state["status"] == "INCOMPLETE"
    assert state["stop_code"] == "production_mutated"


def test_keyboard_interrupt_is_recorded_but_never_swallowed(tmp_path, monkeypatch):
    boom = {"ticks": 0}
    clock = VirtualClock()

    def _sleeper(seconds):
        boom["ticks"] += 1
        if boom["ticks"] >= 2:
            raise KeyboardInterrupt()
        clock.advance(seconds)

    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, monkeypatch, clock=clock, sleeper=_sleeper,
             kr_calendar=_kr_window_body(10 * 300))
    state = _read_state(tmp_path)
    assert state["status"] == "INCOMPLETE"
    assert state["exception_type"] == "KeyboardInterrupt"


def test_no_g2d_ticks_progress_table_and_no_resume_or_replay(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    # The pilot keeps no durable per-tick progress table (M-3): the runner never
    # imports sqlite3 (no connection of its own) and defines no DDL/table at all.
    source = inspect.getsource(g2d)
    tree = ast.parse(source)
    imported = {alias.name for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    assert "sqlite3" not in imported
    assert "CREATE TABLE" not in source
    root = shadow_root(tmp_path, UTC_DAY)
    for leaf in os.listdir(root / "ops"):
        assert "progress" not in leaf, leaf
    connection = sqlite3.connect(root / "ops" / "ops_kernel.sqlite3")
    try:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()
    assert "g2d_ticks" not in tables


def test_same_day_rerun_refuses_without_explicit_approval(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    with pytest.raises(G2AStopError, match="rerun_requires_approval"):
        _run(tmp_path, monkeypatch)
    # The explicit user-approval flag is the only way a second same-day run proceeds.
    report, _wire, _socket = _run(tmp_path, monkeypatch, approved_rerun=True)
    assert report.run_id.endswith(UTC_DAY.isoformat())


# ---------------------------------------------------------------------------
# M-1 (§10.4): bounded in-process token re-acquisition, ten TOTAL calls.
# ---------------------------------------------------------------------------

def test_token_expiry_reacquires_bounded_then_stops_incomplete(tmp_path, monkeypatch):
    from tokmo.programs.integrated_fake_cadence import RequestGateV1
    from tokmo.records.ops_kernel import OpsKernelStore
    from tokmo.programs.g2a_shadow_runner import _private_dir

    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    root = shadow_root(tmp_path, UTC_DAY)
    _private_dir(root / "ops")
    store = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")
    from tokmo.values.ops_kernel import ProducerRole
    import secrets
    store.reconcile_requests(ProducerRole.COLLECTOR,
                             invocation_id=secrets.token_hex(16), now=START)

    wire: list = []
    deps = build_g2d_live_deps(
        project_root=tmp_path, clock=lambda: START,
        glm_call=_glm_screen_all_none, sol_call=_sol_never,
        https_handler_factory=_dispatch_handler(wire))
    gate = RequestGateV1(store)
    token_acquire = g2d._token_acquire_for(deps)
    first = token_acquire(gate, START)
    assert first  # a fresh acquisition succeeds
    # Expiry within the same single process re-acquires a fresh bearer under a
    # new durable TOKEN identity — never a silent reuse, never a restart.
    second = token_acquire(gate, START + timedelta(hours=2))
    assert second
    token_rows = [
        row for row in store.read_planned_requests(0, "HOLDINGS")
        if row["operation"] == "TOKEN"]
    assert len(token_rows) == 2
    # Every further expiry mints one more durable call, up to ten TOTAL; the
    # eleventh is the terminal token_lost_incomplete STOP.
    for extra in range(8):
        assert token_acquire(gate, START + timedelta(hours=4 + 2 * extra))
    token_rows = [
        row for row in store.read_planned_requests(0, "HOLDINGS")
        if row["operation"] == "TOKEN"]
    assert len(token_rows) == 10
    with pytest.raises(G2AStopError, match="token_lost_incomplete"):
        token_acquire(gate, START + timedelta(hours=48))
    # The refused eleventh call never charged a durable row.
    token_rows = [
        row for row in store.read_planned_requests(0, "HOLDINGS")
        if row["operation"] == "TOKEN"]
    assert len(token_rows) == 10


def test_token_allowance_exhaustion_mid_session_is_incomplete(tmp_path, monkeypatch):
    # A 2100s bearer over 78 ticks needs an eleventh acquisition at tick 70:
    # the run must stop token_lost_incomplete with exactly ten durable calls.
    report, _wire, _socket = _run(
        tmp_path, monkeypatch,
        kr_calendar=_kr_window_body(FULL_SESSION_TICKS * 300),
        token_expires_in=2100)

    assert report.stop_reason == "token_lost_incomplete"
    assert report.incomplete is True
    assert report.per_operation.get("TOKEN") == 10
    assert report.green_tick_count == 70
    # Tick 70 aborted mid-flight on the refused eleventh call, so only the 70
    # green ticks were ever committed as executed.
    assert report.tick_count == 70
    assert _read_state(tmp_path)["status"] == "INCOMPLETE"


# ---------------------------------------------------------------------------
# T-N22 (§10.5): campaign data ceiling 2310, calendar (<=70) counted apart.
# ---------------------------------------------------------------------------

def test_campaign_data_ceiling_stops_before_exceeding_and_calendar_is_apart(tmp_path, monkeypatch):
    # A small injected ceiling (just above one tick's 23 data upper bound) lets
    # exactly the first tick run, then the pre-tick gate trips before the second
    # tick would cross the campaign total; production default is the real 2310.
    report, wire, _socket = _run(tmp_path, monkeypatch, campaign_data_ceiling=25)

    assert report.stop_reason == "campaign_ceiling"
    # The first tick ran; no over-ceiling data start ever happened.
    assert 1 <= report.provider_started_count <= 25
    # The calendar wire is a separate budget (M-4): it is not part of the 2310
    # data counter, and the gate/prefetch calendars still occurred.
    calendar = (_paths(wire).count("/api/v1/market-calendar/KR")
                + _paths(wire).count("/api/v1/market-calendar/US"))
    assert calendar >= 2
    connection = sqlite3.connect(shadow_root(tmp_path, UTC_DAY) / "ops" / "ops_kernel.sqlite3")
    try:
        provider_counter = connection.execute(
            "SELECT provider_started_count FROM request_day_counters WHERE utc_day=?",
            (UTC_DAY.isoformat(),)).fetchone()[0]
        calendar_started = connection.execute(
            "SELECT COUNT(*) FROM planned_requests WHERE started_at IS NOT NULL "
            "AND operation LIKE 'CALENDAR_DAY_%'").fetchone()[0]
    finally:
        connection.close()
    # The durable 3744-shaped counter excludes calendar entirely.
    assert provider_counter == report.provider_started_count
    assert calendar_started >= 2
    assert G2D_CAMPAIGN_DATA_CEILING == 2310
    assert G2D_MAX_TICKS == 100


def test_tick_cap_is_one_hundred(tmp_path):
    assert G2D_MAX_TICKS == 100


def test_tick_cap_reached_stops_after_one_hundred_ticks(tmp_path, monkeypatch):
    # A calendar whose close never arrives within the cap: the safety net stops
    # the loop at exactly 100 executed ticks, all green, ceiling never fired.
    report, _wire, _socket = _run(
        tmp_path, monkeypatch, kr_calendar=_kr_window_body(140 * 300))

    assert report.stop_reason == "tick_cap_reached"
    assert report.tick_count == G2D_MAX_TICKS
    assert report.green_tick_count == G2D_MAX_TICKS
    assert report.incomplete is False
    assert report.provider_started_count <= G2D_CAMPAIGN_DATA_CEILING
    assert _read_state(tmp_path)["status"] == "STOPPED"


# ---------------------------------------------------------------------------
# Real 300s grid: injected wall-clock lag keeps tick identity coherent.
# ---------------------------------------------------------------------------

def test_lag_injection_keeps_tick_ids_on_the_300s_grid(tmp_path, monkeypatch):
    clock = VirtualClock()

    def lag_sleeper(seconds):
        # Every grid wait overshoots by 45s of simulated wall-clock lag, so all
        # providers observe scheduled+45s while the tick identity must stay the
        # scheduled 300s bucket.
        clock.advance(seconds + 45)

    metrics: list = []
    report, _wire, _socket = _run(
        tmp_path, monkeypatch, clock=clock, sleeper=lag_sleeper,
        kr_calendar=_kr_window_body(6 * 300), metrics_sink=metrics.append)

    assert report.stop_reason == "session_ended"
    assert report.tick_count == 6
    assert report.green_tick_count == 6
    tick_ids = [metric["tick_id"] for metric in metrics]
    assert tick_ids == sorted(tick_ids)
    assert all(b - a == 1 for a, b in zip(tick_ids, tick_ids[1:]))
    lags = [metric["lag_seconds"] for metric in metrics]
    assert lags[0] == 0.0
    assert all(lag == 45.0 for lag in lags[1:])
    # Lagged providers still filed their durable work under the scheduled tick:
    # otherwise the per-tick receipts could not have read green.
    assert report.receipt_counts.get("holdings:saved") == 6


# ---------------------------------------------------------------------------
# M-2: zero-tick kr_not_open refusal never seals the day, reasons distinct.
# ---------------------------------------------------------------------------

def test_before_open_start_refuses_without_sealing_the_day(tmp_path, monkeypatch):
    early = VirtualClock(START - timedelta(minutes=20))
    report, _wire, _socket = _run(tmp_path, monkeypatch, clock=early)

    assert report.stop_reason == "kr_not_open_before_open"
    assert report.tick_count == 0
    assert report.incomplete is False
    assert _read_state(tmp_path)["status"] == "REFUSED"

    # The real open-hours run later the same day proceeds WITHOUT the approval
    # flag: a zero-tick refusal must not burn the day's only attempt.
    report, _wire, _socket = _run(tmp_path, monkeypatch, clock=VirtualClock(START))
    assert report.stop_reason == "session_ended"
    assert report.tick_count == 2
    assert report.green_tick_count == 2
    assert _read_state(tmp_path)["status"] == "COMPLETED"


def test_holiday_refusal_reason_is_distinct_from_before_open(tmp_path, monkeypatch):
    report, _wire, _socket = _run(
        tmp_path, monkeypatch, kr_calendar=_kr_no_regular_body)

    assert report.stop_reason == "kr_not_open_no_regular"
    assert report.tick_count == 0
    assert _read_state(tmp_path)["status"] == "REFUSED"


def test_after_close_zero_tick_is_a_completed_terminus(tmp_path, monkeypatch):
    late = VirtualClock(START + timedelta(seconds=1200))
    report, _wire, _socket = _run(tmp_path, monkeypatch, clock=late)

    assert report.stop_reason == "session_ended"
    assert report.tick_count == 0
    assert _read_state(tmp_path)["status"] == "COMPLETED"
    with pytest.raises(G2AStopError, match="rerun_requires_approval"):
        _run(tmp_path, monkeypatch, clock=VirtualClock(START + timedelta(seconds=1500)))


# ---------------------------------------------------------------------------
# M-2: real operational entry points with production-safe scripted stubs.
# ---------------------------------------------------------------------------

def test_cli_g2d_preflight_route_exists_and_refuses_cleanly(tmp_path, capsys):
    code = composition.main([
        "g2d", "--preflight",
        "--home", str(tmp_path), "--project-root", str(tmp_path)])
    out = capsys.readouterr().out
    # No credential in the bare tmp home: the real preflight refuses, which
    # proves the route is wired to the real guard (and opens no socket).
    assert code == 1
    assert "g2d preflight=refused" in out


def test_cli_g2d_run_route_passes_scripted_stubs_and_rerun_flag(tmp_path, monkeypatch, capsys):
    recorded = {}

    def fake_build(**kwargs):
        recorded["build"] = kwargs
        return "deps-sentinel"

    def fake_run(**kwargs):
        recorded["run"] = kwargs
        return g2d.G2DReport(
            run_id="g2d-a-2026-08-03", utc_day="2026-08-03", tick_count=0,
            stop_reason="kr_not_open_before_open", provider_started_count=0,
            wire_attempt_count=0, numeric_state_established_count=0,
            trigger_counts={}, disposition_counts={},
            production_invariant_ok=True, incomplete=False, duration_seconds=0.0)

    monkeypatch.setattr(composition, "build_g2d_live_deps", fake_build)
    monkeypatch.setattr(composition, "run_d_full_session", fake_run)

    code = composition.main([
        "g2d", "--run-d-full-session",
        "--home", str(tmp_path), "--project-root", str(tmp_path)])
    assert code == 2
    assert recorded["run"]["approved_rerun"] is False
    # The CLI supplies the composition-owned deterministic scripted stubs — the
    # model slots never default to anything else.
    assert recorded["build"]["glm_call"] is g2d_scripted_glm_call
    assert recorded["build"]["sol_call"] is g2d_scripted_sol_call
    assert recorded["build"]["https_handler_factory"] is None

    code = composition.main([
        "g2d", "--run-d-full-session", "--approved-rerun",
        "--home", str(tmp_path), "--project-root", str(tmp_path)])
    assert code == 2
    assert recorded["run"]["approved_rerun"] is True
    out = capsys.readouterr().out
    assert "g2d run=stopped" in out
    # Masked output only: no token, account seq, symbol, or URL vocabulary.
    assert "005930" not in out and "http" not in out


def test_cli_usage_mentions_g2d_routes(capsys):
    assert composition.main([]) == 64
    err = capsys.readouterr().err
    assert "g2d --preflight" in err
    assert "g2d --run-d-full-session" in err


def test_scripted_stubs_are_deterministic_and_local(tmp_path):
    body = json.dumps({
        "schema_version": 1, "instructions": [], "untrusted": [],
        "candidates": [
            {"candidate_id": "c1", "payload": {}},
            {"candidate_id": "c2", "payload": {}},
        ],
    }).encode()
    first = g2d_scripted_glm_call(body)
    second = g2d_scripted_glm_call(body)
    assert first == second
    annotations = json.loads(first)["annotations"]
    assert [row["candidate_id"] for row in annotations] == ["c1", "c2"]
    assert all(row["relevance"] == "NONE" for row in annotations)
    assert json.loads(g2d_scripted_sol_call(body)) == {
        "schema_version": 1, "analyses": []}


# ---------------------------------------------------------------------------
# M-3: numeric-evidence instrumentation gaps are counted, never silent zeros.
# ---------------------------------------------------------------------------

def test_missing_numeric_evidence_is_counted_and_marks_incomplete(tmp_path, monkeypatch):
    # If the analysis stage stops emitting the explicit per-tick numeric
    # metric, the report must expose the gap and refuse to look complete —
    # instrumentation failure and "no signal" are different facts.
    monkeypatch.setattr(
        integrated_fake_cadence, "run_analysis_pipeline",
        lambda **kwargs: None)
    report, _wire, _socket = _run(tmp_path, monkeypatch)

    assert report.numeric_evidence_missing_tick_count == 2
    assert report.incomplete is True
    assert report.numeric_state_established_count == 0
    assert _read_state(tmp_path)["status"] == "INCOMPLETE"


# ---------------------------------------------------------------------------
# T-N23 (§10.7): GLM/SOL slots are always caller-injected; real model calls = 0.
# ---------------------------------------------------------------------------

def test_model_slots_are_caller_injected_and_no_network_model_call(tmp_path, monkeypatch):
    glm_bodies, sol_bodies = [], []

    def glm(body):
        glm_bodies.append(body)
        return _glm_screen_all_none(body)

    def sol(body):
        sol_bodies.append(body)
        return _sol_never(body)

    report, _wire, real_socket = _run(tmp_path, monkeypatch, glm=glm, sol=sol)

    # The composed deps carry the injection marker on both model slots.
    # (The runner refuses to start otherwise — see the misconfiguration test.)
    assert report.glm_calls == len(glm_bodies)
    assert report.sol_calls == len(sol_bodies)
    # GLM saw at least the first-observation candidate; SOL was screened out here.
    assert report.glm_calls >= 1
    # M-3: the numeric aggregates come from the analysis stage's explicit
    # subject-tick metric, not from GLM projection rows.  By the second
    # in-session tick the ring has warmed to an observed (non-INCOMPLETE)
    # numeric state, and both edge ticks fired a trigger.
    assert report.numeric_state_established_count == 1
    assert report.numeric_state_counts.get("DATA_INCOMPLETE") == 1
    assert sum(report.trigger_counts.values()) >= 1
    assert report.numeric_evidence_missing_tick_count == 0
    # No account/symbol raw ever crossed the model boundary, and no socket opened.
    assert real_socket == []


def test_runner_refuses_non_injected_model_slots(tmp_path, monkeypatch):
    import dataclasses
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    wire: list = []
    clock = VirtualClock()
    deps = build_g2d_live_deps(
        project_root=tmp_path, clock=clock, glm_call=_glm_screen_all_none,
        sol_call=_sol_never, https_handler_factory=_dispatch_handler(wire))
    # A raw (non-marker) model slot smuggled past composition must be rejected
    # at once, before any provider runs.
    tampered = dataclasses.replace(deps, glm_call=lambda body: b"{}")
    with pytest.raises(G2AStopError, match="glm_not_injected"):
        run_d_full_session(
            home=tmp_path, utc_day=UTC_DAY, deps=tampered, credential_path=None,
            clock=clock, sleeper=lambda _s: None, open_handle_probe=lambda _p: 0)


def test_runner_and_composition_import_no_live_model_provider(tmp_path):
    # The runner never imports a live model provider or the live transports; the
    # only sanctioned live importer stays composition.py.
    source = inspect.getsource(g2d)
    tree = ast.parse(source)
    modules = {
        (node.module or "")
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any("composition" in name for name in modules)
    assert not any("live" in name.lower() for name in modules)
    assert not any(name.split(".")[0] in {"socket", "ssl", "http", "urllib"}
                   for name in modules)


def test_glm_sol_are_required_injections_of_build_g2d_live_deps(tmp_path):
    signature = inspect.signature(build_g2d_live_deps)
    for name in ("glm_call", "sol_call", "clock", "project_root"):
        assert signature.parameters[name].default is inspect.Parameter.empty
    # The wrapper is the injection marker the runner checks for.
    marker = _G2DInjectedModelCall(lambda b: b, lambda: START)
    assert callable(marker)
