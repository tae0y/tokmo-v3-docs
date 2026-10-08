#!/usr/bin/env python3
"""One-shot, fail-closed Quant W4 policy supervisor (never a cadence runner)."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import stat
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
REPO = PROJECT.parents[1]
SRC = PROJECT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tokmo.programs.market_regime_shadow import resolve_completed_session_by_market
from tokmo.records.calendar_cache import CalendarCacheStore
from tokmo.records.calendar_lookahead import CalendarLookaheadStoreV1, judge_lookahead_session
from tokmo.records.regular_session_history import RegularSessionHistoryStore
from tokmo.values.quant_multifactor_core import MarketV1
from tokmo.values.quant_recurring import (
    QUANT_CANDLE_MAX_PAGES, QUANT_RECURRING_PROGRAM_ID,
    QUANT_RECURRING_UNIVERSE_POLICY_VERSION, QuantDueIdentityV1,
    membership_plan_starts,
)
from tokmo.values.session_gate import Market
from tokmo.values.material_pack import canonical_decode
from tokmo.values.ops_kernel import iso, parse_iso
from tokmo.values.cadence_policy import CADENCE_OPERATIONS

BASE_COMMIT = "c8eb9ea46ad5fad1cabc43af50b032097e1d0227"
MARKET_COMMIT = "6c837495bd342c97f185fde7bdb8898c88ca9ac1"
QUANT_COMMIT = "4ce2ee95ec55c6a0f5fc3d760debed79e8272f80"
JOB_ID = "1ca78ff85003"
HERMES_JOBS = Path.home() / ".hermes" / "cron" / "jobs.json"
CADENCE_SCRIPT = REPO / "projects/tokmo-v3/ops/hermes-cron/tokmo_v3_cadence.sh"
MARKET_POLICY = "cadence-300s-market-portfolio-v1"
QUANT_POLICY = "cadence-300s-market-quant4-shadow-v1"


class Refusal(RuntimeError):
    pass


KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class RunPlan:
    target_kst_date: date
    completed_session_date: date
    official_close_utc: datetime

    def __post_init__(self) -> None:
        close = self.official_close_utc
        if (close.tzinfo is None or close.utcoffset() != timedelta(0)
                or close.date() != self.completed_session_date
                or self.run_at_utc.astimezone(KST).date() != self.target_kst_date):
            raise Refusal("one-shot plan is internally inconsistent")

    @property
    def run_at_utc(self) -> datetime:
        return self.official_close_utc + timedelta(minutes=2)

    @property
    def slots(self) -> tuple[datetime, datetime, datetime, datetime]:
        close = self.official_close_utc
        return (
            close,
            close + timedelta(minutes=5),
            close + timedelta(minutes=15),
            close + timedelta(minutes=25),
        )

    @property
    def prior_cutoff_utc(self) -> datetime:
        return self.official_close_utc + timedelta(minutes=4)


PLAN = RunPlan(
    target_kst_date=date(2026, 8, 22),
    completed_session_date=date(2026, 8, 21),
    official_close_utc=datetime(2026, 8, 21, 20, 0, tzinfo=timezone.utc),
)
TARGET_DATE = PLAN.target_kst_date
SESSION_DATE = PLAN.completed_session_date
FILES = (
    "projects/tokmo-v3/tools/run_tokmo_cadence.py",
    "projects/tokmo-v3/tests/test_cadence_policy.py",
    "projects/tokmo-v3/tests/test_quant_recurring_architecture.py",
)
TERMINAL = frozenset({"COMPLETE", "SKIPPED_CLOSED", "PARTIAL", "FAILED", "MISSED", "UNKNOWN"})
REPLAY_REASON = "already_terminal"
TERMINAL_ALLOWANCE_SECONDS = 390


class SimulatedCrash(RuntimeError):
    pass


class RunAndRecoveryFailure(Refusal):
    pass


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace(path: Path, raw: bytes, mode: int | None = None) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode or 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    if mode is not None:
        os.chmod(temporary, mode)
    os.replace(temporary, path)
    _sync(path.parent)


def git(args: list[str]) -> bytes:
    return subprocess.run(["git", *args], cwd=REPO, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


@dataclass(frozen=True)
class FilePin:
    path: str
    market_sha256: str
    quant_sha256: str


@dataclass(frozen=True)
class Config:
    arm: Path
    repo: Path
    root: Path
    cadence_lock: Path
    receipt_db: Path
    quant_db: Path
    ops_db: Path
    calendar_cache: Path
    history_db: Path
    lookahead_db: Path
    jobs_store: Path
    python: Path
    files: tuple[FilePin, ...]
    observed_head: str

    @property
    def journal(self):
        return self.root / "quant-w4-journal.json"

    @property
    def supervisor_lock(self):
        return self.root / "quant-w4-supervisor.lock"


_KEYS = {"schema", "repo_root", "runtime_root", "cadence_lock", "receipt_db",
         "quant_db", "ops_db", "calendar_cache", "history_db", "lookahead_db", "job_id",
         "base_commit", "market_commit", "quant_commit", "python", "files",
         "jobs_store"}


def _absolute(value, name):
    path = Path(value)
    if not path.is_absolute():
        raise Refusal(f"{name} must be absolute")
    return path


def _under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def load_and_validate(arm: Path, *, recovery=False) -> Config:
    arm = Path(arm)
    try:
        value = os.lstat(arm)
        if not stat.S_ISREG(value.st_mode) or stat.S_IMODE(value.st_mode) != 0o600:
            raise Refusal("arm config must be a mode-0600 nonsymlink")
        raw = json.loads(arm.read_text())
    except Refusal:
        raise
    except Exception as error:
        raise Refusal("arm config unavailable") from error
    if type(raw) is not dict or set(raw) != _KEYS or raw["schema"] != 1:
        raise Refusal("arm config schema mismatch")
    fixed = {"job_id": JOB_ID, "base_commit": BASE_COMMIT,
             "market_commit": MARKET_COMMIT, "quant_commit": QUANT_COMMIT}
    if any(raw[key] != expected for key, expected in fixed.items()):
        raise Refusal("arm config pin mismatch")
    repo = _absolute(raw["repo_root"], "repo_root")
    try:
        if repo.resolve(strict=True) != REPO.resolve(strict=True):
            raise Refusal("repo_root is not the pinned canonical repo")
    except FileNotFoundError as error:
        raise Refusal("repo_root unavailable") from error
    root = _absolute(raw["runtime_root"], "runtime_root")
    jobs_store = _absolute(raw["jobs_store"], "jobs_store")
    if jobs_store != HERMES_JOBS or jobs_store.is_symlink() or not jobs_store.is_file():
        raise Refusal("canonical Hermes jobs store mismatch")
    paths = {name: _absolute(raw[name], name) for name in (
        "cadence_lock", "receipt_db", "quant_db", "ops_db", "calendar_cache",
        "history_db", "lookahead_db")}
    if not root.is_dir() or not paths["cadence_lock"].parent.is_dir() or any(
            not _under(path, root) for path in paths.values()):
        raise Refusal("runtime path mismatch")
    for name in ("receipt_db", "ops_db", "calendar_cache", "history_db", "lookahead_db"):
        path = paths[name]
        if path.is_symlink() or not path.is_file():
            raise Refusal("runtime DB/cache path missing")
    python = _absolute(raw["python"], "python")
    if python.resolve() != Path(sys.executable).resolve() or sys.version_info[:2] != (3, 12):
        raise Refusal("unsupported Python")
    try:
        top = Path(git(["rev-parse", "--show-toplevel"]).decode().strip())
        if top.resolve(strict=True) != repo:
            raise Refusal("tool is not executing from canonical repo")
        git(["cat-file", "-e", f"{BASE_COMMIT}^{{commit}}"])
        git(["merge-base", "--is-ancestor", BASE_COMMIT, "HEAD"])
        observed_head = git(["rev-parse", "HEAD"]).decode().strip()
        status = git(["status", "--porcelain"]).decode()
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise Refusal("git validation failed") from error
    dirty = {line[3:] for line in status.splitlines() if len(line) > 3}
    pins = []
    if type(raw["files"]) is not list or len(raw["files"]) != 3:
        raise Refusal("exactly three file pins required")
    for item in raw["files"]:
        if type(item) is not dict or set(item) != {"path", "market_sha256", "quant_sha256"}:
            raise Refusal("file pin schema mismatch")
        pin = FilePin(**item)
        if pin.path not in FILES or (pin.path in dirty and not recovery):
            raise Refusal("target bytes dirty")
        path = repo / pin.path
        actual = None if path.is_symlink() or not path.is_file() else _sha(path.read_bytes())
        allowed = {pin.market_sha256, pin.quant_sha256} if recovery else {pin.market_sha256}
        if actual not in allowed:
            raise Refusal("target bytes are not reviewed policy bytes")
        for commit, expected in ((MARKET_COMMIT, pin.market_sha256),
                                 (QUANT_COMMIT, pin.quant_sha256)):
            if _sha(git(["cat-file", "blob", f"{commit}:{pin.path}"])) != expected:
                raise Refusal("commit digest mismatch")
        pins.append(pin)
    if tuple(pin.path for pin in pins) != FILES:
        raise Refusal("file order mismatch")
    try:
        jobs = json.loads(jobs_store.read_text())
        entries = jobs["jobs"]
        matches = [item for item in entries if item.get("id") == JOB_ID]
        job = matches[0] if len(matches) == 1 else {}
        required = {"id", "enabled", "schedule", "no_agent", "script",
                    "workdir", "deliver"}
        if (type(entries) is not list or len(matches) != 1
                or not required <= set(job)
                or job["enabled"] is not True or job["no_agent"] is not True
                or job["schedule"] != {"kind": "cron", "expr": "*/5 * * * *",
                                       "display": "*/5 * * * *"}
                or job["script"] != CADENCE_SCRIPT.name
                or Path(job["workdir"]) != repo / "projects/tokmo-v3"
                or job["deliver"] != "local"):
            raise Refusal("cadence job authority mismatch")
    except Refusal:
        raise
    except Exception as error:
        raise Refusal("cadence job authority unavailable") from error
    return Config(arm, repo, root, jobs_store=jobs_store, python=python,
                  files=tuple(pins), observed_head=observed_head, **paths)


@contextmanager
def acquire(path: Path):
    path = Path(path)
    if not path.parent.is_dir() or path.is_symlink():
        raise Refusal("lock path unavailable")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(descriptor)
        raise Refusal("lock unavailable") from error
    try:
        yield descriptor
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def canonical_session(config: Config, now_utc: datetime) -> date:
    clock = lambda: now_utc
    cache = CalendarCacheStore(config.calendar_cache, clock=clock).read()
    if cache is None or cache.us is None or cache.us.trade_date != SESSION_DATE.isoformat():
        raise Refusal("absent or stale calendar")
    regular = cache.us.sessions.get("regularMarket")
    if regular is None:
        raise Refusal("regular session absent")
    official_close = datetime.fromisoformat(regular.end.replace("Z", "+00:00"))
    if official_close != PLAN.official_close_utc:
        raise Refusal("official close does not match one-shot plan")
    history = RegularSessionHistoryStore(config.history_db, clock=clock)
    today = history.read(Market.US, SESSION_DATE)
    completed = resolve_completed_session_by_market(
        scheduled_for_utc=now_utc, market=Market.US, today_row=today,
        history_store=history, official_close=official_close)
    days = CalendarLookaheadStoreV1(
        config.lookahead_db, clock=clock).read_days(Market.US)
    verdict = judge_lookahead_session(Market.US, days, now_utc)
    if not verdict.open or verdict.trade_date != SESSION_DATE.isoformat():
        raise Refusal("canonical lookahead admission closed")
    if completed != SESSION_DATE or now_utc < official_close:
        raise Refusal("completed session mismatch")
    return completed


def preflight_session(config: Config, now_utc: datetime) -> QuantDueIdentityV1:
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise Refusal("aware time required")
    kst_now = now_utc.astimezone(KST)
    if (kst_now.date() != TARGET_DATE or (kst_now.hour, kst_now.minute) != (5, 2)):
        raise Refusal("target session date mismatch")
    completed = canonical_session(config, now_utc.astimezone(timezone.utc))
    if completed != SESSION_DATE or membership_plan_starts(4) != 7 or QUANT_CANDLE_MAX_PAGES != 2:
        raise Refusal("canonical Quant policy drift")
    return QuantDueIdentityV1(
        QUANT_RECURRING_PROGRAM_ID, QUANT_POLICY, MarketV1.US_NYSE_NASDAQ,
        completed, QUANT_RECURRING_UNIVERSE_POLICY_VERSION)


def _journal(config: Config, value: dict) -> None:
    value["updated_at"] = datetime.now(timezone.utc).isoformat()
    _replace(config.journal, json.dumps(value, sort_keys=True,
             separators=(",", ":")).encode() + b"\n", 0o600)


def transition(config: Config, commit: str, lease, *, crash_after=0, observer=None):
    if lease is None or commit not in (MARKET_COMMIT, QUANT_COMMIT):
        raise Refusal("live cadence lease and reviewed commit required")
    current = {pin.path: (config.repo / pin.path).read_bytes() for pin in config.files}
    target = {pin.path: git(["cat-file", "blob", f"{commit}:{pin.path}"])
              for pin in config.files}
    record = {"schema": 1, "state": "TRANSITIONING", "target_commit": commit,
              "market_commit": MARKET_COMMIT, "quant_commit": QUANT_COMMIT,
              "observed_head": config.observed_head,
              "files": [{"path": pin.path, "mode": stat.S_IMODE((config.repo / pin.path).stat().st_mode),
                         "market": base64.b64encode(git(["cat-file", "blob", f"{MARKET_COMMIT}:{pin.path}"])).decode(),
                         "before_sha256": _sha(current[pin.path]),
                         "after_sha256": _sha(target[pin.path]), "progress": "PENDING"}
                        for pin in config.files]}
    _journal(config, record)
    for index, item in enumerate(record["files"]):
        item["progress"] = "REPLACING"
        _journal(config, record)
        _replace(config.repo / item["path"], target[item["path"]], item["mode"])
        item["progress"] = "REPLACED"
        _journal(config, record)
        if observer:
            observer()
        if crash_after == index + 1:
            raise SimulatedCrash(f"after file {crash_after}")
    record["state"] = "QUANT_ACTIVE" if commit == QUANT_COMMIT else "MARKET_RESTORED"
    _journal(config, record)


def recover(config: Config) -> str:
    with acquire(config.supervisor_lock):
        if not config.journal.exists():
            return "nothing_to_recover"
        try:
            if (json.loads(config.journal.read_text()).get("state") == "MARKET_RESTORED"
                    and all(_sha((config.repo / pin.path).read_bytes())
                            == pin.market_sha256 for pin in config.files)):
                return "already_restored"
        except Exception:
            pass
        try:
            context = acquire(config.cadence_lock)
            context.__enter__()
        except Refusal as error:
            raise Refusal("cadence lease unavailable") from error
        try:
            _restore_journal(config)
        finally:
            context.__exit__(None, None, None)
        return "restored"


def _restore_journal(config):
    try:
        record = json.loads(config.journal.read_text())
        if record.get("schema") != 1 or len(record.get("files", ())) != 3:
            raise ValueError
    except Exception:
        record = {"schema": 1, "state": "RECOVERING", "observed_head": config.observed_head,
                  "files": [{"path": pin.path,
                             "mode": stat.S_IMODE((config.repo / pin.path).stat().st_mode),
                             "progress": "PENDING"} for pin in config.files]}
    for item in record["files"]:
        pin = next(pin for pin in config.files if pin.path == item["path"])
        try:
            raw = base64.b64decode(item["market"], validate=True)
        except Exception:
            raw = git(["cat-file", "blob", f"{MARKET_COMMIT}:{pin.path}"])
        if _sha(raw) != pin.market_sha256:
            raw = git(["cat-file", "blob", f"{MARKET_COMMIT}:{pin.path}"])
        if _sha(raw) != pin.market_sha256:
            raise Refusal("Market commit digest mismatch")
        item["progress"] = "RESTORING"
        _journal(config, record)
        _replace(config.repo / item["path"], raw, item["mode"])
        item["progress"] = "RESTORED"
        _journal(config, record)
    record["state"] = "MARKET_RESTORED"
    _journal(config, record)


def _persist_restore_failure(config, primary, recovery):
    try:
        record = json.loads(config.journal.read_text())
        record.update(state="RESTORE_FAILED", primary_type=type(primary).__name__,
                      recovery_type=type(recovery).__name__)
        _journal(config, record)
    except Exception:
        pass


def _journal_needs_restore(config):
    try:
        return json.loads(config.journal.read_text()).get("state") != "MARKET_RESTORED"
    except Exception:
        return True


@contextmanager
def _acquire_until(path: Path, deadline: float, *, monotonic=time.monotonic,
                   sleep=time.sleep):
    while True:
        try:
            context = acquire(path)
            lease = context.__enter__()
            break
        except Refusal:
            if monotonic() >= deadline:
                raise Refusal("cadence lease unavailable for restore")
            sleep(.25)
    try:
        yield lease
    finally:
        context.__exit__(None, None, None)


def slot_identity(slot: datetime) -> str:
    if slot.tzinfo is None or slot.utcoffset() is None:
        raise Refusal("slot must be aware")
    return iso(slot)


def slot_deadline(slot, *, now_utc, monotonic_now):
    wait = max(0.0, (slot - now_utc).total_seconds())
    return monotonic_now + wait + TERMINAL_ALLOWANCE_SECONDS


def _read_tick(config, policy, slot):
    if not config.receipt_db.is_file() or config.receipt_db.is_symlink():
        raise Refusal("receipt DB absent")
    connection = sqlite3.connect(f"file:{config.receipt_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        raw = connection.execute(
            "SELECT * FROM cadence_tick_receipts WHERE policy_version=? AND scheduled_for_utc=?",
            (policy, slot_identity(slot)),).fetchone()
    except sqlite3.Error as error:
        raise Refusal("receipt DB unavailable") from error
    finally:
        connection.close()
    if raw is None:
        return None
    row = dict(raw)
    for key in ("open_markets_json", "local_dates_json", "stages_json",
                "wire_counts_json", "calendar_attempts_json"):
        row[key.removesuffix("_json")] = (None if row[key] is None else
            canonical_decode(row[key].encode(), max_bytes=256 * 1024))
    for key in ("observed_start", "started_at", "finished_at"):
        row[key] = None if row[key] is None else parse_iso(row[key])
    return row


def _settled_prior(config, policy, slot):
    row = _read_tick(config, policy, slot)
    if (row is None or row["status"] != "COMPLETE" or row["finished_at"] is None
            or row["started_at"] > row["finished_at"]
            or row["observed_start"] > row["started_at"]):
        raise Refusal("prior natural tick not durably settled")
    return row


def _wait_prior(config, policy, slot, deadline, *, monotonic=time.monotonic,
                sleep=time.sleep):
    while monotonic() < deadline:
        row = _read_tick(config, policy, slot)
        if row is None or row["status"] == "STARTED":
            sleep(.25)
            continue
        return _settled_prior(config, policy, slot)
    raise Refusal("prior natural tick not settled by 05:04")


def _wait_tick(config, policy, slot, deadline, *, monotonic=time.monotonic,
               sleep=time.sleep):
    if _read_tick(config, policy, slot) is not None:
        raise Refusal("target receipt already exists")
    saw_started = False
    while monotonic() < deadline:
        row = _read_tick(config, policy, slot)
        if row is not None:
            saw_started |= row["status"] == "STARTED"
            if row["status"] in TERMINAL:
                if (not saw_started or row["status"] != "COMPLETE"
                        or row["observed_start"] > slot + timedelta(seconds=90)):
                    raise Refusal("tick lifecycle or terminal status invalid")
                return row
        sleep(.25)
    raise Refusal("tick terminal timeout")


def _quant_candle_starts(config, policy, slot):
    if not config.ops_db.is_file() or config.ops_db.is_symlink():
        raise Refusal("operations DB absent")
    connection = sqlite3.connect(f"file:{config.ops_db}?mode=ro", uri=True)
    try:
        return connection.execute(
            "SELECT COUNT(*) FROM cadence_wire_starts WHERE policy_version=? "
            "AND scheduled_for_utc=? AND native_identity LIKE 'candles:qt:%'",
            (policy, slot_identity(slot))).fetchone()[0]
    except sqlite3.Error as error:
        raise Refusal("operations DB unavailable") from error
    finally:
        connection.close()


def _membership_native_starts(config, policy, slot):
    connection = sqlite3.connect(f"file:{config.ops_db}?mode=ro", uri=True)
    try:
        return connection.execute(
            "SELECT COUNT(*) FROM cadence_wire_starts WHERE policy_version=? "
            "AND scheduled_for_utc=? AND native_identity LIKE 'membership:%'",
            (policy, slot_identity(slot))).fetchone()[0]
    except sqlite3.Error as error:
        raise Refusal("operations DB unavailable") from error
    finally:
        connection.close()


def _verify_zero_effects(row):
    operations = (row.get("wire_counts") or {}).get("operation_started", {})
    if any(count and name not in CADENCE_OPERATIONS
           for name, count in operations.items()):
        raise Refusal("unknown cadence operation")


def _read_quant(config, identity):
    if not config.quant_db.is_file() or config.quant_db.is_symlink():
        raise Refusal("Quant DB absent")
    connection = sqlite3.connect(f"file:{config.quant_db}?mode=ro", uri=True)
    try:
        due = connection.execute(
            "SELECT state FROM quant_recurring_due WHERE identity_digest=?",
            (identity.identity_digest,)).fetchone()
        result = connection.execute(
            "SELECT membership_starts,candle_page1_starts,candle_page2_starts,"
            "cohort_size,admissible_count FROM quant_recurring_result "
            "WHERE identity_digest=?", (identity.identity_digest,)).fetchone()
        return due, result
    except sqlite3.Error as error:
        raise Refusal("Quant DB unavailable") from error
    finally:
        connection.close()


def _validate_quant(config, identity, row, *, slot, replay=False):
    counts = row["wire_counts"] or {}
    groups, operations = counts.get("group_started", {}), counts.get("operation_started", {})
    stages = {item["stage"]: item for item in row["stages"] or []}
    _verify_zero_effects(row)
    due, result = _read_quant(config, identity)
    if due != ("SUCCEEDED",):
        raise Refusal("Quant due/result invalid")
    qt_starts = _quant_candle_starts(config, row["policy_version"], slot)
    if replay:
        if qt_starts != 0:
            raise Refusal("Quant candle replay was nonzero")
        if (groups.get("MEMBERSHIP", 0) != 0
                or _membership_native_starts(config, row["policy_version"], slot) != 0
                or stages.get("quant_membership", {}).get("reason") != REPLAY_REASON):
            raise Refusal("replay was not zero-wire ALREADY_TERMINAL")
        return
    membership = {"MEMBERSHIP_NASDAQ_LISTED": 1, "MEMBERSHIP_OTHER_LISTED": 1,
                  "MEMBERSHIP_SEC_REGISTRY": 1, "MEMBERSHIP_SEC_ISSUER": 4}
    if (groups.get("MEMBERSHIP") != 7
            or any(operations.get(key) != count for key, count in membership.items())
            or not 4 <= qt_starts <= QUANT_CANDLE_MAX_PAGES * 4):
        raise Refusal("Quant wire counts invalid")
    if counts.get("state_counts", {}).get("STARTED", 0) != 0:
        raise Refusal("wire STARTED remains")
    if due != ("SUCCEEDED",) or result is None or result[0] != 7 or result[3:] != (4, 4) or not 4 <= result[1] + result[2] <= 8:
        raise Refusal("Quant due/result invalid")


def _validate_market(config, row, *, slot):
    groups = (row["wire_counts"] or {}).get("group_started", {})
    stages = {item["stage"]: item for item in row["stages"] or []}
    _verify_zero_effects(row)
    if (row["policy_version"] != MARKET_POLICY
            or groups.get("MEMBERSHIP", 0) != 0
            or _quant_candle_starts(config, MARKET_POLICY, slot) != 0
            or stages.get("quant_membership", {}).get("reason") != "policy_not_pinned"
            or stages.get("quant_shadow", {}).get("reason") != "policy_not_pinned"):
        raise Refusal("Market readback contains Quant effect")


def run(config: Config, *, now=lambda: datetime.now(timezone.utc),
        monotonic=time.monotonic, sleep=time.sleep) -> None:
    with acquire(config.supervisor_lock):
        started_now = now()
        identity = preflight_session(config, started_now)
        slots = PLAN.slots
        prior_cutoff = PLAN.prior_cutoff_utc
        _wait_prior(config, MARKET_POLICY, slots[0], monotonic() + max(
            0.0, (prior_cutoff - started_now).total_seconds()),
            monotonic=monotonic, sleep=sleep)
        swapped = False
        failure = None
        try:
            with acquire(config.cadence_lock) as lease:
                transition(config, QUANT_COMMIT, lease)
            swapped = True
            canary = _wait_tick(config, QUANT_POLICY, slots[1], slot_deadline(
                slots[1], now_utc=now(), monotonic_now=monotonic()),
                monotonic=monotonic, sleep=sleep)
            _validate_quant(config, identity, canary, slot=slots[1])
            replay = _wait_tick(config, QUANT_POLICY, slots[2], slot_deadline(
                slots[2], now_utc=now(), monotonic_now=monotonic()),
                monotonic=monotonic, sleep=sleep)
            _validate_quant(config, identity, replay, slot=slots[2], replay=True)
        except Exception as error:
            failure = error
        finally:
            try:
                if config.journal.exists() and _journal_needs_restore(config):
                    with _acquire_until(config.cadence_lock,
                                        monotonic() + 300,
                                        monotonic=monotonic,
                                        sleep=sleep) as lease:
                        _restore_journal(config)
            except Exception as restore_error:
                if failure is not None:
                    _persist_restore_failure(config, failure, restore_error)
                    raise RunAndRecoveryFailure(
                        f"primary={type(failure).__name__};restore="
                        f"{type(restore_error).__name__}") from restore_error
                raise
        if swapped:
            try:
                market = _wait_tick(config, MARKET_POLICY, slots[3], slot_deadline(
                    slots[3], now_utc=now(), monotonic_now=monotonic()),
                    monotonic=monotonic, sleep=sleep)
                _validate_market(config, market, slot=slots[3])
            except Exception as market_error:
                if failure is not None:
                    raise RunAndRecoveryFailure(
                        f"primary={type(failure).__name__};market="
                        f"{type(market_error).__name__}") from market_error
                raise
        if failure is not None:
            raise failure


def doctor(arm: Path) -> dict:
    config = load_and_validate(arm)
    identity = preflight_session(config, PLAN.run_at_utc)
    return {"state": "ready", "identity_digest": identity.identity_digest,
            "observed_head": config.observed_head}


def install_bundle(arm: Path, destination: Path, python: Path, tool: Path):
    destination, python, tool = map(Path, (destination, python, tool))
    if any(not path.is_absolute() for path in (destination, python, tool, Path(arm))):
        raise Refusal("plist paths must be absolute")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(mode=0o700, exist_ok=True)
    specs = (("primary", "run", 5, 2), ("recovery-0540", "recover", 5, 40),
             ("recovery-0630", "recover", 6, 30))
    paths = []
    for suffix, command, hour, minute in specs:
        payload = {"Label": f"com.woongs.tokmo.quant-w4-supervisor.{suffix}",
                   "ProgramArguments": [str(python), str(tool), command,
                                        "--arm", str(arm)],
                   "RunAtLoad": False, "KeepAlive": False,
                   "StartCalendarInterval": {
                       "Year": PLAN.target_kst_date.year,
                       "Month": PLAN.target_kst_date.month,
                       "Day": PLAN.target_kst_date.day,
                       "Hour": hour,
                       "Minute": minute,
                   }}
        path = destination / f"quant-w4-{suffix}.plist"
        _replace(path, plistlib.dumps(payload, fmt=plistlib.FMT_XML), 0o600)
        paths.append(path)
    return tuple(paths)


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "run", "recover"):
        command = sub.add_parser(name)
        command.add_argument("--arm", type=Path, required=True)
    install = sub.add_parser("install")
    install.add_argument("--arm", type=Path, required=True)
    install.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "doctor":
        print(json.dumps(doctor(args.arm), sort_keys=True)); return 0
    if args.command == "recover":
        config = load_and_validate(args.arm, recovery=True)
        print(recover(config)); return 0
    config = load_and_validate(args.arm)
    if args.command == "install":
        install_bundle(args.arm.resolve(), args.destination.resolve(),
                       config.python, Path(__file__).resolve()); return 0
    run(config); return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Refusal as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
