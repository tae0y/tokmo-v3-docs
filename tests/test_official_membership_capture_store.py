import os
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256

import pytest


def test_capture_store_material_zero_success_exact_bytes_and_private_modes(tmp_path):
    from tokmo.records.official_membership_capture_store import (
        OfficialMembershipCaptureStore,
    )

    path = (
        tmp_path / "official-membership" / "documentation-captures" / "capture.sqlite3"
    )
    body = b"<html><table><tr><td>3571</td></tr></table></html>"
    store = OfficialMembershipCaptureStore(path)
    receipt = store.append_success(
        body=body, captured_at=datetime(2026, 8, 13, tzinfo=UTC)
    )
    read = store.read(receipt.capture_id)
    assert read.body == body
    assert read.body_sha256 == sha256(body).hexdigest()
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert receipt.status == 200
    assert receipt.body_byte_count == len(body)


def test_capture_duplicate_is_typed_idempotent_and_conflict_is_closed(tmp_path):
    from tokmo.records.official_membership_capture_store import (
        OfficialMembershipCaptureStore,
    )

    path = (
        tmp_path / "official-membership" / "documentation-captures" / "capture.sqlite3"
    )
    store = OfficialMembershipCaptureStore(path)
    stamp = datetime(2026, 8, 13, tzinfo=UTC)
    first = store.append_success(
        capture_id="capture-1", body=b"official", captured_at=stamp, status=200
    )
    assert (
        store.append_success(
            capture_id="capture-1", body=b"official", captured_at=stamp, status=200
        )
        == first
    )
    with pytest.raises(ValueError, match="capture_conflict"):
        store.append_success(
            capture_id="capture-1", body=b"changed", captured_at=stamp, status=200
        )


def test_capture_store_rejects_forged_non_strict_schema(tmp_path):
    from tokmo.records.official_membership_capture_store import (
        OfficialMembershipCaptureStore,
    )

    path = (
        tmp_path / "official-membership" / "documentation-captures" / "capture.sqlite3"
    )
    path.parent.mkdir(parents=True, mode=0o700)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE captures(capture_id TEXT PRIMARY KEY, captured_at TEXT, body BLOB, body_sha256 TEXT)"
    )
    connection.commit()
    connection.close()
    os.chmod(path, 0o600)
    with pytest.raises(ValueError, match="schema_mismatch"):
        OfficialMembershipCaptureStore(path).read("capture-1")


def test_capture_store_rejects_hardlink(tmp_path):
    from tokmo.records.official_membership_capture_store import (
        OfficialMembershipCaptureStore,
    )

    path = (
        tmp_path / "official-membership" / "documentation-captures" / "capture.sqlite3"
    )
    store = OfficialMembershipCaptureStore(path)
    store.append_success(
        body=b"official", captured_at=datetime(2026, 8, 13, tzinfo=UTC)
    )
    hard = path.with_name("hard.sqlite3")
    os.link(path, hard)
    with pytest.raises(ValueError, match="private_path"):
        OfficialMembershipCaptureStore(hard).read("missing")
