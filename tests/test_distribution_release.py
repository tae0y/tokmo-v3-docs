"""Distribution release: registry single-model stages, exporter rewrites, and the self-issued product gate install."""

import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tools"))
from datetime import UTC

from tokmo.values.model_registry import (
    ModelRegistryError,
    load_registry,
    single_model,
)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- registry: paper / quant single-model stages -------------------------------------------------
def test_registry_single_model_stages_default_and_override(tmp_path):
    assert single_model({}, "paper", "decide").name == "codex:gpt-6-astra"
    assert single_model({}, "quant", "rotation_entry").name == "codex:gpt-6-astra"
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "schema": "tokmo-models-v1",
                "paper": {"decide": "claude:claude-sonnet-5@high"},
                "quant": {"rotation_entry": "glm-5.3"},
            }
        )
    )
    reg = load_registry(path)
    spec = single_model(reg, "paper", "decide")
    assert (spec.provider, spec.model, spec.effort) == (
        "claude",
        "claude-sonnet-5",
        "high",
    )
    assert single_model(reg, "quant", "rotation_entry").name == "glm-5.3"


def test_registry_rejects_a_chain_on_a_single_model_stage(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "schema": "tokmo-models-v1",
                "paper": {"decide": ["codex:gpt-6-astra", "glm-5.3"]},
            }
        )
    )
    with pytest.raises(ModelRegistryError, match="exactly one model"):
        load_registry(path)
    with pytest.raises(ModelRegistryError, match="not a single-model stage"):
        single_model({}, "news", "screen")


def test_paper_product_model_uses_the_registry_model_for_gate_executor_and_receipt(
    tmp_path, monkeypatch
):
    import opportunity_rotation_model as paper

    (tmp_path / "var/data").mkdir(parents=True)
    (tmp_path / "var/data/models.json").write_text(
        json.dumps(
            {
                "schema": "tokmo-models-v1",
                "paper": {"decide": "claude:claude-sonnet-5@low"},
            }
        )
    )
    assert paper.configured_model(tmp_path) == ("claude", "claude-sonnet-5", "low")
    assert paper.configured_model(tmp_path / "absent") == (
        "codex",
        "gpt-6-astra",
        "medium",
    )  # built-in default
    seen = {}

    def admit(manifest, model, *, provider):
        seen["admit"] = (model, provider)
        return dict(scope={}, route={})

    def execute(prompt, *, provider, model, timeout, effort):
        seen["call"] = (provider, model, effort)
        return json.dumps(dict(cash_reason="x", assessments=[])), dict(
            _model=model, _provider=provider
        )

    from types import SimpleNamespace

    model = paper.ProductModel(
        manifest=tmp_path / "m",
        receipt_root=tmp_path / "r",
        synthetic=True,
        gate=SimpleNamespace(admit=admit, recheck=lambda *a, **k: None),
        executor=execute,
        model=paper.configured_model(tmp_path),
    )
    out = model.decide(
        {"id": "p1", "candidates": []}, call_id="c1", at="2026-10-02T01:05:00+00:00"
    )
    assert seen == {
        "admit": ("claude-sonnet-5", "claude"),
        "call": ("claude", "claude-sonnet-5", "low"),
    }
    assert out["author"] == "claude-sonnet-5"
    receipt = json.loads(next((tmp_path / "r").glob("*-RESULT.json")).read_text())
    assert (receipt["provider"], receipt["model"]) == ("claude", "claude-sonnet-5")


def test_rotation_quant_entry_passes_the_configured_model():
    from tokmo.programs.rotation_quant_entry import DEFAULT_MODEL, ExistingQuantAnalyzer

    assert DEFAULT_MODEL == "codex:gpt-6-astra"
    assert (
        ExistingQuantAnalyzer(
            python="py", entry="e", root="r", model="claude:claude-sonnet-5"
        ).model
        == "claude:claude-sonnet-5"
    )


def test_quant_adapter_has_no_owner_absolute_paths():
    import quant_decision_models as quant

    home = Path.home()
    assert quant.CORE_PATH == home / ".hermes/CRITICAL_OPERATING_CONTRACT.md"
    assert quant.RESOLVER_PATH == home / ".hermes/scripts/model_routing.py"
    source = (PROJECT / "tools/quant_decision_models.py").read_text()
    assert "/Users/" not in source and "parents[3]" not in source


# ---- exporter --------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def exporter():
    return load("export_dist_under_test", PROJECT / "ops/dist/export_dist.py")


def apply(exporter, text):
    for pattern, replacement in exporter.REWRITES:
        text = re.sub(pattern, replacement, text)
    return text


def residual(exporter, text):
    return [
        label for pattern, label in exporter.RESIDUAL_CHECKS if re.search(pattern, text)
    ]


def test_exporter_rewrites_canonical_paths_even_from_a_worktree(exporter):
    canonical = f"{exporter.CANONICAL_PROJECT}/var/data"
    assert apply(exporter, canonical) == "/home/tokmo/tokmo-v3/var/data"
    assert (
        apply(exporter, str(exporter.CANONICAL_REPO) + "/shared/x.py")
        == "/home/tokmo/tokmo-v3/shared/x.py"
    )
    assert (
        apply(exporter, str(Path.home() / ".hermes/scripts/x.py"))
        == "/home/tokmo/.hermes/scripts/x.py"
    )
    assert (
        residual(exporter, "PROJECT=/home/tokmo/tokmo-v3\n") == []
        and residual(exporter, "/home/tokmo/.hermes/.env") == []
    )
    assert residual(exporter, "/home/tokmo/Library/Logs") == [
        "nested or foreign dist path"
    ]
    nested = (
        "/home/tokmo/" + "tokmo/tokmo-v3/projects/tokmo-v3"
    )  # built, so the release grep does not see the literal
    assert residual(exporter, nested) == ["nested or foreign dist path"]


def test_exporter_scrubs_private_telegram_ids_in_every_known_shape(exporter):
    for text in (
        '"failure_deliver": "telegram:<owner-chat-id>",',
        "owns explicit user DM <owner-chat-id> transport",
        "TELEGRAM_ALLOWED_USERS=<owner-chat-id>\n",
        '"chat_id": "<owner-chat-id>",',
        "(개인 Telegram <owner-chat-id>)",
    ):
        out = apply(exporter, text)
        assert "12345678" not in out and "<owner-chat-id>" in out, out
        assert residual(exporter, out) == []
    assert residual(exporter, "telegram:<owner-chat-id>") == ["telegram dm id"]


def test_exporter_rewrites_the_nickname_before_the_with_particle(exporter):
    nick = "\uc6c5"  # escaped, so exporting this test file leaves it unchanged
    assert residual(exporter, nick + "과 헤르 합의") == ["owner nickname"]
    out = apply(exporter, nick + "과 헤르 합의")
    assert out == "사용자와 헤르 합의" and residual(exporter, out) == []


def test_exporter_ships_the_shared_gate_and_refuses_a_differing_installed_copy(
    exporter, tmp_path, monkeypatch
):
    out = tmp_path / "out"
    out.mkdir()
    fake_home = tmp_path / "home"
    (fake_home / ".hermes/scripts").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    added = exporter.add_shared(out)
    assert sorted(added) == [
        "ops/product-gate/model_routing.py",
        "ops/product-gate/product_model_gate.py",
    ]
    gate = out / "ops/product-gate/product_model_gate.py"
    assert (
        gate.read_bytes()
        == (
            exporter.CANONICAL_REPO / "shared/product_model_gate/product_model_gate.py"
        ).read_bytes()
    )
    (fake_home / ".hermes/scripts/product_model_gate.py").write_text("different\n")
    with pytest.raises(SystemExit, match="differs from the installed"):
        exporter.add_shared(tmp_path / "out2")


def test_exporter_release_record_digests_the_tree(exporter, tmp_path):
    out = tmp_path / "out"
    (out / "a").mkdir(parents=True)
    (out / "a/b.py").write_text("x = 1\n")
    (out / "README.md").write_text("hi\n")
    record = exporter.release_record(out, "main", "abc123")
    data = json.loads((out / "RELEASE.json").read_text())
    assert (
        data == record
        and record["files"] == 2
        and record["source_commit"] == "abc123"
        and len(record["tree_sha256"]) == 64
    )
    (out / "a/b.py").write_text("x = 2\n")
    assert (
        exporter.release_record(out, "main", "abc123")["tree_sha256"]
        != record["tree_sha256"]
    )


# ---- installer: a synthetic home + the real gate code, no network, no model --------------------
@pytest.fixture
def installed(tmp_path, monkeypatch):
    """Project copy with the reviewed files, a fake HOME, models.json naming one zai + one codex model."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "tokmo-v3"
    installer_src = PROJECT / "tools/install_product_gate.py"
    names = set()
    module = load("installer_names", installer_src)
    names.update(
        module.NEWS["files"],
        module.PAPER["files"],
        [
            "tools/install_product_gate.py",
            "tools/quant_decision_models.py",
            "ops/product-gate/OPERATING_CONTRACT.md",
            "src/tokmo/values/model_registry.py",
            "src/tokmo/__init__.py",
            "src/tokmo/values/__init__.py",
            "src/tokmo/programs/__init__.py",
            "src/tokmo/records/__init__.py",
            "src/tokmo/facts/__init__.py",
            "src/tokmo/programs/news_classifier_executor.py",
        ],
    )
    import quant_decision_models as quant

    names.update(quant.PRODUCT_FILES)
    for name in names:
        src = PROJECT / name
        if src.is_file():
            dest = root / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
    for rel, shared in (
        (
            "ops/product-gate/product_model_gate.py",
            "shared/product_model_gate/product_model_gate.py",
        ),
        ("ops/product-gate/model_routing.py", "shared/model_routing/model_routing.py"),
    ):
        (root / rel).write_bytes((PROJECT.parents[1] / shared).read_bytes())
    (root / "var/data").mkdir(parents=True)
    (root / "var/data/models.json").write_text(
        json.dumps(
            {
                "schema": "tokmo-models-v1",
                "news": {
                    "screen": "glm-5.3",
                    "first_pass": "glm-5.3",
                    "confirm": ["codex:gpt-5.6-sol@high", "glm-5.3"],
                },
                "paper": {"decide": "codex:gpt-6-astra"},
                "quant": {"rotation_entry": "codex:gpt-6-astra"},
            }
        )
    )
    (root / "RELEASE.json").write_text(
        json.dumps(
            {
                "source_commit": "deadbeef",
                "tree_sha256": "f" * 64,
                "exported_at": "2026-10-02T00:00:00+00:00",
            }
        )
    )
    os.chmod(root, 0o700)
    # the installer derives PROJECT from its own location: run the copy
    for module_name in [
        m
        for m in sys.modules
        if m in ("quant_decision_models", "tokmo.values.model_registry")
    ]:
        del sys.modules[module_name]
    installer = load("installer_under_test", root / "tools/install_product_gate.py")
    return installer, root, home


def run(installer, *argv, capsys):
    code = installer.main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def test_install_issue_check_admits_every_product_in_a_fresh_home(installed, capsys):
    installer, root, home = installed
    code, report = run(installer, "install", capsys=capsys)
    assert code == 0 and (home / ".hermes/scripts/product_model_gate.py").is_file()
    assert (
        stat.S_IMODE((home / ".hermes/MODEL_ROUTING_STATE.md").stat().st_mode) == 0o600
    )
    routing = (home / ".hermes/MODEL_ROUTING_STATE.md").read_text()
    assert (
        '"glm-5.3"' in routing
        and '"gpt-6-astra"' in routing
        and '"gpt-5.6-sol"' in routing
    )
    quota = json.loads(
        (home / ".hermes/cron/state/model-quota-latest.json").read_text()
    )
    assert quota["schema"] == "model-quota-latest-v1" and set(quota["providers"]) == {
        "zai",
        "codex",
    }
    assert all(
        w["remaining"] == 100.0
        for p in quota["providers"].values()
        for w in p["windows"]
    )

    code, report = run(installer, "issue", capsys=capsys)
    assert code == 0, report
    assert (
        report["news"]["models"] == ["glm-5.3"]
        and report["paper"]["models"] == ["gpt-6-astra"]
        and report["quant"]["files"] == 30
    )
    for path in (
        root / "ops/news-product-approval.json",
        root / "ops/opportunity-rotation-product-approval.json",
        root / "quant-on-demand-approval.json",
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        data = json.loads(path.read_text())
        assert (
            data["review"]["independent_review"] is False
            and data["review"]["release"]["source_commit"] == "deadbeef"
        )
        assert data["runtime_root"] == str(root)

    code, report = run(installer, "check", capsys=capsys)
    assert code == 0, report
    assert report["news"]["zai:glm-5.3"]["admission"] == "ADMITTED"
    assert report["paper"]["codex:gpt-6-astra"]["admission"] == "ADMITTED"
    assert report["quant"] == {"admission": "ADMITTED", "problems": []}

    # a second issue without --reissue is refused; with it, the previous manifest digest is chained
    assert run(installer, "issue", "--product", "paper", capsys=capsys)[0] == 2
    code, report = run(
        installer, "issue", "--product", "paper", "--reissue", capsys=capsys
    )
    assert code == 0 and "previous_manifest" in json.loads(
        (root / "ops/opportunity-rotation-product-approval.json").read_text()
    )


def test_check_reports_file_drift_after_a_code_change(installed, capsys):
    installer, root, home = installed
    assert (
        run(installer, "install", capsys=capsys)[0] == 0
        and run(installer, "issue", capsys=capsys)[0] == 0
    )
    (root / "tools/opportunity_rotation_model.py").write_text(
        (root / "tools/opportunity_rotation_model.py").read_text() + "\n# edited\n"
    )
    (root / "src/tokmo/programs/quant_dialogue.py").write_text(
        (root / "src/tokmo/programs/quant_dialogue.py").read_text() + "\n# edited\n"
    )
    code, report = run(installer, "check", capsys=capsys)
    assert code == 1
    assert report["paper"]["codex:gpt-6-astra"]["admission"] == "FILE_DRIFT"
    assert report["news"]["zai:glm-5.3"]["admission"] == "ADMITTED"
    assert report["quant"]["admission"] == "BLOCKED" and any(
        "quant_dialogue" in p for p in report["quant"]["problems"]
    )


def test_heartbeat_is_stale_after_45_minutes_for_the_real_gate(installed, capsys):
    installer, root, home = installed
    assert (
        run(installer, "install", capsys=capsys)[0] == 0
        and run(installer, "issue", "--product", "paper", capsys=capsys)[0] == 0
    )
    gate = load("gate_for_stale", home / ".hermes/scripts/product_model_gate.py")
    from datetime import datetime, timedelta

    entry = str(root / "tools/run_opportunity_rotation.py")
    identity = {"argv0": entry, "main_file": entry, "synthetic": True}
    manifest = root / "ops/opportunity-rotation-product-approval.json"
    with pytest.raises(gate.GateError, match="QUOTA_STALE"):
        gate.admit(
            manifest,
            "gpt-6-astra",
            provider="codex",
            identity=identity,
            now=datetime.now(UTC) + timedelta(minutes=46),
        )
    assert run(installer, "heartbeat", capsys=capsys)[0] == 0
    assert (
        gate.admit(manifest, "gpt-6-astra", provider="codex", identity=identity)[
            "route"
        ]["model"]
        == "gpt-6-astra"
    )


def test_followup_health_notice_uses_the_cron_fallback_without_hermes(
    tmp_path, monkeypatch
):
    """No ~/.hermes/scripts/send_system_alert.py on the machine -> the stand-in script is called with the body."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    stub = tmp_path / "alert.sh"
    stub.write_text(
        "#!/bin/sh\nprintf '%s' \"$1\" > " + str(tmp_path / "sent.txt") + "\n"
    )
    stub.chmod(0o700)
    monkeypatch.setenv("TOKMO_SYSTEM_ALERT_SCRIPT", str(stub))
    cron = load(
        "followup_cron_fallback", PROJECT / "tools/run_investment_followup_cron.py"
    )
    assert cron.send_health_notice("[후속 점검] health") == "fallback"
    assert (tmp_path / "sent.txt").read_text() == "[후속 점검] health"
    assert (
        cron.FALLBACK_ALERT == PROJECT / "ops/cron/fallback/send_system_alert.sh"
        and cron.FALLBACK_ALERT.is_file()
    )
