#!/usr/bin/env python3
"""Scheduler-facing NEWS group runner."""

import argparse
import dataclasses
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from tokmo.facts.telegram_dialogue_live import send_dialogue_message
from tokmo.programs.news_collection_coverage import recent_complete_polls
from tokmo.programs.news_group_runner import (
    GroupSourceResult,
    build_connector_argv,
    classify_failure,
    run_news_group,
    sanitize_http_status,
)
from tokmo.programs.news_poll_manifest import load_news_poll_manifest
from tokmo.programs.news_poll_policy import NewsGroup
from tokmo.programs.news_source_health import (
    apply_collection_health,
    deliver_pending,
    health_adjusted_exit,
    observe_tick,
)
from tokmo.programs.news_universe import (
    build_naver_query,
    effective_finnhub_symbols,
    load_news_universe,
    persist_news_universe,
    resolve_news_universe,
)
from tokmo.records.current_holdings_store import (
    CurrentHoldingsCorruptError,
    CurrentHoldingsStore,
)
from tokmo.records.holdings_snapshots import HoldingsSnapshotStore
from tokmo.records.source_health_store import SourceHealthStore
from tokmo.values.news_collection_diagnostics import (
    collection_complete,
    sanitize_diagnostics,
)
from tokmo.values.news_connectors import NewsFetchStatus
from tokmo.values.news_universe import UniverseRouting
from tokmo.watchlists import WatchlistError, load_watchlist_identities


def _last_progress(stderr, trace):
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if not isinstance(stderr, str):
        return None
    for line in reversed(stderr[-65536:].splitlines()):
        if not line.startswith("NEWS_PROGRESS ") or len(line) > 1024:
            continue
        try:
            value = sanitize_diagnostics(
                json.loads(line.removeprefix("NEWS_PROGRESS "))
            )
            if value["trace"] == trace:
                return value
        except (ValueError, TypeError):
            continue
    return None


def execute_invocation(
    invocation,
    campaign_id,
    *,
    runner: Path,
    db: Path,
    ledger: Path,
    credentials: Path,
    run=subprocess.run,
    clock=time.monotonic,
) -> GroupSourceResult:
    argv = build_connector_argv(
        invocation,
        runner=runner,
        db=db,
        ledger=ledger,
        credentials=credentials,
        campaign_id=campaign_id,
    )
    started = clock()
    trace = hashlib.sha256(campaign_id.encode()).hexdigest()[:16]
    diag = dict(
        stage="CHILD",
        cause=None,
        received=None,
        persisted=None,
        http_status=None,
        trace=trace,
    )
    code, failure, raw = None, None, None
    try:
        child = run(argv, capture_output=True, text=True, timeout=240, check=False)
        code = child.returncode
        diag = _last_progress(child.stderr, trace) or diag
        if code not in (0, 2, 3):
            failure = "CHILD_EXIT"
        elif not child.stdout.strip():
            failure = "CHILD_EXIT" if code else "CHILD_NO_OUTPUT"
        else:
            try:
                if len(child.stdout) > 32768:
                    raise ValueError
                raw = json.loads(child.stdout)
                required = {
                    "status",
                    "source",
                    "inserted",
                    "duplicate",
                    "conflict",
                    "revision",
                    "http_status",
                }
                statuses = {status.value for status in NewsFetchStatus} | {
                    "STORAGE_FAILURE",
                    "RUNNER_FAILURE",
                }
                if (
                    type(raw) is not dict
                    or not required <= set(raw)
                    or raw["source"] != invocation.source
                    or type(raw["status"]) is not str
                    or raw["status"] not in statuses
                    or any(
                        type(raw[key]) is not int or not 0 <= raw[key] <= 2**63 - 1
                        for key in ("inserted", "duplicate", "conflict", "revision")
                    )
                ):
                    raise ValueError
                sanitize_http_status(raw["http_status"])
                if raw.get("diagnostics") is not None:
                    final = sanitize_diagnostics(raw["diagnostics"])
                    if final["trace"] != trace:
                        raise ValueError
                    diag = final
                if raw["status"] in {"COLLECTED", "EMPTY"}:
                    if raw.get("diagnostics") is None and diag["stage"] == "CHILD":
                        diag["stage"] = (
                            "COMPLETE"  # old child final JSON, counts unknown
                        )
                    if code:
                        failure = "CHILD_EXIT"
                    elif not collection_complete(dict(raw, diagnostics=diag)):
                        failure = "INCOMPLETE_STORAGE"
            except (ValueError, TypeError, KeyError):
                failure = (
                    "CHILD_EXIT" if code and raw is None else "CHILD_INVALID_OUTPUT"
                )
                raw = None
    except subprocess.TimeoutExpired as error:
        diag = _last_progress(error.stderr, trace) or diag
        failure = "CHILD_TIMEOUT"
    except OSError:
        failure = "CHILD_LAUNCH"
    except UnicodeError:
        failure = "CHILD_INVALID_OUTPUT"
    diag.update(elapsed_ms=max(0, int((clock() - started) * 1000)), exit_code=code)
    if failure is not None:
        diag["cause"] = failure
        return GroupSourceResult(
            invocation.source,
            "RUNNER_FAILURE",
            0,
            0,
            0,
            0,
            http_status=diag.get("http_status"),
            failure_class=failure,
            target=invocation.symbol,
            diagnostics=diag,
        )
    status, http_status = raw["status"], raw["http_status"]
    cause = (
        None
        if status in {"COLLECTED", "EMPTY"}
        else (diag.get("cause") or classify_failure(status, http_status))
    )
    diag["cause"] = cause
    return GroupSourceResult(
        invocation.source,
        status,
        raw["inserted"],
        raw["duplicate"],
        raw["conflict"],
        raw["revision"],
        http_status,
        cause,
        invocation.symbol,
        diag,
    )


_RECEIPT_STATUSES = frozenset(
    {"SUCCEEDED", "SUCCEEDED_WITH_CONFLICTS", "PARTIAL_FAILURE", "FAILED"}
)
_SUCCESS_SOURCE_STATUSES = frozenset({"COLLECTED", "EMPTY"})
_HEALTH_ENV_KEYS = frozenset({"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"})


def _health_mode(path: Path) -> str:
    """Fail-closed gate read: absent, loose, or invalid always means OFF."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            return "OFF"
        mode = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "OFF"
    return mode if mode in ("RECORD", "DELIVER") else "OFF"


def _load_health_sender(path: Path):
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
    ):
        raise ValueError("unsafe credential file")
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw or raw.startswith("#"):
            continue
        if "=" not in raw:
            raise ValueError("invalid credential line")
        key, value = raw.split("=", 1)
        if key not in _HEALTH_ENV_KEYS or key in values or not value:
            raise ValueError("unknown, duplicate, or empty credential key")
        values[key] = value
    if set(values) != _HEALTH_ENV_KEYS:
        raise ValueError("missing credential key")
    return lambda body: str(
        send_dialogue_message(
            body_text=body,
            bot_token=values["TELEGRAM_BOT_TOKEN"],
            chat_id=values["TELEGRAM_CHAT_ID"],
        )
    )


def _run_health_step(args, result, raw_exit: int, *, now) -> int:
    mode = _health_mode(Path(args.runtime) / "source_health_activation.json")
    if mode == "OFF" or result.status not in _RECEIPT_STATUSES:
        return raw_exit
    receipt = json.loads(
        (Path(args.runtime) / "receipts" / f"{result.tick_id}.json").read_text(
            encoding="utf-8"
        )
    )
    health_db = args.health_db or args.ledger.parent / "source_health.sqlite3"
    store = SourceHealthStore(health_db)
    observations = observe_tick(receipt, now=args.scheduled_for)
    apply_collection_health(store, observations, now=now)
    if mode != "DELIVER":
        return raw_exit
    complete_polls = recent_complete_polls(Path(args.runtime) / "receipts", now=now)
    if args.health_telegram_env is not None:
        sender = _load_health_sender(args.health_telegram_env)
        deliver_pending(
            store, telegram_send=sender, now=now, complete_polls=complete_polls
        )
    failing = tuple(
        sorted(
            {
                row["source"]
                for row in receipt["sources"]
                if row["status"] not in _SUCCESS_SOURCE_STATUSES
            }
        )
    )
    return health_adjusted_exit(
        raw_exit, store, failing, mode=mode, now=now, complete_polls=complete_polls
    )


def _universe_inputs(args, *, now):
    """Read the three universe authorities, each failing soft to the stale
    lane (SDS §4.5); the manifest file is never touched."""
    holdings = None
    if args.holdings_store is not None:
        try:
            holdings = CurrentHoldingsStore(
                args.holdings_store, clock=lambda: now
            ).read()
        except CurrentHoldingsCorruptError:
            holdings = None
    watchlists = None
    if args.watchlists_dir is not None:
        try:
            watchlists = load_watchlist_identities(args.watchlists_dir)
        except WatchlistError:
            watchlists = None
    snapshot_names = {}
    if args.holdings_snapshots is not None:
        try:
            snapshot = HoldingsSnapshotStore(
                args.holdings_snapshots, clock=lambda: now
            ).read_latest()
            for account in snapshot.accounts:
                for item in account.holdings:
                    if item.name:
                        snapshot_names.setdefault(item.symbol, item.name)
        except Exception:
            snapshot_names = {}
    return holdings, watchlists, snapshot_names


def _apply_universe(args, manifest, *, now):
    """Build the effective manifest for this tick (SDS §4.8)."""
    store_path = args.universe_store or Path(args.runtime) / "news_universe.json"
    loaded = load_news_universe(store_path)
    if loaded is None and (store_path.exists() or store_path.is_symlink()):
        print("TOKMO_NEWS_UNIVERSE_CORRUPT: treated_absent", file=sys.stderr)
    holdings, watchlists, snapshot_names = _universe_inputs(args, now=now)
    universe = resolve_news_universe(
        holdings=holdings,
        watchlist_identities=watchlists,
        snapshot_names=snapshot_names,
        previous=loaded.latest if loaded is not None else None,
        now=now,
    )
    if universe is None:
        print("TOKMO_NEWS_UNIVERSE_FALLBACK: static_manifest", file=sys.stderr)
        return manifest
    embedded = None
    if loaded is not None:
        embedded = loaded.latest if not loaded.latest.stale else loaded.previous
    persist_news_universe(store_path, universe, previous=embedded)
    if universe.stale:
        since = universe.stale_since.isoformat()
        print(f"TOKMO_NEWS_UNIVERSE_STALE: since={since}", file=sys.stderr)
    overflow = {"US": 0, "KR": 0}
    for entry in universe.entries:
        if entry.routing is UniverseRouting.OVERFLOW and entry.market in overflow:
            overflow[entry.market] += 1
    if overflow["US"] or overflow["KR"]:
        print(
            f"TOKMO_NEWS_UNIVERSE_OVERFLOW: us={overflow['US']} kr={overflow['KR']}",
            file=sys.stderr,
        )
    effective = dataclasses.replace(
        manifest, finnhub_symbols=effective_finnhub_symbols(universe)
    )
    if manifest.naver_enabled:
        query = build_naver_query(universe)
        if query is None:
            effective = dataclasses.replace(effective, naver_enabled=False)
            print("TOKMO_NEWS_UNIVERSE_NAVER_SKIPPED: no_routable_kr", file=sys.stderr)
        else:
            effective = dataclasses.replace(effective, naver_query=query)
    return effective


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run-news-group")
    parser.add_argument("--group", required=True, choices=("A", "B", "C"))
    parser.add_argument(
        "--fast-primary",
        action="store_true",
        help="5-minute primary sources only; A/B/C offsets 0/1/2",
    )
    parser.add_argument("--scheduled-for", required=True, type=datetime.fromisoformat)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument(
        "--runner", type=Path, default=Path(__file__).with_name("run_news_connector.py")
    )
    parser.add_argument("--health-db", type=Path, default=None)
    parser.add_argument("--health-telegram-env", type=Path, default=None)
    parser.add_argument("--universe", action="store_true")
    parser.add_argument("--holdings-store", type=Path, default=None)
    parser.add_argument("--holdings-snapshots", type=Path, default=None)
    parser.add_argument("--watchlists-dir", type=Path, default=None)
    parser.add_argument("--universe-store", type=Path, default=None)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = load_news_poll_manifest(args.manifest)
        if args.universe:
            # A degraded universe never blocks collection: any resolution
            # error falls open to the static manifest, visibly (SDS §4.5).
            try:
                manifest = _apply_universe(args, manifest, now=datetime.now(UTC))
            except Exception as error:
                print(
                    "TOKMO_NEWS_UNIVERSE_FALLBACK: static_manifest "
                    f"({type(error).__name__})",
                    file=sys.stderr,
                )
        executor = partial(
            execute_invocation,
            runner=args.runner,
            db=args.db,
            ledger=args.ledger,
            credentials=args.credentials,
        )
        result = run_news_group(
            NewsGroup(args.group),
            args.scheduled_for,
            manifest,
            runtime_dir=args.runtime,
            executor=executor,
            fast_primary=args.fast_primary,
        )
    except (OSError, ValueError):
        print("TOKMO_NEWS_GROUP_FAILED: preflight", file=sys.stdout)
        return 2
    raw_exit = (
        0
        if result.status in {"SUCCEEDED", "SUCCEEDED_WITH_CONFLICTS", "DUPLICATE_TICK"}
        else 3
    )
    # The health step runs strictly after collection and can only fail open:
    # any error restores today's raw exit mapping, visibly (S6, SDS §3.7).
    code = raw_exit
    try:
        code = _run_health_step(args, result, raw_exit, now=datetime.now(UTC))
    except Exception as error:
        print(f"TOKMO_NEWS_HEALTH_DEGRADED: {type(error).__name__}", file=sys.stderr)
        code = raw_exit
    if raw_exit == 0:
        print(
            f"news_group status={result.status} tick={result.tick_id}", file=sys.stderr
        )
        return 0
    if code == 0:
        print(
            f"news_group status={result.status} tick={result.tick_id} "
            "health=suppressed",
            file=sys.stderr,
        )
        return 0
    print(f"TOKMO_NEWS_GROUP_FAILED: status={result.status} tick={result.tick_id}")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
