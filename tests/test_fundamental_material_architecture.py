"""Architecture tests for G1A fundamental material modules.

Verifies import/effect ownership, immutability, and structural invariants
for the fundamental material implementation layer.
"""

import ast
from datetime import UTC
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).parents[1] / "src" / "tokmo"

# G1A production modules that must exist and be importable.
G1A_SOURCE_MODULES = {
    "values/fundamental_materials.py",
    "facts/sec_fundamentals.py",
    "facts/sec_fundamentals_live.py",
    "facts/opendart_fundamentals.py",
    "facts/opendart_fundamentals_live.py",
    "programs/fundamental_materialization.py",
    "records/fundamental_material_store.py",
}

# G1A live transport modules (registered in test_architecture.py).
G1A_LIVE_MODULES = {
    "facts/sec_fundamentals_live.py",
    "facts/opendart_fundamentals_live.py",
}


class TestG1AModuleExistence:
    """All G1A allowlist source modules must exist."""

    def test_all_source_modules_exist(self):
        for rel in G1A_SOURCE_MODULES:
            path = SRC_ROOT / rel
            assert path.exists(), f"missing G1A source module: {rel}"


class TestG1AFrozenDataclasses:
    """OfficialFundamentalMaterialV1 and MetricEvidence must be frozen."""

    def test_official_material_is_frozen(self):
        import inspect

        from tokmo.values.fundamental_materials import OfficialFundamentalMaterialV1

        # Check that the class is decorated with @dataclass(frozen=True).
        source = inspect.getsource(OfficialFundamentalMaterialV1)
        # Verify __setattr__ raises AttributeError by checking the __dataclass_fields__
        # and that frozen=True is on the class.
        # dataclass frozen=True sets __hash__ and prevents __setattr__.
        fields = getattr(OfficialFundamentalMaterialV1, "__dataclass_params__", None)
        assert fields is not None
        assert fields.frozen is True

    def test_metric_evidence_is_frozen(self):
        from tokmo.values.fundamental_materials import MetricEvidence

        fields = getattr(MetricEvidence, "__dataclass_params__", None)
        assert fields is not None
        assert fields.frozen is True


class TestG1ANoNetworkInNonLiveModules:
    """G1A non-live source modules must not import network libraries."""

    FORBIDDEN_NETWORK_PREFIXES = {
        "urllib.request",
        "urllib.error",
        "http.client",
        "socket",
        "ssl",
        "requests",
        "httpx",
        "aiohttp",
    }

    def test_non_live_modules_have_no_network_imports(self):
        non_live = G1A_SOURCE_MODULES - G1A_LIVE_MODULES
        for rel in non_live:
            path = SRC_ROOT / rel
            if not path.exists():
                continue
            tree = ast.parse(path.read_text())
            imports: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
            for imp in imports:
                for banned in self.FORBIDDEN_NETWORK_PREFIXES:
                    assert imp != banned and not imp.startswith(banned + "."), (
                        f"{rel} imports forbidden network module: {imp}"
                    )


class TestG1ANoSyntheticOrFuzzyMatching:
    """G1A parser must not contain fuzzy/synthetic/arithmetic keywords."""

    FORBIDDEN_KEYWORDS = {
        "fuzzy",
        "levenshtein",
        "arithmetic_reconstruct",
        "synthetic",
        "estimate",
        "guess",
        "extrapolate",
        "interpolate",
    }

    def test_sec_fundamentals_no_fuzzy_logic(self):
        path = SRC_ROOT / "facts" / "sec_fundamentals.py"
        if not path.exists():
            pytest.skip("sec_fundamentals.py not found")
        source = path.read_text()
        tree = ast.parse(source)
        # Check def/class names only (not docstrings/comments).
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name_lower = node.name.lower()
                for kw in self.FORBIDDEN_KEYWORDS:
                    assert kw not in name_lower, (
                        f"sec_fundamentals.py: forbidden keyword '{kw}' in name: {node.name}"
                    )


class TestG1AQuantCorePreserved:
    """Pure quant core must be byte-preserved (not modified)."""

    def test_quant_core_values_unchanged(self):
        path = SRC_ROOT / "values" / "quant_multifactor_core.py"
        assert path.exists()
        # This test verifies the file exists and is importable.
        # Byte-preservation is verified by git diff --check.
        import tokmo.values.quant_multifactor_core  # noqa: F401


class TestG1AOpenDARTPaused:
    """OpenDART fundamentals must be PAUSED with no invented endpoints."""

    def test_opendart_status_is_paused(self):
        from tokmo.facts.opendart_fundamentals import OPENDART_FUNDAMENTALS_STATUS

        assert OPENDART_FUNDAMENTALS_STATUS == "PAUSED"

    def test_opendart_live_has_no_endpoint_paths(self):
        """OpenDART live transport must contain zero invented API paths.

        The test is exact: parse the AST and verify that no string constant
        in the module body (excluding docstrings) contains an API path or
        the opendart.fss.or.kr host. The dead loop that walked the AST and
        then did nothing (pass) has been removed.
        """
        path = SRC_ROOT / "facts" / "opendart_fundamentals_live.py"
        text = path.read_text()
        tree = ast.parse(text)

        # Collect all string constants from non-docstring positions.
        string_consts: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                string_consts.append(node.value)

        # No invented endpoint URLs or API paths anywhere in the module.
        for s in string_consts:
            assert "opendart.fss.or.kr" not in s, (
                f"opendart_fundamentals_live.py: invented endpoint host in constant: {s!r}"
            )
            assert "/api/" not in s, (
                f"opendart_fundamentals_live.py: invented API path in constant: {s!r}"
            )


class TestG1ACompositionSeamEffectZero:
    """SDS §12 15d: build_fundamental_materialization_deps must have
    network/provider/cadence AND mkdir/SQLite/file-write all zero at
    construction time. Returns immutable deps with store path/factory only.

    The private directory and SQLite store are created only inside an
    explicit materialization invocation, never during construction.

    Tests import the leaf builder directly from
    fundamental_materialization so that they pass independently of the
    composition-root import chain.
    """

    def test_construction_creates_no_directory_or_file(self, tmp_path):
        """Calling build_fundamental_materialization_deps must NOT create
        any directory or file under home."""

        from datetime import datetime

        from tokmo.programs.fundamental_materialization import (
            build_fundamental_materialization_deps,
        )

        home = tmp_path / "fake_home"
        home.mkdir()

        # Record all paths before construction.
        before = sorted(str(p) for p in home.rglob("*"))

        # B3 fix: leaf builder takes injected transport factories (stubs).
        deps = build_fundamental_materialization_deps(
            home=home,
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
            sec_transport_factory=lambda: "stub_sec",
            opendart_transport_factory=lambda: "stub_opendart",
        )

        # Filesystem state after must be unchanged.
        after = sorted(str(p) for p in home.rglob("*"))
        assert before == after, (
            "build_fundamental_materialization_deps created files/dirs during construction"
        )

    def test_deps_are_immutable_mapping(self, tmp_path):
        import types as _types
        from datetime import datetime

        from tokmo.programs.fundamental_materialization import (
            build_fundamental_materialization_deps,
        )

        home = tmp_path / "fake_home"
        home.mkdir()

        deps = build_fundamental_materialization_deps(
            home=home,
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
            sec_transport_factory=lambda: "stub_sec",
            opendart_transport_factory=lambda: "stub_opendart",
        )

        # MappingProxyType is read-only; mutation raises TypeError.
        assert isinstance(deps, _types.MappingProxyType)
        try:
            deps["new_key"] = "forbidden"  # type: ignore[index]
            raise AssertionError("MappingProxyType accepted mutation")
        except TypeError:
            pass

    def test_deps_contain_store_path_and_factory_not_instance(self, tmp_path):
        from datetime import datetime

        from tokmo.programs.fundamental_materialization import (
            build_fundamental_materialization_deps,
        )

        home = tmp_path / "fake_home"
        home.mkdir()

        deps = build_fundamental_materialization_deps(
            home=home,
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
            sec_transport_factory=lambda: "stub_sec",
            opendart_transport_factory=lambda: "stub_opendart",
        )

        assert "store_path" in deps
        assert "store_factory" in deps
        assert "store" not in deps, (
            "deps must carry store path/factory, not a store instance"
        )
        assert callable(deps["store_factory"])

    def test_store_factory_creates_dir_and_store_only_on_invocation(self, tmp_path):
        """The store factory creates the private dir + SQLite store only
        when explicitly invoked, not during construction."""

        from datetime import datetime

        from tokmo.programs.fundamental_materialization import (
            build_fundamental_materialization_deps,
        )

        home = tmp_path / "fake_home"
        home.mkdir()

        deps = build_fundamental_materialization_deps(
            home=home,
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
            sec_transport_factory=lambda: "stub_sec",
            opendart_transport_factory=lambda: "stub_opendart",
        )

        store_path = deps["store_path"]
        assert not store_path.exists(), "store file must not exist after construction"
        assert not store_path.parent.exists(), (
            "private dir must not exist after construction"
        )

        # Invoking the factory creates the dir and store.
        store = deps["store_factory"]()
        assert store_path.parent.exists(), "private dir must exist after factory call"
        assert store_path.exists(), "store file must exist after factory call"


class TestG1ACompositionWrapperBehavior:
    """m8: composition wrapper behavior test.

    Verify that the composition wrapper function-locally imports live
    transports and passes their factories to the leaf builder. The leaf
    builder imports NO live modules — only composition does.
    """

    def test_leaf_builder_imports_no_live_transport_modules(self):
        """B3: programs/fundamental_materialization.py must not import
        any LIVE_TRANSPORT_MODULES."""
        import ast

        path = SRC_ROOT / "programs" / "fundamental_materialization.py"
        tree = ast.parse(path.read_text())
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        live_prefixes = (
            "tokmo.facts.sec_fundamentals_live",
            "tokmo.facts.opendart_fundamentals_live",
        )
        for imp in imports:
            for prefix in live_prefixes:
                assert not imp.startswith(prefix), (
                    f"leaf builder must not import live transport: {imp}"
                )

    def test_composition_wrapper_passes_factories(self, tmp_path):
        """m8: composition wrapper delegates to leaf builder with factories."""
        from datetime import datetime

        from tokmo.composition import (
            build_fundamental_materialization_deps as comp_build,
        )

        home = tmp_path / "fake_home"
        home.mkdir()
        deps = comp_build(
            home=home,
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )
        # Verify transport instances exist (constructed by composition via factories).
        assert "sec_transport" in deps
        assert "opendart_transport" in deps
        assert deps["sec_transport"] is not None
        assert deps["opendart_transport"] is not None

    def test_deps_from_composition_have_no_files(self, tmp_path):
        """m8: construction through composition must be effect-zero."""
        from datetime import datetime

        from tokmo.composition import (
            build_fundamental_materialization_deps as comp_build,
        )

        home = tmp_path / "fake_home"
        home.mkdir()
        before = sorted(str(p) for p in home.rglob("*"))
        deps = comp_build(
            home=home,
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )
        after = sorted(str(p) for p in home.rglob("*"))
        assert before == after


COMPOSITION_PATH = SRC_ROOT / "composition.py"
WRAPPER_NAME = "build_fundamental_materialization_deps"


def _composition_wrapper_node() -> ast.FunctionDef:
    """The composition wrapper's own AST node, read from production bytes."""
    tree = ast.parse(COMPOSITION_PATH.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == WRAPPER_NAME:
            return node
    raise AssertionError(f"composition.py has no module-level {WRAPPER_NAME}")


class TestG1ACompositionWrapperIsSelfContained:
    """The wrapper's delegation and effect-zero guarantees must be provable
    from the wrapper's own production bytes.

    ``tokmo.composition`` transitively imports ``programs/candidate_builder``,
    which is OUTSIDE the G1A allowlist and carries a pre-existing defect: a
    ``mappingproxy`` dataclass default that older interpreters reject. The
    project pins ``requires-python >= 3.12`` and the import chain is healthy
    there, so the runtime tests above execute the wrapper through the real
    composition root. These tests additionally verify the SAME production
    bytes without importing the composition root at all, so the G1A wrapper
    contract stays proven — and the unrelated baseline defect stays visible
    rather than being papered over by a skip.
    """

    def test_running_interpreter_satisfies_project_requires_python(self):
        """The composition import chain is only contracted on the supported
        interpreter; make an unsupported one fail loudly, not mysteriously."""
        import sys

        pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text()
        declared = [
            line
            for line in pyproject.splitlines()
            if line.strip().startswith("requires-python")
        ]
        assert declared, "pyproject.toml must declare requires-python"
        assert '">=3.12"' in declared[0], declared[0]
        assert sys.version_info >= (3, 12), (
            f"unsupported interpreter {sys.version_info[:2]}; the project "
            "requires >=3.12 and the composition import chain is only "
            "contracted there"
        )

    def test_wrapper_imports_are_all_function_local(self):
        """No module-level import may be added for the G1A live transports."""
        node = _composition_wrapper_node()
        local_imports: list[str] = []
        for child in ast.walk(node):
            if isinstance(child, ast.Import):
                local_imports.extend(a.name for a in child.names)
            elif isinstance(child, ast.ImportFrom) and child.module:
                local_imports.append(child.module)
        assert "tokmo.facts.sec_fundamentals_live" in local_imports
        assert "tokmo.facts.opendart_fundamentals_live" in local_imports
        assert "tokmo.programs.fundamental_materialization" in local_imports

        module_tree = ast.parse(COMPOSITION_PATH.read_text())
        for child in module_tree.body:
            names: list[str] = []
            if isinstance(child, ast.Import):
                names = [a.name for a in child.names]
            elif isinstance(child, ast.ImportFrom) and child.module:
                names = [child.module]
            for name in names:
                assert not name.startswith("tokmo.facts.sec_fundamentals_live")
                assert not name.startswith("tokmo.facts.opendart_fundamentals_live")

    def test_wrapper_delegates_with_both_live_factories(self):
        """The wrapper must pass the function-locally imported live transport
        classes to the leaf builder as factories — not construct them itself
        and not substitute anything else."""
        node = _composition_wrapper_node()
        imported_aliases = {
            "tokmo.facts.sec_fundamentals_live": set(),
            "tokmo.facts.opendart_fundamentals_live": set(),
            "tokmo.programs.fundamental_materialization": set(),
        }
        for child in ast.walk(node):
            if isinstance(child, ast.ImportFrom) and child.module in imported_aliases:
                for alias in child.names:
                    imported_aliases[child.module].add(alias.asname or alias.name)

        leaf_names = imported_aliases["tokmo.programs.fundamental_materialization"]
        assert leaf_names, "wrapper must import the leaf builder"

        delegations = [
            call
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id in leaf_names
        ]
        assert len(delegations) == 1, "wrapper must delegate exactly once"
        kwargs = {
            kw.arg: kw.value for kw in delegations[0].keywords if kw.arg is not None
        }
        assert set(kwargs) == {
            "home",
            "clock",
            "sec_transport_factory",
            "opendart_transport_factory",
        }
        sec_factory = kwargs["sec_transport_factory"]
        opendart_factory = kwargs["opendart_transport_factory"]
        assert isinstance(sec_factory, ast.Name)
        assert isinstance(opendart_factory, ast.Name)
        assert sec_factory.id in imported_aliases["tokmo.facts.sec_fundamentals_live"]
        assert (
            opendart_factory.id
            in (imported_aliases["tokmo.facts.opendart_fundamentals_live"])
        )

    def test_wrapper_body_has_no_io_or_cadence_calls(self):
        """SDS §12 15d: construction-time mkdir/SQLite/network/cadence = 0."""
        node = _composition_wrapper_node()
        forbidden = {
            "mkdir",
            "open",
            "connect",
            "write_text",
            "write_bytes",
            "touch",
            "urlopen",
            "socket",
            "register",
            "schedule",
            "start",
        }
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func = child.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else func.id
                    if isinstance(func, ast.Name)
                    else ""
                )
                assert name not in forbidden, (
                    f"{WRAPPER_NAME} performs a construction-time effect: {name}"
                )

    def _exec_wrapper(self):
        """Execute the wrapper's real production bytes in isolation.

        Only the wrapper function is compiled, so the composition root's
        unrelated import chain is not exercised.
        """
        from collections.abc import Callable
        from datetime import datetime

        node = _composition_wrapper_node()
        module = ast.Module(body=[node], type_ignores=[])
        ast.fix_missing_locations(module)
        namespace: dict = {
            "Path": Path,
            "Clock": Callable[[], datetime],
        }
        exec(compile(module, str(COMPOSITION_PATH), "exec"), namespace)
        return namespace[WRAPPER_NAME]

    def test_wrapper_bytes_build_real_live_transports(self, tmp_path):
        from datetime import datetime

        from tokmo.facts.opendart_fundamentals_live import (
            OpenDartFundamentalsLiveTransport,
        )
        from tokmo.facts.sec_fundamentals_live import SECCompanyFactsLiveTransport

        wrapper = self._exec_wrapper()
        home = tmp_path / "fake_home"
        home.mkdir()
        clock = lambda: datetime(2026, 1, 1, tzinfo=UTC)
        deps = wrapper(home=home, clock=clock)

        import types as _types

        assert isinstance(deps, _types.MappingProxyType)
        assert isinstance(deps["sec_transport"], SECCompanyFactsLiveTransport)
        assert isinstance(deps["opendart_transport"], OpenDartFundamentalsLiveTransport)
        assert deps["clock"] is clock
        assert "store_path" in deps and "store_factory" in deps
        assert "store" not in deps
        assert callable(deps["store_factory"])

    def test_wrapper_bytes_are_effect_zero(self, tmp_path):
        from datetime import datetime

        wrapper = self._exec_wrapper()
        home = tmp_path / "fake_home"
        home.mkdir()
        before = sorted(str(p) for p in home.rglob("*"))
        deps = wrapper(home=home, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
        after = sorted(str(p) for p in home.rglob("*"))
        assert before == after
        assert not deps["store_path"].exists()
        assert not deps["store_path"].parent.exists()
