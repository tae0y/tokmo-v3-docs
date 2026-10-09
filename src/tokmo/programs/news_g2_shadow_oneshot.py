"""Bounded G2 NEWS actual-miss collection through the production Pulse seam.

The operator uses an explicit in-memory SHADOW gate and a read-only canonical
corpus path while writing all receipts and NEWS stores below a private isolated
root.  It never creates ``news_activation.json``, binds a model/provider, sends
Telegram, registers a scheduler, or calls the producer outside ``run_pulse_once``.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tokmo.programs.integrated_pulse import run_pulse_once
from tokmo.programs.news_topic_discovery import discover_topics
from tokmo.programs.pulse_production_deps import build_pulse_production_deps
from tokmo.records.dialogue_activation import DialogueActivationV1
from tokmo.records.external_news_query import read_discovery_documents_v1
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.news_config import (
    DISCOVERY_MAX_DOCUMENTS,
    DISCOVERY_WINDOW_HOURS,
    PRODUCER_INTERVAL_SECONDS,
)
from tokmo.values.session_gate import Market


def _source_snapshot(path: Path) -> dict:
    stat = path.stat()
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _sidecar_snapshot(path: Path) -> dict:
    result = {}
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            stat = sidecar.stat()
            result[suffix] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    return result


def _freeze_corpus(source: Path, root: Path) -> Path:
    target = root / "input" / "corpus_snapshot.sqlite3"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise ValueError("invalid existing G2 corpus snapshot")
        return target
    uri = f"file:{source.resolve()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as source_connection:
        with closing(sqlite3.connect(target)) as target_connection:
            source_connection.backup(target_connection)
    os.chmod(target, 0o600)
    return target


def _count(connection, sql, parameters=()):
    return connection.execute(sql, parameters).fetchone()[0]


def _metrics(root: Path) -> dict:
    episode_path = root / "stores" / "news_episodes.sqlite3"
    ops_path = root / "ops" / "ops_kernel.sqlite3"
    metrics = {
        "discovery_runs": 0,
        "scanned_documents": 0,
        "discovery_candidates": 0,
        "candidate_states": {},
        "classifier_calls": 0,
        "telegram_news_outbox": 0,
        "news_event_outbox": 0,
    }
    if episode_path.exists():
        with closing(sqlite3.connect(episode_path)) as connection:
            metrics["discovery_runs"] = _count(
                connection, "SELECT COUNT(*) FROM discovery_runs"
            )
            metrics["scanned_documents"] = _count(
                connection,
                "SELECT COALESCE(SUM(scanned_documents),0) FROM discovery_runs",
            )
            metrics["discovery_candidates"] = _count(
                connection, "SELECT COUNT(*) FROM discovery_candidates"
            )
            metrics["classifier_calls"] = _count(
                connection, "SELECT COUNT(*) FROM classifier_calls"
            )
            metrics["candidate_states"] = dict(
                connection.execute(
                    "SELECT state,COUNT(*) FROM discovery_candidates GROUP BY state"
                ).fetchall()
            )
    if ops_path.exists():
        with closing(sqlite3.connect(ops_path)) as connection:
            metrics["telegram_news_outbox"] = _count(
                connection,
                "SELECT COUNT(*) FROM alert_outbox"
                " WHERE channel='TELEGRAM' AND origin LIKE 'NEWS%'",
            )
            metrics["news_event_outbox"] = _count(
                connection,
                "SELECT COUNT(*) FROM alert_outbox WHERE origin='NEWS_EVENT'",
            )
    return metrics


def _isolated(root: Path, corpus: Path) -> bool:
    root = root.resolve(strict=False)
    corpus = corpus.resolve(strict=True)
    return root != corpus.parent and root not in corpus.parents


def run_news_g2_shadow_oneshot(*, root, corpus_path, cutoff, observed_now_utc) -> dict:
    """Run exactly one aligned Pulse slot with NEWS SHADOW producer enabled."""
    root = Path(root)
    corpus_path = Path(corpus_path)
    if (
        not isinstance(observed_now_utc, datetime)
        or observed_now_utc.tzinfo is None
        or observed_now_utc.utcoffset() is None
    ):
        raise ValueError("observed_now_utc must be aware")
    if (
        not isinstance(cutoff, datetime)
        or cutoff.tzinfo is None
        or cutoff.utcoffset() is None
    ):
        raise ValueError("cutoff must be aware")
    if not corpus_path.is_file() or not _isolated(root, corpus_path):
        raise ValueError("G2 root must be isolated from the corpus")
    if root.exists() and (root / "news_activation.json").exists():
        raise ValueError("G2 root must not contain an activation file")

    source_before = _source_snapshot(corpus_path)
    sidecars_before = _sidecar_snapshot(corpus_path)
    snapshot_path = _freeze_corpus(corpus_path, root)
    snapshot = _source_snapshot(snapshot_path)
    slot_epoch = (
        int(observed_now_utc.timestamp())
        // PRODUCER_INTERVAL_SECONDS
        * PRODUCER_INTERVAL_SECONDS
    )
    scheduled = datetime.fromtimestamp(slot_epoch, UTC)
    observed = scheduled
    window_from = max(scheduled - timedelta(hours=DISCOVERY_WINDOW_HOURS), cutoff)
    read = read_discovery_documents_v1(
        snapshot_path, window_from=window_from, max_rows=DISCOVERY_MAX_DOCUMENTS
    )
    if read.status != "OK":
        raise RuntimeError(f"G2 corpus read failed: {read.status}")
    documents = read.documents
    audit = discover_topics(documents, holdings=())
    gate = DialogueActivationV1(True, cutoff, "g2-news-shadow-oneshot")
    ops = OpsKernelStore(
        root / "ops" / "ops_kernel.sqlite3", stable_phase_ids=frozenset()
    )
    deps = build_pulse_production_deps(
        root=root,
        clock=lambda: observed,
        watchlist_symbols={Market.KR: frozenset(), Market.US: frozenset()},
        dialogue_cutoff=cutoff,
        news_ops_kernel=ops,
        news_gate=(gate, "SHADOW"),
        news_documents_path=snapshot_path,
        quotes_provider_enabled=False,
        execution_provider_enabled=False,
        conditional_provider_enabled=False,
        dialogue_activation_enabled=False,
    )
    tick = run_pulse_once(
        root=root,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=scheduled,
        observed_now_utc=observed,
        deps=deps,
    )
    source_after = _source_snapshot(corpus_path)
    sidecars_after = _sidecar_snapshot(corpus_path)
    metrics = _metrics(root)
    audit_matches_production = metrics["scanned_documents"] == len(
        documents
    ) and metrics["discovery_candidates"] == len(audit.candidates)
    return {
        "status": tick.outcome,
        "reason": tick.reason,
        "scheduled_for_utc": scheduled.isoformat(),
        "stages": [dict(stage) for stage in tick.stages],
        "wire_counts": dict(tick.wire_counts),
        "source": source_before,
        "source_sidecars_before": sidecars_before,
        "source_sidecars_after": sidecars_after,
        "source_unchanged": (
            source_before == source_after and sidecars_before == sidecars_after
        ),
        "snapshot": {**snapshot, "path": str(snapshot_path)},
        "metrics": metrics,
        "actual_miss_evidence": {
            "window_from": window_from.isoformat(),
            "window_to": scheduled.isoformat(),
            "corpus_snapshot_digest": audit.corpus_snapshot_digest,
            "policy_digest": audit.policy_digest,
            "syndicated_documents": audit.syndicated_documents,
            "dropped_over_cap": audit.dropped_over_cap,
            "surfaced_candidates": [asdict(item) for item in audit.candidates],
            "dropped_candidates": [asdict(item) for item in audit.dropped_candidates],
            "decisions": [asdict(item) for item in audit.decisions],
            "matches_production": audit_matches_production,
        },
    }
