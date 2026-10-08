"""Slice 2 — Durable revision lifecycle tests (B2, m5, m6, M11, M12)."""
from __future__ import annotations

import hashlib
import os
import stat
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tokmo.records.pension_revisions import (
    PensionRegistryStore,
    PensionRegistryCorruptError,
    PENSION_REGISTRY_DDL_SHA256,
)
from tokmo.values.current_holdings import HoldingItemV1
from tokmo.values.pension_holdings import (
    PensionHoldingsRevisionV1,
    PENSION_HOLDINGS_SCHEMA_VERSION,
    pension_semantic_digest,
    pension_reconciliation_digest,
)

_KST = ZoneInfo("Asia/Seoul")
_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=_KST)
_H64 = "a" * 64


def _make_revision(
    *,
    revision_id: str = "a" * 32,
    holdings_count: int = 1,
    liquidation_flag: int = 0,
) -> PensionHoldingsRevisionV1:
    if holdings_count == 0:
        holdings = ()
        semantic = pension_semantic_digest(())
    else:
        holdings = tuple(
            HoldingItemV1(
                symbol=f"00593{i}",
                quantity="100",
                avg_price="50000",
                currency="KRW",
                market_raw="KR",
                market_field="market",
            )
            for i in range(holdings_count)
        )
        semantic = pension_semantic_digest(holdings)
    names = tuple((h, f"Name{i}") for i, h in enumerate(holdings))
    return PensionHoldingsRevisionV1(
        revision_id=revision_id,
        schema_version=PENSION_HOLDINGS_SCHEMA_VERSION,
        source_content_sha256=_H64,
        parser_version="pension-excel-v1",
        sheet_schema_version=1,
        holdings=holdings,
        source_as_of=datetime(2026, 7, 1, tzinfo=_KST),
        uploaded_at=_NOW,
        validated_at=_NOW,
        semantic_digest=semantic,
        reconciliation_digest=pension_reconciliation_digest(names),
        holding_count=holdings_count,
        liquidation_flag=liquidation_flag,
    )


def _make_store(tmp_path: Path) -> PensionRegistryStore:
    path = tmp_path / "pension_registry.sqlite3"
    store = PensionRegistryStore(path, clock=lambda: _NOW)
    return store


class TestCandidateLifecycle:
    def test_candidate_is_not_active(self, tmp_path):
        store = _make_store(tmp_path)
        rev = _make_revision()
        store.create_candidate(rev)
        active = store.read_active_revision()
        assert active is None

    def test_activate_sets_active(self, tmp_path):
        store = _make_store(tmp_path)
        rev = _make_revision()
        store.create_candidate(rev)
        store.promote_to_pending(rev.revision_id)
        store.activate(rev.revision_id, approval_reason="test")
        active = store.read_active_revision()
        assert active is not None
        assert active.revision_id == rev.revision_id

    def test_second_activation_supersedes_first(self, tmp_path):
        store = _make_store(tmp_path)
        r1 = _make_revision(revision_id="a" * 32)
        r2 = _make_revision(revision_id="b" * 32)
        store.create_candidate(r1)
        store.promote_to_pending(r1.revision_id)
        store.activate(r1.revision_id, approval_reason="first")
        store.create_candidate(r2)
        store.promote_to_pending(r2.revision_id)
        store.activate(r2.revision_id, approval_reason="second")
        active = store.read_active_revision()
        assert active.revision_id == r2.revision_id

    def test_rollback_reactivates_target(self, tmp_path):
        store = _make_store(tmp_path)
        r1 = _make_revision(revision_id="a" * 32)
        r2 = _make_revision(revision_id="b" * 32)
        store.create_candidate(r1)
        store.promote_to_pending(r1.revision_id)
        store.activate(r1.revision_id, approval_reason="first")
        store.create_candidate(r2)
        store.promote_to_pending(r2.revision_id)
        store.activate(r2.revision_id, approval_reason="second")
        store.rollback_to(r1.revision_id, approval_reason="rollback")
        active = store.read_active_revision()
        assert active.revision_id == r1.revision_id


class TestLiquidatedRevision:
    def test_liquidated_revision_can_be_activated(self, tmp_path):
        store = _make_store(tmp_path)
        rev = _make_revision(holdings_count=0, liquidation_flag=1)
        store.create_candidate(rev)
        store.promote_to_pending(rev.revision_id)
        store.activate(rev.revision_id, approval_reason="liquidated")
        active = store.read_active_revision()
        assert active is not None
        assert active.liquidation_flag == 1
        assert active.holding_count == 0


class TestReminderEvents:
    def test_reserve_reminder(self, tmp_path):
        store = _make_store(tmp_path)
        rev = _make_revision()
        store.create_candidate(rev)
        store.promote_to_pending(rev.revision_id)
        store.activate(rev.revision_id, approval_reason="test")
        reminder_id = hashlib.sha256(b"test-reminder").hexdigest()
        store.reserve_reminder(reminder_id, rev.revision_id, 31, _NOW)
        row = store.read_reminder(reminder_id)
        assert row is not None

    def test_duplicate_reminder_rejected(self, tmp_path):
        store = _make_store(tmp_path)
        rev = _make_revision()
        store.create_candidate(rev)
        store.activate(rev.revision_id, approval_reason="test")
        reminder_id = hashlib.sha256(b"dup").hexdigest()
        store.reserve_reminder(reminder_id, rev.revision_id, 31, _NOW)
        with pytest.raises(Exception):
            store.reserve_reminder(reminder_id, rev.revision_id, 31, _NOW)


class TestDDLChecksum:
    def test_ddl_sha256_constant_matches(self):
        from tokmo.records.pension_revisions import _DDL_STATEMENTS, _ddl_sha256
        recomputed = _ddl_sha256()
        assert recomputed == PENSION_REGISTRY_DDL_SHA256


class TestCorruptStoreRefusal:
    def test_wrong_mode_refused(self, tmp_path):
        store = _make_store(tmp_path)
        rev = _make_revision()
        store.create_candidate(rev)
        store.activate(rev.revision_id, approval_reason="test")
        # Tamper file mode
        path = tmp_path / "pension_registry.sqlite3"
        os.chmod(path, 0o644)
        with pytest.raises((PensionRegistryCorruptError, OSError)):
            store2 = PensionRegistryStore(path, clock=lambda: _NOW)
            store2.read_active_revision()


def test_read_active_revision_ro_is_no_create_and_byte_stable(tmp_path):
    absent = tmp_path / "absent.sqlite3"
    assert PensionRegistryStore(absent, clock=lambda: _NOW).read_active_revision_ro() is None
    assert not absent.exists()
    store = _make_store(tmp_path)
    revision = _make_revision()
    store.create_candidate(revision)
    store.activate(revision.revision_id, approval_reason="test")
    path = tmp_path / "pension_registry.sqlite3"
    before = (path.read_bytes(), path.stat().st_mtime_ns, {item.name for item in tmp_path.iterdir()})
    assert store.read_active_revision_ro() == revision
    assert (path.read_bytes(), path.stat().st_mtime_ns, {item.name for item in tmp_path.iterdir()}) == before


@pytest.mark.parametrize("payload", [b"", b"not-sqlite"])
def test_read_active_revision_ro_corrupt_is_no_mutation(tmp_path, payload):
    path = tmp_path / "pension_registry.sqlite3"
    path.write_bytes(payload)
    path.chmod(0o600)
    before = (path.read_bytes(), path.stat().st_mtime_ns, tuple(tmp_path.iterdir()))
    with pytest.raises(PensionRegistryCorruptError):
        PensionRegistryStore(path, clock=lambda: _NOW).read_active_revision_ro()
    assert (path.read_bytes(), path.stat().st_mtime_ns, tuple(tmp_path.iterdir())) == before
