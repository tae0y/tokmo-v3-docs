"""Strict, Git-tracked user watchlist registries.

Watchlists express user tracking intent only.  They never carry account,
quantity, cost basis, current price, or holding-state facts.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from tokmo.values.session_gate import Market

_TOP_KEYS = frozenset({"schema_version", "market", "entries"})
_ACTIVE_KEYS = frozenset({"symbol", "name", "status", "added_at", "source"})
_RETIRED_KEYS = _ACTIVE_KEYS | frozenset(
    {"retired_at", "retire_reason", "superseded_by"}
)
_KR_SYMBOL = re.compile(r"^[0-9A-Z]{6}$")
_US_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,31}$")


class WatchlistError(ValueError):
    pass


def _aware_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def validate_watchlist_file(
    payload: object, *, expected_market: str
) -> tuple[dict, ...]:
    if not isinstance(payload, dict) or set(payload) != _TOP_KEYS:
        raise WatchlistError("invalid_top_level")
    if payload["schema_version"] != 1 or payload["market"] != expected_market:
        raise WatchlistError("invalid_identity")
    entries = payload["entries"]
    if not isinstance(entries, list):
        raise WatchlistError("invalid_entries")
    symbol_pattern = _KR_SYMBOL if expected_market == "KR" else _US_SYMBOL
    seen: set[str] = set()
    validated: list[dict] = []
    for raw in entries:
        if not isinstance(raw, dict):
            raise WatchlistError("invalid_entry")
        status = raw.get("status")
        allowed = _ACTIVE_KEYS if status == "active" else _RETIRED_KEYS
        if status not in {"active", "retired"} or not set(raw) <= allowed:
            raise WatchlistError("invalid_entry_fields")
        if not _ACTIVE_KEYS <= set(raw):
            raise WatchlistError("missing_entry_fields")
        symbol = raw["symbol"]
        if not isinstance(symbol, str) or not symbol_pattern.fullmatch(symbol):
            raise WatchlistError("invalid_symbol")
        if symbol in seen:
            raise WatchlistError("duplicate_symbol")
        seen.add(symbol)
        if not isinstance(raw["name"], str) or not raw["name"].strip():
            raise WatchlistError("invalid_name")
        if raw["source"] != "user" or not _aware_timestamp(raw["added_at"]):
            raise WatchlistError("invalid_provenance")
        if status == "retired":
            if not {"retired_at", "retire_reason"} <= set(raw):
                raise WatchlistError("missing_retirement")
            if (
                not _aware_timestamp(raw["retired_at"])
                or not isinstance(raw["retire_reason"], str)
                or not raw["retire_reason"].strip()
            ):
                raise WatchlistError("invalid_retirement")
            superseded = raw.get("superseded_by")
            if superseded is not None and (
                not isinstance(superseded, str)
                or not symbol_pattern.fullmatch(superseded)
            ):
                raise WatchlistError("invalid_superseded_by")
        elif set(raw) != _ACTIVE_KEYS:
            raise WatchlistError("active_has_lifecycle_fields")
        validated.append(dict(raw))
    return tuple(sorted(validated, key=lambda row: row["symbol"]))


def write_watchlist_file(
    path: Path,
    payload: object,
    *,
    expected_market: str,
) -> None:
    """Validate and atomically persist a canonically ordered registry."""
    destination = Path(path)
    entries = validate_watchlist_file(payload, expected_market=expected_market)
    normalized = {
        "schema_version": 1,
        "market": expected_market,
        "entries": list(entries),
    }
    encoded = (json.dumps(normalized, ensure_ascii=False, indent=2) + "\n").encode()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise WatchlistError("registry_unavailable") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read(path: Path, market: str) -> tuple[dict, ...]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise WatchlistError("registry_unavailable") from error
    return validate_watchlist_file(payload, expected_market=market)


def load_watchlists(directory: Path) -> dict[Market, frozenset[str]]:
    root = Path(directory)
    result: dict[Market, frozenset[str]] = {}
    for market in Market:
        entries = _read(root / f"{market.value}.json", market.value)
        result[market] = frozenset(
            row["symbol"] for row in entries if row["status"] == "active"
        )
    return result


def load_watchlist_identities(
    directory: Path,
) -> dict[Market, tuple[tuple[str, str], ...]]:
    """Active (symbol, name) identity pairs per market — the same strict
    read as ``load_watchlists``; any malformed registry raises
    ``WatchlistError`` (callers fail closed, never guess)."""
    root = Path(directory)
    result: dict[Market, tuple[tuple[str, str], ...]] = {}
    for market in Market:
        entries = _read(root / f"{market.value}.json", market.value)
        result[market] = tuple(
            (row["symbol"], row["name"]) for row in entries if row["status"] == "active"
        )
    return result
