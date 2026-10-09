"""Operator/cron CLI for one archived 12man report admission (EXT-YT-001).

Admits the closed TRACKING_CANDIDATE blocks of one already-generated
report against an EXPLICIT ops-kernel store. Every input is explicit —
there is no production default path, no network, no Telegram, no clock
read: the observation instant comes from the archive metadata itself, so
an identical replay is durably side-effect free.

Usage: .venv/bin/python tools/run_youtube_report_admission.py \
    --meta <archive .meta.json> --report <report/candidate .txt> \
    --db <ops_kernel.sqlite3> [--expect-source-ref VIDEO_ID]

The meta and report files must satisfy the existing private-store file
rules (regular file, no symlink, mode 0600, private parent, same owner).
stdout carries exactly one bounded machine-readable JSON read-back line
(no private paths, no hypothesis ids); stderr carries refusals.
Exit 0 = admission ran and the JSON line is authoritative;
1 = fail-closed refusal BEFORE the store was opened (nothing mutated);
2 = admission error after the store opened.
"""

import argparse
import json
import sys
from pathlib import Path

_TOOL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = _TOOL_DIR.parent
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tokmo.programs.youtube_report_admission import (
    admit_archived_report,
    observed_at_from_archive_meta,
)
from tokmo.programs.youtube_tracking import (
    TrackingCandidateParseError,
    provenance_from_archive_meta,
)
from tokmo.records.ops_kernel import (
    OpsKernelCorruptError,
    OpsKernelStore,
)
from tokmo.records.private_sqlite import prepare_private_path


def _refuse(reason: str) -> int:
    print(f"run-youtube-report-admission refusal={reason}", file=sys.stderr)
    return 1


def _read_private_text(path: Path, kind: str):
    """Read one private input file or explain the refusal (fail closed)."""
    if path.is_symlink() or not path.is_file():
        return None, f"{kind}_not_a_private_regular_file"
    safety = prepare_private_path(path)
    if safety.unsafe:
        return None, f"{kind}_unsafe_{safety.reason}"
    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError):
        return None, f"{kind}_unreadable"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run-youtube-report-admission",
        add_help=True,
    )
    parser.add_argument(
        "--meta",
        required=True,
        type=Path,
        help="archived 12man .meta.json (immutable archive)",
    )
    parser.add_argument(
        "--report", required=True, type=Path, help="private report/candidate text file"
    )
    parser.add_argument(
        "--db",
        required=True,
        type=Path,
        help="explicit ops-kernel sqlite path (no default)",
    )
    parser.add_argument(
        "--expect-source-ref",
        default=None,
        help="refuse unless the meta video_id equals this",
    )
    args = parser.parse_args(argv)

    meta_text, error = _read_private_text(args.meta, "meta")
    if error is not None:
        return _refuse(error)
    report_text, error = _read_private_text(args.report, "report")
    if error is not None:
        return _refuse(error)
    try:
        meta = json.loads(meta_text)
    except ValueError:
        return _refuse("meta_not_json")
    if not isinstance(meta, dict):
        return _refuse("meta_not_object")
    try:
        provenance = provenance_from_archive_meta(meta)
        observed_at_from_archive_meta(meta)
    except TrackingCandidateParseError:
        return _refuse("bad_provenance")
    if (
        args.expect_source_ref is not None
        and args.expect_source_ref != provenance.source_ref
    ):
        return _refuse("source_mismatch")

    try:
        store = OpsKernelStore(args.db)
    except OpsKernelCorruptError:
        return _refuse("unsafe_store")
    try:
        result = admit_archived_report(
            store,
            meta=meta,
            report_text=report_text,
        )
    except Exception as error:
        print(
            f"run-youtube-report-admission failed={type(error).__name__}",
            file=sys.stderr,
        )
        return 2
    finally:
        store.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
