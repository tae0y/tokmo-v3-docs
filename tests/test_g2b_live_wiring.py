"""G2-B live-deps wiring — offline via scripted HTTPS handlers (no socket).

Proves the composition-owned real G2-B provider factories: the single new B
TOKEN identity (distinct from G2-A's terminal row) charging the shared day
counter forward, the holdings provider reserving ACCOUNTS then per-account
ACCOUNT_HOLDINGS, the hard canary ceilings STOPping before the N+1 transport,
and no retry/redirect.  No real socket, DNS, or credential outside the fixture
is exercised.
"""

import io
import json
import os
import socket
import sqlite3
import urllib.parse
import urllib.request
import urllib.response
from datetime import UTC, date, datetime
from email.message import Message

import pytest

from tokmo.composition import (
    _g2a_plan_start_token,
    _g2a_settle_token,
    _G2BCanaryLedger,
    build_g2b_live_deps,
)
from tokmo.programs.integrated_fake_cadence import RequestGateV1
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import OpsKernelBudgetExceeded
from tokmo.values.ops_tick import tick_id_from_utc
from tokmo.values.session_gate import Market


def _JsonResponse(body: bytes):
    headers = Message()
    response = urllib.response.addinfourl(
        io.BytesIO(body), headers, "https://openapi.tossinvest.com/x", code=200
    )
    response.msg = "OK"
    return response


def _handler_factory(responses, wire):
    queue = iter(responses)

    class _Scripted(urllib.request.HTTPSHandler):
        def https_open(self, request):
            parts = urllib.parse.urlsplit(request.full_url)
            wire.append((request.get_method(), parts.path))
            return next(queue)

    return _Scripted


def _redirect(url: str):
    headers = Message()
    headers["Location"] = "https://openapi.tossinvest.com/elsewhere"
    response = urllib.response.addinfourl(io.BytesIO(b""), headers, url, code=302)
    response.msg = "Found"
    return response


def _token_body(expires_in: int = 3600) -> bytes:
    return json.dumps(
        {
            "access_token": "a" * 40,
            "expires_in": expires_in,
            "token_type": "Bearer",
        }
    ).encode()


def _closed_calendar_body(day: date) -> bytes:
    return json.dumps(
        {
            "result": {
                "today": {"date": day.isoformat()},
                "previousBusinessDay": {"date": "2026-07-30"},
                "nextBusinessDay": {"date": "2026-08-03"},
            }
        }
    ).encode()


def _accounts_body(*seqs: str) -> bytes:
    return json.dumps({"result": [{"accountSeq": seq} for seq in seqs]}).encode()


def _holdings_body() -> bytes:
    return json.dumps(
        {
            "result": {
                "items": [
                    {
                        "symbol": "AAPL",
                        "quantity": "10",
                        "averagePrice": "150",
                        "market": "US",
                        "currency": "USD",
                    },
                ]
            }
        }
    ).encode()


def _price_body() -> bytes:
    return json.dumps({"result": []}).encode()


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


def _counter(kernel_path, day: date) -> int:
    connection = sqlite3.connect(kernel_path)
    try:
        row = connection.execute(
            "SELECT provider_started_count FROM request_day_counters WHERE utc_day=?",
            (day.isoformat(),),
        ).fetchone()
    finally:
        connection.close()
    return 0 if row is None else row[0]


def _rows(kernel_path):
    connection = sqlite3.connect(kernel_path)
    try:
        return connection.execute(
            "SELECT tick_id, job_scope, ordinal, operation, state FROM planned_requests "
            "WHERE started_at IS NOT NULL ORDER BY job_scope, tick_id, ordinal"
        ).fetchall()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 1. B token is a new identity; the shared day counter advances 1 -> 2.
# ---------------------------------------------------------------------------


def test_b_token_is_a_new_identity_and_day_counter_goes_one_to_two(
    tmp_path, monkeypatch
):
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    day = date(2026, 7, 31)
    now = _clock()
    kernel_path = tmp_path / "ops_kernel.sqlite3"

    # Seed G2-A's terminal TOKEN row under CALENDAR: the day counter reads 1.
    seed = OpsKernelStore(kernel_path)
    _g2a_plan_start_token(seed, now)
    _g2a_settle_token(seed, "OK", now)
    assert _counter(kernel_path, day) == 1

    wire: list = []
    factory = _handler_factory(
        (_JsonResponse(_token_body()), _JsonResponse(_closed_calendar_body(day))), wire
    )
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=_clock, https_handler_factory=factory
    )
    deps.calendar_provider(
        market=Market.US,
        local_trade_date=day,
        tick=_Tick(),
        request_gate=RequestGateV1(OpsKernelStore(kernel_path)),
    )

    assert wire == [("POST", "/oauth2/token"), ("GET", "/api/v1/market-calendar/US")]
    # The B TOKEN start advanced the shared non-calendar counter exactly once.
    assert _counter(kernel_path, day) == 2

    started = _rows(kernel_path)
    tokens = [row for row in started if row[3] == "TOKEN"]
    # A's terminal CALENDAR-scoped token is intact; B added a HOLDINGS-scoped one.
    assert (0, "CALENDAR", 0, "TOKEN", "SETTLED") in tokens
    assert (0, "HOLDINGS", 0, "TOKEN", "SETTLED") in tokens
    assert len(tokens) == 2


# ---------------------------------------------------------------------------
# 2. Holdings provider reserves ACCOUNTS then per-account ACCOUNT_HOLDINGS.
# ---------------------------------------------------------------------------


def test_holdings_provider_reserves_accounts_then_per_account_holdings(
    tmp_path, monkeypatch
):
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    day = date(2026, 7, 31)
    kernel_path = tmp_path / "ops_kernel.sqlite3"
    wire: list = []
    factory = _handler_factory(
        (
            _JsonResponse(_token_body()),
            _JsonResponse(_accounts_body("1001")),
            _JsonResponse(_holdings_body()),
        ),
        wire,
    )
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=_clock, https_handler_factory=factory
    )
    provider = deps.holdings_provider_factory(
        RequestGateV1(OpsKernelStore(kernel_path))
    )
    outcome = provider()

    assert outcome.code == "success"
    assert outcome.account_count == 1
    assert len(outcome.facts) == 1
    assert wire == [
        ("POST", "/oauth2/token"),
        ("GET", "/api/v1/accounts"),
        ("GET", "/api/v1/holdings"),
    ]
    tick_id = tick_id_from_utc(_clock()).tick_id
    started = _rows(kernel_path)
    assert (0, "HOLDINGS", 0, "TOKEN", "SETTLED") in started
    assert (tick_id, "HOLDINGS", 0, "ACCOUNTS", "SETTLED") in started
    # ACCOUNT_HOLDINGS lives at ordinal account_ordinal+1 = 2 for the first account.
    assert (tick_id, "HOLDINGS", 2, "ACCOUNT_HOLDINGS", "SETTLED") in started
    # TOKEN + ACCOUNTS + one ACCOUNT_HOLDINGS all charge the shared day counter.
    assert _counter(kernel_path, day) == 3


# ---------------------------------------------------------------------------
# 3. The quote-batch ceiling STOPs before the sixth transport.
# ---------------------------------------------------------------------------


def test_quote_batch_ceiling_stops_before_the_sixth_transport(tmp_path, monkeypatch):
    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    kernel_path = tmp_path / "ops_kernel.sqlite3"
    wire: list = []
    factory = _handler_factory(
        (_JsonResponse(_token_body()),)
        + tuple(_JsonResponse(_price_body()) for _ in range(5)),
        wire,
    )
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=_clock, https_handler_factory=factory
    )
    provider = deps.quotes_provider_factory(RequestGateV1(OpsKernelStore(kernel_path)))

    for index in range(5):
        provider((f"SYM{index}",))
    with pytest.raises(OpsKernelBudgetExceeded, match="g2b_ceiling_quotes"):
        provider(("SYM5",))

    price_calls = [entry for entry in wire if entry[1] == "/api/v1/prices"]
    assert len(price_calls) == 5  # the sixth batch made no transport call
    assert ("POST", "/oauth2/token") in wire


# ---------------------------------------------------------------------------
# 4. The in-memory ledger enforces every hard ceiling.
# ---------------------------------------------------------------------------


def test_ledger_enforces_all_ceilings():
    accounts = _G2BCanaryLedger()
    for _ in range(5):
        accounts.admit_account_holdings()
    with pytest.raises(OpsKernelBudgetExceeded, match="accounts"):
        accounts.admit_account_holdings()

    quotes = _G2BCanaryLedger()
    for _ in range(5):
        quotes.admit_quote_batch()
    with pytest.raises(OpsKernelBudgetExceeded, match="quotes"):
        quotes.admit_quote_batch()

    trades = _G2BCanaryLedger()
    for _ in range(12):
        trades.admit_trade()
    with pytest.raises(OpsKernelBudgetExceeded, match="trades"):
        trades.admit_trade()

    # The campaign total is 286 = up to ten TOKEN attempts + twelve ticks of
    # 23 data starts (each tick run once across the campaign).
    total = _G2BCanaryLedger()
    for _ in range(286):
        total.admit_token()
    with pytest.raises(OpsKernelBudgetExceeded, match="total"):
        total.admit_token()


def test_ledger_per_tick_ceilings_reset_at_tick_boundary():
    # Per-tick sub-ceilings reset on a new tick; the campaign total does not.
    ledger = _G2BCanaryLedger()
    ledger.begin_tick(1000)
    for _ in range(5):
        ledger.admit_quote_batch()
    with pytest.raises(OpsKernelBudgetExceeded, match="quotes"):
        ledger.admit_quote_batch()

    ledger.begin_tick(1001)  # fresh tick -> the 5-quota is available again
    for _ in range(5):
        ledger.admit_quote_batch()
    assert ledger.total == 10  # campaign total accumulated across both ticks


# ---------------------------------------------------------------------------
# 5. No retry / redirect: a calendar redirect STOPs, its start not refunded.
# ---------------------------------------------------------------------------


def test_calendar_redirect_is_a_stop_without_retry(tmp_path, monkeypatch):
    from tokmo.programs.g2a_shadow_runner import G2AStopError

    _credential(tmp_path)
    _forbid_socket(monkeypatch, [])
    day = date(2026, 7, 31)
    kernel_path = tmp_path / "ops_kernel.sqlite3"
    wire: list = []
    factory = _handler_factory(
        (
            _JsonResponse(_token_body()),
            _redirect("https://openapi.tossinvest.com/api/v1/market-calendar/US"),
        ),
        wire,
    )
    deps = build_g2b_live_deps(
        project_root=tmp_path, clock=_clock, https_handler_factory=factory
    )
    with pytest.raises(G2AStopError, match="redirect"):
        deps.calendar_provider(
            market=Market.US,
            local_trade_date=day,
            tick=_Tick(),
            request_gate=RequestGateV1(OpsKernelStore(kernel_path)),
        )
    # Exactly one calendar transport attempt, no follow.
    assert wire == [("POST", "/oauth2/token"), ("GET", "/api/v1/market-calendar/US")]
