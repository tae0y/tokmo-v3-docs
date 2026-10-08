import os
import sqlite3
import stat
from dataclasses import fields
from datetime import datetime, timezone

import pytest

from tokmo.facts.opendart_bulk import Disclosure
from tokmo.records.opendart_disclosures import (
    IMMUTABLE_FIELDS, PROVIDER_MUTABLE_FIELDS, DisclosureStore, UnsafeStore,
)


NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)


def disclosure(name="report"):
    return Disclosure("00126380", "Samsung", "005930", "Y", name,
                      "20260000000001", "filer", "20260701", None)


def test_every_disclosure_field_is_classified():
    classified = (set(IMMUTABLE_FIELDS) | set(PROVIDER_MUTABLE_FIELDS) |
                  {"rcept_no"})
    assert classified == {field.name for field in fields(Disclosure)}
    assert not set(IMMUTABLE_FIELDS) & set(PROVIDER_MUTABLE_FIELDS)


def test_private_modes_duplicate_revision_and_conflict_are_distinct(tmp_path):
    root = tmp_path / "private"
    store = DisclosureStore.open(root / "bulk.sqlite3")
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "bulk.sqlite3").stat().st_mode) == 0o600
    run = store.claim("preflight", "20260701", "20260731", NOW)
    assert store.observe(run, disclosure(), NOW) == "new"
    assert store.observe(run, disclosure(), NOW) == "duplicate"
    assert store.observe(run, disclosure("changed"), NOW) == "revision"
    assert store.connection.execute("select report_nm from disclosures").fetchone() == ("changed",)
    impostor = Disclosure("99999999", "Samsung", "005930", "Y", "changed",
                          "20260000000001", "filer", "20260701", None)
    assert store.observe(run, impostor, NOW) == "conflict"
    assert store.connection.execute("select count(*) from conflicts").fetchone() == (1,)
    store.close()


def test_symlink_and_nonregular_store_rejected(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    target = root / "target"
    target.write_text("x")
    os.chmod(target, 0o600)
    (root / "link").symlink_to(target)
    with pytest.raises(UnsafeStore):
        DisclosureStore.open(root / "link")
    (root / "dir").mkdir()
    with pytest.raises(UnsafeStore):
        DisclosureStore.open(root / "dir")


def test_second_claim_is_skipped_overlap(tmp_path):
    store = DisclosureStore.open(tmp_path / "p" / "d.sqlite3")
    first = store.claim("incremental", "20260701", "20260803", NOW)
    second = store.claim("incremental", "20260701", "20260803", NOW)
    assert first.status == "claimed"
    assert second.status == "skipped_overlap"
    assert store.connection.execute("select count(*) from runs").fetchone() == (2,)


def test_recent_first_observed_is_a_bounded_read_only_candidate_scan(tmp_path):
    from datetime import timedelta

    from tokmo.facts.opendart_bulk import Disclosure

    store = DisclosureStore.open(tmp_path / "p" / "d.sqlite3")
    rows = (
        ("20260000000001", "005930", NOW - timedelta(days=2)),
        ("20260000000002", None, NOW),               # unlisted: excluded
        ("20260000000003", "000660", NOW),
        ("20260000000004", "005930", NOW - timedelta(days=20)),  # too old
        ("20260000000005", "005935", NOW),           # same instant: rcept order
    )
    for rcept_no, stock, observed in rows:
        item = Disclosure("00126380", "Samsung", stock, "Y", "report",
                          rcept_no, "filer", "20260701", None)
        assert store.observe("scan-run", item, observed) == "new"
    before = store.connection.total_changes
    since = (NOW - timedelta(days=14)).isoformat()
    scanned = store.recent_first_observed(since, 200)
    assert [(row[0], row[3]) for row in scanned] == [
        ("20260000000001", "005930"),
        ("20260000000003", "000660"),
        ("20260000000005", "005935"),
    ]
    # Column order is part of the read contract for the enrichment scan.
    assert scanned[0][1:3] == ("00126380", "Samsung")
    assert scanned[0][4:6] == ("report", "20260701")
    assert store.recent_first_observed(since, 2) == scanned[:2]
    assert store.connection.total_changes == before
    store.close()

