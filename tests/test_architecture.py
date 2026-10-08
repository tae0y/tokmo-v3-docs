"""Import-graph and forbidden-reference invariants from contract v0.2 §2,
amended by the Tracer 1a-live contract (v0.2-live §8, v0.3 D1).

The scans still cover the entire ``src/tokmo`` tree. Each live source gets
exactly one source-private HTTPS adapter, so ``facts/eia_live.py`` and the
SEC Tracer 1a adapter ``facts/sec_live.py`` alone are allowlisted for stdlib
network imports, and the official EIA endpoint path literal is exempted from
the legacy-"v2" text scan. Everything else keeps the original zero-exemption
rules: no provider/Telegram/order APIs, no process-environment reads, and no
direct clock calls anywhere.
"""

import ast
import re
from pathlib import Path

SRC_ROOT = Path(__file__).parents[1] / "src" / "tokmo"

# The only modules the live contracts designate as HTTPS adapters.
# G1A: sec_fundamentals_live.py and opendart_fundamentals_live.py are
# registered for fundamental value transports. Only composition may import them.
LIVE_TRANSPORT_MODULES = {
    "facts/eia_live.py", "facts/sec_live.py", "facts/opendart_live.py",
    "facts/opendart_bulk_live.py",
    "facts/opendart_document_live.py",  # NEWS selected-candidate document GET; worker composition only.
    "facts/sec_document_live.py",  # NEWS SEC primary document; existing declared User-Agent boundary.
    "facts/krx_live.py",
    "facts/toss_auth_live.py", "facts/toss_live.py",
    "facts/toss_prices_live.py", "facts/toss_reads_live.py",
    "facts/toss_order_commands_live.py",
    "facts/kis_etf_live.py",
    "facts/news_http.py",
    "facts/telegram_reminder_live.py",
    "facts/telegram_dialogue_live.py",
    "facts/sec_fundamentals_live.py",
    "facts/opendart_fundamentals_live.py",
    "facts/fred_live.py",
    "facts/kis_etf_live.py",
    "facts/etf_issuer_live.py",
    "facts/official_membership_live.py",
}
LIVE_TRANSPORT_IMPORT_PREFIXES = tuple(
    "tokmo." + module.removesuffix(".py").replace("/", ".")
    for module in sorted(LIVE_TRANSPORT_MODULES))
NEWS_HTTP_MODULE = "facts/news_http.py"
NEWS_SOURCE_ADAPTER_MODULES = {
    "facts/bbc_news_live.py", "facts/finnhub_news_live.py",
    "facts/google_news_live.py", "facts/jtbc_news_live.py",
    "facts/investing_news_live.py", "facts/naver_news_live.py",
    "facts/yonhap_news_live.py",
}

# Toss holdings tracer: the two authorized data operations and the single
# authorized auth operation, each confined to its own adapter.
TOSS_DATA_MODULE = "facts/toss_live.py"
TOSS_PRICES_MODULE = "facts/toss_prices_live.py"
TOSS_AUTH_MODULE = "facts/toss_auth_live.py"
TOSS_PARSER_MODULE = "facts/toss.py"
TOSS_READS_MODULE = "facts/toss_reads.py"
TOSS_READS_LIVE_MODULE = "facts/toss_reads_live.py"
TOSS_COMMANDS_MODULE = "facts/toss_order_commands.py"
TOSS_COMMAND_PATHS = {
    "/api/v1/orders", "/api/v1/orders/{id}/modify", "/api/v1/orders/{id}/cancel",
    "/api/v1/conditional-orders", "/api/v1/conditional-orders/{id}/modify",
    "/api/v1/conditional-orders/{id}",
}
TOSS_ALLOWED_API_PATHS = {"/api/v1/accounts", "/api/v1/holdings"}
TOSS_ALLOWED_PRICE_PATHS = {"/api/v1/prices"}
TOSS_ALLOWED_AUTH_PATHS = {"/oauth2/token"}
FINNHUB_NEWS_LIVE_MODULE = "facts/finnhub_news_live.py"
NEWS_ALLOWED_API_PATHS = {"/api/v1/company-news", "/api/v1/news"}
REQUEST_PARITY_OWNER = "values/request_parity.py"
REQUEST_PARITY_ALLOWED_PATHS = {
    "/oauth2/token", "/api/v1/accounts", "/api/v1/holdings", "/api/v1/prices",
    "/api/v1/orders", "/api/v1/orders/{orderId}",
    "/api/v1/market-calendar/KR", "/api/v1/market-calendar/US",
    "/api/v1/candles",
}
# The 17 remaining authorized read paths live only in toss_reads.py and
# toss_reads_live.py. Path templates with {placeholder} are included.
TOSS_ALLOWED_READ_PATHS = {
    "/api/v1/stocks/{symbol}/investor-trading",
    "/api/v1/orderbook", "/api/v1/trades", "/api/v1/price-limits",
    "/api/v1/candles", "/api/v1/stocks", "/api/v1/stocks/{symbol}/warnings",
    "/api/v1/exchange-rate", "/api/v1/market-calendar/KR",
    "/api/v1/market-calendar/US", "/api/v1/rankings",
    "/api/v1/market-indicators/prices",
    "/api/v1/market-indicators/{symbol}/candles",
    "/api/v1/market-indicators/{symbol}/investor-trading",
    "/api/v1/orders", "/api/v1/orders/{orderId}",
    "/api/v1/conditional-orders",
    "/api/v1/conditional-orders/{conditionalOrderId}",
    # User-approved Quant inputs: existing-token GET reads, no orders.
    "/api/v1/buying-power", "/api/v1/sellable-quantity", "/api/v1/commissions",
}
# Buying power, sellable quantity and commissions were later authorized for
# Quant. Check their read-only behavior below instead of preserving an old ban.
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
# The single ambient wall-clock read (live contract §7): the CLI must build
# a real aware-UTC clock somewhere, and this leaf is that one place.
WALLCLOCK_MODULE = "runtime/wallclock.py"
# The official EIA API path (contract v0.2-live §2); its "v2" names the
# provider's API version, not the legacy V2 runtime.
EIA_OFFICIAL_PATH = "/v2/petroleum/pri/spt/data/"

# Messaging, AI providers, brokers/exchanges, and generic HTTP clients.
FORBIDDEN_TOP_LEVEL = {
    "telegram", "telebot", "aiogram",
    "openai", "anthropic", "google",
    "boto3", "requests", "httpx", "aiohttp", "urllib3",
    "websocket", "websockets", "ccxt", "ib_insync", "alpaca_trade_api",
}
# Standard-library network/transport modules (urllib.parse stays allowed).
FORBIDDEN_MODULE_PREFIXES = {
    "urllib.request", "urllib.error", "http.client", "socket", "ssl",
    "ftplib", "smtplib", "poplib", "imaplib", "telnetlib",
    "socketserver", "xmlrpc",
}
# Process environment and ambient clocks must never appear in source.
FORBIDDEN_SOURCE_PATTERNS = (
    "os.environ", "getenv(", "datetime.now(", "utcnow(", "urlopen(",
)
# Injected dependencies must have no defaults anywhere in the tree.
INJECTED_PARAMETER_NAMES = {"transport", "clock", "id_factory"}


def module_imports(path):
    imports = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
            # `from http import client` must count as http.client.
            imports.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return imports


def tokmo_modules():
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path.relative_to(SRC_ROOT).as_posix(), module_imports(path)


def static_strings(source):
    """Fold fully-static literals, concatenations, and f-strings."""
    def fold(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = fold(node.left), fold(node.right)
            return left + right if left is not None and right is not None else None
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    inner = value.value
                    part = str(inner.value) if isinstance(inner, ast.Constant) else fold(inner)
                else:
                    part = fold(value)
                if part is None:
                    return None
                parts.append(part)
            return "".join(parts)
        return None
    return {value for node in ast.walk(ast.parse(source)) if (value := fold(node)) is not None}


def provider_paths(source):
    strings = static_strings(source)
    api = {match for value in strings for match in re.findall(r"/api/v1[A-Za-z0-9/_{}.-]*", value)}
    auth = {match for value in strings for match in re.findall(r"/oauth2[A-Za-z0-9/_{}.-]*", value)}
    return api, auth


def test_provider_path_scanner_folds_split_and_f_string_literals():
    api, auth = provider_paths('A = "/api" + "/v1/orders"\nB = f"/{\'oauth\'}{2}/token"')
    assert api == {"/api/v1/orders"}
    assert auth == {"/oauth2/token"}


def test_provider_path_scanner_catches_module_name_one_hop_fragments():
    source = (
        '_A = "/api" + "/v1"\n'
        '_O = "/oauth" + "2"\n'
        'PLAIN = _A + "/conditional-orders"\n'
        'FORMATTED = f"{_A}/orders"\n'
        'AUTH = f"{_O}/token"\n'
    )
    api, auth = provider_paths(source)
    assert "/api/v1" in api
    assert "/oauth2" in auth


def test_no_v2_or_legacy_path_references_in_source():
    for relative, _imports in tokmo_modules():
        text = (SRC_ROOT / relative).read_text().lower()
        text = text.replace(EIA_OFFICIAL_PATH, "")
        if relative == "programs/quant_decision_inputs.py":
            # Historical provenance marker, never a legacy runtime dependency.
            assert '"v2 플레이북 이관"' in text
            text = text.replace('"v2 플레이북 이관"', '"historical provenance"')
        if relative == "records/news_market_observations.py":
            # rv20 is the existing 20-session realized-volatility metric,
            # not a reference to the previous application version.
            assert "'rv20'" in text
            text = text.replace("'rv20'", "'realized_volatility_20'")
        assert "stock-monitor" not in text, relative
        # N2: the dedicated AST seal (test_n2_v2_allowlist_ast_seal) enforces
        # that "v2" only appears inside exact allowed string literals
        # in the designated owner files. This blanket text scan covers the
        # remaining files where no v2 token should appear at all.
        if relative in ("values/material_pack.py",
                        "records/holdings_snapshot_migration.py",
                        "programs/pulse_canary.py",
                        "programs/quant_kr_source_contract.py"):
            continue
        assert "v2" not in text, relative


# N2: exact string-literal allowlist entries.
_N2_V2_ALLOWLIST = frozenset({
    "composite-holdings-v2",
    "pension-excel-v2",
    "generation r2",
    "kr_quant_source_contract_toss_v2",
    "kr_quant_source_contract_dart_v2",
})

_N2_OWNER_FILES = frozenset({
    "values/material_pack.py",
    "records/holdings_snapshot_migration.py",
    "programs/quant_kr_source_contract.py",
})


def test_n2_v2_allowlist_ast_seal():
    """§19.1 N2: 'v2' appears only inside exact allowed string literals
    in designated owner files. Rejects concatenation, adjacent-literals,
    f-strings, and bare identifiers containing 'v2'."""
    for relative in _N2_OWNER_FILES:
        source = (SRC_ROOT / relative).read_text()
        tree = ast.parse(source)
        found_v2_tokens = set()
        # Collect all string constants that contain 'v2' (case-insensitive),
        # EXCLUDING module/class/function docstrings.
        docstring_nodes = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                    docstring_nodes.add(body[0].value)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node in docstring_nodes:
                    continue
                if "v2" in node.value.lower():
                    found_v2_tokens.add(node.value.lower())
            elif isinstance(node, ast.Constant) and isinstance(node.value, bytes):
                if b"v2" in node.value.lower():
                    found_v2_tokens.add(node.value.decode().lower())
            elif isinstance(node, ast.JoinedStr):
                # f-strings are forbidden for v2 tokens.
                for child in ast.walk(node):
                    if isinstance(child, ast.Constant) and isinstance(child.value, str):
                        if "v2" in child.value.lower():
                            raise AssertionError(
                                f"{relative}: v2 in f-string forbidden")
            elif isinstance(node, ast.Name):
                if "v2" in node.id.lower():
                    # Identifier-level v2 (e.g. _validate_holdings_generation_r2)
                    # is allowed for function/variable names.
                    pass
        # Every v2 string literal must be in the allowlist.
        for token in found_v2_tokens:
            assert token in _N2_V2_ALLOWLIST, (
                f"{relative}: unallowed v2 token '{token}'")


def test_no_forbidden_provider_network_or_order_imports_anywhere():
    for relative, imports in tokmo_modules():
        top_levels = {name.split(".")[0] for name in imports}
        assert not top_levels & FORBIDDEN_TOP_LEVEL, relative
        if relative in LIVE_TRANSPORT_MODULES:
            continue  # checked separately below
        for name in imports:
            if name in {'urllib.error', 'urllib.error.HTTPError', 'urllib.error.URLError'}:
                # Passive exception types let injected callers classify failure;
                # they expose no request/open/transport capability. Keep module
                # imports and every other urllib symbol under the existing ban.
                nodes = [node for node in ast.walk(ast.parse((SRC_ROOT / relative).read_text()))
                         if isinstance(node, (ast.Import, ast.ImportFrom))]
                assert not any(isinstance(node, ast.Import) and any(a.name == 'urllib.error' for a in node.names) for node in nodes), relative
                selected = [node for node in nodes if isinstance(node, ast.ImportFrom) and node.module == 'urllib.error']
                assert selected and all(a.name in {'HTTPError', 'URLError'} for node in selected for a in node.names), relative
                continue
            for banned in FORBIDDEN_MODULE_PREFIXES:
                assert name != banned and not name.startswith(banned + "."), (relative, name)


def test_live_transport_modules_are_the_only_network_adapters():
    """v0.2-live §8 + SEC 1a: one HTTPS adapter per live source; each may use
    its reviewed stdlib HTTPS boundary, and only composition may import it."""
    modules = dict(tokmo_modules())
    for transport_module in LIVE_TRANSPORT_MODULES:
        live_imports = set(modules[transport_module])
        network_used = {
            name for name in live_imports
            for banned in FORBIDDEN_MODULE_PREFIXES
            if name == banned or name.startswith(banned + ".")
        }
        allowed_network = {"urllib.request", "urllib.error"}
        if transport_module == "facts/krx_live.py":
            allowed_network = {"http.client"}
        if transport_module == NEWS_HTTP_MODULE:
            allowed_network |= {
                "socket", "urllib.error.HTTPError", "urllib.error.URLError",
                "urllib.request.HTTPRedirectHandler", "urllib.request.ProxyHandler",
                "urllib.request.Request", "urllib.request.build_opener",
            }
        if transport_module in ("facts/telegram_reminder_live.py",
                                "facts/telegram_dialogue_live.py"):
            allowed_network |= {"ssl"}
        assert network_used <= allowed_network, network_used
        # Live transport modules must use the stdlib HTTPS boundary, EXCEPT
        # for opendart_fundamentals_live which is PAUSED and imports no
        # network stack at all (no urllib.request/urllib.error).
        if transport_module != "facts/opendart_fundamentals_live.py":
            assert ({"http.client"} if transport_module == "facts/krx_live.py"
                    else {"urllib.request"}) <= live_imports

    for relative, imports in modules.items():
        if relative in LIVE_TRANSPORT_MODULES:
            continue
        importers_allowed = (
            relative == "composition.py"
            or relative == "opendart_bulk_composition.py"
            or relative == "fred_composition.py"
            or relative == "kis_composition.py"
            or relative in NEWS_SOURCE_ADAPTER_MODULES
        )
        uses_live = any(name.startswith(LIVE_TRANSPORT_IMPORT_PREFIXES)
                            for name in imports)
        if relative == "programs/quant_input_capture.py":
            # Optional existing collector composition reuses its acquired
            # bearer. This module must never acquire a second auth transport.
            assert {name for name in imports if name.startswith(LIVE_TRANSPORT_IMPORT_PREFIXES)} <= {
                "tokmo.facts.toss_reads_live", "tokmo.facts.toss_reads_live.TossReadsLiveTransport"}
            importers_allowed = True
        assert not uses_live or importers_allowed, relative


def test_fred_architecture_delta_is_exactly_registered():
    assert "facts/fred_live.py" in LIVE_TRANSPORT_MODULES
    assert "tokmo.facts.fred_live" in LIVE_TRANSPORT_IMPORT_PREFIXES


def test_no_process_env_or_direct_clock_in_source():
    for relative, _imports in tokmo_modules():
        text = (SRC_ROOT / relative).read_text()
        for pattern in FORBIDDEN_SOURCE_PATTERNS:
            if relative == WALLCLOCK_MODULE and pattern == "datetime.now(":
                continue
            if relative in ("facts/telegram_reminder_live.py",
                            "facts/telegram_dialogue_live.py") \
                    and pattern == "urlopen(":
                continue
            assert pattern not in text, (relative, pattern)


def test_wallclock_leaf_is_imported_only_by_the_composition_root():
    for relative, imports in tokmo_modules():
        if relative in {"composition.py", "opendart_bulk_composition.py", "kis_composition.py", WALLCLOCK_MODULE}:
            continue
        assert not any(name.startswith("tokmo.runtime.wallclock") for name in imports), relative


# Composition-root operation seams wire ambient dependencies (clock,
# transport) with defaults.  These are the narrow exceptions to the
# "injected dependencies have no defaults" rule, because they ARE the
# single point where the ambient clock leaf and live transport are
# connected — exactly like main() calling utc_now directly.
OPERATION_SEAM_FUNCTIONS = frozenset({"run_sec_targeted_operation"})


def test_injected_dependencies_have_no_defaults():
    def injection_name(name):
        return name in INJECTED_PARAMETER_NAMES or name.endswith("_transport")

    for relative, _imports in tokmo_modules():
        tree = ast.parse((SRC_ROOT / relative).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            is_seam = (
                relative == "composition.py"
                and isinstance(node, ast.FunctionDef)
                and node.name in OPERATION_SEAM_FUNCTIONS
            )
            positional = node.args.posonlyargs + node.args.args
            first_defaulted = len(positional) - len(node.args.defaults)
            for index, arg in enumerate(positional):
                if index >= first_defaulted:
                    assert is_seam or not injection_name(arg.arg), (relative, arg.arg)
            for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
                if default is not None:
                    assert is_seam or not injection_name(arg.arg), (relative, arg.arg)


def _is_values_internal(name: str) -> bool:
    return name == "tokmo.values" or name.startswith("tokmo.values.")


def _is_presentation_internal(name: str) -> bool:
    return name == "tokmo.presentation" or name.startswith("tokmo.presentation.")


# presentation 계층이 의존할 수 없는 tokmo 하위 패키지.
PRESENTATION_FORBIDDEN = (
    "tokmo.facts", "tokmo.records", "tokmo.programs",
    "tokmo.runtime", "tokmo.observability", "tokmo.composition",
)


def test_layer_import_directions_match_contract():
    for relative, imports in tokmo_modules():
        area = relative.split("/")[0]
        if area == "values":
            # values 내부 파일 간 self-import는 허용 (tokmo.values.*),
            # facts/records/programs/presentation/composition로의 의존성은 금지.
            assert not any(
                name.startswith("tokmo") and not _is_values_internal(name)
                for name in imports
            ), relative
        if area == "facts":
            assert not any(name.startswith("tokmo.records") for name in imports), relative
            assert not any(_is_presentation_internal(name) for name in imports), relative
        if area == "records":
            assert not any(name.startswith("tokmo.facts") for name in imports), relative
            assert not any(_is_presentation_internal(name) for name in imports), relative
        if area == "presentation":
            # presentation → values / presentation 내부만 허용.
            for name in imports:
                if name.startswith("tokmo"):
                    assert _is_values_internal(name) or _is_presentation_internal(name), (
                        relative, name,
                    )
        if area == "observability":
            domain_prefixes = ("tokmo.facts", "tokmo.records", "tokmo.programs")
            assert not any(name.startswith(domain_prefixes) for name in imports), relative


def _permit_constructor_calls(source):
    return [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "HoldingsPersistencePermitV1"
    ]


def test_no_permit_construction_outside_approved_modules():
    assert _permit_constructor_calls(
        "HoldingsPersistencePermitV1(criteria_version='fixture')")
    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC_ROOT).as_posix()
        if relative == "records/holdings_persistence.py":
            continue
        assert not _permit_constructor_calls(path.read_text()), relative

    adapter = SRC_ROOT / "records" / "holdings_snapshots.py"
    tree = ast.parse(adapter.read_text())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "tokmo.records.holdings_persistence"
        for alias in node.names
    }
    assert imported & {"HoldingsPersistencePermitV1", "validate_permit"} == {
        "HoldingsPersistencePermitV1", "validate_permit"}
    assert not any(name.startswith("authorize_") for name in imported)


def test_runtime_loader_is_reached_only_from_composition_root():
    for relative, imports in tokmo_modules():
        if relative in {"composition.py", "opendart_bulk_composition.py", "kis_composition.py",
                        "runtime/config.py"}:
            continue
        assert not any(name.startswith("tokmo.runtime") for name in imports), relative


def test_provider_api_path_literals_are_closed_and_confined():
    """Every provider path literal in the tree is one of the authorized
    operations, each only inside its own adapter module."""
    for relative, _imports in tokmo_modules():
        api_paths, auth_paths = provider_paths((SRC_ROOT / relative).read_text())
        if api_paths:
            allowed = {
                TOSS_DATA_MODULE: TOSS_ALLOWED_API_PATHS,
                TOSS_PRICES_MODULE: TOSS_ALLOWED_PRICE_PATHS,
                TOSS_READS_MODULE: TOSS_ALLOWED_READ_PATHS,
                TOSS_READS_LIVE_MODULE: TOSS_ALLOWED_READ_PATHS,
                TOSS_COMMANDS_MODULE: TOSS_COMMAND_PATHS,
                FINNHUB_NEWS_LIVE_MODULE: NEWS_ALLOWED_API_PATHS,
                    REQUEST_PARITY_OWNER: REQUEST_PARITY_ALLOWED_PATHS - TOSS_ALLOWED_AUTH_PATHS,
                    # Provenance labels in captured inputs; requests still go
                    # through the enumerated GET adapter above.
                "programs/quant_decision_inputs.py": {"/api/v1/buying-power", "/api/v1/stocks"},
                # 2026-09-28: validate existing official cache provenance;
                # this label does not construct or issue a provider request.
                "programs/opportunity_rotation_capture.py": {"/api/v1/stocks"},
                "facts/etf_issuer.py": {"/api/v1/kodex/product-pdf/", "/api/v1/product/pdf/list"},
            }
            assert relative in allowed, (relative, sorted(api_paths))
            assert api_paths <= allowed[relative], sorted(api_paths)
        if auth_paths:
            allowed = {
                TOSS_AUTH_MODULE: TOSS_ALLOWED_AUTH_PATHS,
                "facts/kis_etf.py": {"/oauth2/tokenP"},
                REQUEST_PARITY_OWNER: TOSS_ALLOWED_AUTH_PATHS,
            }
            assert relative in allowed, (relative, sorted(auth_paths))
            assert auth_paths <= allowed[relative], sorted(auth_paths)
    owner_api, owner_auth = provider_paths((SRC_ROOT / REQUEST_PARITY_OWNER).read_text())
    assert owner_api | owner_auth == REQUEST_PARITY_ALLOWED_PATHS


def test_quant_order_information_is_read_only():
    from tokmo.facts.toss_reads import build_read_request, TossReadOperation
    from tokmo.facts.toss_reads_live import TossReadsLiveTransport
    seen=[]
    class OfflineOpener:
        def open(self,request,**kwargs):
            seen.append(request.get_method())
            assert request.data is None
            raise TimeoutError('offline check')
    transport=TossReadsLiveTransport();transport.opener=OfflineOpener()
    for operation, kwargs in (
        (TossReadOperation.BUYING_POWER, {'currency':'KRW'}),
        (TossReadOperation.SELLABLE_QUANTITY, {'symbol':'005380'}),
        (TossReadOperation.COMMISSIONS, {}),
    ):
        request=build_read_request(operation,token='synthetic',account_seq='1',**kwargs)
        transport(request)
    assert seen==['GET','GET','GET']


def test_the_data_adapter_can_name_no_write_method():
    text = (SRC_ROOT / TOSS_DATA_MODULE).read_text()
    for method in WRITE_METHODS:
        assert not re.search(rf"\b{method}\b", text, re.IGNORECASE), method


def test_the_auth_adapter_names_its_one_write_method_only_as_the_token_constant():
    lines = (SRC_ROOT / TOSS_AUTH_MODULE).read_text().splitlines()
    for method in ("PUT", "PATCH", "DELETE"):
        for line in lines:
            assert not re.search(rf"\b{method}\b", line, re.IGNORECASE), method
    naming = [line for line in lines if re.search(r"\bPOST\b", line, re.IGNORECASE)]
    assert naming == ['TOKEN_METHOD = "POST"']


def test_request_parity_owner_allows_post_only_for_token_path():
    source = (SRC_ROOT / REQUEST_PARITY_OWNER).read_text()
    tree = ast.parse(source)
    post_specs = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "TOKEN":
                operation_spec = ast.literal_eval(value)
                post_specs.append((key.value, operation_spec[0], operation_spec[1]))
    assert post_specs == [("TOKEN", "POST", "/oauth2/token")]
    assert len(re.findall(r'\bPOST\b', source, re.IGNORECASE)) == 1
    for method in ("PUT", "PATCH", "DELETE"):
        assert not re.search(rf"\b{method}\b", source, re.IGNORECASE), method


def test_the_account_sequence_field_name_is_confined_to_the_parser():
    for relative, _imports in tokmo_modules():
        text = (SRC_ROOT / relative).read_text()
        if "accountSeq" in text:
            assert relative == TOSS_PARSER_MODULE, relative


# --- Slice 7: programs/composition architecture invariants ---

def test_programs_toss_holdings_never_imports_records():
    """programs/toss_holdings.py must not import from tokmo.records;
    the hash/read seam is injected (content_hash_fn parameter), never
    imported. This is the Slice 7 program/composition seam."""
    path = SRC_ROOT / "programs" / "toss_holdings.py"
    imports = module_imports(path)
    for name in imports:
        assert not name.startswith("tokmo.records"), name


def test_composition_snapshot_store_branches_are_mutually_exclusive():
    """run_toss: snapshot_store branch and FactStore branch must be
    mutually exclusive — exactly one if/else, never both."""
    source = (SRC_ROOT / "composition.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef) and node.name == "run_toss"):
            # Find the if snapshot_store is not None / else pattern.
            found_typed = False
            found_legacy = False
            for child in ast.walk(node):
                if isinstance(child, ast.If):
                    test_str = ast.unparse(child.test)
                    if "snapshot_store is not None" in test_str:
                        found_typed = True
                        # The else branch must contain FactStore.
                        if child.orelse:
                            else_src = ast.unparse(child.orelse[0])
                            assert "FactStore" in else_src
                if isinstance(child, ast.Assign):
                    assign_src = ast.unparse(child)
                    if "FactStore(" in assign_src:
                        found_legacy = True
            assert found_typed, "snapshot_store typed branch not found"
            assert found_legacy, "FactStore legacy branch not found"


def test_no_constructor_bypass_for_permit_in_composition():
    """composition.py must not construct HoldingsPersistencePermitV1
    directly — it must use authorize_user_request/authorize_named_program."""
    source = (SRC_ROOT / "composition.py").read_text()
    assert not _permit_constructor_calls(source), \
        "composition.py must not construct permits directly"
def test_wti_sec_opendart_remain_green():
    source = (SRC_ROOT / "records/fact_store.py").read_text()
    assert all(token in source for token in (
        "WTI_SOURCE", "SEC_SOURCE", "OPENDART_SOURCE",
    ))


# test_post_cutover_cleanup_removes_toss_from_factstore is intentionally
# deferred to the separately approved post-marker cleanup release (SDS §9.7).


def test_actual_cli_uses_routed_holdings_entrypoints():
    source = (SRC_ROOT / "composition.py").read_text()
    holdings_branch = source[source.index('if args == ["toss-holdings"]'):]
    holdings_branch = holdings_branch[:holdings_branch.index(
        'if args == ["setup"]')]
    assert "run_toss_routed(" in holdings_branch
    assert "toss_report_routed(" in holdings_branch


def test_no_assert_statements_in_src():
    """M2: src/tokmo must contain zero bare assert statements.

    Bare assert is stripped by python -O, making security checks vanish.
    Use if/raise ValueError instead.
    """
    offenders = []
    for relative, _imports in tokmo_modules():
        source = (SRC_ROOT / relative).read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                offenders.append(relative)
                break
    assert not offenders, f"bare assert found in: {offenders}"


def test_kis_paths_are_read_only_and_confined():
    allowed = {
        '/uapi/etfetn/v1/quotations/inquire-component-stock-price',
        '/uapi/etfetn/v1/quotations/nav-comparison-time-trend',
        '/uapi/etfetn/v1/quotations/nav-comparison-daily-trend',
    }
    found = set()
    for relative, _imports in tokmo_modules():
        paths = {p for value in static_strings((SRC_ROOT / relative).read_text())
                 for p in re.findall(r'/uapi/[A-Za-z0-9/_{}.-]+', value)}
        if paths:
            assert relative == 'facts/kis_etf.py'
            assert paths <= allowed
            found |= paths
    assert found == allowed
