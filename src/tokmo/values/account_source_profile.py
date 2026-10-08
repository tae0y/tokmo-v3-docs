"""Per-account source routing profile (60s Pulse Plan §17.4).

The root-cause repair of the recurring ``partial_accounts``: the account
inventory is not the Toss-Orders-queryable inventory. Quote routing and
transaction routing are separate axes — every scope's symbols are Toss
quote targets, while only live-order-capable Toss scopes with a persisted
native order ref are Toss Orders scan targets. Pension Excel scopes are
``NOT_APPLICABLE`` for the live scan (never a failure) and keep the
retirement Excel import path as their transaction source.
"""

from dataclasses import dataclass
from enum import Enum

from tokmo.values.account_scope_patterns import (
    ACCOUNT_SCOPE_RE,
    PENSION_SCOPE_RE,
)
from tokmo.values.current_holdings import CurrentHoldingsV1


class HoldingsSourceV1(str, Enum):
    TOSS = "TOSS"
    PENSION_EXCEL = "PENSION_EXCEL"


class QuoteSourceV1(str, Enum):
    TOSS_CURRENT_PRICE = "TOSS_CURRENT_PRICE"


class TransactionSourceV1(str, Enum):
    TOSS_ORDERS = "TOSS_ORDERS"
    RETIREMENT_EXCEL_IMPORT = "RETIREMENT_EXCEL_IMPORT"


@dataclass(frozen=True)
class AccountSourceProfileV1:
    account_scope: str
    holdings_source: HoldingsSourceV1
    quote_source: QuoteSourceV1
    transaction_source: TransactionSourceV1
    supports_live_orders: bool
    native_order_ref_present: bool

    def __post_init__(self):
        is_toss = ACCOUNT_SCOPE_RE.fullmatch(self.account_scope) is not None
        is_pension = PENSION_SCOPE_RE.fullmatch(self.account_scope) is not None
        if not (is_toss or is_pension):
            raise ValueError("invalid account scope")
        if not isinstance(self.holdings_source, HoldingsSourceV1):
            raise ValueError("invalid holdings source")
        if self.quote_source is not QuoteSourceV1.TOSS_CURRENT_PRICE:
            raise ValueError("every scope quotes through Toss current price")
        if not isinstance(self.transaction_source, TransactionSourceV1):
            raise ValueError("invalid transaction source")
        if type(self.supports_live_orders) is not bool or type(
                self.native_order_ref_present) is not bool:
            raise ValueError("invalid capability flags")
        if is_pension:
            if self.holdings_source is not HoldingsSourceV1.PENSION_EXCEL:
                raise ValueError("pension scope requires the Excel holdings source")
            if self.transaction_source is not TransactionSourceV1.RETIREMENT_EXCEL_IMPORT:
                raise ValueError("pension transactions never route to Toss Orders")
            if self.supports_live_orders or self.native_order_ref_present:
                raise ValueError("pension scope cannot claim live-order capability")
        else:
            if self.holdings_source is not HoldingsSourceV1.TOSS:
                raise ValueError("toss scope requires the Toss holdings source")
            if self.transaction_source is not TransactionSourceV1.TOSS_ORDERS:
                raise ValueError("toss transactions route to Toss Orders")


def derive_account_source_profiles(
    holdings: CurrentHoldingsV1, *, native_ref_scopes: frozenset,
) -> tuple[AccountSourceProfileV1, ...]:
    """Pure derivation from the canonical composite holdings snapshot."""
    if not isinstance(holdings, CurrentHoldingsV1):
        raise ValueError("invalid holdings snapshot")
    if not isinstance(native_ref_scopes, frozenset):
        raise ValueError("native_ref_scopes must be a frozenset")
    profiles = []
    for account in holdings.accounts:
        scope = account.account_scope
        if PENSION_SCOPE_RE.fullmatch(scope) is not None:
            profiles.append(AccountSourceProfileV1(
                scope, HoldingsSourceV1.PENSION_EXCEL,
                QuoteSourceV1.TOSS_CURRENT_PRICE,
                TransactionSourceV1.RETIREMENT_EXCEL_IMPORT,
                False, False,
            ))
        else:
            profiles.append(AccountSourceProfileV1(
                scope, HoldingsSourceV1.TOSS,
                QuoteSourceV1.TOSS_CURRENT_PRICE,
                TransactionSourceV1.TOSS_ORDERS,
                True, scope in native_ref_scopes,
            ))
    return tuple(profiles)


def toss_orders_target_scopes(profiles) -> tuple[str, ...]:
    """Scopes with a live-scan obligation (§17.4 routing gate)."""
    return tuple(
        profile.account_scope for profile in profiles
        if profile.supports_live_orders
        and profile.transaction_source is TransactionSourceV1.TOSS_ORDERS
        and profile.native_order_ref_present
    )


def orders_scan_status(profile: AccountSourceProfileV1) -> str:
    """Source-specific completeness classification for one scope.

    ``NOT_APPLICABLE`` scopes never enter the live-scan denominator;
    ``ACCOUNT_UNMAPPED`` is the genuine fail-closed state that justifies a
    ``partial_accounts`` receipt reason.
    """
    if profile.transaction_source is TransactionSourceV1.RETIREMENT_EXCEL_IMPORT:
        return "NOT_APPLICABLE"
    if not profile.native_order_ref_present:
        return "ACCOUNT_UNMAPPED"
    return "SCAN_REQUIRED"
