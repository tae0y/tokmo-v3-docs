"""Private operations storage paths."""

from pathlib import Path

from tokmo.values.material_pack import JobKind


def ops_data_root(home: Path) -> Path:
    return (Path(home) / ".local" / "share" / "tokmo-v3").resolve() / "ops"


def packs_dir(home: Path, job: JobKind) -> Path:
    return ops_data_root(home) / "packs" / job.value


def pack_path(home: Path, job: JobKind, tick_id: int) -> Path:
    return packs_dir(home, job) / f"{tick_id}.json"


def receipt_path(home: Path, job: JobKind) -> Path:
    return ops_data_root(home) / "receipts" / f"{job.value}.json"


def calendar_cache_path(home: Path) -> Path:
    return (
        (Path(home) / ".local" / "share" / "tokmo-v3").resolve()
        / "toss"
        / "market_calendar_cache.json"
    )


def current_holdings_path(home: Path) -> Path:
    return (
        (Path(home) / ".local" / "share" / "tokmo-v3").resolve()
        / "toss"
        / "current_holdings.json"
    )


def lock_path(home: Path, job: JobKind) -> Path:
    return ops_data_root(home) / "locks" / f"{job.value}.lock"


def confine_to_parent(path: Path, parent: Path) -> Path:
    resolved = path.resolve()
    resolved_parent = parent.resolve()
    if resolved == resolved_parent or not resolved.is_relative_to(resolved_parent):
        raise ValueError("path escapes owned parent")
    return resolved
