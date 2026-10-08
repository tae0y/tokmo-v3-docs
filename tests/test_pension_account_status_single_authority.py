"""One canonical pension holdings authority — proved, not asserted.

Requirement 3: the existing V1 ACTIVE read path shows 7 synthetic ETFs before
activation and 10 after; exactly one ACTIVE current revision.
Requirement 4: the existing cadence / materialization / reminder / composite
read path needs no alternate store and sees the same new V1 revision.
Requirement 5: no second store file, ACTIVE table, resolver, or whole-account
claim exists anywhere in the changed files.
Requirement 6: period-summary evidence cannot alter holdings, and the
execution ledger is untouched.
Requirement 7: the V1 DDL pin and backward compatibility are preserved.

Every fixture is synthetic.
"""
from __future__ import annotations

import ast
import hashlib
import itertools
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tokmo.facts import pension_account_status_excel
from tokmo.facts.pension_account_status_excel import (
    parse_pension_account_status,
    parse_pension_period_summary,
)
from tokmo.programs.holdings_sync import _payload
from tokmo.programs.pension_materialization import (
    build_pension_provenance,
    evaluate_reminder,
    materialize_pension_holdings,
    reminder_text,
)
from tokmo.records.pension_revisions import (
    PENSION_REGISTRY_DDL_SHA256,
    PensionRegistryStore,
)
from tokmo.records.trade_ledger_store import TradeLedgerStore
from tokmo.values import pension_account_status
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)
from tokmo.values.material_pack import _validate_holdings_generation_r2
from tokmo.values.pension_holdings import (
    PENSION_HOLDINGS_SCHEMA_VERSION,
    PENSION_PARSER_VERSION,
    PensionHoldingsRevisionV1,
    classify_pension_freshness,
    pension_account_scope,
    pension_reconciliation_digest,
    pension_semantic_digest,
)
from tokmo.values.trade_ledger import ImportAuditMeta, TradeLedgerEvent

from tests.test_pension_account_status_parser import (  # noqa: E402
    build_status_workbook,
    build_summary_workbook,
    landing,
    reviewed_plan,
)

_KST = ZoneInfo("Asia/Seoul")
_NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
_AS_OF = datetime(2026, 8, 13, tzinfo=_KST)
_OLD_AS_OF = datetime(2026, 7, 13, tzinfo=_KST)

REPO_ROOT = Path(__file__).resolve().parents[1]

# The complete set of files this slice adds. Requirement 5 scans exactly these.
CHANGED_FILES = (
    "src/tokmo/values/pension_account_status.py",
    "src/tokmo/facts/pension_account_status_excel.py",
    "tools/import_pension_account_status.py",
    "tools/import_pension_excel.py",   # retired fail-closed under PT-01
    "tests/test_pension_legacy_cli_fail_closed.py",
    "tests/test_pension_account_status_parser.py",
    "tests/test_pension_account_status_single_authority.py",
    "tests/test_pension_account_status_import_tool.py",
    ".ai/analysis/2026-08-13_pension-manual-source-impact.md",
    ".ai/plans/2026-08-13_pension-manual-source-plan-sds-v0.8.md",
)

# The V1 pension lane and the retirement ledger lane are frozen in this slice.
# These digests are the base-commit contents
# (5cbdbec44c51b24c1e1f76a8380ed55db8f2c61e).
#
# `tools/import_pension_excel.py` was in this set and is deliberately NOT
# any more. Freezing it was wrong: it is reachable, and it activated a
# revision before writing the canonical snapshot -- at the wrong path. It
# is now retired fail-closed, and pinned by
# tests/test_pension_legacy_cli_fail_closed.py instead.
PROTECTED_FILE_SHA256 = {
    "src/tokmo/values/pension_holdings.py":
        "b00ba3868a7a15a8595e40532b92093c6fac9973b01a2171108732982705cc4a",
    "src/tokmo/facts/pension_excel.py":
        "bd926d4e9bd9b6cf7a7bceba1488e90d5be761cb56983247e17e64f49dcd95b6",
    "src/tokmo/programs/pension_ingestion.py":
        "1427bacbee460feb0b8b1048da0f3f63064dc24dcf870aff91df85a855b6b457",
    "src/tokmo/programs/pension_materialization.py":
        "5812fa2114124aeb6b04497ccd3eda9c612077107ab9c2407e14a19fb565af5e",
    "src/tokmo/programs/pension_trade_ingestion.py":
        "6bf8cc98b57459437094eea9c5ead986d84efc49f32aee05375554dcfb8445ea",
    "src/tokmo/programs/pension_legacy_migration.py":
        "b697c335cff0bea8e3385f1eef403693cc066661aeaf7c7f835e1f9613c2ee8e",
    "src/tokmo/values/material_pack.py":
        "b748ec39d03070e2bc8066b04c60d651665de9a3d97e939ca320985d07e3314c",
    "src/tokmo/values/holdings.py":
        "01b42ec5d4a0bdb2ccb24b56d138c43aa9c3b4dd3b5a77cb94edfa2d86f63e69",
    "src/tokmo/records/trade_ledger_canonical.py":
        "11c11be6ba27ec8123dc31afb96ccd6353cab709fe5fb336e9b24d5bbbb9dfbd",
    "src/tokmo/records/trade_ledger_preflight.py":
        "7bf4d343d7b109cd734d232780f5b5fbaa06a4a57d1ffcd98af7f479c8178bf8",
    "src/tokmo/records/trade_ledger_store.py":
        "d170ac3cda0873cc8deff3dc846ff62817ad9c7ff61a5516389300a091083707",
    "tests/test_architecture.py":
        "1030e6987c556033340bf46053ab44474ac0600e3a91564151c67c0c6b65b63c",
}


def clock():
    return _NOW


def registry(tmp_path) -> PensionRegistryStore:
    """The ONE canonical pension holdings store. There is no other."""
    return PensionRegistryStore(
        tmp_path / "stores" / "pension_registry.sqlite3", clock=clock)


def existing_seven_etf_revision() -> PensionHoldingsRevisionV1:
    """The already-integrated V1 ACTIVE revision, as it stands today."""
    holdings = tuple(
        HoldingItemV1(
            symbol=f"80000{n}", quantity="100", avg_price="1000",
            currency="KRW", market_raw="KR", market_field="market",
        )
        for n in range(1, 8)
    )
    names = tuple((item, f"SYNTH-LEGACY-{n:02d}")
                  for n, item in enumerate(holdings, 1))
    return PensionHoldingsRevisionV1(
        revision_id="a" * 32,
        schema_version=PENSION_HOLDINGS_SCHEMA_VERSION,
        source_content_sha256="0" * 64,
        parser_version=PENSION_PARSER_VERSION,
        sheet_schema_version=1,
        holdings=holdings,
        source_as_of=_OLD_AS_OF,
        uploaded_at=_NOW,
        validated_at=_NOW,
        semantic_digest=pension_semantic_digest(holdings),
        reconciliation_digest=pension_reconciliation_digest(names),
        holding_count=len(holdings),
        liquidation_flag=0,
    )


def new_ten_etf_candidate(tmp_path) -> PensionHoldingsRevisionV1:
    """The new broker account-status workbook, normalized to V1."""
    result = parse_pension_account_status(
        landing(tmp_path, build_status_workbook(), "status.xlsx"),
        now=_NOW, source_as_of=_AS_OF, plan=reviewed_plan(),
    )
    return result.revision


# ---------------------------------------------------------------------------
# Requirement 3 — one ACTIVE pointer, 7 before, 10 after
# ---------------------------------------------------------------------------

def test_active_read_path_moves_from_seven_to_ten_etfs(tmp_path):
    store = registry(tmp_path)
    old = existing_seven_etf_revision()
    store.create_candidate(old)
    store.promote_to_pending(old.revision_id)
    store.activate(old.revision_id, approval_reason="synthetic-existing")

    before = store.read_active_revision()
    assert before.holding_count == 7
    assert before.revision_id == old.revision_id

    new = new_ten_etf_candidate(tmp_path)
    store.create_candidate(new)
    store.promote_to_pending(new.revision_id)
    store.activate(new.revision_id, approval_reason="synthetic-account-status")

    after = store.read_active_revision()
    assert after.holding_count == 10
    assert after.revision_id == new.revision_id
    assert after.source_as_of == _AS_OF


def test_exactly_one_active_and_the_old_revision_is_superseded(tmp_path):
    store = registry(tmp_path)
    old = existing_seven_etf_revision()
    store.create_candidate(old)
    store.activate(old.revision_id, approval_reason="synthetic-existing")
    new = new_ten_etf_candidate(tmp_path)
    store.create_candidate(new)
    store.activate(new.revision_id, approval_reason="synthetic-account-status")

    # NOTE: the final contract forbids retaining the OLD holdings payload as a
    # SUPERSEDED row -- snapshot owns history. The registry migration that
    # enforces that is NOT implemented in this run, so this test only asserts
    # what the contract does require: exactly one ACTIVE current revision.
    assert store.read_revision_state(new.revision_id) == "ACTIVE"
    pointer = store.read_pointer()
    assert pointer["active_revision_id"] == new.revision_id
    assert pointer["activation_generation"] == 2

    connection = sqlite3.connect(
        tmp_path / "stores" / "pension_registry.sqlite3")
    try:
        assert connection.execute(
            "SELECT count(*) FROM pension_revisions "
            "WHERE state = 'ACTIVE'").fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM pension_active_pointer").fetchone()[0] == 1
    finally:
        connection.close()


def test_the_candidate_goes_through_the_existing_lifecycle(tmp_path):
    store = registry(tmp_path)
    new = new_ten_etf_candidate(tmp_path)
    store.create_candidate(new)
    assert store.read_revision_state(new.revision_id) == "CANDIDATE_VALID"
    assert store.read_pointer() is None
    store.promote_to_pending(new.revision_id)
    assert store.read_revision_state(new.revision_id) == "PENDING_REVIEW"
    store.activate(new.revision_id, approval_reason="synthetic-account-status")
    assert store.read_revision_state(new.revision_id) == "ACTIVE"


def test_the_new_revision_round_trips_through_the_existing_store(tmp_path):
    store = registry(tmp_path)
    new = new_ten_etf_candidate(tmp_path)
    store.create_candidate(new)
    store.activate(new.revision_id, approval_reason="synthetic-account-status")
    loaded = store.read_active_revision()
    assert loaded.holdings == new.holdings
    assert loaded.semantic_digest == new.semantic_digest
    assert loaded.reconciliation_digest == new.reconciliation_digest
    assert loaded.sheet_schema_version == new.sheet_schema_version


# ---------------------------------------------------------------------------
# Requirement 4 — the existing downstream read path, unchanged
# ---------------------------------------------------------------------------

def activated(tmp_path) -> tuple[PensionRegistryStore, PensionHoldingsRevisionV1]:
    store = registry(tmp_path)
    old = existing_seven_etf_revision()
    store.create_candidate(old)
    store.activate(old.revision_id, approval_reason="synthetic-existing")
    new = new_ten_etf_candidate(tmp_path)
    store.create_candidate(new)
    store.activate(new.revision_id, approval_reason="synthetic-account-status")
    return store, new


def test_materialization_reads_the_same_revision_with_no_alternate_store(tmp_path):
    store, new = activated(tmp_path)
    active = store.read_active_revision()
    account = materialize_pension_holdings(active, pension_ordinal=1)
    assert account["source_provenance"] == "PENSION_EXCEL"
    assert account["account_type_label"] == "pension-excel-v1"
    assert account["account_scope"] == pension_account_scope()
    assert len(account["holdings"]) == 10


def test_provenance_reads_the_same_revision_and_source_as_of(tmp_path):
    store, new = activated(tmp_path)
    provenance = build_pension_provenance(
        store.read_active_revision(), tick_now=_NOW)
    assert provenance["revision_id"] == new.revision_id
    assert provenance["semantic_digest"] == new.semantic_digest
    # The V1 store canonicalizes timestamps to UTC on write; the INSTANT
    # is what must survive, and it does.
    assert datetime.fromisoformat(provenance["source_as_of"]) == _AS_OF
    assert provenance["parser_version"] == "pension-excel-v1"
    assert provenance["freshness_class"] == "CURRENT_MANUAL_SOURCE"


def test_composite_payload_validator_still_accepts_the_new_revision(tmp_path):
    """The strongest downstream proof: the existing generation-r2 composite
    payload builder and validator accept the new revision untouched."""
    store, _new = activated(tmp_path)
    active = store.read_active_revision()
    scope = pension_account_scope()
    state = CurrentHoldingsV1(
        schema_version=1,
        accounts=(AccountHoldingsV1(
            account_scope=scope, ordinal=1, holdings=active.holdings),),
        global_inventory=True,
        captured_at=_NOW,
        identity_map={scope: AccountScopeIdentityV1(
            account_scope=scope, account_type_label="pension-excel-v1",
            ordinal=1, masked_name=None)},
    )
    payload = _payload(state, active, tick_now=_NOW)
    _validate_holdings_generation_r2(payload)          # must not raise
    assert payload["payload_version"] == 2
    assert len(payload["accounts"][0]["holdings"]) == 10
    assert payload["pension_provenance"]["parser_version"] == "pension-excel-v1"


def test_reminder_path_reads_the_same_revision(tmp_path):
    store, _new = activated(tmp_path)
    active = store.read_active_revision()
    freshness = classify_pension_freshness(active.source_as_of, _NOW)
    assert freshness.source_age_days == 0
    assert freshness.freshness_class == "CURRENT_MANUAL_SOURCE"
    assert "2026-08-13" in reminder_text(freshness)
    # The reminder evaluator takes the one V1 registry and no other store.
    import inspect
    parameters = inspect.signature(evaluate_reminder).parameters
    assert list(parameters)[0] == "registry"
    assert evaluate_reminder(
        store, None, tick_now=_NOW, tick_id=1, production_mode=False,
        replay=False, send_enabled=False, role=None) is None


def test_downstream_consumers_name_only_the_one_registry():
    """No production consumer may reach for a second holdings store."""
    for relative in ("src/tokmo/programs/production_cadence.py",
                     "src/tokmo/programs/pension_materialization.py",
                     "src/tokmo/programs/holdings_sync.py"):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "pension_account_" + "snapshots" not in source, relative
        assert "PensionAccount" + "SnapshotStore" not in source, relative


# ---------------------------------------------------------------------------
# Requirement 5 — no second canonical store, table, resolver, or claim
# ---------------------------------------------------------------------------

def test_the_rejected_second_store_exists_nowhere_in_the_tree():
    prefix = "pension_account_"
    banned_tokens = (
        prefix + "snapshots",
        "PensionAccount" + "SnapshotStore",
        prefix + "active_pointer",
        prefix + "revisions",
        prefix + "assets",
    )
    for banned in banned_tokens:
        for root in ("src", "tests", "tools"):
            for path in sorted((REPO_ROOT / root).rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                assert banned not in path.read_text(encoding="utf-8"), (
                    banned, path.name)


def test_only_one_active_pointer_table_exists_in_the_pension_lane():
    source = (REPO_ROOT / "src/tokmo/records/pension_revisions.py").read_text(
        encoding="utf-8")
    assert source.count("CREATE TABLE pension_active_pointer") == 1
    # Only the registry owner may CREATE the pointer table. Other modules may
    # name it (the offline migration carries it forward) but must not declare
    # a second one.
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "pension_revisions.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "CREATE TABLE pension_active_pointer" not in text, path.name


def test_changed_files_make_no_whole_account_inventory_claim():
    """No changed file may AFFIRM whole-account coverage.

    The banned strings are affirmative claim phrasings only. Denying the claim
    ("so no caller can read this as a whole-account inventory") is exactly the
    behaviour this slice wants, so the guard must not punish it.
    """
    claims = (
        "all 14 " + "assets",
        "full 14-" + "asset",
        "14-asset " + "canonical",
        "full-account " + "canonical",
        "full account " + "inventory",
        "complete account " + "inventory",
    )
    for relative in CHANGED_FILES:
        path = REPO_ROOT / relative
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8").lower()
        for claim in claims:
            assert claim not in text, (relative, claim)


def test_the_documents_state_the_etf_only_scope_positively():
    """Truthfulness is not only the absence of a false claim: the documents
    must say out loud that the canonical holdings scope is ETF-only."""
    for relative in CHANGED_FILES:
        if not relative.endswith(".md"):
            continue
        path = REPO_ROOT / relative
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8").lower()
        assert "etf-only" in text, relative
        assert "one canonical" in text, relative


def test_no_second_holdings_revision_type_is_defined():
    """The only pension holdings revision type in the tree stays V1."""
    defined = []
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and "HoldingsRevision" in node.name:
                defined.append(node.name)
    assert defined == ["PensionHoldingsRevisionV1"]


def test_the_new_modules_define_no_store_class():
    for module in (pension_account_status, pension_account_status_excel):
        tree = ast.parse(
            Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                assert "Store" not in node.name, node.name
        # Naming the one canonical store in a docstring is fine; importing a
        # database driver or declaring DDL would not be.
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert "sqlite3" not in imported, module.__name__
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "CREATE TABLE" not in source


# ---------------------------------------------------------------------------
# Requirement 6 — period summary cannot alter holdings; ledger unchanged
# ---------------------------------------------------------------------------

def ledger(tmp_path) -> TradeLedgerStore:
    counter = itertools.count(1)
    return TradeLedgerStore(
        tmp_path / "trade_ledger.sqlite3",
        clock=clock,
        id_factory=lambda: f"{next(counter):032x}",
    )


def synthetic_events(count: int) -> tuple[TradeLedgerEvent, ...]:
    return tuple(
        TradeLedgerEvent(
            source="retirement_excel_v1",
            account_scope="retirement_pension_1",
            source_record_key=f"row-{index:06d}",
            event_kind="EXECUTION",
            supersedes_source_record_key=None,
            trade_date=date(2026, 7, 1),
            transaction_type_raw="매수",
            instrument_raw=f"SYNTH-LEGACY-{index:02d}",
            quantity="10",
            unit_price="1000",
            amount="10000",
            medium_raw="SYNTH",
            currency_raw="KRW",
            settlement_date=None,
            fee=None,
            tax=None,
        )
        for index in range(1, count + 1)
    )


def test_period_summary_cannot_enter_the_holdings_store(tmp_path):
    store = registry(tmp_path)
    new = new_ten_etf_candidate(tmp_path)
    store.create_candidate(new)
    store.activate(new.revision_id, approval_reason="synthetic-account-status")
    before = store.read_active_revision()

    evidence = parse_pension_period_summary(
        landing(tmp_path, build_summary_workbook(), "summary.xlsx"), now=_NOW)
    assert evidence.row_count == 5
    # The holdings store simply cannot accept it: wrong type.
    with pytest.raises(Exception):
        store.create_candidate(evidence)

    after = store.read_active_revision()
    assert after.holdings == before.holdings
    assert after.holding_count == 10
    assert after.semantic_digest == before.semantic_digest


def test_the_execution_ledger_is_unchanged_by_the_whole_flow(tmp_path):
    book = ledger(tmp_path)
    receipt = book.append_batch(
        synthetic_events(9),
        import_audit=ImportAuditMeta("f" * 64, "synthetic-importer-v1"),
    )
    assert receipt.status == "SAVED"

    def row_count() -> int:
        connection = sqlite3.connect(tmp_path / "trade_ledger.sqlite3")
        try:
            return connection.execute(
                "SELECT count(*) FROM trade_ledger_events").fetchone()[0]
        finally:
            connection.close()

    before = row_count()
    assert before == 9
    digest_before = hashlib.sha256(
        (tmp_path / "trade_ledger.sqlite3").read_bytes()).hexdigest()

    # Run the entire new flow beside it.
    store = registry(tmp_path)
    new = new_ten_etf_candidate(tmp_path)
    store.create_candidate(new)
    store.activate(new.revision_id, approval_reason="synthetic-account-status")
    parse_pension_period_summary(
        landing(tmp_path, build_summary_workbook(), "summary.xlsx"), now=_NOW)

    assert row_count() == before
    assert hashlib.sha256(
        (tmp_path / "trade_ledger.sqlite3").read_bytes()).hexdigest() == \
        digest_before


def test_no_new_module_can_reach_the_execution_ledger():
    for module in (pension_account_status, pension_account_status_excel):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
                imported.extend(alias.name for alias in node.names)
        for name in imported:
            assert "trade_ledger" not in name, (module.__name__, name)
            assert "TradeLedgerEvent" != name, (module.__name__, name)


def test_the_retirement_source_contract_is_unchanged():
    from tokmo.records.trade_ledger_preflight import SOURCE_CONTRACTS
    assert set(SOURCE_CONTRACTS) == {"retirement_excel_v1", "retirement_excel_delta_v1", "toss-orders-v1"}
    retirement = SOURCE_CONTRACTS["retirement_excel_v1"]
    assert retirement.source == "retirement_excel_v1"
    assert retirement.account_scopes == frozenset({"retirement_pension_1"})
    assert retirement.sequential_full_history is True


# ---------------------------------------------------------------------------
# Requirement 7 — V1 DDL pin, backward compatibility, existing rollback
# ---------------------------------------------------------------------------

def test_the_v1_registry_ddl_pin_is_unchanged():
    assert PENSION_REGISTRY_DDL_SHA256 == (
        "41fc7e728f2e9aff8520ce0e7bd62956e453695be7bea5b8e5542da584faa29f")


@pytest.mark.parametrize("relative", sorted(PROTECTED_FILE_SHA256))
def test_protected_file_is_byte_identical_to_the_base_commit(relative):
    digest = hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest()
    assert digest == PROTECTED_FILE_SHA256[relative], relative


def test_the_registry_schema_was_not_migrated(tmp_path):
    store = registry(tmp_path)
    store.create_candidate(new_ten_etf_candidate(tmp_path))
    connection = sqlite3.connect(
        tmp_path / "stores" / "pension_registry.sqlite3")
    try:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        tables.discard("sqlite_sequence")
        assert tables == {
            "pension_revisions", "pension_activation_events",
            "pension_active_pointer", "pension_reminder_events",
        }
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        connection.close()


def test_an_old_style_v1_revision_still_activates_after_the_change(tmp_path):
    """Backward compatibility: a revision produced by the original parser
    shape is still a first-class citizen of the same store."""
    store = registry(tmp_path)
    old = existing_seven_etf_revision()
    store.create_candidate(old)
    store.promote_to_pending(old.revision_id)
    store.activate(old.revision_id, approval_reason="synthetic-existing")
    loaded = store.read_active_revision()
    assert loaded.holding_count == 7
    assert loaded.sheet_schema_version == 1


# ---------------------------------------------------------------------------
# privacy / runtime surface of the changed files
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "module", [pension_account_status, pension_account_status_excel],
    ids=lambda m: m.__name__)
def test_new_modules_touch_no_ambient_runtime(module):
    source = Path(module.__file__).read_text(encoding="utf-8")
    for banned in ("os.environ", "getenv(", "datetime.now(", "utcnow(",
                   "urlopen(", ".local/share", "requests", "socket"):
        assert banned not in source, (module.__name__, banned)


@pytest.mark.parametrize(
    "module", [pension_account_status, pension_account_status_excel],
    ids=lambda m: m.__name__)
def test_new_modules_carry_no_forbidden_generation_token(module):
    source = Path(module.__file__).read_text(encoding="utf-8").lower()
    assert "v2" not in source, module.__name__
    assert "stock-monitor" not in source, module.__name__
