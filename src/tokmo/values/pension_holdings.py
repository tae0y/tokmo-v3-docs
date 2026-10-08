"""Typed pension holdings DTOs, canonical normalization, and digests.

N1: imports PENSION_SCOPE_RE from leaf account_scope_patterns.py.
m1: reuses HoldingItemV1 from current_holdings.py (no duplicate).
B3: account scope is revision-independent (derived from durable registry identity).
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from tokmo.values.account_scope_patterns import PENSION_SCOPE_RE  # N1: leaf
from tokmo.values.current_holdings import HoldingItemV1
from tokmo.values.material_pack import canonical_encode
from tokmo.values.portfolio_scope import (
    AccountScopeResolver,
    PortfolioScope,
    ScopeUnresolved,
)
from tokmo.values.account_scope_patterns import ACCOUNT_SCOPE_RE

PENSION_SOURCE = "pension-excel-v1"
PENSION_HOLDINGS_SCHEMA_VERSION = 1
PENSION_PARSER_VERSION = "pension-excel-v1"

# Durable, revision-independent registry identity (B3 repair).
PENSION_REGISTRY_IDENTITY = "tokmo-v3:pension-registry:v1:fixed"


def pension_account_scope() -> str:
    """Domain-separated, revision-independent account scope (B3 repair).

    account_scope = 'pension-excel-' + sha256(
        'tokmo-v3:pension-scope:v1:' + PENSION_REGISTRY_IDENTITY
    )[:24]

    This value is STABLE across all approved revisions so that subject
    identity and baseline comparison work correctly.
    """
    digest = hashlib.sha256(
        f"tokmo-v3:pension-scope:v1:{PENSION_REGISTRY_IDENTITY}".encode()
    ).hexdigest()[:24]
    return f"pension-excel-{digest}"


def pension_raw_identity_fingerprint() -> str:
    """64-hex fingerprint for the identity map. Same domain separation."""
    return hashlib.sha256(
        f"tokmo-v3:pension-identity-v1:{PENSION_REGISTRY_IDENTITY}".encode()
    ).hexdigest()


@dataclass(frozen=True)
class PensionAccountScopeIdentityV1:
    account_scope: str       # pension_account_scope() — STABLE across revisions
    account_type_label: str  # always "pension-excel-v1"
    ordinal: int             # assigned by composite builder: len(toss_accounts) + 1


def pension_scope_identity(ordinal: int) -> PensionAccountScopeIdentityV1:
    """Revision-independent identity."""
    return PensionAccountScopeIdentityV1(
        account_scope=pension_account_scope(),
        account_type_label="pension-excel-v1",
        ordinal=ordinal,
    )


@dataclass(frozen=True)
class PensionHoldingsRevisionV1:
    revision_id: str
    schema_version: int
    source_content_sha256: str = field(repr=False)
    parser_version: str
    sheet_schema_version: int
    holdings: tuple[HoldingItemV1, ...] = field(repr=False)
    source_as_of: datetime = field(repr=False)
    uploaded_at: datetime = field(repr=False)
    validated_at: datetime = field(repr=False)
    semantic_digest: str = field(repr=False)
    reconciliation_digest: str = field(repr=False)
    holding_count: int
    liquidation_flag: int

    def __post_init__(self):
        if len(self.revision_id) != 32 or not all(
                c in "0123456789abcdef" for c in self.revision_id):
            raise ValueError("invalid revision_id")
        if self.schema_version != PENSION_HOLDINGS_SCHEMA_VERSION:
            raise ValueError("invalid schema_version")
        if len(self.source_content_sha256) != 64:
            raise ValueError("invalid source_content_sha256")
        if not isinstance(self.holdings, tuple) or not all(
                isinstance(h, HoldingItemV1) for h in self.holdings):
            raise ValueError("invalid holdings")
        if len(self.holdings) != self.holding_count:
            raise ValueError("holding_count mismatch")
        if self.holding_count < 0:
            raise ValueError("holding_count must be >= 0")
        if self.liquidation_flag not in (0, 1):
            raise ValueError("invalid liquidation_flag")
        if self.liquidation_flag == 1 and self.holding_count != 0:
            raise ValueError("liquidation requires empty holdings")
        if len(self.semantic_digest) != 64:
            raise ValueError("invalid semantic_digest")
        if len(self.reconciliation_digest) != 64:
            raise ValueError("invalid reconciliation_digest")
        for ts_name in ("source_as_of", "uploaded_at", "validated_at"):
            ts = getattr(self, ts_name)
            if not isinstance(ts, datetime) or ts.utcoffset() is None:
                raise ValueError(f"invalid {ts_name}")
        if self.parser_version != PENSION_PARSER_VERSION:
            raise ValueError("invalid parser_version")


@dataclass(frozen=True)
class PensionFreshnessV1:
    revision_id: str
    source_as_of: datetime
    source_age_days: int
    freshness_class: str    # "CURRENT_MANUAL_SOURCE" or "AGED_MANUAL_SOURCE"


def classify_pension_freshness(
    source_as_of: datetime, tick_now: datetime,
) -> PensionFreshnessV1:
    kst = ZoneInfo("Asia/Seoul")
    age = (tick_now.astimezone(kst).date()
           - source_as_of.astimezone(kst).date()).days
    freshness = "CURRENT_MANUAL_SOURCE" if age <= 30 else "AGED_MANUAL_SOURCE"
    return PensionFreshnessV1(
        revision_id="",
        source_as_of=source_as_of,
        source_age_days=age,
        freshness_class=freshness,
    )


def pension_semantic_digest(holdings: tuple[HoldingItemV1, ...]) -> str:
    obj = {
        "v": 1,
        "domain": "tokmo-v3:pension-holdings-semantic:v1",
        "holdings": [
            {
                "symbol": h.symbol, "quantity": h.quantity,
                "avg_price": h.avg_price, "currency": h.currency,
                "market_raw": h.market_raw,
            }
            for h in sorted(holdings, key=lambda h: (
                h.market_raw or "", h.symbol, h.currency or "",
            ))
        ],
    }
    return hashlib.sha256(canonical_encode(obj)).hexdigest()


def pension_reconciliation_digest(
    holdings_with_names: tuple,
    evaluation_amounts: dict[str, str] | None = None,
    current_prices: dict[str, str] | None = None,
) -> str:
    """PRIVATE: never projected to any public DTO, payload, or log."""
    obj = {
        "v": 1,
        "domain": "tokmo-v3:pension-reconciliation:v1",
        "holdings": [
            {
                "symbol": h.symbol, "quantity": h.quantity,
                "avg_price": h.avg_price, "currency": h.currency,
                "market_raw": h.market_raw,
                "instrument_name": name,
                "evaluation_amount": (evaluation_amounts or {}).get(h.symbol),
                "current_price": (current_prices or {}).get(h.symbol),
            }
            for h, name in sorted(holdings_with_names, key=lambda hn: (
                hn[0].market_raw or "", hn[0].symbol, hn[0].currency or "",
            ))
        ],
    }
    return hashlib.sha256(canonical_encode(obj)).hexdigest()


def make_composite_resolver(
    private_scope_map: dict,
) -> AccountScopeResolver:
    """Resolve both toss-acct-* and pension-excel-* scopes."""
    def resolver(raw_scope: str):
        if ACCOUNT_SCOPE_RE.fullmatch(raw_scope):
            return private_scope_map.get(raw_scope, ScopeUnresolved.UNRESOLVED)
        if PENSION_SCOPE_RE.fullmatch(raw_scope):
            return PortfolioScope.PENSION
        return ScopeUnresolved.UNRESOLVED
    return resolver
