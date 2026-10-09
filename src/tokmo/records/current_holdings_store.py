"""Atomic latest-only CurrentHoldings storage with private collision metadata."""

import re
from datetime import datetime
from pathlib import Path

from tokmo.records.atomic_json import read_atomic_json, write_atomic_json
from tokmo.values.account_scope_patterns import PENSION_SCOPE_RE
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeCollisionError,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
    canonical_holdings_bytes,
)
from tokmo.values.material_pack import canonical_decode, canonical_encode

CURRENT_HOLDINGS_MAX_BYTES = 4 * 1024 * 1024
IDENTITY_MAP_MAX_BYTES = 64 * 1024
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class CurrentHoldingsCorruptError(ValueError):
    pass


def _exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError("exact keys required")


def _aware(value):
    if type(value) is not str:
        raise ValueError("timestamp must be string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp must be aware")
    return result


def _decode_state(raw: bytes) -> CurrentHoldingsV1:
    value = canonical_decode(raw, CURRENT_HOLDINGS_MAX_BYTES)
    _exact(
        value,
        {
            "schema_version",
            "accounts",
            "global_inventory",
            "captured_at",
            "identity_map",
        },
    )
    accounts = []
    for account in value["accounts"]:
        _exact(account, {"account_scope", "ordinal", "holdings"})
        holdings = []
        for item in account["holdings"]:
            _exact(
                item,
                {
                    "symbol",
                    "quantity",
                    "avg_price",
                    "currency",
                    "market_raw",
                    "market_field",
                },
            )
            holdings.append(HoldingItemV1(**item))
        accounts.append(
            AccountHoldingsV1(
                account["account_scope"],
                account["ordinal"],
                tuple(holdings),
            )
        )
    identities = {}
    for scope, identity in value["identity_map"].items():
        _exact(
            identity, {"account_scope", "account_type_label", "ordinal", "masked_name"}
        )
        identities[scope] = AccountScopeIdentityV1(**identity)
    state = CurrentHoldingsV1(
        value["schema_version"],
        tuple(accounts),
        value["global_inventory"],
        _aware(value["captured_at"]),
        identities,
    )
    if canonical_holdings_bytes(state) != raw:
        raise ValueError("non-canonical current holdings")
    return state


class CurrentHoldingsStore:
    def __init__(self, path: Path, *, clock):
        self.path = Path(path)
        self.clock = clock
        self.identity_path = self.path.with_name("current_holdings.identity.json")

    def read(self) -> CurrentHoldingsV1 | None:
        if not self.path.exists() and not self.path.is_symlink():
            return None
        try:
            return _decode_state(
                read_atomic_json(
                    self.path,
                    max_bytes=CURRENT_HOLDINGS_MAX_BYTES,
                )
            )
        except (OSError, TypeError, ValueError) as error:
            raise CurrentHoldingsCorruptError("corrupt current holdings") from error

    def _read_identities(self):
        if not self.identity_path.exists() and not self.identity_path.is_symlink():
            return {}
        try:
            raw = read_atomic_json(
                self.identity_path,
                max_bytes=IDENTITY_MAP_MAX_BYTES,
            )
            value = canonical_decode(raw, IDENTITY_MAP_MAX_BYTES)
            if type(value) is not dict:
                raise ValueError("identity map must be object")
            for scope, row in value.items():
                AccountScopeIdentityV1(scope, None, 1, None)
                if scope.startswith(
                    "pension-excel-"
                ) and not PENSION_SCOPE_RE.fullmatch(scope):
                    raise ValueError("invalid pension identity scope")
                _exact(
                    row,
                    {
                        "raw_identity_fingerprint",
                        "account_type_label",
                        "masked_name",
                        "latest_ordinal",
                    },
                )
                fingerprint = row["raw_identity_fingerprint"]
                if (
                    type(fingerprint) is not str
                    or _FINGERPRINT_RE.fullmatch(fingerprint) is None
                ):
                    raise ValueError("invalid identity fingerprint")
                if (
                    row["account_type_label"] is not None
                    and type(row["account_type_label"]) is not str
                ):
                    raise ValueError("invalid account_type_label")
                if (
                    row["masked_name"] is not None
                    and type(row["masked_name"]) is not str
                ):
                    raise ValueError("invalid masked_name")
                if type(row["latest_ordinal"]) is not int or row["latest_ordinal"] < 1:
                    raise ValueError("invalid latest_ordinal")
            if canonical_encode(value) != raw:
                raise ValueError("non-canonical identity map")
            return value
        except (OSError, TypeError, ValueError) as error:
            raise CurrentHoldingsCorruptError(
                "corrupt current holdings identity map"
            ) from error

    def replace(self, state: CurrentHoldingsV1, *, identity_fingerprints) -> None:
        if not isinstance(state, CurrentHoldingsV1):
            raise ValueError("state must be CurrentHoldingsV1")
        if set(identity_fingerprints) != set(state.identity_map):
            raise ValueError("identity fingerprints mismatch")
        prior = self._read_identities()
        updated = dict(prior)
        for scope, identity in state.identity_map.items():
            fingerprint = identity_fingerprints[scope]
            if (
                type(fingerprint) is not str
                or _FINGERPRINT_RE.fullmatch(fingerprint) is None
            ):
                raise ValueError("invalid identity fingerprint")
            old = prior.get(scope)
            if old is not None and old["raw_identity_fingerprint"] != fingerprint:
                raise AccountScopeCollisionError("account scope collision")
            updated[scope] = {
                "raw_identity_fingerprint": fingerprint,
                "account_type_label": identity.account_type_label,
                "masked_name": identity.masked_name,
                "latest_ordinal": identity.ordinal,
            }
        write_atomic_json(
            self.identity_path,
            canonical_encode(updated),
            max_bytes=IDENTITY_MAP_MAX_BYTES,
        )
        write_atomic_json(
            self.path,
            canonical_holdings_bytes(state),
            max_bytes=CURRENT_HOLDINGS_MAX_BYTES,
        )
