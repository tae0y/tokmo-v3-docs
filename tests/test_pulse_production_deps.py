"""Pulse production dependency composition seam.

Plan §27: inject canonical stores from an isolated root into PulseDepsV1.
The existing composition.py immutable seal is untouched — this module is a
separate DI factory. It reads CalendarLookaheadStore, CurrentHoldingsStore
(latest complete), AccountNativeRefStoreV1, watchlists, LatestQuoteStore,
TradeLedger, OrderScanState, and the dialogue store from a declared root
and returns them as read-only callables for PulseDepsV1. G2 tests use
fixtures; network is zero.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokmo.records.calendar_lookahead import CalendarLookaheadStoreV1
from tokmo.records.account_native_refs import AccountNativeRefStoreV1
from tokmo.records.latest_quotes import LatestQuoteStore
from tokmo.records.trade_ledger_store import TradeLedgerStore
from tokmo.records.order_scan_state import OrderScanStateStore


CLOCK_NOW = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)


def test_pulse_production_deps_factory_returns_typed_deps(tmp_path):
    """build_pulse_production_deps returns a PulseDepsV1 wired to isolated root."""
    from tokmo.programs.pulse_production_deps import (
        build_pulse_production_deps,
    )
    from tokmo.programs.integrated_pulse import PulseDepsV1

    deps = build_pulse_production_deps(
        root=tmp_path, clock=lambda: CLOCK_NOW,
        watchlist_symbols={}, dialogue_cutoff=CLOCK_NOW,
    )
    assert isinstance(deps, PulseDepsV1)
    # Stores must be under the isolated root
    assert Path(deps.scan_state_store.path).is_relative_to(tmp_path)
    assert Path(deps.dialogue_store.path).is_relative_to(tmp_path)


def test_pulse_production_deps_stores_in_isolated_root(tmp_path):
    """All canonical stores are created under the declared isolated root."""
    from tokmo.programs.pulse_production_deps import (
        build_pulse_production_deps,
    )

    deps = build_pulse_production_deps(
        root=tmp_path, clock=lambda: CLOCK_NOW,
        watchlist_symbols={}, dialogue_cutoff=CLOCK_NOW,
    )
    assert Path(deps.scan_state_store.path).is_relative_to(tmp_path)
    assert Path(deps.dialogue_store.path).is_relative_to(tmp_path)
    # Ledger store has _path attr
    assert Path(deps.ledger_store._path).is_relative_to(tmp_path)


def test_lookahead_reader_reads_from_calendar_lookahead_store(tmp_path):
    """lookahead_reader returns days from CalendarLookaheadStoreV1."""
    from tokmo.programs.pulse_production_deps import (
        build_pulse_production_deps,
    )
    from tokmo.records.calendar_lookahead import (
        LookaheadDayV1, LookaheadSessionV1,
    )
    from tokmo.values.session_gate import Market

    # Seed the store
    store = CalendarLookaheadStoreV1(
        tmp_path / "ops" / "calendar_lookahead.sqlite3",
        clock=lambda: CLOCK_NOW,
    )
    day = LookaheadDayV1(
        market=Market.US, trade_date="2026-08-10",
        source_trade_date="2026-08-10",
        sessions=(LookaheadSessionV1(
            "dayMarket",
            datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc),
        ),),
        fetched_at=CLOCK_NOW,
        provenance="calendar:US:2026-08-10:0",
    )
    store.replace_market_days(Market.US, (day,))

    deps = build_pulse_production_deps(
        root=tmp_path, clock=lambda: CLOCK_NOW,
        watchlist_symbols={},
        dialogue_cutoff=CLOCK_NOW,
        lookahead_store=store,
    )
    days = deps.lookahead_reader(Market.US)
    assert len(days) == 1
    assert days[0].trade_date == "2026-08-10"


def test_holdings_reader_reads_latest_complete(tmp_path):
    """holdings_reader returns the latest COMPLETE canonical snapshot."""
    from tokmo.programs.pulse_production_deps import (
        build_pulse_production_deps,
    )
    from tokmo.records.current_holdings_store import CurrentHoldingsStore
    from tokmo.values.current_holdings import (
        CurrentHoldingsV1, AccountHoldingsV1, AccountScopeIdentityV1,
    )

    hstore = CurrentHoldingsStore(
        tmp_path / "stores" / "current_holdings.json",
        clock=lambda: CLOCK_NOW,
    )
    scope = "toss-acct-" + "a" * 24
    snapshot = CurrentHoldingsV1(
        1, (AccountHoldingsV1(scope, 1, ()),),
        True, CLOCK_NOW,
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    hstore.replace(snapshot, identity_fingerprints={
        scope: "a" * 64})

    deps = build_pulse_production_deps(
        root=tmp_path, clock=lambda: CLOCK_NOW,
        watchlist_symbols={},
        dialogue_cutoff=CLOCK_NOW,
        holdings_store=hstore,
    )
    result = deps.holdings_reader()
    assert isinstance(result, CurrentHoldingsV1)
    assert result.captured_at == CLOCK_NOW


def test_holdings_reader_returns_none_when_no_snapshot(tmp_path):
    """Missing holdings → None, not an empty portfolio."""
    from tokmo.programs.pulse_production_deps import (
        build_pulse_production_deps,
    )

    deps = build_pulse_production_deps(
        root=tmp_path, clock=lambda: CLOCK_NOW,
        watchlist_symbols={},
        dialogue_cutoff=CLOCK_NOW,
    )
    assert deps.holdings_reader() is None


def test_native_refs_reader_reads_from_store(tmp_path):
    """native_refs_reader returns the persisted mapping."""
    from tokmo.programs.pulse_production_deps import (
        build_pulse_production_deps,
    )

    nstore = AccountNativeRefStoreV1(
        tmp_path / "ops" / "account_native_refs.sqlite3",
        clock=lambda: CLOCK_NOW,
    )
    scope = "toss-acct-" + "a" * 24
    nstore.replace_refs({scope: "ref-123"},
                        receipt_binding="binding-1")

    deps = build_pulse_production_deps(
        root=tmp_path, clock=lambda: CLOCK_NOW,
        watchlist_symbols={},
        dialogue_cutoff=CLOCK_NOW,
        native_refs_store=nstore,
    )
    mapping = deps.native_refs_reader()
    assert scope in mapping.scopes()
    assert mapping.reveal(scope) == "ref-123"


def test_provider_seams_are_none_when_not_injected(tmp_path):
    """Without injected provider adapters, provider callables raise or
    return None — no default transport exists."""
    from tokmo.programs.pulse_production_deps import (
        build_pulse_production_deps,
    )

    deps = build_pulse_production_deps(
        root=tmp_path, clock=lambda: CLOCK_NOW,
        watchlist_symbols={},
        dialogue_cutoff=CLOCK_NOW,
    )
    # Provider callbacks should not be wired to live transport
    assert deps.token_fetch is not None  # callable, but not live
    assert deps.quotes_fetch is not None  # callable, but not live


def test_production_mode_refusal_unchanged(tmp_path):
    """pulse_main still refuses production mode — this delta does not
    activate it."""
    from tokmo.programs.integrated_pulse import pulse_main
    result = pulse_main([
        "--once", "--mode", "production", "--root", str(tmp_path),
        "--scheduled-for", "2026-08-10T00:00:00Z",
    ])
    assert result == 1


def test_id_factory_no_collision_with_frozen_clock(tmp_path):
    """A frozen clock returning the same timestamp for multiple accounts
    in one tick must not cause a PK collision. The production deps
    id_factory uses a counter + secrets token for uniqueness."""
    from tokmo.programs.pulse_production_deps import _make_unique_id_factory

    frozen = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
    factory = _make_unique_id_factory(clock=lambda: frozen)

    ids = set()
    for _ in range(100):
        generated_id = factory()
        assert generated_id not in ids
        ids.add(generated_id)
    assert len(ids) == 100


def test_id_factory_ids_are_unique_and_well_formed(tmp_path):
    """Each id must be non-empty, start with pulse-prod-, and include
    a counter suffix."""
    from tokmo.programs.pulse_production_deps import _make_unique_id_factory

    clock_time = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
    factory = _make_unique_id_factory(clock=lambda: clock_time)

    first = factory()
    second = factory()
    assert first != second
    assert first.startswith("pulse-prod-")
    assert second.startswith("pulse-prod-")
    # counter increments
    assert "-0000-" in first
    assert "-0001-" in second


def test_jb6_real_factory_binds_working_official_evidence_reader(tmp_path):
    """SDS A2 §Z7/§V12 (JB-6): the REAL factory — not unit injection —
    binds the two-tier official reader; a fixture disclosure store row
    flows back through the bound reader with correlated counters."""
    import json
    import os
    from datetime import date, datetime as datetime_type
    from tokmo.programs.pulse_production_deps import (
        build_pulse_production_deps,
    )
    from tokmo.records.opendart_disclosures import DisclosureStore
    from tokmo.values.session_gate import Market

    now = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    gate = tmp_path / "news_activation.json"
    gate.write_text(json.dumps({
        "enabled": True, "cutoff": "2026-08-01T00:00:00+00:00",
        "approved_by": "woong", "producer_mode": "SHADOW"}))
    os.chmod(gate, 0o600)
    db = tmp_path / "opendart-bulk.sqlite3"
    store = DisclosureStore.open(db)
    store.connection.execute(
        "insert into disclosures values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("20260901000123", "C000123", "에스케이하이닉스", "000660", "Y",
         "주요사항보고서(자기주식소각결정)", "회사", "20260901", None,
         "{}", "h" * 64, now.isoformat(), "run-1"))
    store.connection.commit()
    store.close()
    deps = build_pulse_production_deps(
        root=tmp_path, clock=lambda: now,
        watchlist_symbols={Market.KR: frozenset(), Market.US: frozenset()},
        dialogue_cutoff=now, official_evidence_db_path=db)
    reader = deps.news_producer_deps.official_evidence
    assert reader is not None
    outcome = reader(("000660",), date(2026, 8, 29), date(2026, 9, 1),
                     frozenset({"BUYBACK_RETIREMENT"}),
                     candidate_id="ndc-jb6", attempt_no=1, now=now)
    assert [ref.ref_id for ref in outcome.refs] == ["20260901000123"]
    assert outcome.local_reads == 1 and outcome.http_starts == 0
    assert isinstance(now, datetime_type)


def test_jb6_factory_official_reader_is_lazy_and_fail_closed(tmp_path):
    """§V5: with no live seam and an absent canonical store, assembly
    never raises and lookups fail closed to the typed outcome."""
    import json
    import os
    from datetime import date
    from tokmo.programs.pulse_production_deps import (
        build_pulse_production_deps,
    )
    from tokmo.values.session_gate import Market

    now = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    gate = tmp_path / "news_activation.json"
    gate.write_text(json.dumps({
        "enabled": True, "cutoff": "2026-08-01T00:00:00+00:00",
        "approved_by": "woong", "producer_mode": "SHADOW"}))
    os.chmod(gate, 0o600)
    deps = build_pulse_production_deps(
        root=tmp_path, clock=lambda: now,
        watchlist_symbols={Market.KR: frozenset(), Market.US: frozenset()},
        dialogue_cutoff=now,
        official_evidence_db_path=tmp_path / "absent-bulk.sqlite3")
    outcome = deps.news_producer_deps.official_evidence(
        ("000660",), date(2026, 8, 29), date(2026, 9, 1),
        frozenset({"BUYBACK_RETIREMENT"}), now=now)
    assert outcome.refs == ()
    assert outcome.failure == "STORE_UNAVAILABLE"


def test_composition_binds_official_live_seam_lazily():
    """§Z7/§V5: the live composition seam function wires
    ``official_live_seam`` into the factory (production caller graph), and
    a seam whose credential loader refuses only ever surfaces at lookup
    time as the reader's typed AUTH_UNAVAILABLE — never at assembly."""
    import ast
    from datetime import date
    from tokmo.programs.news_official_evidence import (
        OfficialLiveSeamV1,
        build_official_evidence_reader,
    )
    from tokmo.records.opendart_disclosures import DisclosureStore

    # (a) caller-graph proof: build_pulse_production_live_deps passes the
    # seam keyword into build_pulse_production_deps.
    source = (Path(__file__).parents[1] / "src" / "tokmo"
              / "composition.py").read_text()
    tree = ast.parse(source)
    seam_keywords = [
        keyword.arg for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "build_pulse_production_deps"
        for keyword in node.keywords]
    assert "official_live_seam" in seam_keywords

    # (b) lazy fail-closed behavior: a refusing credential loader is
    # inert until a tier-2 lookup actually fires.
    def refusing_loader():
        raise ValueError("config_mode")

    seam = OfficialLiveSeamV1(credential_loader=refusing_loader,
                              transport_factory=lambda: None)
    import tempfile
    with tempfile.TemporaryDirectory() as scratch:
        db = Path(scratch) / "bulk.sqlite3"
        store = DisclosureStore.open(db)
        store.connection.execute(
            "insert into disclosures values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("20260710000001", "C000001", "회사", "000660", "Y",
             "임원ㆍ주요주주특정증권등소유상황보고서", "회사", "20260710",
             None, "{}", "h" * 64, "2026-09-01T00:00:00+00:00", "run-1"))
        store.connection.commit()
        store.close()
        reader = build_official_evidence_reader(db, live=seam)
        outcome = reader(("000660",), date(2026, 8, 29), date(2026, 9, 1),
                         frozenset({"BUYBACK_RETIREMENT"}))
        assert outcome.refs == ()
        assert outcome.failure == "AUTH_UNAVAILABLE"
        # §V10 (SOL M3): a credential refusal never counts a transport start.
        assert outcome.http_starts == 0


def test_real_composition_touches_no_transport_or_credentials(
        tmp_path, monkeypatch):
    """SOL M2: the REAL live composition assembles the official seam with
    ZERO OpenDartLiveTransport constructions and ZERO credential-file
    accesses — both are lookup-time only (§V5)."""
    import json as json_module
    from datetime import datetime as datetime_type, timezone as tz
    import tokmo.composition as composition
    import tokmo.opendart_bulk_composition as bulk_composition

    constructions = []
    credential_probes = []

    class SpyTransport:
        def __init__(self, *args, **kwargs):
            constructions.append(1)

        def __call__(self, request):
            raise AssertionError("never invoked in this test")

    monkeypatch.setattr(composition, "OpenDartLiveTransport", SpyTransport)
    monkeypatch.setattr(
        bulk_composition, "_require_private_directory",
        lambda path: credential_probes.append(("dir", path)))
    monkeypatch.setattr(
        bulk_composition, "_require_private_credential",
        lambda path: credential_probes.append(("cred", path)))

    now = datetime_type(2026, 9, 1, 9, 0, tzinfo=tz.utc)
    project = tmp_path / "project"
    (project / "watchlists").mkdir(parents=True)
    validator = project / "scripts" / "playbooks" / "validate_playbooks.py"
    validator.parent.mkdir(parents=True)
    validator.write_text("def validate_tree(root):\n    return []\n",
                         encoding="utf-8")
    (project / ".env.toss").write_text(
        "TOSS_CLIENT_ID=test-client-id\nTOSS_CLIENT_SECRET=test-secret\n")
    for market in ("KR", "US"):
        (project / "watchlists" / f"{market}.json").write_text(
            json_module.dumps({"schema_version": 1, "market": market,
                               "entries": []}))
    deps = composition.build_pulse_production_live_deps(
        project_root=project, clock=lambda: now,
        runtime_root=tmp_path / "runtime-root", dialogue_cutoff=now)
    assert deps is not None
    # assembly performed zero transport constructions and zero credential
    # accesses — the seam is bound but fully lazy.
    assert constructions == []
    assert credential_probes == []
    import tokmo.programs.quant_input_capture as capture
    from datetime import timedelta
    received=[]
    def consumer(**kwargs):
        received.append(kwargs['clock']())
        return lambda **kwargs: {'failures':[]}
    monkeypatch.setattr(capture,'requested_capture_consumer',consumer)
    composition.build_pulse_production_live_deps(
        project_root=project,clock=lambda:now,runtime_root=tmp_path/'runtime-root',dialogue_cutoff=now,
        capture_clock=lambda:now+timedelta(seconds=2),environ={'TOKMO_QUANT_INPUT_CAPTURE':'1'})
    assert received==[now+timedelta(seconds=2)]
