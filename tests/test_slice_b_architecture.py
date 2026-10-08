import ast
from pathlib import Path

from tokmo.records.holdings_persistence import (
    NAMED_PROGRAM_ALLOWLIST_V1, PENSION_NAMED_PROGRAM_ALLOWLIST,
)
from tokmo.values.receipt_v1 import RECEIPT_REASON_TOKENS


ROOT = Path(__file__).parents[1]
SLICE_B_PROGRAMS = ("programs/holdings_sync.py", "programs/quotes_sync.py")
SLICE_B_NEW = (
    "values/current_holdings.py", "records/current_holdings_store.py",
    *SLICE_B_PROGRAMS,
)


def test_slice_b_programs_use_only_values_and_injected_object_seams():
    for relative in SLICE_B_PROGRAMS:
        source = (ROOT / "src" / "tokmo" / relative).read_text()
        tree = ast.parse(source)
        imported = {
            node.module or "" for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not any(name.startswith(("tokmo.facts", "tokmo.records")) for name in imported)
        assert all(token not in source for token in (
            "/api/", "/oauth", "api.tossInvest.com", "urlopen(",
            "datetime.now(", "os.environ",
        ))


def test_slice_b_has_no_raw_identity_or_historical_holdings_path():
    assert NAMED_PROGRAM_ALLOWLIST_V1 == frozenset()
    assert PENSION_NAMED_PROGRAM_ALLOWLIST == frozenset({"tokmo-v3-pension-import-v1"})
    for relative in SLICE_B_NEW:
        source = (ROOT / "src" / "tokmo" / relative).read_text()
        assert "accountSeq" not in source
        assert "HoldingsSnapshot" not in source
        assert "holdings_snapshots" not in source


def test_slice_b_reason_and_classifier_contract_is_closed():
    assert {
        "failed_provider_auth", "failed_provider_rate_limit",
        "failed_provider_contract", "failed_provider_other", "failed_ceiling",
    } <= RECEIPT_REASON_TOKENS
    definitions = []
    for path in (ROOT / "src" / "tokmo").rglob("*.py"):
        tree = ast.parse(path.read_text())
        if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == "classify_market" for node in ast.walk(tree)):
            definitions.append(path.relative_to(ROOT / "src" / "tokmo").as_posix())
    assert definitions == ["values/current_holdings.py"]
