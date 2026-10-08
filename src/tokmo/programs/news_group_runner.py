"""One scheduled NEWS group execution with durable duplicate rejection."""

from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from tokmo.programs.news_poll_manifest import plan_news_group
from tokmo.programs.news_poll_policy import news_tick_id
from tokmo.values.news_collection_diagnostics import collection_complete


@dataclass(frozen=True)
class GroupSourceResult:
    source: str
    status: str
    inserted: int
    duplicate: int
    conflict: int
    revision: int = 0
    http_status: int | None = None
    failure_class: str | None = None
    target: str | None = None
    diagnostics: dict | None = None


_SUCCESS_STATUSES = frozenset({"COLLECTED", "EMPTY"})

# Finnhub returns 503 under burst traffic; B's consecutive company calls and a
# catch-up C-then-B tick pair can hit it back-to-back. The pace marker lives in
# the shared runtime dir so the gap survives the process boundary; access is
# already serialized by the collection flock. Public sources are never paced.
FINNHUB_SOURCES = frozenset({"finnhub-company", "finnhub-market"})
FINNHUB_MIN_INTERVAL_SECONDS = 2.0
_FINNHUB_PACE_NAME = "tokmo-news-finnhub-pace.json"


def _last_finnhub_call(path: Path) -> float | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))["monotonic"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if type(value) is not float or not math.isfinite(value):
        return None
    return value


def _pace_finnhub(path: Path, *, clock, sleeper) -> None:
    last = _last_finnhub_call(path)
    if last is None:
        return
    remaining = FINNHUB_MIN_INTERVAL_SECONDS - (clock() - last)
    if remaining > 0:
        sleeper(min(remaining, FINNHUB_MIN_INTERVAL_SECONDS))


def sanitize_http_status(value) -> int | None:
    """Return a plain in-range HTTP status; anything else is a ValueError."""
    if value is None:
        return None
    if type(value) is not int or not 100 <= value <= 599:
        raise ValueError("http_status must be None or an int in 100..599")
    return value


def classify_failure(status: str, http_status: int | None) -> str | None:
    """Bounded failure class for durable receipts; carries no URL/query/token/body."""
    if status in _SUCCESS_STATUSES:
        return None
    if http_status is not None:
        return f"HTTP_{http_status}"
    return "NO_HTTP_STATUS"


@dataclass(frozen=True)
class GroupRunResult:
    tick_id: str
    status: str


def build_connector_argv(
        invocation, *, runner: Path, db: Path, ledger: Path,
        credentials: Path, campaign_id: str,
) -> list[str]:
    import sys
    argv = [
        sys.executable, str(runner), "--source", invocation.source,
        "--db", str(db), "--ledger", str(ledger),
        "--campaign-id", campaign_id, "--ceiling", "1",
    ]
    if invocation.source in {"naver", "finnhub-company", "finnhub-market"}:
        argv += ["--credentials", str(credentials)]
    if invocation.query is not None:
        argv += ["--query", invocation.query]
    if invocation.display is not None:
        argv += ["--display", str(invocation.display)]
    if invocation.symbol is not None:
        argv += ["--symbol", invocation.symbol]
    if invocation.from_date is not None:
        argv += ["--from-date", invocation.from_date]
    if invocation.to_date is not None:
        argv += ["--to-date", invocation.to_date]
    return argv


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _write_receipt(path: Path, payload: dict) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temp.exists():
            temp.unlink()


def _run_news_group_locked(group, scheduled_for, manifest, *, runtime_dir,
                           executor, clock, sleeper, fast_primary=False):
    tick_id = news_tick_id(group, scheduled_for, fast_primary=fast_primary)
    runtime_dir = Path(runtime_dir)
    markers, receipts = runtime_dir / "ticks", runtime_dir / "receipts"
    _private_dir(markers)
    _private_dir(receipts)
    marker = markers / f"{tick_id}.started"
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return GroupRunResult(tick_id, "DUPLICATE_TICK")
    else:
        os.close(fd)
    day = scheduled_for.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
    pace_path = runtime_dir / _FINNHUB_PACE_NAME
    results = []
    for invocation in plan_news_group(group, manifest, on_date=day):
        if fast_primary and invocation.source not in {"yna-market", "jtbc-breaking", "yna-industry"}:
            continue
        if invocation.source in FINNHUB_SOURCES:
            _pace_finnhub(pace_path, clock=clock, sleeper=sleeper)
        fingerprint = hashlib.sha256(repr(invocation).encode()).hexdigest()[:16]
        campaign_id = f"{tick_id}/{invocation.source}/{fingerprint}"
        results.append(executor(invocation, campaign_id))
        if invocation.source in FINNHUB_SOURCES:
            _write_receipt(pace_path, {"monotonic": clock()})
    # A conflict means one document did not match its connector's declared
    # revision policy. That is worth surfacing, but the fetch itself succeeded,
    # so it gets its own status rather than failing the whole tick.
    healthy = [collection_complete(asdict(result)) for result in results]
    conflicted = any(result.conflict for result in results)
    if all(healthy):
        status = "SUCCEEDED_WITH_CONFLICTS" if conflicted else "SUCCEEDED"
    elif not any(healthy):
        status = "FAILED"
    else:
        status = "PARTIAL_FAILURE"
    _write_receipt(receipts / f"{tick_id}.json", {
        "tick_id": tick_id, "status": status,
        "sources": [asdict(result) for result in results],
    })
    return GroupRunResult(tick_id, status)


def run_news_group(group, scheduled_for, manifest, *, runtime_dir, executor,
                   clock=time.monotonic, sleeper=time.sleep, fast_primary=False):
    runtime_dir = Path(runtime_dir)
    _private_dir(runtime_dir)
    # The full sweep can wait on slow secondary sources. Primary polling has
    # its own single-worker lock so that sweep cannot suppress urgent inputs.
    # Both paths still use the canonical source attempt ledger and news DB.
    lock_path = runtime_dir / ("tokmo-news-primary.lock" if fast_primary else "tokmo-news-collection.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return GroupRunResult(news_tick_id(group, scheduled_for, fast_primary=fast_primary),
                                  "SKIPPED_OVERLAP")
        return _run_news_group_locked(
            group, scheduled_for, manifest,
            runtime_dir=runtime_dir, executor=executor,
            clock=clock, sleeper=sleeper, fast_primary=fast_primary,
        )
