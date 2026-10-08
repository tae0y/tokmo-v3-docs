"""Versioned source-contract registry and store-side sequence preflight.

The store does not trust the external importer for sequence integrity: a
batch is accepted only under a registered per-source contract, and the
``retirement_excel_v1`` full-history contract requires ``row-`` plus exactly
six digits (value >= 1) with the distinct submitted keys forming exactly
``1..N`` — any gap, malformed key, or start other than 1 fails the whole
batch closed before payload classification. Future sources register their
own contract with their own key format; the row-key rules here are specific
to the retirement export shape, not universal, so the SQLite schema carries
no key-format CHECK and this module is the authoritative enforcement point.
"""

import re
from dataclasses import dataclass
from collections.abc import Callable

from tokmo.values.receipt_v1 import ACCOUNT_SCOPE_RE


@dataclass(frozen=True)
class SourceContract:
    source: str
    contract_version: int
    account_scopes: frozenset[str]
    key_pattern: str
    sequential_full_history: bool
    allows_linked_supersession: bool
    scope_validator: Callable[[str], bool] | None = None

    def key_matches(self, key: object) -> bool:
        return isinstance(key, str) and (
            re.fullmatch(self.key_pattern, key) is not None
        )

    def key_number(self, key: object) -> int | None:
        """Sequence number of a well-formed sequential key, else None."""
        if not self.sequential_full_history or not self.key_matches(key):
            return None
        number = int(key.split("-", 1)[1])
        return number if number >= 1 else None


SOURCE_CONTRACTS: dict[str, SourceContract] = {
    "retirement_excel_v1": SourceContract(
        source="retirement_excel_v1",
        contract_version=1,
        account_scopes=frozenset({"retirement_pension_1"}),
        key_pattern=r"row-[0-9]{6}",
        sequential_full_history=True,
        allows_linked_supersession=False,
    ),
    "retirement_excel_delta_v1": SourceContract(
        source="retirement_excel_delta_v1", contract_version=1,
        account_scopes=frozenset({"retirement_pension_1"}),
        key_pattern=r"batch-[0-9a-f]{64}-row-[0-9]{6}",
        sequential_full_history=False, allows_linked_supersession=False,
    ),
    "toss-orders-v1": SourceContract(
        source="toss-orders-v1",
        contract_version=1,
        account_scopes=frozenset(),
        key_pattern=r"^[A-Za-z0-9._:-]{4,128}$",
        sequential_full_history=False,
        allows_linked_supersession=False,
        scope_validator=lambda value: ACCOUNT_SCOPE_RE.fullmatch(value) is not None,
    ),
}


def registered_contract(
    contracts: dict[str, SourceContract], source: str, account_scope: str
) -> SourceContract | None:
    contract = contracts.get(source)
    if contract is None:
        return None
    if contract.scope_validator is not None:
        return contract if contract.scope_validator(account_scope) else None
    if account_scope not in contract.account_scopes:
        return None
    return contract


def sequence_preflight_ok(
    contract: SourceContract, distinct_keys: tuple[str, ...]
) -> bool:
    """Validate the distinct submitted key set against the source contract.

    Sequential full-history contracts demand exactly ``1..N`` independent of
    submitted order; non-sequential contracts demand well-formed keys only.
    An empty distinct set never passes.
    """
    if not distinct_keys:
        return False
    if not contract.sequential_full_history:
        return all(contract.key_matches(key) for key in distinct_keys)
    numbers = []
    for key in distinct_keys:
        number = contract.key_number(key)
        if number is None:
            return False
        numbers.append(number)
    return sorted(numbers) == list(range(1, len(numbers) + 1))
