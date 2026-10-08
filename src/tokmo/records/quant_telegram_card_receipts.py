"""Fail-closed activation gate and one-attempt receipt for the Quant
Telegram decision-card canary.

Gate: a mode-0600 JSON file with an exact key set; absence, invalidity,
expiry, or any parse error resolves to OFF. Receipt: one JSON file per
activation id under a mode-0700 directory. The reservation is persisted
BEFORE any transport start and is never deleted; SENT, FAILED, UNKNOWN,
and an unsettled RESERVED row (crash after a possible send) all keep the
one-attempt cap consumed. No SQLite schema or migration is introduced.
"""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re

from tokmo.records.atomic_json import read_atomic_json, write_atomic_json
from tokmo.records.durability import fsync_directory

GATE_KEYS = frozenset({"schema_version", "activation_id", "mode", "expires_at", "markets"})
GATE_MODES = frozenset({"OFF", "CANARY"})
PART_STATUSES = frozenset({"RESERVED", "SENT", "FAILED", "UNKNOWN", "SKIPPED"})
ACTIVATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{3,63}$")
_MAX_GATE_BYTES = 4096
_MAX_RECEIPT_BYTES = 65536
# finite-canary analysis receipt: ≤4 validated analyses (bounded statements
# and labels only) plus digests/counts — never a prompt or raw model output
_MAX_ANALYSIS_RECEIPT_BYTES = 262144
_OFF = {"mode": "OFF", "activation_id": None, "expires_at": None}


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be aware")
    return value.astimezone(timezone.utc)


def valid_activation_id(value) -> bool:
    return type(value) is str and ACTIVATION_ID_RE.fullmatch(value) is not None


def read_card_activation(path: Path, *, now: datetime) -> dict:
    """Exact-shape gate read; anything unexpected is OFF with a reason."""
    now_utc = _aware(now)
    try:
        raw = Path(path).read_bytes()
        if len(raw) > _MAX_GATE_BYTES:
            return dict(_OFF, reason="gate_too_large")
        value = json.loads(raw)
        if type(value) is not dict or set(value) != GATE_KEYS:
            return dict(_OFF, reason="gate_shape")
        if value["schema_version"] != 1 or value["mode"] not in GATE_MODES:
            return dict(_OFF, reason="gate_shape")
        if not valid_activation_id(value["activation_id"]):
            return dict(_OFF, reason="gate_activation_id")
        if value["markets"] != ["KR", "US"]:
            return dict(_OFF, reason="gate_markets")
        expires = datetime.fromisoformat(str(value["expires_at"]).replace("Z", "+00:00"))
        if expires.tzinfo is None or expires.astimezone(timezone.utc) <= now_utc:
            return dict(_OFF, reason="gate_expired")
        if value["mode"] == "OFF":
            return dict(_OFF, reason="gate_off")
        return {"mode": "CANARY", "activation_id": value["activation_id"],
                "expires_at": expires.astimezone(timezone.utc).isoformat(), "reason": None}
    except FileNotFoundError:
        return dict(_OFF, reason="gate_absent")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return dict(_OFF, reason="gate_invalid")


def write_card_activation(path: Path, *, activation_id: str, mode: str,
                          expires_at: datetime) -> None:
    """Operator-only gate write (arm/disable); atomic, mode 0600."""
    if not valid_activation_id(activation_id) or mode not in GATE_MODES:
        raise ValueError("invalid gate parameters")
    payload = {"schema_version": 1, "activation_id": activation_id, "mode": mode,
               "expires_at": _aware(expires_at).isoformat(), "markets": ["KR", "US"]}
    write_atomic_json(Path(path), json.dumps(payload, sort_keys=True,
                                             separators=(",", ":")).encode(),
                      max_bytes=_MAX_GATE_BYTES)


class QuantTelegramCardReceiptStore:
    """One JSON receipt per activation id; reservation precedes transport."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def path(self, activation_id: str) -> Path:
        if not valid_activation_id(activation_id):
            raise ValueError("invalid activation id")
        return self.root / f"{activation_id}.receipt.json"

    def read(self, activation_id: str) -> dict | None:
        path = self.path(activation_id)
        if not path.exists() and not path.is_symlink():
            return None
        return json.loads(read_atomic_json(path, max_bytes=_MAX_RECEIPT_BYTES))

    def _write(self, activation_id: str, receipt: dict) -> None:
        write_atomic_json(self.path(activation_id),
                          json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode(),
                          max_bytes=_MAX_RECEIPT_BYTES)

    def reserve(self, activation_id: str, *, report_digests: dict, part_keys: tuple,
                now: datetime, deduped: dict | None = None) -> bool:
        """Persist the one-attempt reservation; False when already consumed.
        ``deduped`` records markets excluded because their report digest was
        already attempted under an earlier activation (audit only)."""
        if self.read(activation_id) is not None:
            return False
        if not part_keys or len(set(part_keys)) != len(part_keys):
            raise ValueError("part keys must be unique and non-empty")
        receipt = {
            "schema_version": 1, "activation_id": activation_id, "attempt_cap": 1,
            "reserved_at": _aware(now).isoformat(),
            "report_digests": {str(k): str(v) for k, v in sorted(report_digests.items())},
            "deduped": {str(k): str(v) for k, v in sorted((deduped or {}).items())},
            "parts": [{"key": key, "status": "RESERVED", "provider_message_id": None,
                       "settled_at": None, "reason": None} for key in part_keys],
            "status": "RESERVED",
        }
        self._write(activation_id, receipt)
        return True

    def acknowledge(self, activation_id: str, part_key: str, *, note: str, now: datetime) -> dict:
        """Operator acknowledgement (e.g. user confirmed an UNKNOWN part arrived).
        Audit-only: the machine status is never changed by an acknowledgement."""
        if type(note) is not str or not note.strip() or len(note) > 200:
            raise ValueError("invalid acknowledgement note")
        receipt = self.read(activation_id)
        if receipt is None:
            raise ValueError("reservation missing")
        matched = [part for part in receipt["parts"] if part["key"] == part_key]
        if len(matched) != 1:
            raise ValueError("unknown part")
        matched[0]["operator_ack"] = {"note": note.strip(), "at": _aware(now).isoformat()}
        self._write(activation_id, receipt)
        return receipt

    def settle(self, activation_id: str, part_key: str, status: str, *, now: datetime,
               provider_message_id: str | None = None, reason: str | None = None) -> dict:
        if status not in PART_STATUSES - {"RESERVED"}:
            raise ValueError("invalid settle status")
        receipt = self.read(activation_id)
        if receipt is None:
            raise ValueError("reservation missing")
        matched = [part for part in receipt["parts"] if part["key"] == part_key]
        if len(matched) != 1 or matched[0]["status"] != "RESERVED":
            raise ValueError("part not reserved")
        matched[0].update({"status": status, "settled_at": _aware(now).isoformat(),
                           "provider_message_id": provider_message_id, "reason": reason})
        statuses = {part["status"] for part in receipt["parts"]}
        if statuses == {"SENT"}:
            receipt["status"] = "SENT"
        elif "RESERVED" in statuses:
            receipt["status"] = "RESERVED"
        elif "UNKNOWN" in statuses:
            receipt["status"] = "UNKNOWN"
        elif "SENT" in statuses:
            receipt["status"] = "PARTIAL"
        else:
            receipt["status"] = "FAILED"
        self._write(activation_id, receipt)
        return receipt

    # --- finite-canary analysis receipt (2026-09-04): one sidecar per activation id --
    # ``<id>.analysis.json`` sits beside the send receipt and is written
    # BEFORE the first model call (reservation) and after every call; it is
    # never deleted, so a replay of the same activation id makes no call.
    # The ``*.receipt.json`` glob above never sees it.

    def analysis_path(self, activation_id: str) -> Path:
        if not valid_activation_id(activation_id):
            raise ValueError("invalid activation id")
        return self.root / f"{activation_id}.analysis.json"

    def read_analysis(self, activation_id: str) -> dict | None:
        path = self.analysis_path(activation_id)
        if not path.exists() and not path.is_symlink():
            return None
        return json.loads(read_atomic_json(path, max_bytes=_MAX_ANALYSIS_RECEIPT_BYTES))

    def analysis_lock_path(self, activation_id: str) -> Path:
        return self.analysis_path(activation_id).with_suffix(".lock")

    def _analysis_payload(self, activation_id: str, receipt) -> bytes:
        if type(receipt) is not dict or receipt.get("activation_id") != activation_id:
            raise ValueError("analysis receipt must carry its activation id")
        payload = json.dumps(receipt, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8")
        if len(payload) > _MAX_ANALYSIS_RECEIPT_BYTES:
            raise ValueError("payload exceeds max_bytes")
        return payload

    def reserve_analysis(self, activation_id: str, receipt: dict) -> bool:
        """Atomic exclusive publication of the reservation: the complete bytes
        are written to a private temp and hard-linked to the final name, so
        exactly one process ever wins for an activation id and no reader can
        observe a partial file. False when the id is already reserved (by
        anyone, with any content)."""
        payload = self._analysis_payload(activation_id, receipt)
        path = self.analysis_path(activation_id)
        temp = path.with_name(f".{path.name}.{os.urandom(16).hex()}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temp, flags, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(fd)
            try:
                os.link(temp, path)
            except FileExistsError:
                return False
            fsync_directory(self.root)
            return True
        finally:
            os.close(fd)
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def update_analysis(self, activation_id: str, mutate) -> dict:
        """ONE locked, durable read-modify-write: an exclusive flock on the
        id's sidecar lock serialises every contender (threads and processes)
        while the receipt is read, ``mutate(receipt) -> receipt`` is applied
        and the result is atomically replaced. ``mutate`` raising leaves the
        file untouched; a never-reserved id is refused without creating."""
        path = self.analysis_path(activation_id)
        if not path.exists():
            raise ValueError("reservation missing")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        lock = os.open(self.analysis_lock_path(activation_id), flags, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if not path.exists():
                    raise ValueError("reservation missing")
                updated = mutate(json.loads(read_atomic_json(path, max_bytes=_MAX_ANALYSIS_RECEIPT_BYTES)))
                write_atomic_json(path, self._analysis_payload(activation_id, updated),
                                  max_bytes=_MAX_ANALYSIS_RECEIPT_BYTES)
                return updated
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            os.close(lock)

    def write_analysis(self, activation_id: str, receipt: dict) -> None:
        """Unlocked whole-receipt replace (operator/test repair only); the
        canary itself reserves and updates through the two methods above."""
        write_atomic_json(self.analysis_path(activation_id), self._analysis_payload(activation_id, receipt),
                          max_bytes=_MAX_ANALYSIS_RECEIPT_BYTES)

    def observation_path(self, activation_id: str) -> Path:
        return self.analysis_path(activation_id).with_suffix('.private-observation.json')

    def observe_failure(self, activation_id: str, key: str, finding: dict) -> dict:
        """Controller-only sanitized finding; private contents never enter the receipt.
        Uses the existing activation lock; settled/historical receipts cannot be changed.
        Sidecar first, then digest link: a crash may leave an unlinked private sidecar.
        """
        def mutate(receipt):
            if receipt['status'] != 'RESERVED' or key not in receipt['cards']:
                raise ValueError('observation_not_reserved')
            path = self.observation_path(activation_id)
            payload = (json.loads(read_atomic_json(path, max_bytes=_MAX_ANALYSIS_RECEIPT_BYTES))
                       if path.exists() or path.is_symlink() else
                       {'schema': 'quant-finite-canary-private-observation-v1',
                        'activation_id': activation_id, 'failures': {}})
            if key in payload['failures']:
                raise ValueError('observation_already_recorded')
            payload['failures'][key] = finding
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
            write_atomic_json(path, raw, max_bytes=_MAX_ANALYSIS_RECEIPT_BYTES)
            return dict(receipt, observation={'schema': payload['schema'],
                        'sha256': hashlib.sha256(raw).hexdigest(), 'count': len(payload['failures']),
                        'failures': {card: {name: row[name] for name in
                            ('class', 'path', 'linkage', 'code_sha256', 'prompt_sha256', 'validator_sha256')}
                            for card, row in payload['failures'].items()}})
        return self.update_analysis(activation_id, mutate)

    def report_attempted(self, report_sha256: str) -> tuple[str, str] | None:
        """(activation_id, status) of a prior attempt whose transport may have
        started for this report digest, else None.

        Per-market truth: a digest is attempted only when a part of ITS market
        is RESERVED, SENT, UNKNOWN, or FAILED. A market whose parts are all
        SKIPPED never reached the transport and stays eligible. A digest bound
        with no parts at all is treated as RESERVED (fail-closed)."""
        worst_order = ("SENT", "FAILED", "UNKNOWN", "RESERVED")
        for path in sorted(self.root.glob("*.receipt.json")):
            try:
                receipt = json.loads(read_atomic_json(path, max_bytes=_MAX_RECEIPT_BYTES))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if type(receipt) is not dict or type(receipt.get("report_digests")) is not dict:
                continue
            for market, digest in receipt["report_digests"].items():
                if digest != report_sha256:
                    continue
                statuses = [part.get("status") for part in receipt.get("parts", ())
                            if type(part) is dict and str(part.get("key", "")).startswith(f"{market}:")]
                started = [status for status in statuses if status in worst_order]
                if not statuses:
                    return receipt.get("activation_id"), "RESERVED"
                if started:
                    return (receipt.get("activation_id"),
                            max(started, key=worst_order.index))
        return None
