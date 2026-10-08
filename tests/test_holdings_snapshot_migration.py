"""Slice 8: frozen legacy decoder and deterministic migration dry-run/clone.

The frozen decoder (`legacy_holdings_v1`) admits only the exact known legacy
Toss v1 envelope and refuses everything else fail-closed; admitted captures
map deterministically onto the pinned SDS §5.8/§10.1 golden vectors. The
offline tool decodes explicit caller-supplied paths only: dry-run creates
and mutates nothing, clone builds one separate candidate and proves parity
on read-only reopen while the source stays byte-and-mtime invariant.

All fixture values are synthetic. Marker states, promotion crash windows,
routing, source-change-under-lock refusal, rollback, and quarantine tests
are Slice 9 (`test_all_physical_marker_states`, `test_promotion_crash_windows`,
`test_routing_legacy_sqlite_staged_invalid`, `test_source_change_refusal`,
`test_rollback_before_promotion`, `test_rollback_after_promotion_before_marker`,
`test_whole_file_quarantine_simulation`).
"""

import ast
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokmo.records import holdings_snapshot_migration as generation_migration

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tokmo.records.holdings_persistence import (
    DenialToken,
    HoldingsPersistencePermitV1,
    MIGRATION_CRITERIA_VERSION,
    MIGRATION_PROGRAM_TOKEN,
    PurposeKind,
    authorize_legacy_migration,
    authorize_user_request,
)
from tokmo.records.holdings_snapshots import (
    HoldingsSnapshotStore,
    canonical_snapshot_bytes,
    legacy_snapshot_id,
    snapshot_content_hash,
)
from tokmo.records.legacy_holdings_v1 import (
    LEGACY_POLICY_VERSION,
    LEGACY_REFUSAL_REASONS,
    LEGACY_SOURCE,
    validate_legacy_json,
)
from tokmo.records.holdings_cutover import (
    CUTOVER_MARKER_BYTES,
    CutoverContext,
    FamilyLockTimeout,
    HoldingsRoute,
    acquire_family_lock,
    parse_marker_bytes,
    resolve_route,
    with_family_lock,
)
from tokmo.composition import LiveRunError, execute_holdings_route

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOSS_FIXTURE = FIXTURES / "holdings_legacy_toss_v1.json"
REFUSAL_FIXTURE = FIXTURES / "holdings_legacy_non_toss_refusal_v1.json"

# Pinned golden vectors (SDS §5.8/§10.1, computed with Python 3.12).
EXPECTED_LEGACY_DIGESTS = {
    "capture_1": "5cafa5c45986675183f3185088e415f6995b34ae68fbece140ed80ba9b351ccc",
    "capture_2": "cb4a61363326b55da3e593668b0dd4ab5468243123d87065a3bdc4b294493ba1",
}
EXPECTED_CONTENT_HASHES = {
    "capture_1": "6eb2d30a92f8d66cb9857f4b01adaa766562f9d543cd0b4cc78aa6ec2d3df6ec",
    "capture_2": "8b0c5b7afe0479ebf9722c877b879e380d686a4ee483f92c6d402b22cdd03806",
}


def toss_payload() -> dict:
    return json.loads(TOSS_FIXTURE.read_bytes())


def decode(payload: dict):
    return validate_legacy_json(
        json.dumps(payload, separators=(",", ":")).encode())


# --- Frozen decoder: exact two-capture admission ---


def test_exact_toss_two_capture_import():
    result = validate_legacy_json(TOSS_FIXTURE.read_bytes())
    assert result.status == "VALID"
    assert result.reason == ""
    assert len(result.captures) == 2

    first, second = result.captures
    assert first.captured_at < second.captured_at
    for capture in result.captures:
        assert capture.source == LEGACY_SOURCE
        assert capture.captured_at.utcoffset().total_seconds() == 0
        assert capture.discovered_account_count == 2

    assert first.snapshot_id == EXPECTED_LEGACY_DIGESTS["capture_1"]
    assert first.content_hash == EXPECTED_CONTENT_HASHES["capture_1"]
    assert second.snapshot_id == EXPECTED_LEGACY_DIGESTS["capture_2"]
    assert second.content_hash == EXPECTED_CONTENT_HASHES["capture_2"]

    # Capture 1 groups the whole capture including the empty account.
    assert [account.account_ordinal for account in first.accounts] == [1, 2]
    assert [item.symbol for item in first.accounts[0].holdings] == ["AAPL"]
    assert first.accounts[1].holdings == ()

    # Capture 2 holdings come back in canonical symbol order even though the
    # fixture stores GOOGL before AAPL.
    assert [item.symbol for item in second.accounts[0].holdings] == [
        "AAPL", "GOOGL"]
    assert [item.symbol for item in second.accounts[1].holdings] == ["MSFT"]


def test_legacy_id_stability():
    baseline = validate_legacy_json(TOSS_FIXTURE.read_bytes())
    again = validate_legacy_json(TOSS_FIXTURE.read_bytes())

    # Reformatting the surface JSON (indentation, object key order) must not
    # change the deterministic IDs; only the schema-relevant content counts.
    payload = toss_payload()
    reordered = {"records": [
        dict(reversed(list(record.items()))) for record in payload["records"]
    ]}
    reformatted = validate_legacy_json(
        json.dumps(reordered, indent=2).encode())

    for result in (again, reformatted):
        assert result.status == "VALID"
        assert [c.snapshot_id for c in result.captures] == [
            c.snapshot_id for c in baseline.captures]
        assert [c.content_hash for c in result.captures] == [
            c.content_hash for c in baseline.captures]


def test_canonical_parity_with_typed_store():
    """Decoder digests equal the shared §5 serializer over the typed values."""
    result = validate_legacy_json(TOSS_FIXTURE.read_bytes())
    assert result.status == "VALID"
    for capture in result.captures:
        typed_bytes = canonical_snapshot_bytes(capture)
        assert snapshot_content_hash(capture) == capture.content_hash
        assert legacy_snapshot_id(typed_bytes) == capture.snapshot_id


def test_empty_records_valid_zero_captures():
    result = decode({"records": []})
    assert result.status == "VALID"
    assert result.reason == ""
    assert result.captures == ()


# --- Frozen decoder: fail-closed refusal matrix ---


def mutate(transform):
    payload = toss_payload()
    transform(payload)
    return payload


def drop_key(record_index: int, key: str):
    def transform(payload):
        del payload["records"][record_index][key]
    return transform


def set_field(record_index: int, key: str, value):
    def transform(payload):
        payload["records"][record_index][key] = value
    return transform


def set_holding(record_index: int, holding_index: int, key: str, value):
    def transform(payload):
        payload["records"][record_index]["holdings"][holding_index][key] = value
    return transform


def test_non_toss_refusal():
    result = validate_legacy_json(REFUSAL_FIXTURE.read_bytes())
    assert result.status == "INVALID"
    assert result.reason == "non_toss_source"
    assert result.captures == ()


def test_mixed_non_toss_row_refused():
    payload = toss_payload()
    payload["records"].append(
        json.loads(REFUSAL_FIXTURE.read_bytes())["records"][0])
    result = decode(payload)
    assert result.status == "INVALID"
    assert result.reason == "non_toss_source"
    assert result.captures == ()


def test_incomplete_capture_refusal():
    payload = toss_payload()
    del payload["records"][1]  # capture 1 loses ordinal 2 of account_count 2
    result = decode(payload)
    assert result.status == "INVALID"
    assert result.reason == "capture_incomplete"
    assert result.captures == ()


@pytest.mark.parametrize("transform, reason", [
    # Envelope strictness.
    (lambda payload: payload.update(extra=1), "envelope_outside_contract"),
    (lambda payload: payload.update(records={}), "envelope_outside_contract"),
    # Record key-set/type strictness.
    (drop_key(0, "policy_version"), "record_outside_contract"),
    (set_field(0, "unexpected", "x"), "record_outside_contract"),
    (set_field(0, "account_ordinal", "1"), "record_outside_contract"),
    (set_field(0, "account_ordinal", True), "record_outside_contract"),
    (set_field(0, "account_ordinal", 0), "record_outside_contract"),
    (set_field(0, "account_ordinal", 3), "record_outside_contract"),
    (set_field(0, "account_count", 0), "record_outside_contract"),
    (set_field(0, "holdings", {}), "record_outside_contract"),
    (drop_key(2, "source"), "record_outside_contract"),
    (set_field(2, "source", 7), "record_outside_contract"),
    # Frozen policy/source tokens.
    (set_field(0, "policy_version", "toss1-v2"), "policy_outside_contract"),
    (set_field(3, "source", "toss-holdings-v2"), "non_toss_source"),
    # Record IDs.
    (set_field(0, "record_id", ""), "record_id_outside_contract"),
    (set_field(1, "record_id", "legacy-0001"), "record_id_outside_contract"),
    # Timestamps.
    (set_field(0, "captured_at", "2026-07-28T00:00:00"),
     "timestamp_outside_contract"),
    (set_field(0, "captured_at", "yesterday"), "timestamp_outside_contract"),
    (set_field(0, "stored_at", ""), "record_outside_contract"),
    # Holding semantics.
    (set_holding(0, 0, "quantity", "10.0"), "holding_outside_contract"),
    (set_holding(0, 0, "quantity", "-1"), "holding_outside_contract"),
    (set_holding(0, 0, "avg_price", "1E+2"), "holding_outside_contract"),
    (set_holding(0, 0, "symbol", ""), "holding_outside_contract"),
    (set_holding(0, 0, "market_raw", None), "holding_outside_contract"),
    (set_holding(0, 0, "name", 5), "holding_outside_contract"),
    (set_holding(0, 0, "extra", "x"), "holding_outside_contract"),
    (lambda payload: payload["records"][2]["holdings"].append(
        dict(payload["records"][2]["holdings"][0])),
     "holding_outside_contract"),
    (lambda payload: payload["records"][0]["holdings"][0].pop("asset_type"),
     "holding_outside_contract"),
    # Capture grouping conflicts.
    (set_field(1, "account_count", 3), "capture_conflict"),
    (set_field(1, "account_ordinal", 1), "capture_conflict"),
    (set_field(1, "captured_at", "2026-07-28T09:00:00+09:00"),
     "capture_conflict"),
])
def test_refusal_matrix(transform, reason):
    result = decode(mutate(transform))
    assert result.status == "INVALID"
    assert result.reason == reason
    assert result.captures == ()
    assert reason in LEGACY_REFUSAL_REASONS


def test_malformed_json_refused():
    for raw in (b"{", b"\xff\xfe", b"null", b"[]"):
        result = validate_legacy_json(raw)
        assert result.status == "INVALID"
        assert result.reason in {"malformed_json", "envelope_outside_contract"}
        assert result.captures == ()


def test_oversized_source_refused():
    filler = b'{"records": [' + b" " * (16 * 1024 * 1024) + b"]}"
    result = validate_legacy_json(filler)
    assert result.status == "INVALID"
    assert result.reason == "source_too_large"


def dup_variant(needle: str, replacement: str) -> bytes:
    """Fixture bytes with the first occurrence of ``needle`` textually
    replaced, used to inject duplicate object member names that no dict
    round-trip can express."""
    raw = TOSS_FIXTURE.read_bytes()
    assert needle.encode() in raw
    return raw.replace(needle.encode(), replacement.encode(), 1)


@pytest.mark.parametrize("raw_builder", [
    # Envelope-level duplicates: same and differing member values.
    lambda: b'{"records":[],"records":[]}',
    lambda: b'{"records":[],"records":[{"bogus":1}]}',
    # Record-level duplicates inside records[0].
    lambda: dup_variant(
        '"policy_version":"toss1-v1"',
        '"policy_version":"toss1-v1","policy_version":"toss1-v1"'),
    lambda: dup_variant(
        '"policy_version":"toss1-v1"',
        '"policy_version":"toss1-v1","policy_version":"toss1-v9"'),
    # Holding-level duplicates inside records[0].holdings[0].
    lambda: dup_variant(
        '"symbol":"AAPL"', '"symbol":"AAPL","symbol":"AAPL"'),
    lambda: dup_variant(
        '"symbol":"AAPL"', '"symbol":"AAPL","symbol":"ZZZZ"'),
], ids=["envelope_same", "envelope_differs", "record_same", "record_differs",
        "holding_same", "holding_differs"])
def test_duplicate_json_member_names_refused(raw_builder):
    """Duplicate object member names refuse fail-closed at every nesting
    level — even when the duplicate carries the identical value — with one
    closed token and no captured or leaked key/value."""
    result = validate_legacy_json(raw_builder())
    assert result.status == "INVALID"
    assert result.reason == "malformed_json"
    assert result.captures == ()
    rendered = repr(result)
    for leaked in ("records", "policy_version", "toss1-v9", "AAPL", "ZZZZ"):
        assert leaked not in rendered


# --- Migration authorization: authorize_legacy_migration only ---

CLOCK_NOW = datetime(2026, 7, 29, 0, 0, 0, tzinfo=timezone.utc)


def clock():
    return CLOCK_NOW


def decoded_captures():
    result = validate_legacy_json(TOSS_FIXTURE.read_bytes())
    assert result.status == "VALID"
    return result.captures


def private_store(tmp_path):
    leaf = tmp_path / "toss"
    leaf.mkdir(mode=0o700)
    return HoldingsSnapshotStore(leaf / "candidate.sqlite3", clock=clock)


def test_authorize_legacy_migration_permit_fields():
    capture = decoded_captures()[0]
    permit = authorize_legacy_migration(
        snapshot_id=capture.snapshot_id,
        canonical_bytes=canonical_snapshot_bytes(capture),
        clock=clock,
    )
    assert isinstance(permit, HoldingsPersistencePermitV1)
    assert permit.criteria_version == MIGRATION_CRITERIA_VERSION
    assert permit.purpose_kind is PurposeKind.APPROVED_MIGRATION
    assert permit.program_name == MIGRATION_PROGRAM_TOKEN
    assert permit.snapshot_id == capture.snapshot_id
    assert permit.authorized_at == CLOCK_NOW


def test_authorize_legacy_migration_recomputes_digest():
    """The caller's snapshot_id is only an equality check against the digest
    recomputed inside the factory; any mismatch denies."""
    capture = decoded_captures()[0]
    canonical = canonical_snapshot_bytes(capture)
    for wrong in ("f" * 64, decoded_captures()[1].snapshot_id,
                  uuid.uuid4().hex, ""):
        denial = authorize_legacy_migration(
            snapshot_id=wrong, canonical_bytes=canonical, clock=clock)
        assert isinstance(denial, DenialToken)
        assert denial.reason == "invalid_snapshot_id"


def test_import_legacy_accepts_migration_permit(tmp_path):
    store = private_store(tmp_path)
    for capture in decoded_captures():
        permit = authorize_legacy_migration(
            snapshot_id=capture.snapshot_id,
            canonical_bytes=canonical_snapshot_bytes(capture),
            clock=clock,
        )
        receipt = store.import_legacy(capture, permit)
        assert receipt.status == "SAVED"
    replay = decoded_captures()[0]
    permit = authorize_legacy_migration(
        snapshot_id=replay.snapshot_id,
        canonical_bytes=canonical_snapshot_bytes(replay),
        clock=clock,
    )
    assert store.import_legacy(replay, permit).status == "DUPLICATE"


def test_runtime_append_rejects_migration_permit(tmp_path):
    store = private_store(tmp_path)
    capture = decoded_captures()[0]
    permit = authorize_legacy_migration(
        snapshot_id=capture.snapshot_id,
        canonical_bytes=canonical_snapshot_bytes(capture),
        clock=clock,
    )
    receipt = store.append(capture, permit)
    assert receipt.status == "FAILED"
    assert receipt.reason == "invalid_permit"
    assert not store._path.exists()


def test_import_legacy_rejects_runtime_permit(tmp_path):
    store = private_store(tmp_path)
    capture = decoded_captures()[0]
    permit = authorize_user_request(snapshot_id=uuid.uuid4().hex, clock=clock)
    assert isinstance(permit, HoldingsPersistencePermitV1)
    receipt = store.import_legacy(capture, permit)
    assert receipt.status == "FAILED"
    assert receipt.reason == "invalid_permit"
    assert not store._path.exists()


def test_migration_permit_repr_closed():
    capture = decoded_captures()[0]
    permit = authorize_legacy_migration(
        snapshot_id=capture.snapshot_id,
        canonical_bytes=canonical_snapshot_bytes(capture),
        clock=clock,
    )
    rendered = repr(permit)
    assert capture.snapshot_id not in rendered
    assert "authorized_at=" not in rendered


def test_result_repr_closed():
    """No decoded value, symbol, ID, or hash renders through result repr."""
    result = validate_legacy_json(TOSS_FIXTURE.read_bytes())
    rendered = repr(result)
    for leaked in ("AAPL", "GOOGL", "MSFT", "150.5", "2800", "380.25",
                   "Apple", "Alphabet", "Microsoft",
                   EXPECTED_LEGACY_DIGESTS["capture_1"],
                   EXPECTED_CONTENT_HASHES["capture_1"]):
        assert leaked not in rendered
    assert LEGACY_POLICY_VERSION == "toss1-v1"


# --- Offline migration tool: dry-run creates and mutates nothing ---

from tools.migrate_holdings_factstore import (  # noqa: E402
    approve_cutover_build,
    execute_cutover,
    TOOL_REFUSAL_REASONS,
    format_report,
    main,
    run_clone,
    run_dry_run,
)

TOOL_SOURCE_PATH = ROOT / "tools" / "migrate_holdings_factstore.py"


def workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Private 0700 workspace: a copied source file and a candidate path."""
    source_dir = tmp_path / "legacy"
    source_dir.mkdir(mode=0o700)
    source = source_dir / "holdings_store.json"
    source.write_bytes(TOSS_FIXTURE.read_bytes())
    os.chmod(source, 0o600)
    candidate_dir = tmp_path / "rehearsal"
    candidate_dir.mkdir(mode=0o700)
    return source, candidate_dir / "holdings_candidate.sqlite3"


def cutover_workspace(tmp_path: Path):
    leaf = tmp_path / "toss"
    leaf.mkdir(mode=0o700)
    source = leaf / "holdings_store.json"
    source.write_bytes(TOSS_FIXTURE.read_bytes())
    os.chmod(source, 0o600)
    return (
        leaf,
        source,
        leaf / "holdings_snapshots.sqlite3",
        leaf / "holdings_cutover_v1.json",
        leaf / ".holdings_family.lock",
    )


def test_marker_parser_is_exact_two_key_contract():
    marker = parse_marker_bytes(CUTOVER_MARKER_BYTES)
    assert marker.state == "sqlite-v1"
    assert marker.schema_version == 1
    refused = (
        b"{}",
        b'{"state":"sqlite-v1"}',
        b'{"state":"sqlite-v1","schema_version":1,"count":2}',
        b'{"state":"sqlite-v1","schema_version":2}',
        b'{"state":"SQLITE_V1","schema_version":1}',
        b'{"schema_version":1,"state":"sqlite-v1","state":"sqlite-v1"}',
    )
    for raw in refused:
        with pytest.raises(ValueError, match="cutover_incomplete"):
            parse_marker_bytes(raw)


def test_routing_legacy_sqlite_staged_invalid(tmp_path):
    leaf, source, canonical, marker, _lock = cutover_workspace(tmp_path)
    context = CutoverContext(first_use_legacy=False)
    assert resolve_route(
        marker_path=marker, sqlite_path=canonical, json_path=source,
        context=context,
    ) is HoldingsRoute.LEGACY

    candidate = leaf / "candidate.sqlite3"
    assert run_clone(source, candidate).status == "closed"
    os.link(candidate, canonical)
    assert resolve_route(
        marker_path=marker, sqlite_path=canonical, json_path=source,
        context=context,
    ) is HoldingsRoute.STAGED_OR_CRASHED

    marker.write_bytes(CUTOVER_MARKER_BYTES)
    os.chmod(marker, 0o600)
    assert resolve_route(
        marker_path=marker, sqlite_path=canonical, json_path=source,
        context=context,
    ) is HoldingsRoute.SQLITE_V1

    marker.write_bytes(b'{"state":"sqlite-v1","schema_version":1,"x":0}')
    assert resolve_route(
        marker_path=marker, sqlite_path=canonical, json_path=source,
        context=context,
    ) is HoldingsRoute.INVALID


def test_first_use_safe_absent_is_explicit_context(tmp_path):
    leaf = tmp_path / "toss"
    leaf.mkdir(mode=0o700)
    args = dict(
        marker_path=leaf / "holdings_cutover_v1.json",
        sqlite_path=leaf / "holdings_snapshots.sqlite3",
        json_path=leaf / "holdings_store.json",
    )
    assert resolve_route(
        **args, context=CutoverContext(first_use_legacy=True),
    ) is HoldingsRoute.LEGACY
    assert resolve_route(
        **args, context=CutoverContext(first_use_legacy=False),
    ) is HoldingsRoute.INVALID


def test_family_lock_shared_exclusive_and_finally_release(tmp_path):
    leaf = tmp_path / "toss"
    leaf.mkdir(mode=0o700)
    lock = leaf / ".holdings_family.lock"
    first = acquire_family_lock(lock, exclusive=False, deadline_s=0.1)
    second = acquire_family_lock(lock, exclusive=False, deadline_s=0.1)
    try:
        with pytest.raises(FamilyLockTimeout):
            acquire_family_lock(lock, exclusive=True, deadline_s=0.03)
    finally:
        os.close(second)
        os.close(first)

    with pytest.raises(KeyboardInterrupt):
        with_family_lock(
            lock, exclusive=True,
            fn=lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
    fd = acquire_family_lock(lock, exclusive=True, deadline_s=0.1)
    os.close(fd)


def test_promotion_marker_and_retained_alias(tmp_path):
    leaf, source, canonical, marker, lock = cutover_workspace(tmp_path)
    approval = approve_cutover_build(source)
    report = execute_cutover(
        approval=approval, canonical=canonical, marker=marker, lock_path=lock)
    assert report.status == "closed"
    assert report.reason == ""
    assert canonical.exists()
    assert marker.read_bytes() == CUTOVER_MARKER_BYTES
    assert stat.S_IMODE(os.lstat(marker).st_mode) == 0o600
    aliases = list(leaf.glob(".holdings_snapshots.staging.*.sqlite3"))
    assert len(aliases) == 1
    assert os.path.samefile(aliases[0], canonical)


def test_source_change_refusal_under_exclusive_lock(tmp_path):
    _leaf, source, canonical, marker, lock = cutover_workspace(tmp_path)
    approval = approve_cutover_build(source)
    payload = source.read_bytes() + b" "
    source.write_bytes(payload)
    os.chmod(source, 0o600)
    report = execute_cutover(
        approval=approval, canonical=canonical, marker=marker, lock_path=lock)
    assert report.status == "refused"
    assert report.reason == "source_changed"
    assert not canonical.exists()
    assert not marker.exists()


def test_promotion_crash_windows_never_route_sqlite_without_marker(tmp_path):
    leaf, source, canonical, marker, lock = cutover_workspace(tmp_path)
    approval = approve_cutover_build(source)
    report = execute_cutover(
        approval=approval, canonical=canonical, marker=marker, lock_path=lock,
        stop_after="promotion",
    )
    assert report.status == "interrupted"
    assert not marker.exists()
    assert resolve_route(
        marker_path=marker, sqlite_path=canonical, json_path=source,
        context=CutoverContext(first_use_legacy=False),
    ) is HoldingsRoute.STAGED_OR_CRASHED
    assert list(leaf.glob(".holdings_snapshots.staging.*.sqlite3"))


def test_marker_write_failure_windows_are_deterministic(tmp_path, monkeypatch):
    import tools.migrate_holdings_factstore as tool

    _leaf, source, canonical, marker, lock = cutover_workspace(tmp_path)
    approval = approve_cutover_build(source)
    monkeypatch.setattr(tool, "_commit_marker",
                        lambda _path: "marker_commit_failed")
    report = execute_cutover(
        approval=approval, canonical=canonical, marker=marker, lock_path=lock)
    assert (report.status, report.reason) == (
        "refused", "marker_commit_failed")
    assert not marker.exists()
    assert resolve_route(
        marker_path=marker, sqlite_path=canonical, json_path=source,
        context=CutoverContext(first_use_legacy=False),
    ) is HoldingsRoute.STAGED_OR_CRASHED


def test_crash_after_marker_replace_routes_sqlite(tmp_path, monkeypatch):
    import tools.migrate_holdings_factstore as tool

    _leaf, source, canonical, marker, lock = cutover_workspace(tmp_path)
    real_fsync = tool.fsync_directory
    calls = 0

    def fail_marker_parent(directory):
        nonlocal calls
        calls += 1
        if calls == 2:  # promotion parent fsync succeeded; marker replace did.
            raise OSError("synthetic")
        return real_fsync(directory)

    monkeypatch.setattr(tool, "fsync_directory", fail_marker_parent)
    report = execute_cutover(
        approval=approve_cutover_build(source),
        canonical=canonical, marker=marker, lock_path=lock)
    assert (report.status, report.reason) == (
        "recovery_required", "marker_durability_uncertain")
    assert marker.read_bytes() == CUTOVER_MARKER_BYTES
    assert resolve_route(
        marker_path=marker, sqlite_path=canonical, json_path=source,
        context=CutoverContext(first_use_legacy=False),
    ) is HoldingsRoute.SQLITE_V1


def test_no_replace_existing_canonical_and_no_deletion(tmp_path):
    leaf, source, canonical, marker, lock = cutover_workspace(tmp_path)
    canonical.write_bytes(b"sentinel")
    os.chmod(canonical, 0o600)
    before = canonical.read_bytes()
    report = execute_cutover(
        approval=approve_cutover_build(source),
        canonical=canonical, marker=marker, lock_path=lock)
    assert report.status == "refused"
    assert report.reason == "canonical_exists"
    assert canonical.read_bytes() == before
    assert source.exists()
    assert not marker.exists()


def test_whole_file_quarantine_simulation_is_non_mutating(tmp_path):
    from tools.migrate_holdings_factstore import simulate_whole_file_quarantine

    _leaf, source, canonical, marker, _lock = cutover_workspace(tmp_path)
    before = tree_inventory(tmp_path)
    report = simulate_whole_file_quarantine(
        source=source, canonical=canonical, marker=marker)
    assert report.status == "simulation"
    assert report.reason == "separate_approval_required"
    assert tree_inventory(tmp_path) == before


def test_route_dominance_legacy_sqlite_and_refused(tmp_path):
    leaf, source, canonical, marker, lock = cutover_workspace(tmp_path)
    calls = []

    def invoke():
        return execute_holdings_route(
            marker_path=marker, sqlite_path=canonical, json_path=source,
            lock_path=lock, context=CutoverContext(first_use_legacy=False),
            legacy_fn=lambda: calls.append("legacy"),
            sqlite_fn=lambda: calls.append("sqlite"),
        )

    invoke()
    assert calls == ["legacy"]

    approval = approve_cutover_build(source)
    assert execute_cutover(
        approval=approval, canonical=canonical, marker=marker,
        lock_path=lock).status == "closed"
    calls.clear()
    invoke()
    assert calls == ["sqlite"]

    marker.write_bytes(b"{}")
    calls.clear()
    with pytest.raises(LiveRunError, match="cutover_incomplete"):
        invoke()
    assert calls == []


def test_legacy_reaches_only_json(tmp_path):
    leaf, source, canonical, marker, lock = cutover_workspace(tmp_path)
    calls = []
    execute_holdings_route(
        marker_path=marker, sqlite_path=canonical, json_path=source,
        lock_path=lock, context=CutoverContext(first_use_legacy=False),
        legacy_fn=lambda: calls.append("json"),
        sqlite_fn=lambda: calls.append("sqlite"),
    )
    assert calls == ["json"]


def test_sqlite_v1_reaches_only_sqlite(tmp_path):
    leaf, source, canonical, marker, lock = cutover_workspace(tmp_path)
    assert execute_cutover(
        approval=approve_cutover_build(source), canonical=canonical,
        marker=marker, lock_path=lock,
    ).status == "closed"
    calls = []
    execute_holdings_route(
        marker_path=marker, sqlite_path=canonical, json_path=source,
        lock_path=lock, context=CutoverContext(first_use_legacy=False),
        legacy_fn=lambda: calls.append("json"),
        sqlite_fn=lambda: calls.append("sqlite"),
    )
    assert calls == ["sqlite"]


def test_staged_invalid_reach_neither(tmp_path):
    leaf, source, canonical, marker, lock = cutover_workspace(tmp_path)
    candidate = leaf / "candidate.sqlite3"
    assert run_clone(source, candidate).status == "closed"
    os.link(candidate, canonical)
    calls = []
    with pytest.raises(LiveRunError, match="cutover_incomplete"):
        execute_holdings_route(
            marker_path=marker, sqlite_path=canonical, json_path=source,
            lock_path=lock, context=CutoverContext(first_use_legacy=False),
            legacy_fn=lambda: calls.append("json"),
            sqlite_fn=lambda: calls.append("sqlite"),
        )
    assert calls == []


def test_route_transition_recheck_reaches_neither(tmp_path, monkeypatch):
    import tokmo.composition as composition

    leaf, source, canonical, marker, lock = cutover_workspace(tmp_path)
    calls = []
    routes = iter([HoldingsRoute.LEGACY, HoldingsRoute.SQLITE_V1])
    monkeypatch.setattr(composition, "resolve_route", lambda **_kw: next(routes))
    with pytest.raises(composition.LiveRunError, match="cutover_incomplete"):
        composition.execute_holdings_route(
            marker_path=marker, sqlite_path=canonical, json_path=source,
            lock_path=lock, context=CutoverContext(first_use_legacy=False),
            legacy_fn=lambda: calls.append("legacy"),
            sqlite_fn=lambda: calls.append("sqlite"),
        )
    assert calls == []


def test_rollback_before_promotion_is_non_destructive_simulation(tmp_path):
    from tools.migrate_holdings_factstore import (
        simulate_rollback_before_promotion,
    )

    leaf, source, _canonical, _marker, _lock = cutover_workspace(tmp_path)
    staging = leaf / ".holdings_snapshots.staging.synthetic.sqlite3"
    staging.write_bytes(b"retained")
    os.chmod(staging, 0o600)
    before = tree_inventory(tmp_path)
    report = simulate_rollback_before_promotion(
        source=source, staging=staging)
    assert (report.status, report.reason) == ("simulation", "artifact_retained")
    assert tree_inventory(tmp_path) == before


def test_rollback_after_promotion_before_marker_requires_reversal_approval(
    tmp_path,
):
    from tools.migrate_holdings_factstore import (
        simulate_rollback_after_promotion_before_marker,
    )

    leaf, source, canonical, marker, _lock = cutover_workspace(tmp_path)
    staging = leaf / ".holdings_snapshots.staging.synthetic.sqlite3"
    staging.write_bytes(b"retained")
    os.chmod(staging, 0o600)
    os.link(staging, canonical)
    before = tree_inventory(tmp_path)
    report = simulate_rollback_after_promotion_before_marker(
        source=source, staging=staging, canonical=canonical, marker=marker)
    assert (report.status, report.reason) == (
        "simulation", "separate_reversal_required")
    assert tree_inventory(tmp_path) == before


def tree_inventory(base: Path) -> set[tuple[str, int, int]]:
    entries = set()
    for path in sorted(base.rglob("*")):
        value = os.lstat(path)
        entries.add((str(path.relative_to(base)), value.st_size,
                     value.st_mtime_ns))
    return entries


def source_state(source: Path) -> tuple[bytes, int]:
    return source.read_bytes(), os.lstat(source).st_mtime_ns


def test_dry_run_creates_and_mutates_nothing(tmp_path):
    source, candidate = workspace(tmp_path)
    before_tree = tree_inventory(tmp_path)
    before_source = source_state(source)

    report = run_dry_run(source)

    assert report.status == "closed"
    assert report.reason == ""
    assert (report.captures, report.accounts, report.items) == (2, 4, 4)
    assert report.schema_version == 1
    assert report.criteria_version == MIGRATION_CRITERIA_VERSION
    assert tree_inventory(tmp_path) == before_tree
    assert source_state(source) == before_source
    assert not candidate.exists()


def test_dry_run_refuses_non_toss(tmp_path):
    source, candidate = workspace(tmp_path)
    source.write_bytes(REFUSAL_FIXTURE.read_bytes())
    before_tree = tree_inventory(tmp_path)

    report = run_dry_run(source)

    assert report.status == "refused"
    assert report.reason == "non_toss_source"
    assert (report.captures, report.accounts, report.items) == (0, 0, 0)
    assert tree_inventory(tmp_path) == before_tree


# --- Offline migration tool: clone candidate parity and reopen ---


def test_clone_dry_run(tmp_path):
    source, candidate = workspace(tmp_path)
    report = run_clone(source, candidate)

    assert report.status == "closed"
    assert report.reason == ""
    assert (report.captures, report.accounts, report.items) == (2, 4, 4)

    value = os.lstat(candidate)
    assert stat.S_ISREG(value.st_mode)
    assert stat.S_IMODE(value.st_mode) == 0o600
    assert value.st_uid == os.getuid()
    parent = os.lstat(candidate.parent)
    assert stat.S_IMODE(parent.st_mode) == 0o700
    for suffix in ("-journal", "-wal", "-shm"):
        assert not Path(f"{candidate}{suffix}").exists()


def test_clone_candidate_parity_reopen(tmp_path):
    source, candidate = workspace(tmp_path)
    assert run_clone(source, candidate).status == "closed"

    store = HoldingsSnapshotStore(candidate, clock=clock)
    headers = store.list_capture_headers()
    assert len(headers) == 2
    assert {header.snapshot_id for header in headers} == set(
        EXPECTED_LEGACY_DIGESTS.values())
    assert {header.content_hash for header in headers} == set(
        EXPECTED_CONTENT_HASHES.values())
    for capture in decoded_captures():
        read_back = store.read_snapshot(capture.snapshot_id)
        assert canonical_snapshot_bytes(read_back) == canonical_snapshot_bytes(
            capture)
        assert read_back.content_hash == capture.content_hash


def test_whole_source_byte_invariance(tmp_path):
    source, candidate = workspace(tmp_path)
    before_source = source_state(source)
    before_dir = tree_inventory(source.parent)

    assert run_clone(source, candidate).status == "closed"

    assert source_state(source) == before_source
    assert tree_inventory(source.parent) == before_dir


def test_clone_refuses_existing_candidate(tmp_path):
    source, candidate = workspace(tmp_path)
    assert run_clone(source, candidate).status == "closed"
    after_first = os.lstat(candidate).st_mtime_ns, candidate.read_bytes()

    report = run_clone(source, candidate)
    assert report.status == "refused"
    assert report.reason == "candidate_exists"
    assert (os.lstat(candidate).st_mtime_ns,
            candidate.read_bytes()) == after_first


def test_clone_rerun_deterministic(tmp_path):
    """A rerun into a fresh candidate reproduces the exact same IDs/digests
    and canonical content."""
    source, candidate = workspace(tmp_path)
    second = candidate.with_name("holdings_candidate_2.sqlite3")
    assert run_clone(source, candidate).status == "closed"
    assert run_clone(source, second).status == "closed"

    first_store = HoldingsSnapshotStore(candidate, clock=clock)
    second_store = HoldingsSnapshotStore(second, clock=clock)
    first_ids = [h.snapshot_id for h in first_store.list_capture_headers()]
    assert first_ids == [
        h.snapshot_id for h in second_store.list_capture_headers()]
    for snapshot_id in first_ids:
        assert canonical_snapshot_bytes(
            first_store.read_snapshot(snapshot_id)
        ) == canonical_snapshot_bytes(second_store.read_snapshot(snapshot_id))


def test_clone_refuses_non_toss_source(tmp_path):
    source, candidate = workspace(tmp_path)
    source.write_bytes(REFUSAL_FIXTURE.read_bytes())
    report = run_clone(source, candidate)
    assert report.status == "refused"
    assert report.reason == "non_toss_source"
    assert not candidate.exists()


def test_clone_refuses_unsafe_source(tmp_path):
    source, candidate = workspace(tmp_path)

    missing = source.parent / "absent.json"
    assert run_clone(missing, candidate).reason == "source_missing"

    link = source.parent / "link.json"
    link.symlink_to(source)
    assert run_clone(link, candidate).reason == "source_unsafe"

    fifo = source.parent / "fifo.json"
    os.mkfifo(fifo, 0o600)
    assert run_clone(fifo, candidate).reason == "source_unsafe"

    os.chmod(source, 0o646)
    assert run_clone(source, candidate).reason == "source_unsafe"

    os.chmod(source, 0o600)
    assert not candidate.exists()


def test_clone_refuses_unsafe_candidate_parent(tmp_path):
    source, candidate = workspace(tmp_path)

    os.chmod(candidate.parent, 0o755)
    assert run_clone(source, candidate).reason == "candidate_unsafe"
    os.chmod(candidate.parent, 0o700)

    alias_dir = tmp_path / "alias"
    alias_dir.symlink_to(candidate.parent)
    assert run_clone(
        source, alias_dir / "candidate.sqlite3").reason == "candidate_unsafe"

    missing_parent = tmp_path / "missing" / "candidate.sqlite3"
    assert run_clone(source, missing_parent).reason == "candidate_unsafe"
    assert not candidate.exists()


def test_clone_injected_import_failure(tmp_path, monkeypatch):
    import tools.migrate_holdings_factstore as tool_module

    source, candidate = workspace(tmp_path)

    class FailingStore(HoldingsSnapshotStore):
        def import_legacy(self, snapshot, permit):
            return self._failure(snapshot.snapshot_id, clock(),
                                 "store_write_failed")

    monkeypatch.setattr(tool_module, "HoldingsSnapshotStore", FailingStore)
    report = run_clone(source, candidate)
    assert report.status == "refused"
    assert report.reason == "import_failed"


# --- Clone rehearsal source copy (SDS §10.2 step 3, §10.5 no-deletion) ---


import hashlib  # noqa: E402

import tools.migrate_holdings_factstore as tool_module  # noqa: E402


def expected_copy_path(candidate: Path) -> Path:
    """The pinned deterministic rehearsal-source-copy filename."""
    return candidate.parent / (candidate.name + ".rehearsal-source.json")


def private_artifacts(candidate: Path) -> set[str]:
    return {entry.name for entry in candidate.parent.iterdir()}


def test_rehearsal_copy_path_pinned(tmp_path):
    source, candidate = workspace(tmp_path)
    copy = tool_module.rehearsal_source_path(candidate)
    assert copy == expected_copy_path(candidate)
    assert copy != candidate
    assert copy != source
    assert copy.parent == candidate.parent


def test_clone_creates_owner_only_rehearsal_source_copy(tmp_path):
    source, candidate = workspace(tmp_path)
    original = source.read_bytes()

    assert run_clone(source, candidate).status == "closed"

    copy = expected_copy_path(candidate)
    value = os.lstat(copy)
    assert stat.S_ISREG(value.st_mode)
    assert stat.S_IMODE(value.st_mode) == 0o600
    assert value.st_uid == os.getuid()
    assert copy.read_bytes() == original
    assert (hashlib.sha256(copy.read_bytes()).hexdigest()
            == hashlib.sha256(original).hexdigest())
    # Exactly the candidate and its rehearsal source copy — nothing else was
    # created, and nothing was deleted.
    assert private_artifacts(candidate) == {candidate.name, copy.name}


def test_clone_source_hash_and_mtime_invariant(tmp_path):
    source, candidate = workspace(tmp_path)
    before_bytes = source.read_bytes()
    before_hash = hashlib.sha256(before_bytes).hexdigest()
    before_mtime = os.lstat(source).st_mtime_ns

    assert run_clone(source, candidate).status == "closed"

    after_bytes = source.read_bytes()
    assert after_bytes == before_bytes
    assert hashlib.sha256(after_bytes).hexdigest() == before_hash
    assert os.lstat(source).st_mtime_ns == before_mtime
    copy = expected_copy_path(candidate)
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == before_hash
    assert stat.S_IMODE(os.lstat(copy).st_mode) == 0o600


def test_clone_refuses_preexisting_rehearsal_copy(tmp_path):
    source, candidate = workspace(tmp_path)
    copy = expected_copy_path(candidate)
    copy.write_bytes(b"sentinel")
    os.chmod(copy, 0o600)

    report = run_clone(source, candidate)
    assert report.status == "refused"
    assert report.reason == "rehearsal_copy_exists"
    assert (report.captures, report.accounts, report.items) == (0, 0, 0)
    # Never overwritten, never deleted; candidate never created.
    assert copy.read_bytes() == b"sentinel"
    assert not candidate.exists()
    rendered = repr(report) + "\n" + format_report(report)
    assert "/" not in rendered
    assert str(tmp_path) not in rendered


def test_clone_refuses_symlink_rehearsal_copy_path(tmp_path):
    source, candidate = workspace(tmp_path)
    before_source = source_state(source)
    copy = expected_copy_path(candidate)
    copy.symlink_to(source)

    report = run_clone(source, candidate)
    assert report.status == "refused"
    assert report.reason == "rehearsal_copy_exists"
    assert stat.S_ISLNK(os.lstat(copy).st_mode)
    assert source_state(source) == before_source
    assert not candidate.exists()


def test_clone_copy_created_before_decode_and_preserved_on_refusal(tmp_path):
    """SDS §10.2 orders the copy before validation; §10.5 forbids automatic
    deletion, so a refused source still leaves its owner-only copy behind."""
    source, candidate = workspace(tmp_path)
    source.write_bytes(REFUSAL_FIXTURE.read_bytes())

    report = run_clone(source, candidate)
    assert report.status == "refused"
    assert report.reason == "non_toss_source"
    assert not candidate.exists()
    copy = expected_copy_path(candidate)
    assert copy.read_bytes() == REFUSAL_FIXTURE.read_bytes()
    assert stat.S_IMODE(os.lstat(copy).st_mode) == 0o600
    assert private_artifacts(candidate) == {copy.name}


def test_clone_decoder_reads_rehearsal_copy(tmp_path, monkeypatch):
    source, candidate = workspace(tmp_path)
    copy = expected_copy_path(candidate)
    seen = []
    real = tool_module.validate_legacy_json

    def spy(raw):
        seen.append((raw, copy.read_bytes() if copy.exists() else None))
        return real(raw)

    monkeypatch.setattr(tool_module, "validate_legacy_json", spy)
    assert run_clone(source, candidate).status == "closed"

    assert len(seen) == 1
    raw, copy_bytes_at_decode = seen[0]
    # The copy already existed when the decoder ran, and the decoder input is
    # exactly the copy's bytes.
    assert copy_bytes_at_decode == raw
    assert raw == TOSS_FIXTURE.read_bytes()


def test_partial_import_failure_preserves_private_artifacts(
        tmp_path, monkeypatch):
    """MINOR-2 as explicit policy: after a partial clone failure the private
    0600 candidate and rehearsal copy both survive (SDS §10.5 requires a
    separate deletion approval), and a rerun refuses candidate_exists."""
    source, candidate = workspace(tmp_path)
    original = source.read_bytes()

    class SecondImportFailingStore(HoldingsSnapshotStore):
        imports = 0

        def import_legacy(self, snapshot, permit):
            type(self).imports += 1
            if type(self).imports > 1:
                return self._failure(snapshot.snapshot_id, clock(),
                                     "store_write_failed")
            return super().import_legacy(snapshot, permit)

    monkeypatch.setattr(tool_module, "HoldingsSnapshotStore",
                        SecondImportFailingStore)
    report = run_clone(source, candidate)
    assert report.status == "refused"
    assert report.reason == "import_failed"

    # The partial candidate holds exactly capture 1 and stays private.
    copy = expected_copy_path(candidate)
    assert stat.S_IMODE(os.lstat(candidate).st_mode) == 0o600
    assert stat.S_IMODE(os.lstat(copy).st_mode) == 0o600
    assert copy.read_bytes() == original
    headers = HoldingsSnapshotStore(candidate, clock=clock) \
        .list_capture_headers()
    assert [h.snapshot_id for h in headers] == [
        EXPECTED_LEGACY_DIGESTS["capture_1"]]
    assert private_artifacts(candidate) == {candidate.name, copy.name}

    # A rerun with the real store refuses without touching either artifact.
    monkeypatch.undo()
    before = {name: (os.lstat(candidate.parent / name).st_mtime_ns,
                     (candidate.parent / name).read_bytes())
              for name in private_artifacts(candidate)}
    rerun = run_clone(source, candidate)
    assert rerun.status == "refused"
    assert rerun.reason == "candidate_exists"
    assert private_artifacts(candidate) == set(before)
    for name, state in before.items():
        path = candidate.parent / name
        assert (os.lstat(path).st_mtime_ns, path.read_bytes()) == state


def test_clone_success_never_deletes_rehearsal_copy(tmp_path):
    source, candidate = workspace(tmp_path)
    assert run_clone(source, candidate).status == "closed"
    copy = expected_copy_path(candidate)
    assert copy.exists()
    assert private_artifacts(candidate) == {candidate.name, copy.name}


def test_rehearsal_reason_tokens_closed():
    assert {"rehearsal_copy_exists", "copy_failed"} <= TOOL_REFUSAL_REASONS


def test_fresh_process_reopen(tmp_path):
    source, candidate = workspace(tmp_path)
    assert run_clone(source, candidate).status == "closed"

    code = (
        "import json, sys\n"
        "from datetime import datetime, timezone\n"
        "from pathlib import Path\n"
        "from tokmo.records.holdings_snapshots import HoldingsSnapshotStore\n"
        "store = HoldingsSnapshotStore(Path(sys.argv[1]),\n"
        "    clock=lambda: datetime.now(timezone.utc))\n"
        "headers = store.list_capture_headers()\n"
        "snapshots = [store.read_snapshot(h.snapshot_id) for h in headers]\n"
        "print(json.dumps({\n"
        "    'ids': sorted(h.snapshot_id for h in headers),\n"
        "    'hashes': sorted(s.content_hash for s in snapshots),\n"
        "    'accounts': sum(len(s.accounts) for s in snapshots),\n"
        "}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, str(candidate)],
        capture_output=True, text=True, cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ids"] == sorted(EXPECTED_LEGACY_DIGESTS.values())
    assert payload["hashes"] == sorted(EXPECTED_CONTENT_HASHES.values())
    assert payload["accounts"] == 4


# --- CLI: import-safe, exact closed output, redaction ---


def run_cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "tools.migrate_holdings_factstore", *argv],
        capture_output=True, text=True, cwd=ROOT,
        env={**os.environ,
             "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}"},
        timeout=60,
    )


def closed_report_text(status: str, reason: str, captures: int,
                       accounts: int, items: int) -> str:
    return (
        f"status={status}\n"
        f"reason={reason}\n"
        f"captures={captures}\n"
        f"accounts={accounts}\n"
        f"items={items}\n"
        "schema_version=1\n"
        "criteria_version=legacy-factstore-import-v1\n"
    )


def test_cli_dry_run_exact_closed_output(tmp_path):
    source, candidate = workspace(tmp_path)
    completed = run_cli(
        "--source", str(source), "--candidate", str(candidate), "--dry-run")
    assert completed.returncode == 0
    assert completed.stdout == closed_report_text("closed", "", 2, 4, 4)
    assert completed.stderr == ""
    assert not candidate.exists()


def test_cli_clone_exact_closed_output(tmp_path):
    source, candidate = workspace(tmp_path)
    completed = run_cli(
        "--source", str(source), "--candidate", str(candidate), "--clone")
    assert completed.returncode == 0
    assert completed.stdout == closed_report_text("closed", "", 2, 4, 4)
    assert completed.stderr == ""
    assert candidate.exists()


def test_cli_refusal_exact_closed_output(tmp_path):
    source, candidate = workspace(tmp_path)
    source.write_bytes(REFUSAL_FIXTURE.read_bytes())
    completed = run_cli(
        "--source", str(source), "--candidate", str(candidate), "--dry-run")
    assert completed.returncode == 1
    assert completed.stdout == closed_report_text(
        "refused", "non_toss_source", 0, 0, 0)
    assert completed.stderr == ""


def test_cli_help_documents_rehearsal_vs_preflight():
    """--clone is documented as the SDS full rehearsal protocol and
    --dry-run as preflight-only, without editing the closed SDS."""
    completed = run_cli("--help")
    assert completed.returncode == 0
    assert "rehearsal" in completed.stdout
    assert "preflight" in completed.stdout


def test_cli_import_safe():
    source_text = TOOL_SOURCE_PATH.read_text()
    assert 'if __name__ == "__main__":' in source_text
    assert callable(main)
    # Importing the module (already done at collection) runs no CLI, opens no
    # store, and prints nothing; main() is the only entry point.
    tree = ast.parse(source_text)
    top_level_calls = [
        node for node in tree.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    ]
    assert top_level_calls == []


def test_report_reasons_and_redaction(tmp_path):
    source, candidate = workspace(tmp_path)
    closed = run_clone(source, candidate)
    refusal = run_dry_run(ROOT / "tests" / "fixtures"
                          / "holdings_legacy_non_toss_refusal_v1.json")
    for report in (closed, refusal):
        assert report.reason == "" or report.reason in TOOL_REFUSAL_REASONS
        rendered = repr(report) + "\n" + format_report(report)
        for leaked in ("AAPL", "GOOGL", "MSFT", "150.5", "2800", "380.25",
                       "Apple", "Alphabet", "Microsoft", "/", str(tmp_path),
                       *EXPECTED_LEGACY_DIGESTS.values(),
                       *EXPECTED_CONTENT_HASHES.values()):
            assert leaked not in rendered


# --- Architecture: frozen decoder isolation, no runtime authorize/append ---

DECODER_SOURCE_PATH = ROOT / "src" / "tokmo" / "records" / "legacy_holdings_v1.py"

FORBIDDEN_IMPORT_PREFIXES = (
    "tokmo.records.fact_store", "tokmo.facts", "tokmo.composition",
    "tokmo.runtime", "urllib", "http", "socket", "requests", "ssl",
)


def test_generation_metadata_boundaries_are_frozen():
    assert generation_migration._V1_METADATA == frozenset({
        ("holdings_snapshot_schema_version", "1"),
        ("holdings_snapshot_adapter", "tokmo-holdings-snapshots-v1"),
    })
    assert generation_migration._GEN2_METADATA == frozenset({
        ("holdings_snapshot_schema_version", "2"),
        ("holdings_snapshot_adapter", "tokmo-holdings-snapshots-v1"),
    })


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def test_frozen_decoder_import_isolation():
    """The frozen decoder imports only stdlib codecs and the values leaf —
    never FactStore, facts, composition, live, network, or any module that
    could write (no os/pathlib/io in its import graph)."""
    names = imported_modules(DECODER_SOURCE_PATH)
    assert names <= {"json", "hashlib", "dataclasses", "datetime", "decimal",
                     "tokmo.values"}


def test_frozen_decoder_no_factstore_reuse():
    source_text = DECODER_SOURCE_PATH.read_text()
    for token in ("fact_store", "_validate_toss_holding", "_holding_records",
                  "FactStore"):
        assert token not in source_text


def test_tool_import_isolation():
    names = imported_modules(TOOL_SOURCE_PATH)
    for name in names:
        assert not any(
            name == prefix or name.startswith(prefix + ".")
            for prefix in FORBIDDEN_IMPORT_PREFIXES
        ), name


def test_tool_no_permit_constructor_no_runtime_authorize_no_append():
    source_text = TOOL_SOURCE_PATH.read_text()
    assert "HoldingsPersistencePermitV1(" not in source_text
    assert "authorize_user_request" not in source_text
    assert "authorize_named_program" not in source_text
    assert ".append(" not in source_text
    assert "import_legacy(" in source_text
    assert "authorize_legacy_migration(" in source_text


def test_runtime_composition_cannot_reach_migration():
    runtime_files = [ROOT / "src" / "tokmo" / "composition.py"]
    runtime_files.extend((ROOT / "src" / "tokmo" / "programs").glob("*.py"))
    for path in runtime_files:
        source_text = path.read_text()
        for token in ("authorize_legacy_migration", "import_legacy",
                      "legacy_holdings_v1", "migrate_holdings_factstore"):
            assert token not in source_text, (path.name, token)
