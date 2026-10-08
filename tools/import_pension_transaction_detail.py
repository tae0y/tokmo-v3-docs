#!/usr/bin/env python3
"""Operator entrypoint for the pension transaction-detail batch.

This is the executable path for contract items 5 and 7. It parses an approved
detailed-transaction batch, classifies it against the real ledger, captures the
CURRENT pension holdings into the official snapshot store with an exact
read-back BEFORE anything is written, and only then appends to the real
``TradeLedgerStore``.

    import_pension_transaction_detail.py --file BATCH.json --plan PLAN.json \\
        --root DIR [--dry-run]

`--file` is the approved detailed transaction export, normalized to JSON by the
operator. `--plan` is the reviewed batch plan: it names the source contract, the
account scope, the importer version, the exact record count, and the source file
digest, so a batch that does not match what a human approved cannot be admitted.

Aggregate/summary input is rejected here, before any snapshot, link or ledger
write: a period summary has no per-execution date and no order identity, and
inventing either would fabricate executions.

There is no production default path. Nothing is written unless `--root` names a
directory explicitly. No network, no credentials, no scheduler.

Output privacy: counts, opaque identifiers and 16-character digest prefixes
only. No instrument name, quantity, price or amount is printed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_BLOCKED = 2

REGISTRY_RELATIVE_PATH = ("stores", "pension_registry.sqlite3")
LEDGER_RELATIVE_PATH = ("stores", "trade_ledger.sqlite3")
# The snapshot database is resolved by the shared canonical resolver: it
# lives under the Toss leaf, not under stores/.

# The approved contract lives in programs/pension_snapshot_adapter.py and is
# enforced there; this tool never re-declares or relaxes it.


class BatchRejected(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _load_plan(path: Path) -> dict:
    """Read and AUTHORIZE the plan against the approved contract.

    Authorization happens here, before any registry, snapshot or ledger object
    is constructed, so an unauthorized source, scope, importer version or event
    kind can never reach a durable write.
    """
    from tokmo.programs.pension_snapshot_adapter import (
        TransactionDetailUnauthorized,
        authorize_transaction_detail_plan,
    )
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        authorize_transaction_detail_plan(raw)
    except TransactionDetailUnauthorized as error:
        raise BatchRejected(error.reason) from None
    return raw


def _load_batch(path: Path, plan: dict):
    """Parse the approved detail export into real ledger events.

    Aggregate input is refused here, before any store is touched.
    """
    from tokmo.values.trade_ledger import TradeLedgerEvent

    raw_bytes = Path(path).read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    if digest != plan["source_sha256"]:
        raise BatchRejected("source_digest_mismatch")

    from tokmo.programs.pension_snapshot_adapter import (
        TransactionDetailUnauthorized,
        authorize_transaction_detail_document,
    )
    document = json.loads(raw_bytes.decode("utf-8"))
    try:
        authorize_transaction_detail_document(document, plan)
    except TransactionDetailUnauthorized as error:
        raise BatchRejected(error.reason) from None

    events = []
    for row in document["records"]:
        events.append(TradeLedgerEvent(
            source=plan["source"],
            account_scope=plan["account_scope"],
            source_record_key=row["source_record_key"],
            event_kind=row.get("event_kind", "EXECUTION"),
            supersedes_source_record_key=row.get(
                "supersedes_source_record_key"),
            trade_date=date.fromisoformat(row["trade_date"]),
            transaction_type_raw=row["transaction_type_raw"],
            instrument_raw=row["instrument_raw"],
            quantity=row["quantity"],
            unit_price=row["unit_price"],
            amount=row["amount"],
            medium_raw=row["medium_raw"],
            currency_raw=row.get("currency_raw"),
            settlement_date=None,
            fee=None,
            tax=None,
        ))
    return tuple(events), digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import an approved pension transaction-detail batch")
    parser.add_argument("--file", type=Path, required=True,
                        help="approved detailed transaction export (JSON)")
    parser.add_argument("--plan", type=Path, required=True,
                        help="reviewed batch plan (JSON)")
    parser.add_argument("--root", type=Path, required=True,
                        help="root holding stores/; no production default")
    parser.add_argument("--dry-run", action="store_true",
                        help="parse and classify only; write nothing")
    args = parser.parse_args(argv)

    from tokmo.programs.pension_current_update import (
        OUTCOME_APPENDED,
        OUTCOME_BLOCKED,
        OUTCOME_CONFLICT_REVIEW,
        OUTCOME_DUPLICATE_NOOP,
    )
    from tokmo.programs.pension_snapshot_adapter import (
        classify_pension_batch,
        run_transaction_detail_batch,
    )
    from tokmo.records.holdings_snapshots import HoldingsSnapshotStore
    from tokmo.records.holdings_snapshots import holdings_snapshot_store_path
    from tokmo.records.pension_revisions import PensionRegistryStore
    from tokmo.records.trade_ledger_store import TradeLedgerStore

    try:
        plan = _load_plan(args.plan)
        events, digest = _load_batch(args.file, plan)
    except BatchRejected as error:
        print(f"ERROR: batch refused: {error.reason}", file=sys.stderr)
        print("Nothing was captured, linked or appended.", file=sys.stderr)
        return EXIT_REFUSED
    except (json.JSONDecodeError, ValueError, OSError) as error:
        print(f"ERROR: batch unreadable: {type(error).__name__}",
              file=sys.stderr)
        return EXIT_REFUSED

    print("Batch parsed.")
    print(f"  record_count:   {len(events)}")
    print(f"  source:         {plan['source']}")
    print(f"  account_scope:  {plan['account_scope']}")
    print(f"  source_sha256:  {digest[:16]}...")

    ledger_path = args.root.joinpath(*LEDGER_RELATIVE_PATH)
    if args.dry_run:
        classification = classify_pension_batch(
            ledger_path, events, source=plan["source"],
            account_scope=plan["account_scope"])
        print(f"  classification: {classification}")
        print("Dry run complete. Nothing written.")
        return EXIT_OK

    now = datetime.now(tz=timezone.utc)
    registry = PensionRegistryStore(
        args.root.joinpath(*REGISTRY_RELATIVE_PATH), clock=lambda: now)
    snapshots = HoldingsSnapshotStore(
        holdings_snapshot_store_path(args.root), clock=lambda: now)
    counter = {"n": 0}

    def identifier() -> str:
        counter["n"] += 1
        return hashlib.sha256(
            f"{digest}:{counter['n']}".encode()).hexdigest()[:32]

    ledger = TradeLedgerStore(
        ledger_path, clock=lambda: now, id_factory=identifier)

    before = registry.read_active_revision()
    trace: list[str] = []
    receipt = run_transaction_detail_batch(
        registry=registry, snapshot_store=snapshots, ledger_store=ledger,
        ledger_path=ledger_path, events=events, file_sha256=digest,
        importer_version=plan["importer_version"], source=plan["source"],
        account_scope=plan["account_scope"], clock=lambda: now, trace=trace)

    print(f"  order:          {' -> '.join(trace)}")
    print(f"  outcome:        {receipt.outcome}")
    print(f"  batch_class:    {receipt.batch_class}")
    if receipt.pre_snapshot_id:
        print(f"  pre_snapshot:   {receipt.pre_snapshot_id}")

    after = registry.read_active_revision()
    if before is not None and after is not None:
        if (after.revision_id != before.revision_id
                or after.source_as_of != before.source_as_of
                or after.holdings != before.holdings):
            print("ERROR: transaction detail changed current holdings; this "
                  "must never happen", file=sys.stderr)
            return EXIT_BLOCKED
        print(f"  holdings:       unchanged ({after.holding_count} items, "
              f"source_as_of preserved)")

    if receipt.outcome == OUTCOME_APPENDED:
        print(f"Appended {receipt.appended_count} events.")
        return EXIT_OK
    if receipt.outcome == OUTCOME_DUPLICATE_NOOP:
        print("Exact duplicate: no snapshot, no ledger write, no mutation.")
        return EXIT_OK
    if receipt.outcome == OUTCOME_CONFLICT_REVIEW:
        print("ERROR: conflict; nothing written. Left for review.",
              file=sys.stderr)
        return EXIT_REFUSED
    print(f"ERROR: blocked: {receipt.reason}; nothing appended.",
          file=sys.stderr)
    return EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
