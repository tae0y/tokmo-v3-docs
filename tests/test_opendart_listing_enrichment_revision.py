"""Guarded OpenDART E-to-K listing-enrichment revision contract."""

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tokmo.facts.opendart_bulk import Disclosure
from tokmo.records.opendart_disclosures import IMMUTABLE_FIELDS, DisclosureStore


NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)
RCEPT_NO = "20260815000001"


def disclosure(**changes):
    baseline = Disclosure(
        corp_code="01234567",
        corp_name="상장전법인",
        stock_code=None,
        corp_cls="E",
        report_nm="주요사항보고서",
        rcept_no=RCEPT_NO,
        flr_nm="상장전법인",
        rcept_dt="20260815",
        rm=None,
    )
    return replace(baseline, **changes)


def store(tmp_path):
    return DisclosureStore.open(tmp_path / "private" / "bulk.sqlite3")


@pytest.mark.parametrize("stored_stock_code", [None, ""])
def test_exact_e_to_k_listing_enrichment_is_atomic_revision_and_settles(
        tmp_path, stored_stock_code):
    db = store(tmp_path)
    run = db.claim("partition", "20260815", "20260815", NOW)
    previous = disclosure(stock_code=stored_stock_code)
    observed = disclosure(stock_code="123456", corp_cls="K")

    assert "stock_code" in IMMUTABLE_FIELDS
    assert db.observe(run, previous, NOW) == "new"
    assert db.observe(run, observed, NOW) == "revision"

    canonical, payload_hash, stock_code, corp_cls = db.connection.execute(
        "select canonical_json,payload_hash,stock_code,corp_cls "
        "from disclosures where rcept_no=?", (RCEPT_NO,),
    ).fetchone()
    assert canonical == observed.canonical_json()
    assert payload_hash == observed.payload_hash()
    assert (stock_code, corp_cls) == ("123456", "K")

    revisions = db.connection.execute(
        "select previous_canonical_json,observed_canonical_json,changed_fields "
        "from dart_revisions where rcept_no=?", (RCEPT_NO,),
    ).fetchall()
    assert revisions == [(
        previous.canonical_json(), observed.canonical_json(),
        "corp_cls,stock_code",
    )]
    assert db.connection.execute(
        "select count(*) from conflicts where rcept_no=?", (RCEPT_NO,),
    ).fetchone() == (0,)

    assert db.observe(run, observed, NOW) == "duplicate"
    assert db.connection.execute(
        "select count(*) from dart_revisions where rcept_no=?", (RCEPT_NO,),
    ).fetchone() == (1,)
    receipt = db.receipt(run.run_id)
    assert (receipt.revision_count, receipt.duplicate_count,
            receipt.conflict_count) == (1, 1, 0)
    db.close()


@pytest.mark.parametrize(
    ("stored", "observed"),
    [
        (disclosure(stock_code="111111"),
         disclosure(stock_code="222222", corp_cls="K")),
        (disclosure(stock_code="111111"),
         disclosure(stock_code=None, corp_cls="K")),
        (disclosure(), disclosure(stock_code="123456")),
        (disclosure(corp_cls="Y"),
         disclosure(stock_code="123456", corp_cls="K")),
        (disclosure(),
         disclosure(corp_code="76543210", stock_code="123456", corp_cls="K")),
        (disclosure(),
         disclosure(stock_code="123456", corp_cls="K", rcept_dt="20260814")),
    ],
    ids=[
        "nonempty-to-different", "nonempty-to-null", "missing-stock-without-e-to-k",
        "non-e-to-k", "corp-code-drift", "additional-provider-drift",
    ],
)
def test_all_nonexact_stock_code_transitions_fail_closed(tmp_path, stored, observed):
    db = store(tmp_path)
    run = db.claim("partition", "20260815", "20260815", NOW)
    assert db.observe(run, stored, NOW) == "new"
    assert db.observe(run, observed, NOW) == "conflict"

    row = db.connection.execute(
        "select canonical_json,payload_hash,stock_code,corp_cls "
        "from disclosures where rcept_no=?", (RCEPT_NO,),
    ).fetchone()
    assert row == (stored.canonical_json(), stored.payload_hash(),
                   stored.stock_code, stored.corp_cls)
    assert db.connection.execute(
        "select count(*) from dart_revisions where rcept_no=?", (RCEPT_NO,),
    ).fetchone() == (0,)
    assert db.connection.execute(
        "select count(*) from conflicts where rcept_no=?", (RCEPT_NO,),
    ).fetchone() == (1,)
    db.close()


def test_existing_conflict_evidence_is_not_rewritten_or_deleted(tmp_path):
    db = store(tmp_path)
    run = db.claim("partition", "20260815", "20260815", NOW)
    previous = disclosure()
    assert db.observe(run, previous, NOW) == "new"
    assert db.observe(run, disclosure(stock_code="654321", corp_cls="Y"), NOW) == "conflict"
    evidence_before = db.connection.execute(
        "select * from conflicts where rcept_no=?", (RCEPT_NO,),
    ).fetchall()

    observed = disclosure(stock_code="123456", corp_cls="K")
    assert db.observe(run, observed, NOW) == "revision"
    assert db.connection.execute(
        "select * from conflicts where rcept_no=?", (RCEPT_NO,),
    ).fetchall() == evidence_before
    db.close()


def test_listing_enrichment_revision_rolls_back_every_write(tmp_path):
    db = store(tmp_path)
    run = db.claim("partition", "20260815", "20260815", NOW)
    previous = disclosure()
    observed = disclosure(stock_code="123456", corp_cls="K")
    assert db.observe(run, previous, NOW) == "new"
    db.connection.execute("""
        CREATE TRIGGER fail_listing_revision_counter
        BEFORE UPDATE OF revision_count ON runs
        WHEN NEW.revision_count > OLD.revision_count
        BEGIN SELECT RAISE(ABORT, 'forced_listing_revision_failure'); END
    """)

    with pytest.raises(sqlite3.IntegrityError,
                       match="forced_listing_revision_failure"):
        db.observe(run, observed, NOW)

    canonical, payload_hash, stock_code, corp_cls = db.connection.execute(
        "select canonical_json,payload_hash,stock_code,corp_cls "
        "from disclosures where rcept_no=?", (RCEPT_NO,),
    ).fetchone()
    assert canonical == previous.canonical_json()
    assert payload_hash == previous.payload_hash()
    assert (stock_code, corp_cls) == (None, "E")
    assert db.connection.execute(
        "select count(*) from dart_revisions where rcept_no=?", (RCEPT_NO,),
    ).fetchone() == (0,)
    assert db.receipt(run.run_id).revision_count == 0
    db.close()
