#!/usr/bin/env python3
"""Durable alert gate for the five-minute Tokmo cadence wrapper."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

COOLDOWN_SECONDS = 3600


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("state_not_object")
    return value


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def record_failure(
    path: Path, *, fingerprint: str, detail: str, now: int,
    cooldown_seconds: int = COOLDOWN_SECONDS,
) -> tuple[bool, str]:
    previous = _read(path)
    same = previous.get("active") is True and previous.get("fingerprint") == fingerprint
    last_notified = previous.get("last_notified_at")
    within = same and isinstance(last_notified, int) and now - last_notified < cooldown_seconds
    count = int(previous.get("consecutive", 0)) + 1 if same else 1
    notified_at = last_notified if within else now
    _write(path, {
        "active": True,
        "consecutive": count,
        "fingerprint": fingerprint,
        "first_seen_at": previous.get("first_seen_at", now) if same else now,
        "last_notified_at": notified_at,
        "last_seen_at": now,
    })
    if within:
        return False, ""
    kind = "REMINDER" if same else "FAILED"
    return True, f"TOKMO_CADENCE_{kind}: {detail} consecutive={count}"


def record_success(path: Path, *, now: int) -> tuple[bool, str]:
    previous = _read(path)
    if previous.get("active") is not True:
        return False, ""
    count = int(previous.get("consecutive", 0))
    fingerprint = str(previous.get("fingerprint", "unknown"))
    _write(path, {
        "active": False,
        "last_recovered_at": now,
        "previous_consecutive": count,
        "previous_fingerprint": fingerprint,
    })
    return True, (
        "TOKMO_CADENCE_RECOVERED: "
        f"previous_fingerprint={fingerprint} consecutive={count}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--now", type=int, default=None)
    sub = parser.add_subparsers(dest="action", required=True)
    failed = sub.add_parser("failure")
    failed.add_argument("--fingerprint", required=True)
    failed.add_argument("--detail", required=True)
    sub.add_parser("success")
    args = parser.parse_args()
    now = int(time.time()) if args.now is None else args.now
    if args.action == "failure":
        emit, message = record_failure(
            args.state, fingerprint=args.fingerprint, detail=args.detail, now=now,
        )
    else:
        emit, message = record_success(args.state, now=now)
    if emit:
        print(message)
        return 10
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
