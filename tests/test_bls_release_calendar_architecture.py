"""G2 Delta SDS §6 / §8.1 acceptance A38-A39: the dedicated BLS seal (M3).

This is a separate file rather than a subset bolted into an existing seal
because every FRED seal keys on a ``*fred*`` or ``fred_query``/``macro_fred``
path glob that the BLS module deliberately does not match. Without this file
the only new ``src`` module in the delta would carry no invariant test at all.

It enforces the exact BLS path set, the stdlib-only import allowlist, the
absence of every forbidden network module, the absence of FRED credential and
transport symbols, the structural no-redirect assertion on the tool-side
transport, and the campaign/effect vocabulary exclusions — and it proves its
own scanner is not vacuous.
"""

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]

PARSER_MODULE = "src/tokmo/facts/bls_release_calendar.py"
TRANSPORT_OWNER = "tools/fred_ondemand_canary.py"
SEAL_TESTS = (
    "tests/test_bls_release_calendar.py",
    "tests/test_bls_release_calendar_architecture.py",
)

# The exact stdlib import allowlist for the pure parser.
ALLOWED_PARSER_IMPORTS = {
    "dataclasses", "dataclasses.dataclass",
    "datetime", "datetime.date", "datetime.datetime", "datetime.timedelta",
    "datetime.timezone",
    "hashlib", "re", "zoneinfo", "zoneinfo.ZoneInfo",
}

# Reused verbatim from tests/test_fred_query_architecture.py:37-48.
CAMPAIGN_TOKENS = (
    "campaign", "r2_g_live", "r3_g_live", "r4_g_live", "g_live_manifest",
    "execute_r4", "manifest_digest", "manifest_revision", "manifest_code",
    "quarantine", "invocation_identity", "high_water", "fredmaterialstore",
    "fred_material_store", "reserve_campaign", "ordinal", "campaign_cap",
    "purpose_cumulative_ceiling", "store_material", "store_page",
)
EFFECT_TOKENS = (
    "cron", "launchagent", "launchd", "plist", "telegram", "webhook", "smtp",
    "place_order", "submit_order", "order_id", "orderid", "send_alert",
)

FRED_CREDENTIAL_SYMBOLS = (
    "load_fred_api_key", "FredLiveTransport", "build_live_opener", "api_key",
    "fred.env", "credential",
)


def _architecture():
    spec = importlib.util.spec_from_file_location(
        "repository_architecture", ROOT / "tests/test_architecture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _imports(relative):
    names = []
    for node in ast.walk(ast.parse((ROOT / relative).read_text())):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def scan_tokens(source: str, tokens) -> set:
    """Every identifier, import name and non-docstring literal, lowercased.

    Prose in a docstring may name the boundary the module is separated from;
    a reachable symbol may not. This is the scanner A39 proves non-vacuous.
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)) or not body:
            continue
        head = body[0]
        if (isinstance(head, ast.Expr) and isinstance(head.value, ast.Constant)
                and isinstance(head.value.value, str)):
            docstrings.add(id(head.value))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, ast.alias):
            found.update((node.name, node.asname or ""))
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.keyword) and node.arg:
            found.add(node.arg)
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            found.add(node.value)
    lowered = {item.lower() for item in found if item}
    return {token for token in tokens
            for symbol in lowered if token in symbol}


def test_a38_the_bls_path_set_is_exactly_one_parser_and_one_transport_owner():
    discovered = {
        path.relative_to(ROOT).as_posix()
        for base in (ROOT / "src", ROOT / "tools", ROOT / "tests")
        for path in base.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
        and "bls" in path.name.lower()
    }
    assert discovered == {PARSER_MODULE, *SEAL_TESTS,
                          "tests/fixtures/bls/official/bls-ics.md"}
    assert (ROOT / TRANSPORT_OWNER).is_file()
    # The transport boundary — and the provider host literal — exist in
    # exactly one file, and it is not under ``src``.
    owners = [path.relative_to(ROOT).as_posix()
              for base in (ROOT / "src", ROOT / "tools")
              for path in base.rglob("*.py")
              if "__pycache__" not in path.parts
              and "bls.gov" in path.read_text()]
    assert owners == [TRANSPORT_OWNER]


def test_a38_the_parser_imports_stdlib_only_and_nothing_from_the_inner_layers():
    names = _imports(PARSER_MODULE)
    assert set(names) <= ALLOWED_PARSER_IMPORTS, sorted(set(names)
                                                        - ALLOWED_PARSER_IMPORTS)
    assert not any(name.startswith("tokmo") for name in names), names
    forbidden_layers = ("tokmo.records", "tokmo.programs", "tokmo.runtime",
                        "tokmo.composition", "tokmo.presentation")
    assert not any(name.startswith(forbidden_layers) for name in names)
    architecture = _architecture()
    assert not any(name.startswith(architecture.LIVE_TRANSPORT_IMPORT_PREFIXES)
                   for name in names)


def test_a38_the_parser_names_no_network_module_and_registers_no_transport():
    architecture = _architecture()
    names = _imports(PARSER_MODULE)
    for name in names:
        for banned in architecture.FORBIDDEN_MODULE_PREFIXES:
            assert name != banned and not name.startswith(banned + "."), name
    top_levels = {name.split(".")[0] for name in names}
    assert not top_levels & architecture.FORBIDDEN_TOP_LEVEL
    # It is deliberately NOT a registered live transport module.
    assert PARSER_MODULE.removeprefix("src/tokmo/") not in (
        architecture.LIVE_TRANSPORT_MODULES)
    source = (ROOT / PARSER_MODULE).read_text()
    for pattern in architecture.FORBIDDEN_SOURCE_PATTERNS:
        assert pattern not in source, pattern


def test_a38_the_parser_names_no_fred_credential_or_transport_symbol():
    source = (ROOT / PARSER_MODULE).read_text()
    for symbol in FRED_CREDENTIAL_SYMBOLS:
        assert symbol not in source, symbol
    assert "bls.gov" not in source
    assert "http" not in source.lower()


def test_a38_the_parser_names_no_campaign_scheduler_alert_or_order_token():
    source = (ROOT / PARSER_MODULE).read_text()
    assert scan_tokens(source, CAMPAIGN_TOKENS) == set()
    assert scan_tokens(source, EFFECT_TOKENS) == set()


def test_a38_the_tool_side_transport_follows_no_redirect_structurally():
    """Mirrors facts/fred_live.py:44-50 — asserted on the AST, not by prose."""
    tree = ast.parse((ROOT / TRANSPORT_OWNER).read_text())
    handler = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.ClassDef) and node.name == "_NoRedirect")
    assert [ast.unparse(base) for base in handler.bases] == [
        "urllib.request.HTTPRedirectHandler"]
    method = next(node for node in handler.body
                  if isinstance(node, ast.FunctionDef)
                  and node.name == "redirect_request")
    assert len(method.body) == 1
    assert isinstance(method.body[0], ast.Return)
    assert ast.unparse(method.body[0]) == "return None"
    builder = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef)
                   and node.name == "build_bls_opener")
    built = ast.unparse(builder)
    assert "urllib.request.ProxyHandler({})" in built
    assert "_NoRedirect()" in built


def test_a38_the_parser_holds_no_product_effect_symbol():
    source = (ROOT / PARSER_MODULE).read_text().lower()
    for token in ("telegram", "order", "model", "cadence", "openai",
                  "anthropic", "alert", "notify", "schedule_agent"):
        assert token not in source, token


def test_a39_the_token_scanner_is_not_vacuous_against_an_injected_mutant():
    """A seal that inspected an empty or mis-globbed path set cannot pass."""
    mutant = (
        '"""A docstring naming campaign and telegram must not be flagged."""\n'
        "def handler(campaign_cap=1):\n"
        "    launchagent = 'send_alert'\n"
        "    return campaign_cap, launchagent\n"
    )
    campaign_hits = scan_tokens(mutant, CAMPAIGN_TOKENS)
    effect_hits = scan_tokens(mutant, EFFECT_TOKENS)
    assert "campaign_cap" in campaign_hits and "campaign" in campaign_hits
    assert "launchagent" in effect_hits and "send_alert" in effect_hits
    # The same scanner finds nothing in the real module, so the empty result
    # above is a real absence rather than a broken scan.
    assert scan_tokens((ROOT / PARSER_MODULE).read_text(),
                       CAMPAIGN_TOKENS + EFFECT_TOKENS) == set()
    # Docstring prose alone is never a hit.
    assert scan_tokens('"""campaign telegram cron"""\nx = 1\n',
                       CAMPAIGN_TOKENS + EFFECT_TOKENS) == set()


def test_a39_the_path_glob_itself_is_proven_non_empty():
    discovered = [path for path in (ROOT / "src").rglob("bls_*.py")
                  if "__pycache__" not in path.parts]
    assert [path.relative_to(ROOT).as_posix() for path in discovered] == [
        PARSER_MODULE]
    assert (ROOT / PARSER_MODULE).read_text().strip() != ""


def test_the_new_bls_module_keeps_the_repository_layer_directions():
    architecture = _architecture()
    modules = dict(architecture.tokmo_modules())
    key = PARSER_MODULE.removeprefix("src/tokmo/")
    assert key in modules
    architecture.test_layer_import_directions_match_contract()
    architecture.test_no_forbidden_provider_network_or_order_imports_anywhere()
    architecture.test_no_process_env_or_direct_clock_in_source()
    architecture.test_no_assert_statements_in_src()
    architecture.test_no_v2_or_legacy_path_references_in_source()
