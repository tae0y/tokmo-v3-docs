"""SEC targeted operation seam tests.

Operation seam for ``run_sec_targeted``: cron-facing composition function
and one-shot CLI script.  Covers:

- scheduled slot derivation (KST ``10,40 * * * *``) boundary tests
- credential presence/mode/symlink validation
- canonical snapshot path injection (no arbitrary CLI path)
- default transport monkeypatched / no network
- same-slot duplicate overlap
- success silence (stdout+stderr both empty)
- complete_with_unresolved / failure sanitized nonzero
- secret/symbol/ticker/CIK not emitted
- exactly one operation call
- architecture: no order/AI/Telegram/OpenDART
"""

import ast
import json
import os
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tokmo.composition import (
    SecTargetedRunResult,
    _sec_scheduled_slot_kst,
    run_sec_targeted,
    run_sec_targeted_operation,
    setup_sec_targeted,
)
from tokmo.facts.sec import _SecTransportSuccess

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src" / "tokmo"

NOW = datetime(2026, 8, 3, 12, 10, 0, tzinfo=UTC)
UA_SECRET = "TokmoTest/0.0 (fixture-contact@example.invalid)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_env(home: Path, user_agent: str = UA_SECRET) -> Path:
    config_dir = home / ".config" / "tokmo-v3"
    config_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config_dir, 0o700)
    env_path = config_dir / "sec.env"
    env_path.write_text(f"SEC_USER_AGENT={user_agent}\n")
    os.chmod(env_path, 0o600)
    return env_path


def _make_snapshot(home: Path) -> Path:
    """Write a minimal COMPLETE holdings snapshot with one US holding."""
    from tokmo.records.holdings_snapshots import HoldingsSnapshotStore
    from tokmo.values import (
        TOSS_SOURCE,
        CompleteHoldingsSnapshot,
        HoldingsSnapshotAccount,
        TossHoldingFact,
    )

    snap_path = (
        Path(home)
        / ".local"
        / "share"
        / "tokmo-v3"
        / "toss"
        / "holdings_snapshots.sqlite3"
    )
    snap_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(snap_path.parent, 0o700)

    holding = TossHoldingFact(
        symbol="AAA",
        quantity="1",
        avg_price="1",
        avg_price_field="avgPrice",
        currency="USD",
        market_raw="US",
        market_field="market_raw",
        name=None,
        asset_type=None,
    )
    from tokmo.records.holdings_snapshots import snapshot_content_hash

    snap = CompleteHoldingsSnapshot(
        snapshot_id="16fd27068baf433b82eb8c7fada847da",
        source=TOSS_SOURCE,
        captured_at=NOW,
        discovered_account_count=1,
        accounts=(HoldingsSnapshotAccount(1, (holding,)),),
        content_hash="0" * 64,
    )
    snap = CompleteHoldingsSnapshot(
        snapshot_id="16fd27068baf433b82eb8c7fada847da",
        source=TOSS_SOURCE,
        captured_at=NOW,
        discovered_account_count=1,
        accounts=(HoldingsSnapshotAccount(1, (holding,)),),
        content_hash=snapshot_content_hash(snap),
    )
    from tokmo.records.holdings_persistence import authorize_user_request

    permit = authorize_user_request(
        snapshot_id="16fd27068baf433b82eb8c7fada847da",
        clock=lambda: NOW,
    )
    store = HoldingsSnapshotStore(snap_path, clock=lambda: NOW)
    store.append(snap, permit)
    return snap_path


def _tickers_body(entries):
    payload = {}
    for i, (cik, ticker, title) in enumerate(entries):
        payload[str(i)] = {"cik_str": cik, "ticker": ticker, "title": title}
    return json.dumps(payload).encode()


def _submissions_body(cik, filings):
    recent = {
        "accessionNumber": [],
        "filingDate": [],
        "reportDate": [],
        "form": [],
        "primaryDocument": [],
        "isXBRL": [],
    }
    for acc, fdate, rdate, form, doc, xbrl in filings:
        recent["accessionNumber"].append(acc)
        recent["filingDate"].append(fdate)
        recent["reportDate"].append(rdate)
        recent["form"].append(form)
        recent["primaryDocument"].append(doc)
        recent["isXBRL"].append(xbrl)
    return json.dumps({"cik": str(int(cik)), "filings": {"recent": recent}}).encode()


def _ok_transport():
    """Transport returning one mapping + one filing."""
    tb = _tickers_body([(320193, "AAA", "Company A")])
    sb = _submissions_body(
        "0000320193",
        [
            ("0000320193-26-000077", "2026-08-03", "2026-07-01", "10-Q", "d.htm", 1),
        ],
    )

    def transport(request):
        if request.endpoint_id == "sec_company_tickers":
            return _SecTransportSuccess(tb)
        return _SecTransportSuccess(sb)

    return transport


# ---------------------------------------------------------------------------
# Slot derivation: _sec_scheduled_slot_kst
# ---------------------------------------------------------------------------


class TestScheduledSlotKst:
    def test_10_past_hour_maps_to_10(self):
        slot = _sec_scheduled_slot_kst(
            datetime(2026, 8, 3, 3, 10, 0, tzinfo=UTC)
        )  # 12:10 KST
        assert slot == "20260803T1210"

    def test_11_past_hour_still_10(self):
        slot = _sec_scheduled_slot_kst(
            datetime(2026, 8, 3, 3, 39, 0, tzinfo=UTC)
        )  # 12:39 KST
        assert slot == "20260803T1210"

    def test_40_past_hour_maps_to_40(self):
        slot = _sec_scheduled_slot_kst(
            datetime(2026, 8, 3, 3, 40, 0, tzinfo=UTC)
        )  # 12:40 KST
        assert slot == "20260803T1240"

    def test_41_past_still_40(self):
        slot = _sec_scheduled_slot_kst(
            datetime(2026, 8, 3, 3, 41, 0, tzinfo=UTC)
        )  # 12:41 KST
        assert slot == "20260803T1240"

    def test_09_past_maps_to_previous_hour_40(self):
        slot = _sec_scheduled_slot_kst(
            datetime(2026, 8, 3, 3, 9, 0, tzinfo=UTC)
        )  # 12:09 KST
        assert slot == "20260803T1140"

    def test_00_past_maps_to_previous_hour_40(self):
        slot = _sec_scheduled_slot_kst(
            datetime(2026, 8, 3, 3, 0, 0, tzinfo=UTC)
        )  # 12:00 KST
        assert slot == "20260803T1140"

    def test_midnight_boundary(self):
        """00:00 KST maps to previous day 23:40."""
        # 2026-08-03 15:00 UTC = 2026-08-04 00:00 KST
        slot = _sec_scheduled_slot_kst(datetime(2026, 8, 3, 15, 0, 0, tzinfo=UTC))
        assert slot == "20260803T2340"

    def test_midnight_10_past(self):
        """00:10 KST maps to 00:10 same day."""
        slot = _sec_scheduled_slot_kst(datetime(2026, 8, 3, 15, 10, 0, tzinfo=UTC))
        assert slot == "20260804T0010"

    def test_run_id_has_no_symbols_or_account(self):
        slot = _sec_scheduled_slot_kst(datetime(2026, 8, 3, 3, 10, 0, tzinfo=UTC))
        assert "$" not in slot
        assert "@" not in slot
        assert "/" not in slot
        assert "AAA" not in slot

    def test_naive_datetime_raises(self):
        with pytest.raises((ValueError, TypeError)):
            _sec_scheduled_slot_kst(datetime(2026, 8, 3, 12, 10, 0))


# ---------------------------------------------------------------------------
# Common autouse isolation: monkeypatch the canonical watchlist loader to
# return empty for pre-existing holdings-only (AAA) fixtures.  This isolates
# the operation from the real project watchlists/US.json which contains V.
# Tests that explicitly exercise watchlist coverage have their own patch.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_watchlist(monkeypatch):
    """Every test in this module defaults to empty watchlists so the AAA-only
    fake transport is sufficient.  Tests that need watchlist symbols override
    the patch themselves."""
    from tokmo.values.session_gate import Market

    monkeypatch.setattr(
        "tokmo.composition.load_watchlists",
        lambda _dir: {Market.KR: frozenset(), Market.US: frozenset()},
    )


# ---------------------------------------------------------------------------


class TestOperationCredentials:
    def test_missing_env_fails_closed(self, tmp_path):
        setup_sec_targeted(home=tmp_path)
        # No sec.env written
        result = run_sec_targeted_operation(
            home=tmp_path,
            clock=lambda: NOW,
            sleeper=lambda _: None,
            transport=_ok_transport(),
        )
        assert result.code == "failed_config"

    def test_symlink_env_rejected(self, tmp_path):
        setup_sec_targeted(home=tmp_path)
        config_dir = tmp_path / ".config" / "tokmo-v3"
        target = config_dir / "real.env"
        target.write_text(f"SEC_USER_AGENT={UA_SECRET}\n")
        os.chmod(target, 0o600)
        link = config_dir / "sec.env"
        link.symlink_to(target)
        result = run_sec_targeted_operation(
            home=tmp_path,
            clock=lambda: NOW,
            sleeper=lambda _: None,
            transport=_ok_transport(),
        )
        assert result.code == "failed_config"

    def test_wrong_mode_rejected(self, tmp_path):
        setup_sec_targeted(home=tmp_path)
        config_dir = tmp_path / ".config" / "tokmo-v3"
        env_path = config_dir / "sec.env"
        env_path.write_text(f"SEC_USER_AGENT={UA_SECRET}\n")
        os.chmod(env_path, 0o644)  # too open
        result = run_sec_targeted_operation(
            home=tmp_path,
            clock=lambda: NOW,
            sleeper=lambda _: None,
            transport=_ok_transport(),
        )
        assert result.code == "failed_config"


# ---------------------------------------------------------------------------
# Operation: canonical path injection (no arbitrary CLI path)
# ---------------------------------------------------------------------------


class TestOperationCanonicalPath:
    def test_uses_holdings_snapshot_store_path(self, tmp_path, monkeypatch):
        """Operation must use _holdings_snapshot_store_path, not a CLI arg."""
        setup_sec_targeted(home=tmp_path)
        _make_env(tmp_path)
        _make_snapshot(tmp_path)

        calls = []
        original = run_sec_targeted

        def spy(**kwargs):
            calls.append(kwargs)
            return original(**kwargs)

        monkeypatch.setattr("tokmo.composition.run_sec_targeted", spy)

        result = run_sec_targeted_operation(
            home=tmp_path,
            clock=lambda: NOW,
            sleeper=lambda _: None,
            transport=_ok_transport(),
        )
        assert result.code == "complete"
        assert len(calls) == 1
        # snapshot_store_path must come from _holdings_snapshot_store_path
        from tokmo.composition import _holdings_snapshot_store_path

        expected = _holdings_snapshot_store_path(tmp_path)
        assert calls[0]["snapshot_store_path"] == expected


# ---------------------------------------------------------------------------
# Operation: default transport monkeypatched / no network
# ---------------------------------------------------------------------------


class TestOperationDefaultTransport:
    def test_default_transport_no_network(self, tmp_path, monkeypatch):
        """When transport=None, SecLiveTransport is used but monkeypatched."""
        setup_sec_targeted(home=tmp_path)
        _make_env(tmp_path)
        _make_snapshot(tmp_path)

        # Monkeypatch SecLiveTransport to return our fake
        fake = _ok_transport()

        class FakeLive:
            def __call__(self, request):
                return fake(request)

        monkeypatch.setattr("tokmo.composition.SecLiveTransport", lambda: FakeLive())

        result = run_sec_targeted_operation(
            home=tmp_path,
            clock=lambda: NOW,
            sleeper=lambda _: None,
            transport=None,
        )
        assert result.code == "complete"


# ---------------------------------------------------------------------------
# Operation: same-slot duplicate overlap
# ---------------------------------------------------------------------------


class TestOperationOverlap:
    def test_same_slot_duplicate_gets_overlap(self, tmp_path):
        setup_sec_targeted(home=tmp_path)
        _make_env(tmp_path)
        _make_snapshot(tmp_path)

        clock = lambda: NOW
        sleeper = lambda _: None
        transport = _ok_transport()

        r1 = run_sec_targeted_operation(
            home=tmp_path, clock=clock, sleeper=sleeper, transport=transport
        )
        assert r1.code == "complete"

        # Same slot -> overlap
        r2 = run_sec_targeted_operation(
            home=tmp_path, clock=clock, sleeper=sleeper, transport=transport
        )
        assert r2.code == "overlap"


# ---------------------------------------------------------------------------
# Operation: success silence
# ---------------------------------------------------------------------------


class TestOperationSuccess:
    def test_success_returns_complete_result(self, tmp_path):
        setup_sec_targeted(home=tmp_path)
        _make_env(tmp_path)
        _make_snapshot(tmp_path)

        result = run_sec_targeted_operation(
            home=tmp_path,
            clock=lambda: NOW,
            sleeper=lambda _: None,
            transport=_ok_transport(),
        )
        assert isinstance(result, SecTargetedRunResult)
        assert result.code == "complete"
        assert result.filings_saved == 1

    def test_passes_real_sleeper(self, tmp_path):
        setup_sec_targeted(home=tmp_path)
        _make_env(tmp_path)
        _make_snapshot(tmp_path)

        sleeps = []
        result = run_sec_targeted_operation(
            home=tmp_path,
            clock=lambda: NOW,
            sleeper=sleeps.append,
            transport=_ok_transport(),
        )
        assert result.code == "complete"
        assert len(sleeps) == 1  # 1 pacing between registry + submissions


# ---------------------------------------------------------------------------
# Operation: unresolved / failure sanitized
# ---------------------------------------------------------------------------


class TestOperationSanitized:
    def test_unmapped_ticker_code(self, tmp_path):
        setup_sec_targeted(home=tmp_path)
        _make_env(tmp_path)
        _make_snapshot(tmp_path)

        # Registry returns no mapping for AAA
        tb = _tickers_body([(999999, "ZZZ", "Company Z")])

        def transport(request):
            if request.endpoint_id == "sec_company_tickers":
                return _SecTransportSuccess(tb)
            raise RuntimeError("must not reach submissions")

        result = run_sec_targeted_operation(
            home=tmp_path,
            clock=lambda: NOW,
            sleeper=lambda _: None,
            transport=transport,
        )
        assert result.code == "complete_with_unresolved"
        assert result.unresolved == ("AAA",)

    def test_config_failure_sanitized(self, tmp_path):
        setup_sec_targeted(home=tmp_path)
        # No env file -> failed_config, no exception
        result = run_sec_targeted_operation(
            home=tmp_path,
            clock=lambda: NOW,
            sleeper=lambda _: None,
            transport=_ok_transport(),
        )
        assert result.code == "failed_config"

    def test_result_never_contains_secret(self, tmp_path):
        setup_sec_targeted(home=tmp_path)
        _make_env(tmp_path, user_agent="SuperSecret/1.0 (leak@test.invalid)")
        _make_snapshot(tmp_path)

        result = run_sec_targeted_operation(
            home=tmp_path,
            clock=lambda: NOW,
            sleeper=lambda _: None,
            transport=_ok_transport(),
        )
        assert result.code == "complete"
        # The result dataclass must not carry the user agent
        assert "SuperSecret" not in repr(result)
        assert "leak@test.invalid" not in repr(result)


# ---------------------------------------------------------------------------
# Operation: architecture
# ---------------------------------------------------------------------------


class TestOperationArchitecture:
    def test_no_order_ai_telegram_opendart_imports(self):
        source = (SRC_ROOT / "composition.py").read_text()
        # Find run_sec_targeted_operation
        idx = source.index("def run_sec_targeted_operation(")
        next_def = source.index("\ndef ", idx + 10)
        func_body = source[idx:next_def]
        for forbidden in (
            "telegram",
            "openai",
            "anthropic",
            "aiogram",
            "ccxt",
            "alpaca_trade_api",
            "boto3",
            "opendart",
            "OpenDART",
        ):
            assert forbidden not in func_body, (
                f"run_sec_targeted_operation imports {forbidden}"
            )

    def test_no_arbitrary_snapshot_path_cli(self):
        """The operation function takes no snapshot_store_path parameter."""
        source = (SRC_ROOT / "composition.py").read_text()
        idx = source.index("def run_sec_targeted_operation(")
        func_body = source[idx:]
        end_markers = ["\ndef ", "\nif __name__"]
        end = len(func_body)
        for marker in end_markers:
            pos = func_body.find(marker, 1)
            if pos != -1:
                end = min(end, pos)
        func_body = func_body[:end]
        # snapshot_store_path must not be a function parameter
        tree = ast.parse(textwrap.dedent(func_body))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                params = [a.arg for a in node.args.args]
                params += [a.arg for a in node.args.posonlyargs]
                params += [a.arg for a in node.args.kwonlyargs]
                assert "snapshot_store_path" not in params, (
                    "snapshot_store_path must not be a parameter"
                )

    def test_no_custom_run_id_cli(self):
        source = (SRC_ROOT / "composition.py").read_text()
        idx = source.index("def run_sec_targeted_operation(")
        func_body = source[idx:]
        end_markers = ["\ndef ", "\nif __name__"]
        end = len(func_body)
        for marker in end_markers:
            pos = func_body.find(marker, 1)
            if pos != -1:
                end = min(end, pos)
        func_body = func_body[:end]
        # run_id is derived internally, never accepted as parameter
        tree = ast.parse(textwrap.dedent(func_body))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                params = [a.arg for a in node.args.args]
                params += [a.arg for a in node.args.posonlyargs]
                params += [a.arg for a in node.args.kwonlyargs]
                assert "run_id" not in params, "run_id must not be a parameter"

    def test_no_debug_output(self):
        source = (SRC_ROOT / "composition.py").read_text()
        idx = source.index("def run_sec_targeted_operation(")
        func_body = source[idx:]
        end_markers = ["\ndef ", "\nif __name__"]
        end = len(func_body)
        for marker in end_markers:
            pos = func_body.find(marker, 1)
            if pos != -1:
                end = min(end, pos)
        func_body = func_body[:end]
        assert "print(" not in func_body
        assert "logging" not in func_body


# ---------------------------------------------------------------------------
# CLI script: tools/run_sec_targeted_operation.py
# ---------------------------------------------------------------------------


class TestCliScript:
    def _setup_home(self, tmp_path):
        setup_sec_targeted(home=tmp_path)
        _make_env(tmp_path)
        _make_snapshot(tmp_path)

    def test_success_exit0_silent(self, tmp_path, monkeypatch, capsys):
        self._setup_home(tmp_path)

        monkeypatch.setattr(
            "tools.run_sec_targeted_operation.run_sec_targeted_operation",
            lambda **kw: SecTargetedRunResult(
                "complete",
                2,
                1,
                1,
                0,
                0,
                (),
            ),
        )
        from tools.run_sec_targeted_operation import main

        assert main(["--home", str(tmp_path)]) == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_unresolved_exit_nonzero_sanitized(self, tmp_path, monkeypatch, capsys):
        self._setup_home(tmp_path)

        monkeypatch.setattr(
            "tools.run_sec_targeted_operation.run_sec_targeted_operation",
            lambda **kw: SecTargetedRunResult(
                "complete_with_unresolved",
                2,
                1,
                1,
                0,
                0,
                ("SECRET_TICKER",),
            ),
        )
        from tools.run_sec_targeted_operation import main

        assert main(["--home", str(tmp_path)]) != 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "complete_with_unresolved" in captured.err
        assert "SECRET_TICKER" not in captured.err

    def test_failure_exit_nonzero_sanitized(self, tmp_path, monkeypatch, capsys):
        self._setup_home(tmp_path)

        monkeypatch.setattr(
            "tools.run_sec_targeted_operation.run_sec_targeted_operation",
            lambda **kw: SecTargetedRunResult(
                "failed_registry",
                1,
                5,
                0,
                0,
                0,
                (),
            ),
        )
        from tools.run_sec_targeted_operation import main

        assert main(["--home", str(tmp_path)]) != 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "failed_registry" in captured.err

    def test_exception_maps_to_sanitized_category(self, tmp_path, monkeypatch, capsys):
        self._setup_home(tmp_path)

        def boom(**kw):
            raise RuntimeError("internal trace with secret@example.com")

        monkeypatch.setattr(
            "tools.run_sec_targeted_operation.run_sec_targeted_operation",
            boom,
        )
        from tools.run_sec_targeted_operation import main

        assert main(["--home", str(tmp_path)]) != 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Traceback" not in captured.err
        assert "secret@example.com" not in captured.err
        assert "RuntimeError" not in captured.err
        assert "internal_error" in captured.err

    def test_overlap_exit_nonzero(self, tmp_path, monkeypatch, capsys):
        self._setup_home(tmp_path)

        monkeypatch.setattr(
            "tools.run_sec_targeted_operation.run_sec_targeted_operation",
            lambda **kw: SecTargetedRunResult(
                "overlap",
                0,
                0,
                0,
                0,
                0,
                (),
            ),
        )
        from tools.run_sec_targeted_operation import main

        assert main(["--home", str(tmp_path)]) != 0
        captured = capsys.readouterr()
        assert "overlap" in captured.err

    def test_sanitized_no_ticker_cik_name(self, tmp_path, monkeypatch, capsys):
        """stderr must never contain ticker, CIK, company name, or UA."""
        self._setup_home(tmp_path)

        monkeypatch.setattr(
            "tools.run_sec_targeted_operation.run_sec_targeted_operation",
            lambda **kw: SecTargetedRunResult(
                "complete_with_unresolved",
                1,
                1,
                0,
                0,
                0,
                ("AAPL",),
            ),
        )
        from tools.run_sec_targeted_operation import main

        assert main(["--home", str(tmp_path)]) != 0
        captured = capsys.readouterr()
        for forbidden in ("AAPL", "0000320193", "Company A", UA_SECRET):
            assert forbidden not in captured.err, f"stderr leaked: {forbidden}"
            assert forbidden not in captured.out, f"stdout leaked: {forbidden}"


# ---------------------------------------------------------------------------
# Canonical V watchlist operation: loader returns V, transport covers V
# ---------------------------------------------------------------------------


def _v_tickers_body():
    payload = {"0": {"cik_str": 1403161, "ticker": "V", "title": "Visa Inc."}}
    return json.dumps(payload).encode()


def _v_submissions_body():
    cik = "0001403161"
    recent = {
        "accessionNumber": ["0001403161-26-000010"],
        "filingDate": ["2026-08-03"],
        "reportDate": ["2026-06-30"],
        "form": ["10-Q"],
        "primaryDocument": ["v-10q.htm"],
        "isXBRL": [1],
    }
    return json.dumps({"cik": str(int(cik)), "filings": {"recent": recent}}).encode()


def _v_transport():
    """Transport that maps V and returns one filing for V."""
    tb = _v_tickers_body()
    sb = _v_submissions_body()

    calls = []

    def transport(request):
        calls.append(request.endpoint_id)
        if request.endpoint_id == "sec_company_tickers":
            return _SecTransportSuccess(tb)
        return _SecTransportSuccess(sb)

    transport.calls = calls
    return transport


class TestOperationWatchlistCanonicalV:
    """Explicit tests where the loader returns V and the fake registry/
    submissions cover V — the counterpart to the empty-watchlist isolation."""

    def test_v_watchlist_success_completes(self, tmp_path, monkeypatch):
        setup_sec_targeted(home=tmp_path)
        _make_env(tmp_path)
        # No snapshot: the operation falls through to watchlist-only path

        from tokmo.values.session_gate import Market

        monkeypatch.setattr(
            "tokmo.composition.load_watchlists",
            lambda _dir: {Market.KR: frozenset(), Market.US: frozenset({"V"})},
        )

        result = run_sec_targeted_operation(
            home=tmp_path,
            clock=lambda: NOW,
            sleeper=lambda _: None,
            transport=_v_transport(),
        )
        assert result.code == "complete"
        assert result.filings_saved == 1


class TestOperationMalformedWatchlist:
    """Malformed or missing watchlist returns failed_watchlist with zero
    transport calls."""

    def test_malformed_watchlist_returns_failed_watchlist(self, tmp_path, monkeypatch):
        setup_sec_targeted(home=tmp_path)
        _make_env(tmp_path)
        _make_snapshot(tmp_path)

        from tokmo.watchlists import WatchlistError

        monkeypatch.setattr(
            "tokmo.composition.load_watchlists",
            lambda _dir: (_ for _ in ()).throw(WatchlistError("registry_unavailable")),
        )

        calls = []

        def transport(request):
            calls.append(request.endpoint_id)
            return _SecTransportSuccess(b"{}")

        result = run_sec_targeted_operation(
            home=tmp_path,
            clock=lambda: NOW,
            sleeper=lambda _: None,
            transport=transport,
        )
        assert result.code == "failed_watchlist"
        assert len(calls) == 0

    def test_missing_watchlist_dir_returns_failed_watchlist(
        self, tmp_path, monkeypatch
    ):
        setup_sec_targeted(home=tmp_path)
        _make_env(tmp_path)
        _make_snapshot(tmp_path)

        from tokmo.watchlists import WatchlistError

        # Simulate missing directory → WatchlistError
        def raise_unavailable(_dir):
            raise WatchlistError("registry_unavailable")

        monkeypatch.setattr(
            "tokmo.composition.load_watchlists",
            raise_unavailable,
        )

        calls = []

        def transport(request):
            calls.append(request.endpoint_id)
            return _SecTransportSuccess(b"{}")

        result = run_sec_targeted_operation(
            home=tmp_path,
            clock=lambda: NOW,
            sleeper=lambda _: None,
            transport=transport,
        )
        assert result.code == "failed_watchlist"
        assert len(calls) == 0


# ---------------------------------------------------------------------------
# Production watchlist path: canonical dir only, no CLI override
# ---------------------------------------------------------------------------


class TestOperationCanonicalWatchlistPath:
    def test_operation_loads_project_root_watchlists_dir(self, tmp_path, monkeypatch):
        """The operation resolves watchlists from PROJECT_ROOT/watchlists
        and nowhere else."""
        setup_sec_targeted(home=tmp_path)
        _make_env(tmp_path)

        from tokmo import composition
        from tokmo.values.session_gate import Market

        seen_dirs = []

        def capture(directory):
            seen_dirs.append(Path(directory))
            return {Market.KR: frozenset(), Market.US: frozenset({"V"})}

        monkeypatch.setattr("tokmo.composition.load_watchlists", capture)

        result = run_sec_targeted_operation(
            home=tmp_path,
            clock=lambda: NOW,
            sleeper=lambda _: None,
            transport=_v_transport(),
        )
        assert result.code == "complete"
        assert seen_dirs == [composition.PROJECT_ROOT / "watchlists"]

    def test_operation_function_has_no_watchlist_parameter(self):
        import inspect

        from tokmo.composition import run_sec_targeted_operation as op

        params = set(inspect.signature(op).parameters)
        assert params == {"home", "clock", "sleeper", "transport"}
        assert not any("watchlist" in name for name in params)

    def test_cli_script_has_no_watchlist_argument(self):
        source = (PROJECT_ROOT / "tools" / "run_sec_targeted_operation.py").read_text()
        tree = ast.parse(source)
        arguments = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
            ):
                for arg in node.args:
                    if isinstance(arg, ast.Constant):
                        arguments.append(arg.value)
        assert arguments == ["--home"]
        assert "watchlist" not in source.lower()
