"""Durable non-overwriting candidate → canonical promotion protocol.

``os.link`` is the atomic no-replace commit point; the parent-directory
fsync is the promotion completion point. The candidate remains a same-inode
staging alias (never a second history); recovery re-verifies inode and
content hash and repeats the directory fsync; candidate-name cleanup is a
separate, non-authoritative housekeeping step whose failure can never roll
back a completed promotion. Synthetic fixtures and temporary paths only.
"""

import itertools
import os
import sqlite3
from datetime import date, datetime, timezone

import pytest

import tokmo.records.trade_ledger_store as trade_ledger_store
from tokmo.records.trade_ledger_store import (
    TradeLedgerStore,
    cleanup_candidate_alias,
    finalize_candidate,
    recover_promotion,
)
from tokmo.values.trade_ledger import ImportAuditMeta, TradeLedgerEvent


NOW = datetime(2026, 7, 28, 1, 2, 3, tzinfo=timezone.utc)
SOURCE = "retirement_excel_v1"
SCOPE = "retirement_pension_1"
_ID_COUNTER = itertools.count(1)


def make_store(path):
    return TradeLedgerStore(
        path,
        clock=lambda: NOW,
        id_factory=lambda: f"fixture-id-{next(_ID_COUNTER):08d}",
    )


def ledger_event(number):
    return TradeLedgerEvent(
        source=SOURCE,
        account_scope=SCOPE,
        source_record_key=f"row-{number:06d}",
        event_kind="EXECUTION",
        supersedes_source_record_key=None,
        trade_date=date(2026, 3, 3),
        transaction_type_raw="ETF매수",
        instrument_raw="Fixture ETF Promotion",
        quantity="2",
        unit_price="500",
        amount="1000",
        medium_raw="FixtureApp",
    )


def populated_candidate(tmp_path, name="candidate.sqlite3"):
    path = tmp_path / "private" / name
    store = make_store(path)
    receipt = store.append_batch(
        (ledger_event(1), ledger_event(2)),
        import_audit=ImportAuditMeta("ab" * 32, "fixture-importer-1"),
    )
    assert receipt.status == "SAVED"
    return path


def test_promotion_links_canonical_as_same_inode_and_preserves_candidate(
    tmp_path,
):
    candidate = populated_candidate(tmp_path)
    canonical = candidate.with_name("canonical.sqlite3")

    finalize_candidate(candidate, canonical)

    assert canonical.exists()
    assert candidate.exists()
    assert candidate.stat().st_ino == canonical.stat().st_ino
    assert not candidate.with_name(candidate.name + "-wal").exists()
    assert not candidate.with_name(candidate.name + "-shm").exists()
    read_back = make_store(canonical).read_scope(
        source=SOURCE, account_scope=SCOPE
    )
    assert len(read_back) == 2


def test_promotion_requires_same_directory(tmp_path):
    candidate = populated_candidate(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    with pytest.raises(ValueError) as captured:
        finalize_candidate(candidate, elsewhere / "canonical.sqlite3")
    assert str(captured.value) == "directory_mismatch"
    assert not (elsewhere / "canonical.sqlite3").exists()


def test_promotion_requires_an_existing_candidate(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    with pytest.raises(FileNotFoundError) as captured:
        finalize_candidate(
            private / "candidate.sqlite3", private / "canonical.sqlite3"
        )
    assert str(captured.value) == "candidate_missing"


def test_promotion_never_overwrites_an_existing_canonical(tmp_path):
    candidate = populated_candidate(tmp_path)
    canonical = candidate.with_name("canonical.sqlite3")
    canonical.write_bytes(b"fixture-existing-canonical")
    candidate_before = candidate.read_bytes()

    with pytest.raises(FileExistsError) as captured:
        finalize_candidate(candidate, canonical)

    assert str(captured.value) == "canonical_exists"
    assert canonical.read_bytes() == b"fixture-existing-canonical"
    assert candidate.read_bytes() == candidate_before


def test_sidecars_surviving_the_checkpoint_fail_promotion_before_linking(
    tmp_path, monkeypatch
):
    # SQLite normally recovers/removes stray sidecars on open, so stub the
    # checkpoint connection to exercise the store's own guard: sidecars
    # still present after checkpointing must abort before the link.
    candidate = populated_candidate(tmp_path)
    canonical = candidate.with_name("canonical.sqlite3")
    sidecar = candidate.with_name(candidate.name + "-wal")
    sidecar.write_bytes(b"fixture-stray-sidecar")

    class StubCursor:
        def fetchone(self):
            return (0, 0, 0)

    class StubConnection:
        def execute(self, sql):
            return StubCursor()

        def close(self):
            pass

    monkeypatch.setattr(
        trade_ledger_store.sqlite3, "connect", lambda path: StubConnection()
    )
    with pytest.raises(RuntimeError) as captured:
        finalize_candidate(candidate, canonical)
    monkeypatch.undo()

    assert str(captured.value) == "sidecars_present"
    assert not canonical.exists()
    assert candidate.exists()


def test_pre_link_candidate_fsync_failure_aborts_promotion(
    tmp_path, monkeypatch
):
    candidate = populated_candidate(tmp_path)
    canonical = candidate.with_name("canonical.sqlite3")
    candidate_before = candidate.read_bytes()

    def failing_fsync(fd):
        raise OSError("fixture fsync failure")

    monkeypatch.setattr(trade_ledger_store.os, "fsync", failing_fsync)
    with pytest.raises(RuntimeError) as captured:
        finalize_candidate(candidate, canonical)
    monkeypatch.undo()

    assert str(captured.value) == "store_write_failed"
    assert not canonical.exists()
    assert candidate.read_bytes() == candidate_before


def test_link_failure_preserves_candidate_and_leaves_canonical_absent(
    tmp_path, monkeypatch
):
    candidate = populated_candidate(tmp_path)
    canonical = candidate.with_name("canonical.sqlite3")
    candidate_before = candidate.read_bytes()

    def failing_link(src, dst):
        raise OSError("fixture link failure")

    monkeypatch.setattr(trade_ledger_store.os, "link", failing_link)
    with pytest.raises(RuntimeError) as captured:
        finalize_candidate(candidate, canonical)
    monkeypatch.undo()

    assert str(captured.value) == "promotion_link_failed"
    assert not canonical.exists()
    assert candidate.read_bytes() == candidate_before


def test_post_link_directory_fsync_failure_requires_recovery_then_recovers(
    tmp_path, monkeypatch
):
    candidate = populated_candidate(tmp_path)
    canonical = candidate.with_name("canonical.sqlite3")
    real_fsync = os.fsync
    calls = []

    def second_call_fails(fd):
        calls.append(fd)
        if len(calls) >= 2:
            raise OSError("fixture directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(trade_ledger_store.os, "fsync", second_call_fails)
    with pytest.raises(RuntimeError) as captured:
        finalize_candidate(candidate, canonical)
    monkeypatch.undo()

    assert str(captured.value) == "promotion_recovery_required"
    assert candidate.exists()
    assert canonical.exists()
    assert candidate.stat().st_ino == canonical.stat().st_ino

    recover_promotion(candidate, canonical)
    assert candidate.exists()
    assert canonical.exists()
    assert candidate.stat().st_ino == canonical.stat().st_ino


def test_recover_promotion_is_a_noop_when_either_name_is_absent(tmp_path):
    candidate = populated_candidate(tmp_path)
    canonical = candidate.with_name("canonical.sqlite3")
    recover_promotion(candidate, canonical)  # canonical absent: nothing to do
    assert not canonical.exists()

    missing = candidate.with_name("missing-candidate.sqlite3")
    recover_promotion(missing, canonical)
    assert not canonical.exists()


def test_recover_promotion_rejects_divergent_names(tmp_path):
    candidate = populated_candidate(tmp_path)
    canonical = candidate.with_name("canonical.sqlite3")
    canonical.write_bytes(b"fixture-divergent-content")

    with pytest.raises(RuntimeError) as captured:
        recover_promotion(candidate, canonical)
    assert str(captured.value) == "store_corrupt"


def test_cleanup_unlinks_only_the_same_inode_alias(tmp_path):
    candidate = populated_candidate(tmp_path)
    canonical = candidate.with_name("canonical.sqlite3")
    finalize_candidate(candidate, canonical)

    cleanup_candidate_alias(candidate, canonical)

    assert not candidate.exists()
    assert canonical.exists()
    assert len(
        make_store(canonical).read_scope(source=SOURCE, account_scope=SCOPE)
    ) == 2
    # Idempotent: a second cleanup with the candidate gone is a no-op.
    cleanup_candidate_alias(candidate, canonical)


def test_cleanup_refuses_a_divergent_candidate(tmp_path):
    candidate = populated_candidate(tmp_path)
    canonical = candidate.with_name("canonical.sqlite3")
    canonical.write_bytes(b"fixture-not-the-alias")

    with pytest.raises(RuntimeError) as captured:
        cleanup_candidate_alias(candidate, canonical)
    assert str(captured.value) == "store_corrupt"
    assert candidate.exists()


def test_cleanup_failure_never_invalidates_a_completed_promotion(
    tmp_path, monkeypatch
):
    candidate = populated_candidate(tmp_path)
    canonical = candidate.with_name("canonical.sqlite3")
    finalize_candidate(candidate, canonical)

    def failing_unlink(path):
        raise OSError("fixture unlink failure")

    monkeypatch.setattr(trade_ledger_store.os, "unlink", failing_unlink)
    with pytest.raises(RuntimeError) as captured:
        cleanup_candidate_alias(candidate, canonical)
    monkeypatch.undo()

    assert str(captured.value) == "store_write_failed"
    assert canonical.exists()
    assert candidate.exists()
    assert len(
        make_store(canonical).read_scope(source=SOURCE, account_scope=SCOPE)
    ) == 2


def test_promotion_errors_carry_closed_tokens_without_paths(tmp_path):
    candidate = populated_candidate(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    with pytest.raises(ValueError) as captured:
        finalize_candidate(candidate, elsewhere / "canonical.sqlite3")
    rendered = repr(captured.value) + str(captured.value)
    assert str(tmp_path) not in rendered
    assert "candidate.sqlite3" not in rendered


def test_wal_journal_candidate_is_checkpointed_before_linking(tmp_path):
    candidate = populated_candidate(tmp_path)
    canonical = candidate.with_name("canonical.sqlite3")
    # Force the candidate into WAL mode with live sidecars, as a WAL-based
    # writer would leave it, then verify promotion checkpoints and links.
    connection = sqlite3.connect(candidate, isolation_level=None)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "UPDATE trade_ledger_import_batches SET stored_at=stored_at"
    )
    connection.close()

    finalize_candidate(candidate, canonical)

    assert canonical.exists()
    assert candidate.stat().st_ino == canonical.stat().st_ino
    assert not candidate.with_name(candidate.name + "-wal").exists()
