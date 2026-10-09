#!/usr/bin/env python3
"""Install the shared product model gate for THIS checkout and self-issue its product approvals.

The three AI call paths (NEWS GLM decisions, PAPER opportunity rotation, Quant analysis) refuse to call a
model unless a private approval record binds this installation: runtime root, entrypoints, sha256 of the
reviewed call-path files, the gate and resolver code, and the operating contract. The owner's records are
issued by Hermes after review; a distribution install has no Hermes, so this tool does the same mechanical
steps for the person installing it. The result is a *self-issued* approval (recorded as such), not a review.

    install_product_gate.py install   [--force-routing]   gate + resolver -> ~/.hermes/scripts, contract, routing, heartbeat
    install_product_gate.py heartbeat                     refresh ~/.hermes/cron/state/model-quota-latest.json (cron every 30 min)
    install_product_gate.py issue     [--product P] [--reissue]   write the approval files for news / paper / quant
    install_product_gate.py check     [--product P]       judge each approval with the real gate (no model call)

The gate's own paths are fixed in its code (``~/.hermes/...``); this tool never patches the gate.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import shutil
import stat
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
sys.path.insert(0, str(PROJECT / "tools"))

HERMES = Path.home() / ".hermes"  # fixed by product_model_gate.PATHS
GATE_PATHS = {
    "core": HERMES / "CRITICAL_OPERATING_CONTRACT.md",
    "routing": HERMES / "MODEL_ROUTING_STATE.md",
    "quota": HERMES / "cron" / "state" / "model-quota-latest.json",
    "parser": HERMES / "scripts" / "model_routing.py",
    "gate": HERMES / "scripts" / "product_model_gate.py",
}
# Where the shared files come from: the distribution ships them in ops/product-gate/; the private repo keeps them in shared/.
SOURCES = {
    "product_model_gate.py": (
        PROJECT / "ops/product-gate/product_model_gate.py",
        PROJECT.parents[1] / "shared/product_model_gate/product_model_gate.py",
    ),
    "model_routing.py": (
        PROJECT / "ops/product-gate/model_routing.py",
        PROJECT.parents[1] / "shared/model_routing/model_routing.py",
    ),
}
CONTRACT_TEMPLATE = PROJECT / "ops/product-gate/OPERATING_CONTRACT.md"
BASE_WINDOWS = {
    "zai": ("5시간", "주간"),
    "codex": ("Session",),
    "claude": ("Current session", "Current week"),
}
CONTRACTS = {"zai": "zai-chat", "codex": "codex-cli", "claude": "claude-cli"}

# Reviewed call-path files per product. These mirror the owner's live approvals; a product's adapter checks
# exactly these names, so adding a file here without the adapter knowing it changes nothing but the digest.
NEWS = dict(
    product="tokmo-news",
    manifest=PROJECT / "ops/news-product-approval.json",
    entrypoints=("ops/hermes-cron/tokmo_news_classifier_worker.py",),
    files=(
        "ops/hermes-cron/tokmo_news_classifier_worker.py",
        "src/tokmo/facts/news_http.py",
        "src/tokmo/programs/model_chain.py",
        "src/tokmo/programs/news_decision_engine.py",
        "src/tokmo/programs/news_model_wait.py",
        "src/tokmo/records/quant_decision_reports.py",
        "tools/news_decision_models.py",
        "tools/news_product_gate.py",
        "tools/run_news_decision_worker.py",
    ),
)
PAPER = dict(
    product="tokmo-opportunity-rotation-paper",
    manifest=PROJECT / "ops/opportunity-rotation-product-approval.json",
    entrypoints=("tools/run_opportunity_rotation.py",),
    files=(
        "src/tokmo/facts/opportunity_rotation.py",
        "src/tokmo/programs/opportunity_rotation.py",
        "src/tokmo/programs/opportunity_rotation_capture.py",
        "src/tokmo/programs/opportunity_rotation_schedule.py",
        "tools/opportunity_rotation_model.py",
        "tools/run_opportunity_rotation.py",
        "tools/subscription_json_model.py",
    ),
)
QUANT_MANIFEST = PROJECT / "quant-on-demand-approval.json"
QUANT_SCHEMA = "quant-on-demand-install-v2"


class InstallError(SystemExit):
    def __init__(self, message):
        super().__init__("ERROR: " + message)


def utc_now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_of(name: str) -> Path:
    for candidate in SOURCES[name]:
        if candidate.is_file():
            return candidate
    raise InstallError(f"{name} not found in ops/product-gate/ or ../../shared/")


def private_dir(path: Path, mode=0o700):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


def write_private(path: Path, text: str):
    private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ---- models: which ids this installation uses -------------------------------------------------
def registry():
    from tokmo.values.model_registry import load_registry, registry_path

    return load_registry(registry_path(PROJECT))


def configured_specs():
    """Every model spec this installation may call, by product: {'news': [...], 'paper': [spec], 'quant': [spec]}."""
    from tokmo.values.model_registry import news_chains, single_model

    reg = registry()
    config_path = PROJECT / "var/data/news_decision.json"
    try:
        config = (
            json.loads(config_path.read_text(encoding="utf-8"))
            if config_path.exists()
            else {}
        )
    except ValueError:
        config = {}
    news = [
        spec for chain in news_chains(reg, config).values() if chain for spec in chain
    ]
    return {
        "news": news,
        "paper": [single_model(reg, "paper", "decide")],
        "quant": [single_model(reg, "quant", "rotation_entry")],
    }


def routing_markdown(specs_by_product) -> str:
    """A MODEL_ROUTING_STATE.md the shared resolver accepts, listing exactly this installation's models."""
    routes, selectors = {}, {}
    for specs in specs_by_product.values():
        for spec in specs:
            routes[spec.model] = {
                "provider": spec.provider,
                "contract": CONTRACTS[spec.provider],
                "uses": ["agent", "product", "news"],
                "windows": list(BASE_WINDOWS[spec.provider]),
            }
            selectors.setdefault(
                {"zai": "glm", "codex": "codex", "claude": "claude"}[spec.provider],
                spec.model,
            )
    if not routes:
        raise InstallError(
            "no models configured (var/data/models.json or news_decision.json)"
        )
    block = json.dumps(
        {"routes": routes, "selectors": selectors}, ensure_ascii=False, indent=2
    )
    return (
        "# MODEL_ROUTING_STATE (tokmo-v3 distribution)\n\n"
        "이 파일은 제품 게이트가 읽는 모델 목록입니다. 쓸 모델을 바꾸려면 var/data/models.json을 고치고\n"
        "`tools/install_product_gate.py install --force-routing` 뒤 `issue --reissue`를 실행하세요.\n\n"
        "## 현재 상태\n\n```model-routing\n" + block + "\n```\n"
    )


def quota_snapshot(routing_text: str, resolver) -> dict:
    """Heartbeat only: this installation has no quota probe, so every window reads 100 (unmetered)."""
    policy = resolver.parse_policy(routing_text)
    providers = {}
    for row in policy["routes"].values():
        windows = providers.setdefault(
            row["provider"],
            {
                "ok": True,
                "plan": "unmetered (distribution heartbeat)",
                "name": row["provider"],
                "windows": [],
            },
        )["windows"]
        for label in row["windows"]:
            if not any(w["label"] == label for w in windows):
                windows.append({"label": label, "remaining": 100.0, "reset_at": None})
    return {
        "schema": "model-quota-latest-v1",
        "observed_at": utc_now(),
        "providers": providers,
        "_note": "written by tools/install_product_gate.py heartbeat; not a measurement",
    }


# ---- commands --------------------------------------------------------------------------------
def cmd_install(args) -> dict:
    canonical_home()
    report = {"installed": [], "kept": []}
    private_dir(HERMES)
    private_dir(HERMES / "scripts")
    private_dir(HERMES / "cron" / "state")
    for name in ("product_model_gate.py", "model_routing.py"):
        src, dest = source_of(name), HERMES / "scripts" / name
        if dest.is_file() and dest.read_bytes() == src.read_bytes():
            report["kept"].append(str(dest))
            continue
        if dest.is_file() and not args.force_gate:
            raise InstallError(
                f"{dest} exists with different content; this is an owner installation (Hermes) or an older "
                f"release. Re-run with --force-gate only if you own this machine's gate."
            )
        shutil.copyfile(src, dest)
        os.chmod(dest, 0o600)
        report["installed"].append(str(dest))
    core = GATE_PATHS["core"]
    if core.is_file():
        report["kept"].append(str(core))
    else:
        write_private(core, CONTRACT_TEMPLATE.read_text(encoding="utf-8"))
        report["installed"].append(str(core))
    routing = GATE_PATHS["routing"]
    if routing.is_file() and not args.force_routing:
        report["kept"].append(str(routing))
    else:
        write_private(routing, routing_markdown(configured_specs()))
        report["installed"].append(str(routing))
    report["heartbeat"] = cmd_heartbeat(args)
    report["next"] = [
        "crontab: */30 * * * * "
        + str(PROJECT / ".venv/bin/python")
        + " "
        + str(Path(__file__).resolve())
        + " heartbeat",
        f"{sys.executable} {Path(__file__).name} issue",
        f"{sys.executable} {Path(__file__).name} check",
    ]
    return report


def cmd_heartbeat(args) -> dict:
    resolver = load_module("dist_model_routing", GATE_PATHS["parser"])
    snapshot = quota_snapshot(
        GATE_PATHS["routing"].read_text(encoding="utf-8"), resolver
    )
    write_private(
        GATE_PATHS["quota"], json.dumps(snapshot, ensure_ascii=False, indent=1) + "\n"
    )
    return {
        "quota": str(GATE_PATHS["quota"]),
        "observed_at": snapshot["observed_at"],
        "providers": sorted(snapshot["providers"]),
    }


def canonical_home():
    """The gate rejects any non-canonical path, and every path it reads starts at $HOME."""
    home = Path.home()
    if home.resolve(strict=True) != home:
        raise InstallError(
            f"HOME {home} is a symlinked or non-canonical path; the gate reads only canonical paths. "
            f"Use HOME={home.resolve()} (on macOS /tmp is a symlink to /private/tmp)"
        )


def preflight(root_mode_exact: int | None = None):
    canonical_home()
    if PROJECT.resolve(strict=True) != PROJECT:
        raise InstallError(
            f"project path contains a symlink: {PROJECT} (the gate rejects it)"
        )
    info = PROJECT.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise InstallError(
            f"project root must be owned by you and not group/other writable: chmod 755 {PROJECT}"
        )
    if root_mode_exact is not None and stat.S_IMODE(info.st_mode) != root_mode_exact:
        raise InstallError(
            f"Quant requires the project root mode {oct(root_mode_exact)}: chmod {oct(root_mode_exact)[2:]} {PROJECT}"
        )
    for key in ("core", "routing", "parser", "gate"):
        if not GATE_PATHS[key].is_file():
            raise InstallError(f"{GATE_PATHS[key]} missing: run `install` first")


def review_facts(files: dict, release: dict | None) -> dict:
    candidate = sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "verdict": "PASS",
        "candidate_sha256": candidate,
        "reviewer": "self-issued by tools/install_product_gate.py for this installation; not an independent review",
        "independent_review": False,
        "release": release or None,
        "issued_by": getpass.getuser(),
        "issued_at": utc_now(),
    }


def release_info():
    path = PROJECT / "RELEASE.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: data.get(k) for k in ("source_commit", "tree_sha256", "exported_at")}
    except (OSError, ValueError):
        return None


def policy_text(product: str) -> str:
    return (
        f"self-issued {utc_now()} by {getpass.getuser()} for {PROJECT} ({product}); distribution install, "
        "models from var/data/models.json; no independent review"
    )


def issue_gate_product(spec, specs, gate, reissue: bool, release) -> dict:
    manifest = spec["manifest"]
    if manifest.exists() and not reissue:
        raise InstallError(f"{manifest} exists; use --reissue to replace it")
    models = {s.model: {"provider": s.provider, "model": s.model} for s in specs}
    files = {name: digest(PROJECT / name) for name in spec["files"]}
    previous = digest(manifest) if manifest.exists() else None
    gate.issue(
        product=spec["product"],
        runtime_root=PROJECT,
        entrypoints=[PROJECT / e for e in spec["entrypoints"]],
        files=list(spec["files"]),
        models=models,
        policy_approval=policy_text(spec["product"]),
        review=review_facts(files, release),
        out_path=manifest,
        gate_file=GATE_PATHS["gate"],
        previous_manifest=previous,
    )
    return {"manifest": str(manifest), "models": sorted(models), "files": len(files)}


def issue_quant(reissue: bool, release) -> dict:
    import quant_decision_models as quant  # PRODUCT_FILES / PRODUCT_SCOPE are the adapter's own definitions

    if QUANT_MANIFEST.exists() and not reissue:
        raise InstallError(f"{QUANT_MANIFEST} exists; use --reissue to replace it")
    files = {name: digest(PROJECT / name) for name in quant.PRODUCT_FILES}
    manifest = {
        "schema": QUANT_SCHEMA,
        "runtime_root": str(PROJECT),
        "entrypoint": str(PROJECT / "tools/run_quant_decisions.py"),
        "review": review_facts(files, release),
        "policy_approval": policy_text("tokmo-quant"),
        "scope": quant.PRODUCT_SCOPE,
        "files": files,
        "authority": {
            str(quant.CORE_PATH): digest(quant.CORE_PATH),
            str(quant.ROUTING_PATH): "current",
        },
        "resolver_sha256": digest(quant.RESOLVER_PATH),
        "issued_at": utc_now(),
    }
    write_private(
        QUANT_MANIFEST,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return {
        "manifest": str(QUANT_MANIFEST),
        "files": len(files),
        "providers": sorted(quant.PROVIDER_PROFILES),
    }


def cmd_issue(args) -> dict:
    preflight(root_mode_exact=0o700 if args.product in ("all", "quant") else None)
    gate = load_module("dist_product_model_gate", GATE_PATHS["gate"])
    specs, release, report = configured_specs(), release_info(), {}
    if args.product in ("all", "news"):
        zai = [
            s for s in specs["news"] if s.provider == "zai"
        ]  # the NEWS gate admits Z.AI (GLM) calls only
        report["news"] = (
            issue_gate_product(NEWS, zai, gate, args.reissue, release)
            if zai
            else {"skipped": "no zai model in news stages"}
        )
    if args.product in ("all", "paper"):
        report["paper"] = issue_gate_product(
            PAPER, specs["paper"], gate, args.reissue, release
        )
    if args.product in ("all", "quant"):
        report["quant"] = issue_quant(args.reissue, release)
    return report


def check_gate_product(spec, specs, gate) -> dict:
    results = {}
    for s in specs:
        for entry in spec["entrypoints"]:
            path = str(PROJECT / entry)
            identity = {"argv0": path, "main_file": path, "synthetic": True}
            try:
                facts = gate.admit(
                    spec["manifest"],
                    s.model,
                    provider=s.provider,
                    identity=identity,
                    gate_file=GATE_PATHS["gate"],
                )
                results[f"{s.provider}:{s.model}"] = {
                    "admission": "ADMITTED",
                    "route": facts["route"]["model"],
                }
            except Exception as error:  # GateError or anything the gate raises
                results[f"{s.provider}:{s.model}"] = {
                    "admission": getattr(error, "code", type(error).__name__),
                    "detail": getattr(error, "detail", str(error)),
                }
    return results


def check_quant() -> dict:
    """The Quant adapter checks its own process identity, so judge the static facts the same way it does."""
    import quant_decision_models as quant

    problems = []
    try:
        if stat.S_IMODE(PROJECT.stat().st_mode) != 0o700:
            problems.append("runtime root mode is not 0700")
        approval = json.loads(QUANT_MANIFEST.read_text(encoding="utf-8"))
        if stat.S_IMODE(QUANT_MANIFEST.stat().st_mode) != 0o600:
            problems.append("manifest mode is not 0600")
        if approval.get("schema") != QUANT_SCHEMA or approval.get(
            "runtime_root"
        ) != str(PROJECT):
            problems.append("schema or runtime_root")
        if set(approval.get("files", {})) != set(quant.PRODUCT_FILES):
            problems.append("file set differs from PRODUCT_FILES")
        drift = [
            n
            for n, h in approval.get("files", {}).items()
            if not (PROJECT / n).is_file() or digest(PROJECT / n) != h
        ]
        if drift:
            problems.append("reviewed file drift: " + ", ".join(drift))
        if approval.get("authority", {}).get(str(quant.CORE_PATH)) != digest(
            quant.CORE_PATH
        ):
            problems.append("contract (core) drift")
        if approval.get("resolver_sha256") != digest(quant.RESOLVER_PATH):
            problems.append("resolver drift")
        if json.dumps(approval.get("scope"), sort_keys=True) != json.dumps(
            quant.PRODUCT_SCOPE, sort_keys=True
        ):
            problems.append("scope differs from PRODUCT_SCOPE")
    except (OSError, ValueError) as error:
        problems.append(f"manifest unreadable: {type(error).__name__}")
    return {
        "admission": "ADMITTED" if not problems else "BLOCKED",
        "problems": problems,
    }


def cmd_check(args) -> dict:
    preflight()
    gate = load_module("dist_product_model_gate", GATE_PATHS["gate"])
    specs, report = configured_specs(), {}
    if args.product in ("all", "news"):
        zai = [s for s in specs["news"] if s.provider == "zai"]
        report["news"] = (
            check_gate_product(NEWS, zai, gate)
            if zai
            else {"skipped": "no zai model in news stages"}
        )
    if args.product in ("all", "paper"):
        report["paper"] = check_gate_product(PAPER, specs["paper"], gate)
    if args.product in ("all", "quant"):
        report["quant"] = check_quant()
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("install")
    p.add_argument(
        "--force-routing",
        action="store_true",
        help="regenerate MODEL_ROUTING_STATE.md from models.json",
    )
    p.add_argument(
        "--force-gate",
        action="store_true",
        help="overwrite a differing installed gate/resolver (owner machines: do not)",
    )
    sub.add_parser("heartbeat")
    for name in ("issue", "check"):
        p = sub.add_parser(name)
        p.add_argument(
            "--product", choices=("all", "news", "paper", "quant"), default="all"
        )
        if name == "issue":
            p.add_argument(
                "--reissue",
                action="store_true",
                help="replace an existing approval (after a code update)",
            )
    args = parser.parse_args(argv)
    try:
        report = {
            "install": cmd_install,
            "heartbeat": cmd_heartbeat,
            "issue": cmd_issue,
            "check": cmd_check,
        }[args.command](args)
    except InstallError as error:
        print(error, file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    if args.command == "check":
        blocked = [
            k
            for k, v in report.items()
            if isinstance(v, dict)
            and (
                v.get("admission") not in (None, "ADMITTED")
                or any(
                    isinstance(r, dict) and r.get("admission") not in (None, "ADMITTED")
                    for r in v.values()
                )
            )
        ]
        return 1 if blocked else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
