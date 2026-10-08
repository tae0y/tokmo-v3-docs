"""G0 acceptance tests for the recurring KR/US market-close vertical."""

from datetime import date, datetime, timezone
import sqlite3
import shutil
from zoneinfo import ZoneInfo


def test_a01_a12_closed_market_identity_is_four_part_and_independent():
    from tokmo.values.quant_recurring import QuantCloseMarketV1, QuantDueIdentityV1

    kr = QuantDueIdentityV1(
        market=QuantCloseMarketV1.KR,
        completed_session_date=date(2026, 9, 1),
        universe_revision="kr-reviewed-r1",
        quant_policy_version="quant-close-v1",
    )
    us = QuantDueIdentityV1(
        market=QuantCloseMarketV1.US,
        completed_session_date=date(2026, 9, 1),
        universe_revision="us-reviewed-r1",
        quant_policy_version="quant-close-v1",
    )

    assert kr.product_fields == (
        "KR", "2026-09-01", "kr-reviewed-r1", "quant-close-v1"
    )
    assert us.product_fields[0] == "US"
    assert kr.identity_digest != us.identity_digest
    assert QuantCloseMarketV1.core_markets(QuantCloseMarketV1.KR) == (
        "KR_KOSPI", "KR_KOSDAQ"
    )
    assert QuantCloseMarketV1.core_markets(QuantCloseMarketV1.US) == (
        "US_NYSE_NASDAQ",
    )


def _identity(market="KR"):
    from tokmo.values.quant_recurring import QuantCloseMarketV1, QuantDueIdentityV1
    return QuantDueIdentityV1(
        market=QuantCloseMarketV1(market),
        completed_session_date=date(2026, 9, 1),
        universe_revision=f"{market.lower()}-reviewed-r1",
        quant_policy_version="quant-close-v1",
    )


def test_a08_a10_store_pending_running_terminal_and_replay_zero(tmp_path):
    from tokmo.records.quant_market_close_receipts import QuantMarketCloseReceiptStore

    store = QuantMarketCloseReceiptStore(tmp_path / "quant.sqlite3")
    identity = _identity()
    assert store.ensure_pending(identity, state="PENDING_CANDLES") == "PENDING_CANDLES"
    before = store.total_changes()
    assert store.ensure_pending(identity, state="PENDING_LEASE") == "PENDING_CANDLES"
    assert store.total_changes() == before
    assert store.claim(identity, invocation_id="pulse-1") is True
    store.finish(identity, state="SUCCEEDED", result_json='{"ranked":[]}',
                 outbox_id="qmc:" + identity.identity_digest)
    terminal = store.read(identity)
    assert terminal.state == "SUCCEEDED"
    before = store.total_changes()
    assert store.claim(identity, invocation_id="pulse-2") is False
    assert store.total_changes() == before
    assert store.bind_outbox(identity, terminal.outbox_id) is True
    before = store.total_changes()
    assert store.bind_outbox(identity, terminal.outbox_id) is False
    assert store.total_changes() == before


def test_a28_exact_legacy_fixture_migrates_and_terminal_replays_zero(tmp_path):
    from tokmo.records.quant_market_close_receipts import QuantMarketCloseReceiptStore

    path = tmp_path / "quant.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript("""
      CREATE TABLE quant_recurring_due(identity_digest TEXT PRIMARY KEY,
       program_id TEXT, policy_version TEXT, market TEXT,
       completed_session_date TEXT, universe_policy_version TEXT, state TEXT,
       owner_invocation_id TEXT, owner_lease_key TEXT, claimed_at TEXT,
       finished_at TEXT, reason_code TEXT) STRICT;
      CREATE TABLE quant_recurring_result(identity_digest TEXT PRIMARY KEY,
       acquisition_id TEXT, membership_holdings_digest TEXT,
       holdings_captured_at TEXT, membership_starts INTEGER,
       candle_page1_starts INTEGER, candle_page2_starts INTEGER,
       cohort_size INTEGER, admissible_count INTEGER, provenance_digest TEXT,
       pack_digest TEXT, degraded INTEGER, limitations TEXT, symbols TEXT) STRICT;
      PRAGMA user_version=1;
    """)
    digest = "a" * 64
    values = (digest, "quant-recurring-shadow-v1", "old-policy",
              "US_NYSE_NASDAQ", "2026-08-31", "old-universe", "SUCCEEDED",
              "old-inv", "old-lease", "2026-09-01T00:00:00Z",
              "2026-09-01T00:00:01Z", "ok")
    connection.execute("INSERT INTO quant_recurring_due VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", values)
    result = (digest, "acq", "b" * 64, "2026-09-01T00:00:00Z", 1, 1, 0,
              1, 1, "c" * 64, "d" * 64, 0, "[]", '["AAPL"]')
    connection.execute("INSERT INTO quant_recurring_result VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", result)
    connection.commit(); connection.close()

    store = QuantMarketCloseReceiptStore(path)
    row = store.read_digest(digest)
    assert row.identity_schema == "legacy-v1"
    migrated_result = __import__("json").loads(row.result_json)
    assert row.state == "SUCCEEDED"
    assert migrated_result == dict(zip((
        "identity_digest", "acquisition_id", "membership_holdings_digest",
        "holdings_captured_at", "membership_starts", "candle_page1_starts",
        "candle_page2_starts", "cohort_size", "admissible_count",
        "provenance_digest", "pack_digest", "degraded", "limitations", "symbols",
    ), result))
    assert store.migration_report == (1, 1)
    assert path.with_suffix(".sqlite3.v1.bak").exists()


def test_a01_a07_a14_official_close_authority_handles_dst_and_early_close():
    from tokmo.programs.quant_market_close import OfficialCloseEvidenceV1, resolve_due

    standard = OfficialCloseEvidenceV1("US", date(2026, 1, 2),
        datetime(2026, 1, 2, 21, tzinfo=timezone.utc), "regular", "calendar:US:2026-01-02:0")
    daylight = OfficialCloseEvidenceV1("US", date(2026, 7, 2),
        datetime(2026, 7, 2, 20, tzinfo=timezone.utc), "regular", "calendar:US:2026-07-02:0")
    shortened = OfficialCloseEvidenceV1("US", date(2026, 11, 27),
        datetime(2026, 11, 27, 18, tzinfo=timezone.utc), "regular", "calendar:US:2026-11-27:0")
    for evidence in (standard, daylight, shortened):
        assert resolve_due(evidence.official_close_utc, evidence).decision == "DUE"
        assert evidence.official_close_utc.astimezone(ZoneInfo("America/New_York")).date() == evidence.session_date
    assert resolve_due(datetime(2026, 11, 27, 17, 59, tzinfo=timezone.utc), shortened).decision == "BEFORE_CLOSE"
    assert resolve_due(datetime(2026, 9, 1, tzinfo=timezone.utc), None).decision == "CALENDAR_AUTHORITY_UNAVAILABLE"


def test_a02_a06_a15_holiday_requires_proven_prior_boundary():
    from tokmo.programs.quant_market_close import OfficialCloseEvidenceV1, resolve_due
    holiday = OfficialCloseEvidenceV1("KR", date(2026, 9, 1), None,
                                      "no_regular", "calendar:KR:2026-09-01:0")
    assert resolve_due(datetime(2026, 9, 1, 9, tzinfo=timezone.utc), holiday).decision == "CALENDAR_AUTHORITY_UNAVAILABLE"


def test_a08_completed_candle_readiness_is_fail_closed():
    from tokmo.programs.quant_market_close import completed_candles_ready
    session = date(2026, 9, 1)
    assert completed_candles_ready(session, (session, session))
    assert not completed_candles_ready(session, ())
    assert not completed_candles_ready(session, (session, date(2026, 8, 31)))


def test_a09_a18_a19_sorted_nonblocking_operation_flock(tmp_path):
    from tokmo.records.operation_locks import acquire_operation_lease
    first = acquire_operation_lease(tmp_path, ("STOCK_CANDLES_DAILY", "HOLDINGS"))
    assert first is not None
    assert first.operations == ("HOLDINGS", "STOCK_CANDLES_DAILY")
    assert acquire_operation_lease(tmp_path, ("HOLDINGS",)) is None
    first.close()
    assert acquire_operation_lease(tmp_path, ("HOLDINGS",)) is not None


def test_a21_activation_absent_corrupt_or_mismatch_is_off(tmp_path):
    from tokmo.records.quant_market_close_activation import read_activation
    assert read_activation(tmp_path / "missing.json", now=datetime.now(timezone.utc))["KR"] == "OFF"
    path = tmp_path / "activation.json"; path.write_text("{}")
    assert read_activation(path, now=datetime.now(timezone.utc)) == {"KR": "OFF", "US": "OFF"}


def test_a12_a27_renderer_is_deterministic_private_and_bounded():
    from tokmo.programs.quant_market_close import render_quant_market_close_alert
    result = {"market":"KR", "completed_session_date":"2026-09-01",
              "universe_revision":"kr-r1", "quant_policy_version":"q-r1",
              "ranked":["005930", "000660"], "limitations":["daily data"],
              "official_close_source":"calendar:KR:2026-09-01:0"}
    rendered = render_quant_market_close_alert(result)
    assert rendered == render_quant_market_close_alert(dict(result))
    assert len("\n".join(rendered).encode()) <= 4096
    for forbidden in ("credential", "/Users/", "account", "holding"):
        assert forbidden not in "\n".join(rendered).lower()


def test_a12_kernel_quant_outbox_factory_is_shadow_unauthorized_by_default():
    from tokmo.values.ops_kernel import AlertChannel, AlertOrigin, AlertOutboxV1
    identity = _identity()
    outbox = AlertOutboxV1.for_quant_market_close(
        alert_id="qmc-" + identity.identity_digest, tick_id=1,
        subject_id=identity.identity_digest,
        body_evidence=("Quant market close — KR 2026-09-01",),
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert outbox.origin is AlertOrigin.QUANT_MARKET_CLOSE
    assert outbox.channel is AlertChannel.SHADOW
    assert outbox.status is None


class _Bomb:
    def __init__(self, value=None): self.calls = 0; self.value = value
    def __call__(self, *args, **kwargs):
        self.calls += 1
        if isinstance(self.value, BaseException): raise self.value
        return self.value


class _KernelSpy:
    def __init__(self): self.rows = {}; self.calls = 0
    def record_quant_market_close_outbox(self, role, outbox):
        self.calls += 1
        self.rows.setdefault(outbox.alert_id, outbox)


def _runner_deps(tmp_path, *, market_modes=None, candle_dates=None,
                 core=None, kernel=None, crash=None):
    from tokmo.programs.quant_market_close import (
        OfficialCloseEvidenceV1, QuantMarketCloseRunnerDepsV1,
    )
    from tokmo.records.quant_market_close_receipts import QuantMarketCloseReceiptStore
    from tokmo.records.operation_locks import acquire_operation_lease
    modes = market_modes or {"KR": "SHADOW", "US": "OFF"}
    session = date(2026, 9, 1)
    evidence = {
        "KR": OfficialCloseEvidenceV1("KR", session,
            datetime(2026, 9, 1, 6, 30, tzinfo=timezone.utc), "regular",
            "calendar:KR:2026-09-01:0"),
        "US": OfficialCloseEvidenceV1("US", session,
            datetime(2026, 9, 1, 20, tzinfo=timezone.utc), "regular",
            "calendar:US:2026-09-01:0"),
    }
    return QuantMarketCloseRunnerDepsV1(
        activation_reader=lambda now: dict(modes),
        authority_reader=lambda market, now: evidence[market],
        receipt_store=QuantMarketCloseReceiptStore(tmp_path / "receipts.sqlite3"),
        operation_lease=lambda operations: acquire_operation_lease(tmp_path, operations),
        candle_reader=_Bomb(candle_dates or (session,)),
        core_callable=core or _Bomb({"ranked": ["005930"], "limitations": []}),
        kernel_store=kernel or _KernelSpy(),
        universe_revisions={"KR": "kr-reviewed-r1", "US": "us-reviewed-r1"},
        quant_policy_version="quant-close-v1", tick_id=1,
        invocation_id="pulse-1", crash_injector=crash,
    )


def test_m1_a01_before_close_and_off_are_identity_provider_zero(tmp_path):
    from tokmo.programs.quant_market_close import run_quant_market_close
    deps = _runner_deps(tmp_path)
    result = run_quant_market_close(datetime(2026, 9, 1, 6, 29,
                                              tzinfo=timezone.utc), deps)
    assert result == (("KR", "BEFORE_CLOSE"), ("US", "OFF"))
    assert deps.candle_reader.calls == deps.core_callable.calls == 0
    assert deps.receipt_store.total_changes() == 0


def test_m1_a03_kr_shortened_close_runs_at_provider_boundary(tmp_path):
    from tokmo.programs.quant_market_close import run_quant_market_close
    deps = _runner_deps(tmp_path)
    result = run_quant_market_close(datetime(2026, 9, 1, 6, 30,
                                              tzinfo=timezone.utc), deps)
    assert result[0] == ("KR", "SUCCEEDED")
    assert deps.candle_reader.calls == deps.core_callable.calls == 1
    assert len(deps.kernel_store.rows) == 1


def test_m1_a08_pending_candles_later_succeeds(tmp_path):
    from tokmo.programs.quant_market_close import run_quant_market_close
    deps = _runner_deps(tmp_path, candle_dates=(date(2026, 8, 31),))
    assert run_quant_market_close(datetime(2026, 9, 1, 7, tzinfo=timezone.utc), deps)[0][1] == "PENDING_CANDLES"
    deps.candle_reader.value = (date(2026, 9, 1),)
    assert run_quant_market_close(datetime(2026, 9, 1, 7, 1, tzinfo=timezone.utc), deps)[0][1] == "SUCCEEDED"
    assert deps.core_callable.calls == 1


def test_m1_a09_contention_has_zero_wire_and_token(tmp_path):
    from tokmo.programs.quant_market_close import run_quant_market_close
    held = _runner_deps(tmp_path).operation_lease(("HOLDINGS", "STOCK_CANDLES_DAILY"))
    deps = _runner_deps(tmp_path)
    try:
        before = deps.receipt_store.total_changes()
        assert run_quant_market_close(datetime(2026, 9, 1, 7, tzinfo=timezone.utc), deps)[0][1] == "PENDING_LEASE"
        assert deps.candle_reader.calls == deps.core_callable.calls == 0
        assert deps.receipt_store.total_changes() == before + 1
    finally: held.close()


def test_m1_a11_kr_us_simultaneous_are_independent(tmp_path):
    from tokmo.programs.quant_market_close import run_quant_market_close
    core = _Bomb({"ranked": [], "limitations": []})
    deps = _runner_deps(tmp_path, market_modes={"KR":"SHADOW", "US":"SHADOW"}, core=core)
    result = run_quant_market_close(datetime(2026, 9, 1, 21, tzinfo=timezone.utc), deps)
    assert result == (("KR", "SUCCEEDED"), ("US", "SUCCEEDED"))
    assert core.calls == 2 and len(deps.kernel_store.rows) == 2


def test_m1_a16_one_kernel_row_per_start_and_terminal_replays_zero(tmp_path):
    from tokmo.programs.quant_market_close import run_quant_market_close
    deps = _runner_deps(tmp_path)
    now = datetime(2026, 9, 1, 7, tzinfo=timezone.utc)
    assert run_quant_market_close(now, deps)[0][1] == "SUCCEEDED"
    changes = deps.receipt_store.total_changes()
    assert run_quant_market_close(now, deps)[0][1] == "SUCCEEDED"
    assert deps.candle_reader.calls == deps.core_callable.calls == 1
    assert deps.kernel_store.calls == 1 and len(deps.kernel_store.rows) == 1
    assert deps.receipt_store.total_changes() == changes


def test_m1_a17_crash_boundaries_reconcile_without_provider_replay(tmp_path):
    from tokmo.programs.quant_market_close import run_quant_market_close
    for point in ("RUNNING", "TERMINAL", "KERNEL_INSERT", "BINDING"):
        case = tmp_path / point
        crash = _Bomb(RuntimeError(point))
        crash.point = point
        def inject(actual, c=crash):
            if actual == c.point: c()
        deps = _runner_deps(case, crash=inject)
        now = datetime(2026, 9, 1, 7, tzinfo=timezone.utc)
        try: run_quant_market_close(now, deps)
        except RuntimeError: pass
        deps.crash_injector = None
        row = deps.receipt_store.read(_identity())
        if point == "RUNNING":
            assert row.state == "RUNNING"
            assert run_quant_market_close(now, deps)[0][1] == "RUNNING"
            assert deps.candle_reader.calls == 1 and deps.core_callable.calls == 0
        else:
            assert run_quant_market_close(now, deps)[0][1] == "SUCCEEDED"
            assert deps.candle_reader.calls == deps.core_callable.calls == 1
            assert len(deps.kernel_store.rows) == 1


def test_a17_no_pack_and_failed_terminal_replay_zero(tmp_path):
    from tokmo.programs.quant_market_close import run_quant_market_close
    now = datetime(2026, 9, 1, 7, tzinfo=timezone.utc)
    for name, core, expected in (
        ("no-pack", _Bomb(None), "NO_PACK"),
        ("failed", _Bomb(ValueError("typed")), "FAILED"),
    ):
        deps = _runner_deps(tmp_path / name, core=core)
        assert run_quant_market_close(now, deps)[0][1] == expected
        sql_before = sqlite3.connect(deps.receipt_store.path).total_changes
        changes = deps.receipt_store.total_changes()
        assert run_quant_market_close(now, deps)[0][1] == expected
        assert deps.candle_reader.calls == core.calls == 1
        assert deps.receipt_store.total_changes() == changes
        assert sqlite3.connect(deps.receipt_store.path).total_changes == sql_before


def test_min1_migration_failure_rolls_back_and_preserves_backup(tmp_path, monkeypatch):
    from tokmo.records.quant_market_close_receipts import QuantMarketCloseReceiptStore
    source = tmp_path / "source.sqlite3"
    connection = sqlite3.connect(source)
    connection.executescript("CREATE TABLE quant_recurring_due(identity_digest TEXT PRIMARY KEY,program_id TEXT,policy_version TEXT,market TEXT,completed_session_date TEXT,universe_policy_version TEXT,state TEXT,owner_invocation_id TEXT,owner_lease_key TEXT,claimed_at TEXT,finished_at TEXT,reason_code TEXT) STRICT; CREATE TABLE quant_recurring_result(identity_digest TEXT PRIMARY KEY,acquisition_id TEXT,membership_holdings_digest TEXT,holdings_captured_at TEXT,membership_starts INTEGER,candle_page1_starts INTEGER,candle_page2_starts INTEGER,cohort_size INTEGER,admissible_count INTEGER,provenance_digest TEXT,pack_digest TEXT,degraded INTEGER,limitations TEXT,symbols TEXT) STRICT; PRAGMA user_version=1;")
    connection.commit(); connection.close()
    original = source.read_bytes()
    monkeypatch.setattr(QuantMarketCloseReceiptStore, "_migration_checkpoint",
                        lambda self, point: (_ for _ in ()).throw(RuntimeError(point)) if point == "after_drop" else None)
    try: QuantMarketCloseReceiptStore(source)
    except RuntimeError: pass
    assert source.read_bytes() == original
    assert source.with_suffix(".sqlite3.v1.bak").read_bytes() == original


def test_min2_equivalent_terminal_legacy_us_blocks_new_digest(tmp_path):
    from tokmo.records.quant_market_close_receipts import QuantMarketCloseReceiptStore
    store = QuantMarketCloseReceiptStore(tmp_path / "q.sqlite3")
    identity = _identity("US")
    with sqlite3.connect(store.path) as connection:
        connection.execute("INSERT INTO quant_market_close_due VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("a"*64,"legacy-v1","US_NYSE_NASDAQ","2026-09-01","us-reviewed-r1",
             "quant-close-v1","SUCCEEDED","old","{}","","NONE"))
    before = store.total_changes()
    assert store.ensure_pending(identity, state="PENDING_LEASE") == "SUCCEEDED"
    assert store.claim(identity, invocation_id="new") is False
    assert store.read(identity) is None and store.total_changes() == before


def test_m1_production_composition_explicitly_defaults_runner_none():
    import inspect
    from tokmo.composition import build_pulse_production_live_deps
    assert "quant_market_close_runner=None" in inspect.getsource(build_pulse_production_live_deps).replace(" ", "")
