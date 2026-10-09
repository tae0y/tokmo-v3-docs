"""Stage 2-B: material packs and receipts carry the exact mask digest.

The optional ``mask_digest`` field is serialized only when present, so every
historical (pre-mask) pack/receipt byte stream, content hash, and stored G2
evidence file remains valid unchanged.
"""

from datetime import UTC, datetime

import pytest

from tokmo.records.material_pack_store import MaterialPackWriter
from tokmo.records.receipt_store import ReceiptStore
from tokmo.values.material_pack import (
    JobKind,
    build_material_pack_v1,
    canonical_pack_file_bytes,
)
from tokmo.values.receipt_v1 import (
    AttemptStatus,
    AttemptV1,
    MarketFreshness,
    ReceiptV1,
    canonical_receipt_bytes,
)

NOW = datetime(2026, 8, 3, 0, 30, tzinfo=UTC)
MASK_DIGEST = "ab" * 32


def pack(**extra):
    return build_material_pack_v1(
        schema_version=1,
        job=JobKind.QUOTES,
        tick_id=41,
        market_partition=("US",),
        payload={"markets": {"US": []}},
        captured_at=NOW,
        source_timestamps={
            "provider": None,
            "fetched": "2026-08-03T00:30:00Z",
            "stored": "2026-08-03T00:30:01Z",
        },
        parser_version="toss-current-price-v1",
        **extra,
    )


def receipt(**extra):
    return ReceiptV1(
        1,
        JobKind.QUOTES,
        AttemptV1(41, AttemptStatus.SKIPPED, "skipped_market_closed", NOW),
        None,
        {"US": MarketFreshness("US", "closed", None, None)},
        None,
        **extra,
    )


def test_legacy_pack_bytes_do_not_mention_mask_digest():
    legacy = pack()
    assert legacy.mask_digest is None
    assert b"mask_digest" not in canonical_pack_file_bytes(legacy)


def test_masked_pack_carries_digest_and_changes_content_hash():
    masked = pack(mask_digest=MASK_DIGEST)
    assert masked.mask_digest == MASK_DIGEST
    assert b"mask_digest" in canonical_pack_file_bytes(masked)
    assert masked.content_hash != pack().content_hash


def test_pack_rejects_malformed_mask_digest():
    with pytest.raises(ValueError):
        pack(mask_digest="not-a-digest")
    with pytest.raises(ValueError):
        pack(mask_digest="AB" * 32)  # canonical digests are lowercase hex


def test_pack_store_round_trips_mask_digest(tmp_path):
    writer = MaterialPackWriter(tmp_path, clock=lambda: NOW)
    masked = pack(mask_digest=MASK_DIGEST)
    path = writer.write(masked)
    loaded = writer.read_and_verify(
        path,
        masked.content_hash
        and __import__("hashlib").sha256(canonical_pack_file_bytes(masked)).hexdigest(),
    )
    assert loaded.mask_digest == MASK_DIGEST
    assert loaded.content_hash == masked.content_hash


def test_pack_store_round_trips_legacy_pack(tmp_path):
    writer = MaterialPackWriter(tmp_path, clock=lambda: NOW)
    legacy = pack()
    path = writer.write(legacy)
    digest = __import__("hashlib").sha256(canonical_pack_file_bytes(legacy)).hexdigest()
    assert writer.read_and_verify(path, digest).mask_digest is None


def test_legacy_receipt_bytes_do_not_mention_mask_digest():
    legacy = receipt()
    assert legacy.mask_digest is None
    assert b"mask_digest" not in canonical_receipt_bytes(legacy)


def test_masked_receipt_round_trips_through_store(tmp_path):
    masked = receipt(mask_digest=MASK_DIGEST)
    assert b"mask_digest" in canonical_receipt_bytes(masked)
    store = ReceiptStore(tmp_path / "quotes.json", clock=lambda: NOW)
    store.replace(masked)
    loaded = store.read()
    assert loaded.mask_digest == MASK_DIGEST
    assert canonical_receipt_bytes(loaded) == canonical_receipt_bytes(masked)


def test_legacy_receipt_round_trips_through_store(tmp_path):
    store = ReceiptStore(tmp_path / "quotes.json", clock=lambda: NOW)
    store.replace(receipt())
    assert store.read().mask_digest is None


def test_receipt_rejects_malformed_mask_digest():
    with pytest.raises(ValueError):
        receipt(mask_digest="zz" * 32)
