"""N1 true leaf module: account scope regex patterns.

This module imports ONLY ``re``. It must not import or reference any
``tokmo.*`` symbol. Every consumer of the account-scope patterns imports
from here so there is no import cycle.
"""

import re

# Moved from receipt_v1.py (re-exported there for backward compatibility).
ACCOUNT_SCOPE_RE = re.compile(r"^toss-acct-[0-9a-f]{24}$")

# Pension account scope pattern (N1: defined in leaf, not pension_holdings).
PENSION_SCOPE_RE = re.compile(r"^pension-excel-[0-9a-f]{24}$")


def is_valid_account_scope(value: str) -> bool:
    """True if *value* matches either known account scope pattern."""
    return (
        ACCOUNT_SCOPE_RE.fullmatch(value) is not None
        or PENSION_SCOPE_RE.fullmatch(value) is not None
    )
