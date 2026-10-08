"""Narrow composition root for the GET-only OpenDART bulk lane."""

from datetime import datetime, timedelta
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from tokmo.facts.opendart_bulk_live import OpenDartBulkLiveTransport
from tokmo.facts.opendart_live import OpenDartLiveTransport
from tokmo.facts.opendart_targeted import (
    OpenDartTargetError, resolve_kr_relevance,
)
from tokmo.programs.opendart_fundamentals_enrichment import (
    enrichment_deadline, run_fundamentals_enrichment,
)
from tokmo.programs.opendart_pilot import (
    TICK_DEADLINE, retention_floor, run_baseline, run_incremental,
    run_operate, run_preflight,
)
from tokmo.records.holdings_snapshots import (
    HoldingsSnapshotReadError, HoldingsSnapshotStore,
    holdings_snapshot_store_path,
)
from tokmo.records.opendart_disclosures import DisclosureStore
from tokmo.records.opendart_fundamentals_store import OpenDartFundamentalsStore
from tokmo.runtime.config import load_opendart_config
from tokmo.runtime.wallclock import utc_now
from tokmo.values.session_gate import Market
from tokmo.watchlists import WatchlistError, load_watchlists

DART_ENV_NAME = "opendart.env"
ENRICHMENT_DB_NAME = "opendart-fundamentals.sqlite3"
# Operator-only meta commands (fundamentals SDS §10); never scheduled.
ENRICH_COMMANDS = frozenset({
    "enrich-init", "enrich-status", "enrich-pause", "enrich-resume",
    "enrich-set-cap",
})
_PENSION_ABSENT_REASONS = frozenset({"not_found", "schema_mismatch"})


def _seoul_date(now: datetime):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware_now_required")
    return now.astimezone(ZoneInfo("Asia/Seoul")).date()


def _require_private_directory(path: Path):
    # An owner-owned symlink into the project is accepted (roots relocated 2026-09-24);
    # the target must be the caller's 0700 directory. Credential files still refuse symlinks.
    if path.is_symlink() and path.lstat().st_uid != os.getuid():
        raise ValueError("private_state_invalid")
    real = path.resolve()
    if (not real.is_dir() or real.is_symlink() or
            (real.stat().st_mode & 0o777) != 0o700 or real.stat().st_uid != os.getuid()):
        raise ValueError("private_state_invalid")


def _require_private_credential(path: Path):
    if (path.is_symlink() or not path.is_file() or
            (path.stat().st_mode & 0o777) != 0o600):
        raise ValueError("config_mode")


class _EnrichmentSourceUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _kr_relevance_sources(data_dir: Path):
    """SDS §4.1 fresh fail-closed universe reads for one tick.

    project_root resolves inside the git working tree the scheduler executes
    (worktree-is-production), which is why the tracked watchlists directory
    is reachable relative to this file.
    """
    project_root = Path(__file__).resolve().parents[2]
    scope_note = None
    try:
        holdings = HoldingsSnapshotStore(
            holdings_snapshot_store_path(data_dir), clock=utc_now)
        snapshots = (holdings.read_latest(),)
        try:
            snapshots += (holdings.read_latest_pension_snapshot(),)
        except HoldingsSnapshotReadError as error:
            if error.reason not in _PENSION_ABSENT_REASONS:
                raise
            scope_note = "pension_scope_absent"
        watchlist = load_watchlists(project_root / "watchlists")[Market.KR]
        relevance = resolve_kr_relevance(
            snapshots=snapshots, watchlist_symbols=watchlist)
    except (HoldingsSnapshotReadError, WatchlistError,
            OpenDartTargetError) as error:
        reason = getattr(error, "reason", None) or type(error).__name__
        raise _EnrichmentSourceUnavailable(reason) from None
    return relevance, scope_note


def _run_enrichment_after_bulk(*, bulk_status: str, bulk_store, data_dir: Path,
                               enrichment_path: Path, api_key: str,
                               now: datetime) -> dict:
    """SDS §8: strictly after the bulk receipts committed; no exception may
    escape into the bulk path, and the runner exit code stays a function of
    the bulk status only."""
    try:
        if bulk_status != "complete":
            return {"status": "not_attempted"}
        if not enrichment_path.is_file():
            # Absence-gate (§6): no store means inactive, starts 0, no writes.
            return {"status": "inactive"}
        store = OpenDartFundamentalsStore.open(enrichment_path)
        try:
            if store.meta("enabled") != "1":
                return {"status": "paused"}
            try:
                relevance, scope_note = _kr_relevance_sources(data_dir)
            except _EnrichmentSourceUnavailable as unavailable:
                store.append_tick_event(
                    at=utc_now(), outcome="refused_inputs_unavailable",
                    reason=unavailable.reason, reports_processed=0, starts=0)
                return {"status": "refused_inputs_unavailable"}
            return run_fundamentals_enrichment(
                bulk_store=bulk_store, enrichment_store=store,
                relevance=relevance, api_key=api_key,
                transport=OpenDartLiveTransport(), now=now, clock=utc_now,
                deadline=enrichment_deadline(now, utc_now()),
                scope_note=scope_note)
        finally:
            store.close()
    except Exception:
        return {"status": "error"}


def _execute_enrich_command(command: str, path: Path, now: datetime,
                            cap_value: int | None) -> dict:
    if command == "enrich-init":
        store = OpenDartFundamentalsStore.init_create(path, now=now)
        try:
            return {"status": "initialized",
                    "activation_epoch": store.meta("activation_epoch"),
                    "cumulative_start_cap": int(
                        store.meta("cumulative_start_cap")),
                    "enabled": 1}
        finally:
            store.close()
    if command == "enrich-status":
        if not path.is_file():
            return {"status": "absent"}
        reader = OpenDartFundamentalsStore.open_readonly(path)
        try:
            return {"status": "present", **reader.status_summary()}
        finally:
            reader.close()
    if command == "enrich-set-cap":
        # Requires a fresh explicit user approval per SDS §5; the CLI passes
        # the approved value through --expected-planned-pages (the argparse
        # surface admits choices additions only).
        if cap_value is None or cap_value < 0:
            raise ValueError("cap_value_required")
        store = OpenDartFundamentalsStore.open(path)
        try:
            store.set_meta("cumulative_start_cap", str(cap_value))
            store.set_meta("cap_set_at", now.isoformat())
            return {"status": "cap_set", "cumulative_start_cap": cap_value}
        finally:
            store.close()
    store = OpenDartFundamentalsStore.open(path)
    try:
        enabled = "0" if command == "enrich-pause" else "1"
        store.set_meta("enabled", enabled)
        store.set_meta("enabled_changed_at", now.isoformat())
        return {"status": "paused" if enabled == "0" else "resumed"}
    finally:
        store.close()


def execute_opendart_bulk(*, home: Path, command: str, now: datetime,
                          expected_preflight_id: str | None = None,
                          expected_planned_pages: int | None = None) -> dict:
    config_dir = (home / ".config" / "tokmo-v3").resolve()  # roots may be symlinks into the project (2026-09-24)
    data_dir = (home / ".local" / "share" / "tokmo-v3").resolve()
    db_path = data_dir / "opendart-bulk.sqlite3"
    if command in ENRICH_COMMANDS:
        return _execute_enrich_command(
            command, data_dir / ENRICHMENT_DB_NAME, now,
            expected_planned_pages)
    if command in {"status", "integrity"}:
        # Strictly read-only: never create, migrate, or touch the store here.
        if not db_path.is_file():
            return {"integrity": "absent", "runs": {}, "partitions": {}}
        reader = DisclosureStore.open_readonly(db_path)
        try:
            runs = dict(reader.connection.execute(
                "select status,count(*) from runs group by status order by status"
            ).fetchall())
            partitions = {}
            if reader.partitions_migrated():
                partitions = dict(reader.connection.execute(
                    "select status,count(*) from day_partitions "
                    "group by status order by status").fetchall())
            return {"integrity": reader.integrity(), "runs": runs,
                    "partitions": partitions}
        finally:
            reader.close()
    store = DisclosureStore.open(db_path)
    config = None
    try:
        today = _seoul_date(now)
        if command == "migrate":
            return {"status": "migrated",
                    "seeded": store.migrate_partitions(today, now)}
        if command == "backfill-seed":
            return {"status": "seeded",
                    "seeded": store.backfill_seed(retention_floor(today), now)}
        if command == "prune":
            return {"status": "pruned",
                    **store.prune(retention_floor(today), now)}
        _require_private_directory(config_dir)
        config_path = config_dir / DART_ENV_NAME
        _require_private_credential(config_path)
        config = load_opendart_config(config_path)
        transport = OpenDartBulkLiveTransport()
        if command == "preflight":
            receipt = run_preflight(store, fixed_cutoff=today - timedelta(days=1),
                                    api_key=config.api_key, transport=transport, now=now)
        elif command == "baseline":
            if expected_preflight_id is None or expected_planned_pages is None:
                raise ValueError("expected_preflight_required")
            receipt = run_baseline(
                store, expected_preflight_id=expected_preflight_id,
                expected_planned_pages=expected_planned_pages,
                api_key=config.api_key, transport=transport, now=now)
        elif command in {"incremental", "pilot-tick"}:
            receipt = run_incremental(store, today=today, api_key=config.api_key,
                                      transport=transport, now=now)
        elif command == "operate":
            result = run_operate(store, today=today, api_key=config.api_key,
                                 transport=transport, now=now,
                                 deadline=now + TICK_DEADLINE, clock=utc_now)
            result["enrichment"] = _run_enrichment_after_bulk(
                bulk_status=result["status"], bulk_store=store,
                data_dir=data_dir,
                enrichment_path=data_dir / ENRICHMENT_DB_NAME,
                api_key=config.api_key, now=now)
            return result
        else:
            raise ValueError("unknown_bulk_command")
        return {"run_id": receipt.run_id, "kind": receipt.kind,
                "status": receipt.status, "begin": receipt.begin_de,
                "end": receipt.end_de, "planned_pages": receipt.planned_pages,
                "fetched_pages": receipt.fetched_pages, "new": receipt.new_count,
                "duplicate": receipt.duplicate_count,
                "conflict": receipt.conflict_count,
                "revision": receipt.revision_count}
    finally:
        if config is not None:
            del config
        store.close()
