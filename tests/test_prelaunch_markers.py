"""Stage 4 D: signed/hashed prelaunch PASS markers.

Hermes creates the markers after commit; the 48-hour harness fails closed
unless both markers verify against the exact expected commit SHA and their
canonical content hashes — never a plain boolean flag. Markers carry the
suite command, test count, timestamp, and reviewer provenance, and support
an optional HMAC signature keyed from the environment.
"""

import json
from datetime import UTC, datetime

import pytest

from tokmo.records.prelaunch_markers import (
    MARKER_KINDS,
    MarkerError,
    create_pass_marker,
    verify_pass_marker,
)

COMMIT = "55b65f63359f0d3a97920ea794b9d5b60bc8003e"
CREATED = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def make(
    path,
    *,
    kind="full-suite",
    commit=COMMIT,
    hmac_key=None,
    provenance="hermes/full-suite",
    test_count=2466,
):
    return create_pass_marker(
        path,
        kind=kind,
        commit_sha=commit,
        command=".venv/bin/python -m pytest -q",
        test_count=test_count,
        created_at=CREATED,
        provenance=provenance,
        hmac_key=hmac_key,
    )


def test_marker_kinds_are_closed():
    assert MARKER_KINDS == frozenset({"full-suite", "pilot-acceptance"})


def test_create_then_verify_round_trip(tmp_path):
    path = tmp_path / "full_suite_pass.json"
    make(path)
    payload = verify_pass_marker(path, kind="full-suite", expected_commit=COMMIT)
    assert payload["commit_sha"] == COMMIT
    assert payload["test_count"] == 2466
    assert payload["command"].endswith("pytest -q")
    assert payload["provenance"] == "hermes/full-suite"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    marker = json.loads(path.read_text())
    assert len(marker["sha256"]) == 64  # content hash, not a boolean


def test_review_marker_carries_reviewer_provenance(tmp_path):
    path = tmp_path / "independent_review_pass.json"
    make(path, kind="pilot-acceptance", provenance="direct-zai/glm-5.2")
    payload = verify_pass_marker(
        path,
        kind="pilot-acceptance",
        expected_commit=COMMIT,
    )
    assert payload["provenance"] == "direct-zai/glm-5.2"


def test_absent_marker_fails_closed(tmp_path):
    with pytest.raises(MarkerError):
        verify_pass_marker(
            tmp_path / "missing.json",
            kind="full-suite",
            expected_commit=COMMIT,
        )


def test_commit_mismatch_fails_closed(tmp_path):
    path = tmp_path / "marker.json"
    make(path)
    with pytest.raises(MarkerError):
        verify_pass_marker(
            path,
            kind="full-suite",
            expected_commit="f" * 40,
        )


def test_kind_mismatch_fails_closed(tmp_path):
    path = tmp_path / "marker.json"
    make(path)
    with pytest.raises(MarkerError):
        verify_pass_marker(
            path,
            kind="pilot-acceptance",
            expected_commit=COMMIT,
        )


def test_tampered_payload_fails_the_hash(tmp_path):
    path = tmp_path / "marker.json"
    make(path)
    marker = json.loads(path.read_text())
    marker["payload"]["test_count"] = 99999
    path.write_text(json.dumps(marker))
    with pytest.raises(MarkerError):
        verify_pass_marker(path, kind="full-suite", expected_commit=COMMIT)


def test_corrupt_marker_fails_closed(tmp_path):
    path = tmp_path / "marker.json"
    path.write_text("not json at all")
    with pytest.raises(MarkerError):
        verify_pass_marker(path, kind="full-suite", expected_commit=COMMIT)


def test_hmac_signature_verifies_and_rejects_wrong_key(tmp_path):
    path = tmp_path / "marker.json"
    make(path, hmac_key=b"stage4-signing-key")
    payload = verify_pass_marker(
        path,
        kind="full-suite",
        expected_commit=COMMIT,
        hmac_key=b"stage4-signing-key",
    )
    assert payload["commit_sha"] == COMMIT
    with pytest.raises(MarkerError):
        verify_pass_marker(
            path,
            kind="full-suite",
            expected_commit=COMMIT,
            hmac_key=b"wrong-key",
        )
    # a key-expecting verifier refuses an unsigned marker
    unsigned = tmp_path / "unsigned.json"
    make(unsigned)
    with pytest.raises(MarkerError):
        verify_pass_marker(
            unsigned,
            kind="full-suite",
            expected_commit=COMMIT,
            hmac_key=b"stage4-signing-key",
        )


def test_invalid_creation_inputs_are_refused(tmp_path):
    with pytest.raises(MarkerError):
        make(tmp_path / "m.json", kind="vibes")
    with pytest.raises(MarkerError):
        make(tmp_path / "m.json", commit="short-sha")
    with pytest.raises(MarkerError):
        make(tmp_path / "m.json", test_count=0)
