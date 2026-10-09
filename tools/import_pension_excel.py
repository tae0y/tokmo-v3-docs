#!/usr/bin/env python3
"""DEPRECATED legacy pension import entrypoint. Fail-closed; writes nothing.

This tool used to parse a pension workbook, create a candidate revision, and
optionally activate it. It is retired and now refuses every invocation.

Why it had to be refused rather than repaired
---------------------------------------------
It activated the revision FIRST and only then wrote the canonical holdings
snapshot -- and it resolved that snapshot at ``<root>/stores/``, which is not
where the canonical snapshot database lives. On a real root the snapshot step
therefore failed AFTER the registry had already been mutated to ACTIVE, which
is exactly the inversion the pension contract forbids: a snapshot failure must
leave current state untouched. The same wrong path would, on a writable root,
have created a second parallel snapshot authority.

Refusal, not preview
--------------------
The refusal covers ``--preview-only`` too. A read-only preview would be
defensible only if it could be PROVEN non-mutating, and that proof would have
to cover the legacy parser and everything it reaches. There is no need to buy
that proof: ``import_pension_account_status.py parse`` is the reviewed,
non-mutating preview for the approved workbook form. Refusing everything also
lets this module import nothing from ``tokmo`` at all, which makes "no store is
ever constructed" a property of the file rather than a claim about its
branches.

Approved entrypoints
--------------------
    tools/import_pension_account_status.py       holdings (current state)
    tools/import_pension_transaction_detail.py   transaction-detail ledger

Both take a snapshot of current pension holdings, and verify the read-back,
BEFORE they mutate anything.
"""

from __future__ import annotations

import sys

NEW_HOLDINGS_ENTRYPOINT = "tools/import_pension_account_status.py"
NEW_LEDGER_ENTRYPOINT = "tools/import_pension_transaction_detail.py"

# Refusal is a usage error, distinct from the 1 this tool used to return for a
# genuine parse or snapshot failure. Nothing here can return 0 except --help.
REFUSED_EXIT_CODE = 2

REFUSAL = f"""\
REFUSED: tools/import_pension_excel.py is DEPRECATED and writes nothing.

It is fail-closed for every invocation, including --preview-only and --approve.
No workbook is read, no store is opened, and no state is changed.

It was retired because it activated a pension revision BEFORE writing the
canonical holdings snapshot, and resolved that snapshot at the wrong location
(<root>/stores/ instead of the canonical <root>/toss/). A snapshot failure
could therefore leave current pension state already changed.

Use the approved entrypoints instead:

  holdings (current state):   {NEW_HOLDINGS_ENTRYPOINT}
  transaction-detail ledger:  {NEW_LEDGER_ENTRYPOINT}

Both snapshot current pension holdings and verify the read-back before any
durable mutation.
"""

HELP = f"""\
tools/import_pension_excel.py -- DEPRECATED, fail-closed, writes nothing.

Every invocation is refused with exit code {REFUSED_EXIT_CODE}; only --help
succeeds. The former --file/--root/--approve/--preview-only/--source-as-of
arguments are no longer accepted or acted on.

Approved replacements:

  holdings (current state):   {NEW_HOLDINGS_ENTRYPOINT}
  transaction-detail ledger:  {NEW_LEDGER_ENTRYPOINT}
"""


def main(argv: list[str] | None = None) -> int:
    """Refuse everything except --help, before reading or opening anything."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "-h" in arguments or "--help" in arguments:
        print(HELP)
        return 0
    print(REFUSAL, file=sys.stderr)
    return REFUSED_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
