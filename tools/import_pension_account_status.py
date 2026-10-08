#!/usr/bin/env python3
"""Operator entrypoint for the broker account-status workbook.

It writes into the ONE canonical pension holdings store,
``stores/pension_registry.sqlite3``, through the existing
``PensionRegistryStore`` lifecycle (candidate -> pending -> single ACTIVE).
No second store, schema, or ACTIVE pointer exists or is created here.

The canonical holdings scope is ETF-only by user decision. Rows the reviewed
classification plan marks as non-ETF (corporate bonds, mutual fund, cash-like
balance) are deliberately excluded from the revision; they survive only inside
the private reconciliation digest. This tool prints that exclusion explicitly
so an operator can never mistake the revision for a whole-account listing.

Subcommands are separate and nothing is implicit:

    parse         parse and report; touches no store, deletes nothing
    candidate     parse and store a CANDIDATE_VALID revision; never activates,
                  never deletes the landing workbook
    activate      the explicit activation gate; deletes the landing workbook
                  only with --delete-landing, and only after commit ->
                  read-back -> file-hash equality -> directory fsync
    summary       parse the period summary as evidence; stores nothing

OFFLINE: no network, no credentials, no scheduler, no notification. The store
path is derived from the operator-supplied --root, so nothing is written unless
a root is named explicitly.

Output privacy: only revision ids, integer counts, coverage statement and
16-character digest prefixes are printed. No instrument name, quantity, price,
amount, account identifier or full hash is ever emitted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")

REGISTRY_RELATIVE_PATH = ("stores", "pension_registry.sqlite3")
# The snapshot database is resolved by the shared canonical resolver, never
# re-declared here: it lives under the Toss leaf, not under stores/.


def _registry(root: Path):
    """The one canonical pension holdings store."""
    from tokmo.records.pension_revisions import PensionRegistryStore
    now = datetime.now(tz=timezone.utc)
    return PensionRegistryStore(
        root.joinpath(*REGISTRY_RELATIVE_PATH), clock=lambda: now)


def _kst_date(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=_KST)


def _load_plan(path: Path):
    """Read the reviewed classification plan from a JSON file.

    The plan is an operator artifact held outside this repository. It is never
    inferred from the workbook: it is what a human reviewed and approved.
    """
    from tokmo.values.pension_account_status import (
        PensionClassificationPlanV1,
        PensionRowDispositionV1,
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = tuple(
        PensionRowDispositionV1(
            ordinal=int(entry["ordinal"]),
            disposition=entry["disposition"],
            symbol=entry.get("symbol"),
            exclusion_kind=entry.get("exclusion_kind"),
        )
        for entry in raw["rows"]
    )
    return PensionClassificationPlanV1(
        plan_version=raw["plan_version"],
        expected_row_count=int(raw["expected_row_count"]),
        expected_etf_count=int(raw["expected_etf_count"]),
        expected_excluded_count=int(raw["expected_excluded_count"]),
        rows=rows,
    )


def _parse(args):
    from tokmo.facts.pension_account_status_excel import (
        parse_pension_account_status,
    )
    return parse_pension_account_status(
        args.file,
        now=datetime.now(tz=timezone.utc),
        source_as_of=_kst_date(args.source_as_of),
        plan=_load_plan(args.plan),
    )


def _report(result) -> None:
    revision = result.revision
    print("Parse successful (account status).")
    print(f"  revision_id:           {revision.revision_id}")
    print(f"  source_row_count:      {result.source_row_count}")
    print(f"  admitted_etf_count:    {result.included_etf_count}")
    print(f"  excluded_row_count:    {result.excluded_row_count}")
    for kind, count in result.excluded_kind_counts:
        print(f"    excluded {kind}: {count}")
    print(f"  coverage_statement:    {result.coverage_statement}")
    print(f"  holding_count:         {revision.holding_count}")
    print(f"  semantic_digest:       {revision.semantic_digest[:16]}...")
    print(f"  source_content_sha256: {revision.source_content_sha256[:16]}...")
    print(f"  plan_digest:           {result.plan_digest[:16]}...")
    print(f"  source_as_of:          {revision.source_as_of.isoformat()}")
    if result.excluded_row_count:
        print("  NOTE: canonical pension holdings are ETF-only by decision. "
              "The excluded rows are not represented as holdings.")


def cmd_parse(args) -> int:
    _report(_parse(args))
    print("Parse only - no candidate created, no file removed.")
    return 0


def cmd_candidate(args) -> int:
    result = _parse(args)
    _report(result)
    _registry(args.root).create_candidate(result.revision)
    print(f"Candidate created: {result.revision.revision_id}")
    print("NOT activated. Run the 'activate' subcommand separately.")
    print("Landing file left in place.")
    return 0


def cmd_activate(args) -> int:
    """The activation gate, and the only path that changes current holdings.

    It goes through the snapshot-first protocol: the CURRENT holdings are
    captured into the official snapshot store and read back exactly, then the
    CANDIDATE holdings are captured and read back exactly, and only then is the
    registry's current payload replaced. There is deliberately no direct
    ``store.activate`` call here — on the current read model that call is
    refused anyway, because current may only be installed from a verified
    snapshot.
    """
    import dataclasses as _dataclasses
    from datetime import datetime as _datetime, timezone as _timezone

    from tokmo.programs.pension_current_update import (
        OUTCOME_REPLACED,
        replace_current_holdings,
    )
    from tokmo.programs.pension_snapshot_adapter import (
        BINDING_CANDIDATE,
        BINDING_CURRENT,
        pension_snapshot_port,
    )
    from tokmo.records.holdings_snapshots import HoldingsSnapshotStore
    from tokmo.programs.pension_current_update import (
        PensionSnapshotPortV1, SnapshotOutcomeV1,
    )

    store = _registry(args.root)
    metadata = store.read_revision_metadata(args.revision_id)
    if metadata is None:
        print("ERROR: revision not found", file=sys.stderr)
        return 1

    # The registry keeps the candidate's digests but never its holdings, so the
    # operator re-presents the reviewed source and it must digest-match before
    # anything is captured or replaced.
    result = _parse(args)
    candidate = result.revision
    if (candidate.source_content_sha256 != metadata["source_content_sha256"]
            or candidate.semantic_digest != metadata["semantic_digest"]):
        print("ERROR: re-presented source does not match the stored candidate "
              "digests; nothing captured, nothing replaced", file=sys.stderr)
        return 1
    candidate = _dataclasses.replace(
        candidate, revision_id=args.revision_id)

    now = _datetime.now(tz=_timezone.utc)
    from tokmo.records.holdings_snapshots import holdings_snapshot_store_path
    snapshots = HoldingsSnapshotStore(
        holdings_snapshot_store_path(args.root), clock=lambda: now)

    current = store.read_active_revision()
    current_port = pension_snapshot_port(snapshots, binding=BINDING_CURRENT)
    candidate_port = pension_snapshot_port(snapshots, binding=BINDING_CANDIDATE)

    def capture(revision):
        port = current_port if (
            current is not None and revision.revision_id == current.revision_id
        ) else candidate_port
        return port.capture(revision)

    trace: list[str] = []
    receipt = replace_current_holdings(
        registry=store,
        snapshot_port=PensionSnapshotPortV1(capture=capture),
        current_revision=current,
        candidate_revision=candidate,
        approval_reason=args.approval_reason,
        trace=trace,
    )
    if receipt.outcome != OUTCOME_REPLACED:
        print(f"ERROR: activation blocked: {receipt.reason}; current holdings "
              f"and the registry are unchanged", file=sys.stderr)
        return 1
    print("Snapshot-first activation complete.")
    print(f"  order:                 {' -> '.join(trace)}")
    if receipt.current_snapshot_id:
        print(f"  current snapshot:      {receipt.current_snapshot_id}")
    print(f"  candidate snapshot:    {receipt.candidate_snapshot_id}")

    readback = store.read_active_revision()
    if readback is None or readback.revision_id != args.revision_id:
        print("ERROR: activation read-back failed", file=sys.stderr)
        return 1
    print(f"Activated: {readback.revision_id}")
    print(f"  holding_count:    {readback.holding_count}")
    print(f"  semantic_digest:  {readback.semantic_digest[:16]}...")
    print("  This is the single ACTIVE pension holdings revision; the previous "
          "one is now SUPERSEDED.")

    if args.delete_landing is None:
        print("Landing file left in place (--delete-landing not given).")
        return 0

    landing = args.delete_landing
    if not landing.is_file() or landing.is_symlink():
        print("ERROR: landing path is not a regular file", file=sys.stderr)
        return 1
    digest = hashlib.sha256(landing.read_bytes()).hexdigest()
    if digest != readback.source_content_sha256:
        print("ERROR: landing file does not match the activated revision; "
              "nothing removed", file=sys.stderr)
        return 1

    from tokmo.records.durability import fsync_directory
    landing.unlink()
    fsync_directory(landing.parent)
    print("Landing file removed after commit, read-back, hash equality "
          "and directory fsync.")
    return 0


def cmd_summary(args) -> int:
    from tokmo.facts.pension_account_status_excel import (
        parse_pension_period_summary,
    )
    evidence = parse_pension_period_summary(
        args.file, now=datetime.now(tz=timezone.utc))
    print("Parse successful (period summary).")
    print(f"  row_count:             {evidence.row_count}")
    print(f"  aggregate_only:        {evidence.aggregate_only}")
    print(f"  semantic_digest:       {evidence.semantic_digest[:16]}...")
    print(f"  source_content_sha256: "
          f"{evidence.source_content_sha256[:16]}...")
    print("This is NON-HOLDINGS, NON-EXECUTION evidence. It was not stored, "
          "not added to pension holdings, and not appended to any execution "
          "ledger.")
    print("Landing file left in place.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import the broker account-status workbook (offline).")
    sub = parser.add_subparsers(dest="command", required=True)

    parse_cmd = sub.add_parser("parse", help="parse only; no store, no deletion")
    parse_cmd.add_argument("--file", type=Path, required=True)
    parse_cmd.add_argument("--plan", type=Path, required=True,
                           help="reviewed classification plan (JSON)")
    parse_cmd.add_argument("--source-as-of", required=True,
                           help="user-confirmed KST date, e.g. 2026-08-13")
    parse_cmd.set_defaults(handler=cmd_parse)

    candidate_cmd = sub.add_parser(
        "candidate", help="store a candidate; never activates")
    candidate_cmd.add_argument("--file", type=Path, required=True)
    candidate_cmd.add_argument("--plan", type=Path, required=True)
    candidate_cmd.add_argument("--root", type=Path, required=True)
    candidate_cmd.add_argument("--source-as-of", required=True)
    candidate_cmd.set_defaults(handler=cmd_candidate)

    activate_cmd = sub.add_parser(
        "activate",
        help="the activation gate: captures current and candidate holdings "
             "into the official snapshot store, reads each back exactly, and "
             "only then replaces the registry's current payload")
    activate_cmd.add_argument("--root", type=Path, required=True)
    activate_cmd.add_argument("--revision-id", required=True)
    activate_cmd.add_argument("--approval-reason", required=True)
    activate_cmd.add_argument(
        "--file", type=Path, required=True,
        help="the reviewed source, re-presented; must digest-match the "
             "stored candidate")
    activate_cmd.add_argument("--plan", type=Path, required=True)
    activate_cmd.add_argument("--source-as-of", required=True)
    activate_cmd.add_argument(
        "--delete-landing", type=Path, default=None,
        help="remove this landing file after read-back and hash equality")
    activate_cmd.set_defaults(handler=cmd_activate)

    summary_cmd = sub.add_parser(
        "summary", help="parse the period summary as evidence; stores nothing")
    summary_cmd.add_argument("--file", type=Path, required=True)
    summary_cmd.set_defaults(handler=cmd_summary)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "file", None) is not None and not args.file.exists():
        print("ERROR: file not found", file=sys.stderr)
        return 1
    from tokmo.facts.pension_excel import PensionExcelParseError
    from tokmo.records.pension_revisions import PensionRegistryCorruptError
    from tokmo.values.pension_account_status import PensionClassificationError
    try:
        return args.handler(args)
    except PensionExcelParseError as error:
        print(f"ERROR: parse rejected: {error.reason}", file=sys.stderr)
        return 1
    except PensionClassificationError as error:
        print(f"ERROR: classification plan rejected: {error.reason}",
              file=sys.stderr)
        return 1
    except PensionRegistryCorruptError as error:
        print(f"ERROR: registry rejected: {error.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
