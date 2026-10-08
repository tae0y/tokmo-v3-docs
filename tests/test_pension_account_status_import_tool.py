"""Operator tool: writes only into the one canonical V1 registry.

Candidate creation and activation stay separate, the landing workbook is
removed only inside the guarded activation path, and the ETF-only exclusion is
stated out loud rather than hidden.

Every fixture is synthetic; the tool is only ever pointed at ``tmp_path``.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokmo.values.pension_account_status import (
    CLASSIFICATION_PLAN_VERSION,
    DISPOSITION_EXCLUDED_NON_ETF,
    DISPOSITION_VERIFIED_KRX_ETF,
    EXCLUSION_CASH_LIKE,
    EXCLUSION_CORPORATE_BOND,
    EXCLUSION_MUTUAL_FUND,
)

from tests.test_pension_account_status_parser import (  # noqa: E402
    build_status_workbook,
    build_summary_workbook,
    etf_symbol,
    landing,
)

TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / \
    "import_pension_account_status.py"

SOURCE_AS_OF = "2026-08-13"


def load_tool():
    spec = importlib.util.spec_from_file_location(
        "synthetic_pension_account_status_tool", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool():
    return load_tool()


@pytest.fixture()
def workbook(tmp_path):
    return landing(tmp_path, build_status_workbook(), "status.xlsx")


@pytest.fixture()
def plan_file(tmp_path):
    rows = [
        {"ordinal": n, "disposition": DISPOSITION_VERIFIED_KRX_ETF,
         "symbol": etf_symbol(n)}
        for n in range(1, 11)
    ] + [
        {"ordinal": 11, "disposition": DISPOSITION_EXCLUDED_NON_ETF,
         "exclusion_kind": EXCLUSION_CORPORATE_BOND},
        {"ordinal": 12, "disposition": DISPOSITION_EXCLUDED_NON_ETF,
         "exclusion_kind": EXCLUSION_CORPORATE_BOND},
        {"ordinal": 13, "disposition": DISPOSITION_EXCLUDED_NON_ETF,
         "exclusion_kind": EXCLUSION_MUTUAL_FUND},
        {"ordinal": 14, "disposition": DISPOSITION_EXCLUDED_NON_ETF,
         "exclusion_kind": EXCLUSION_CASH_LIKE},
    ]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({
        "plan_version": CLASSIFICATION_PLAN_VERSION,
        "expected_row_count": 14,
        "expected_etf_count": 10,
        "expected_excluded_count": 4,
        "rows": rows,
    }), encoding="utf-8")
    return path


def registry_path(root: Path) -> Path:
    return root / "stores" / "pension_registry.sqlite3"


def revision_id_from(output: str) -> str:
    for line in output.splitlines():
        if line.strip().startswith("revision_id:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("no revision_id in tool output")


def open_store(root: Path):
    from tokmo.records.pension_revisions import PensionRegistryStore
    return PensionRegistryStore(
        registry_path(root),
        clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# parse — reports the ETF-only truth, stores nothing
# ---------------------------------------------------------------------------

def test_parse_reports_ten_admitted_and_four_excluded(
        tmp_path, tool, workbook, plan_file, capsys):
    assert tool.main([
        "parse", "--file", str(workbook), "--plan", str(plan_file),
        "--source-as-of", SOURCE_AS_OF,
    ]) == 0
    output = capsys.readouterr().out
    assert "source_row_count:      14" in output
    assert "admitted_etf_count:    10" in output
    assert "excluded_row_count:    4" in output
    assert "holding_count:         10" in output
    assert "ETF_ONLY_PARTIAL_ACCOUNT_COVERAGE" in output
    assert "ETF-only by decision" in output
    assert workbook.exists()
    assert not (tmp_path / "root").exists()


def test_parse_names_the_excluded_kinds(
        tmp_path, tool, workbook, plan_file, capsys):
    tool.main([
        "parse", "--file", str(workbook), "--plan", str(plan_file),
        "--source-as-of", SOURCE_AS_OF,
    ])
    output = capsys.readouterr().out
    assert "excluded CORPORATE_BOND: 2" in output
    assert "excluded MUTUAL_FUND: 1" in output
    assert "excluded CASH_LIKE: 1" in output


def test_tool_output_carries_no_private_content(
        tmp_path, tool, workbook, plan_file, capsys):
    tool.main([
        "parse", "--file", str(workbook), "--plan", str(plan_file),
        "--source-as-of", SOURCE_AS_OF,
    ])
    output = capsys.readouterr().out

    # Opaque identifiers are hex by construction and are proved to be ids
    # elsewhere; scanning them for decimal substrings would be a coin flip on
    # the random revision id, not a privacy signal.
    scanned = "\n".join(
        line for line in output.splitlines()
        if not line.strip().startswith(
            ("revision_id:", "semantic_digest:", "source_content_sha256:",
             "plan_digest:"))
    )
    for banned in ("SYNTH-ETF", "SYNTH-BOND", "SYNTH-FUND", "SYNTH-CASH",
                   "1000", "1100", "506"):
        assert banned not in scanned, banned

    # And the identifier lines really are opaque identifiers.
    import re
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("revision_id:"):
            assert re.fullmatch(r"[0-9a-f]{32}", stripped.split(":", 1)[1].strip())
        elif stripped.startswith(("semantic_digest:", "source_content_sha256:",
                                  "plan_digest:")):
            assert stripped.endswith("...")
            assert re.fullmatch(
                r"[0-9a-f]{16}", stripped.split(":", 1)[1].strip()[:-3])


def test_a_rejected_workbook_exits_nonzero_with_a_typed_reason(
        tmp_path, tool, plan_file, capsys):
    bad = landing(tmp_path, b"synthetic-not-a-zip", "bad.xlsx")
    assert tool.main([
        "parse", "--file", str(bad), "--plan", str(plan_file),
        "--source-as-of", SOURCE_AS_OF,
    ]) == 1
    assert "not_zip_format" in capsys.readouterr().err


def test_a_rejected_plan_exits_nonzero_with_a_typed_reason(
        tmp_path, tool, workbook, capsys):
    path = tmp_path / "bad_plan.json"
    path.write_text(json.dumps({
        "plan_version": CLASSIFICATION_PLAN_VERSION,
        "expected_row_count": 1, "expected_etf_count": 1,
        "expected_excluded_count": 0,
        "rows": [{"ordinal": 1, "disposition": DISPOSITION_VERIFIED_KRX_ETF,
                  "symbol": "900001"},
                 {"ordinal": 2, "disposition": DISPOSITION_VERIFIED_KRX_ETF,
                  "symbol": "900002"}],
    }), encoding="utf-8")
    assert tool.main([
        "parse", "--file", str(workbook), "--plan", str(path),
        "--source-as-of", SOURCE_AS_OF,
    ]) == 1
    assert "plan_row_count_mismatch" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# candidate — writes to the one registry, never activates, never deletes
# ---------------------------------------------------------------------------

def test_candidate_writes_only_the_one_canonical_registry(
        tmp_path, tool, workbook, plan_file, capsys):
    root = tmp_path / "root"
    assert tool.main([
        "candidate", "--file", str(workbook), "--plan", str(plan_file),
        "--root", str(root), "--source-as-of", SOURCE_AS_OF,
    ]) == 0
    output = capsys.readouterr().out
    assert "Candidate created" in output
    assert "NOT activated" in output
    assert workbook.exists()

    assert registry_path(root).exists()
    stores = sorted(p.name for p in (root / "stores").iterdir())
    assert stores == ["pension_registry.sqlite3"]

    store = open_store(root)
    assert store.read_active_revision() is None
    assert store.read_pointer() is None
    assert store.read_revision_state(revision_id_from(output)) == \
        "CANDIDATE_VALID"


def test_candidate_does_not_migrate_the_registry_schema(
        tmp_path, tool, workbook, plan_file, capsys):
    root = tmp_path / "root"
    tool.main([
        "candidate", "--file", str(workbook), "--plan", str(plan_file),
        "--root", str(root), "--source-as-of", SOURCE_AS_OF,
    ])
    connection = sqlite3.connect(registry_path(root))
    try:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        tables.discard("sqlite_sequence")
        assert tables == {
            "pension_revisions", "pension_activation_events",
            "pension_active_pointer", "pension_reminder_events",
        }
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# summary — evidence only, stored nowhere
# ---------------------------------------------------------------------------

def test_summary_stores_nothing_and_says_so(tmp_path, tool, capsys):
    summary = landing(tmp_path, build_summary_workbook(), "summary.xlsx")
    assert tool.main(["summary", "--file", str(summary)]) == 0
    output = capsys.readouterr().out
    assert "row_count:             5" in output
    assert "aggregate_only:        True" in output
    assert "NON-HOLDINGS, NON-EXECUTION" in output
    assert "not appended to any execution ledger" in output
    assert summary.exists()
    assert not (tmp_path / "root").exists()


# ---------------------------------------------------------------------------
# structural guards
# ---------------------------------------------------------------------------

def test_the_tool_names_only_the_one_registry():
    """The tool may declare exactly one database filename: the pension
    current-state registry. The snapshot database is resolved canonically, so
    a literal here would risk a second, parallel authority."""
    import re
    source = TOOL_PATH.read_text(encoding="utf-8")
    # PT-01: the tool no longer spells the snapshot filename at all -- it goes
    # through the shared canonical resolver, so the only database name it may
    # declare is the current-state registry.
    assert set(re.findall(r"[\w.-]+\.sqlite3", source)) == {
        "pension_registry.sqlite3"}
    assert "holdings_snapshot_store_path" in source
    assert "pension_account_" + "snapshots" not in source
    assert "PensionAccount" + "SnapshotStore" not in source


def test_the_tool_never_reaches_the_execution_ledger():
    source = TOOL_PATH.read_text(encoding="utf-8")
    for banned in ("trade_ledger", "TradeLedgerEvent", "retirement_excel",
                   "pension_trade_ingestion"):
        assert banned not in source


def test_the_tool_has_exactly_one_unlink_call():
    source = TOOL_PATH.read_text(encoding="utf-8")
    assert source.count(".unlink(") == 1
    assert source.count("shutil") == 0
    unlink_line = next(
        index for index, line in enumerate(source.splitlines())
        if ".unlink(" in line)
    body = "\n".join(source.splitlines()[:unlink_line])
    assert "def cmd_activate" in body
    assert "def cmd_candidate" in body.split("def cmd_activate")[0]


def test_the_tool_writes_nowhere_outside_the_named_root():
    source = TOOL_PATH.read_text(encoding="utf-8")
    for banned in (".local/share", "os.environ", "getenv("):
        assert banned not in source
