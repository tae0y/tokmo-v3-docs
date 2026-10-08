"""Closed, privacy-safe exception vocabulary for the trade-ledger store.

Every raised diagnostic carries exactly one closed reason token. Nothing
value-bearing — keys, scopes, instruments, quantities, prices, amounts,
dates, paths, SQL, or wrapped provider/driver text — may ride on these
exceptions; an out-of-vocabulary reason is coerced to ``invalid_batch``
rather than echoed.
"""

from tokmo.values.trade_ledger import CLOSED_REASON_TOKENS


class LedgerError(Exception):
    def __init__(self, reason: str) -> None:
        if reason not in CLOSED_REASON_TOKENS:
            reason = "invalid_batch"
        super().__init__(reason)
        self.reason = reason

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} reason={self.reason!r}>"

    def __str__(self) -> str:
        return self.__repr__()


class SourceContractViolationError(LedgerError):
    pass


class BatchConflictError(LedgerError):
    pass


class StoreCorruptError(LedgerError):
    pass


class StoreLockedError(LedgerError):
    pass


class StoreReadError(LedgerError):
    pass
