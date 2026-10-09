"""Signed/hashed prelaunch PASS markers (Stage 4 D).

Hermes creates a marker after commit; the 48-hour live-shadow harness fails
closed unless the marker verifies against the exact expected commit SHA and
its canonical content hash — a marker is evidence, never a plain boolean.
The payload names the suite command, test count, creation instant, and
reviewer provenance; an optional HMAC-SHA256 signature binds the marker to
an environment-supplied key that is never stored in the marker itself.
"""

import hashlib
import hmac as hmac_module
import json
import os
import re
from datetime import datetime
from pathlib import Path

from tokmo.records.durability import fsync_directory
from tokmo.values.material_pack import canonical_encode

MARKER_SCHEMA = "tokmo-pass-marker-v1"
MARKER_KINDS = frozenset({"full-suite", "pilot-acceptance"})

_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


class MarkerError(ValueError):
    """Absent, corrupt, tampered, mismatched, or unsigned PASS marker."""


def _validated_payload(
    *, kind, commit_sha, command, test_count, created_at, provenance
) -> dict:
    if kind not in MARKER_KINDS:
        raise MarkerError("unknown marker kind")
    if type(commit_sha) is not str or not _COMMIT_SHA.fullmatch(commit_sha):
        raise MarkerError("commit sha must be the full 40-hex identity")
    if type(command) is not str or not command.strip():
        raise MarkerError("suite command required")
    if type(test_count) is not int or test_count < 1:
        raise MarkerError("test count must be a positive integer")
    if (
        not isinstance(created_at, datetime)
        or created_at.tzinfo is None
        or created_at.utcoffset() is None
    ):
        raise MarkerError("created_at must be an aware datetime")
    if type(provenance) is not str or not provenance.strip():
        raise MarkerError("provenance required")
    return {
        "schema": MARKER_SCHEMA,
        "kind": kind,
        "commit_sha": commit_sha,
        "command": command,
        "test_count": test_count,
        "created_at": created_at.isoformat(),
        "provenance": provenance,
    }


def _payload_digest(payload: dict) -> str:
    return hashlib.sha256(canonical_encode(payload)).hexdigest()


def create_pass_marker(
    path: Path,
    *,
    kind: str,
    commit_sha: str,
    command: str,
    test_count: int,
    created_at: datetime,
    provenance: str,
    hmac_key: bytes | None = None,
) -> dict:
    """Write one hashed (optionally HMAC-signed) PASS marker atomically."""
    payload = _validated_payload(
        kind=kind,
        commit_sha=commit_sha,
        command=command,
        test_count=test_count,
        created_at=created_at,
        provenance=provenance,
    )
    digest = _payload_digest(payload)
    marker = {"payload": payload, "sha256": digest}
    if hmac_key is not None:
        marker["hmac_sha256"] = hmac_module.new(
            hmac_key,
            digest.encode(),
            hashlib.sha256,
        ).hexdigest()
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.write(descriptor, json.dumps(marker, sort_keys=True, indent=1).encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    fsync_directory(path.parent)
    return marker


def verify_pass_marker(
    path: Path, *, kind: str, expected_commit: str, hmac_key: bytes | None = None
) -> dict:
    """Verify hash, kind, and exact commit; return the trusted payload."""
    if kind not in MARKER_KINDS:
        raise MarkerError("unknown marker kind")
    if type(expected_commit) is not str or not _COMMIT_SHA.fullmatch(expected_commit):
        raise MarkerError("expected commit must be the full 40-hex identity")
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise MarkerError("marker absent")
    try:
        marker = json.loads(path.read_text())
        payload = dict(marker["payload"])
        recorded_digest = marker["sha256"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise MarkerError("marker corrupt") from error
    revalidated = _validated_payload(
        kind=payload.get("kind"),
        commit_sha=payload.get("commit_sha"),
        command=payload.get("command"),
        test_count=payload.get("test_count"),
        created_at=_parse_created(payload.get("created_at")),
        provenance=payload.get("provenance"),
    )
    if payload != revalidated:
        raise MarkerError("marker payload is non-canonical")
    if _payload_digest(payload) != recorded_digest:
        raise MarkerError("marker hash mismatch")
    if hmac_key is not None:
        signature = marker.get("hmac_sha256")
        if type(signature) is not str:
            raise MarkerError("marker signature absent")
        expected_signature = hmac_module.new(
            hmac_key,
            recorded_digest.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac_module.compare_digest(signature, expected_signature):
            raise MarkerError("marker signature mismatch")
    if payload["kind"] != kind:
        raise MarkerError("marker kind mismatch")
    if payload["commit_sha"] != expected_commit:
        raise MarkerError("marker commit mismatch")
    return payload


def _parse_created(value) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as error:
        raise MarkerError("marker created_at invalid") from error
