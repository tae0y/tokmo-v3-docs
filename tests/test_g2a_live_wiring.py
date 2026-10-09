"""G2-A live-deps wiring — offline via scripted HTTPS handlers (no socket).

Proves the composition-owned live provider factories are real (built from the
production Toss adapters), that the calendar closure performs one token POST
plus one calendar GET and accounts the calendar start, and that every data
factory is fail-closed.  No real socket, DNS, or credential outside the fixture
is exercised.  A fresh handler is minted per adapter (shared response queue and
wire log), so the two openers keep independent error-processing chains.
"""

import io
import json
import os
import socket
import sqlite3
import urllib.parse
import urllib.request
import urllib.response
from datetime import UTC, date, datetime, timedelta
from email.message import Message

import pytest

from tokmo.values.ops_tick import tick_id_from_utc
from tokmo.values.session_gate import Market
from tokmo.values.toss_market_calendar import KrMarketDay


def _JsonResponse(body: bytes):
    """A 200 response the urllib error-processing chain accepts (needs
    ``code``/``msg``/``info()``) and the transport reads (needs
    ``status``/``headers``/``read``)."""
    headers = Message()
    response = urllib.response.addinfourl(
        io.BytesIO(body), headers, "https://openapi.tossinvest.com/x", code=200
    )
    response.msg = "OK"
    return response


def _handler_factory(responses, wire):
    """Return a zero-arg factory minting fresh scripted handlers that share one
    response queue and one wire log."""
    queue = iter(responses)

    class _Scripted(urllib.request.HTTPSHandler):
        def https_open(self, request):
            parts = urllib.parse.urlsplit(request.full_url)
            wire.append((request.get_method(), parts.path))
            return next(queue)

    return _Scripted


def _token_body(expires_in: int = 3600) -> bytes:
    return json.dumps(
        {
            "access_token": "a" * 40,
            "expires_in": expires_in,
            "token_type": "Bearer",
        }
    ).encode()


def _closed_calendar_body(day: date) -> bytes:
    # No "integrated"/session keys → no regular session → closed verdict.
    return json.dumps(
        {
            "result": {
                "today": {"date": day.isoformat()},
                "previousBusinessDay": {"date": "2026-07-30"},
                "nextBusinessDay": {"date": "2026-08-03"},
            }
        }
    ).encode()


def _redirect(url: str):
    headers = Message()
    headers["Location"] = "https://openapi.tossinvest.com/elsewhere"
    response = urllib.response.addinfourl(io.BytesIO(b""), headers, url, code=302)
    response.msg = "Found"
    return response


def _credential(tmp_path):
    cred = tmp_path / ".env.toss"
    cred.write_text("TOSS_CLIENT_ID=real-id\nTOSS_CLIENT_SECRET=real-secret\n")
    os.chmod(cred, 0o600)
    return cred


class _Tick:
    tick_id = 12345
    start_utc = datetime(2026, 7, 31, 7, 0, tzinfo=UTC)


def _clock():
    return datetime(2026, 7, 31, 7, 0, tzinfo=UTC)


def _forbid_socket(monkeypatch, sink):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: sink.append(a))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: sink.append(a))


def test_live_deps_calendar_closure_does_token_then_calendar_and_accounts(
    tmp_path, monkeypatch
):
    from tokmo.composition import build_g2a_live_deps
    from tokmo.programs.integrated_fake_cadence import RequestGateV1
    from tokmo.records.ops_kernel import OpsKernelStore

    _credential(tmp_path)
    real_socket = []
    _forbid_socket(monkeypatch, real_socket)

    day = date(2026, 7, 31)
    wire: list = []
    factory = _handler_factory(
        (_JsonResponse(_token_body()), _JsonResponse(_closed_calendar_body(day))), wire
    )
    deps = build_g2a_live_deps(
        project_root=tmp_path, clock=_clock, https_handler_factory=factory
    )

    kernel = OpsKernelStore(tmp_path / "ops_kernel.sqlite3")
    result = deps.calendar_provider(
        market=Market.KR,
        local_trade_date=day,
        tick=_Tick(),
        request_gate=RequestGateV1(kernel),
    )

    assert isinstance(result, KrMarketDay)
    assert wire == [("POST", "/oauth2/token"), ("GET", "/api/v1/market-calendar/KR")]
    rows = kernel.read_planned_requests(_Tick.tick_id, "CALENDAR")
    assert [(r["operation"], r["state"]) for r in rows] == [
        ("CALENDAR_DAY_KR", "SETTLED")
    ]
    assert real_socket == []


def test_live_deps_data_factories_are_fail_closed(tmp_path):
    from tokmo.composition import build_g2a_live_deps
    from tokmo.programs.g2a_shadow_runner import G2AStopError

    _credential(tmp_path)
    deps = build_g2a_live_deps(project_root=tmp_path, clock=_clock)
    for factory in (
        deps.holdings_provider_factory,
        deps.quotes_provider_factory,
        deps.orders_provider_factory,
        deps.detail_provider_factory,
    ):
        closure = factory(object())
        with pytest.raises(G2AStopError, match="provider_call_on_closed_session"):
            closure()


def test_live_deps_calendar_transport_failure_is_a_stop(tmp_path, monkeypatch):
    from tokmo.composition import build_g2a_live_deps
    from tokmo.programs.g2a_shadow_runner import G2AStopError
    from tokmo.programs.integrated_fake_cadence import RequestGateV1
    from tokmo.records.ops_kernel import OpsKernelStore

    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    wire: list = []
    factory = _handler_factory(
        (
            _JsonResponse(_token_body()),
            _redirect("https://openapi.tossinvest.com/api/v1/market-calendar/KR"),
        ),
        wire,
    )
    deps = build_g2a_live_deps(
        project_root=tmp_path, clock=_clock, https_handler_factory=factory
    )
    kernel = OpsKernelStore(tmp_path / "ops_kernel.sqlite3")
    with pytest.raises(G2AStopError, match="redirect"):
        deps.calendar_provider(
            market=Market.KR,
            local_trade_date=date(2026, 7, 31),
            tick=_Tick(),
            request_gate=RequestGateV1(kernel),
        )
    # The start was reserved before the calendar I/O and is not refunded.
    rows = kernel.read_planned_requests(_Tick.tick_id, "CALENDAR")
    assert [(r["operation"], r["state"]) for r in rows] == [
        ("CALENDAR_DAY_KR", "STARTED")
    ]


# ---------------------------------------------------------------------------
# Full fake-live hour: one metered token, calendar capped one call per market.
# ---------------------------------------------------------------------------


def test_full_hour_acquires_one_token_and_caps_calendar_per_market(
    tmp_path, monkeypatch
):
    """Twelve real ticks through the composed live deps make exactly one auth
    POST and one calendar GET per market; the durable store shows exactly three
    started wire rows (TOKEN + KR + US), with no retry or redirect.

    This kills the per-tick token (only one POST over the hour) and the
    unmetered token (the TOKEN start charges the 3744 day counter once), and
    proves the JSONL/report label wire attempts truthfully — no fabricated
    socket/dns zero.
    """
    from tokmo.composition import build_g2a_live_deps
    from tokmo.programs import g2a_shadow_runner as runner

    _credential(tmp_path)
    real_socket = []
    _forbid_socket(monkeypatch, real_socket)

    start = datetime(2026, 7, 31, 7, 0, tzinfo=UTC)
    now = {"t": start}

    def clock():
        return now["t"]

    def sleeper(seconds):
        now["t"] = now["t"] + timedelta(seconds=seconds)

    day = start.date()
    wire: list = []
    factory = _handler_factory(
        (
            _JsonResponse(_token_body()),
            _JsonResponse(_closed_calendar_body(day)),
            _JsonResponse(_closed_calendar_body(day)),
        ),
        wire,
    )
    deps = build_g2a_live_deps(
        project_root=tmp_path, clock=clock, https_handler_factory=factory
    )

    emitted: list = []
    report = runner.run_a_one_hour(
        home=tmp_path,
        utc_day=day,
        deps=deps,
        credential_path=None,
        clock=clock,
        sleeper=sleeper,
        open_handle_probe=lambda _p: 0,
        metrics_sink=emitted.append,
    )

    assert report.tick_count == 12
    # One auth POST + one calendar GET per market, in order, and nothing else:
    # no retry, no redirect, no second token.
    assert wire == [
        ("POST", "/oauth2/token"),
        ("GET", "/api/v1/market-calendar/KR"),
        ("GET", "/api/v1/market-calendar/US"),
    ]
    assert report.calendar_started_count == 2
    assert report.provider_started_count == 1  # TOKEN charged the 3744 ceiling once
    assert report.wire_attempt_count == 3  # durable TOKEN + KR + US starts
    assert report.per_operation.get("ACCOUNT_HOLDINGS", 0) == 0
    assert report.per_operation.get("PRICE_BATCH", 0) == 0
    assert report.per_operation.get("ORDERS_PAGE", 0) == 0
    assert real_socket == []

    # Truthful labeling: the JSONL carries wire_attempt_count and never a
    # fabricated socket/dns measurement.
    assert emitted, "expected per-tick metrics"
    for record in emitted:
        assert "wire_attempt_count" in record
        assert "socket_count" not in record and "dns_count" not in record
    assert not hasattr(report, "socket_count") and not hasattr(report, "dns_count")

    # Durable evidence: exactly three started rows, all settled, TOKEN under the
    # fixed pre-cadence tick id 0 that no cadence tick can reach.
    from tokmo.programs.g2a_shadow_runner import shadow_root

    root = shadow_root(tmp_path, day)
    connection = sqlite3.connect(root / "ops" / "ops_kernel.sqlite3")
    try:
        started = connection.execute(
            "SELECT operation, state, tick_id FROM planned_requests "
            "WHERE started_at IS NOT NULL ORDER BY operation"
        ).fetchall()
    finally:
        connection.close()
    assert started == [
        ("CALENDAR_DAY_KR", "SETTLED", tick_id_from_utc(start).tick_id),
        ("CALENDAR_DAY_US", "SETTLED", tick_id_from_utc(start).tick_id),
        ("TOKEN", "SETTLED", 0),
    ]


def test_expired_campaign_token_stops_without_refresh(tmp_path, monkeypatch):
    """A bearer that has expired mid-hour STOPs the next read; it is never
    refreshed (no second auth POST)."""
    from tokmo.composition import build_g2a_live_deps
    from tokmo.programs.g2a_shadow_runner import G2AStopError
    from tokmo.programs.integrated_fake_cadence import RequestGateV1
    from tokmo.records.ops_kernel import OpsKernelStore

    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    day = date(2026, 7, 31)
    now = {"t": datetime(2026, 7, 31, 7, 0, tzinfo=UTC)}

    def clock():
        return now["t"]

    wire: list = []
    factory = _handler_factory(
        (
            _JsonResponse(_token_body(expires_in=1)),
            _JsonResponse(_closed_calendar_body(day)),
        ),
        wire,
    )
    deps = build_g2a_live_deps(
        project_root=tmp_path, clock=clock, https_handler_factory=factory
    )
    kernel = OpsKernelStore(tmp_path / "ops_kernel.sqlite3")

    deps.calendar_provider(
        market=Market.KR,
        local_trade_date=day,
        tick=_Tick(),
        request_gate=RequestGateV1(kernel),
    )
    # Advance past the real expiry; the next read must STOP, never refresh.
    now["t"] = now["t"] + timedelta(seconds=5)
    with pytest.raises(G2AStopError, match="token_expired"):
        deps.calendar_provider(
            market=Market.US,
            local_trade_date=day,
            tick=_Tick(),
            request_gate=RequestGateV1(kernel),
        )
    assert [entry for entry in wire if entry[0] == "POST"] == [
        ("POST", "/oauth2/token")
    ]


def test_restart_without_memory_token_stops_rather_than_recalling(
    tmp_path, monkeypatch
):
    """A fresh deps closure (a restarted process) over a store that already
    holds a completed TOKEN row STOPs rather than re-acquiring: the bearer is
    memory-only and disk persistence is forbidden."""
    from tokmo.composition import build_g2a_live_deps
    from tokmo.programs.g2a_shadow_runner import G2AStopError
    from tokmo.programs.integrated_fake_cadence import RequestGateV1
    from tokmo.records.ops_kernel import OpsKernelStore

    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    day = date(2026, 7, 31)
    kernel_path = tmp_path / "ops_kernel.sqlite3"

    # Process 1: acquire and settle the campaign bearer (durable TOKEN row).
    wire1: list = []
    deps1 = build_g2a_live_deps(
        project_root=tmp_path,
        clock=_clock,
        https_handler_factory=_handler_factory(
            (_JsonResponse(_token_body()), _JsonResponse(_closed_calendar_body(day))),
            wire1,
        ),
    )
    deps1.calendar_provider(
        market=Market.KR,
        local_trade_date=day,
        tick=_Tick(),
        request_gate=RequestGateV1(OpsKernelStore(kernel_path)),
    )
    assert ("POST", "/oauth2/token") in wire1

    # Process 2: a fresh closure with empty in-memory bearer over the same store.
    wire2: list = []
    deps2 = build_g2a_live_deps(
        project_root=tmp_path,
        clock=_clock,
        https_handler_factory=_handler_factory((_JsonResponse(_token_body()),), wire2),
    )
    with pytest.raises(G2AStopError, match="token_unrecoverable"):
        deps2.calendar_provider(
            market=Market.US,
            local_trade_date=day,
            tick=_Tick(),
            request_gate=RequestGateV1(OpsKernelStore(kernel_path)),
        )
    assert wire2 == []  # no re-acquisition attempted
