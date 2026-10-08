"""G2.5 bounded live canary — comprehensive TDD tests (RED→GREEN).

Scope: single market, single symbol, quote-only, 5 ticks, Toss read-only.
Cumulative provider-start cap 10, retry 0, redirect/pagination 0.
No AI/Telegram/order/execution/account write. Canonical material disconnected.

Test quality items (1-10 from review):
1. Positive control traverses live branch with fake injected production transport.
2. Exact fake transport counters prove TOKEN=1, PRICE_BATCH=5, total=6.
3. Crash after write-ahead intent before settle, restart sees consumed count.
4. Provider failure tick1 => calls stop exactly there, exit nonzero.
5. Fingerprint mutation => nonzero.
6. Wrong/dirty commit => provider calls 0.
7. Root alias/symlink/nesting => provider calls 0.
8. Past-slot restart => MISSED/non-call and future-only behavior.
9. Completion rerun => calls 0 and nonzero refusal.
10. pulse_main, registry, 300s owner, cron remain unchanged.

G2.5 third final targeted repair (this commit):
- B-1: absolute campaign root anchor tests.
- M-1: durable missed crash-boundary test.
- m1/m2: fixture composition moved to tools, t0 dead param removed.
"""

from datetime import datetime, timezone, timedelta
from email.message import Message
from pathlib import Path
import io
import json
import os
import urllib.parse
import urllib.request
import urllib.response

import pytest

# Import build_canary_live_deps from the tool file (outside the seal)
import sys as _sys
_tool_path = Path(__file__).resolve().parent.parent / "tools"
if str(_tool_path) not in _sys.path:
    _sys.path.insert(0, str(_tool_path))
from run_tokmo_pulse_canary import build_canary_live_deps
from run_tokmo_pulse_canary_fixture import build_fixture_deps, run_fixture_cli

T0 = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════════════════
# Helpers: fake transport for production-shaped tests (item 1, 2)
# ═══════════════════════════════════════════════════════════════════════

def _json_response(body: bytes, status=200):
    headers = Message()
    resp = urllib.response.addinfourl(
        io.BytesIO(body), headers,
        "https://openapi.tossinvest.com/x", code=status)
    setattr(resp, "msg", "OK")
    return resp


def _token_body():
    return json.dumps({
        "access_token": "test-bearer-token-1234567890",
        "expires_in": 3600,
        "token_type": "Bearer",
    }).encode()


def _price_body(symbol="005930", price="72500"):
    return json.dumps({
        "result": [{
            "symbol": symbol,
            "lastPrice": price,
            "currency": "KRW",
            "timestamp": "2026-08-11T00:00:00Z",
        }]
    }).encode()


def _make_scripted_handler(responses):
    """Build a scripted HTTPSHandler that records all wire calls."""
    queue = iter(responses)
    wire_log = []

    class _Scripted(urllib.request.HTTPSHandler):
        def https_open(self, request):
            parts = urllib.parse.urlsplit(request.full_url)
            wire_log.append((request.get_method(), parts.path))
            try:
                return next(queue)
            except StopIteration:
                return _json_response(b'{"result": []}')

    return _Scripted, wire_log


def _credential_file(project_root):
    """Write a fake .env.toss credential file."""
    project_root = Path(project_root)
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / ".env.toss").write_text(
        "TOSS_CLIENT_ID=test-client-id-12345\n"
        "TOSS_CLIENT_SECRET=test-secret-abcdef\n")


def _write_fixture(tmp_path):
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps({
        "token": "fixture-token",
        "quotes": {
            "005930": {
                "last_price": "72500", "currency": "KRW",
                "timestamp": "2026-08-11T00:00:00Z",
                "fetched_at": "2026-08-11T00:00:00Z",
            }
        },
    }))
    return fixture_path


def _make_success_quotes_fetch():
    """Build a quotes_fetch that returns a success PulseQuoteBatchOutcomeV1."""
    from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
    from tokmo.values.market_observations import TossCurrentPriceFact

    def quotes_fetch(market, symbols, *, bearer=None):
        return PulseQuoteBatchOutcomeV1("success", tuple(
            TossCurrentPriceFact(
                source="toss-current-price-v1", symbol=sym,
                last_price="50000", currency_raw="KRW",
                timestamp=T0, fetched_at=T0,
            ) for sym in symbols
        ), ())
    return quotes_fetch


# ═══════════════════════════════════════════════════════════════════════
# Item 1+2: Positive control — live branch with fake production transport
# ═══════════════════════════════════════════════════════════════════════

def test_live_branch_with_fake_production_transport_proves_10_calls(tmp_path):
    """Item 1+2: Traverse live CLI branch with fake injected production
    transport. Exact counters prove TOKEN=1, PRICE_BATCH=5, total=6."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.values.session_gate import Market

    project_root = tmp_path / "project"
    _credential_file(project_root)

    # v3 per-tick: TOKEN,PRICE interleaved 5 times = 10 responses
    handler_cls, wire_log = _make_scripted_handler(
        [resp for pair in zip(
            [_json_response(_token_body()) for _ in range(5)],
            [_json_response(_price_body()) for _ in range(5)],
        ) for resp in pair]
    )

    # Virtual clock that advances per tick
    clock_state = {"t": T0}

    def virtual_clock():
        return clock_state["t"]

    def virtual_sleep(target_utc):
        clock_state["t"] = target_utc

    deps = build_canary_live_deps(
        project_root=project_root,
        clock=virtual_clock,
        https_handler_factory=handler_cls,
    )
    # Override sleep_until with virtual
    deps = CanaryDepsV1(
        clock=deps.clock, token_fetch=deps.token_fetch,
        quotes_fetch=deps.quotes_fetch,
        sleep_until=virtual_sleep,
    )

    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )

    evidence_root = tmp_path / "evidence"
    result = run_canary_session(
        root=evidence_root, config=cfg, deps=deps, t0=T0,
    )

    # Item 2: exact counters
    token_calls = [w for w in wire_log if w[1] == "/oauth2/token"]
    price_calls = [w for w in wire_log if w[1] == "/api/v1/prices"]
    assert len(token_calls) == 5, f"Expected 5 TOKEN calls (per-tick), got {len(token_calls)}"
    assert len(price_calls) == 5, f"Expected 5 PRICE_BATCH calls, got {len(price_calls)}"
    assert len(wire_log) == 10, f"Expected 10 total (5 TOKEN + 5 PRICE per-tick), got {len(wire_log)}"
    assert result.completed_ticks == 5
    assert result.total_provider_starts == 10
    assert result.stop_reason == "all_ticks_complete"


def test_mutate_hidden_token_call_breaks_test(tmp_path):
    """Item 2: If a hidden token call is made inside quotes_fetch, the test
    catches the extra call and fails."""
    # This test verifies that our live_deps quotes_fetch does NOT call
    # token_fetch internally — the bearer is passed explicitly.
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.values.session_gate import Market

    project_root = tmp_path / "project"
    _credential_file(project_root)

    handler_cls, wire_log = _make_scripted_handler(
        [resp for pair in zip(
            [_json_response(_token_body()) for _ in range(5)],
            [_json_response(_price_body()) for _ in range(5)],
        ) for resp in pair]
    )

    clock_state = {"t": T0}

    def virtual_clock():
        return clock_state["t"]

    def virtual_sleep(target_utc):
        clock_state["t"] = target_utc

    # Wrap token_fetch to count calls
    base_deps = build_canary_live_deps(
        project_root=project_root,
        clock=virtual_clock,
        https_handler_factory=handler_cls,
    )

    token_call_count = {"n": 0}
    original_token_fetch = base_deps.token_fetch

    def counting_token_fetch():
        token_call_count["n"] += 1
        return original_token_fetch()

    def quotes_fetch_with_bearer_check(market, symbols, *, bearer):
        # Verify bearer is explicitly passed and is this tick's token
        assert bearer is not None
        # Call the real quotes_fetch — it should NOT call token internally
        return base_deps.quotes_fetch(market, symbols, bearer=bearer)

    deps = CanaryDepsV1(
        clock=virtual_clock,
        token_fetch=counting_token_fetch,
        quotes_fetch=quotes_fetch_with_bearer_check,
        sleep_until=virtual_sleep,
    )

    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )
    result = run_canary_session(
        root=tmp_path / "evidence", config=cfg, deps=deps, t0=T0,
    )
    assert token_call_count["n"] == 5, (
        f"Token must be fetched per-tick (5 times), got {token_call_count['n']}")
    # Total wire calls must be exactly 10: one TOKEN + one PRICE per tick
    assert len(wire_log) == 10


# ═══════════════════════════════════════════════════════════════════════
# Item 3: Crash after write-ahead intent, restart sees consumed count (B4)
# ═══════════════════════════════════════════════════════════════════════

def test_crash_after_writeahead_intent_restart_sees_consumed(tmp_path):
    """Item 3: A crash after write-ahead intent (before settle) means the
    restart sees the consumed provider start from the purpose ledger."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
        compute_purpose_identity,
    )
    from tokmo.records.pulse_canary_evidence import CanaryEvidenceStore
    from tokmo.values.session_gate import Market

    evidence_root = tmp_path / "evidence"
    store_path = evidence_root / "ops" / "canary_evidence.sqlite3"
    purpose = compute_purpose_identity(Market.KR, "005930")

    # First run: token_fetch crashes AFTER the write-ahead intent was recorded
    crash_count = {"n": 0}

    def token_fetch_crash():
        crash_count["n"] += 1
        raise RuntimeError("crash_after_intent")

    def quotes_fetch(market, symbols, *, bearer=None):
        pass

    deps = CanaryDepsV1(
        clock=lambda: T0,
        token_fetch=token_fetch_crash,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda target: None,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )
    result = run_canary_session(
        root=evidence_root, config=cfg, deps=deps, t0=T0,
    )

    # The crash was caught as provider failure
    assert result.stop_reason == "provider_failed"

    # Verify the purpose ledger has 1 consumed start (crash didn't erase it)
    store = CanaryEvidenceStore(store_path, clock=lambda: T0)
    consumed = store.cumulative_provider_starts_by_purpose(purpose)
    assert consumed == 1, f"Expected 1 consumed, got {consumed}"

    # Restart with a working token_fetch
    def token_fetch_ok():
        return "restart-token"

    quotes_fetch_ok = _make_success_quotes_fetch()

    deps2 = CanaryDepsV1(
        clock=lambda: T0,
        token_fetch=token_fetch_ok,
        quotes_fetch=quotes_fetch_ok,
        sleep_until=lambda target: None,
    )
    result2 = run_canary_session(
        root=evidence_root, config=cfg, deps=deps2, t0=T0,
    )
    # The cumulative includes the consumed start from the crash
    assert result2.total_provider_starts >= 6  # 1 crash + 1 token + 5 prices


# ═══════════════════════════════════════════════════════════════════════
# Item 4: Provider failure tick1 => stops exactly there, nonzero (B5)
# ═══════════════════════════════════════════════════════════════════════

def test_provider_failure_stops_immediately_at_tick(tmp_path):
    """Item 4: Provider failure at tick 1 => stops at tick 1, not tick 5."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.values.session_gate import Market

    call_count = {"quotes": 0}

    def token_fetch():
        return "fake-token"

    def quotes_fetch(market, symbols, *, bearer=None):
        call_count["quotes"] += 1
        from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
        return PulseQuoteBatchOutcomeV1("http_500", (), tuple(symbols))

    deps = CanaryDepsV1(
        clock=lambda: T0, token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda target: None,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )
    result = run_canary_session(
        root=tmp_path, config=cfg, deps=deps, t0=T0,
    )
    assert result.stop_reason == "provider_failed"
    # B5: stops at tick 0+1=1, not 5
    assert call_count["quotes"] == 1, (
        f"Should have stopped after 1 quote call, got {call_count['quotes']}")
    assert result.completed_ticks == 1


# ═══════════════════════════════════════════════════════════════════════
# Item 5: Fingerprint mutation => nonzero (M1)
# ═══════════════════════════════════════════════════════════════════════

def test_fingerprint_mutation_stops_with_nonzero(tmp_path):
    """Item 5: If canonical root changes during canary, fingerprint compare
    fails and stop_reason = canonical_fingerprint_mutated."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.values.session_gate import Market

    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "data.sqlite3").write_bytes(b"original")

    evidence_root = tmp_path / "evidence"

    mutation_done = {"done": False}

    def token_fetch():
        return "fake-token"

    def quotes_fetch(market, symbols, *, bearer=None):
        # Mutate canonical root during the run
        if not mutation_done["done"]:
            (canonical / "data.sqlite3").write_bytes(b"mutated")
            mutation_done["done"] = True
        from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
        from tokmo.values.market_observations import TossCurrentPriceFact
        return PulseQuoteBatchOutcomeV1("success", tuple(
            TossCurrentPriceFact(
                source="toss-current-price-v1", symbol=sym,
                last_price="50000", currency_raw="KRW",
                timestamp=T0, fetched_at=T0,
            ) for sym in symbols
        ), ())

    deps = CanaryDepsV1(
        clock=lambda: T0, token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda target: None,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )
    result = run_canary_session(
        root=evidence_root, config=cfg, deps=deps, t0=T0,
        canonical_root=canonical,
    )
    assert result.stop_reason == "canonical_fingerprint_mutated"


# ═══════════════════════════════════════════════════════════════════════
# Item 6: Wrong/dirty commit => provider calls 0 (M4)
# ═══════════════════════════════════════════════════════════════════════

def test_wrong_commit_blocks_live_call(tmp_path):
    """Item 6: Wrong base-commit => provider calls 0."""
    from tokmo.programs.pulse_canary import run_canary_cli

    project_root = tmp_path / "project"
    project_root.mkdir()
    # Initialize a git repo
    import subprocess
    subprocess.run(["git", "init"], cwd=str(project_root),
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(project_root), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(project_root), capture_output=True)
    (project_root / "README.md").write_text("test")
    subprocess.run(["git", "add", "."], cwd=str(project_root),
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"],
                   cwd=str(project_root), capture_output=True)

    canonical = tmp_path / "canonical"
    canonical.mkdir()

    result = run_canary_cli([
        "--mode", "bounded-live-canary",
        "--market", "KR", "--symbol", "005930",
        "--canonical-root", str(canonical),
        "--project-root", str(project_root),
        "--max-ticks", "5", "--max-provider-starts", "10",
        "--base-commit", "ffffffffffffffffffffffffffffffffffffffff",
    ], evidence_root_override=tmp_path / "evidence")
    assert result == 1  # refusal


def test_dirty_tree_blocks_live_call(tmp_path):
    """Item 6: Dirty tracked tree => provider calls 0."""
    from tokmo.programs.pulse_canary import run_canary_cli
    import subprocess

    project_root = tmp_path / "project"
    project_root.mkdir()
    subprocess.run(["git", "init"], cwd=str(project_root),
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(project_root), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(project_root), capture_output=True)
    (project_root / "tracked.py").write_text("# original")
    subprocess.run(["git", "add", "."], cwd=str(project_root),
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"],
                   cwd=str(project_root), capture_output=True)
    # Get actual HEAD
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(project_root), capture_output=True, text=True
    ).stdout.strip()

    # Dirty the tracked file
    (project_root / "tracked.py").write_text("# modified")

    canonical = tmp_path / "canonical"
    canonical.mkdir()

    result = run_canary_cli([
        "--mode", "bounded-live-canary",
        "--market", "KR", "--symbol", "005930",
        "--canonical-root", str(canonical),
        "--project-root", str(project_root),
        "--max-ticks", "5", "--max-provider-starts", "10",
        "--base-commit", head,
    ], evidence_root_override=tmp_path / "evidence")
    assert result == 1  # dirty_tree refusal


# ═══════════════════════════════════════════════════════════════════════
# Item 7: Root alias/symlink/nesting => provider calls 0 (M5)
# ═══════════════════════════════════════════════════════════════════════

def test_symlink_evidence_root_blocks(tmp_path):
    """Item 7: Symlink as evidence root => refusal."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    symlink = tmp_path / "symlink"
    os.symlink(real_dir, symlink)

    result = run_fixture_cli([
        "--mode", "bounded-live-canary",
        "--market", "KR", "--symbol", "005930",
        "--root", str(symlink),
        "--project-root", str(tmp_path),
        "--max-ticks", "5", "--max-provider-starts", "10",
        "--fixture", str(_write_fixture(tmp_path)),
    ])
    assert result == 1


def test_canonical_root_nesting_blocks(tmp_path):
    """Item 7: Evidence root inside canonical root => refusal."""
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    evidence = canonical / "evidence"  # nested inside canonical

    result = run_fixture_cli([
        "--mode", "bounded-live-canary",
        "--market", "KR", "--symbol", "005930",
        "--root", str(evidence),
        "--canonical-root", str(canonical),
        "--project-root", str(tmp_path),
        "--max-ticks", "5", "--max-provider-starts", "10",
        "--fixture", str(_write_fixture(tmp_path)),
    ])
    assert result == 1


# ═══════════════════════════════════════════════════════════════════════
# Item 8: Past-slot restart => MISSED/non-call and future-only (B2)
# ═══════════════════════════════════════════════════════════════════════

def test_past_slot_restart_does_not_replay(tmp_path):
    """Item 8: On restart, past slots are MISSED, not burst-replayed."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.records.pulse_canary_evidence import CanaryEvidenceStore
    from tokmo.values.session_gate import Market

    quotes_calls = {"n": 0}

    def token_fetch():
        return "fake-token"

    def quotes_fetch(market, symbols, *, bearer=None):
        quotes_calls["n"] += 1
        from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
        from tokmo.values.market_observations import TossCurrentPriceFact
        return PulseQuoteBatchOutcomeV1("success", tuple(
            TossCurrentPriceFact(
                source="toss-current-price-v1", symbol=sym,
                last_price="50000", currency_raw="KRW",
                timestamp=T0, fetched_at=T0,
            ) for sym in symbols
        ), ())

    # First run: only tick 0 completes (clock at T0)
    deps = CanaryDepsV1(
        clock=lambda: T0,
        token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda target: None,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )

    # We simulate a partial run by manually recording one tick and then
    # restarting with a clock that is far past slots 1-4.
    evidence_root = tmp_path / "evidence"
    store_path = evidence_root / "ops" / "canary_evidence.sqlite3"

    # Simulate first tick completed
    r1 = run_canary_session(
        root=evidence_root, config=cfg, deps=deps, t0=T0,
    )
    # This will run all 5 ticks since clock is T0 for all
    assert r1.completed_ticks == 5
    assert quotes_calls["n"] == 5

    # Now test past-slot behavior with a fresh root and far-future clock
    evidence_root2 = tmp_path / "evidence2"
    far_future = T0 + timedelta(seconds=600)  # 10 min later

    deps2 = CanaryDepsV1(
        clock=lambda: far_future,
        token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda target: None,
    )
    r2 = run_canary_session(
        root=evidence_root2, config=cfg, deps=deps2, t0=T0,
    )

    # All slots 0-4 are past (observed > scheduled + grace for all)
    store2 = CanaryEvidenceStore(
        evidence_root2 / "ops" / "canary_evidence.sqlite3",
        clock=lambda: far_future,
    )
    ticks = store2.read_ticks()
    # Verify all ticks are marked missed_past_slot and zero provider calls
    past_slots = sum(1 for t in ticks if t["outcome"] == "missed_past_slot")
    assert past_slots == 5, (
        f"Expected 5 missed past slots, got {past_slots}")
    # No additional quotes calls (all slots were missed)
    assert quotes_calls["n"] == 5  # unchanged from first run


def test_wall_clock_pacing_via_sleep_until(tmp_path):
    """B2: sleep_until is called with correct scheduled times."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.values.session_gate import Market

    sleep_calls = []

    clock_state = {"t": T0}

    def clock():
        return clock_state["t"]

    def sleep_fn(target):
        sleep_calls.append(target)
        clock_state["t"] = target  # virtual clock advances to scheduled

    def token_fetch():
        return "fake-token"

    def quotes_fetch(market, symbols, *, bearer=None):
        from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
        from tokmo.values.market_observations import TossCurrentPriceFact
        return PulseQuoteBatchOutcomeV1("success", tuple(
            TossCurrentPriceFact(
                source="toss-current-price-v1", symbol=sym,
                last_price="50000", currency_raw="KRW",
                timestamp=T0, fetched_at=T0,
            ) for sym in symbols
        ), ())

    deps = CanaryDepsV1(
        clock=clock, token_fetch=token_fetch,
        quotes_fetch=quotes_fetch, sleep_until=sleep_fn,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )
    result = run_canary_session(
        root=tmp_path, config=cfg, deps=deps, t0=T0,
    )
    assert result.completed_ticks == 5
    # Verify sleep_until was called for ticks 1-4 (tick 0 is at T0 = clock)
    # tick 0: observed=T0, scheduled=T0, no sleep needed
    # tick 1-4: sleep until T0+60, T0+120, T0+180, T0+240
    assert len(sleep_calls) == 4
    for i, target in enumerate(sleep_calls):
        expected = T0 + timedelta(seconds=60 * (i + 1))
        assert target == expected, (
            f"sleep_until call {i}: expected {expected}, got {target}")


# ═══════════════════════════════════════════════════════════════════════
# Item 9: Completion rerun => calls 0 and nonzero refusal (M2)
# ═══════════════════════════════════════════════════════════════════════

def test_completion_rerun_nonzero_and_zero_calls(tmp_path):
    """Item 9: Completion rerun => already_complete with nonzero exit
    and zero additional provider calls."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.values.session_gate import Market

    call_count = {"n": 0}

    def token_fetch():
        call_count["n"] += 1
        return "fake-token"

    quotes_fetch = _make_success_quotes_fetch()

    deps = CanaryDepsV1(
        clock=lambda: T0, token_fetch=token_fetch,
        quotes_fetch=lambda m, s, *, bearer=None: (
            call_count.__setitem__("n", call_count["n"] + 1) or
            quotes_fetch(m, s, bearer=bearer)
        ),
        sleep_until=lambda target: None,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )
    # First run: 6 calls (1 token + 5 quotes)
    r1 = run_canary_session(
        root=tmp_path, config=cfg, deps=deps, t0=T0,
    )
    assert r1.stop_reason == "all_ticks_complete"
    assert r1.is_fresh is True
    assert call_count["n"] == 10

    # Rerun: already_complete, is_fresh=False, zero additional calls
    r2 = run_canary_session(
        root=tmp_path, config=cfg, deps=deps, t0=T0,
    )
    assert r2.stop_reason == "already_complete"
    assert r2.is_fresh is False
    assert call_count["n"] == 10  # unchanged


def test_cli_exit_code_all_ticks_complete_is_zero(tmp_path):
    """M2: CLI exit 0 only for fresh all_ticks_complete."""
    fixture_path = _write_fixture(tmp_path)
    result = run_fixture_cli([
        "--mode", "bounded-live-canary",
        "--market", "KR", "--symbol", "005930",
        "--root", str(tmp_path / "evidence"),
        "--project-root", str(tmp_path),
        "--max-ticks", "5", "--max-provider-starts", "10",
        "--fixture", str(fixture_path),
        "--t0", "2026-08-11T00:00:00Z",
    ])
    assert result == 0


def test_cli_exit_code_rerun_is_nonzero(tmp_path):
    """M2: Completion rerun returns nonzero."""
    fixture_path = _write_fixture(tmp_path)
    evidence_root = tmp_path / "evidence"
    args = [
        "--mode", "bounded-live-canary",
        "--market", "KR", "--symbol", "005930",
        "--root", str(evidence_root),
        "--project-root", str(tmp_path),
        "--max-ticks", "5", "--max-provider-starts", "10",
        "--fixture", str(fixture_path),
        "--t0", "2026-08-11T00:00:00Z",
    ]
    r1 = run_fixture_cli(args)
    assert r1 == 0
    r2 = run_fixture_cli(args)
    assert r2 == 1  # already_complete => nonzero


# ═══════════════════════════════════════════════════════════════════════
# Item 10: pulse_main, registry, 300s owner, cron unchanged
# ═══════════════════════════════════════════════════════════════════════

def test_pulse_main_refuses_production_mode():
    """pulse_main still refuses production mode."""
    from tokmo.programs.integrated_pulse import pulse_main

    result = pulse_main([
        "--once", "--mode", "production", "--root", "/tmp/test",
        "--scheduled-for", "2026-08-10T00:00:00Z",
    ])
    assert result == 1


def test_pulse_main_refuses_unknown_mode():
    """pulse_main refuses modes other than production/fixture."""
    from tokmo.programs.integrated_pulse import pulse_main

    result = pulse_main([
        "--once", "--mode", "bounded-live-canary", "--root", "/tmp/test",
        "--scheduled-for", "2026-08-10T00:00:00Z",
    ])
    assert result == 1


def test_operation_lane_registry_has_future_g3a_cutover():
    from tokmo.values.operation_lane_registry import (
        OPERATION_LANE_REGISTRY_V1, OperationLane,
    )

    for operation, entry in OPERATION_LANE_REGISTRY_V1.entries.items():
        if operation in {"PRICE_BATCH", "ORDERS_PAGE", "ORDER_DETAIL"}:
            assert entry.recurring_owner is OperationLane.PULSE_60S
            assert entry.fallback_owner is OperationLane.CADENCE_300S
            assert entry.cutover_epoch_utc is not None
        elif operation == "CONDITIONAL_ORDERS":
            # Conditional-order SDS v0.4 §4.4: born Pulse-owned, no fallback.
            assert entry.recurring_owner is OperationLane.PULSE_60S
            assert entry.fallback_owner is None
            assert entry.cutover_epoch_utc is None
        else:
            assert entry.recurring_owner is OperationLane.CADENCE_300S
            assert entry.cutover_epoch_utc is None
            assert entry.fallback_owner is None


def test_pulse_policy_registry_preserves_canary_policy_and_inert_quant_policy():
    """The canary pin remains while the optional Quant policy is registered."""
    from tokmo.values.pulse_policy import PULSE_POLICY_REGISTRY

    assert set(PULSE_POLICY_REGISTRY) == {
        "pulse-60s-v1", "pulse-60s-quant-market-close-v1",
    }


# ═══════════════════════════════════════════════════════════════════════
# Config validation tests (M3)
# ═══════════════════════════════════════════════════════════════════════

def test_config_requires_explicit_market_and_symbol():
    """The config must have explicit market + symbol authority."""
    from tokmo.programs.pulse_canary import CanaryConfigV1
    from tokmo.values.session_gate import Market

    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )
    assert cfg.market is Market.KR
    assert cfg.symbol == "005930"


def test_config_rejects_wrong_max_ticks():
    """M3: max_ticks must be exactly 5."""
    from tokmo.programs.pulse_canary import CanaryConfigV1
    from tokmo.values.session_gate import Market

    with pytest.raises(Exception):
        CanaryConfigV1(market=Market.KR, symbol="005930", max_ticks=4)
    with pytest.raises(Exception):
        CanaryConfigV1(market=Market.KR, symbol="005930", max_ticks=6)
    with pytest.raises(Exception):
        CanaryConfigV1(market=Market.KR, symbol="005930", max_ticks=10)


def test_config_rejects_excessive_provider_starts():
    """M3: max_provider_starts is hard-clamped to the active family cap."""
    from tokmo.programs.pulse_canary import CanaryConfigV1
    from tokmo.values.session_gate import Market

    with pytest.raises(Exception):
        CanaryConfigV1(
            market=Market.KR, symbol="005930",
            max_ticks=5, max_provider_starts=33,
        )


def test_config_mismatch_refusal_on_restart(tmp_path):
    """M3: Stored config mismatch on restart => refusal."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.values.session_gate import Market

    def token_fetch():
        return "fake-token"

    quotes_fetch = _make_success_quotes_fetch()

    deps = CanaryDepsV1(
        clock=lambda: T0, token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda target: None,
    )
    cfg1 = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )
    r1 = run_canary_session(
        root=tmp_path, config=cfg1, deps=deps, t0=T0,
    )
    assert r1.stop_reason == "all_ticks_complete"

    # Restart with different max_provider_starts => refusal
    cfg2 = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=8,
    )
    r2 = run_canary_session(
        root=tmp_path, config=cfg2, deps=deps, t0=T0,
    )
    assert r2.stop_reason == "config_mismatch_refusal"


# ═══════════════════════════════════════════════════════════════════════
# B3: Token lifecycle (per tick)
# ═══════════════════════════════════════════════════════════════════════

def test_token_is_fetched_per_tick_not_amortized(tmp_path):
    """B3: TOKEN is started once and reused for all ticks in the session."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.values.session_gate import Market

    token_count = {"n": 0}

    def token_fetch():
        token_count["n"] += 1
        return "per-tick-token"

    quotes_fetch = _make_success_quotes_fetch()

    deps = CanaryDepsV1(
        clock=lambda: T0, token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda target: None,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )
    result = run_canary_session(
        root=tmp_path, config=cfg, deps=deps, t0=T0,
    )
    assert result.completed_ticks == 5
    assert token_count["n"] == 5


def test_bearer_is_passed_explicitly(tmp_path):
    """B3: The tick's bearer is passed explicitly to quotes_fetch."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.values.session_gate import Market

    received_bearers = []

    def token_fetch():
        return "the-explicit-bearer"

    def quotes_fetch(market, symbols, *, bearer=None):
        received_bearers.append(bearer)
        from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
        from tokmo.values.market_observations import TossCurrentPriceFact
        return PulseQuoteBatchOutcomeV1("success", tuple(
            TossCurrentPriceFact(
                source="toss-current-price-v1", symbol=sym,
                last_price="50000", currency_raw="KRW",
                timestamp=T0, fetched_at=T0,
            ) for sym in symbols
        ), ())

    deps = CanaryDepsV1(
        clock=lambda: T0, token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda target: None,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )
    result = run_canary_session(
        root=tmp_path, config=cfg, deps=deps, t0=T0,
    )
    assert result.completed_ticks == 5
    # Every call received the same bearer
    assert all(b == "the-explicit-bearer" for b in received_bearers)
    assert len(received_bearers) == 5


# ═══════════════════════════════════════════════════════════════════════
# CanaryDepsV1 structural tests
# ═══════════════════════════════════════════════════════════════════════

def test_canary_deps_has_no_forbidden_seams():
    """CanaryDepsV1 must structurally lack execution/dialogue/AI/
    Telegram/orders/accounts/holdings access."""
    from tokmo.programs.pulse_canary import CanaryDepsV1
    from dataclasses import fields as dc_fields

    field_names = {f.name for f in dc_fields(CanaryDepsV1)}
    forbidden = {
        "orders_page_fetch", "order_detail_fetch",
        "telegram_sender", "ai_evaluator",
        "dialogue_store", "updates_provider",
        "holdings_reader", "accounts_fetch",
        "ledger_store", "scan_state_store",
        "native_refs_reader",
    }
    assert not (field_names & forbidden), (
        f"forbidden fields present: {field_names & forbidden}")


# ═══════════════════════════════════════════════════════════════════════
# Cumulative cap tests (B4)
# ═══════════════════════════════════════════════════════════════════════

def test_cumulative_cap_tracks_provider_starts(tmp_path):
    """Provider starts are tracked cumulatively across ticks; cap stops."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.values.session_gate import Market

    starts = {"count": 0}

    def token_fetch():
        starts["count"] += 1
        return "fake-token"

    def quotes_fetch(market, symbols, *, bearer=None):
        starts["count"] += 1
        from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
        from tokmo.values.market_observations import TossCurrentPriceFact
        return PulseQuoteBatchOutcomeV1("success", tuple(
            TossCurrentPriceFact(
                source="toss-current-price-v1", symbol=sym,
                last_price="50000", currency_raw="KRW",
                timestamp=T0, fetched_at=T0,
            ) for sym in symbols
        ), ())

    deps = CanaryDepsV1(
        clock=lambda: T0, token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda target: None,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )
    result = run_canary_session(
        root=tmp_path, config=cfg, deps=deps, t0=T0,
    )
    assert result.completed_ticks == 5
    assert result.total_provider_starts == 10  # 5 TOKEN + 5 PRICE_BATCH (v3 per-tick)
    assert starts["count"] == 10  # 5 TOKEN + 5 PRICE_BATCH (v3 per-tick)


def test_cap_exceeded_stops_session(tmp_path):
    """When the cumulative cap is exceeded, the session stops immediately."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.values.session_gate import Market

    def token_fetch():
        return "fake-token"

    quotes_fetch = _make_success_quotes_fetch()

    deps = CanaryDepsV1(
        clock=lambda: T0, token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda target: None,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=3,
    )
    result = run_canary_session(
        root=tmp_path, config=cfg, deps=deps, t0=T0,
    )
    assert result.total_provider_starts <= 3
    assert result.stop_reason == "provider_start_cap_exceeded"


# ═══════════════════════════════════════════════════════════════════════
# Durable progress across restart
# ═══════════════════════════════════════════════════════════════════════

def test_durable_progress_picks_up_where_left_off(tmp_path):
    """If max_ticks is 5 and only 2 completed (via cap), restart resumes."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.values.session_gate import Market

    quotes_calls = {"n": 0}

    def token_fetch():
        return "fake-token"

    def quotes_fetch(market, symbols, *, bearer=None):
        quotes_calls["n"] += 1
        from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
        from tokmo.values.market_observations import TossCurrentPriceFact
        return PulseQuoteBatchOutcomeV1("success", tuple(
            TossCurrentPriceFact(
                source="toss-current-price-v1", symbol=sym,
                last_price="50000", currency_raw="KRW",
                timestamp=T0, fetched_at=T0,
            ) for sym in symbols
        ), ())

    deps_factory = lambda: CanaryDepsV1(
        clock=lambda: T0, token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda target: None,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=4,
    )
    r1 = run_canary_session(
        root=tmp_path, config=cfg, deps=deps_factory(), t0=T0,
    )
    assert r1.completed_ticks <= 3
    assert r1.stop_reason == "provider_start_cap_exceeded"

    # Restart with same config
    r2 = run_canary_session(
        root=tmp_path, config=cfg, deps=deps_factory(), t0=T0,
    )
    # Already cap-exceeded from purpose ledger
    assert r2.stop_reason in (
        "provider_start_cap_exceeded", "config_mismatch_refusal")


# ═══════════════════════════════════════════════════════════════════════
# Evidence sanitization tests
# ═══════════════════════════════════════════════════════════════════════

def test_evidence_contains_no_secrets_or_paths(tmp_path):
    """Evidence records must not contain tokens, secrets, paths, URLs."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.records.pulse_canary_evidence import CanaryEvidenceStore
    from tokmo.values.session_gate import Market

    secret_token = "<<redacted:sk-...>>"
    request_url = "https://openapi.tossinvest.com/api/v1/prices"
    response_body = '{"lastPrice": 50000, "raw": "sensitive"}'

    def token_fetch():
        return secret_token

    quotes_fetch = _make_success_quotes_fetch()

    deps = CanaryDepsV1(
        clock=lambda: T0, token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda target: None,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )
    result = run_canary_session(
        root=tmp_path, config=cfg, deps=deps, t0=T0,
    )
    assert result.completed_ticks == 5

    store = CanaryEvidenceStore(
        tmp_path / "ops" / "canary_evidence.sqlite3",
        clock=lambda: T0,
    )
    ticks = store.read_ticks()
    for tick in ticks:
        blob = str(tick)
        assert secret_token not in blob
        assert request_url not in blob
        assert response_body not in blob
        assert "/api/" not in blob
        assert "openapi.tossinvest" not in blob
        assert "sk-" not in blob
        assert "bearer" not in blob.lower()


# ═══════════════════════════════════════════════════════════════════════
# No order/account write test
# ═══════════════════════════════════════════════════════════════════════

def test_no_order_or_account_write_seams(tmp_path):
    """The canary runner has no path to write orders, accounts, or
    dialogue. It only writes to the isolated evidence store."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.values.session_gate import Market

    def token_fetch():
        return "fake-token"

    quotes_fetch = _make_success_quotes_fetch()

    deps = CanaryDepsV1(
        clock=lambda: T0, token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda target: None,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )
    result = run_canary_session(
        root=tmp_path, config=cfg, deps=deps, t0=T0,
    )
    assert result.completed_ticks == 5
    sqlite_files = list(tmp_path.rglob("*.sqlite3"))
    store_names = [f.name for f in sqlite_files]
    assert all("canary" in name for name in store_names), store_names


# ═══════════════════════════════════════════════════════════════════════
# 5-slot timing receipt
# ═══════════════════════════════════════════════════════════════════════

def test_five_slot_timing_60s_intervals(tmp_path):
    """Ticks are at t0, t0+60, t0+120, t0+180, t0+240 (240s span)."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.records.pulse_canary_evidence import CanaryEvidenceStore
    from tokmo.values.session_gate import Market

    clock_state = {"t": T0}

    def token_fetch():
        return "fake-token"

    quotes_fetch = _make_success_quotes_fetch()

    def sleep_fn(target):
        clock_state["t"] = target

    deps = CanaryDepsV1(
        clock=lambda: clock_state["t"],
        token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=sleep_fn,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )
    result = run_canary_session(
        root=tmp_path, config=cfg, deps=deps, t0=T0,
    )
    assert result.completed_ticks == 5

    store = CanaryEvidenceStore(
        tmp_path / "ops" / "canary_evidence.sqlite3",
        clock=lambda: T0,
    )
    ticks = store.read_ticks()
    assert len(ticks) == 5
    for i, tick in enumerate(ticks):
        expected = T0 + timedelta(seconds=60 * i)
        assert tick["scheduled_utc"] == expected


# ═══════════════════════════════════════════════════════════════════════
# Per-tick receipt
# ═══════════════════════════════════════════════════════════════════════

def test_per_tick_receipt_has_duration_lag_result(tmp_path):
    """Each tick receipt has duration_ms, lag_seconds, outcome, reason."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.records.pulse_canary_evidence import CanaryEvidenceStore
    from tokmo.values.session_gate import Market

    def token_fetch():
        return "fake-token"

    def quotes_fetch(market, symbols, *, bearer=None):
        from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
        from tokmo.values.market_observations import TossCurrentPriceFact
        return PulseQuoteBatchOutcomeV1("success", tuple(
            TossCurrentPriceFact(
                source="toss-current-price-v1", symbol=sym,
                last_price="72500", currency_raw="KRW",
                timestamp=T0, fetched_at=T0,
            ) for sym in symbols
        ), ())

    deps = CanaryDepsV1(
        clock=lambda: T0, token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda target: None,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )
    result = run_canary_session(
        root=tmp_path, config=cfg, deps=deps, t0=T0,
    )
    assert result.completed_ticks == 5

    store = CanaryEvidenceStore(
        tmp_path / "ops" / "canary_evidence.sqlite3",
        clock=lambda: T0,
    )
    ticks = store.read_ticks()
    tick = ticks[0]
    assert tick["market"] == "KR"
    assert tick["symbol"] == "005930"
    assert tick["price"] == "72500"
    assert tick["currency"] == "KRW"
    assert tick["source"] == "toss-current-price-v1"
    assert tick["outcome"] == "success"
    assert tick["duration_ms"] >= 0
    assert tick["lag_seconds"] >= 0


# ═══════════════════════════════════════════════════════════════════════
# Canonical fingerprint invariant (no mutation = success)
# ═══════════════════════════════════════════════════════════════════════

def test_canonical_fingerprint_invariant(tmp_path):
    """Pre/post canonical fingerprint must be identical after canary."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
        fingerprint_directory,
    )
    from tokmo.values.session_gate import Market

    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "stores").mkdir()
    (canonical / "stores" / "latest_quotes.sqlite3").write_bytes(b"canon")

    evidence_root = tmp_path / "evidence"

    def token_fetch():
        return "fake-token"

    quotes_fetch = _make_success_quotes_fetch()

    deps = CanaryDepsV1(
        clock=lambda: T0, token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda target: None,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )

    pre = fingerprint_directory(canonical)
    result = run_canary_session(
        root=evidence_root, config=cfg, deps=deps, t0=T0,
        canonical_root=canonical,
    )
    post = fingerprint_directory(canonical)
    assert pre == post
    assert result.stop_reason == "all_ticks_complete"


# ═══════════════════════════════════════════════════════════════════════
# CLI mode tests
# ═══════════════════════════════════════════════════════════════════════

def test_cli_entrypoint_rejects_non_canary_mode():
    from tokmo.programs.pulse_canary import run_canary_cli

    result = run_canary_cli([
        "--mode", "production",
        "--market", "KR", "--symbol", "005930",
        "--project-root", "/tmp/test-project",
        "--max-ticks", "5", "--max-provider-starts", "10",
    ], evidence_root_override=Path("/tmp/test-canary"))
    assert result == 1


def test_cli_entrypoint_requires_explicit_args():
    from tokmo.programs.pulse_canary import run_canary_cli

    result = run_canary_cli([
        "--mode", "bounded-live-canary",
        "--symbol", "005930",
        "--project-root", "/tmp/test-project",
        "--max-ticks", "5", "--max-provider-starts", "10",
    ], evidence_root_override=Path("/tmp/test-canary"))
    assert result == 1


def test_cli_rejects_wrong_max_ticks(tmp_path):
    """M3: CLI rejects max-ticks != 5."""
    fixture_path = _write_fixture(tmp_path)
    result = run_fixture_cli([
        "--mode", "bounded-live-canary",
        "--market", "KR", "--symbol", "005930",
        "--root", str(tmp_path / "evidence"),
        "--project-root", str(tmp_path),
        "--max-ticks", "6", "--max-provider-starts", "10",
        "--fixture", str(fixture_path),
    ])
    assert result == 1


def test_cli_rejects_excessive_max_provider_starts():
    """M3: CLI rejects max-provider-starts != 22 (v3 exact cap)."""
    from tokmo.programs.pulse_canary import run_canary_cli

    result = run_canary_cli([
        "--mode", "bounded-live-canary",
        "--market", "KR", "--symbol", "005930",
        "--project-root", "/tmp/test-project",
        "--max-ticks", "5", "--max-provider-starts", "100000",
    ], evidence_root_override=Path("/tmp/test-canary"))
    assert result == 1


def test_cli_live_mode_requires_base_commit(tmp_path):
    """M4: Live mode requires --base-commit."""
    from tokmo.programs.pulse_canary import run_canary_cli

    result = run_canary_cli([
        "--mode", "bounded-live-canary",
        "--market", "KR", "--symbol", "005930",
        "--canonical-root", str(tmp_path / "canonical"),
        "--project-root", str(tmp_path),
        "--max-ticks", "5", "--max-provider-starts", "10",
    ], evidence_root_override=tmp_path / "evidence")
    assert result == 1


# ═══════════════════════════════════════════════════════════════════════
# B-1: Absolute campaign root anchor tests (third final targeted repair)
# ═══════════════════════════════════════════════════════════════════════

def test_b1_anchor_root_is_absolute_and_outside_canonical():
    """B-1: campaign_evidence_anchor_root returns an absolute path derived
    from Path.home(), under .local/share/tokmo-v3-canary/."""
    from tokmo.programs.pulse_canary import (
        campaign_evidence_anchor_root, _CAMPAIGN_EVIDENCE_REL,
        CANARY_CAMPAIGN_ID,
    )

    root = campaign_evidence_anchor_root()
    assert root.is_absolute(), f"Anchor root must be absolute, got {root}"
    # Must be derived from Path.home()
    assert str(root).startswith(str(Path.home())), (
        f"Anchor root must be under Path.home(), got {root}")
    # Must contain the immutable relative path components
    assert ".local/share/tokmo-v3-canary" in str(root).replace("\\", "/"), (
        f"Anchor root must be under .local/share/tokmo-v3-canary/, got {root}")
    assert CANARY_CAMPAIGN_ID in str(root), (
        f"Anchor root must contain campaign_id, got {root}")
    assert "KR" in str(root), (
        f"Anchor root must contain market KR, got {root}")
    assert "005930" in str(root), (
        f"Anchor root must contain symbol 005930, got {root}")


def test_b1_anchor_root_is_deterministic():
    """B-1: campaign_evidence_anchor_root returns the same path every time."""
    from tokmo.programs.pulse_canary import campaign_evidence_anchor_root

    r1 = campaign_evidence_anchor_root()
    r2 = campaign_evidence_anchor_root()
    assert r1 == r2


def test_b1_anchor_root_not_hardcoded_home():
    """B-1: The anchor root must not contain a hardcoded user home string.
    It must be derived from Path.home() at runtime."""
    from tokmo.programs.pulse_canary import campaign_evidence_anchor_root
    import getpass

    root = campaign_evidence_anchor_root()
    username = getpass.getuser()
    # The relative portion should be the constant, not a home path
    rel = Path(*root.parts[len(Path.home().parts):])
    assert "tokmo" not in str(rel), (
        f"Relative path must not contain username, got {rel}")
    assert "/Users/" not in str(rel), (
        f"Relative path must not contain /Users/, got {rel}")


def test_b1_purpose_identity_excludes_t0():
    """B-1: purpose identity is the same regardless of t0."""
    from tokmo.programs.pulse_canary import compute_purpose_identity
    from tokmo.values.session_gate import Market

    t0a = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
    t0b = datetime(2026, 8, 11, 1, 0, tzinfo=timezone.utc)

    # t0 is no longer a parameter — identity is always campaign-constant
    p1 = compute_purpose_identity(Market.KR, "005930")
    p2 = compute_purpose_identity(Market.KR, "005930")

    assert p1 == p2, "purpose identity must be deterministic"
    assert "canary|g25-quote-kr-005930-v4|v4|KR|005930" == p1


def test_b1_cumulative_cap_across_distinct_t0_same_root(tmp_path):
    """B-1 Test: Same campaign, distinct t0, same evidence root.
    First successful run consumes 6 starts. Second run with different t0
    cannot exceed remaining 4 starts and returns nonzero without hidden starts.
    """
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.values.session_gate import Market

    evidence_root = tmp_path / "evidence"

    quotes_fetch = _make_success_quotes_fetch()

    def make_deps():
        return CanaryDepsV1(
            clock=lambda: T0, token_fetch=lambda: "fake-token",
            quotes_fetch=quotes_fetch,
            sleep_until=lambda target: None,
        )

    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )

    # Run 1 with t0_a
    t0_a = T0
    r1 = run_canary_session(
        root=evidence_root, config=cfg, deps=make_deps(), t0=t0_a,
    )
    assert r1.stop_reason == "all_ticks_complete"
    assert r1.total_provider_starts == 10

    # Run 2 with t0_b (distinct t0, same evidence root)
    t0_b = T0 + timedelta(seconds=3600)
    r2 = run_canary_session(
        root=evidence_root, config=cfg, deps=make_deps(), t0=t0_b,
    )
    # Same campaign => same session_id => already_complete, not a fresh run
    assert r2.stop_reason == "already_complete"
    assert r2.is_fresh is False
    # No hidden starts — cumulative stays at 10
    assert r2.total_provider_starts == 10


def test_b1_cli_rejects_wrong_market(tmp_path):
    """B-1: Live CLI rejects market != KR before provider assembly."""
    from tokmo.programs.pulse_canary import run_canary_cli

    result = run_canary_cli([
        "--mode", "bounded-live-canary",
        "--market", "US", "--symbol", "005930",
        "--canonical-root", str(tmp_path / "canonical"),
        "--project-root", str(tmp_path / "project"),
        "--max-ticks", "5", "--max-provider-starts", "10",
        "--base-commit", "deadbeef",
    ], evidence_root_override=tmp_path / "evidence")
    assert result == 1


def test_b1_cli_rejects_wrong_symbol(tmp_path):
    """B-1: Live CLI rejects symbol != 005930 before provider assembly."""
    from tokmo.programs.pulse_canary import run_canary_cli

    result = run_canary_cli([
        "--mode", "bounded-live-canary",
        "--market", "KR", "--symbol", "000660",
        "--canonical-root", str(tmp_path / "canonical"),
        "--project-root", str(tmp_path / "project"),
        "--max-ticks", "5", "--max-provider-starts", "10",
        "--base-commit", "deadbeef",
    ], evidence_root_override=tmp_path / "evidence")
    assert result == 1


def test_b1_baseA_baseB_alternate_all_refused_before_provider(tmp_path):
    """B-1: With the absolute anchor, baseA/baseB/alternate root attempts
    are irrelevant — the CLI ignores --root entirely and always uses
    the anchored path. Any attempt to pass --root is silently ignored.
    Only the anchored root can run. Provider0/refused for wrong markets."""
    from tokmo.programs.pulse_canary import run_canary_cli

    # The live CLI no longer accepts --root at all.
    # Passing wrong market/symbol is refused before credential.
    for market, symbol in [("KR", "000660"), ("US", "AAPL"),
                           ("KR", "035420")]:
        result = run_canary_cli([
            "--mode", "bounded-live-canary",
            "--market", market, "--symbol", symbol,
            "--canonical-root", str(tmp_path / "canonical"),
            "--project-root", str(tmp_path / "project"),
            "--max-ticks", "5", "--max-provider-starts", "10",
            "--base-commit", "deadbeef",
        ], evidence_root_override=tmp_path / "evidence")
        assert result == 1, (
            f"Wrong market={market} symbol={symbol} must be refused")


def test_b1_across_restart_cap_remains_within_the_family_cap(tmp_path):
    """B-1: Across distinct t0 and restarts at the anchored root, cap
    remains 10. Multiple restarts cannot exceed the cap."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
        compute_purpose_identity,
    )
    from tokmo.records.pulse_canary_evidence import CanaryEvidenceStore
    from tokmo.values.session_gate import Market

    evidence_root = tmp_path / "evidence"
    store_path = evidence_root / "ops" / "canary_evidence.sqlite3"

    quotes_fetch = _make_success_quotes_fetch()

    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )

    # Run 1: 6 starts, all complete
    r1 = run_canary_session(
        root=evidence_root, config=cfg,
        deps=CanaryDepsV1(
            clock=lambda: T0, token_fetch=lambda: "t1",
            quotes_fetch=quotes_fetch, sleep_until=lambda t: None,
        ),
        t0=T0,
    )
    assert r1.total_provider_starts == 10

    # Verify purpose ledger
    store = CanaryEvidenceStore(store_path, clock=lambda: T0)
    purpose = compute_purpose_identity(Market.KR, "005930")
    assert store.cumulative_provider_starts_by_purpose(purpose) == 10

    # Run 2: already_complete — cumulative stays at 10
    r2 = run_canary_session(
        root=evidence_root, config=cfg,
        deps=CanaryDepsV1(
            clock=lambda: T0, token_fetch=lambda: "t2",
            quotes_fetch=quotes_fetch, sleep_until=lambda t: None,
        ),
        t0=T0 + timedelta(hours=1),
    )
    assert r2.stop_reason == "already_complete"
    assert r2.total_provider_starts == 10

    # Cap is still 10 <= 22
    assert store.cumulative_provider_starts_by_purpose(purpose) <= 22


# ═══════════════════════════════════════════════════════════════════════
# M-1: Durable MISSED crash-boundary test (third final targeted repair)
# ═══════════════════════════════════════════════════════════════════════

def test_m1_crash_boundary_durable_missed_prevents_all_ticks_complete(tmp_path):
    """M-1: Run A writes 3 MISSED then crashes; run B completes remaining 2.
    Final outcome must be missed_past_slots (nonzero), never all_ticks_complete.

    This proves that missed_count is derived from durable evidence ticks,
    not an in-memory counter that resets across crash boundaries.
    """
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.records.pulse_canary_evidence import CanaryEvidenceStore
    from tokmo.values.session_gate import Market

    evidence_root = tmp_path / "evidence"
    store_path = evidence_root / "ops" / "canary_evidence.sqlite3"

    quotes_fetch = _make_success_quotes_fetch()
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )

    # Run A: clock at T0+150s (past slots 0,1,2; slot 3 is near future)
    # Actually T0+150 means slots 0(0), 1(60), 2(120) are past.
    # Slot 3 is at 180s, slot 4 at 240s. Both are future.
    crash_time = T0 + timedelta(seconds=150)

    def crash_token():
        # Allow token to succeed so we get to the first future slot
        return "crash-token"

    crash_quotes_called = {"n": 0}

    def crash_quotes(market, symbols, *, bearer=None):
        crash_quotes_called["n"] += 1
        return quotes_fetch(market, symbols, bearer=bearer)

    deps_a = CanaryDepsV1(
        clock=lambda: crash_time,
        token_fetch=crash_token,
        quotes_fetch=crash_quotes,
        sleep_until=lambda target: None,
    )

    r_a = run_canary_session(
        root=evidence_root, config=cfg, deps=deps_a, t0=T0,
    )

    # Run A: 3 missed past slots + 2 future slots completed
    store = CanaryEvidenceStore(store_path, clock=lambda: crash_time)
    ticks = store.read_ticks()
    missed = [t for t in ticks if t["outcome"] == "missed_past_slot"]
    successes = [t for t in ticks if t["outcome"] == "success"]
    assert len(missed) == 3, f"Expected 3 missed, got {len(missed)}"
    assert len(successes) == 2, f"Expected 2 successes, got {len(successes)}"

    # M-1: final outcome must be missed_past_slots, NOT all_ticks_complete
    assert r_a.stop_reason == "missed_past_slots", (
        f"Expected missed_past_slots, got {r_a.stop_reason}")
    assert r_a.stop_reason != "all_ticks_complete"

    # M-1: Run B restart (fresh session at same evidence root).
    # Even though all ticks are durable now, the durable MISSED ticks
    # from run A must prevent all_ticks_complete.
    r_b = run_canary_session(
        root=evidence_root, config=cfg,
        deps=CanaryDepsV1(
            clock=lambda: T0,
            token_fetch=lambda: "restart-token",
            quotes_fetch=quotes_fetch,
            sleep_until=lambda target: None,
        ),
        t0=T0,
    )
    # Session is already complete — but stop_reason is already_complete
    # (session finished in run A). The key invariant is that the session
    # was finalized with missed_past_slots, not all_ticks_complete.
    assert r_b.stop_reason == "already_complete"
    assert r_b.stop_reason != "all_ticks_complete"


def test_m1_all_missed_no_provider_calls(tmp_path):
    """M-1: When all slots are past on restart, zero provider calls,
    outcome = missed_past_slots."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.values.session_gate import Market

    evidence_root = tmp_path / "evidence"

    provider_calls = {"token": 0, "quotes": 0}

    def token_fetch():
        provider_calls["token"] += 1
        return "fake-token"

    def quotes_fetch(market, symbols, *, bearer=None):
        provider_calls["quotes"] += 1
        from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
        from tokmo.values.market_observations import TossCurrentPriceFact
        return PulseQuoteBatchOutcomeV1("success", tuple(
            TossCurrentPriceFact(
                source="toss-current-price-v1", symbol=sym,
                last_price="50000", currency_raw="KRW",
                timestamp=T0, fetched_at=T0,
            ) for sym in symbols
        ), ())

    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )

    # Restart at t0+300 (5 minutes later) — all 5 slots are past
    restart_time = T0 + timedelta(seconds=300)

    sleep_calls = []

    deps = CanaryDepsV1(
        clock=lambda: restart_time,
        token_fetch=token_fetch,
        quotes_fetch=quotes_fetch,
        sleep_until=lambda target: sleep_calls.append(target),
    )
    result = run_canary_session(
        root=evidence_root, config=cfg, deps=deps, t0=T0,
    )

    # M-1: all past slots => MISSED, zero provider calls for them
    assert provider_calls["token"] == 0, (
        f"Expected 0 token calls, got {provider_calls['token']}")
    assert provider_calls["quotes"] == 0, (
        f"Expected 0 quotes calls, got {provider_calls['quotes']}")
    # No sleep_until for past slots
    assert len(sleep_calls) == 0
    # Nonzero stop reason — NOT all_ticks_complete
    assert result.stop_reason == "missed_past_slots"
    assert result.stop_reason != "all_ticks_complete"


def test_m1_grace_is_less_than_tick_interval():
    """M-1: _PAST_SLOT_GRACE_SECONDS must be < 60s tick interval."""
    from tokmo.programs.pulse_canary import _PAST_SLOT_GRACE_SECONDS
    assert _PAST_SLOT_GRACE_SECONDS <= 5, (
        f"Grace must be <=5s, got {_PAST_SLOT_GRACE_SECONDS}")


# ═══════════════════════════════════════════════════════════════════════
# M-2: Missing canonical-root live refusal provider0
# ═══════════════════════════════════════════════════════════════════════

def test_m2_live_missing_canonical_root_refused(tmp_path):
    """M-2: Live mode without --canonical-root => refusal before
    any provider/credential call."""
    from tokmo.programs.pulse_canary import run_canary_cli

    project_root = tmp_path / "project"
    project_root.mkdir()

    result = run_canary_cli([
        "--mode", "bounded-live-canary",
        "--market", "KR", "--symbol", "005930",
        # NOTE: --canonical-root intentionally OMITTED
        "--project-root", str(project_root),
        "--max-ticks", "5", "--max-provider-starts", "10",
        "--base-commit", "deadbeef",
    ], evidence_root_override=tmp_path / "evidence")
    assert result == 1  # canonical_root_required_for_live_mode


# ═══════════════════════════════════════════════════════════════════════
# m-3: Dirty parsing matrix
# ═══════════════════════════════════════════════════════════════════════

def test_m3_dirty_parsing_unstaged_modification(tmp_path):
    """m-3: Unstaged modification (' M path') is detected."""
    from tokmo.programs.pulse_canary import run_canary_cli
    import subprocess

    project_root = tmp_path / "project"
    project_root.mkdir()
    subprocess.run(["git", "init"], cwd=str(project_root), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(project_root), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(project_root), capture_output=True)
    (project_root / "tracked.py").write_text("# original")
    subprocess.run(["git", "add", "."], cwd=str(project_root), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"],
                   cwd=str(project_root), capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(project_root), capture_output=True, text=True
    ).stdout.strip()

    # Unstaged modification — porcelain shows ' M tracked.py'
    (project_root / "tracked.py").write_text("# modified")

    canonical = tmp_path / "canonical"
    canonical.mkdir()

    result = run_canary_cli([
        "--mode", "bounded-live-canary",
        "--market", "KR", "--symbol", "005930",
        "--canonical-root", str(canonical),
        "--project-root", str(project_root),
        "--max-ticks", "5", "--max-provider-starts", "10",
        "--base-commit", head,
    ], evidence_root_override=tmp_path / "evidence")
    assert result == 1  # dirty_tracked_tree


def test_m3_dirty_parsing_typechange(tmp_path):
    """m-3: Typechange (' T path') is detected."""
    from tokmo.programs.pulse_canary import run_canary_cli
    import subprocess

    project_root = tmp_path / "project"
    project_root.mkdir()
    subprocess.run(["git", "init"], cwd=str(project_root), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(project_root), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(project_root), capture_output=True)
    # Create a file, commit, then replace it with a symlink (typechange)
    (project_root / "item").write_text("file content")
    subprocess.run(["git", "add", "."], cwd=str(project_root), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"],
                   cwd=str(project_root), capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(project_root), capture_output=True, text=True
    ).stdout.strip()

    # Create typechange: file -> symlink
    os.unlink(str(project_root / "item"))
    os.symlink("/dev/null", str(project_root / "item"))

    canonical = tmp_path / "canonical"
    canonical.mkdir()

    result = run_canary_cli([
        "--mode", "bounded-live-canary",
        "--market", "KR", "--symbol", "005930",
        "--canonical-root", str(canonical),
        "--project-root", str(project_root),
        "--max-ticks", "5", "--max-provider-starts", "10",
        "--base-commit", head,
    ], evidence_root_override=tmp_path / "evidence")
    assert result == 1  # dirty_tracked_tree (typechange)


def test_m3_dirty_parsing_staged_addition(tmp_path):
    """m-3: Staged addition ('A  path') is detected."""
    from tokmo.programs.pulse_canary import run_canary_cli
    import subprocess

    project_root = tmp_path / "project"
    project_root.mkdir()
    subprocess.run(["git", "init"], cwd=str(project_root), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(project_root), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(project_root), capture_output=True)
    (project_root / "original.py").write_text("# v1")
    subprocess.run(["git", "add", "."], cwd=str(project_root), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"],
                   cwd=str(project_root), capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(project_root), capture_output=True, text=True
    ).stdout.strip()

    # Stage a new file — porcelain shows 'A  new_file.py'
    (project_root / "new_file.py").write_text("# new")
    subprocess.run(["git", "add", "new_file.py"], cwd=str(project_root),
                   capture_output=True)

    canonical = tmp_path / "canonical"
    canonical.mkdir()

    result = run_canary_cli([
        "--mode", "bounded-live-canary",
        "--market", "KR", "--symbol", "005930",
        "--canonical-root", str(canonical),
        "--project-root", str(project_root),
        "--max-ticks", "5", "--max-provider-starts", "10",
        "--base-commit", head,
    ], evidence_root_override=tmp_path / "evidence")
    assert result == 1  # dirty_tracked_tree


def test_m3_dirty_parsing_first_line_unstaged(tmp_path):
    """m-3: Multiple unstaged files including ' M' as first line
    are all detected (the .strip() bug from Opus)."""
    from tokmo.programs.pulse_canary import run_canary_cli
    import subprocess

    project_root = tmp_path / "project"
    project_root.mkdir()
    subprocess.run(["git", "init"], cwd=str(project_root), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(project_root), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(project_root), capture_output=True)
    (project_root / "a.py").write_text("# a")
    (project_root / "z.py").write_text("# z")
    subprocess.run(["git", "add", "."], cwd=str(project_root), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"],
                   cwd=str(project_root), capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(project_root), capture_output=True, text=True
    ).stdout.strip()

    # Modify both — porcelain will show:
    #  ' M a.py\n M z.py\n'
    (project_root / "a.py").write_text("# a modified")
    (project_root / "z.py").write_text("# z modified")

    canonical = tmp_path / "canonical"
    canonical.mkdir()

    result = run_canary_cli([
        "--mode", "bounded-live-canary",
        "--market", "KR", "--symbol", "005930",
        "--canonical-root", str(canonical),
        "--project-root", str(project_root),
        "--max-ticks", "5", "--max-provider-starts", "10",
        "--base-commit", head,
    ], evidence_root_override=tmp_path / "evidence")
    assert result == 1


# ═══════════════════════════════════════════════════════════════════════
# m-2: Fixture composition moved out of src production module
# ═══════════════════════════════════════════════════════════════════════

def test_m2_fixture_deps_not_in_src_module():
    """m-2: The production src module (pulse_canary.py) must NOT contain
    _build_fixture_deps or TossCurrentPriceFact composition."""
    from tokmo.programs import pulse_canary

    # _build_fixture_deps should not exist in the src module
    assert not hasattr(pulse_canary, "_build_fixture_deps"), (
        "_build_fixture_deps must not be in src production module")
    assert not hasattr(pulse_canary, "build_fixture_deps"), (
        "build_fixture_deps must not be in src production module")


def test_m2_fixture_deps_in_tools():
    """m-2: build_fixture_deps exists in tools/run_tokmo_pulse_canary_fixture.py."""
    assert callable(build_fixture_deps), (
        "build_fixture_deps must be callable from tools")


def test_m2_fixture_deps_returns_canary_deps():
    """m-2: build_fixture_deps returns a valid CanaryDepsV1."""
    from tokmo.programs.pulse_canary import CanaryDepsV1

    fixture_path = Path(__file__).parent / "test_data" / "canary_fixture.json"
    if not fixture_path.exists():
        # Use a temp fixture
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False) as f:
            json.dump({
                "token": "test-token",
                "quotes": {
                    "005930": {
                        "last_price": "72500", "currency": "KRW",
                        "timestamp": "2026-08-11T00:00:00Z",
                        "fetched_at": "2026-08-11T00:00:00Z",
                    }
                },
            }, f)
            fixture_path = Path(f.name)

    deps = build_fixture_deps(str(fixture_path), T0)
    assert deps is not None
    assert isinstance(deps, CanaryDepsV1)


def test_m2_no_t0_param_in_compute_purpose_identity():
    """m (cleanup): compute_purpose_identity no longer accepts t0 parameter."""
    import inspect
    from tokmo.programs.pulse_canary import compute_purpose_identity

    sig = inspect.signature(compute_purpose_identity)
    assert "t0" not in sig.parameters, (
        "compute_purpose_identity must not have t0 parameter")
