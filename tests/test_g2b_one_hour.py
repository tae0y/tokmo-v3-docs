"""G2-B one-hour twelve-tick canary campaign — offline (no socket).

Drives ``run_b_one_hour`` end to end through the composed live G2-B deps with
every Toss response dispatched by path (and calendar date), so the twelve-tick
grid, the first-tick canary gate, the one shared token, the cumulative
non-rearmable day counter, the twelve metrics rows, the production invariant
around the whole hour, and the resume/refuse run-state semantics are all
exercised without a real socket, DNS, credential, or production database.
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

from tokmo.composition import (
    _g2a_plan_start_token, _g2a_settle_token, build_g2b_live_deps,
)
from tokmo.programs.g2a_shadow_runner import G2AStopError, stable_run_id
from tokmo.programs.g2b_canary_runner import (
    _private_dir, _production_paths, _snapshot_production, run_b_one_hour,
    shadow_root,
)
from tokmo.records.ops_kernel import OpsKernelStore


# ---------------------------------------------------------------------------
# Path/date-dispatched scripted HTTPS (robust across an unknown call count).
# ---------------------------------------------------------------------------

def _JsonResponse(body: bytes):
    headers = Message()
    response = urllib.response.addinfourl(
        io.BytesIO(body), headers, "https://openapi.tossinvest.com/x", code=200)
    setattr(response, "msg", "OK")
    return response


def _token_body() -> bytes:
    return json.dumps({
        "access_token": "a" * 40, "expires_in": 36000, "token_type": "Bearer",
    }).encode()


def _kr_open(day: date, start="00:00:00", end="06:30:00") -> bytes:
    return json.dumps({"result": {
        "today": {"date": day.isoformat(), "integrated": {"regularMarket": {
            "startTime": f"{day.isoformat()}T{start}+00:00",
            "endTime": f"{day.isoformat()}T{end}+00:00"}}},
        "previousBusinessDay": {"date": "2026-01-02"},
        "nextBusinessDay": {"date": "2026-01-05"}}}).encode()


def _kr_closed(day: date) -> bytes:
    return json.dumps({"result": {
        "today": {"date": day.isoformat()},
        "previousBusinessDay": {"date": "2026-01-02"},
        "nextBusinessDay": {"date": "2026-01-05"}}}).encode()


def _us_open(day: date, start="13:30:00", end="20:00:00") -> bytes:
    return json.dumps({"result": {
        "today": {"date": day.isoformat(), "regularMarket": {
            "startTime": f"{day.isoformat()}T{start}+00:00",
            "endTime": f"{day.isoformat()}T{end}+00:00"}},
        "previousBusinessDay": {"date": "2026-01-02"},
        "nextBusinessDay": {"date": "2026-01-05"}}}).encode()


def _accounts_body(*seqs: str) -> bytes:
    return json.dumps({"result": [{"accountSeq": s} for s in seqs]}).encode()


def _holdings_body() -> bytes:
    return json.dumps({"result": {"items": [
        {"symbol": "AAPL", "quantity": "10", "averagePrice": "150",
         "market": "US", "currency": "USD"},
        {"symbol": "005930", "quantity": "5", "averagePrice": "70000",
         "market": "KR", "currency": "KRW"},
    ]}}).encode()


def _price_body() -> bytes:
    # One valid current-price fact per requested US symbol (the US holding is
    # AAPL; the KR holding is dropped upstream by the closed KR session), so the
    # quotes parser/store/pack/receipt path actually proves green.
    return json.dumps({"result": [
        {"symbol": "AAPL", "lastPrice": "150.25", "currency": "USD",
         "timestamp": "2026-07-31T14:00:00+00:00"},
    ]}).encode()


def _orders_empty() -> bytes:
    return json.dumps({"result": {
        "orders": [], "nextCursor": None, "hasNext": False}}).encode()


DAY = date(2026, 7, 31)
START = datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc)


def _dispatch_handler(wire, *, kr_gate=_kr_closed, us_gate=_us_open,
                      price=lambda call_index: _price_body()):
    """Path/date-dispatched scripted HTTPS.

    ``price`` is a callable ``(call_index) -> bytes`` returning the price body
    for the Nth ``/api/v1/prices`` GET, so a test can make one particular tick's
    quotes fail (an empty body) while the rest stay green.
    """
    price_calls = {"n": 0}

    def _query_date(query: str) -> date | None:
        for key, value in urllib.parse.parse_qsl(query):
            if key == "date":
                return date.fromisoformat(value)
        return None

    class _Dispatch(urllib.request.HTTPSHandler):
        def https_open(self, request):
            parts = urllib.parse.urlsplit(request.full_url)
            path = parts.path
            wire.append((request.get_method(), path))
            if path == "/oauth2/token":
                return _JsonResponse(_token_body())
            if path == "/api/v1/market-calendar/KR":
                queried = _query_date(parts.query)
                # Today's gate fact vs. a past session-date prefetch fact.
                if queried == DAY:
                    return _JsonResponse(kr_gate(DAY))
                return _JsonResponse(_kr_open(queried))
            if path == "/api/v1/market-calendar/US":
                queried = _query_date(parts.query)
                if queried == DAY:
                    return _JsonResponse(us_gate(DAY))
                return _JsonResponse(_us_open(queried))
            if path == "/api/v1/accounts":
                return _JsonResponse(_accounts_body("1001"))
            if path == "/api/v1/holdings":
                return _JsonResponse(_holdings_body())
            if path == "/api/v1/prices":
                index = price_calls["n"]
                price_calls["n"] += 1
                return _JsonResponse(price(index))
            if path == "/api/v1/orders":
                return _JsonResponse(_orders_empty())
            raise AssertionError(f"unexpected path {path}")

    return _Dispatch


def _credential(tmp_path):
    cred = tmp_path / ".env.toss"
    cred.write_text("TOSS_CLIENT_ID=real-id\nTOSS_CLIENT_SECRET=real-secret\n")
    os.chmod(cred, 0o600)
    return cred


def _forbid_socket(monkeypatch, sink):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: sink.append(a))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: sink.append(a))


def _advancing_clock():
    # A clock the sleeper advances, so the twelve ticks land on their 300s grid
    # and the session verdict is re-judged against real elapsed time.
    state = {"now": START}
    def clock():
        return state["now"]
    def sleeper(seconds):
        state["now"] = state["now"] + timedelta(seconds=seconds)
    return clock, sleeper, state


@pytest.fixture(autouse=True)
def _restore_socket():
    saved = (socket.getaddrinfo, socket.create_connection)
    yield
    socket.getaddrinfo, socket.create_connection = saved


def _metrics_rows(root):
    path = root / "ops" / "metrics" / "g2b_metrics.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _token_rows(root):
    connection = sqlite3.connect(root / "ops" / "ops_kernel.sqlite3")
    try:
        return connection.execute(
            "SELECT job_scope, ordinal, state FROM planned_requests "
            "WHERE operation='TOKEN' ORDER BY job_scope").fetchall()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 1. A green twelve-tick hour: one token, twelve metrics rows, KR quotes zero.
# ---------------------------------------------------------------------------

def test_green_twelve_tick_hour(tmp_path, monkeypatch):
    _credential(tmp_path)
    real_socket: list = []
    _forbid_socket(monkeypatch, real_socket)
    wire: list = []
    clock, sleeper, _state = _advancing_clock()
    sleeps: list = []
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=clock,
        https_handler_factory=_dispatch_handler(wire))
    report = run_b_one_hour(
        home=tmp_path, utc_day=DAY, deps=deps, credential_path=None,
        clock=clock, sleeper=lambda d: (sleeps.append(d), sleeper(d))[-1],
        open_handle_probe=lambda _p: 0)

    assert report.tick_count == 12
    assert report.token_started_count == 1          # exactly one token all hour
    assert report.kr_quote_count == 0
    # US holdings are quoted at least once (stages legitimately skip a tick when
    # their data is already current, so the count is not uniformly twelve).
    assert report.us_quote_batch_count >= 1
    assert report.orders_page_count >= 1            # completed trades really ran
    assert report.production_invariant_ok is True
    assert real_socket == []

    # Twelve metrics rows, one per scheduled tick, private-safe (no raw values).
    rows = _metrics_rows(shadow_root(tmp_path, DAY))
    assert len(rows) == 12
    assert [r["tick_index"] for r in rows] == list(range(12))
    for row in rows:
        # The masked row carries only session-safe aggregates (US batches are
        # derivable from started PRICE_BATCH; KR quotes are structurally zero).
        assert set(row) >= {"scheduled_at", "started_at", "ended_at",
                            "duration_seconds", "lag_seconds", "started_per_operation",
                            "receipts", "wire_attempt_count"}
        assert row["started_per_operation"].get("PRICE_BATCH", 0) >= 0
        assert "005930" not in json.dumps(row)      # no raw KR symbol leaked

    # No catch-up: eleven positive inter-tick sleeps of one grid step each.
    assert sleeps == [300.0] * 11

    # One token identity persisted under HOLDINGS scope, settled OK.
    assert _token_rows(shadow_root(tmp_path, DAY)) == [("HOLDINGS", 0, "SETTLED")]


# ---------------------------------------------------------------------------
# 2. Cumulative day counter: A(1) -> B token = 2, never re-armed.
# ---------------------------------------------------------------------------

def test_day_counter_carries_forward_from_a_prior_run(tmp_path, monkeypatch):
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    root = shadow_root(tmp_path, DAY)
    _private_dir(root / "ops")
    seed = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")
    _g2a_plan_start_token(seed, START)         # A's terminal CALENDAR token
    _g2a_settle_token(seed, "OK", START)

    wire: list = []
    clock, sleeper, _state = _advancing_clock()
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=clock,
        https_handler_factory=_dispatch_handler(wire))
    report = run_b_one_hour(
        home=tmp_path, utc_day=DAY, deps=deps, credential_path=None,
        clock=clock, sleeper=sleeper, open_handle_probe=lambda _p: 0)

    assert report.tick_count == 12
    # Both tokens present: A's terminal CALENDAR one and B's single HOLDINGS one.
    assert _token_rows(root) == [("CALENDAR", 0, "SETTLED"), ("HOLDINGS", 0, "SETTLED")]
    # The shared non-rearmable provider counter advanced past A's 1 by the whole
    # campaign's non-calendar starts (never reset to a fresh counter).
    assert report.provider_started_count > 1
    assert report.token_started_count == 2


# ---------------------------------------------------------------------------
# 3. First tick is the canary: a wrong session shape STOPs with no continuation.
# ---------------------------------------------------------------------------

def test_first_tick_canary_failure_stops_before_any_continuation(tmp_path, monkeypatch):
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    wire: list = []
    clock, sleeper, _state = _advancing_clock()
    sleeps: list = []
    # A KR regular session that spans the 14:00 UTC wall instant makes KR judged
    # open now, so the canary's KR-must-be-closed rule trips on the first tick.
    kr_open_now = lambda day: _kr_open(day, start="13:00:00", end="15:00:00")
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=clock,
        https_handler_factory=_dispatch_handler(wire, kr_gate=kr_open_now))
    with pytest.raises(G2AStopError, match="kr_must_be_closed"):
        run_b_one_hour(
            home=tmp_path, utc_day=DAY, deps=deps, credential_path=None,
            clock=clock, sleeper=lambda d: (sleeps.append(d), sleeper(d))[-1],
            open_handle_probe=lambda _p: 0)

    root = shadow_root(tmp_path, DAY)
    state = json.loads((root / "g2b_run_state.json").read_text())
    assert state["status"] == "STOPPED" and state["stop_code"] == "kr_must_be_closed"
    # No later tick was scheduled: no inter-tick sleep happened after the canary.
    assert sleeps == []
    # No tick was durably committed.
    connection = sqlite3.connect(root / "ops" / "g2b_hour_progress.sqlite3")
    try:
        committed = connection.execute("SELECT COUNT(*) FROM g2b_hour_ticks").fetchone()[0]
    finally:
        connection.close()
    assert committed == 0
    # No accounts/holdings/prices/orders wire fired behind the closed gate.
    paths = {entry[1] for entry in wire}
    assert "/api/v1/accounts" not in paths
    assert "/api/v1/orders" not in paths


# ---------------------------------------------------------------------------
# 4. A later tick that loses the session STOPs mid-hour.
# ---------------------------------------------------------------------------

def test_later_tick_session_loss_stops(tmp_path, monkeypatch):
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    wire: list = []
    clock, sleeper, _state = _advancing_clock()
    # US regular closes at 14:12, so ticks at 14:00/14:05/14:10 are open and the
    # 14:15 tick (index 3) is after close.
    us_short = lambda day: _us_open(day, end="14:12:00")
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=clock,
        https_handler_factory=_dispatch_handler(wire, us_gate=us_short))
    with pytest.raises(G2AStopError, match="us_not_open"):
        run_b_one_hour(
            home=tmp_path, utc_day=DAY, deps=deps, credential_path=None,
            clock=clock, sleeper=sleeper, open_handle_probe=lambda _p: 0)

    root = shadow_root(tmp_path, DAY)
    connection = sqlite3.connect(root / "ops" / "g2b_hour_progress.sqlite3")
    try:
        committed = connection.execute(
            "SELECT tick_index FROM g2b_hour_ticks ORDER BY tick_index").fetchall()
    finally:
        connection.close()
    # Three ticks completed before the session was lost at the fourth.
    assert [r[0] for r in committed] == [0, 1, 2]


def _empty_price(_call_index: int) -> bytes:
    # A structurally empty price body: the US quote batch parses to zero facts,
    # so the quotes receipt is ``failed_provider_contract`` (not green).
    return json.dumps({"result": []}).encode()


# ---------------------------------------------------------------------------
# 4b. The first (canary) tick is not green: STOP before any continuation.
# ---------------------------------------------------------------------------

def test_first_tick_data_failure_stops_before_any_continuation(tmp_path, monkeypatch):
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    wire: list = []
    clock, sleeper, _state = _advancing_clock()
    sleeps: list = []
    # The session gate passes (US open, KR closed) but every price body is empty,
    # so the first tick's quotes never go green: the canary STOPs on the receipt
    # gate, not the session gate.
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=clock,
        https_handler_factory=_dispatch_handler(wire, price=_empty_price))
    with pytest.raises(G2AStopError, match="data_tick_not_green"):
        run_b_one_hour(
            home=tmp_path, utc_day=DAY, deps=deps, credential_path=None,
            clock=clock, sleeper=lambda d: (sleeps.append(d), sleeper(d))[-1],
            open_handle_probe=lambda _p: 0)

    root = shadow_root(tmp_path, DAY)
    state = json.loads((root / "g2b_run_state.json").read_text())
    assert state["status"] == "STOPPED" and state["stop_code"] == "data_tick_not_green"
    # No later tick was scheduled: no inter-tick sleep after the failed canary.
    assert sleeps == []
    # Nothing was durably committed.
    connection = sqlite3.connect(root / "ops" / "g2b_hour_progress.sqlite3")
    try:
        committed = connection.execute("SELECT COUNT(*) FROM g2b_hour_ticks").fetchone()[0]
    finally:
        connection.close()
    assert committed == 0


# ---------------------------------------------------------------------------
# 4c. A later tick that loses its data (not its session) STOPs mid-hour.
# ---------------------------------------------------------------------------

def test_later_tick_data_failure_stops_mid_hour(tmp_path, monkeypatch):
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    wire: list = []
    clock, sleeper, _state = _advancing_clock()
    # Ticks 0,1,2 quote green; the fourth price GET (tick index 3) comes back
    # empty, so tick 3 fails the receipt gate while the session is still open.
    price = lambda call_index: (_empty_price(call_index) if call_index >= 3
                                else _price_body())
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=clock,
        https_handler_factory=_dispatch_handler(wire, price=price))
    with pytest.raises(G2AStopError, match="data_tick_not_green"):
        run_b_one_hour(
            home=tmp_path, utc_day=DAY, deps=deps, credential_path=None,
            clock=clock, sleeper=sleeper, open_handle_probe=lambda _p: 0)

    root = shadow_root(tmp_path, DAY)
    connection = sqlite3.connect(root / "ops" / "g2b_hour_progress.sqlite3")
    try:
        committed = connection.execute(
            "SELECT tick_index FROM g2b_hour_ticks ORDER BY tick_index").fetchall()
    finally:
        connection.close()
    # Three green ticks committed before the data was lost on the fourth.
    assert [r[0] for r in committed] == [0, 1, 2]


# ---------------------------------------------------------------------------
# 4d. A real green hour: every tick's three receipts saved, unique durable rows.
# ---------------------------------------------------------------------------

def test_green_twelve_tick_hour_proves_all_receipts_saved(tmp_path, monkeypatch):
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    wire: list = []
    clock, sleeper, _state = _advancing_clock()
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=clock,
        https_handler_factory=_dispatch_handler(wire))
    report = run_b_one_hour(
        home=tmp_path, utc_day=DAY, deps=deps, credential_path=None,
        clock=clock, sleeper=sleeper, open_handle_probe=lambda _p: 0)
    assert report.tick_count == 12

    root = shadow_root(tmp_path, DAY)
    rows = _metrics_rows(root)
    assert len(rows) == 12
    # Every tick durably saved all three data receipts — no hidden failed/skipped.
    for row in rows:
        assert row["receipts"] == {
            "holdings": "saved", "quotes": "saved", "trades": "saved"}, row["tick_index"]
    # Twelve distinct durable cadence tick ids, one committed row each.
    metric_tick_ids = [row["tick_id"] for row in rows]
    assert len(set(metric_tick_ids)) == 12
    connection = sqlite3.connect(root / "ops" / "g2b_hour_progress.sqlite3")
    try:
        committed = connection.execute(
            "SELECT tick_id FROM g2b_hour_ticks").fetchall()
    finally:
        connection.close()
    assert sorted(r[0] for r in committed) == sorted(metric_tick_ids)

    kernel = sqlite3.connect(root / "ops" / "ops_kernel.sqlite3")
    try:
        dangling = kernel.execute(
            "SELECT operation, tick_id, ordinal, state FROM planned_requests "
            "WHERE started_at IS NULL AND state <> 'CANCELLED_UNSTARTED'").fetchall()
    finally:
        kernel.close()
    assert dangling == []


# ---------------------------------------------------------------------------
# 5. Run-state: COMPLETED refuses a same-day rerun; STOPPED resumes.
# ---------------------------------------------------------------------------

def test_completed_run_refuses_same_day_rerun(tmp_path, monkeypatch):
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    clock, sleeper, _state = _advancing_clock()
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=clock,
        https_handler_factory=_dispatch_handler([]))
    run_b_one_hour(home=tmp_path, utc_day=DAY, deps=deps, credential_path=None,
                   clock=clock, sleeper=sleeper, open_handle_probe=lambda _p: 0)

    clock2, sleeper2, _s2 = _advancing_clock()
    deps2 = build_g2b_live_deps(
        project_root=tmp_path, clock=clock2,
        https_handler_factory=_dispatch_handler([]))
    with pytest.raises(G2AStopError, match="canary_already_complete"):
        run_b_one_hour(home=tmp_path, utc_day=DAY, deps=deps2, credential_path=None,
                       clock=clock2, sleeper=sleeper2, open_handle_probe=lambda _p: 0)


def test_stopped_state_allows_reentry_but_token_guard_stops(tmp_path, monkeypatch):
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    # First invocation loses the session at tick 3 (STOPPED, 0..2 committed, and
    # the one memory-only bearer already acquired durably).
    wire: list = []
    clock, sleeper, _state = _advancing_clock()
    us_short = lambda day: _us_open(day, end="14:12:00")
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=clock,
        https_handler_factory=_dispatch_handler(wire, us_gate=us_short))
    with pytest.raises(G2AStopError, match="us_not_open"):
        run_b_one_hour(home=tmp_path, utc_day=DAY, deps=deps, credential_path=None,
                       clock=clock, sleeper=sleeper, open_handle_probe=lambda _p: 0)

    # A repaired invocation is NOT hard-refused at the run-state gate the way a
    # COMPLETED run is: it re-enters against the same root and non-rearmable
    # counter and re-checks the session on the next tick.  Here the session has
    # since closed, so it STOPs ``us_not_open`` rather than the COMPLETED
    # refusal — proving STOPPED state permits a repaired stable re-invocation.
    clock2, sleeper2, _s2 = _advancing_clock()
    deps2 = build_g2b_live_deps(
        project_root=tmp_path, clock=clock2,
        https_handler_factory=_dispatch_handler([]))
    with pytest.raises(G2AStopError) as excinfo:
        run_b_one_hour(home=tmp_path, utc_day=DAY, deps=deps2, credential_path=None,
                       clock=clock2, sleeper=sleeper2, open_handle_probe=lambda _p: 0)
    assert excinfo.value.code != "canary_already_complete"


# ---------------------------------------------------------------------------
# 6. A production-state mutation around the hour is the dominant STOP.
# ---------------------------------------------------------------------------

def test_production_mutation_around_the_hour_dominates(tmp_path, monkeypatch):
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    clock, sleeper, _state = _advancing_clock()
    # Mutate production (create its kernel) partway through by wrapping the sleeper.
    kernel = _production_paths(tmp_path)[0]

    def _mutating_sleeper(seconds):
        kernel.parent.mkdir(parents=True, exist_ok=True)
        kernel.write_bytes(b"x")
        sleeper(seconds)

    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=clock,
        https_handler_factory=_dispatch_handler([]))
    with pytest.raises(G2AStopError, match="production_mutated"):
        run_b_one_hour(home=tmp_path, utc_day=DAY, deps=deps, credential_path=None,
                       clock=clock, sleeper=_mutating_sleeper, open_handle_probe=lambda _p: 0)


# ---------------------------------------------------------------------------
# 7. CLI routing: `g2b --run-b-one-hour` dispatches and prints masked counts.
# ---------------------------------------------------------------------------

def test_cli_routes_g2b_run_b_one_hour(monkeypatch, capsys):
    from tokmo import composition
    from tokmo.programs.g2b_canary_runner import G2BReport

    calls: dict = {}
    report = G2BReport(
        run_id="g2a-a-2026-07-31", utc_day="2026-07-31", tick_count=12,
        provider_started_count=49, token_started_count=1, accounts_started_count=12,
        account_holdings_started_count=12, us_quote_batch_count=12, kr_quote_count=0,
        orders_page_count=12, order_detail_count=0, calendar_started_count=22,
        wire_attempt_count=21, production_invariant_ok=True, duration_seconds=0.0,
        per_operation={})

    monkeypatch.setattr(composition, "build_g2b_live_deps", lambda **k: object())
    monkeypatch.setattr(composition, "run_b_one_hour",
                        lambda **k: (calls.__setitem__("run", k), report)[1])
    code = composition.main(["g2b", "--run-b-one-hour"])
    out = capsys.readouterr().out
    assert code == 0
    assert "run" in calls and "sleeper" in calls["run"]     # the one-hour branch ran
    assert "g2b run=complete" in out and "ticks=12" in out
    assert "kr_quotes=0" in out and "us_quote_batches=12" in out


def test_cli_help_lists_g2b_one_hour(capsys):
    from tokmo import composition
    assert composition.main(["bogus-unknown-command"]) == 64
    assert "g2b --run-b-one-hour" in capsys.readouterr().err
