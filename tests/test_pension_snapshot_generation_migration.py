"""Official snapshot store, widened to admit the pension source.

Proof A: an existing Toss-only store migrates safely and its Toss rows,
bytes and consumer behaviour are unchanged.
Proof B: pension snapshots append and read back exactly, isolated by source.
Proof G: there is no second snapshot database and no abandoned alternate path.

Every fixture is synthetic and every store lives under ``tmp_path``. The
migration is rehearsed on a cloned temporary file only; nothing runtime is
touched.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from tokmo.records import holdings_snapshot_migration as migration
from tokmo.records.holdings_persistence import (
    authorize_pension_import,
    authorize_user_request,
)
from tokmo.records.holdings_snapshots import (
    PENSION_SNAPSHOT_SOURCE,
    SCHEMA_GENERATION_ONE,
    SCHEMA_GENERATION_TWO,
    TOSS_SNAPSHOT_SOURCE,
    HoldingsSnapshotReadError,
    HoldingsSnapshotStore,
    snapshot_content_hash,
)
from tokmo.values.holdings import (
    CompleteHoldingsSnapshot,
    HoldingsSnapshotAccount,
    TossHoldingFact,
)

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
TOSS_CAPTURED = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
PENSION_SEVEN_AT = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
PENSION_TEN_AT = datetime(2026, 8, 13, 0, 0, tzinfo=UTC)

REPO_ROOT = Path(__file__).resolve().parents[1]


def clock():
    return NOW


def item(symbol: str, quantity: str = "10", price: str = "1000"):
    return TossHoldingFact(
        symbol=symbol,
        quantity=quantity,
        avg_price=price,
        avg_price_field="average_cost",
        currency="KRW",
        market_raw="KR",
        market_field="market",
        name=None,
        asset_type=None,
    )


def snapshot(source: str, captured_at: datetime, symbols, snapshot_id=None):
    accounts = (HoldingsSnapshotAccount(1, tuple(item(symbol) for symbol in symbols)),)
    draft = CompleteHoldingsSnapshot(
        snapshot_id or uuid4().hex, source, captured_at, 1, accounts, "0" * 64
    )
    return CompleteHoldingsSnapshot(
        draft.snapshot_id,
        source,
        captured_at,
        1,
        accounts,
        snapshot_content_hash(draft),
    )


def toss_capture(symbols=("069500", "005930")):
    return snapshot(TOSS_SNAPSHOT_SOURCE, TOSS_CAPTURED, symbols)


def pension_seven():
    return snapshot(
        PENSION_SNAPSHOT_SOURCE,
        PENSION_SEVEN_AT,
        tuple(f"80000{n}" for n in range(1, 8)),
    )


def pension_ten():
    return snapshot(
        PENSION_SNAPSHOT_SOURCE,
        PENSION_TEN_AT,
        tuple(f"90000{n}" for n in range(1, 10)) + ("900010",),
    )


def toss_permit(snapshot_id: str):
    # The v1 named-program allowlist is empty by design, so a Toss capture is
    # only persistable as an explicit user-requested capture.
    return authorize_user_request(snapshot_id=snapshot_id, clock=clock)


def pension_permit(snapshot_id: str):
    return authorize_pension_import(
        snapshot_id=snapshot_id, program_name="tokmo-v3-pension-import-v1", clock=clock
    )


def generation_one_store(tmp_path) -> tuple[HoldingsSnapshotStore, Path]:
    """A store exactly as it exists today: created fresh, Toss rows only."""
    path = tmp_path / "toss" / "holdings_snapshots.sqlite3"
    store = HoldingsSnapshotStore(path, clock=clock)
    capture = toss_capture()
    receipt = store.append(capture, toss_permit(capture.snapshot_id))
    if receipt.status != "SAVED":
        raise AssertionError(receipt.reason)
    return store, path


def migrate_to_generation_two(path: Path, tmp_path: Path) -> Path:
    """Rehearse the offline migration on a clone, then install it."""
    inventory = migration.inspect_v1(path)
    sidecar = tmp_path / "sidecar" / "holdings_snapshots.sqlite3"
    migration.build_generation_r2_sidecar(path, sidecar)
    migration.verify_generation_r2_sidecar(inventory, sidecar)
    migration.install_generation_r2_sidecar(path, sidecar)
    return path


# ---------------------------------------------------------------------------
# Proof A: the existing Toss-only store survives the migration untouched
# ---------------------------------------------------------------------------


def test_fresh_store_is_still_generation_one(tmp_path):
    """No implicit upgrade: creating a store today yields the old schema."""
    _store, path = generation_one_store(tmp_path)
    connection = sqlite3.connect(path)
    try:
        assert set(connection.execute("SELECT key, value FROM storage_metadata")) == {
            ("holdings_snapshot_schema_version", "1"),
            ("holdings_snapshot_adapter", "tokmo-holdings-snapshots-v1"),
        }
    finally:
        connection.close()


def test_migration_preserves_every_toss_row_exactly(tmp_path):
    store, path = generation_one_store(tmp_path)
    before = store.read_latest()
    inventory = migration.inspect_v1(path)

    migrate_to_generation_two(path, tmp_path)

    after = HoldingsSnapshotStore(path, clock=clock).read_latest()
    assert after == before
    assert after.content_hash == before.content_hash
    assert after.source == TOSS_SNAPSHOT_SOURCE

    connection = sqlite3.connect(path)
    try:
        assert set(connection.execute("SELECT key, value FROM storage_metadata")) == {
            ("holdings_snapshot_schema_version", "2"),
            ("holdings_snapshot_adapter", "tokmo-holdings-snapshots-v1"),
        }
        for table in (
            "holdings_snapshots",
            "holdings_snapshot_accounts",
            "holdings_snapshot_items",
        ):
            assert (
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                == inventory.row_counts[table]
            )
    finally:
        connection.close()


def test_migration_does_not_mutate_the_source_while_building(tmp_path):
    _store, path = generation_one_store(tmp_path)
    inventory = migration.inspect_v1(path)
    sidecar = tmp_path / "sidecar" / "holdings_snapshots.sqlite3"
    migration.build_generation_r2_sidecar(path, sidecar)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == inventory.sha256
    migration.verify_generation_r2_sidecar(inventory, sidecar)


def test_verification_detects_a_mutated_source(tmp_path):
    _store, path = generation_one_store(tmp_path)
    inventory = migration.inspect_v1(path)
    sidecar = tmp_path / "sidecar" / "holdings_snapshots.sqlite3"
    migration.build_generation_r2_sidecar(path, sidecar)
    path.write_bytes(path.read_bytes() + b"\x00")
    with pytest.raises(migration.SidecarBuildError) as error:
        migration.verify_generation_r2_sidecar(inventory, sidecar)
    assert error.value.reason == "source_mutated_during_build"


def test_building_over_an_existing_sidecar_is_refused(tmp_path):
    _store, path = generation_one_store(tmp_path)
    sidecar = tmp_path / "sidecar" / "holdings_snapshots.sqlite3"
    migration.build_generation_r2_sidecar(path, sidecar)
    with pytest.raises(migration.SidecarBuildError) as error:
        migration.build_generation_r2_sidecar(path, sidecar)
    assert error.value.reason == "sidecar_already_exists"


def test_migrating_an_already_migrated_store_is_refused(tmp_path):
    _store, path = generation_one_store(tmp_path)
    migrate_to_generation_two(path, tmp_path)
    with pytest.raises(migration.SidecarBuildError) as error:
        migration.build_generation_r2_sidecar(
            path, tmp_path / "second" / "holdings_snapshots.sqlite3"
        )
    assert error.value.reason == "source_not_generation_one"


def test_toss_append_still_works_after_migration(tmp_path):
    store, path = generation_one_store(tmp_path)
    migrate_to_generation_two(path, tmp_path)
    later = snapshot(
        TOSS_SNAPSHOT_SOURCE, datetime(2026, 8, 12, 9, 0, tzinfo=UTC), ("069500",)
    )
    receipt = store.append(later, toss_permit(later.snapshot_id))
    assert receipt.status == "SAVED"
    assert store.read_latest().snapshot_id == later.snapshot_id


# ---------------------------------------------------------------------------
# Unknown / partial schema fails closed
# ---------------------------------------------------------------------------


def test_an_unknown_metadata_version_fails_closed(tmp_path):
    _store, path = generation_one_store(tmp_path)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE storage_metadata SET value='99' "
            "WHERE key='holdings_snapshot_schema_version'"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(HoldingsSnapshotReadError) as error:
        HoldingsSnapshotStore(path, clock=clock).read_latest()
    assert error.value.reason == "schema_mismatch"


def test_generation_two_metadata_over_generation_one_ddl_fails_closed(tmp_path):
    """A half-applied migration must never be accepted."""
    _store, path = generation_one_store(tmp_path)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE storage_metadata SET value='2' "
            "WHERE key='holdings_snapshot_schema_version'"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(HoldingsSnapshotReadError) as error:
        HoldingsSnapshotStore(path, clock=clock).read_latest()
    assert error.value.reason == "schema_mismatch"


# ---------------------------------------------------------------------------
# Proof B: pension append / read-back, and source isolation
# ---------------------------------------------------------------------------


def test_pension_append_is_refused_before_migration(tmp_path):
    store, _path = generation_one_store(tmp_path)
    capture = pension_seven()
    receipt = store.append(capture, pension_permit(capture.snapshot_id))
    assert receipt.status == "FAILED"
    assert receipt.reason == "schema_mismatch"


def test_pension_append_is_refused_on_a_store_created_by_this_call(tmp_path):
    store = HoldingsSnapshotStore(
        tmp_path / "fresh" / "holdings_snapshots.sqlite3", clock=clock
    )
    capture = pension_seven()
    receipt = store.append(capture, pension_permit(capture.snapshot_id))
    assert receipt.status == "FAILED"
    assert receipt.reason == "schema_mismatch"


def migrated_store(tmp_path) -> HoldingsSnapshotStore:
    store, path = generation_one_store(tmp_path)
    migrate_to_generation_two(path, tmp_path)
    return store


def test_pension_seven_appends_and_reads_back_exactly(tmp_path):
    store = migrated_store(tmp_path)
    capture = pension_seven()
    receipt = store.append(capture, pension_permit(capture.snapshot_id))
    assert receipt.status == "SAVED"

    loaded = store.read_snapshot(capture.snapshot_id)
    assert loaded == capture
    assert loaded.content_hash == capture.content_hash
    assert len(loaded.accounts[0].holdings) == 7


def test_pension_ten_appends_and_reads_back_exactly(tmp_path):
    store = migrated_store(tmp_path)
    seven = pension_seven()
    store.append(seven, pension_permit(seven.snapshot_id))
    ten = pension_ten()
    assert store.append(ten, pension_permit(ten.snapshot_id)).status == "SAVED"

    loaded = store.read_snapshot(ten.snapshot_id)
    assert loaded == ten
    assert len(loaded.accounts[0].holdings) == 10
    assert store.read_latest_pension_snapshot().snapshot_id == ten.snapshot_id


def test_pension_history_keeps_both_captures(tmp_path):
    store = migrated_store(tmp_path)
    seven = pension_seven()
    ten = pension_ten()
    store.append(seven, pension_permit(seven.snapshot_id))
    store.append(ten, pension_permit(ten.snapshot_id))

    headers = store.list_pension_capture_headers()
    assert [header.snapshot_id for header in headers] == [
        ten.snapshot_id,
        seven.snapshot_id,
    ]
    previous, current = store.read_latest_pension_pair()
    assert previous.snapshot_id == seven.snapshot_id
    assert current.snapshot_id == ten.snapshot_id


def test_a_pension_capture_is_never_served_to_a_toss_consumer(tmp_path):
    """The isolation that matters: pension rows share the database but can
    never be selected by the pre-existing Toss consumer APIs."""
    store = migrated_store(tmp_path)
    toss_before = store.read_latest()
    ten = pension_ten()  # captured LATER than the Toss capture
    store.append(ten, pension_permit(ten.snapshot_id))

    assert store.read_latest() == toss_before
    assert store.read_latest().source == TOSS_SNAPSHOT_SOURCE
    previous, current = store.read_latest_pair()
    assert current == toss_before
    assert previous is None
    headers = store.list_capture_headers()
    assert {header.source for header in headers} == {TOSS_SNAPSHOT_SOURCE}
    assert ten.snapshot_id not in {header.snapshot_id for header in headers}


def test_a_toss_capture_is_never_served_to_a_pension_consumer(tmp_path):
    store = migrated_store(tmp_path)
    seven = pension_seven()
    store.append(seven, pension_permit(seven.snapshot_id))
    assert store.read_latest_pension_snapshot().snapshot_id == seven.snapshot_id
    headers = store.list_pension_capture_headers()
    assert {header.source for header in headers} == {PENSION_SNAPSHOT_SOURCE}


def test_pension_readers_fail_closed_on_a_generation_one_store(tmp_path):
    store, _path = generation_one_store(tmp_path)
    for reader in (
        store.read_latest_pension_snapshot,
        store.read_latest_pension_pair,
        store.list_pension_capture_headers,
    ):
        with pytest.raises(HoldingsSnapshotReadError) as error:
            reader()
        assert error.value.reason == "schema_mismatch"


def test_pension_reader_reports_not_found_when_no_pension_capture_exists(tmp_path):
    store = migrated_store(tmp_path)
    with pytest.raises(HoldingsSnapshotReadError) as error:
        store.read_latest_pension_snapshot()
    assert error.value.reason == "not_found"


def test_duplicate_pension_append_is_a_duplicate_not_a_second_row(tmp_path):
    store = migrated_store(tmp_path)
    capture = pension_seven()
    assert store.append(capture, pension_permit(capture.snapshot_id)).status == "SAVED"
    assert (
        store.append(capture, pension_permit(capture.snapshot_id)).status == "DUPLICATE"
    )
    assert len(store.list_pension_capture_headers()) == 1


def test_a_pension_capture_without_a_pension_permit_is_refused(tmp_path):
    """Source and criteria must agree, so a pension capture cannot be written
    under a Toss user-request permit and lose its provenance."""
    store = migrated_store(tmp_path)
    capture = pension_seven()
    receipt = store.append(capture, toss_permit(capture.snapshot_id))
    assert receipt.status == "FAILED"
    assert receipt.reason == "invalid_permit"
    assert len(store.list_pension_capture_headers()) == 0


def test_a_toss_capture_under_a_pension_permit_is_refused(tmp_path):
    store = migrated_store(tmp_path)
    capture = snapshot(
        TOSS_SNAPSHOT_SOURCE, datetime(2026, 8, 12, 9, 0, tzinfo=UTC), ("069500",)
    )
    receipt = store.append(capture, pension_permit(capture.snapshot_id))
    assert receipt.status == "FAILED"
    assert receipt.reason == "invalid_permit"


# ---------------------------------------------------------------------------
# Proof G: one snapshot database, no abandoned alternate path
# ---------------------------------------------------------------------------


def test_only_one_snapshot_database_filename_exists_in_the_tree():
    names = set()
    for root in ("src", "tools"):
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            import re

            names |= set(re.findall(r"holdings[\w-]*\.sqlite3", text))
    assert names <= {"holdings_snapshots.sqlite3"}, names


def test_the_rejected_second_store_exists_nowhere():
    prefix = "pension_account_"
    banned = (
        prefix + "snapshots",
        "PensionAccount" + "SnapshotStore",
        prefix + "active_pointer",
        prefix + "revisions",
        prefix + "assets",
    )
    for root in ("src", "tests", "tools"):
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for token in banned:
                assert token not in text, (token, path.name)


def test_the_snapshot_store_declares_exactly_two_generations():
    from tokmo.records import holdings_snapshots

    assert set(holdings_snapshots._GENERATIONS) == {
        SCHEMA_GENERATION_ONE,
        SCHEMA_GENERATION_TWO,
    }
    assert holdings_snapshots.PENSION_SNAPSHOT_SOURCE == "pension-excel-v1"
    assert holdings_snapshots.TOSS_SNAPSHOT_SOURCE == "toss-holdings-v1"


def test_the_two_generations_differ_only_in_the_source_check():
    from tokmo.records import holdings_snapshots

    one, two = holdings_snapshots._DDL, holdings_snapshots._DDL_GEN2
    assert len(one) == len(two)
    differing = [index for index in range(len(one)) if one[index] != two[index]]
    assert differing == [1]
    assert (
        one[1].replace(
            "CHECK (source = 'toss-holdings-v1')",
            "CHECK (source IN ('toss-holdings-v1', 'pension-excel-v1'))",
        )
        == two[1]
    )
