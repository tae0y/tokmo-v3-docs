"""One-shot success-silent, no-delivery G1-C prospective shadow policy."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BUNDLE_MEMBERS_V1 = tuple(
    sorted(
        (
            "src/tokmo/programs/ai_infra_capex_allocation.py",
            "src/tokmo/programs/ai_infra_capex_shadow_runtime.py",
            "src/tokmo/records/ai_infra_capex_shadow_receipts.py",
            "src/tokmo/records/external_news_query.py",
            "src/tokmo/values/ai_infra_capex.py",
            "src/tokmo/values/signal_program_lifecycle.py",
            "tools/run_ai_infra_capex_shadow.py",
        )
    )
)
CONFIG_KEYS_V1 = {
    "schema",
    "enabled",
    "policy",
    "project_root",
    "runner_path",
    "venv_python",
    "canonical_news_path",
    "canonical_path_identity_digest",
    "expected_commit",
    "spec_digest",
    "classifier_digest",
    "runtime_bundle_digest",
    "cadence_version",
    "minute_kst",
    "activation_utc",
    "review_due_utc",
    "expiry_utc",
    "review_days",
    "expiry_days",
    "query_policy",
    "http_start_cap",
    "model_start_cap",
    "retry_cap",
    "query_cap",
    "delivery_cap",
    "news_write_cap",
}


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def runtime_bundle_digest_v1(paths):
    if tuple(sorted(paths)) != BUNDLE_MEMBERS_V1:
        raise ValueError("bundle members")
    members = []
    for name in BUNDLE_MEMBERS_V1:
        path = Path(paths[name])
        members.append(
            {"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
    return hashlib.sha256(
        _bytes({"members": members, "schema": "tokmo.g1c.runtime-bundle.v1"})
    ).hexdigest()


def canonical_path_identity_digest_v1(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("unsafe source")
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise ValueError("unsafe source")
    return hashlib.sha256(
        _bytes(
            {
                "inode": info.st_ino,
                "mode": 384,
                "nlink": 1,
                "path": str(resolved),
                "uid": info.st_uid,
            }
        )
    ).hexdigest()


def _parse(value):
    if value is None:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _fmt(value):
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class ShadowRuntimeDepsV1:
    clock: object
    scheduler_authority: object
    query_local_news: object
    evaluate: object
    receipts: object
    bundle_paths: dict


def _identity(cfg, scheduled):
    hour = scheduled.replace(minute=0, second=0, microsecond=0)
    pre = {
        "cadence_version": cfg["cadence_version"],
        "classifier_digest": cfg["classifier_digest"],
        "source_path_identity_digest": cfg["canonical_path_identity_digest"],
        "spec_digest": cfg["spec_digest"],
        "utc_hour": _fmt(hour),
    }
    return hashlib.sha256(_bytes(pre)).hexdigest(), hour


def _settle_failure(
    store, identity, base, error, *, status="BLOCKED", phase="PREFLIGHT", queries=0
):
    store.settle(
        identity,
        "FAILED",
        {
            **base,
            "status": status,
            "phase": phase,
            "error_code": error,
            "query_count": queries,
            "finished_at_utc": base["started_at_utc"],
            "duration_ms": 0,
        },
    )


def _valid_config(config):
    if (
        set(config) != CONFIG_KEYS_V1
        or config.get("schema") != "tokmo.g1c.shadow-config.v1"
    ):
        return False
    if type(config.get("enabled")) is not bool or config.get("policy") not in (
        "PAUSED",
        "SHADOW",
    ):
        return False
    if (
        config.get("review_days") != 45
        or config.get("expiry_days") != 120
        or config.get("minute_kst") != 12
    ):
        return False
    if (
        any(
            config.get(k) != 0
            for k in (
                "http_start_cap",
                "model_start_cap",
                "retry_cap",
                "delivery_cap",
                "news_write_cap",
            )
        )
        or config.get("query_cap") != 1
    ):
        return False
    if (
        config.get("query_policy") != "STRICT_RO_V1"
        or config.get("cadence_version") != "g1c-hourly-minute12-kst-v1"
    ):
        return False
    if any(
        type(config.get(k)) is not str
        for k in (
            "project_root",
            "runner_path",
            "venv_python",
            "canonical_news_path",
            "canonical_path_identity_digest",
            "expected_commit",
            "spec_digest",
            "classifier_digest",
            "runtime_bundle_digest",
        )
    ):
        return False
    if any(
        value is not None and type(value) is not str
        for value in (
            config.get("activation_utc"),
            config.get("review_due_utc"),
            config.get("expiry_utc"),
        )
    ):
        return False
    return True


def run_ai_infra_capex_shadow_once_v1(config, deps):
    try:
        if not _valid_config(config):
            return 1
        if (
            not config["enabled"]
            or config["policy"] != "SHADOW"
            or config["activation_utc"] is None
        ):
            return 0
        now = deps.clock()
        if now.tzinfo is None:
            return 1
        now = now.astimezone(UTC)
        activation, review, expiry = map(
            _parse,
            (config["activation_utc"], config["review_due_utc"], config["expiry_utc"]),
        )
        if now < activation:
            return 0
        local = now.astimezone(ZoneInfo("Asia/Seoul"))
        scheduled_local = local.replace(minute=12, second=0, microsecond=0)
        scheduled = scheduled_local.astimezone(UTC)
        identity, hour = _identity(config, scheduled)
        store = deps.receipts
        token = store.acquire()
        if token is None:
            return 1
        try:
            existing = store.read(identity)
            if existing is not None:
                if existing.get("state") == "RESERVED":
                    store.reconcile_unknown(identity)
                return 1
            base = {
                "cadence_version": config["cadence_version"],
                "utc_hour": _fmt(hour),
                "scheduled_at_utc": _fmt(scheduled),
                "started_at_utc": _fmt(now),
                "policy": "SHADOW",
                "spec_digest": config["spec_digest"],
                "classifier_digest": config["classifier_digest"],
                "source_path_identity_digest": config["canonical_path_identity_digest"],
            }
            try:
                bundle_ok = (
                    runtime_bundle_digest_v1(deps.bundle_paths)
                    == config["runtime_bundle_digest"]
                )
            except Exception:
                bundle_ok = False
            if not bundle_ok:
                store.reserve(identity, {**base, "error_code": "NONE"})
                _settle_failure(store, identity, base, "RUNTIME_BUNDLE_MISMATCH")
                return 1
            try:
                path_ok = (
                    canonical_path_identity_digest_v1(config["canonical_news_path"])
                    == config["canonical_path_identity_digest"]
                )
            except Exception:
                path_ok = False
            if not path_ok:
                store.reserve(identity, {**base, "error_code": "NONE"})
                _settle_failure(store, identity, base, "PATH_IDENTITY_MISMATCH")
                return 1
            if not deps.scheduler_authority():
                store.reserve(identity, {**base, "error_code": "NONE"})
                _settle_failure(store, identity, base, "SCHEDULER_AUTHORITY_INVALID")
                return 1
            if not (12 <= local.minute < 17):
                store.reserve(identity, {**base, "error_code": "NONE"})
                _settle_failure(store, identity, base, "WINDOW_INVALID")
                return 1
            if now >= expiry:
                store.reserve(identity, {**base, "error_code": "NONE"})
                store.settle(
                    identity,
                    "EXPIRED",
                    {
                        **base,
                        "status": "EXPIRED",
                        "phase": "PREFLIGHT",
                        "error_code": "EXPIRED",
                        "query_count": 0,
                        "lifecycle_status": "EXPIRED",
                        "review_due": True,
                        "finished_at_utc": _fmt(now),
                        "duration_ms": 0,
                    },
                )
                return 1
            from tokmo.values.ai_infra_capex import LocalNewsQueryV1

            query = LocalNewsQueryV1(now - timedelta(days=30), now)
            store.reserve(identity, {**base, "error_code": "NONE"})
            calls = 0

            def once(path, q):
                nonlocal calls
                if calls:
                    raise ValueError("query count")
                calls += 1
                return deps.query_local_news(path, q)

            result = once(Path(config["canonical_news_path"]), query)
            query_status = getattr(
                _get(result, "status"), "value", _get(result, "status")
            )
            if query_status != "OK":
                _settle_failure(
                    store,
                    identity,
                    base,
                    "QUERY_NON_OK",
                    status="QUERY_FAILED",
                    phase="QUERY",
                    queries=1,
                )
                return 1
            run = deps.evaluate(
                lifecycle=_lifecycle(config, now),
                query=query,
                query_local_news=lambda q: result,
            )
            fields = _reduce(result, run)
            fields.update(
                base,
                status="OK",
                phase="SETTLE",
                error_code="NONE",
                query_count=calls,
                http_starts=0,
                model_starts=0,
                delivery_count=0,
                news_write_count=0,
                retry_count=0,
                lifecycle_status="REVIEW_DUE" if now >= review else "ACTIVE",
                review_due=now >= review,
                finished_at_utc=_fmt(now),
                duration_ms=0,
            )
            store.settle(identity, "SUCCEEDED", fields)
            return 0
        except Exception:
            try:
                if (
                    "identity" in locals()
                    and store.read(identity)
                    and store.read(identity).get("state") == "RESERVED"
                ):
                    store.settle(
                        identity,
                        "FAILED",
                        {
                            **base,
                            "status": "QUERY_FAILED",
                            "phase": "SETTLE",
                            "error_code": "QUERY_EXCEPTION",
                            "query_count": locals().get("calls", 0),
                        },
                    )
            except Exception:
                pass
            return 1
        finally:
            store.release(token)
    except Exception:
        return 1


def _lifecycle(config, now):
    from tokmo.values.ai_infra_capex import PROGRAM_ID_V1, PROGRAM_VERSION_V1
    from tokmo.values.signal_program_lifecycle import (
        ProgramKindV1,
        ProgramLifecycleSpecV1,
        evaluate_lifecycle_status,
    )

    spec = ProgramLifecycleSpecV1(
        PROGRAM_ID_V1,
        PROGRAM_VERSION_V1,
        ProgramKindV1.HYPOTHESIS_SENSOR,
        _parse(config["activation_utc"]),
        _parse(config["review_due_utc"]),
        _parse(config["expiry_utc"]),
        45,
        config["spec_digest"],
    )
    return evaluate_lifecycle_status(
        spec, as_of=now, last_evidence_at=None, evidence_count=0, renewal_count=0
    )


def _get(value, name, default=None):
    return (
        value.get(name, default)
        if isinstance(value, dict)
        else getattr(value, name, default)
    )


def _reduce(result, run):
    coverage = _get(result, "coverage")
    semantics = _get(result, "all_match_semantics")
    pack = _get(run, "pack")
    state = _get(pack, "state", _get(run, "sensor_state", None))
    state = getattr(state, "value", state)
    out = {
        "query_digest": _get(result, "query_digest"),
        "result_digest": _get(result, "result_digest"),
        "source_snapshot_digest": _get(result, "source_snapshot_digest"),
        "all_match_semantic_digest": _get(
            semantics,
            "all_match_semantic_digest",
            _get(run, "all_match_semantic_digest"),
        ),
        "pack_digest": _get(pack, "pack_digest", _get(run, "pack_digest")),
        "sensor_state": state,
    }
    for name in (
        "database_validation_row_count",
        "temporal_candidate_count",
        "temporal_candidate_normalized_bytes",
        "total_matched_rows",
        "total_matched_distinct_stories",
        "matched_distinct_source_count",
        "returned_count",
        "returned_distinct_story_count",
        "truncated_rows",
        "truncated_bytes",
        "future_source_skew_count",
        "future_availability_count",
    ):
        out[name] = _get(coverage, name)
    out.update(
        evidence_count=len(_get(pack, "evidence", ()) or ()),
        contradiction_count=len(_get(pack, "contradictions", ()) or ()),
        missing_need_count=len(_get(pack, "missing_needs", ()) or ()),
    )
    return out
