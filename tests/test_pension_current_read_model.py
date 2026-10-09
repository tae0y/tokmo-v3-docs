"""Current read model, import ordering, and ledger pre-snapshot protocols.

Proof C: any snapshot or read-back failure leaves current at 7 and the ledger
unchanged.
Proof D: success leaves current at exactly 10, exactly one current payload, and
zero historical payload rows; the old 7 survives only in snapshot history.
Proof E: rollback reconstructs current from a selected pension snapshot, with
no historical-payload resurrection.
Proof F lives in tests/test_pension_operational_tools.py, against the real
ledger, the real snapshot store and the real operator CLI.

Everything is synthetic and lives under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from tests.test_pension_snapshot_generation_migration import (
    generation_one_store,
    migrate_to_generation_two,
)
from tokmo.programs.pension_current_update import (
    BLOCKED_READBACK_MISMATCH,
    BLOCKED_SNAPSHOT_FAILED,
    OUTCOME_BLOCKED,
    OUTCOME_REPLACED,
    PensionSnapshotPortV1,
    SnapshotOutcomeV1,
    replace_current_holdings,
    rollback_current_from_snapshot,
)
from tokmo.records import pension_registry_migration as registry_migration
from tokmo.records.holdings_persistence import authorize_pension_import
from tokmo.records.holdings_snapshots import (
    PENSION_SNAPSHOT_SOURCE,
    HoldingsSnapshotStore,
    snapshot_content_hash,
)
from tokmo.records.pension_revisions import (
    EMPTY_HOLDINGS_PAYLOAD,
    SCHEMA_GENERATION_ONE,
    SCHEMA_GENERATION_TWO,
    PensionRegistryCorruptError,
    PensionRegistryStore,
)
from tokmo.values.current_holdings import HoldingItemV1
from tokmo.values.holdings import (
    CompleteHoldingsSnapshot,
    HoldingsSnapshotAccount,
    TossHoldingFact,
)
from tokmo.values.pension_holdings import (
    PENSION_HOLDINGS_SCHEMA_VERSION,
    PENSION_PARSER_VERSION,
    PensionHoldingsRevisionV1,
    pension_reconciliation_digest,
    pension_semantic_digest,
)

_KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
SEVEN_AS_OF = datetime(2026, 7, 13, tzinfo=_KST)
TEN_AS_OF = datetime(2026, 8, 13, tzinfo=_KST)


def clock():
    return NOW


# ---------------------------------------------------------------------------
# Synthetic revisions
# ---------------------------------------------------------------------------


def revision(symbols, *, revision_id, source_as_of):
    holdings = tuple(
        HoldingItemV1(
            symbol=symbol,
            quantity="100",
            avg_price="1000",
            currency="KRW",
            market_raw="KR",
            market_field="market",
        )
        for symbol in symbols
    )
    names = tuple((item, f"SYNTH-{item.symbol}") for item in holdings)
    return PensionHoldingsRevisionV1(
        revision_id=revision_id,
        schema_version=PENSION_HOLDINGS_SCHEMA_VERSION,
        source_content_sha256=hashlib.sha256(revision_id.encode()).hexdigest(),
        parser_version=PENSION_PARSER_VERSION,
        sheet_schema_version=1,
        holdings=holdings,
        source_as_of=source_as_of,
        uploaded_at=NOW,
        validated_at=NOW,
        semantic_digest=pension_semantic_digest(holdings),
        reconciliation_digest=pension_reconciliation_digest(names),
        holding_count=len(holdings),
        liquidation_flag=0,
    )


def seven():
    return revision(
        tuple(f"80000{n}" for n in range(1, 8)),
        revision_id="a" * 32,
        source_as_of=SEVEN_AS_OF,
    )


def ten():
    return revision(
        tuple(f"90000{n}" for n in range(1, 10)) + ("900010",),
        revision_id="b" * 32,
        source_as_of=TEN_AS_OF,
    )


# ---------------------------------------------------------------------------
# A migrated registry holding the current 7
# ---------------------------------------------------------------------------


def current_read_model_registry(tmp_path) -> PensionRegistryStore:
    path = tmp_path / "stores" / "pension_registry.sqlite3"
    store = PensionRegistryStore(path, clock=clock)
    old = seven()
    store.create_candidate(old)
    store.promote_to_pending(old.revision_id)
    store.activate(old.revision_id, approval_reason="synthetic-existing")
    assert store.schema_generation() == SCHEMA_GENERATION_ONE

    inventory = registry_migration.inspect_registry(path)
    sidecar = tmp_path / "sidecar" / "pension_registry.sqlite3"
    registry_migration.build_current_read_model_sidecar(path, sidecar)
    registry_migration.verify_current_read_model_sidecar(inventory, sidecar)
    registry_migration.install_current_read_model_sidecar(path, sidecar)

    migrated = PensionRegistryStore(path, clock=clock)
    assert migrated.schema_generation() == SCHEMA_GENERATION_TWO
    return migrated


def registry_rows(tmp_path):
    connection = sqlite3.connect(tmp_path / "stores" / "pension_registry.sqlite3")
    try:
        return connection.execute(
            "SELECT revision_id, state, holdings_json FROM pension_revisions "
            "ORDER BY revision_id"
        ).fetchall()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Contract 4: the migration itself
# ---------------------------------------------------------------------------


def test_migration_preserves_the_current_seven(tmp_path):
    store = current_read_model_registry(tmp_path)
    active = store.read_active_revision()
    assert active.holding_count == 7
    assert active.revision_id == "a" * 32
    assert active.source_as_of == SEVEN_AS_OF
    assert store.count_payload_bearing_rows() == 1
    assert store.count_superseded_payload_rows() == 0


def test_migration_preserves_audit_and_reminder_rows(tmp_path):
    path = tmp_path / "stores" / "pension_registry.sqlite3"
    store = PensionRegistryStore(path, clock=clock)
    old = seven()
    store.create_candidate(old)
    store.activate(old.revision_id, approval_reason="synthetic-existing")
    reminder_id = "c" * 64
    store.reserve_reminder(
        reminder_id, old.revision_id, 31, NOW, alert_id="synthetic-alert"
    )
    before = registry_migration.inspect_registry(path)

    sidecar = tmp_path / "sidecar" / "pension_registry.sqlite3"
    registry_migration.build_current_read_model_sidecar(path, sidecar)
    registry_migration.verify_current_read_model_sidecar(before, sidecar)
    registry_migration.install_current_read_model_sidecar(path, sidecar)

    migrated = PensionRegistryStore(path, clock=clock)
    assert migrated.read_reminder(reminder_id) is not None
    after = sqlite3.connect(path)
    try:
        assert (
            after.execute("SELECT count(*) FROM pension_activation_events").fetchone()[
                0
            ]
            == before.row_counts["pension_activation_events"]
        )
        assert after.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        after.close()


def test_migration_refuses_an_already_migrated_registry(tmp_path):
    current_read_model_registry(tmp_path)
    with pytest.raises(registry_migration.RegistryMigrationError) as error:
        registry_migration.build_current_read_model_sidecar(
            tmp_path / "stores" / "pension_registry.sqlite3",
            tmp_path / "second" / "pension_registry.sqlite3",
        )
    assert error.value.reason == "source_not_generation_one"


def test_migration_refuses_an_existing_sidecar(tmp_path):
    path = tmp_path / "stores" / "pension_registry.sqlite3"
    store = PensionRegistryStore(path, clock=clock)
    old = seven()
    store.create_candidate(old)
    store.activate(old.revision_id, approval_reason="synthetic")
    sidecar = tmp_path / "sidecar" / "pension_registry.sqlite3"
    registry_migration.build_current_read_model_sidecar(path, sidecar)
    with pytest.raises(registry_migration.RegistryMigrationError) as error:
        registry_migration.build_current_read_model_sidecar(path, sidecar)
    assert error.value.reason == "sidecar_already_exists"


def test_verification_detects_a_source_mutated_during_build(tmp_path):
    path = tmp_path / "stores" / "pension_registry.sqlite3"
    store = PensionRegistryStore(path, clock=clock)
    old = seven()
    store.create_candidate(old)
    store.activate(old.revision_id, approval_reason="synthetic")
    inventory = registry_migration.inspect_registry(path)
    sidecar = tmp_path / "sidecar" / "pension_registry.sqlite3"
    registry_migration.build_current_read_model_sidecar(path, sidecar)
    path.write_bytes(path.read_bytes() + b"\x00")
    with pytest.raises(registry_migration.RegistryMigrationError) as error:
        registry_migration.verify_current_read_model_sidecar(inventory, sidecar)
    assert error.value.reason == "source_mutated_during_build"


def test_an_unknown_registry_schema_fails_closed(tmp_path):
    store = current_read_model_registry(tmp_path)
    path = tmp_path / "stores" / "pension_registry.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE synthetic_extra(a TEXT) STRICT")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(PensionRegistryCorruptError):
        store.read_active_revision()


def test_generation_two_forbids_rollback_by_historical_payload(tmp_path):
    store = current_read_model_registry(tmp_path)
    with pytest.raises(PensionRegistryCorruptError) as error:
        store.rollback_to("a" * 32, approval_reason="synthetic")
    assert error.value.reason == "rollback_by_historical_payload_forbidden"


def test_the_database_check_rejects_a_historical_payload(tmp_path):
    """The invariant is enforced by the schema, not only by the code path."""
    current_read_model_registry(tmp_path)
    connection = sqlite3.connect(tmp_path / "stores" / "pension_registry.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE pension_revisions SET state='SUPERSEDED', "
                'holdings_json=\'[{"symbol":"900001"}]\' '
                "WHERE revision_id=?",
                ("a" * 32,),
            )
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Snapshot port wired to the real official store
# ---------------------------------------------------------------------------


def pension_snapshot(revision_value) -> CompleteHoldingsSnapshot:
    holdings = tuple(
        TossHoldingFact(
            symbol=item.symbol,
            quantity=item.quantity,
            avg_price=item.avg_price,
            avg_price_field="average_cost",
            currency=item.currency,
            market_raw=item.market_raw,
            market_field=item.market_field,
            name=None,
            asset_type=None,
        )
        for item in revision_value.holdings
    )
    accounts = (HoldingsSnapshotAccount(1, holdings),)
    draft = CompleteHoldingsSnapshot(
        uuid4().hex,
        PENSION_SNAPSHOT_SOURCE,
        revision_value.source_as_of,
        1,
        accounts,
        "0" * 64,
    )
    return CompleteHoldingsSnapshot(
        draft.snapshot_id,
        PENSION_SNAPSHOT_SOURCE,
        revision_value.source_as_of,
        1,
        accounts,
        snapshot_content_hash(draft),
    )


def real_snapshot_port(store: HoldingsSnapshotStore) -> PensionSnapshotPortV1:
    def capture(revision_value) -> SnapshotOutcomeV1:
        capture_value = pension_snapshot(revision_value)
        permit = authorize_pension_import(
            snapshot_id=capture_value.snapshot_id,
            program_name="tokmo-v3-pension-import-v1",
            clock=clock,
        )
        receipt = store.append(capture_value, permit)
        if receipt.status != "SAVED":
            return SnapshotOutcomeV1(
                False, capture_value.snapshot_id, BLOCKED_SNAPSHOT_FAILED
            )
        # Exact read-back: the stored capture must equal what we submitted.
        loaded = store.read_snapshot(capture_value.snapshot_id)
        if loaded != capture_value:
            return SnapshotOutcomeV1(
                False, capture_value.snapshot_id, BLOCKED_READBACK_MISMATCH
            )
        return SnapshotOutcomeV1(True, capture_value.snapshot_id)

    return PensionSnapshotPortV1(capture=capture)


def migrated_snapshot_store(tmp_path) -> HoldingsSnapshotStore:
    store, path = generation_one_store(tmp_path)
    migrate_to_generation_two(path, tmp_path)
    return store


# ---------------------------------------------------------------------------
# Contract 3 / proofs C and D
# ---------------------------------------------------------------------------


def test_import_order_is_current_snapshot_candidate_snapshot_then_replace(tmp_path):
    registry = current_read_model_registry(tmp_path)
    snapshots = migrated_snapshot_store(tmp_path)
    trace: list[str] = []
    receipt = replace_current_holdings(
        registry=registry,
        snapshot_port=real_snapshot_port(snapshots),
        current_revision=registry.read_active_revision(),
        candidate_revision=ten(),
        approval_reason="synthetic-import",
        trace=trace,
    )
    assert receipt.outcome == OUTCOME_REPLACED
    assert trace == ["capture_current", "capture_candidate", "replace_current"]


def test_success_leaves_exactly_ten_current_and_zero_historical_payload(tmp_path):
    registry = current_read_model_registry(tmp_path)
    snapshots = migrated_snapshot_store(tmp_path)
    replace_current_holdings(
        registry=registry,
        snapshot_port=real_snapshot_port(snapshots),
        current_revision=registry.read_active_revision(),
        candidate_revision=ten(),
        approval_reason="synthetic-import",
    )

    active = registry.read_active_revision()
    assert active.holding_count == 10
    assert active.revision_id == "b" * 32
    assert registry.count_payload_bearing_rows() == 1
    assert registry.count_superseded_payload_rows() == 0

    rows = registry_rows(tmp_path)
    payloads = [row for row in rows if row[2] != EMPTY_HOLDINGS_PAYLOAD]
    assert [row[0] for row in payloads] == ["b" * 32]
    assert [row[1] for row in payloads] == ["ACTIVE"]
    old_row = [row for row in rows if row[0] == "a" * 32][0]
    assert old_row[1] == "SUPERSEDED"
    assert old_row[2] == EMPTY_HOLDINGS_PAYLOAD
    assert "80000" not in old_row[2]


def test_the_old_seven_survives_only_in_snapshot_history(tmp_path):
    registry = current_read_model_registry(tmp_path)
    snapshots = migrated_snapshot_store(tmp_path)
    receipt = replace_current_holdings(
        registry=registry,
        snapshot_port=real_snapshot_port(snapshots),
        current_revision=registry.read_active_revision(),
        candidate_revision=ten(),
        approval_reason="synthetic-import",
    )

    old = snapshots.read_snapshot(receipt.current_snapshot_id)
    assert len(old.accounts[0].holdings) == 7
    assert {item.symbol for item in old.accounts[0].holdings} == {
        f"80000{n}" for n in range(1, 8)
    }
    new = snapshots.read_snapshot(receipt.candidate_snapshot_id)
    assert len(new.accounts[0].holdings) == 10
    assert len(snapshots.list_pension_capture_headers()) == 2


def test_source_as_of_is_preserved_not_replaced_by_receipt_time(tmp_path):
    registry = current_read_model_registry(tmp_path)
    snapshots = migrated_snapshot_store(tmp_path)
    replace_current_holdings(
        registry=registry,
        snapshot_port=real_snapshot_port(snapshots),
        current_revision=registry.read_active_revision(),
        candidate_revision=ten(),
        approval_reason="synthetic-import",
    )
    active = registry.read_active_revision()
    assert active.source_as_of == TEN_AS_OF
    assert active.source_as_of != NOW


# --- proof C: any snapshot/read-back failure changes nothing ---------------


def failing_port(fail_on: int) -> PensionSnapshotPortV1:
    calls = {"n": 0}

    def capture(_revision) -> SnapshotOutcomeV1:
        calls["n"] += 1
        if calls["n"] == fail_on:
            return SnapshotOutcomeV1(False, "", BLOCKED_SNAPSHOT_FAILED)
        return SnapshotOutcomeV1(True, uuid4().hex)

    return PensionSnapshotPortV1(capture=capture)


def readback_mismatch_port() -> PensionSnapshotPortV1:
    def capture(_revision) -> SnapshotOutcomeV1:
        return SnapshotOutcomeV1(False, uuid4().hex, BLOCKED_READBACK_MISMATCH)

    return PensionSnapshotPortV1(capture=capture)


@pytest.mark.parametrize("fail_on", [1, 2])
def test_a_snapshot_failure_leaves_current_at_seven(tmp_path, fail_on):
    registry = current_read_model_registry(tmp_path)
    receipt = replace_current_holdings(
        registry=registry,
        snapshot_port=failing_port(fail_on),
        current_revision=registry.read_active_revision(),
        candidate_revision=ten(),
        approval_reason="synthetic-import",
    )
    assert receipt.outcome == OUTCOME_BLOCKED
    assert receipt.reason == BLOCKED_SNAPSHOT_FAILED
    assert registry.read_active_revision().holding_count == 7
    assert registry.count_payload_bearing_rows() == 1


def test_a_readback_mismatch_leaves_current_at_seven(tmp_path):
    registry = current_read_model_registry(tmp_path)
    trace: list[str] = []
    receipt = replace_current_holdings(
        registry=registry,
        snapshot_port=readback_mismatch_port(),
        current_revision=registry.read_active_revision(),
        candidate_revision=ten(),
        approval_reason="synthetic-import",
        trace=trace,
    )
    assert receipt.outcome == OUTCOME_BLOCKED
    assert receipt.reason == BLOCKED_READBACK_MISMATCH
    assert "replace_current" not in trace
    assert registry.read_active_revision().holding_count == 7


def test_a_blocked_import_never_creates_a_superseded_payload(tmp_path):
    registry = current_read_model_registry(tmp_path)
    replace_current_holdings(
        registry=registry,
        snapshot_port=failing_port(1),
        current_revision=registry.read_active_revision(),
        candidate_revision=ten(),
        approval_reason="synthetic-import",
    )
    assert registry.count_superseded_payload_rows() == 0
    assert [row[1] for row in registry_rows(tmp_path)] == ["ACTIVE"]


# --- proof E: rollback from a selected snapshot ----------------------------


def test_rollback_reconstructs_current_from_a_selected_snapshot(tmp_path):
    registry = current_read_model_registry(tmp_path)
    snapshots = migrated_snapshot_store(tmp_path)
    receipt = replace_current_holdings(
        registry=registry,
        snapshot_port=real_snapshot_port(snapshots),
        current_revision=registry.read_active_revision(),
        candidate_revision=ten(),
        approval_reason="synthetic-import",
    )
    assert registry.read_active_revision().holding_count == 10

    # Rebuild the old revision out of the verified snapshot, not out of any
    # retained registry payload (there is none).
    captured = snapshots.read_snapshot(receipt.current_snapshot_id)
    rebuilt = revision(
        tuple(item.symbol for item in captured.accounts[0].holdings),
        revision_id="a" * 32,
        source_as_of=SEVEN_AS_OF,
    )

    rolled = rollback_current_from_snapshot(
        registry=registry,
        reconstructed_revision=rebuilt,
        approval_reason="synthetic-rollback",
    )
    assert rolled.outcome == OUTCOME_REPLACED

    active = registry.read_active_revision()
    assert active.holding_count == 7
    assert active.revision_id == "a" * 32
    assert active.source_as_of == SEVEN_AS_OF
    assert registry.count_payload_bearing_rows() == 1
    assert registry.count_superseded_payload_rows() == 0


def test_rollback_leaves_exactly_one_current_row(tmp_path):
    registry = current_read_model_registry(tmp_path)
    snapshots = migrated_snapshot_store(tmp_path)
    receipt = replace_current_holdings(
        registry=registry,
        snapshot_port=real_snapshot_port(snapshots),
        current_revision=registry.read_active_revision(),
        candidate_revision=ten(),
        approval_reason="synthetic-import",
    )
    captured = snapshots.read_snapshot(receipt.current_snapshot_id)
    rebuilt = revision(
        tuple(item.symbol for item in captured.accounts[0].holdings),
        revision_id="a" * 32,
        source_as_of=SEVEN_AS_OF,
    )
    rollback_current_from_snapshot(
        registry=registry,
        reconstructed_revision=rebuilt,
        approval_reason="synthetic-rollback",
    )

    rows = registry_rows(tmp_path)
    assert sum(1 for row in rows if row[1] == "ACTIVE") == 1
    assert sum(1 for row in rows if row[2] != EMPTY_HOLDINGS_PAYLOAD) == 1
    ten_row = [row for row in rows if row[0] == "b" * 32][0]
    assert ten_row[1] == "SUPERSEDED"
    assert ten_row[2] == EMPTY_HOLDINGS_PAYLOAD


# ---------------------------------------------------------------------------
# Contracts 5 and 7 (proof F) are proved against the REAL ledger, the REAL
# snapshot store and the REAL operator CLI in
# tests/test_pension_operational_tools.py. The earlier fake-port versions of
# those tests were removed: an in-memory dict link port cannot show that the
# executable path is wired, which is precisely what had to be proved.
# ---------------------------------------------------------------------------
