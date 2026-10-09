"""Approved product calls: core/code/private scope pins + current routing/quota.

Canonical source: shared/product_model_gate/product_model_gate.py; installed
beside model_routing.py in ~/.hermes/scripts. See README.md for the v2 one-time
migration. The routing Markdown owns IDs/selectors/windows, never product
permission. Approval binds calling contracts or explicit pins, entrypoints,
reviewed product files, the core, this gate and the shared resolver code.

Each admission reads one routing/quota snapshot and returns its actual hash.
Callers must persist intent, recheck immediately before transport, then persist
the outcome. Synthetic identity is diagnostic only and cannot authorize a call.
paths= is a test/diagnostic seam, never an operational policy override.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

SCHEMA = "product-call-approval-v2"
QUOTA_SCHEMA = "model-quota-latest-v1"
HERMES = Path.home() / ".hermes"
PATHS = {  # 운영 고정값. 테스트·진단만 paths= 로 대체.
    "core": HERMES / "CRITICAL_OPERATING_CONTRACT.md",
    "routing": HERMES / "MODEL_ROUTING_STATE.md",
    "quota": HERMES / "cron" / "state" / "model-quota-latest.json",
    "parser": HERMES / "scripts" / "model_routing.py",
}
QUOTA_MAX_AGE_SECONDS = 45 * 60
QUOTA_MAX_FUTURE_SECONDS = 5 * 60
EXHAUSTED_REMAINING = 0.5  # known_reset(): 이 이하 창의 reset_at을 대기 근거로 쓴다(톡모 quota_snapshot과 동일)
RESET_MAX_AHEAD = timedelta(days=7)
REQUIRED_WINDOWS = {
    "zai": ("5시간", "주간"),
    "codex": ("Session",),
    "claude": ("Current session", "Current week"),
}


class GateError(PermissionError):
    """code: MANIFEST_* | RUNTIME_ROOT | ENTRYPOINT | FILE_* | AUTHORITY_DRIFT | GATE_DRIFT
    | ROUTE_NOT_ADMITTED | ROUTING_POLICY_UNPARSEABLE | MODEL_NOT_IN_SCOPE | PROVIDER_MISMATCH | QUOTA_*"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def utc_now() -> datetime:
    return datetime.now(UTC)


def _hex64(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _paths(paths: dict | None) -> dict:
    merged = dict(PATHS)
    merged.update({k: Path(v) for k, v in (paths or {}).items() if k in PATHS})
    return merged


def read_private(path: Path, *, private: bool = False) -> tuple[bytes, dict]:
    """정규 경로(심링크 거부)·정규 파일·소유자·group/other 쓰기 금지(private면 정확히 0600). 한 번 읽은 바이트와 facts."""
    if path.resolve(strict=True) != path:
        raise GateError("FILE_PATH", f"symlink or noncanonical: {path}")
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
            or (private and stat.S_IMODE(info.st_mode) != 0o600)
        ):
            raise GateError("FILE_MODE", str(path))
        data = stream.read()
    return data, {
        "path": str(path),
        "mtime_ns": info.st_mtime_ns,
        "sha256": sha256(data).hexdigest(),
    }


# ---- manifest -------------------------------------------------------------
def load_manifest(manifest_path, *, paths: dict | None = None) -> tuple[dict, bytes]:
    path = Path(manifest_path)
    try:
        raw, _ = read_private(path, private=True)
        manifest = json.loads(raw)
    except (OSError, ValueError, GateError) as error:
        raise GateError(
            "MANIFEST_UNREADABLE", f"{path}: {error.__class__.__name__}"
        ) from error
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise GateError(
            "MANIFEST_SCHEMA",
            str(manifest.get("schema") if isinstance(manifest, dict) else None),
        )
    fixed = _paths(paths)
    try:
        review = manifest["review"]
        models = manifest["scope"]["models"]
        contracts = manifest["scope"].get("contracts", {})
        if (
            review["verdict"] != "PASS"
            or not _hex64(review["candidate_sha256"])
            or not isinstance(manifest["policy_approval"], str)
            or not manifest["policy_approval"].strip()
            or not isinstance(manifest["product"], str)
            or not manifest["product"].strip()
            or not isinstance(manifest["runtime_root"], str)
            or not isinstance(manifest["entrypoints"], list)
            or not manifest["entrypoints"]
            or not all(isinstance(e, str) for e in manifest["entrypoints"])
            or not isinstance(manifest["files"], dict)
            or not manifest["files"]
            or not all(
                isinstance(k, str) and _hex64(v) for k, v in manifest["files"].items()
            )
            or not isinstance(manifest["authority"], dict)
            or set(manifest["authority"]) != {str(fixed["core"]), str(fixed["routing"])}
            or not _hex64(manifest["authority"][str(fixed["core"])])
            or manifest["authority"][str(fixed["routing"])] != "current"
            or not _hex64(manifest["resolver_sha256"])
            or not isinstance(models, dict)
            or not isinstance(contracts, dict)
            or not (models or contracts)
            or not all(
                p in REQUIRED_WINDOWS
                and c
                == {"zai": "zai-chat", "claude": "claude-cli", "codex": "codex-cli"}[p]
                for p, c in contracts.items()
            )
            or not all(
                isinstance(k, str)
                and isinstance(v, dict)
                and v.get("model") == k
                and v.get("provider") in REQUIRED_WINDOWS
                for k, v in models.items()
            )
            or not _hex64(manifest["gate_sha256"])
        ):
            raise GateError("MANIFEST_FACTS")
    except (KeyError, TypeError, AttributeError) as error:
        raise GateError("MANIFEST_FACTS", error.__class__.__name__) from error
    return manifest, raw


# ---- 1~5: 제품 범위 -------------------------------------------------------
def _read_authorities(paths: dict) -> dict:
    """두 정본을 각각 한 번 읽는다: {"core": (bytes, facts), "routing": (bytes, facts)}. 못 읽으면 AUTHORITY_DRIFT."""
    reads = {}
    for key in ("core", "routing"):
        path = paths[key]
        try:
            reads[key] = read_private(path)
        except (OSError, GateError) as error:
            raise GateError("AUTHORITY_DRIFT", f"{path}: unreadable") from error
    return reads


def _check_scope(
    manifest: dict,
    raw: bytes,
    manifest_path,
    *,
    identity: dict | None,
    gate_file,
    paths: dict,
    authorities: dict | None = None,
) -> dict:
    root = Path(manifest["runtime_root"])
    try:
        info = root.stat()
        if (
            root.resolve(strict=True) != root
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
        ):
            raise GateError("RUNTIME_ROOT", str(root))
    except OSError as error:
        raise GateError("RUNTIME_ROOT", str(root)) from error
    if identity is None:
        main = sys.modules.get("__main__")
        main_file = getattr(main, "__file__", "") or ""
        identity = {
            "argv0": str(Path(sys.argv[0]).absolute())
            if sys.argv and sys.argv[0]
            else "",
            "main_file": str(Path(main_file).absolute()) if main_file else "",
            "synthetic": False,
        }
    entrypoints = {str(Path(e).absolute()) for e in manifest["entrypoints"]}
    if identity.get("argv0") not in entrypoints or identity.get(
        "main_file"
    ) != identity.get("argv0"):
        raise GateError(
            "ENTRYPOINT", f"{identity.get('argv0')!r} not in approved entrypoints"
        )
    for name, digest in manifest["files"].items():
        path = root / name
        if path.resolve(strict=False) != path:
            raise GateError("FILE_PATH", name)
        for directory in path.parents:
            if directory == root:
                break
            try:
                dinfo = directory.stat()
            except OSError as error:
                raise GateError("FILE_DRIFT", name) from error
            if dinfo.st_uid != os.getuid() or dinfo.st_mode & 0o022:
                raise GateError("RUNTIME_ROOT", f"directory mode: {directory}")
        try:
            _, facts = read_private(path)
        except (OSError, GateError) as error:
            raise GateError("FILE_DRIFT", f"{name}: unreadable") from error
        if facts["sha256"] != digest:
            raise GateError("FILE_DRIFT", name)
    authority = {}
    for key, (_, facts) in (authorities or _read_authorities(paths)).items():
        path = paths[key]
        if key == "core" and facts["sha256"] != manifest["authority"][str(path)]:
            raise GateError("AUTHORITY_DRIFT", str(path))
        authority[str(path)] = facts
    gate_path = Path(gate_file or __file__).absolute()
    try:
        gate_digest = sha256(gate_path.read_bytes()).hexdigest()
    except OSError as error:
        raise GateError("GATE_DRIFT", str(gate_path)) from error
    if gate_digest != manifest["gate_sha256"]:
        raise GateError("GATE_DRIFT", str(gate_path))
    return {
        "product": manifest["product"],
        "manifest_path": str(Path(manifest_path)),
        "manifest_sha256": sha256(raw).hexdigest(),
        "review_candidate_sha256": manifest["review"]["candidate_sha256"],
        "runtime_root": str(root),
        "entrypoint": identity.get("argv0"),
        "identity_synthetic": bool(identity.get("synthetic")),
        "files_sha256": sha256(
            json.dumps(
                manifest["files"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
        "authority": authority,
        "gate_path": str(gate_path),
        "gate_sha256": gate_digest,
        "checked_at": utc_now().isoformat(),
    }


def require_product_scope(
    manifest_path,
    *,
    identity: dict | None = None,
    gate_file=None,
    paths: dict | None = None,
) -> dict:
    """사용자 승인 manifest에 묶인 제품 범위 검사(1~5). identity=None이면 현재 프로세스(sys.argv[0], __main__)를 본다."""
    fixed = _paths(paths)
    manifest, raw = load_manifest(manifest_path, paths=fixed)
    return _check_scope(
        manifest,
        raw,
        manifest_path,
        identity=identity,
        gate_file=gate_file,
        paths=fixed,
    )


# ---- 6~7: route + quota -------------------------------------------------
def _resolver(paths, expected=None):
    try:
        raw, facts = read_private(paths["parser"])
        if expected is not None and facts["sha256"] != expected:
            raise GateError("GATE_DRIFT", "resolver")
        namespace = {
            "__name__": "product_route_resolver",
            "__file__": str(paths["parser"]),
        }
        exec(compile(raw, str(paths["parser"]), "exec"), namespace)
        return namespace, facts["sha256"]
    except GateError:
        raise
    except (OSError, ValueError, SyntaxError) as error:
        raise GateError("ROUTING_POLICY_UNPARSEABLE", "resolver") from error


def route_authority(
    manifest: dict,
    model: str,
    *,
    provider: str | None = None,
    now: datetime | None = None,
    paths: dict | None = None,
    routing_read: tuple[bytes, dict] | None = None,
) -> dict:
    """모델이 manifest scope 안이고(호출자가 말한 provider와 일치), routing 정본의 exact route이며, 쿼터 정본이 신선하고
    필수 창에 잔여가 있어야 한다. routing·quota는 한 번 읽은 바이트로 해시와 판정을 같이 한다.
    routing_read를 주면(admit이 scope 대조에 쓴 바로 그 바이트) 다시 읽지 않는다 — 대조와 판정이 같은 버전(헤르 F02)."""
    fixed = _paths(paths)
    scope = manifest.get("scope", {})
    profile = scope.get("models", {}).get(model)
    contracts = scope.get("contracts", {})
    if profile is not None and provider is not None and provider != profile["provider"]:
        raise GateError("PROVIDER_MISMATCH", model)
    try:
        routing_bytes, routing = routing_read or read_private(fixed["routing"])
        resolver, parser_digest = _resolver(fixed, manifest["resolver_sha256"])
        route = resolver["resolve"](
            routing_bytes.decode("utf-8"), model, provider=provider, use="product"
        )
    except (OSError, ValueError, KeyError) as error:
        raise GateError(
            getattr(error, "code", "ROUTING_POLICY_UNPARSEABLE"), str(error)
        ) from error
    provider_name = route["provider"]
    profile = scope.get("models", {}).get(route["model"])
    if profile is not None:
        if provider_name != profile["provider"]:
            raise GateError("PROVIDER_MISMATCH", model)
    elif contracts.get(provider_name) != route["contract"]:
        raise GateError("MODEL_NOT_IN_SCOPE", model)
    model = route["model"]
    try:
        quota_bytes, quota = read_private(fixed["quota"])
        data = json.loads(quota_bytes)
    except (OSError, GateError, ValueError) as error:
        raise GateError("QUOTA_MISSING", error.__class__.__name__) from error
    if not isinstance(data, dict) or data.get("schema") != QUOTA_SCHEMA:
        raise GateError("QUOTA_SCHEMA_INVALID")
    try:
        observed = datetime.fromisoformat(
            str(data["observed_at"]).replace("Z", "+00:00")
        )
    except (KeyError, ValueError, TypeError) as error:
        raise GateError("QUOTA_SCHEMA_INVALID", "observed_at") from error
    now = now or utc_now()
    if (
        observed.tzinfo is None
        or not -QUOTA_MAX_FUTURE_SECONDS
        <= (now - observed).total_seconds()
        <= QUOTA_MAX_AGE_SECONDS
    ):
        raise GateError("QUOTA_STALE", observed.isoformat())
    providers = data.get("providers")
    provider_data = (
        providers.get(provider_name) if isinstance(providers, dict) else None
    )
    if (
        not isinstance(provider_data, dict)
        or provider_data.get("ok") is not True
        or not isinstance(provider_data.get("windows"), list)
    ):
        raise GateError("QUOTA_PROVIDER_INVALID", provider_name)
    for label in route["windows"]:
        windows = [
            w
            for w in provider_data["windows"]
            if isinstance(w, dict) and w.get("label") == label
        ]
        if len(windows) != 1:
            raise GateError("QUOTA_WINDOW_INVALID", label)
        remaining = windows[0].get("remaining")
        if (
            not isinstance(remaining, (int, float))
            or isinstance(remaining, bool)
            or not math.isfinite(remaining)
            or not 0 < remaining <= 100
        ):
            raise GateError("QUOTA_UNAVAILABLE", f"{label} {remaining}")
    return {
        "model": model,
        "provider": provider_name,
        "route_admission": "ADMITTED",
        "routing_state": routing,
        "route_parser_sha256": parser_digest,
        "quota": {**quota, "observed_at": observed.isoformat()},
    }


def known_reset(
    provider: str, now: datetime | None = None, *, paths: dict | None = None
) -> datetime | None:
    """소진(≤0.5%p)된 창 중 가장 이른 미래 reset_at(7일 이내). 모르면 None. 한도 실패 뒤 대기 시각 계산용."""
    now = now or utc_now()
    try:
        data = json.loads(_paths(paths)["quota"].read_text(encoding="utf-8"))
        windows = data["providers"][provider]["windows"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    candidates = []
    for window in windows if isinstance(windows, list) else []:
        try:
            remaining = float(window["remaining"])
            when = (
                datetime.fromisoformat(str(window["reset_at"]).replace("Z", "+00:00"))
                if window.get("reset_at")
                else None
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if (
            when is not None
            and when.tzinfo is not None
            and remaining <= EXHAUSTED_REMAINING
            and now < when <= now + RESET_MAX_AHEAD
        ):
            candidates.append(when)
    return min(candidates) if candidates else None


def admit(
    manifest_path,
    model: str,
    *,
    provider: str | None = None,
    identity: dict | None = None,
    now: datetime | None = None,
    gate_file=None,
    paths: dict | None = None,
) -> dict:
    """제품 범위(1~5) + route/quota(6~7). manifest는 한 번만 읽는다. 통과하면 receipt용 facts, 아니면 GateError."""
    fixed = _paths(paths)
    manifest, raw = load_manifest(manifest_path, paths=fixed)
    authorities = _read_authorities(
        fixed
    )  # 정본은 한 번만 읽어 대조와 판정에 같이 쓴다
    scope = _check_scope(
        manifest,
        raw,
        manifest_path,
        identity=identity,
        gate_file=gate_file,
        paths=fixed,
        authorities=authorities,
    )
    route = route_authority(
        manifest,
        model,
        provider=provider,
        now=now,
        paths=fixed,
        routing_read=authorities["routing"],
    )
    return {"scope": scope, "route": route}


def recheck(manifest_path, facts, **kwargs):
    """Bind this prepared call through the receipt write to the transport boundary."""
    fresh = admit(
        manifest_path,
        facts["route"]["model"],
        provider=facts["route"]["provider"],
        **kwargs,
    )
    for key in ("scope", "route"):
        before = {k: v for k, v in facts[key].items() if k != "checked_at"}
        after = {k: v for k, v in fresh[key].items() if k != "checked_at"}
        if before != after:
            raise GateError("PREFLIGHT_STALE", key)


# ---- 발급(헤르메스 승인 단계에서만) --------------------------------------
def issue(
    *,
    product: str,
    runtime_root,
    entrypoints,
    files,
    models: dict,
    policy_approval: str,
    review: dict,
    out_path,
    gate_file=None,
    paths: dict | None = None,
    previous_manifest=None,
    contracts: dict | None = None,
) -> dict:
    """manifest 작성: 파일·정본·게이트 sha256을 지금 계산해 0600으로 저장한다. review는 호출자가 채운다(PASS 판정 포함)."""
    fixed = _paths(paths)
    root = Path(runtime_root)
    manifest = {
        "schema": SCHEMA,
        "product": product,
        "runtime_root": str(root),
        "entrypoints": [str(Path(e).absolute()) for e in entrypoints],
        "files": {
            name: sha256((root / name).read_bytes()).hexdigest()
            for name in sorted(files)
        },
        "scope": {"models": models, "contracts": contracts or {}},
        "authority": {
            str(fixed["core"]): sha256(fixed["core"].read_bytes()).hexdigest(),
            str(fixed["routing"]): "current",
        },
        "resolver_sha256": sha256(read_private(fixed["parser"])[0]).hexdigest(),
        "gate_sha256": sha256(Path(gate_file or __file__).read_bytes()).hexdigest(),
        "policy_approval": policy_approval,
        "review": review,
        "issued_at": utc_now().isoformat(),
    }
    if previous_manifest:
        manifest["previous_manifest"] = previous_manifest
    out = Path(out_path)
    tmp = out.with_name(out.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, out)
    return manifest


def _cli_paths(args) -> dict | None:
    override = {
        k: getattr(args, f"{k}_path") for k in PATHS if getattr(args, f"{k}_path", None)
    }
    return override or None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="product-call-approval-v2 gate: check | issue"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser(
        "check",
        help="manifest·정본·쿼터 판정(호출 0). --as-entrypoint는 합성 identity(진단 전용)",
    )
    p.add_argument("--manifest", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--provider", default=None)
    p.add_argument("--as-entrypoint", default=None)
    for key in PATHS:
        p.add_argument(f"--{key}-path", default=None, help="진단·테스트 전용 경로 대체")
    p = sub.add_parser(
        "issue",
        help="manifest 발급(승인 단계 전용). review JSON 파일에 verdict PASS·candidate_sha256 필수",
    )
    p.add_argument("--product", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--entrypoint", action="append", required=True)
    p.add_argument("--file", action="append", required=True)
    p.add_argument(
        "--model", action="append", default=[], help="명시 exact pin: provider:model"
    )
    p.add_argument(
        "--contract",
        action="append",
        default=[],
        help="현재 product route 추종: provider:contract",
    )
    p.add_argument("--policy-approval", required=True)
    p.add_argument("--review-json", required=True)
    p.add_argument("--out", required=True)
    for key in PATHS:
        p.add_argument(f"--{key}-path", default=None, help="진단·테스트 전용 경로 대체")
    args = parser.parse_args(argv)
    if args.command == "check":
        identity = None
        if args.as_entrypoint:
            entry = str(Path(args.as_entrypoint).absolute())
            identity = {"argv0": entry, "main_file": entry, "synthetic": True}
        try:
            facts = admit(
                args.manifest,
                args.model,
                provider=args.provider,
                identity=identity,
                paths=_cli_paths(args),
            )
        except GateError as error:
            print(
                json.dumps(
                    {"admission": error.code, "detail": error.detail},
                    ensure_ascii=False,
                )
            )
            return 1
        print(
            json.dumps(
                {"admission": "ADMITTED", **facts},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0
    models = {}
    for spec in args.model:
        provider, _, model = spec.partition(":")
        if not model or provider not in REQUIRED_WINDOWS:
            parser.error(f"bad --model {spec!r}")
        models[model] = {"provider": provider, "model": model}
    contracts = {}
    for value in args.contract:
        provider, _, contract = value.partition(":")
        if contract != {
            "zai": "zai-chat",
            "claude": "claude-cli",
            "codex": "codex-cli",
        }.get(provider):
            parser.error(f"bad --contract {value!r}")
        contracts[provider] = contract
    if not models and not contracts:
        parser.error("--model or --contract is required")
    review = json.loads(Path(args.review_json).read_text(encoding="utf-8"))
    issue(
        product=args.product,
        runtime_root=args.root,
        entrypoints=args.entrypoint,
        files=args.file,
        models=models,
        contracts=contracts,
        policy_approval=args.policy_approval,
        review=review,
        out_path=args.out,
        paths=_cli_paths(args),
    )
    print(json.dumps({"issued": args.out}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
