"""T1 separate leaf values module: scope enums and resolver protocol.

May import stdlib typing/enum support and ``tokmo.values.account_scope_patterns``,
and no other ``tokmo.*`` module.
"""

from enum import Enum
from typing import Protocol

from tokmo.values.account_scope_patterns import (
    ACCOUNT_SCOPE_RE,
    PENSION_SCOPE_RE,
)


class PortfolioScope(str, Enum):
    PERSONAL = "personal"
    PENSION = "retirement"


class ScopeUnresolved(Enum):
    UNRESOLVED = "UNRESOLVED"


# Type alias for the union a resolver may return.
ScopeResolution = PortfolioScope | ScopeUnresolved


class AccountScopeResolver(Protocol):
    def __call__(self, account_scope: str) -> ScopeResolution: ...


def resolve_account_scope_pattern(account_scope: str) -> ScopeResolution:
    """Deterministic pattern-based resolver: toss-acct-* → PERSONAL,
    pension-excel-* → PENSION, everything else → UNRESOLVED."""
    if ACCOUNT_SCOPE_RE.fullmatch(account_scope):
        return PortfolioScope.PERSONAL
    if PENSION_SCOPE_RE.fullmatch(account_scope):
        return PortfolioScope.PENSION
    return ScopeUnresolved.UNRESOLVED
