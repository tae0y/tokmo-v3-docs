"""Read-only exact activation source; absence or invalidity is OFF."""

import json
from datetime import UTC, datetime
from pathlib import Path

_OFF = {"KR": "OFF", "US": "OFF"}
_MODES = frozenset({"OFF", "SHADOW", "LIVE_NO_SEND"})


def read_activation(path: Path, *, now: datetime) -> dict[str, str]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now")
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 4096:
            return dict(_OFF)
        value = json.loads(raw)
        if set(value) != {"schema_version", "expires_at", "markets"}:
            return dict(_OFF)
        if value["schema_version"] != 1 or set(value["markets"]) != {"KR", "US"}:
            return dict(_OFF)
        expires = datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
        if expires.astimezone(UTC) <= now.astimezone(UTC):
            return dict(_OFF)
        result = {market: value["markets"][market] for market in ("KR", "US")}
        return result if all(mode in _MODES for mode in result.values()) else dict(_OFF)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return dict(_OFF)
