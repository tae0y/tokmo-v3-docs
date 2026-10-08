import hashlib
import os
from datetime import datetime, timezone

import pytest

from tokmo.values.material_pack import (
    JobKind,
    build_material_pack_v1,
    canonical_decode,
    canonical_encode,
    canonical_pack_bytes,
    canonical_pack_file_bytes,
)
from tokmo.records.material_pack_store import (
    MaterialPackWriter,
    PackCorruptError,
    PackHashMismatchError,
    PackIdentityConflictError,
)
from tokmo.records.ops_paths import confine_to_parent
from tokmo.records.receipt_store import ReceiptStore
from tokmo.values.receipt_v1 import (
    AttemptStatus,
    AttemptV1,
    LastSuccessV1,
    MarketFreshness,
    ReceiptV1,
)


NOW = datetime(2026, 7, 30, 1, 2, 3, 456789, tzinfo=timezone.utc)


def make_pack(*, tick_id=17, payload=None):
    return build_material_pack_v1(
        schema_version=1,
        job=JobKind.QUOTES,
        tick_id=tick_id,
        market_partition=("KR",),
        payload=payload or {"markets": {"KR": []}},
        captured_at=NOW,
        source_timestamps={"provider": None, "fetched": "2026-07-30T01:02:03Z", "stored": None},
        parser_version="calendar-v1",
    )


def saved_receipt_for_pack(tick_id, path=None):
    pack_path = f"packs/quotes/{tick_id}.json"
    pack_sha = "a" * 64
    if path is not None:
        pack_path = str(path)
        pack_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return ReceiptV1(
        1, JobKind.QUOTES,
        AttemptV1(tick_id, AttemptStatus.SAVED, "saved", NOW),
        LastSuccessV1(tick_id, pack_path, pack_sha, NOW, NOW),
        {"KR": MarketFreshness("KR", "fresh", tick_id, NOW)}, None,
    )


def test_canonical_codec_is_closed_bounded_and_byte_stable():
    value = {"한글": True, "n": 3, "at": NOW, "items": ("x", None)}
    encoded = canonical_encode(value)
    assert encoded == (
        b'{"at":"2026-07-30T01:02:03.456789Z","items":["x",null],"n":3,'
        b'"\xed\x95\x9c\xea\xb8\x80":true}'
    )
    assert canonical_encode(canonical_decode(encoded, max_bytes=len(encoded))) == encoded

    for rejected in (1.0, b"x", bytearray(b"x"), complex(1), {1}, frozenset({1}), NOW.replace(tzinfo=None), {1: "x"}):
        with pytest.raises((TypeError, ValueError)):
            canonical_encode(rejected)
    with pytest.raises(ValueError, match="duplicate"):
        canonical_decode(b'{"a":1,"a":2}', max_bytes=100)
    with pytest.raises(ValueError, match="max_bytes"):
        canonical_decode(b"null", max_bytes=3)
    with pytest.raises(ValueError, match="depth"):
        canonical_decode(b"[" * 65 + b"null" + b"]" * 65, max_bytes=1000)
    with pytest.raises(ValueError, match="list"):
        canonical_decode(canonical_encode([None] * 4097), max_bytes=30_000)
    with pytest.raises(ValueError, match="string"):
        canonical_decode(canonical_encode("x" * 65537), max_bytes=70_000)


def test_canonical_encoder_rejects_objects_the_decoder_would_reject():
    with pytest.raises(ValueError, match="string"):
        canonical_encode("x" * 65537)
    with pytest.raises(ValueError, match="list"):
        canonical_encode([None] * 4097)
    nested = None
    for _ in range(65):
        nested = [nested]
    with pytest.raises(ValueError, match="depth"):
        canonical_encode(nested)


def test_material_pack_factory_rejects_final_file_over_pack_limit():
    maximum_strings = ["x" * 65536] * 257
    payload = {
        "markets": {
            "KR": [
                {
                    "symbol": symbol, "last_price": "1", "currency_raw": "KRW",
                    "provider_timestamp": None,
                    "fetched_at": "2026-07-30T01:02:03Z",
                    "stored_at": "2026-07-30T01:02:03Z",
                }
                for symbol in maximum_strings
            ]
        }
    }
    with pytest.raises(ValueError, match="PACK_MAX_BYTES"):
        make_pack(payload=payload)


def test_canonical_decode_converts_json_recursion_to_value_error():
    raw = b"[" * 10_000 + b"null" + b"]" * 10_000
    with pytest.raises(ValueError, match="invalid canonical JSON"):
        canonical_decode(raw, max_bytes=len(raw))


def test_material_pack_semantic_hash_excludes_hash_but_file_hash_includes_it():
    pack = make_pack()
    assert hashlib.sha256(canonical_pack_bytes(pack)).hexdigest() == pack.content_hash
    file_bytes = canonical_pack_file_bytes(pack)
    assert b'"content_hash":"' in file_bytes
    assert hashlib.sha256(file_bytes).hexdigest() != pack.content_hash
    with pytest.raises(Exception):
        pack.content_hash = "0" * 64


def test_write_once_verify_list_retention_and_path_safety(tmp_path):
    writer = MaterialPackWriter(tmp_path, clock=lambda: NOW)
    first = make_pack(tick_id=1)
    path = writer.write(first)
    original = path.read_bytes()
    assert writer.write(first) == path
    assert path.read_bytes() == original
    assert os.stat(path).st_mode & 0o777 == 0o600

    with pytest.raises(PackIdentityConflictError):
        writer.write(make_pack(tick_id=1, payload={"markets": {"US": []}}))
    assert path.read_bytes() == original

    file_sha = hashlib.sha256(original).hexdigest()
    assert writer.read_and_verify(path, file_sha) == first
    with pytest.raises(PackHashMismatchError):
        writer.read_and_verify(path, "0" * 64)

    for tick_id in range(2, 15):
        writer.write(make_pack(tick_id=tick_id))
    listed = writer.list_packs(JobKind.QUOTES)
    assert [item[0] for item in listed] == list(range(1, 15))
    keep_top_12 = frozenset(range(3, 15))
    assert writer.reap_orphans(JobKind.QUOTES, keep_top_12, frozenset({1})) == 1
    assert [item[0] for item in writer.list_packs(JobKind.QUOTES)] == [1, *range(3, 15)]

    with pytest.raises(ValueError):
        confine_to_parent(tmp_path / ".." / "escape", tmp_path)
    symlink = path.parent / "99.json"
    symlink.symlink_to(path)
    with pytest.raises(Exception):
        writer.list_packs(JobKind.QUOTES)


def test_delayed_orphan_is_retained_inside_top_12_then_removed_outside_window(tmp_path):
    writer = MaterialPackWriter(tmp_path, clock=lambda: NOW)
    for tick_id in range(1, 14):
        writer.write(make_pack(tick_id=tick_id))
    orphan_tick = 2
    top_12 = frozenset(range(2, 14))
    assert writer.reap_orphans(JobKind.QUOTES, top_12, frozenset()) == 1
    assert (tmp_path / "packs" / "quotes" / f"{orphan_tick}.json").exists()

    writer.write(make_pack(tick_id=14))
    next_top_12 = frozenset(range(3, 15))
    assert writer.reap_orphans(JobKind.QUOTES, next_top_12, frozenset()) == 1
    assert not (tmp_path / "packs" / "quotes" / f"{orphan_tick}.json").exists()


def test_temp_only_is_not_visible_and_receipt_controls_pack_visibility(tmp_path):
    writer = MaterialPackWriter(tmp_path, clock=lambda: NOW)
    directory = tmp_path / "packs" / "quotes"
    directory.mkdir(parents=True)
    temp = directory / ".21.json.0123456789abcdef.tmp"
    temp.write_bytes(b"partial")
    assert writer.list_packs(JobKind.QUOTES) == []
    assert not (directory / "21.json").exists()

    receipt_path = tmp_path / "receipts" / "quotes.json"
    receipt_store = ReceiptStore(receipt_path, clock=lambda: NOW)
    previous = saved_receipt_for_pack(1)
    receipt_store.replace(previous)
    new_pack = writer.write(make_pack(tick_id=22))
    assert receipt_store.read().last_success.tick_id == 1
    receipt_store.replace(saved_receipt_for_pack(22, new_pack))
    assert receipt_store.read().last_success.tick_id == 22


def test_pack_exact_types_and_reader_root_confinement(tmp_path):
    with pytest.raises(ValueError):
        build_material_pack_v1(
            schema_version=True, job=JobKind.QUOTES, tick_id=1,
            market_partition=("KR",), payload={"markets": {"KR": []}},
            captured_at=NOW,
            source_timestamps={"provider": None, "fetched": None, "stored": None},
            parser_version="v1",
        )
    with pytest.raises(ValueError):
        make_pack(payload={"markets": {"KR": [{
            "symbol": 123, "last_price": "1", "currency_raw": "KRW",
            "provider_timestamp": None, "fetched_at": "2026-07-30T01:02:03Z",
            "stored_at": "2026-07-30T01:02:03Z",
        }]}})
    with pytest.raises(ValueError):
        make_pack(payload={"markets": {"KR": [{
            "symbol": "005930", "last_price": "1", "currency_raw": "KRW",
            "provider_timestamp": None, "fetched_at": "2026-07-30T01:02:03",
            "stored_at": "2026-07-30T01:02:03Z",
        }]}})

    writer = MaterialPackWriter(tmp_path / "owned", clock=lambda: NOW)
    outside = tmp_path / "outside.json"
    outside.write_bytes(canonical_pack_file_bytes(make_pack()))
    with pytest.raises(Exception):
        writer.read_and_verify(outside, hashlib.sha256(outside.read_bytes()).hexdigest())

    inside = writer.write(make_pack(tick_id=18))
    symlink = inside.parent / "19.json"
    symlink.symlink_to(inside)
    with pytest.raises(PackCorruptError):
        writer.read_and_verify(symlink, hashlib.sha256(inside.read_bytes()).hexdigest())


def test_material_pack_reader_maps_deep_json_recursion_to_typed_corrupt(tmp_path):
    writer = MaterialPackWriter(tmp_path, clock=lambda: NOW)
    path = tmp_path / "packs" / "quotes" / "20.json"
    path.parent.mkdir(parents=True)
    raw = b"[" * 10_000 + b"null" + b"]" * 10_000
    path.write_bytes(raw)
    with pytest.raises(PackCorruptError):
        writer.read_and_verify(path, hashlib.sha256(raw).hexdigest())
