"""Bounded read-only production portfolio shape gate.

The gate is inert unless explicitly opted in by its CLI.  Its core accepts a
canonical tick/mask and a private resolver so fixture tests and the production
discovery layer exercise the same receipt -> immutable pack -> barrier ->
portfolio path.  Output is a closed aggregate summary: paths, account scopes,
secrets, symbols, quantities, and raw exceptions are never emitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Callable
from urllib.parse import quote

from tokmo.programs.analysis_barrier import assemble_analysis_materials
from tokmo.programs.portfolio_calculations import (
    _semantic, compute_portfolio_snapshot,
)
from tokmo.records.material_pack_store import MaterialPackWriter
from tokmo.records.calendar_cache import CalendarCacheStore
from tokmo.records.receipt_store import ReceiptStore
from tokmo.programs.production_cadence import (
    _day_from_cache_entry, _production_canonical_root,
)
from tokmo.values.cadence_policy import cadence_tick_v1
from tokmo.values.calendar_refresh import market_local_date
from tokmo.values.material_pack import (
    JobKind, canonical_decode, canonical_encode,
)
from tokmo.values.open_market_mask import build_open_market_mask
from tokmo.values.portfolio_calculations import (
    PortfolioScope, ScopeUnresolved, SnapshotStatus,
)
from tokmo.values.receipt_v1 import ACCOUNT_SCOPE_RE
from tokmo.values.account_scope_patterns import PENSION_SCOPE_RE
from tokmo.values.session_gate import Market, judge_sessions
from tokmo.values.ops_kernel import parse_iso


_SCOPE_RESOLVER = Callable[[str], PortfolioScope | ScopeUnresolved]


class CanonicalRuntimeUnavailable(Exception):
    """Canonical cadence has no complete, reconstructable runtime evidence."""


def _load_latest_complete_tick_mask(root: Path):
    """Read the latest COMPLETE cadence row and reconstruct its exact mask."""
    root = Path(root).resolve()
    database = root / "ops" / "cadence_ticks.sqlite3"
    uri = f"file:{quote(str(database))}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT policy_version,scheduled_for_utc,tick_id,status,"
            "mask_digest,open_markets_json FROM cadence_tick_receipts "
            "WHERE status='COMPLETE' ORDER BY scheduled_for_utc DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if row is None or row["mask_digest"] is None:
        raise CanonicalRuntimeUnavailable
    scheduled = parse_iso(row["scheduled_for_utc"])
    if scheduled is None:
        raise ValueError("cadence slot absent")
    tick = cadence_tick_v1(row["policy_version"], scheduled)
    if tick.tick_id != row["tick_id"]:
        raise ValueError("cadence tick identity mismatch")
    cache = CalendarCacheStore(
        root / "ops" / "calendar_cache.json", clock=lambda: scheduled,
    ).read()
    if cache is None or cache.kr is None or cache.us is None:
        raise CanonicalRuntimeUnavailable
    verdicts = judge_sessions(
        _day_from_cache_entry(Market.KR, cache.kr),
        _day_from_cache_entry(Market.US, cache.us), scheduled,
    )
    mask = build_open_market_mask(
        policy_version=tick.policy_version,
        scheduled_for_utc=scheduled, tick_id=tick.tick_id,
        verdicts=verdicts,
        local_dates={
            market: market_local_date(market, scheduled) for market in Market
        },
    )
    open_markets = canonical_decode(
        row["open_markets_json"].encode(), max_bytes=1024)
    if (mask.digest != row["mask_digest"]
            or sorted(market.value for market in mask.markets) != open_markets):
        raise ValueError("cadence mask evidence mismatch")
    return tick, mask, row["status"]


def _load_private_scope_resolver(path: Path | None):
    if path is None:
        return (lambda _raw: ScopeUnresolved.UNRESOLVED), ()
    path = Path(path)
    value = os.lstat(path)
    if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
            or stat.S_IMODE(value.st_mode) != 0o600):
        raise ValueError("unsafe scope map")
    mapping = canonical_decode(path.read_bytes(), max_bytes=64 * 1024)
    if type(mapping) is not dict:
        raise ValueError("invalid scope map")
    parsed = {}
    for raw, scope in mapping.items():
        if type(raw) is not str or (
            not ACCOUNT_SCOPE_RE.fullmatch(raw)
            and not PENSION_SCOPE_RE.fullmatch(raw)
        ):
            raise ValueError("invalid scope map key")
        parsed[raw] = PortfolioScope(scope)
    return (
        lambda raw: parsed.get(raw, ScopeUnresolved.UNRESOLVED),
        tuple(parsed),
    )


def _protected_paths(root: Path) -> tuple[Path, ...]:
    candidates = []
    for relative in (
        "ops/cadence_ticks.sqlite3", "ops/cadence_ticks.sqlite3-wal",
        "ops/cadence_ticks.sqlite3-shm", "ops/cadence_ticks.sqlite3-journal",
        "ops/calendar_cache.json",
    ):
        path = root / relative
        if path.exists() or path.is_symlink():
            candidates.append(path)
    for relative in ("ops/receipts", "ops/packs"):
        directory = root / relative
        if directory.is_dir():
            candidates.extend(path for path in directory.rglob("*")
                              if path.is_file() or path.is_symlink())
    return tuple(sorted(set(candidates), key=lambda path: str(path)))


def _outer_protected_paths(
        root: Path, scope_map_file: Path | None) -> tuple[Path, ...]:
    paths = _protected_paths(root)
    if scope_map_file is not None:
        paths = tuple(sorted(
            (*paths, scope_map_file.resolve()), key=lambda path: str(path)))
    return paths


def _snapshot(paths: tuple[Path, ...]) -> dict[str, tuple[int, int, int, str]]:
    result = {}
    for path in paths:
        value = os.lstat(path)
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise ValueError("unsafe protected evidence")
        raw = path.read_bytes()
        result[str(path)] = (
            stat.S_IMODE(value.st_mode), value.st_size, value.st_mtime_ns,
            hashlib.sha256(raw).hexdigest(),
        )
    return result


def _body_digest(body) -> str:
    return hashlib.sha256(canonical_encode({
        "v": 1,
        "domain": "tokmo-v3:portfolio-snapshot-body:v1",
        "body": _semantic(body),
    })).hexdigest()


def _reconciles(body) -> bool:
    for row in body.partition_totals:
        if row.unrealized_pnl_native != (
                row.gross_gains_native + row.gross_losses_native):
            return False
        if row.position_count != row.priced_position_count + row.unpriced_position_count:
            return False
    return True


def _base_report(tick, mask, cadence_status: str) -> dict:
    return {
        "gate_status": "FAIL",
        "tick_id": tick.tick_id,
        "cadence_status": cadence_status,
        "open_markets": sorted(market.value for market in mask.markets),
        "receipt_statuses": {},
        "receipt_reads": {job.value: 0 for job in JobKind},
        "pack_reads": {job.value: 0 for job in JobKind},
        "verified_pack_count": 0,
        "material_count": 0,
        "result_status": "NOT_REACHED",
        "result_reason": None,
        "unresolved_account_count": 0,
        "privacy": "NOT_REACHED",
        "digest_replay": "NOT_REACHED",
        "reconciliation": "NOT_REACHED",
        "before_after_equal": False,
        "provider_calls": 0,
        "network_calls": 0,
        "telegram_calls": 0,
        "order_calls": 0,
        "write_count": 0,
        "pension_live_subcase": "UNVERIFIED_UD-1",
    }


def run_portfolio_shape_gate(
        *, root: Path, tick, mask, resolve_scope: _SCOPE_RESOLVER,
        private_markers=(),
        cadence_status: str = "INJECTED_FIXTURE") -> dict:
    """Run the mutation-free gate and return only sanitized aggregate evidence."""
    root = Path(root).resolve()
    report = _base_report(tick, mask, cadence_status)
    paths = ()
    before = None
    preflight_complete = False
    try:
        paths = _protected_paths(root)
        before = _snapshot(paths)
        preflight_complete = True
        receipt_reads = report["receipt_reads"]
        pack_reads = report["pack_reads"]
        writer = MaterialPackWriter(root / "ops", clock=lambda: tick.scheduled_for_utc)

        def receipt_reader(job):
            def read():
                receipt_reads[job.value] += 1
                receipt = ReceiptStore(
                    root / "ops" / "receipts" / f"{job.value}.json",
                    clock=lambda: tick.scheduled_for_utc,
                ).read()
                if receipt is None:
                    raise ValueError("receipt absent")
                report["receipt_statuses"][job.value] = receipt.attempt.status.value
                return receipt
            return read

        def pack_reader(path, expected_sha):
            pack = writer.read_and_verify(Path(path), expected_sha)
            if pack.tick_id != tick.tick_id:
                raise ValueError("pack tick mismatch")
            pack_reads[pack.job.value] += 1
            report["verified_pack_count"] += 1
            return pack

        barrier = assemble_analysis_materials(
            tick=tick, mask=mask,
            receipt_readers={job: receipt_reader(job) for job in JobKind},
            pack_reader=pack_reader, now=tick.scheduled_for_utc,
        )
        report["material_count"] = len(barrier.materials)
        required_material_verified = (
            receipt_reads == {job.value: 1 for job in JobKind}
            and pack_reads[JobKind.HOLDINGS.value] == 1
            and JobKind.HOLDINGS in barrier.materials
        )
        if not required_material_verified:
            report["failure_reason"] = "REQUIRED_MATERIAL_NOT_VERIFIED"
        result = compute_portfolio_snapshot(
            barrier, resolve_scope=resolve_scope, fx_evidence=None)
        report["result_status"] = result.status.value
        report["result_reason"] = result.invalid_reason
        report["unresolved_account_count"] = result.unresolved_account_count
        if result.status is SnapshotStatus.SCOPE_RESOLUTION_INCOMPLETE:
            report["digest_replay"] = "NOT_REACHED_SCOPE_FAIL_CLOSED"
            report["reconciliation"] = "NOT_REACHED_SCOPE_FAIL_CLOSED"
        elif result.status is SnapshotStatus.COMPLETE and result.body is not None:
            report["digest_replay"] = (
                "PASS" if result.semantic_digest == _body_digest(result.body)
                else "FAIL")
            report["reconciliation"] = (
                "PASS" if _reconciles(result.body) else "FAIL")
        else:
            report["digest_replay"] = "NOT_REACHED_RESULT_FAIL_CLOSED"
            report["reconciliation"] = "NOT_REACHED_RESULT_FAIL_CLOSED"

        rendered = repr(result)
        if result.body is not None:
            rendered += canonical_encode(_semantic(result.body)).decode()
        forbidden = tuple(str(value) for value in private_markers if value)
        account_scope_pattern = ACCOUNT_SCOPE_RE.pattern
        if account_scope_pattern.startswith("^"):
            account_scope_pattern = account_scope_pattern[1:]
        if account_scope_pattern.endswith("$"):
            account_scope_pattern = account_scope_pattern[:-1]
        pension_scope_pattern = PENSION_SCOPE_RE.pattern
        if pension_scope_pattern.startswith("^"):
            pension_scope_pattern = pension_scope_pattern[1:]
        if pension_scope_pattern.endswith("$"):
            pension_scope_pattern = pension_scope_pattern[:-1]
        report["privacy"] = (
            "PASS" if not any(value in rendered for value in forbidden)
            and re.search(account_scope_pattern, rendered) is None
            and re.search(pension_scope_pattern, rendered) is None
            and "account_scope" not in rendered else "FAIL")
        if (required_material_verified
                and report["privacy"] == "PASS"
                and report["digest_replay"] in {
                    "PASS", "NOT_REACHED_SCOPE_FAIL_CLOSED"}
                and report["reconciliation"] in {
                    "PASS", "NOT_REACHED_SCOPE_FAIL_CLOSED"}):
            report["gate_status"] = "PASS"
    except Exception:
        report["gate_status"] = "FAIL"
        report["failure_reason"] = (
            "READ_ONLY_GATE_VALIDATION_FAILED" if preflight_complete
            else "PROTECTED_EVIDENCE_INVALID"
        )
    finally:
        if before is not None:
            try:
                after_paths = _protected_paths(root)
                after = _snapshot(after_paths)
                report["before_after_equal"] = (
                    paths == after_paths and before == after)
            except Exception:
                report["before_after_equal"] = False
            if not report["before_after_equal"]:
                report["gate_status"] = "FAIL"
                report["failure_reason"] = "IMMUTABLE_EVIDENCE_CHANGED"
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="run-portfolio-shape-gate")
    parser.add_argument("--opt-in", action="store_true")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--scope-map-file", type=Path)
    args = parser.parse_args(argv)
    if not args.opt_in:
        print(json.dumps({
            "gate_status": "SKIPPED",
            "reason": "EXPLICIT_OPT_IN_REQUIRED",
        }, sort_keys=True))
        return 0
    canonical = _production_canonical_root().resolve()
    root = canonical if args.root is None else args.root.resolve()
    if root != canonical:
        print(json.dumps({
            "gate_status": "REFUSED",
            "reason": "NONCANONICAL_ROOT",
        }, sort_keys=True))
        return 1
    if args.scope_map_file is not None:
        try:
            resolver, private_markers = _load_private_scope_resolver(
                args.scope_map_file)
        except (OSError, ValueError, json.JSONDecodeError):
            print(json.dumps({
                "gate_status": "FAIL",
                "failure_reason": "SCOPE_MAP_INVALID",
            }, sort_keys=True))
            return 1
    else:
        resolver, private_markers = _load_private_scope_resolver(None)
    if (not root.is_dir()
            or not (root / "ops" / "cadence_ticks.sqlite3").is_file()
            or not (root / "ops" / "calendar_cache.json").is_file()):
        print(json.dumps({
            "gate_status": "SKIPPED",
            "reason": "CANONICAL_RUNTIME_UNAVAILABLE",
        }, sort_keys=True))
        return 0
    outer_paths = ()
    outer_before = None
    try:
        outer_paths = _outer_protected_paths(root, args.scope_map_file)
        outer_before = _snapshot(outer_paths)
        tick, mask, cadence_status = _load_latest_complete_tick_mask(root)
        report = run_portfolio_shape_gate(
            root=root, tick=tick, mask=mask, resolve_scope=resolver,
            private_markers=private_markers,
            cadence_status=cadence_status,
        )
    except CanonicalRuntimeUnavailable:
        try:
            unchanged = (
                outer_before is not None
                and outer_paths == _outer_protected_paths(
                    root, args.scope_map_file)
                and outer_before == _snapshot(outer_paths)
            )
        except Exception:
            unchanged = False
        if not unchanged:
            print(json.dumps({
                "gate_status": "FAIL",
                "failure_reason": "IMMUTABLE_EVIDENCE_CHANGED",
            }, sort_keys=True))
            return 1
        print(json.dumps({
            "gate_status": "SKIPPED",
            "reason": "CANONICAL_RUNTIME_UNAVAILABLE",
        }, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({
            "gate_status": "FAIL",
            "failure_reason": "READ_ONLY_GATE_VALIDATION_FAILED",
        }, sort_keys=True))
        return 1
    try:
        outer_equal = (
            outer_paths == _outer_protected_paths(root, args.scope_map_file)
            and outer_before == _snapshot(outer_paths)
        )
    except Exception:
        outer_equal = False
    report["before_after_equal"] = (
        report["before_after_equal"] and outer_equal)
    if not report["before_after_equal"]:
        report["gate_status"] = "FAIL"
        report["failure_reason"] = "IMMUTABLE_EVIDENCE_CHANGED"
    print(json.dumps(report, sort_keys=True))
    return 0 if report["gate_status"] == "PASS" else 1
