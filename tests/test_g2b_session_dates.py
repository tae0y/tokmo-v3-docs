"""G2-B real regular-session-date source — offline via scripted HTTPS (no socket).

Proves the composition-owned ``_G2BCalendarSessionDates`` that the completed-
trades window needs: it walks descending local calendar dates against the same
wired official Toss calendar transport, collects exactly twenty regular-session
dates per market, caches every sanitized answer durably at mode 0600, accounts
each probe on the shared ops kernel under CALENDAR (its own budget, never the
3744 provider ceiling), reuses the cache with zero new wire, STOPs
``session_dates_incomplete`` past the bound, and STOPs a redirect without retry.
"""

from datetime import date, datetime, timedelta, timezone
from email.message import Message
import io
import json
import os
import socket
import sqlite3

import pytest

import urllib.parse
import urllib.request
import urllib.response

from tokmo.composition import build_g2b_live_deps
from tokmo.programs.g2a_shadow_runner import G2AStopError
from tokmo.programs.integrated_fake_cadence import RequestGateV1
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.session_gate import Market


# ---------------------------------------------------------------------------
# Scripted-HTTPS fixtures.
# ---------------------------------------------------------------------------

def _JsonResponse(body: bytes):
    headers = Message()
    response = urllib.response.addinfourl(
        io.BytesIO(body), headers, "https://openapi.tossinvest.com/x", code=200)
    setattr(response, "msg", "OK")
    return response


def _redirect(url: str):
    headers = Message()
    headers["Location"] = "https://openapi.tossinvest.com/elsewhere"
    response = urllib.response.addinfourl(io.BytesIO(b""), headers, url, code=302)
    setattr(response, "msg", "Found")
    return response


def _handler_factory(responses, wire):
    queue = iter(responses)

    class _Scripted(urllib.request.HTTPSHandler):
        def https_open(self, request):
            parts = urllib.parse.urlsplit(request.full_url)
            wire.append((request.get_method(), parts.path))
            return next(queue)

    return _Scripted


def _token_body() -> bytes:
    return json.dumps({
        "access_token": "a" * 40, "expires_in": 3600, "token_type": "Bearer",
    }).encode()


def _kr_body(day: date, is_open: bool) -> bytes:
    today: dict = {"date": day.isoformat()}
    if is_open:
        today["integrated"] = {"regularMarket": {
            "startTime": f"{day.isoformat()}T00:00:00+00:00",
            "endTime": f"{day.isoformat()}T06:30:00+00:00"}}
    return json.dumps({"result": {
        "today": today,
        "previousBusinessDay": {"date": "2026-01-02"},
        "nextBusinessDay": {"date": "2026-01-05"},
    }}).encode()


def _us_body(day: date, is_open: bool) -> bytes:
    today: dict = {"date": day.isoformat()}
    if is_open:
        today["regularMarket"] = {
            "startTime": f"{day.isoformat()}T13:30:00+00:00",
            "endTime": f"{day.isoformat()}T20:00:00+00:00"}
    return json.dumps({"result": {
        "today": today,
        "previousBusinessDay": {"date": "2026-01-02"},
        "nextBusinessDay": {"date": "2026-01-05"},
    }}).encode()


def _credential(tmp_path):
    cred = tmp_path / ".env.toss"
    cred.write_text("TOSS_CLIENT_ID=real-id\nTOSS_CLIENT_SECRET=real-secret\n")
    os.chmod(cred, 0o600)
    return cred


ANCHOR = date(2026, 7, 31)  # a Friday
NOW = datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc)


def _clock():
    return NOW


def _forbid_socket(monkeypatch, sink):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: sink.append(a))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: sink.append(a))


def _weekday_bodies(builder, count_days: int):
    """One calendar body per descending calendar date; open on Mon-Fri."""
    return tuple(
        _JsonResponse(builder(ANCHOR - timedelta(days=offset),
                              (ANCHOR - timedelta(days=offset)).weekday() < 5))
        for offset in range(count_days))


def _kernel(tmp_path):
    return OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")


def _capture_gate(deps, store) -> None:
    # Building any data provider factory captures the gate the session-date
    # source reads, exactly as the calendar stage does before trades run.
    deps.orders_provider_factory(RequestGateV1(store))


def _counter(kernel_path, day: date) -> int:
    connection = sqlite3.connect(kernel_path)
    try:
        row = connection.execute(
            "SELECT provider_started_count FROM request_day_counters WHERE utc_day=?",
            (day.isoformat(),)).fetchone()
    finally:
        connection.close()
    return 0 if row is None else row[0]


def _calendar_started(kernel_path) -> int:
    connection = sqlite3.connect(kernel_path)
    try:
        return connection.execute(
            "SELECT COUNT(*) FROM planned_requests WHERE started_at IS NOT NULL "
            "AND operation IN ('CALENDAR_DAY_KR','CALENDAR_DAY_US')").fetchone()[0]
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def _restore_socket():
    saved = (socket.getaddrinfo, socket.create_connection)
    yield
    socket.getaddrinfo, socket.create_connection = saved


# ---------------------------------------------------------------------------
# 1. Twenty KR session dates from a bounded descending walk; separate budget.
# ---------------------------------------------------------------------------

def test_kr_prefetch_collects_twenty_sessions_and_caches_them(tmp_path, monkeypatch):
    _credential(tmp_path)
    real_socket: list = []
    _forbid_socket(monkeypatch, real_socket)
    wire: list = []
    store = _kernel(tmp_path)
    kernel_path = tmp_path / "ops" / "ops_kernel.sqlite3"
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=_clock,
        https_handler_factory=_handler_factory(
            (_JsonResponse(_token_body()),) + _weekday_bodies(_kr_body, 30), wire))
    _capture_gate(deps, store)

    dates = deps.regular_session_dates.regular_session_dates(Market.KR, ANCHOR, 20)

    assert len(dates) == 20
    assert all(isinstance(d, date) for d in dates)
    assert all(d.weekday() < 5 for d in dates)        # every result is a weekday
    assert all(d <= ANCHOR for d in dates)
    assert len(set(dates)) == 20                       # no duplicates
    assert dates == tuple(sorted(dates, reverse=True))  # descending
    assert real_socket == []

    # Exactly one token POST then one calendar GET per examined date (26 to reach
    # twenty weekday sessions across five weekends of holidays-free calendar).
    assert wire[0] == ("POST", "/oauth2/token")
    gets = [entry for entry in wire if entry[1] == "/api/v1/market-calendar/KR"]
    assert len(gets) == 26 and len(gets) <= 35

    # The calendar probes charge the separate calendar budget, never the 3744
    # provider ceiling: only the one TOKEN start advanced it.
    assert _counter(kernel_path, NOW.date()) == 1
    assert _calendar_started(kernel_path) == 26

    # The sanitized cache is owner-only and holds only market/date/open facts.
    cache_path = tmp_path / "ops" / "g2b_session_dates.sqlite3"
    assert cache_path.exists()
    assert (os.stat(cache_path).st_mode & 0o777) == 0o600
    connection = sqlite3.connect(cache_path)
    try:
        cols = [row[1] for row in connection.execute("PRAGMA table_info(session_open)")]
        rows = connection.execute("SELECT COUNT(*) FROM session_open").fetchone()[0]
    finally:
        connection.close()
    assert cols == ["market", "local_date", "is_open"]
    assert rows == 26


# ---------------------------------------------------------------------------
# 2. The durable cache is reused with zero new calendar wire.
# ---------------------------------------------------------------------------

def test_second_call_reuses_cache_without_new_wire(tmp_path, monkeypatch):
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    wire: list = []
    store = _kernel(tmp_path)
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=_clock,
        https_handler_factory=_handler_factory(
            (_JsonResponse(_token_body()),) + _weekday_bodies(_kr_body, 30), wire))
    _capture_gate(deps, store)

    first = deps.regular_session_dates.regular_session_dates(Market.KR, ANCHOR, 20)
    wire_after_first = list(wire)
    second = deps.regular_session_dates.regular_session_dates(Market.KR, ANCHOR, 20)

    assert first == second
    assert wire == wire_after_first          # not one extra transport on reuse


# ---------------------------------------------------------------------------
# 3. US prefetch uses the top-level regularMarket shape.
# ---------------------------------------------------------------------------

def test_us_prefetch_collects_twenty_sessions(tmp_path, monkeypatch):
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    wire: list = []
    store = _kernel(tmp_path)
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=_clock,
        https_handler_factory=_handler_factory(
            (_JsonResponse(_token_body()),) + _weekday_bodies(_us_body, 30), wire))
    _capture_gate(deps, store)

    dates = deps.regular_session_dates.regular_session_dates(Market.US, ANCHOR, 20)
    assert len(dates) == 20
    assert all(d.weekday() < 5 for d in dates)
    gets = [entry for entry in wire if entry[1] == "/api/v1/market-calendar/US"]
    assert len(gets) == 26


# ---------------------------------------------------------------------------
# 4. Not enough sessions within the bound -> STOP session_dates_incomplete.
# ---------------------------------------------------------------------------

def test_incomplete_within_thirty_five_dates_stops(tmp_path, monkeypatch):
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    wire: list = []
    store = _kernel(tmp_path)
    # Every examined date is a closed day, so twenty sessions are never found.
    closed = tuple(_JsonResponse(_kr_body(ANCHOR - timedelta(days=offset), False))
                   for offset in range(40))
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=_clock,
        https_handler_factory=_handler_factory(
            (_JsonResponse(_token_body()),) + closed, wire))
    _capture_gate(deps, store)

    with pytest.raises(G2AStopError, match="session_dates_incomplete"):
        deps.regular_session_dates.regular_session_dates(Market.KR, ANCHOR, 20)

    gets = [entry for entry in wire if entry[1] == "/api/v1/market-calendar/KR"]
    assert len(gets) == 35            # walked exactly the bound, then stopped


# ---------------------------------------------------------------------------
# 4b. A calendar that echoes an adjacent date STOPs before caching (M1).
# ---------------------------------------------------------------------------

def test_adjacent_date_echo_stops_before_cache(tmp_path, monkeypatch):
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    wire: list = []
    store = _kernel(tmp_path)
    kernel_path = tmp_path / "ops" / "ops_kernel.sqlite3"
    # We probe the anchor date (a Friday), but the official calendar answers with
    # the *previous* business day's date — the kind of weekend/holiday echo a
    # real calendar can return.  Caching that as the anchor's fact would be a
    # silent off-by-one, so the walk STOPs before any cache put.
    echoed = ANCHOR - timedelta(days=1)
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=_clock,
        https_handler_factory=_handler_factory((
            _JsonResponse(_token_body()),
            _JsonResponse(_kr_body(echoed, True)),   # today.date != requested date
        ), wire))
    _capture_gate(deps, store)

    with pytest.raises(G2AStopError, match="calendar_date_mismatch"):
        deps.regular_session_dates.regular_session_dates(Market.KR, ANCHOR, 20)

    # Exactly one calendar attempt, its consumed start durably settled, and the
    # sanitized cache holds nothing (no wrong-date fact was written).
    gets = [entry for entry in wire if entry[1] == "/api/v1/market-calendar/KR"]
    assert len(gets) == 1
    assert _calendar_started(kernel_path) == 1
    cache_path = tmp_path / "ops" / "g2b_session_dates.sqlite3"
    connection = sqlite3.connect(cache_path)
    try:
        rows = connection.execute("SELECT COUNT(*) FROM session_open").fetchone()[0]
    finally:
        connection.close()
    assert rows == 0


# ---------------------------------------------------------------------------
# 5. A redirect mid-prefetch STOPs without retry, its start consumed.
# ---------------------------------------------------------------------------

def test_redirect_mid_prefetch_stops_without_retry(tmp_path, monkeypatch):
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    wire: list = []
    store = _kernel(tmp_path)
    kernel_path = tmp_path / "ops" / "ops_kernel.sqlite3"
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=_clock,
        https_handler_factory=_handler_factory((
            _JsonResponse(_token_body()),
            _redirect("https://openapi.tossinvest.com/api/v1/market-calendar/KR"),
        ), wire))
    _capture_gate(deps, store)

    with pytest.raises(G2AStopError, match="redirect"):
        deps.regular_session_dates.regular_session_dates(Market.KR, ANCHOR, 20)

    # Exactly one calendar attempt, no follow; the consumed start is durable.
    gets = [entry for entry in wire if entry[1] == "/api/v1/market-calendar/KR"]
    assert len(gets) == 1
    assert _calendar_started(kernel_path) == 1
    connection = sqlite3.connect(kernel_path)
    try:
        state = connection.execute(
            "SELECT state, exit FROM planned_requests WHERE job_scope='CALENDAR' "
            "AND started_at IS NOT NULL").fetchone()
    finally:
        connection.close()
    assert state[0] == "SETTLED" and state[1] == "REDIRECT"
