"""Holdings types: holding fact, account holdings, snapshot account, snapshot, header."""

from dataclasses import dataclass, field
from datetime import datetime

from tokmo.values import TOSS_SOURCE

# N4: PENSION source alongside TOSS.
PENSION_SOURCE = "pension-excel-v1"

__all__ = [
    "TossHoldingFact", "TossAccountHoldingsFact",
    "HoldingsSnapshotAccount", "CompleteHoldingsSnapshot",
    "HoldingsSnapshotHeader",
]


@dataclass(frozen=True)
class TossHoldingFact:
    """One provider holdings line item, preserved without interpretation.

    ``quantity`` and ``avg_price`` are canonical Decimal strings of the
    verbatim provider values; ``avg_price_field``/``market_field`` name which
    evidenced provider key supplied them. The value-bearing fields stay out of
    ``repr`` so that no exception, log line, or report can render a position;
    the surviving repr carries provider field names only.
    """

    symbol: str = field(repr=False)
    quantity: str = field(repr=False)
    avg_price: str = field(repr=False)
    avg_price_field: str
    currency: str | None
    market_raw: str | None
    market_field: str | None
    name: str | None = field(repr=False)
    asset_type: str | None


@dataclass(frozen=True)
class TossAccountHoldingsFact:
    """One account's current holdings at one campaign-wide capture instant.

    ``account_ordinal`` is the account's position in this capture's accounts
    response and nothing else: it is not a durable account key, so two
    captures' equal ordinals are not a claim that the same account is meant.
    """

    source: str
    captured_at: datetime
    account_ordinal: int
    account_count: int
    holdings: tuple[TossHoldingFact, ...]

    @property
    def source_ref(self) -> str:
        return f"{self.source}:{self.captured_at.isoformat()}:{self.account_ordinal}"


@dataclass(frozen=True)
class HoldingsSnapshotAccount:
    """One account's holdings at a shared capture instant.

    ``account_ordinal`` is capture-local positional only: it is never a
    durable account key. ``holdings`` may be an empty tuple (account
    discovered but holding nothing). Both fields stay out of ``repr`` so no
    ordinal or nested position can render through routine output.
    """

    account_ordinal: int = field(repr=False)
    holdings: tuple[TossHoldingFact, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.account_ordinal, bool):
            raise ValueError("holdings_snapshot_account_ordinal_bool")
        if not isinstance(self.holdings, tuple):
            raise ValueError("holdings_snapshot_holdings_not_tuple")
        symbols = [item.symbol for item in self.holdings]
        if len(symbols) != len(set(symbols)):
            raise ValueError("holdings_snapshot_duplicate_symbol_in_account")


@dataclass(frozen=True)
class CompleteHoldingsSnapshot:
    """The sole holdings write/read unit: one complete capture across every
    discovered account.

    ``snapshot_id`` is an opaque unique string (runtime UUID4 hex or the
    deterministic legacy migration digest); ``content_hash`` is the SHA-256
    hexdigest of the canonical serialization. Both stay out of ``repr``
    along with the nested accounts.
    """

    snapshot_id: str = field(repr=False)
    source: str
    captured_at: datetime
    discovered_account_count: int
    accounts: tuple[HoldingsSnapshotAccount, ...] = field(repr=False)
    content_hash: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.source not in {TOSS_SOURCE, PENSION_SOURCE}:
            raise ValueError("holdings_snapshot_source_outside_contract")
        if self.captured_at.tzinfo is None:
            raise ValueError("holdings_snapshot_captured_at_naive")
        if isinstance(self.discovered_account_count, bool):
            raise ValueError("holdings_snapshot_count_bool")
        if not isinstance(self.accounts, tuple):
            raise ValueError("holdings_snapshot_accounts_not_tuple")
        if not self.accounts:
            raise ValueError("holdings_snapshot_no_accounts")
        if self.discovered_account_count != len(self.accounts):
            raise ValueError("holdings_snapshot_count_mismatch")
        ordinals = tuple(account.account_ordinal for account in self.accounts)
        if ordinals != tuple(range(1, len(self.accounts) + 1)):
            raise ValueError("holdings_snapshot_ordinals_not_contiguous")


@dataclass(frozen=True)
class HoldingsSnapshotHeader:
    """Lightweight capture header for listing captures without positions."""

    snapshot_id: str = field(repr=False)
    source: str
    captured_at: datetime
    discovered_account_count: int
    content_hash: str = field(repr=False)
