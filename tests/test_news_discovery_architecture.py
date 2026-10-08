import ast
from pathlib import Path

ROOT = Path(__file__).parents[1] / "src" / "tokmo"
PROJECT = Path(__file__).parents[1]
NEWS_PROGRAMS = tuple(ROOT / "programs" / name for name in (
    "news_topic_discovery.py", "news_investigation.py", "news_event_admission.py",
    "news_alert_delivery.py", "news_feedback.py", "news_production.py",
    "news_source_health.py", "news_universe.py",
))


def test_news_programs_do_not_import_sqlite_or_live_transport():
    forbidden = {"sqlite3", "urllib", "requests", "httpx", "tokmo.facts.telegram_dialogue_live"}
    for path in NEWS_PROGRAMS:
        tree = ast.parse(path.read_text())
        imports = {node.names[0].name for node in ast.walk(tree) if isinstance(node, ast.Import)}
        imports |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert not (imports & forbidden), (path, imports & forbidden)


def test_news_discovery_has_no_write_or_model_surface():
    discovery = (ROOT / "programs" / "news_topic_discovery.py").read_text()
    assert "sqlite3" not in discovery
    assert "classifier_call" not in discovery
    assert "TELEGRAM" not in discovery


def test_single_getupdates_owner_is_preserved():
    sources = [path for path in ROOT.glob("**/*.py") if "getUpdates" in path.read_text()]
    assert sorted(path.name for path in sources) == ["execution_dialogue.py", "telegram_dialogue_live.py"]


def _scan_files():
    for folder in ("src", "tools", "scripts"):
        root = PROJECT / folder
        if root.exists():
            yield from sorted(root.glob("**/*.py"))


def _references(tree, name):
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            found.append(node)
        elif isinstance(node, ast.Attribute) and node.attr == name:
            found.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if any(alias.name == name for alias in node.names):
                found.append(node)
    return found


def test_r_lck_2_producer_runs_only_inside_pulse_lock_scope():
    """SDS §6.8-4: the producer executable exists only as a module-private
    helper whose single callsite sits lexically inside run_pulse_once's
    held-flock try block (R-LCK-2 a–f)."""
    pulse_path = ROOT / "programs" / "integrated_pulse.py"
    pulse_tree = ast.parse(pulse_path.read_text())
    # (a) PulseDepsV1 has the inert bundle field and no runner callable.
    deps_class = next(node for node in ast.walk(pulse_tree)
                      if isinstance(node, ast.ClassDef)
                      and node.name == "PulseDepsV1")
    fields = [node.target.id for node in deps_class.body
              if isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name)]
    assert "news_producer_runner" not in fields
    assert "news_producer_deps" in fields
    # (b) module-private helper defined at module level; its only reference
    # outside the definition, project-wide, is one Call in run_pulse_once.
    helper_defs = [node for node in pulse_tree.body
                   if isinstance(node, ast.FunctionDef)
                   and node.name == "_run_news_producer_stage"]
    assert len(helper_defs) == 1
    run_pulse = next(node for node in ast.walk(pulse_tree)
                     if isinstance(node, ast.FunctionDef)
                     and node.name == "run_pulse_once")
    helper_calls = [node for node in ast.walk(run_pulse)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_run_news_producer_stage"]
    assert len(helper_calls) == 1
    for path in _scan_files():
        tree = ast.parse(path.read_text())
        refs = _references(tree, "_run_news_producer_stage")
        if path == pulse_path:
            # definition Name + the single Call inside run_pulse_once only
            call_refs = [ref for ref in refs if isinstance(ref, ast.Name)
                         and ref.lineno != helper_defs[0].lineno]
            assert len(call_refs) == 1
            assert call_refs[0].lineno == helper_calls[0].func.lineno
        else:
            assert refs == [], path
    # (c) the call sits after the flock acquisition assignment and inside
    # the try whose finalbody closes that descriptor.
    acquire = next(node for node in ast.walk(run_pulse)
                   if isinstance(node, ast.Assign)
                   and isinstance(node.value, ast.Call)
                   and isinstance(node.value.func, ast.Name)
                   and node.value.func.id == "_acquire_pulse_lock")
    call_line = helper_calls[0].lineno
    assert call_line > acquire.lineno
    lock_tries = []
    for node in ast.walk(run_pulse):
        if not isinstance(node, ast.Try):
            continue
        closes = [inner for final in node.finalbody
                  for inner in ast.walk(final)
                  if isinstance(inner, ast.Call)
                  and isinstance(inner.func, ast.Attribute)
                  and inner.func.attr == "close"
                  and isinstance(inner.func.value, ast.Name)
                  and inner.func.value.id == "os"
                  and inner.args and isinstance(inner.args[0], ast.Name)
                  and inner.args[0].id == "descriptor"]
        if closes:
            lock_tries.append(node)
    assert len(lock_tries) == 1
    lock_try = lock_tries[0]
    body_start = lock_try.body[0].lineno
    body_end = max(inner.end_lineno for inner in lock_try.body)
    assert body_start <= call_line <= body_end
    # (d) run_news_production references stay in the two owning modules.
    allowed_run = {ROOT / "programs" / "news_production.py", pulse_path}
    for path in _scan_files():
        refs = _references(ast.parse(path.read_text()), "run_news_production")
        if path not in allowed_run:
            assert refs == [], path
    assert _references(pulse_tree, "run_news_production")
    # (e) NewsProducerDepsV1 references: definition, DI factory, pulse only.
    allowed_deps = {ROOT / "programs" / "news_production.py",
                    ROOT / "programs" / "pulse_production_deps.py", pulse_path}
    for path in _scan_files():
        refs = _references(ast.parse(path.read_text()), "NewsProducerDepsV1")
        if path not in allowed_deps:
            assert refs == [], path
    # (f) the inert bundle is never constructed with a production-pipeline
    # executable as a field value.
    factory_tree = ast.parse(
        (ROOT / "programs" / "pulse_production_deps.py").read_text())
    for node in ast.walk(factory_tree):
        if isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Name)
                 and node.func.id == "NewsProducerDepsV1")
                or (isinstance(node.func, ast.Attribute)
                    and node.func.attr == "NewsProducerDepsV1")):
            for keyword in node.keywords:
                for inner in ast.walk(keyword.value):
                    if isinstance(inner, (ast.Name, ast.Attribute)):
                        name = (inner.id if isinstance(inner, ast.Name)
                                else inner.attr)
                        assert name not in (
                            "run_news_production",
                            "_run_news_producer_stage"), keyword.arg
    production_tree = ast.parse(
        (ROOT / "programs" / "news_production.py").read_text())
    bundle_class = next(node for node in ast.walk(production_tree)
                        if isinstance(node, ast.ClassDef)
                        and node.name == "NewsProducerDepsV1")
    bundle_fields = [node.target.id for node in bundle_class.body
                     if isinstance(node, ast.AnnAssign)
                     and isinstance(node.target, ast.Name)]
    assert "news_producer_runner" not in bundle_fields
    assert not any("runner" in field for field in bundle_fields)
