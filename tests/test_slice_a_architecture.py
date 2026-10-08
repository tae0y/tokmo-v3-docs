import ast
from datetime import datetime, timezone
from pathlib import Path

from tokmo.programs.ops_clock import COLLECTOR_OFFSETS, RUN_CEILINGS, current_tick
from tokmo.records.holdings_persistence import NAMED_PROGRAM_ALLOWLIST_V1
from tokmo.values.material_pack import JobKind
from tokmo.values.receipt_v1 import RECEIPT_REASON_TOKENS
from tokmo.values.session_gate import SESSION_REASON_TOKENS

ROOT = Path(__file__).parents[1]
NEW_SOURCE = [
    "values/ops_tick.py", "values/session_gate.py", "values/material_pack.py",
    "values/receipt_v1.py", "records/atomic_json.py", "records/ops_paths.py",
    "records/material_pack_store.py", "records/receipt_store.py",
    "records/calendar_cache.py", "programs/session_gate.py", "programs/ops_clock.py",
]


def test_slice_b_closed_receipt_tokens_are_declared():
    assert {
        "failed_provider_auth", "failed_provider_rate_limit",
        "failed_provider_contract", "failed_provider_other", "failed_ceiling",
    } <= RECEIPT_REASON_TOKENS


def test_slice_c_receipt_completeness_contract():
    from tokmo.values.receipt_v1 import CompletenessV1

    assert {"partial_incomplete", "partial_quarantine"} <= RECEIPT_REASON_TOKENS
    value = CompletenessV1(1, 2, True, 3, 4)
    assert (value.quarantine_count, value.incomplete_count) == (3, 4)


def test_slice_a_import_and_scope_closure():
    assert NAMED_PROGRAM_ALLOWLIST_V1 == frozenset()
    for relative in NEW_SOURCE:
        text = (ROOT / "src" / "tokmo" / relative).read_text()
        assert "holdings_snapshots" not in text
        assert "HoldingsSnapshotStore" not in text
        assert "tokmo.runtime.wallclock" not in text
        assert "os.environ" not in text
        assert "datetime.now(" not in text
    program_imports = ast.parse((ROOT / "src/tokmo/programs/session_gate.py").read_text())
    assert all(
        not (isinstance(node, ast.ImportFrom) and (node.module or "").startswith("tokmo.records"))
        for node in ast.walk(program_imports)
    )
    assert "open" in SESSION_REASON_TOKENS
    assert "open" not in RECEIPT_REASON_TOKENS


def test_ops_clock_offsets_ceilings_and_injected_clock():
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    context = current_tick(lambda: now)
    assert context.now == now
    assert COLLECTOR_OFFSETS == {
        JobKind.HOLDINGS: 0, JobKind.QUOTES: 20, JobKind.TRADES: 40,
    }
    assert RUN_CEILINGS == {
        JobKind.HOLDINGS: 8, JobKind.QUOTES: 4, JobKind.TRADES: 12,
    }
