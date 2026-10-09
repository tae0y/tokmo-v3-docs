"""Account native ref writer seam: CadenceAccountsV1 → AccountNativeRefStoreV1.

Plan §17.1(a): the 300s successful holdings stage persists the scope→native
ref mapping from the Toss ACCOUNTS response. Only live-order-capable Toss
scopes are eligible; pension/manual scopes must never be stored. The
receipt binding ties the refs to the exact holdings wire receipt that
produced them. Atomic replace means stale mappings from a prior account
rotation are fully removed.
"""

from datetime import UTC, datetime

import pytest

from tokmo.records.account_native_refs import AccountNativeRefStoreV1
from tokmo.values.production_cadence import (
    AccountScopeIdentityV1,
    CadenceAccountsV1,
)

CLOCK_NOW = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
TOSS_SCOPE = "toss-acct-" + "a" * 24
TOSS_SCOPE_2 = "toss-acct-" + "b" * 24
PENSION_SCOPE = "pension-excel-" + "c" * 24
RAW_REF_1 = "raw-native-ref-000111222333"
RAW_REF_2 = "raw-native-ref-aaa444555666"
PENSION_REF = "pension-native-ref-xyz"
BINDING = "cadence-300s-market-portfolio-v1:5952870"


def _store(tmp_path):
    return AccountNativeRefStoreV1(
        tmp_path / "private" / "account_native_refs.sqlite3",
        clock=lambda: CLOCK_NOW,
    )


def _accounts(identities, native_refs):
    return CadenceAccountsV1(
        tuple(identities),
        dict(native_refs),
        {
            identity.account_scope: "fp-" + identity.account_scope[-4:]
            for identity in identities
        },
    )


def _identity(scope, ordinal=1):
    return AccountScopeIdentityV1(scope, None, ordinal, None)


def test_toss_scopes_committed_pension_excluded(tmp_path):
    """Only Toss scopes are persisted; pension scopes are silently
    excluded — they never had native refs. The pension scope and its ref
    are included in the inventory but must not appear in the store."""
    from tokmo.programs.account_native_ref_writer import (
        write_native_refs_from_accounts,
    )

    store = _store(tmp_path)
    accounts = _accounts(
        [
            _identity(TOSS_SCOPE, 1),
            _identity(TOSS_SCOPE_2, 2),
            _identity(PENSION_SCOPE, 3),
        ],
        {TOSS_SCOPE: RAW_REF_1, TOSS_SCOPE_2: RAW_REF_2, PENSION_SCOPE: PENSION_REF},
    )
    write_native_refs_from_accounts(
        accounts=accounts,
        receipt_binding=BINDING,
        store=store,
    )
    mapping = store.read_refs()
    assert mapping.scopes() == frozenset({TOSS_SCOPE, TOSS_SCOPE_2})
    assert mapping.reveal(TOSS_SCOPE) == RAW_REF_1
    assert mapping.reveal(TOSS_SCOPE_2) == RAW_REF_2
    assert PENSION_SCOPE not in mapping.scopes()
    assert mapping.receipt_binding == BINDING


def test_account_rotation_atomic_replace(tmp_path):
    """When account inventory changes (rotation), stale mappings are fully
    removed — replace, not merge."""
    from tokmo.programs.account_native_ref_writer import (
        write_native_refs_from_accounts,
    )

    store = _store(tmp_path)
    accounts_1 = _accounts(
        [_identity(TOSS_SCOPE, 1), _identity(TOSS_SCOPE_2, 2)],
        {TOSS_SCOPE: RAW_REF_1, TOSS_SCOPE_2: RAW_REF_2},
    )
    write_native_refs_from_accounts(
        accounts=accounts_1,
        receipt_binding="old-binding-1",
        store=store,
    )
    assert store.read_refs().scopes() == frozenset({TOSS_SCOPE, TOSS_SCOPE_2})

    # Account rotation: only one account now, different ref
    TOSS_SCOPE_3 = "toss-acct-" + "d" * 24
    accounts_2 = _accounts(
        [_identity(TOSS_SCOPE_3, 1)],
        {TOSS_SCOPE_3: "new-ref-3"},
    )
    write_native_refs_from_accounts(
        accounts=accounts_2,
        receipt_binding="new-binding-2",
        store=store,
    )
    mapping = store.read_refs()
    assert mapping.scopes() == frozenset({TOSS_SCOPE_3})
    assert TOSS_SCOPE not in mapping.scopes()
    assert TOSS_SCOPE_2 not in mapping.scopes()
    assert mapping.receipt_binding == "new-binding-2"


def test_empty_accounts_fails_closed(tmp_path):
    """An empty accounts inventory must not silently clear the store.
    The writer itself (not the constructor) must raise."""
    store = _store(tmp_path)
    # CadenceAccountsV1 rejects empty identities in __post_init__, so we
    # test the writer-level fail-closed with valid-but-non-Toss inventory
    # below. Here we verify the constructor path raises.
    with pytest.raises(ValueError):
        _accounts([], {})


def test_no_toss_scope_writer_fails_closed(tmp_path):
    """An inventory with valid identities but no live-order-capable Toss
    scope must fail-closed at the writer level — not silently clear."""
    from tokmo.programs.account_native_ref_writer import (
        NativeRefWriterError,
        write_native_refs_from_accounts,
    )

    store = _store(tmp_path)
    # Pension-only inventory — valid identities, no Toss scope
    accounts = _accounts(
        [_identity(PENSION_SCOPE, 1)],
        {PENSION_SCOPE: PENSION_REF},
    )
    with pytest.raises(NativeRefWriterError):
        write_native_refs_from_accounts(
            accounts=accounts,
            receipt_binding=BINDING,
            store=store,
        )


def test_toss_scope_empty_native_ref_fails_closed(tmp_path):
    """A Toss scope with an empty native ref is a genuine anomaly — the
    writer must fail-closed, not silently skip and partial-commit."""
    from tokmo.programs.account_native_ref_writer import (
        NativeRefWriterError,
        write_native_refs_from_accounts,
    )

    store = _store(tmp_path)
    accounts = _accounts(
        [_identity(TOSS_SCOPE, 1)],
        {TOSS_SCOPE: ""},  # empty ref for a Toss scope
    )
    with pytest.raises(NativeRefWriterError):
        write_native_refs_from_accounts(
            accounts=accounts,
            receipt_binding=BINDING,
            store=store,
        )


def test_toss_scope_missing_native_ref_fails_closed(tmp_path):
    """A Toss scope with a None native ref is a genuine anomaly — the
    writer must fail-closed."""
    from tokmo.programs.account_native_ref_writer import (
        NativeRefWriterError,
        write_native_refs_from_accounts,
    )

    store = _store(tmp_path)
    accounts = _accounts(
        [_identity(TOSS_SCOPE, 1)],
        {TOSS_SCOPE: None},  # type: ignore[dict-item]
    )
    with pytest.raises(NativeRefWriterError):
        write_native_refs_from_accounts(
            accounts=accounts,
            receipt_binding=BINDING,
            store=store,
        )


def test_receipt_binding_mandatory(tmp_path):
    from tokmo.programs.account_native_ref_writer import (
        write_native_refs_from_accounts,
    )

    store = _store(tmp_path)
    accounts = _accounts(
        [_identity(TOSS_SCOPE)],
        {TOSS_SCOPE: RAW_REF_1},
    )
    with pytest.raises(ValueError):
        write_native_refs_from_accounts(
            accounts=accounts,
            receipt_binding="",
            store=store,
        )


def test_raw_ref_never_in_exception_or_repr(tmp_path):
    """Raw ref must not leak through the writer's exception paths."""
    from tokmo.programs.account_native_ref_writer import (
        NativeRefWriterError,
        write_native_refs_from_accounts,
    )

    store = _store(tmp_path)
    accounts = _accounts(
        [_identity(TOSS_SCOPE)],
        {TOSS_SCOPE: RAW_REF_1},
    )
    # Try to trigger an error — empty binding
    try:
        write_native_refs_from_accounts(
            accounts=accounts,
            receipt_binding="",
            store=store,
        )
        assert False, "should have raised"
    except (ValueError, NativeRefWriterError) as error:
        assert RAW_REF_1 not in str(error)
        assert RAW_REF_1 not in repr(error)


def test_write_after_read_roundtrip_is_idempotent(tmp_path):
    """Writing the same accounts twice produces the same store state."""
    from tokmo.programs.account_native_ref_writer import (
        write_native_refs_from_accounts,
    )

    store = _store(tmp_path)
    accounts = _accounts(
        [_identity(TOSS_SCOPE)],
        {TOSS_SCOPE: RAW_REF_1},
    )
    write_native_refs_from_accounts(
        accounts=accounts,
        receipt_binding=BINDING,
        store=store,
    )
    write_native_refs_from_accounts(
        accounts=accounts,
        receipt_binding=BINDING,
        store=store,
    )
    mapping = store.read_refs()
    assert mapping.scopes() == frozenset({TOSS_SCOPE})
    assert mapping.reveal(TOSS_SCOPE) == RAW_REF_1
