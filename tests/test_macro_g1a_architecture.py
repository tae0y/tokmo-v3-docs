"""G1-A architecture/import-guard tests (SDS §8.6 ARCH-01..07, §8.7 SPY-01..03).

Verifies that G1-A source files obey the import-direction contract,
contain no forbidden tokens, and perform zero I/O during evaluation.
"""

import ast
import re
from pathlib import Path
from unittest.mock import patch

SRC_ROOT = Path(__file__).parents[1] / "src" / "tokmo"
TESTS_ROOT = Path(__file__).parents[1] / "tests"

# G1-A source files (new files only)
G1A_VALUES_FILES = [
    SRC_ROOT / "values" / "signal_program_lifecycle.py",
    SRC_ROOT / "values" / "macro_observations.py",
    SRC_ROOT / "values" / "macro_divergence.py",
]
G1A_PROGRAMS_FILES = [
    SRC_ROOT / "programs" / "macro_divergence_analyzer.py",
]
G1A_ALL_SOURCE = G1A_VALUES_FILES + G1A_PROGRAMS_FILES


def _module_imports(path: Path) -> list[str]:
    """Extract all import names from a Python source file."""
    imports: list[str] = []
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
            imports.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return imports


def _is_values_internal(name: str) -> bool:
    return name == "tokmo.values" or name.startswith("tokmo.values.")


# ==========================================================================
# ARCH-01: G1-A values files import only tokmo.values.*
# ==========================================================================


def test_arch01_values_import_direction():
    for path in G1A_VALUES_FILES:
        imports = _module_imports(path)
        tokmo_imports = [name for name in imports if name.startswith("tokmo")]
        for name in tokmo_imports:
            assert _is_values_internal(name), (
                f"{path.name}: import {name!r} is not tokmo.values.*"
            )


# ==========================================================================
# ARCH-02: G1-A programs file imports only tokmo.values.*
# ==========================================================================


def test_arch02_programs_import_direction():
    for path in G1A_PROGRAMS_FILES:
        imports = _module_imports(path)
        tokmo_imports = [name for name in imports if name.startswith("tokmo")]
        for name in tokmo_imports:
            assert _is_values_internal(name), (
                f"{path.name}: import {name!r} is not tokmo.values.*"
            )


# ==========================================================================
# ARCH-03: No forbidden top-level imports
# ==========================================================================

FORBIDDEN_TOP_LEVEL = {
    "telegram",
    "telebot",
    "aiogram",
    "openai",
    "anthropic",
    "google",
    "boto3",
    "requests",
    "httpx",
    "aiohttp",
    "urllib3",
    "websocket",
    "websockets",
    "ccxt",
    "ib_insync",
    "alpaca_trade_api",
}

FORBIDDEN_MODULE_PREFIXES = {
    "urllib.request",
    "urllib.error",
    "http.client",
    "socket",
    "ssl",
    "ftplib",
    "smtplib",
    "poplib",
    "imaplib",
    "telnetlib",
    "socketserver",
    "xmlrpc",
}


def test_arch03_no_forbidden_imports():
    for path in G1A_ALL_SOURCE:
        imports = _module_imports(path)
        top_levels = {name.split(".")[0] for name in imports}
        assert not top_levels & FORBIDDEN_TOP_LEVEL, (
            f"{path.name}: forbidden top-level imports"
        )
        for name in imports:
            for banned in FORBIDDEN_MODULE_PREFIXES:
                assert name != banned and not name.startswith(banned + "."), (
                    f"{path.name}: forbidden module {name!r}"
                )


# ==========================================================================
# ARCH-04: No datetime.now(), utcnow(), os.environ, getenv() in G1-A source
# ==========================================================================

FORBIDDEN_SOURCE_PATTERNS = (
    "os.environ",
    "getenv(",
    "datetime.now(",
    "utcnow(",
    "urlopen(",
)


def test_arch04_no_forbidden_source_patterns():
    for path in G1A_ALL_SOURCE:
        text = path.read_text()
        for pattern in FORBIDDEN_SOURCE_PATTERNS:
            assert pattern not in text, f"{path.name}: forbidden pattern {pattern!r}"


# ==========================================================================
# ARCH-05: No http.client, socket, ssl, urllib.request in G1-A source (AST)
# ==========================================================================


def test_arch05_no_network_modules_in_ast():
    for path in G1A_ALL_SOURCE:
        imports = _module_imports(path)
        for name in imports:
            for banned in ("http.client", "socket", "ssl", "urllib.request"):
                assert name != banned and not name.startswith(banned + "."), (
                    f"{path.name}: network module {name!r}"
                )


# ==========================================================================
# ARCH-06: No buy/sell/target-price/ceiling/floor vocabulary in pack output
# ==========================================================================

ARCH06_FORBIDDEN_VOCAB = (
    "매수",
    "매도",
    "목표가",
    "천장",
    "바닥",
    "target price",
    "ceiling",
    "floor",
    "buy",
    "sell",
    "order",
)


def _scan_pack_strings(pack) -> list[str]:
    """Collect all string fields from a pack for vocabulary scanning."""
    strings = [
        pack.program_id,
        pack.program_version,
        pack.program_kind,
        pack.formula_version,
        pack.coverage_reason,
        pack.primary_state,
        pack.lifecycle_status,
        pack.lifecycle_reason,
        pack.confidence_ceiling,
    ]
    for comp in pack.components:
        strings.append(comp.reason)
        strings.extend(comp.details.values())
    strings.extend(pack.evidence)
    strings.extend(pack.counter_evidence)
    strings.extend(pack.limitations)
    # window_summary fields
    ws = pack.window_summary
    strings.extend(
        [
            ws.as_of,
            ws.latest_common_session,
            ws.kospi_latest_close,
            ws.proxy_latest_close,
            ws.kospi_percentile or "",
            ws.proxy_percentile or "",
            ws.foreign_rolling_flow_20 or "",
            ws.foreign_prior_flow_20 or "",
            ws.fx_return_8 or "",
        ]
    )
    for sc in pack.symbol_contacts:
        strings.extend([sc.symbol, sc.role, sc.latest_session, sc.latest_close or ""])
    return strings


def test_arch06_no_trading_vocabulary_in_t1_pack():
    """Scan T1 pack output for forbidden trading vocabulary."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_macro_divergence_analyzer import _core_lifecycle, _t1_bundle

    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of)

    from tokmo.programs.macro_divergence_analyzer import evaluate_macro_divergence

    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)

    all_strings = _scan_pack_strings(pack)
    for text in all_strings:
        lower = text.lower()
        for forbidden in ARCH06_FORBIDDEN_VOCAB:
            assert forbidden.lower() not in lower, (
                f"forbidden vocabulary {forbidden!r} found in pack string: {text!r}"
            )


def test_arch06_no_trading_vocabulary_in_must_not_fire_pack():
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_macro_divergence_analyzer import _core_lifecycle, _must_not_fire_bundle

    bundle, as_of = _must_not_fire_bundle()
    lifecycle = _core_lifecycle(as_of)

    from tokmo.programs.macro_divergence_analyzer import evaluate_macro_divergence

    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)

    all_strings = _scan_pack_strings(pack)
    for text in all_strings:
        lower = text.lower()
        for forbidden in ARCH06_FORBIDDEN_VOCAB:
            assert forbidden.lower() not in lower, (
                f"forbidden vocabulary {forbidden!r} found in: {text!r}"
            )


# ==========================================================================
# ARCH-07: No credential/token/URL/path/private-ID in G1-A pack fields
# ==========================================================================

ARCH07_PATTERNS = {
    "credential": re.compile(
        r"(?i)(bearer|token|api[_-]?key|secret|password|passwd|authorization)"
    ),
    "url": re.compile(r"(?i)(https?://|www\.|\.com|\.kr|\.net)"),
    "path": re.compile(r"(/Users/|/home/|C:\\\\|~/|\.local/)"),
    # m7: add the two missing SDS pattern categories
    "private_id": re.compile(r"\b[A-Za-z0-9_-]{32,}\b"),
    "raw_fragment": re.compile(r'\{.*"|<[a-z]+'),
}

# 64-hex digests are allowed despite matching private_id length
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# Known SDS-defined long identifiers that are public enum values, not secrets
_ARCH07_KNOWN_LONG_TOKENS = frozenset(
    {
        "OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL",
        "INDEX_HIGH_FX_PROXY_HIGH",
        "FOREIGN_OUTFLOW_CONFIRMATION",
        "macro-divergence-v1",
        "INDICATOR_INVESTOR_TRADING",
    }
)


def _scan_for_arch07(text: str) -> str | None:
    """Return the first pattern name that matches, or None."""
    for name, pattern in ARCH07_PATTERNS.items():
        if name == "private_id":
            for m in pattern.finditer(text):
                tok = m.group()
                if _HEX64_RE.match(tok):
                    continue
                if tok in _ARCH07_KNOWN_LONG_TOKENS:
                    continue
                return name
        else:
            if pattern.search(text):
                return name
    return None


def test_arch07_no_privacy_leaks_in_t1_pack():
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_macro_divergence_analyzer import _core_lifecycle, _t1_bundle

    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of)

    from tokmo.programs.macro_divergence_analyzer import evaluate_macro_divergence

    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)

    all_strings = _scan_pack_strings(pack)
    for text in all_strings:
        hit = _scan_for_arch07(text)
        assert hit is None, f"{hit} pattern matched in pack string: {text!r}"


def test_arch07_no_privacy_leaks_in_t2_pack():
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_macro_divergence_analyzer import _core_lifecycle, _t2_bundle

    bundle, as_of = _t2_bundle()
    lifecycle = _core_lifecycle(as_of)

    from tokmo.programs.macro_divergence_analyzer import evaluate_macro_divergence

    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)

    all_strings = _scan_pack_strings(pack)
    for text in all_strings:
        hit = _scan_for_arch07(text)
        assert hit is None, f"{hit} pattern matched in pack string: {text!r}"


# m7: add T3 and MUST_NOT_FIRE pack scans
def test_arch07_no_privacy_leaks_in_t3_pack():
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_macro_divergence_analyzer import _core_lifecycle, _t3_bundle

    bundle, as_of = _t3_bundle()
    lifecycle = _core_lifecycle(as_of)

    from tokmo.programs.macro_divergence_analyzer import evaluate_macro_divergence

    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)

    all_strings = _scan_pack_strings(pack)
    for text in all_strings:
        hit = _scan_for_arch07(text)
        assert hit is None, f"{hit} pattern matched in pack string: {text!r}"


def test_arch07_no_privacy_leaks_in_must_not_fire_pack():
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_macro_divergence_analyzer import _core_lifecycle, _must_not_fire_bundle

    bundle, as_of = _must_not_fire_bundle()
    lifecycle = _core_lifecycle(as_of)

    from tokmo.programs.macro_divergence_analyzer import evaluate_macro_divergence

    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)

    all_strings = _scan_pack_strings(pack)
    for text in all_strings:
        hit = _scan_for_arch07(text)
        assert hit is None, f"{hit} pattern matched in pack string: {text!r}"


# ==========================================================================
# SPY-01: Analyzer does not open files during evaluation
# ==========================================================================


def test_spy01_no_file_io_during_evaluation():
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_macro_divergence_analyzer import _core_lifecycle, _t1_bundle

    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of)

    from tokmo.programs.macro_divergence_analyzer import evaluate_macro_divergence

    def blocking_open(*args, **kwargs):
        raise AssertionError("file I/O during evaluate")

    # m7: patch both builtins.open and pathlib.Path.open
    with (
        patch("builtins.open", blocking_open),
        patch("pathlib.Path.open", blocking_open),
    ):
        pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
        assert pack is not None


# ==========================================================================
# SPY-02: Analyzer does not make network calls during evaluation
# ==========================================================================


def test_spy02_no_network_during_evaluation():
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_macro_divergence_analyzer import _core_lifecycle, _t1_bundle

    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of)

    from tokmo.programs.macro_divergence_analyzer import evaluate_macro_divergence

    def blocking_socket(*args, **kwargs):
        raise AssertionError("network during evaluate")

    # m7: add http.client.HTTPConnection to the patched network entry points
    with (
        patch("socket.socket", blocking_socket),
        patch("urllib.request.urlopen", blocking_socket),
        patch("http.client.HTTPConnection", blocking_socket),
    ):
        pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
        assert pack is not None


# ==========================================================================
# SPY-03: Analyzer does not access os.getenv during evaluation
# ==========================================================================


def test_spy03_no_getenv_during_evaluation():
    import os
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_macro_divergence_analyzer import _core_lifecycle, _t1_bundle

    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of)

    from tokmo.programs.macro_divergence_analyzer import evaluate_macro_divergence

    def blocking_getenv(*args, **kwargs):
        raise AssertionError("getenv during evaluate")

    with patch.object(os, "getenv", blocking_getenv):
        pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
        assert pack is not None


# ==========================================================================
# Additional architectural checks
# ==========================================================================


def test_no_subprocess_import():
    """G1-A must not import subprocess."""
    for path in G1A_ALL_SOURCE:
        imports = _module_imports(path)
        assert "subprocess" not in imports, path.name
        assert not any(name.startswith("subprocess.") for name in imports), path.name


def test_no_db_storage_import():
    """G1-A must not import sqlite3, sqlalchemy, shelve, pickle, etc."""
    forbidden_db = {"sqlite3", "sqlalchemy", "shelve", "pickle", "json"}
    # Note: json is allowed for serialization, but let's verify it's used
    # for serialization only, not for DB access.
    # Actually json is needed for digest computation — remove from forbidden.
    forbidden_db.discard("json")
    for path in G1A_ALL_SOURCE:
        imports = _module_imports(path)
        for name in imports:
            top = name.split(".")[0]
            assert top not in forbidden_db, f"{path.name}: {name}"


def test_all_g1a_files_exist():
    for path in G1A_ALL_SOURCE:
        assert path.exists(), f"missing: {path}"


def test_no_os_import_in_source():
    """G1-A source should not import os at all."""
    for path in G1A_ALL_SOURCE:
        imports = _module_imports(path)
        for name in imports:
            assert name != "os" and not name.startswith("os."), (
                f"{path.name}: imports os"
            )


def test_g1a_does_not_import_facts_records_composition_runtime():
    """G1-A must not import from forbidden layers (§6.7)."""
    forbidden_prefixes = (
        "tokmo.facts",
        "tokmo.records",
        "tokmo.composition",
        "tokmo.runtime",
        "tokmo.observability",
    )
    for path in G1A_ALL_SOURCE:
        imports = _module_imports(path)
        for name in imports:
            for prefix in forbidden_prefixes:
                assert name != prefix and not name.startswith(prefix + "."), (
                    f"{path.name}: forbidden layer import {name!r}"
                )
