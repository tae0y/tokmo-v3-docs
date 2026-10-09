"""Structured JSONL logger with bounded DEBUG flight recorder.

INFO and above persist to ``events.jsonl``. DEBUG stays in a fixed-size
memory buffer until an ERROR (→ ``incident.jsonl``) or an explicit RCA
request (→ ``rca.jsonl``) flushes the pre-failure context.

Events are restricted to the contract field schema so that URLs, payloads,
and secrets cannot enter any log artifact through an ad-hoc field.
"""

import json
import os
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

Clock = Callable[[], datetime]

EVENT_FIELDS = frozenset(
    {"run_id", "stage", "action", "reason", "source", "decision_id"}
)
DEFAULT_FLIGHT_RECORDER_CAPACITY = 200

# Conservative deterministic secrecy policy: any field value containing one of
# these case-insensitive markers (URL schemes, query-string syntax, credential
# vocabulary) is rejected before it can reach buffer or disk. Rejection never
# echoes the value itself.
PROHIBITED_VALUE_MARKERS = (
    "://",
    "http",
    "www.",
    "?",
    "&",
    "=",
    "%",
    "api_key",
    "apikey",
    "token",
    "secret",
    "passw",
    "bearer",
    "credential",
    "authorization",
)


class TracerLogger:
    def __init__(
        self,
        root: Path,
        *,
        clock: Clock,
        capacity: int = DEFAULT_FLIGHT_RECORDER_CAPACITY,
    ) -> None:
        self.root = root
        self.clock = clock
        self.buffer: deque[dict] = deque(maxlen=capacity)

    def debug(self, **fields) -> None:
        self.buffer.append(self._event("DEBUG", fields))

    def info(self, **fields) -> None:
        self._append("events.jsonl", self._event("INFO", fields))

    def error(self, **fields) -> None:
        event = self._event("ERROR", fields)
        self._append("events.jsonl", event)
        self._flush("incident.jsonl", trigger=event)

    def flush_rca(self, run_id: str) -> None:
        trigger = self._event(
            "INFO",
            {
                "run_id": run_id,
                "stage": "rca",
                "action": "FLUSH",
                "reason": "explicit_rca",
                "source": "tracer",
                "decision_id": None,
            },
        )
        self._flush("rca.jsonl", trigger=trigger)

    def _event(self, level: str, fields: dict) -> dict:
        if set(fields) != EVENT_FIELDS:
            missing = sorted(EVENT_FIELDS - set(fields))
            unexpected = sorted(set(fields) - EVENT_FIELDS)
            raise ValueError(
                f"log_fields_outside_contract:missing={missing}:unexpected={unexpected}"
            )
        for name, value in fields.items():
            _reject_prohibited_value(name, value)
        return {"timestamp": self.clock().isoformat(), "level": level, **fields}

    def _flush(self, name: str, *, trigger: dict) -> None:
        """Persist and drain each event in order.

        If an append fails, only the unpersisted suffix remains for retry, so
        an already durable prefix cannot be duplicated.
        """
        while self.buffer:
            self._append(name, self.buffer[0])
            self.buffer.popleft()
        self._append(name, trigger)

    def _append(self, name: str, event: dict) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        line = json.dumps(event, separators=(",", ":")) + "\n"
        path = self.root / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)


def _reject_prohibited_value(field: str, value) -> None:
    """Raise without storing or echoing the offending value."""
    if value is None and field == "decision_id":
        return
    if not isinstance(value, str) or not value:
        raise ValueError(f"log_value_not_a_string:{field}")
    lowered = value.lower()
    for marker in PROHIBITED_VALUE_MARKERS:
        if marker in lowered:
            raise ValueError(f"log_value_prohibited:{field}")
