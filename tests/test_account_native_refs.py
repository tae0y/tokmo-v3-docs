"""AccountNativeRefStoreV1: private, receipt-bound scope→ref persistence.

Plan §17.1(a): the 300s holdings owner persists the scope→native-order-ref
mapping so Pulse never calls ACCOUNTS. The raw ref is private material: the
store file is 0600, every read hands back a redacted wrapper, and no repr,
str, exception, or receipt path ever contains the raw value.
"""

from datetime import UTC, datetime

import pytest

from tokmo.records.account_native_refs import (
    AccountNativeRefStoreV1,
    NativeRefCorruptError,
    NativeRefMappingV1,
)

TOSS_SCOPE = "toss-acct-" + "a" * 24
TOSS_SCOPE_2 = "toss-acct-" + "b" * 24
PENSION_SCOPE = "pension-excel-" + "c" * 24
RAW_REF = "raw-native-ref-000111222333"
CLOCK_NOW = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
BINDING = "cadence-300s-market-portfolio-v1:5952870"


def _store(tmp_path):
    return AccountNativeRefStoreV1(
        tmp_path / "private" / "account_native_refs.sqlite3",
        clock=lambda: CLOCK_NOW,
    )


def test_roundtrip_persists_refs_bound_to_the_holdings_receipt(tmp_path):
    store = _store(tmp_path)
    store.replace_refs(
        {TOSS_SCOPE: RAW_REF, TOSS_SCOPE_2: "raw-ref-2"},
        receipt_binding=BINDING,
    )
    mapping = store.read_refs()
    assert isinstance(mapping, NativeRefMappingV1)
    assert mapping.scopes() == frozenset({TOSS_SCOPE, TOSS_SCOPE_2})
    assert mapping.receipt_binding == BINDING
    assert mapping.reveal(TOSS_SCOPE) == RAW_REF
    assert mapping.reveal(PENSION_SCOPE) is None


def test_store_file_mode_is_0600_and_parent_0700(tmp_path):
    import stat

    store = _store(tmp_path)
    store.replace_refs({TOSS_SCOPE: RAW_REF}, receipt_binding=BINDING)
    path = tmp_path / "private" / "account_native_refs.sqlite3"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_raw_ref_never_leaks_through_repr_or_str(tmp_path):
    store = _store(tmp_path)
    store.replace_refs({TOSS_SCOPE: RAW_REF}, receipt_binding=BINDING)
    mapping = store.read_refs()
    for text in (repr(mapping), str(mapping)):
        assert RAW_REF not in text
        assert "REDACTED" in text


def test_write_requires_nonempty_receipt_binding(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.replace_refs({TOSS_SCOPE: RAW_REF}, receipt_binding="")
    with pytest.raises(ValueError):
        store.replace_refs({TOSS_SCOPE: RAW_REF}, receipt_binding=None)


def test_write_rejects_pension_or_malformed_scopes_and_empty_refs(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.replace_refs({PENSION_SCOPE: RAW_REF}, receipt_binding=BINDING)
    with pytest.raises(ValueError):
        store.replace_refs({"not-a-scope": RAW_REF}, receipt_binding=BINDING)
    with pytest.raises(ValueError):
        store.replace_refs({TOSS_SCOPE: ""}, receipt_binding=BINDING)


def test_replace_is_whole_inventory_not_merge(tmp_path):
    store = _store(tmp_path)
    store.replace_refs(
        {TOSS_SCOPE: RAW_REF, TOSS_SCOPE_2: "raw-ref-2"},
        receipt_binding=BINDING,
    )
    store.replace_refs({TOSS_SCOPE: RAW_REF}, receipt_binding=BINDING)
    mapping = store.read_refs()
    assert mapping.scopes() == frozenset({TOSS_SCOPE})
    assert mapping.reveal(TOSS_SCOPE_2) is None


def test_absent_store_reads_as_empty_mapping(tmp_path):
    mapping = _store(tmp_path).read_refs()
    assert mapping.scopes() == frozenset()
    assert mapping.receipt_binding is None


def test_corrupt_store_fails_closed(tmp_path):
    store = _store(tmp_path)
    store.replace_refs({TOSS_SCOPE: RAW_REF}, receipt_binding=BINDING)
    (tmp_path / "private" / "account_native_refs.sqlite3").write_bytes(b"junk")
    with pytest.raises(NativeRefCorruptError):
        store.read_refs()
