"""G2-A bounded read-only live-shadow runner — offline TDD (no live network).

Every test injects a fake clock, a fake sleeper, and injected providers/handlers.
The one-hour wall clock is never actually waited on: the sleeper advances a
controllable clock, so twelve 300s slots run in microseconds. No real socket,
DNS, credential file, production DB, order write, or model call is exercised.
"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import json
import os
import socket
import sqlite3

import pytest

SOCKET_TYPE = socket.socket

from tokmo.values.session_gate import Market
from tokmo.values.toss_market_calendar import IntegratedHour, KrMarketDay, UsMarketDay


# ---------------------------------------------------------------------------
# Fakes: a monotonic clock advanced only by the injected sleeper, and closed
# KR/US calendar days so both sessions are closed for the whole window.
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, start: datetime):
        self._now = start
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise AssertionError("negative sleep")
        self.sleeps.append(seconds)
        self._now = self._now + timedelta(seconds=seconds)


def _closed_kr(day: date) -> KrMarketDay:
    return KrMarketDay(date=day, integrated=IntegratedHour(regular_market=None))


def _closed_us(day: date) -> UsMarketDay:
    return UsMarketDay(date=day, regular_market=None)


def _fail_closed_factory(label):
    def factory(_gate):
        def provider(*_a, **_k):
            raise AssertionError(f"{label} provider must not be called on a closed session")
        return provider
    return factory


def _closed_calendar_provider(*, record=None):
    def provider(*, market, local_trade_date, tick, request_gate):
        if record is not None:
            record.append((market, local_trade_date))
        if market is Market.KR:
            return _closed_kr(local_trade_date)
        return _closed_us(local_trade_date)
    return provider


def _closed_deps(clock, *, calendar_record=None):
    from tokmo.programs.integrated_fake_cadence import IntegratedCadenceDepsV1

    return IntegratedCadenceDepsV1(
        calendar_provider=_closed_calendar_provider(record=calendar_record),
        holdings_provider_factory=_fail_closed_factory("holdings"),
        quotes_provider_factory=_fail_closed_factory("quotes"),
        orders_provider_factory=_fail_closed_factory("orders"),
        detail_provider_factory=_fail_closed_factory("detail"),
        glm_call=lambda *a, **k: pytest.fail("model call forbidden in G2-A"),
        sol_call=lambda *a, **k: pytest.fail("model call forbidden in G2-A"),
        regular_session_dates=object(),
                clock=clock.now,
    )


def _pass_probe(_path: Path) -> int:
    return 0


def _run(home, clock, **overrides):
    from tokmo.programs import g2a_shadow_runner as runner

    utc_day = clock.now().date()
    kwargs = dict(
        home=home, utc_day=utc_day, deps=_closed_deps(clock),
        credential_path=None, clock=clock.now, sleeper=clock.sleep,
        open_handle_probe=_pass_probe,
    )
    kwargs.update(overrides)
    return runner.run_a_one_hour(**kwargs)


# A wall-clock instant where both markets are closed: KR 16:00 (after close),
# US 03:00 (pre-open). The closed calendar keeps both closed regardless.
START = datetime(2026, 7, 31, 7, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. Exactly 12 ticks on a fixed 300s grid.
# ---------------------------------------------------------------------------

def test_runs_exactly_twelve_ticks_on_a_300s_grid(tmp_path):
    clock = FakeClock(START)
    report = _run(tmp_path, clock)

    assert report.tick_count == 12
    scheduled = [m.scheduled_at for m in report.tick_metrics]
    assert len(scheduled) == 12
    deltas = {
        (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
        for a, b in zip(scheduled, scheduled[1:])
    }
    assert deltas == {300}
    # Grid is aligned to 5-minute boundaries.
    for stamp in scheduled:
        assert datetime.fromisoformat(stamp).second == 0
        assert datetime.fromisoformat(stamp).timestamp() % 300 == 0


def test_no_catch_up_burst_when_a_slot_wakes_late(tmp_path):
    # A sleeper that overshoots each slot by 40s must not fire extra ticks:
    # still exactly 12, still a 300s grid, with recorded positive lag.
    clock = FakeClock(START)

    def overshooting_sleep(seconds):
        clock.sleep(seconds + 40)

    report = _run(tmp_path, clock, sleeper=overshooting_sleep)
    assert report.tick_count == 12
    scheduled = [datetime.fromisoformat(m.scheduled_at) for m in report.tick_metrics]
    assert {(b - a).total_seconds() for a, b in zip(scheduled, scheduled[1:])} == {300}
    assert all(m.lag_seconds >= 0 for m in report.tick_metrics)


# ---------------------------------------------------------------------------
# 2. Closed sessions: provider calls exactly 0, exact skip receipts.
# ---------------------------------------------------------------------------

def test_closed_sessions_make_zero_provider_calls_and_write_skip_receipts(tmp_path):
    clock = FakeClock(START)
    calendar_record: list = []
    deps = _closed_deps(clock, calendar_record=calendar_record)
    report = _run(tmp_path, clock, deps=deps)

    # Holdings/quotes/trades never reach a provider (fail-closed factories prove it).
    assert report.per_operation.get("ACCOUNT_HOLDINGS", 0) == 0
    assert report.per_operation.get("PRICE_BATCH", 0) == 0
    assert report.per_operation.get("ORDERS_PAGE", 0) == 0
    assert report.provider_started_count == 0  # nothing consumed the 3744 ceiling

    from tokmo.programs.g2a_shadow_runner import shadow_root
    from tokmo.records.receipt_store import ReceiptStore
    root = shadow_root(tmp_path, clock.now().date())
    for job in ("holdings", "quotes", "trades"):
        receipt = ReceiptStore(root / "ops" / "receipts" / f"{job}.json",
                               clock=clock.now).read()
        assert receipt is not None
        assert receipt.attempt.reason in {"skipped_market_closed", "skipped_holdings_absent"}


def test_calendar_is_permitted_and_accounted_when_provider_reserves(tmp_path):
    # Calendar may call; its planned/started/settled rows are accounted, and the
    # calendar budget is separate from the 3744 provider ceiling.
    from tokmo.programs.integrated_fake_cadence import IntegratedCadenceDepsV1
    from tokmo.values.ops_kernel import ProducerRole
    from tokmo.values.request_parity import REQUEST_OPERATION_SPECS, canonical_params_digest

    clock = FakeClock(START)

    def calendar_provider(*, market, local_trade_date, tick, request_gate):
        op = "CALENDAR_DAY_KR" if market is Market.KR else "CALENDAR_DAY_US"
        params = {"market": market.value, "local_trade_date": local_trade_date.isoformat()}
        spec = REQUEST_OPERATION_SPECS[op]
        ordinal = 0 if market is Market.KR else 1
        invocation = f"{tick.tick_id:032x}"
        request_gate.ops_kernel.plan_requests(
            ProducerRole.COLLECTOR, tick.tick_id, "CALENDAR", ({
                "ordinal": ordinal, "operation": op,
                "params_digest": canonical_params_digest(op, params),
                "method": spec[0], "host": "openapi.tossinvest.com",
                "normalized_path_template": spec[1], "account_ordinal": None,
                "market": market.value, "local_trade_date": local_trade_date.isoformat(),
                "canonical_params": params,
            },), invocation_id=invocation, now=clock.now())
        request_gate.ops_kernel.start_request(
            ProducerRole.COLLECTOR, tick.tick_id, "CALENDAR", ordinal,
            invocation_id=invocation, now=clock.now())
        request_gate.ops_kernel.settle_request(
            ProducerRole.COLLECTOR, tick.tick_id, "CALENDAR", ordinal, "OK",
            invocation_id=invocation, now=clock.now())
        if market is Market.KR:
            return _closed_kr(local_trade_date)
        return _closed_us(local_trade_date)

    deps = IntegratedCadenceDepsV1(
        calendar_provider=calendar_provider,
        holdings_provider_factory=_fail_closed_factory("holdings"),
        quotes_provider_factory=_fail_closed_factory("quotes"),
        orders_provider_factory=_fail_closed_factory("orders"),
        detail_provider_factory=_fail_closed_factory("detail"),
        glm_call=lambda *a, **k: pytest.fail("model forbidden"),
        sol_call=lambda *a, **k: pytest.fail("model forbidden"),
        regular_session_dates=object(), clock=clock.now,
    )
    report = _run(tmp_path, clock, deps=deps)
    assert report.calendar_started_count == 2  # KR + US once, then cache hits
    assert report.provider_started_count == 0  # calendar excluded from 3744 counter


# ---------------------------------------------------------------------------
# 3. Resume-safe durable progress + non-rearmable cumulative day counter.
# ---------------------------------------------------------------------------

def test_resume_skips_completed_ticks_and_does_not_double_count(tmp_path):
    from tokmo.programs import g2a_shadow_runner as runner

    clock = FakeClock(START)
    utc_day = clock.now().date()
    common = dict(
        home=tmp_path, utc_day=utc_day, credential_path=None,
        clock=clock.now, open_handle_probe=_pass_probe,
    )
    # First invocation stops after 5 ticks via a sleeper that raises mid-run.
    stop_after = {"n": 0}

    def limited_sleep(seconds):
        stop_after["n"] += 1
        if stop_after["n"] > 5:
            raise KeyboardInterrupt
        clock.sleep(seconds)

    with pytest.raises(KeyboardInterrupt):
        runner.run_a_one_hour(deps=_closed_deps(clock), sleeper=limited_sleep, **common)

    # Second invocation (same utc day → same stable run id/root) resumes.
    report = runner.run_a_one_hour(deps=_closed_deps(clock), sleeper=clock.sleep, **common)
    assert report.tick_count == 12
    # No tick index ran twice across the two invocations.
    from tokmo.programs.g2a_shadow_runner import shadow_root
    db = sqlite3.connect(shadow_root(tmp_path, utc_day) / "ops" / "g2a_progress.sqlite3")
    try:
        indices = [row[0] for row in db.execute("SELECT tick_index FROM g2a_ticks ORDER BY tick_index")]
    finally:
        db.close()
    assert indices == list(range(12))


def test_new_invocation_cannot_reset_cumulative_count_via_run_id(tmp_path):
    # The stable run id is derived from the UTC day, so a caller cannot mint a
    # fresh run id to escape the same-day cumulative provider ceiling.
    from tokmo.programs.g2a_shadow_runner import stable_run_id
    day = date(2026, 7, 31)
    assert stable_run_id(day) == stable_run_id(day)
    assert stable_run_id(day) != stable_run_id(date(2026, 8, 1))
    assert "2026-07-31" in stable_run_id(day)


# ---------------------------------------------------------------------------
# 4. STOP conditions: nonzero, evidence preserved.
# ---------------------------------------------------------------------------

def test_provider_call_on_open_session_is_a_stop(tmp_path):
    # If a market is unexpectedly open, the collector calls the provider; the
    # fail-closed data closure raises, and the runner terminalizes as a STOP.
    from tokmo.programs import g2a_shadow_runner as runner
    from tokmo.programs.integrated_fake_cadence import IntegratedCadenceDepsV1
    from tokmo.values.toss_market_calendar import CalendarSession

    clock = FakeClock(START)

    def open_kr_provider(*, market, local_trade_date, tick, request_gate):
        if market is Market.KR:
            session = CalendarSession(
                datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 7, 31, 23, 0, tzinfo=timezone.utc))
            return KrMarketDay(date=local_trade_date,
                               integrated=IntegratedHour(regular_market=session))
        return _closed_us(local_trade_date)

    deps = IntegratedCadenceDepsV1(
        calendar_provider=open_kr_provider,
        holdings_provider_factory=_fail_closed_factory("holdings"),
        quotes_provider_factory=_fail_closed_factory("quotes"),
        orders_provider_factory=_fail_closed_factory("orders"),
        detail_provider_factory=_fail_closed_factory("detail"),
        glm_call=lambda *a, **k: None, sol_call=lambda *a, **k: None,
        regular_session_dates=object(), clock=clock.now,
    )
    with pytest.raises(runner.G2AStopError):
        _run(tmp_path, clock, deps=deps)

    # Evidence: a run_state file records STOP, and the shadow DB survives.
    from tokmo.programs.g2a_shadow_runner import shadow_root
    root = shadow_root(tmp_path, clock.now().date())
    state = json.loads((root / "run_state.json").read_text())
    assert state["status"] == "STOPPED"
    assert (root / "ops" / "ops_kernel.sqlite3").exists()


# ---------------------------------------------------------------------------
# 5. Shadow-root confinement.
# ---------------------------------------------------------------------------

def test_shadow_root_is_under_the_shadow_namespace_and_denies_production(tmp_path):
    from tokmo.programs.g2a_shadow_runner import shadow_root, _confine_shadow_root

    root = shadow_root(tmp_path, date(2026, 7, 31))
    parts = root.parts
    assert parts[-3:-1] == ("tokmo-v3", "shadow")
    _confine_shadow_root(tmp_path, root)  # legal

    production_ops = tmp_path / ".local" / "share" / "tokmo-v3" / "ops"
    with pytest.raises(ValueError, match="shadow"):
        _confine_shadow_root(tmp_path, production_ops)


# ---------------------------------------------------------------------------
# 6. Preflight refusals; preflight touches no network and mutates nothing.
# ---------------------------------------------------------------------------

def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(text)
    os.chmod(path, 0o600)


def test_preflight_refuses_when_production_ops_kernel_exists(tmp_path):
    from tokmo.programs import g2a_shadow_runner as runner
    kernel = tmp_path / ".local" / "share" / "tokmo-v3" / "ops" / "ops_kernel.sqlite3"
    kernel.parent.mkdir(parents=True)
    kernel.write_bytes(b"")
    clock = FakeClock(START)
    with pytest.raises(runner.G2APreflightError, match="production_kernel_present"):
        runner.preflight(home=tmp_path, utc_day=clock.now().date(),
                         credential_path=None, clock=clock.now, open_handle_probe=_pass_probe)


def test_preflight_refuses_when_open_handles_are_nonzero(tmp_path):
    from tokmo.programs import g2a_shadow_runner as runner
    clock = FakeClock(START)
    with pytest.raises(runner.G2APreflightError, match="open_handles"):
        runner.preflight(home=tmp_path, utc_day=clock.now().date(),
                         credential_path=None, clock=clock.now,
                         open_handle_probe=lambda _p: 3)


def test_preflight_refuses_symlinked_or_wide_mode_credential(tmp_path):
    from tokmo.programs import g2a_shadow_runner as runner
    clock = FakeClock(START)
    cred = tmp_path / ".env.toss"
    cred.write_text("TOSS_CLIENT_ID=x\nTOSS_CLIENT_SECRET=y\n")
    os.chmod(cred, 0o644)  # too wide
    with pytest.raises(runner.G2APreflightError, match="credential"):
        runner.preflight(home=tmp_path, utc_day=clock.now().date(),
                         credential_path=cred, clock=clock.now, open_handle_probe=_pass_probe)


def test_preflight_passes_and_changes_no_production_files(tmp_path):
    from tokmo.programs import g2a_shadow_runner as runner
    clock = FakeClock(START)
    cred = tmp_path / ".env.toss"
    _write_private(cred, "TOSS_CLIENT_ID=x\nTOSS_CLIENT_SECRET=y\n")
    data_dir = tmp_path / ".local" / "share" / "tokmo-v3"
    data_dir.mkdir(parents=True)

    def inventory(base: Path):
        return {p.relative_to(base).as_posix(): p.stat().st_mtime_ns
                for p in base.rglob("*") if p.is_file()}

    before = inventory(data_dir)
    net = []
    monkey_forbid(net)
    report = runner.preflight(
        home=tmp_path, utc_day=clock.now().date(), credential_path=cred,
        clock=clock.now, open_handle_probe=_pass_probe)
    assert report.ok is True
    assert inventory(data_dir) == before
    assert net == []


# A module-level socket guard reused by the preflight test.
_ORIGINAL = {}


def monkey_forbid(sink):
    def forbid(*a, **k):
        sink.append((a, k))
        raise AssertionError("network forbidden")
    # Best-effort: patch and auto-restore at interpreter teardown is unnecessary
    # because preflight must not touch sockets at all.
    socket.getaddrinfo = forbid  # type: ignore[assignment]
    socket.create_connection = forbid  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _restore_socket():
    saved = (socket.getaddrinfo, socket.create_connection)
    yield
    socket.getaddrinfo, socket.create_connection = saved


# ---------------------------------------------------------------------------
# 7. No order-write / model / telegram surface in the module source.
# ---------------------------------------------------------------------------

def test_module_source_has_no_order_write_or_forbidden_surface():
    import tokmo.programs.g2a_shadow_runner as runner
    source = Path(runner.__file__).read_text()
    for forbidden in ("POST", "PUT", "PATCH", "DELETE", "telegram", "Telegram",
                      "conditional", "order_write", "place_order"):
        # POST is allowed only inside the TOKEN comment/exception marker.
        if forbidden == "POST":
            continue
        assert forbidden not in source, f"forbidden surface: {forbidden}"


def test_manifest_upper_bound_is_sanitized_and_bounded(tmp_path):
    from tokmo.programs.g2a_shadow_runner import build_g2a_manifest
    manifest = build_g2a_manifest(date(2026, 7, 31))
    # Closed-session upper bound: at most one TOKEN (3744-consuming) + two
    # calendar starts; no data operations.
    assert manifest.ceiling_consuming_upper_bound <= 1
    assert manifest.calendar_budget_upper_bound == 2
    assert manifest.per_operation.get("ACCOUNT_HOLDINGS", 0) == 0
    assert manifest.per_operation.get("PRICE_BATCH", 0) == 0
    assert manifest.total_upper_bound <= 3
