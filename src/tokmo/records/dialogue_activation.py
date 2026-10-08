"""Dialogue send-activation gate file (G3 activation SDS v0.2 §4.2).

The gate is a user-authored 0600 JSON file under the canonical runtime root:
``{"enabled": bool, "cutoff": ISO-8601 aware, "approved_by": "woong"}``.
Deployment alone never activates sending: the code default stays disabled
and only this file flips it. Every degenerate state — file absent, not a
regular file, unreadable, malformed JSON, ``enabled`` not exactly ``true``,
missing/naive ``cutoff``, empty ``approved_by`` — resolves to the legacy
disabled state (no send), never to an error: collection lanes are never
halted by this gate. The cutoff is the gate's FIXED approved instant; it
replaces the per-tick advancing scheduled-slot cutoff, which permanently
starved any row left unsent in its own tick (``created_at >= cutoff``).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import stat
from pathlib import Path

GATE_FILE_NAME = "dialogue_activation.json"


@dataclass(frozen=True)
class DialogueActivationV1:
    enabled: bool
    cutoff: "datetime | None"
    approved_by: "str | None"


_DISABLED = DialogueActivationV1(False, None, None)


def activation_from_payload(payload) -> DialogueActivationV1:
    """Pure payload judgment shared by the dialogue and NEWS gate readers;
    behavior-identical to the original inline checks."""
    if type(payload) is not dict or payload.get("enabled") is not True:
        return _DISABLED
    cutoff_raw = payload.get("cutoff")
    approved_by = payload.get("approved_by")
    if type(cutoff_raw) is not str or type(approved_by) is not str \
            or not approved_by:
        return _DISABLED
    try:
        cutoff = datetime.fromisoformat(cutoff_raw.replace("Z", "+00:00"))
    except ValueError:
        return _DISABLED
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        return _DISABLED
    return DialogueActivationV1(
        True, cutoff.astimezone(timezone.utc), approved_by)


def read_dialogue_activation(path: Path) -> DialogueActivationV1:
    """Resolve the gate file; every failure mode is the disabled state."""
    path = Path(path)
    try:
        value = os.lstat(path)
    except OSError:
        return _DISABLED
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        return _DISABLED
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _DISABLED
    return activation_from_payload(payload)
