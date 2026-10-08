"""G2-B one-tick canary runner — offline via scripted HTTPS handlers (no socket).

Drives the whole production A-E cadence for one tick through the composed live
G2-B deps, with every Toss response scripted, so the US-open/KR-closed gate,
the KR-quotes-zero guarantee, the reused durable day counter, the single new B
token identity, and the untouched-production invariant are all exercised end to
end without a real socket, DNS, credential, or production database.
"""

from datetime import date, datetime, timedelta, timezone
from email.message import Message
import io
import json
import os
import socket
import sqlite3

import pytest

from tokmo.composition import build_g2b_live_deps
from tokmo.programs.g2a_shadow_runner import (
    G2APreflightError, G2AStopError, stable_run_id,
)
from tokmo.programs.g2b_canary_runner import (
    _fingerprint, _production_paths, _reconcile_production, _snapshot_production,
    run_b_canary, shadow_root,
)
from tokmo.records.ops_kernel import OpsKernelStore

import urllib.parse
import urllib.request
import urllib.response


# ---------------------------------------------------------------------------
# Scripted-HTTPS fixtures shared across the file.
# ---------------------------------------------------------------------------

def _JsonResponse(body: bytes):
    headers = Message()
    response = urllib.response.addinfourl(
        io.BytesIO(body), headers, "https://openapi.tossinvest.com/x", code=200)
    setattr(response, "msg", "OK")
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


def _kr_closed_body(day: date) -> bytes:
    return json.dumps({"result": {
        "today": {"date": day.isoformat()},
        "previousBusinessDay": {"date": "2026-07-30"},
        "nextBusinessDay": {"date": "2026-08-03"},
    }}).encode()


def _kr_open_body(day: date) -> bytes:
    return json.dumps({"result": {
        "today": {"date": day.isoformat(), "integrated": {"regularMarket": {
            "startTime": f"{day.isoformat()}T00:00:00+00:00",
            "endTime": f"{day.isoformat()}T23:00:00+00:00"}}},
        "previousBusinessDay": {"date": "2026-07-30"},
        "nextBusinessDay": {"date": "2026-08-03"},
    }}).encode()


def _us_open_body(day: date) -> bytes:
    return json.dumps({"result": {
        "today": {"date": day.isoformat(), "regularMarket": {
            "startTime": f"{day.isoformat()}T13:30:00+00:00",
            "endTime": f"{day.isoformat()}T20:00:00+00:00"}},
        "previousBusinessDay": {"date": "2026-07-30"},
        "nextBusinessDay": {"date": "2026-08-03"},
    }}).encode()


def _us_closed_body(day: date) -> bytes:
    return json.dumps({"result": {
        "today": {"date": day.isoformat()},
        "previousBusinessDay": {"date": "2026-07-30"},
        "nextBusinessDay": {"date": "2026-08-03"},
    }}).encode()


def _accounts_body(*seqs: str) -> bytes:
    return json.dumps({"result": [{"accountSeq": seq} for seq in seqs]}).encode()


def _us_and_kr_holdings_body() -> bytes:
    return json.dumps({"result": {"items": [
        {"symbol": "AAPL", "quantity": "10", "averagePrice": "150",
         "market": "US", "currency": "USD"},
        {"symbol": "005930", "quantity": "5", "averagePrice": "70000",
         "market": "KR", "currency": "KRW"},
    ]}}).encode()


def _price_body() -> bytes:
    # One valid current-price fact per requested US symbol (the US holding is
    # AAPL; the KR holding is dropped upstream by the closed KR session), so the
    # single US quote batch actually parses, stores, and receipts green.
    return json.dumps({"result": [
        {"symbol": "AAPL", "lastPrice": "150.25", "currency": "USD",
         "timestamp": "2026-07-31T14:00:00+00:00"},
    ]}).encode()


def _kr_prefetch_open(day: date) -> bytes:
    # A past KR regular-session date (top-level integrated.regularMarket present).
    return _kr_open_body(day)


def _kr_prefetch_bodies(anchor: date, count: int = 20):
    # One open KR calendar body per descending date, so exactly ``count`` probes
    # find ``count`` regular sessions.
    return tuple(
        _JsonResponse(_kr_prefetch_open(anchor - timedelta(days=offset)))
        for offset in range(count))


def _us_prefetch_bodies(anchor: date, count: int = 20):
    # One open US calendar body per descending date: the mixed US+KR account
    # anchors each classified market's own session-date window (no KR-only
    # shortcut), so the US window is prefetched exactly like the KR one.
    return tuple(
        _JsonResponse(_us_open_body(anchor - timedelta(days=offset)))
        for offset in range(count))


def _orders_empty_closed() -> bytes:
    return json.dumps({"result": {
        "orders": [], "nextCursor": None, "hasNext": False}}).encode()


def _completing_tick_responses(day: date):
    """Every scripted response for one US-open/KR-closed tick that runs trades.

    The mixed US+KR account anchors each classified market's own session-date
    window (Stage 2 removed the mixed->KR shortcut), so the real session-date
    source prefetches twenty KR sessions and twenty US sessions, then the
    CLOSED orders page comes back empty (no order details needed).
    """
    return (
        _JsonResponse(_token_body()),
        _JsonResponse(_kr_closed_body(day)),       # session gate KR (closed today)
        _JsonResponse(_us_open_body(day)),         # session gate US (open now)
        _JsonResponse(_accounts_body("1001")),
        _JsonResponse(_us_and_kr_holdings_body()),
        _JsonResponse(_price_body()),              # one US quote batch
    ) + _kr_prefetch_bodies(day, 20) + _us_prefetch_bodies(day, 20) + (
        _JsonResponse(_orders_empty_closed()),     # one CLOSED page, empty
    )


def _credential(tmp_path):
    cred = tmp_path / ".env.toss"
    cred.write_text("TOSS_CLIENT_ID=real-id\nTOSS_CLIENT_SECRET=real-secret\n")
    os.chmod(cred, 0o600)
    return cred


# A wall instant where US regular is open (13:30-20:00 UTC) and KR is closed.
START = datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc)


def _clock():
    return START


def _forbid_socket(monkeypatch, sink):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: sink.append(a))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: sink.append(a))


def _paths(wire):
    return [entry[1] for entry in wire]


def _run(tmp_path, responses, monkeypatch):
    _credential(tmp_path)
    real_socket: list = []
    _forbid_socket(monkeypatch, real_socket)
    wire: list = []
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=_clock,
        https_handler_factory=_handler_factory(responses, wire))
    report = run_b_canary(
        home=tmp_path, utc_day=START.date(), deps=deps, credential_path=None,
        clock=_clock, open_handle_probe=lambda _p: 0)
    return report, wire, real_socket


@pytest.fixture(autouse=True)
def _restore_socket():
    saved = (socket.getaddrinfo, socket.create_connection)
    yield
    socket.getaddrinfo, socket.create_connection = saved


# ---------------------------------------------------------------------------
# 1. Full US-open / KR-closed one-tick canary through the composed live deps.
# ---------------------------------------------------------------------------

def test_us_open_kr_closed_one_tick_quotes_us_only_and_leaves_production_untouched(
        tmp_path, monkeypatch):
    day = START.date()
    report, wire, real_socket = _run(
        tmp_path, _completing_tick_responses(day), monkeypatch)

    assert report.tick_count == 1
    assert report.token_started_count == 1
    assert report.accounts_started_count == 1
    assert report.account_holdings_started_count == 1
    # Two session-gate calendars plus twenty KR and twenty US session-date
    # prefetch probes (each classified market anchors its own window).
    assert report.calendar_started_count == 42
    # Only the US holding is quoted; KR is closed and dropped upstream.
    assert report.us_quote_batch_count == 1
    assert report.kr_quote_count == 0
    # Completed CLOSED trades really queried now that session dates are available.
    assert report.orders_page_count == 1
    assert report.order_detail_count == 0             # the CLOSED page was empty
    assert report.production_invariant_ok is True
    assert real_socket == []

    # The leading fixed prologue: token, both gate calendars, accounts, holdings,
    # one price GET.
    assert _paths(wire)[:6] == [
        "/oauth2/token", "/api/v1/market-calendar/KR", "/api/v1/market-calendar/US",
        "/api/v1/accounts", "/api/v1/holdings", "/api/v1/prices",
    ]
    # Twenty descending KR session-date probes, then exactly one CLOSED orders GET.
    assert _paths(wire).count("/api/v1/market-calendar/KR") == 21   # 1 gate + 20 prefetch
    assert _paths(wire).count("/api/v1/orders") == 1
    # A single price GET means KR was never quoted.
    assert _paths(wire).count("/api/v1/prices") == 1

    # Production kernel and its sidecars remain absent after the run.
    for path, fingerprint in _snapshot_production(_production_paths(tmp_path)):
        assert fingerprint == ("absent",), path


def test_day_counter_is_reused_from_a_prior_run_not_reset(tmp_path, monkeypatch):
    # Seed a completed G2-A-style TOKEN so the shared day counter reads 1, then
    # prove the canary advances it rather than minting a fresh counter.
    from tokmo.composition import _g2a_plan_start_token, _g2a_settle_token
    from tokmo.programs.g2b_canary_runner import _private_dir
    day = START.date()
    root = shadow_root(tmp_path, day)
    _private_dir(root / "ops")
    seed = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")
    _g2a_plan_start_token(seed, START)
    _g2a_settle_token(seed, "OK", START)

    report, _wire, _socket = _run(
        tmp_path, _completing_tick_responses(day), monkeypatch)

    # A(1) + B TOKEN + ACCOUNTS + ACCOUNT_HOLDINGS + PRICE_BATCH + ORDERS_PAGE = 6.
    # (The twenty calendar prefetch probes spend the separate calendar budget.)
    assert report.provider_started_count == 6
    connection = sqlite3.connect(root / "ops" / "ops_kernel.sqlite3")
    try:
        tokens = connection.execute(
            "SELECT job_scope, ordinal, state FROM planned_requests "
            "WHERE operation='TOKEN' ORDER BY job_scope").fetchall()
    finally:
        connection.close()
    # A's terminal CALENDAR token is preserved; B added exactly one HOLDINGS one.
    assert tokens == [("CALENDAR", 0, "SETTLED"), ("HOLDINGS", 0, "SETTLED")]


# ---------------------------------------------------------------------------
# 2. Gate policy: KR open or US closed STOPs before any data provider.
# ---------------------------------------------------------------------------

def test_kr_open_stops_before_any_data_provider(tmp_path, monkeypatch):
    day = START.date()
    responses = (
        _JsonResponse(_token_body()),
        _JsonResponse(_kr_open_body(day)),
        _JsonResponse(_us_open_body(day)),
    )
    with pytest.raises(G2AStopError, match="kr_must_be_closed"):
        _run(tmp_path, responses, monkeypatch)
    # STOP is at the gate: no accounts/holdings/prices GET ever happened.
    root = shadow_root(tmp_path, day)
    state = json.loads((root / "g2b_run_state.json").read_text())
    assert state["status"] == "STOPPED"


def test_us_closed_stops_before_any_data_provider(tmp_path, monkeypatch):
    day = START.date()
    responses = (
        _JsonResponse(_token_body()),
        _JsonResponse(_kr_closed_body(day)),
        _JsonResponse(_us_closed_body(day)),
    )
    with pytest.raises(G2AStopError, match="us_not_open"):
        _run(tmp_path, responses, monkeypatch)


def test_gate_stop_makes_no_data_wire(tmp_path, monkeypatch):
    day = START.date()
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    wire: list = []
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=_clock,
        https_handler_factory=_handler_factory((
            _JsonResponse(_token_body()), _JsonResponse(_kr_open_body(day)),
            _JsonResponse(_us_open_body(day)),
        ), wire))
    with pytest.raises(G2AStopError, match="kr_must_be_closed"):
        run_b_canary(home=tmp_path, utc_day=day, deps=deps, credential_path=None,
                     clock=_clock, open_handle_probe=lambda _p: 0)
    paths = _paths(wire)
    assert "/api/v1/accounts" not in paths
    assert "/api/v1/holdings" not in paths
    assert "/api/v1/prices" not in paths


# ---------------------------------------------------------------------------
# 2b. A tick whose data is not green STOPs on the receipt gate, commits nothing.
# ---------------------------------------------------------------------------

def _empty_price_responses(day: date):
    """A US-open/KR-closed tick whose price body is empty: holdings and trades
    go green but the quotes receipt fails, so the whole tick is not green."""
    return (
        _JsonResponse(_token_body()),
        _JsonResponse(_kr_closed_body(day)),
        _JsonResponse(_us_open_body(day)),
        _JsonResponse(_accounts_body("1001")),
        _JsonResponse(_us_and_kr_holdings_body()),
        _JsonResponse(json.dumps({"result": []}).encode()),   # empty US quote batch
    ) + _kr_prefetch_bodies(day, 20) + _us_prefetch_bodies(day, 20) + (
        _JsonResponse(_orders_empty_closed()),
    )


def test_non_green_data_tick_stops_and_commits_nothing(tmp_path, monkeypatch):
    day = START.date()
    with pytest.raises(G2AStopError, match="data_tick_not_green"):
        _run(tmp_path, _empty_price_responses(day), monkeypatch)

    root = shadow_root(tmp_path, day)
    state = json.loads((root / "g2b_run_state.json").read_text())
    assert state["status"] == "STOPPED" and state["stop_code"] == "data_tick_not_green"
    # The one-tick progress row was never committed: the canary did not "run".
    connection = sqlite3.connect(root / "ops" / "g2b_progress.sqlite3")
    try:
        committed = connection.execute("SELECT COUNT(*) FROM g2b_ticks").fetchone()[0]
    finally:
        connection.close()
    assert committed == 0


# ---------------------------------------------------------------------------
# 3. Exactly one tick: a second invocation refuses.
# ---------------------------------------------------------------------------

def test_second_invocation_refuses_to_run_a_second_tick(tmp_path, monkeypatch):
    day = START.date()
    _run(tmp_path, _completing_tick_responses(day), monkeypatch)

    _forbid_socket(monkeypatch, [])
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=_clock,
        https_handler_factory=_handler_factory((), []))
    with pytest.raises(G2AStopError, match="canary_already_ran"):
        run_b_canary(home=tmp_path, utc_day=day, deps=deps, credential_path=None,
                     clock=_clock, open_handle_probe=lambda _p: 0)


# ---------------------------------------------------------------------------
# 4. Preflight: a shadow schema mismatch refuses before any work.
# ---------------------------------------------------------------------------

def test_preflight_refuses_on_shadow_schema_mismatch(tmp_path, monkeypatch):
    day = START.date()
    root = shadow_root(tmp_path, day)
    (root / "ops").mkdir(parents=True)
    connection = sqlite3.connect(root / "ops" / "ops_kernel.sqlite3")
    connection.execute("PRAGMA user_version = 999999")
    connection.commit()
    connection.close()

    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=_clock,
        https_handler_factory=_handler_factory((), []))
    with pytest.raises(G2APreflightError, match="shadow_schema"):
        run_b_canary(home=tmp_path, utc_day=day, deps=deps, credential_path=None,
                     clock=_clock, open_handle_probe=lambda _p: 0)


# ---------------------------------------------------------------------------
# 5. Private canary artifacts are 0600; production fingerprint mismatch STOPs.
# ---------------------------------------------------------------------------

def test_private_canary_artifacts_are_owner_only(tmp_path, monkeypatch):
    day = START.date()
    _run(tmp_path, _completing_tick_responses(day), monkeypatch)
    root = shadow_root(tmp_path, day)
    for leaf in ("g2b_run_state.json", "ops/g2b_progress.sqlite3",
                 "ops/ops_kernel.sqlite3", "ops/g2b_session_dates.sqlite3"):
        mode = os.stat(root / leaf).st_mode & 0o777
        assert mode == 0o600, leaf


def test_reconcile_production_flags_a_mutation(tmp_path):
    paths = _production_paths(tmp_path)
    before = _snapshot_production(paths)
    # No mutation: the prior code (including None) is preserved.
    assert _reconcile_production(paths, before, None) is None
    assert _reconcile_production(paths, before, "some_stop") == "some_stop"
    # A production sidecar appearing is a mutation, and dominates any code.
    kernel = paths[0]
    kernel.parent.mkdir(parents=True, exist_ok=True)
    kernel.write_bytes(b"x")
    assert _reconcile_production(paths, before, None) == "production_mutated"
    assert _reconcile_production(paths, before, "gate_stop") == "production_mutated"


def test_fingerprint_distinguishes_absent_regular_and_symlink(tmp_path):
    assert _fingerprint(tmp_path / "nope") == ("absent",)
    regular = tmp_path / "f"
    regular.write_bytes(b"abc")
    fp = _fingerprint(regular)
    assert fp[0] == "present" and fp[1] == "regular" and fp[3] == 3
    link = tmp_path / "l"
    link.symlink_to(regular)
    assert _fingerprint(link)[1] == "symlink"


# ---------------------------------------------------------------------------
# 6. CLI routing: `g2b --run-b-canary` dispatches and prints masked counts.
# ---------------------------------------------------------------------------

def _fake_report():
    from tokmo.programs.g2b_canary_runner import G2BReport
    return G2BReport(
        run_id="g2a-a-2026-07-31", utc_day="2026-07-31", tick_count=1,
        provider_started_count=4, token_started_count=1, accounts_started_count=1,
        account_holdings_started_count=1, us_quote_batch_count=1, kr_quote_count=0,
        orders_page_count=0, order_detail_count=0, calendar_started_count=2,
        wire_attempt_count=6, production_invariant_ok=True, duration_seconds=0.0,
        per_operation={})


def test_cli_routes_g2b_run_b_canary(monkeypatch, capsys):
    from tokmo import composition

    calls: dict = {}

    def _deps(**kwargs):
        calls["deps"] = kwargs
        return object()

    def _run_stub(**kwargs):
        calls["run"] = kwargs
        return _fake_report()

    monkeypatch.setattr(composition, "build_g2b_live_deps", _deps)
    monkeypatch.setattr(composition, "run_b_canary", _run_stub)
    code = composition.main(["g2b", "--run-b-canary"])
    out = capsys.readouterr().out
    assert code == 0
    assert "deps" in calls and "run" in calls          # the g2b branch was taken
    assert "g2b run=complete" in out
    assert "kr_quotes=0" in out and "us_quote_batches=1" in out
    assert "production_ok=True" in out


def test_cli_g2b_stop_returns_two(monkeypatch, capsys):
    from tokmo import composition

    def _stop(**_kwargs):
        raise G2AStopError("kr_must_be_closed")

    monkeypatch.setattr(composition, "build_g2b_live_deps", lambda **k: object())
    monkeypatch.setattr(composition, "run_b_canary", _stop)
    code = composition.main(["g2b", "--run-b-canary"])
    assert code == 2
    assert "g2b run=stopped reason=kr_must_be_closed" in capsys.readouterr().out


def test_cli_help_lists_g2b(capsys):
    from tokmo import composition
    assert composition.main(["bogus-unknown-command"]) == 64
    assert "g2b --run-b-canary" in capsys.readouterr().err
