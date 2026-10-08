"""Typed latest-only current-holdings projection."""

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from tokmo.values.account_scope_patterns import (  # N1: import from leaf
    ACCOUNT_SCOPE_RE,
    PENSION_SCOPE_RE,
)
from tokmo.values.material_pack import canonical_encode
from tokmo.values.session_gate import Market

CURRENT_HOLDINGS_SCHEMA_VERSION = 1
CLASSIFY_MARKET_VERSION = 1
KR_OBSERVED_VALUES = frozenset({"KR"})
US_OBSERVED_VALUES = frozenset({"US"})


class AccountScopeCollisionError(ValueError):
    code = "account_scope_collision"


class AccountScopeDuplicateError(ValueError):
    code = "account_scope_duplicate"


def _scope(value: str) -> None:
    # B1 repair: accept both toss-acct-* and pension-excel-* scopes
    if type(value) is not str:
        raise ValueError("invalid account_scope")
    if (ACCOUNT_SCOPE_RE.fullmatch(value) is None
            and PENSION_SCOPE_RE.fullmatch(value) is None):
        raise ValueError("invalid account_scope")


@dataclass(frozen=True)
class AccountScopeIdentityV1:
    account_scope: str
    account_type_label: str | None = field(repr=False)
    ordinal: int
    masked_name: str | None = field(repr=False)

    def __post_init__(self):
        _scope(self.account_scope)
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValueError("invalid ordinal")
        if self.account_type_label is not None and type(self.account_type_label) is not str:
            raise ValueError("invalid account_type_label")
        if self.masked_name is not None and type(self.masked_name) is not str:
            raise ValueError("invalid masked_name")


@dataclass(frozen=True)
class HoldingItemV1:
    symbol: str
    quantity: str
    avg_price: str
    currency: str | None
    market_raw: str | None
    market_field: str | None

    def __post_init__(self):
        if not all(type(value) is str and value for value in
                   (self.symbol, self.quantity, self.avg_price)):
            raise ValueError("invalid holding")
        if any(value is not None and type(value) is not str for value in
               (self.currency, self.market_raw, self.market_field)):
            raise ValueError("invalid holding metadata")


@dataclass(frozen=True)
class AccountHoldingsV1:
    account_scope: str
    ordinal: int
    holdings: tuple[HoldingItemV1, ...]

    def __post_init__(self):
        _scope(self.account_scope)
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise ValueError("invalid ordinal")
        if type(self.holdings) is not tuple or not all(
                isinstance(item, HoldingItemV1) for item in self.holdings):
            raise ValueError("invalid holdings")


@dataclass(frozen=True)
class CurrentHoldingsV1:
    schema_version: int
    accounts: tuple[AccountHoldingsV1, ...]
    global_inventory: bool
    captured_at: datetime
    identity_map: Mapping[str, AccountScopeIdentityV1]

    def __post_init__(self):
        if self.schema_version != CURRENT_HOLDINGS_SCHEMA_VERSION:
            raise ValueError("unsupported current holdings")
        if type(self.accounts) is not tuple or not self.accounts:
            raise ValueError("accounts must be non-empty")
        if self.global_inventory is not True:
            raise ValueError("current holdings must be global inventory")
        if (not isinstance(self.captured_at, datetime)
                or self.captured_at.tzinfo is None
                or self.captured_at.utcoffset() is None):
            raise ValueError("captured_at must be aware")
        if not all(isinstance(account, AccountHoldingsV1)
                   for account in self.accounts):
            raise ValueError("accounts must contain AccountHoldingsV1")
        scopes = [account.account_scope for account in self.accounts]
        if len(set(scopes)) != len(scopes):
            raise ValueError("duplicate account_scope")
        if [account.ordinal for account in self.accounts] != list(range(1, len(self.accounts) + 1)):
            raise ValueError("ordinals must be contiguous")
        if set(self.identity_map) != set(scopes):
            raise ValueError("identity map mismatch")
        for scope, identity in self.identity_map.items():
            if not isinstance(identity, AccountScopeIdentityV1) or identity.account_scope != scope:
                raise ValueError("invalid identity map")
        object.__setattr__(self, "identity_map", MappingProxyType(dict(self.identity_map)))


def classify_market(market_raw: str | None, market_field: str | None) -> Market | None:
    del market_field
    if market_raw in KR_OBSERVED_VALUES:
        return Market.KR
    if market_raw in US_OBSERVED_VALUES:
        return Market.US
    return None


def holdings_dict(state: CurrentHoldingsV1) -> dict:
    return {
        "schema_version": state.schema_version,
        "accounts": [{
            "account_scope": account.account_scope,
            "ordinal": account.ordinal,
            "holdings": [{
                "symbol": item.symbol, "quantity": item.quantity,
                "avg_price": item.avg_price, "currency": item.currency,
                "market_raw": item.market_raw, "market_field": item.market_field,
            } for item in account.holdings],
        } for account in state.accounts],
        "global_inventory": state.global_inventory,
        "captured_at": state.captured_at,
        "identity_map": {
            scope: {
                "account_scope": identity.account_scope,
                "account_type_label": identity.account_type_label,
                "ordinal": identity.ordinal,
                "masked_name": identity.masked_name,
            } for scope, identity in state.identity_map.items()
        },
    }


def canonical_holdings_bytes(state: CurrentHoldingsV1) -> bytes:
    return canonical_encode(holdings_dict(state))
