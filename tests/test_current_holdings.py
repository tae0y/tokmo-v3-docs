from datetime import datetime, timezone

import pytest

from tokmo.facts.toss import (
    _derive_account_identities_from_seqs,
    derive_account_scope,
    derive_raw_identity_fingerprint,
)
from tokmo.records.current_holdings_store import (
    AccountScopeCollisionError,
    CurrentHoldingsCorruptError,
    CurrentHoldingsStore,
)
from tokmo.values.current_holdings import (
    CLASSIFY_MARKET_VERSION,
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
    canonical_holdings_bytes,
    classify_market,
)
from tokmo.values.receipt_v1 import ACCOUNT_SCOPE_RE
from tokmo.values.session_gate import Market


NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def test_identity_derivation_is_deterministic_domain_separated_and_order_independent():
    seqs = ("123", "456")
    scopes = {derive_account_scope(value) for value in seqs}
    assert scopes == {derive_account_scope(value) for value in reversed(seqs)}
    assert len(scopes) == 2
    assert all(ACCOUNT_SCOPE_RE.fullmatch(value) for value in scopes)
    fingerprints = [derive_raw_identity_fingerprint(value) for value in seqs]
    assert all(len(value) == 64 for value in fingerprints)
    assert fingerprints[0] != fingerprints[1]
    assert derive_account_scope("123").removeprefix("toss-acct-") != fingerprints[0][:24]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("KR", Market.KR), ("US", Market.US), ("UNKNOWN", None),
     (None, None), ("KOREA", None), ("NASDAQ", None)],
)
def test_market_classifier_is_observed_only(raw, expected):
    assert CLASSIFY_MARKET_VERSION == 1
    assert classify_market(raw, "market" if raw else None) is expected


def test_current_holdings_is_frozen_validated_and_canonical():
    scope = derive_account_scope("123")
    identity = AccountScopeIdentityV1(scope, None, 1, None)
    item = HoldingItemV1("005930", "1", "70000", "KRW", "KR", "market")
    state = CurrentHoldingsV1(
        1, (AccountHoldingsV1(scope, 1, (item,)),), True, NOW,
        {scope: identity},
    )
    encoded = canonical_holdings_bytes(state)
    assert encoded == (
        b'{"accounts":[{"account_scope":"' + scope.encode() +
        b'","holdings":[{"avg_price":"70000","currency":"KRW",'
        b'"market_field":"market","market_raw":"KR","quantity":"1",'
        b'"symbol":"005930"}],"ordinal":1}],"captured_at":'
        b'"2026-07-30T00:00:00Z","global_inventory":true,"identity_map":{"' +
        scope.encode() + b'":{"account_scope":"' + scope.encode() +
        b'","account_type_label":null,"masked_name":null,"ordinal":1}},'
        b'"schema_version":1}'
    )
    with pytest.raises(Exception):
        state.global_inventory = False


def test_account_identity_repr_contains_only_scope_and_ordinal():
    scope = derive_account_scope("123")
    rendered = repr(AccountScopeIdentityV1(scope, "private-label", 1, "***123"))
    assert scope in rendered and "ordinal=1" in rendered
    assert "private-label" not in rendered
    assert "***123" not in rendered
    assert "account_type_label" not in rendered
    assert "masked_name" not in rendered


def _state(seq="123", ordinal=1):
    scope = derive_account_scope(seq)
    identity = AccountScopeIdentityV1(scope, None, ordinal, None)
    account = AccountHoldingsV1(scope, ordinal, ())
    return CurrentHoldingsV1(1, (account,), True, NOW, {scope: identity})


def test_store_replaces_latest_and_reads_typed_with_private_0600_identity_map(tmp_path):
    path = tmp_path / "toss" / "current_holdings.json"
    store = CurrentHoldingsStore(path, clock=lambda: NOW)
    state = _state()
    scope = state.accounts[0].account_scope
    store.replace(state, identity_fingerprints={
        scope: derive_raw_identity_fingerprint("123"),
    })
    assert store.read() == state
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.with_name("current_holdings.identity.json").stat().st_mode & 0o777 == 0o600


def test_store_collision_is_fingerprint_only_and_fail_closed(tmp_path):
    path = tmp_path / "current_holdings.json"
    store = CurrentHoldingsStore(path, clock=lambda: NOW)
    state = _state()
    scope = state.accounts[0].account_scope
    good = derive_raw_identity_fingerprint("123")
    store.replace(state, identity_fingerprints={scope: good})
    before = path.read_bytes()
    identity_path = path.with_name("current_holdings.identity.json")
    identity_before = identity_path.read_bytes()
    with pytest.raises(AccountScopeCollisionError):
        store.replace(state, identity_fingerprints={scope: "f" * 64})
    assert path.read_bytes() == before
    assert identity_path.read_bytes() == identity_before


def test_store_allows_actual_ordinal_reorder_and_updates_latest_ordinal(tmp_path):
    path = tmp_path / "current_holdings.json"
    store = CurrentHoldingsStore(path, clock=lambda: NOW)
    scopes = [derive_account_scope(seq) for seq in ("123", "456")]
    fingerprints = {
        scope: derive_raw_identity_fingerprint(seq)
        for scope, seq in zip(scopes, ("123", "456"))
    }
    def state(order):
        identities = {
            scope: AccountScopeIdentityV1(scope, None, ordinal, None)
            for ordinal, scope in enumerate(order, 1)
        }
        accounts = tuple(
            AccountHoldingsV1(scope, ordinal, ())
            for ordinal, scope in enumerate(order, 1)
        )
        return CurrentHoldingsV1(1, accounts, True, NOW, identities)
    store.replace(state(scopes), identity_fingerprints=fingerprints)
    reordered = state(tuple(reversed(scopes)))
    store.replace(reordered, identity_fingerprints=fingerprints)
    assert store.read() == reordered
    private = path.with_name("current_holdings.identity.json").read_text()
    assert f'"{scopes[0]}"' in private and '"latest_ordinal":2' in private


def test_identity_commit_before_state_failure_prevents_later_collision_bypass(
        tmp_path, monkeypatch):
    path = tmp_path / "current_holdings.json"
    store = CurrentHoldingsStore(path, clock=lambda: NOW)
    state = _state()
    scope = state.accounts[0].account_scope
    import tokmo.records.current_holdings_store as module
    real_write = module.write_atomic_json
    def fail_state(target, raw, **kwargs):
        if target == path:
            raise OSError("injected state crash")
        return real_write(target, raw, **kwargs)
    monkeypatch.setattr(module, "write_atomic_json", fail_state)
    with pytest.raises(OSError):
        store.replace(state, identity_fingerprints={scope: "a" * 64})
    monkeypatch.setattr(module, "write_atomic_json", real_write)
    with pytest.raises(AccountScopeCollisionError):
        store.replace(state, identity_fingerprints={scope: "b" * 64})


def test_identity_derivation_rejects_duplicate_scope_by_fingerprint():
    same_scope = lambda value: "toss-acct-" + "0" * 24
    with pytest.raises(ValueError, match="duplicate account scope"):
        _derive_account_identities_from_seqs(
            ("123", "123"), scope_deriver=same_scope,
            fingerprint_deriver=derive_raw_identity_fingerprint,
        )
    with pytest.raises(AccountScopeCollisionError):
        _derive_account_identities_from_seqs(
            ("123", "456"), scope_deriver=same_scope,
            fingerprint_deriver=derive_raw_identity_fingerprint,
        )


@pytest.mark.parametrize("captured", [None, "2026-07-30T00:00:00Z", 1])
def test_current_holdings_rejects_non_datetime_with_value_error(captured):
    scope = derive_account_scope("123")
    with pytest.raises(ValueError, match="captured_at"):
        CurrentHoldingsV1(
            1, (AccountHoldingsV1(scope, 1, ()),), True, captured,
            {scope: AccountScopeIdentityV1(scope, None, 1, None)},
        )


def test_store_rejects_non_account_and_corrupt_private_identity_typed(tmp_path):
    scope = derive_account_scope("123")
    with pytest.raises(ValueError, match="accounts"):
        CurrentHoldingsV1(
            1, (object(),), True, NOW,
            {scope: AccountScopeIdentityV1(scope, None, 1, None)},
        )
    path = tmp_path / "current_holdings.json"
    identity = path.with_name("current_holdings.identity.json")
    identity.write_bytes(b"{")
    with pytest.raises(CurrentHoldingsCorruptError):
        CurrentHoldingsStore(path, clock=lambda: NOW).replace(
            _state(), identity_fingerprints={
                scope: derive_raw_identity_fingerprint("123"),
            },
        )


def test_sixty_replacements_keep_one_state_and_bounded_identity_map(tmp_path):
    path = tmp_path / "current_holdings.json"
    store = CurrentHoldingsStore(path, clock=lambda: NOW)
    state = _state()
    scope = state.accounts[0].account_scope
    for _ in range(60):
        store.replace(state, identity_fingerprints={
            scope: derive_raw_identity_fingerprint("123"),
        })
    assert store.read() == state
    assert sorted(item.name for item in tmp_path.iterdir()) == [
        "current_holdings.identity.json", "current_holdings.json",
    ]


def test_store_preserves_corrupt_file_and_raises_typed_error(tmp_path):
    path = tmp_path / "current_holdings.json"
    path.write_bytes(b"{")
    before = path.read_bytes()
    with pytest.raises(CurrentHoldingsCorruptError):
        CurrentHoldingsStore(path, clock=lambda: NOW).read()
    assert path.read_bytes() == before
