"""Account native ref writer seam: CadenceAccountsV1 → AccountNativeRefStoreV1.

Plan §17.1(a): the 300s successful holdings stage persists the scope→native
ref mapping so Pulse never calls ACCOUNTS. Only live-order-capable Toss
scopes are eligible (pension/manual scopes never had native refs). The
receipt binding ties the refs to the exact holdings wire receipt that
produced them. Atomic replace ensures stale mappings from a prior account
rotation are fully removed.
"""

from tokmo.records.account_native_refs import AccountNativeRefStoreV1
from tokmo.values.account_scope_patterns import ACCOUNT_SCOPE_RE
from tokmo.values.production_cadence import CadenceAccountsV1


class NativeRefWriterError(ValueError):
    """The accounts inventory is invalid for native ref persistence."""


def write_native_refs_from_accounts(
    *,
    accounts: CadenceAccountsV1,
    receipt_binding: str,
    store: AccountNativeRefStoreV1,
) -> None:
    """Filter Toss scopes from the account inventory and atomically replace
    the native ref store.

    ``accounts`` is the ``CadenceAccountsV1`` from a successful ACCOUNTS
    wire. ``receipt_binding`` is the holdings receipt identity (e.g.
    ``cadence-300s-market-portfolio-v1:5952870``). Pension/manual scopes
    are silently excluded — they never had native refs.
    """
    if not isinstance(accounts, CadenceAccountsV1):
        raise NativeRefWriterError("invalid accounts type")
    if type(receipt_binding) is not str or not receipt_binding:
        raise ValueError("receipt binding is mandatory")

    # Only live-order-capable Toss scopes carry native refs. Pension and
    # other manual scopes are silently excluded — they never had refs.
    refs = {}
    for scope, native_ref in accounts.native_refs.items():
        if ACCOUNT_SCOPE_RE.fullmatch(scope) is None:
            continue  # pension/manual scope: skip silently
        if type(native_ref) is not str or not native_ref:
            raise NativeRefWriterError("toss scope with empty or missing native ref")
        refs[scope] = native_ref

    if not refs:
        raise NativeRefWriterError(
            "no live-order-capable Toss scopes in account inventory"
        )

    store.replace_refs(refs, receipt_binding=receipt_binding)
