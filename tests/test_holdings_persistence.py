"""Persistence criteria v1: closed registry, permit minting, and validation.

The permit is typed policy metadata, not a capability token (SDS §3.2), so
these tests pin the closed criteria/purpose/program pairings, the denial
tokens, the UUID4 snapshot-id grammar, and snapshot binding. All values are
synthetic; no account, symbol, quantity, or price appears anywhere.
"""

from datetime import UTC, datetime

import pytest

from tokmo.records.holdings_persistence import (
    CRITERIA_VERSION_V1,
    MIGRATION_CRITERIA_VERSION,
    NAMED_PROGRAM_ALLOWLIST_V1,
    DenialToken,
    HoldingsPersistencePermitV1,
    PurposeKind,
    authorize_named_program,
    authorize_user_request,
    validate_permit,
)

# A fixed, valid version-4 / RFC 4122-variant UUID in lowercase hex form
# (third group starts with "4", fourth with "8").
UUID4_HEX = "16fd27068baf433b82eb8c7fada847da"
OTHER_UUID4_HEX = "a6e4c9a06b1743188cb2dd8e5a06a3d4"
LEGACY_DIGEST = "5cafa5c45986675183f3185088e415f6995b34ae68fbece140ed80ba9b351ccc"

AUTHORIZED_AT = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)


def fixed_clock():
    return AUTHORIZED_AT


def test_user_request_permit_valid():
    permit = authorize_user_request(snapshot_id=UUID4_HEX, clock=fixed_clock)
    assert isinstance(permit, HoldingsPersistencePermitV1)
    assert permit.criteria_version == CRITERIA_VERSION_V1 == "holdings-persistence-v1"
    assert permit.purpose_kind is PurposeKind.USER_REQUESTED_CAPTURE
    assert permit.program_name == "user-requested-capture"
    assert permit.authorized_at == AUTHORIZED_AT


def test_unknown_program_denied():
    denial = authorize_named_program(
        snapshot_id=UUID4_HEX,
        program_name="nonexistent",
        clock=fixed_clock,
    )
    assert isinstance(denial, DenialToken)
    assert denial.reason == "unknown_program"


def test_probe_cannot_obtain_permit():
    """A development probe/diagnostic/incidental lookup cannot get a permit:
    the v1 allowlist is empty and no other minting path exists."""
    assert NAMED_PROGRAM_ALLOWLIST_V1 == frozenset()
    for probe in ("development-probe", "diagnostic", "incidental-read"):
        result = authorize_named_program(
            snapshot_id=UUID4_HEX,
            program_name=probe,
            clock=fixed_clock,
        )
        assert isinstance(result, DenialToken)

    import tokmo.records.holdings_persistence as module

    minting = [name for name in vars(module) if name.startswith("authorize_")]
    # The closed minting set: the two runtime factories plus the
    # migration-only factory and the pension-import factory.
    assert sorted(minting) == [
        "authorize_legacy_migration",
        "authorize_named_program",
        "authorize_pension_import",  # N4: pension import minter
        "authorize_user_request",
    ]


def test_permit_bound_to_snapshot_id():
    permit = authorize_user_request(snapshot_id=UUID4_HEX, clock=fixed_clock)
    assert permit.snapshot_id == UUID4_HEX


def test_fabricated_permit_rejected():
    """Constructing a permit with a criteria version outside the closed set
    raises; the type itself refuses unknown policy vocabulary."""
    with pytest.raises(ValueError):
        HoldingsPersistencePermitV1(
            criteria_version="holdings-persistence-v99",
            snapshot_id=UUID4_HEX,
            purpose_kind=PurposeKind.USER_REQUESTED_CAPTURE,
            program_name="user-requested-capture",
            authorized_at=AUTHORIZED_AT,
        )


def test_mismatched_snapshot_id_permit_rejected():
    permit = authorize_user_request(snapshot_id=UUID4_HEX, clock=fixed_clock)
    assert validate_permit(permit, snapshot_id=UUID4_HEX)
    assert not validate_permit(permit, snapshot_id=OTHER_UUID4_HEX)


def test_expired_context_permit_rejected():
    """A permit minted for one capture cannot be reused for a later one:
    each capture gets a new snapshot_id and the old permit fails validation."""
    permit = authorize_user_request(snapshot_id=UUID4_HEX, clock=fixed_clock)
    later_capture = OTHER_UUID4_HEX
    assert not validate_permit(permit, snapshot_id=later_capture)


def test_invalid_pairing_permit_rejected():
    """Both closed criteria versions exist, but only the exact registry
    pairings validate; crossed pairings and off-registry program names fail."""
    crossed = HoldingsPersistencePermitV1(
        criteria_version=MIGRATION_CRITERIA_VERSION,
        snapshot_id=LEGACY_DIGEST,
        purpose_kind=PurposeKind.USER_REQUESTED_CAPTURE,
        program_name="user-requested-capture",
        authorized_at=AUTHORIZED_AT,
    )
    assert not validate_permit(crossed, snapshot_id=LEGACY_DIGEST)

    wrong_program = HoldingsPersistencePermitV1(
        criteria_version=CRITERIA_VERSION_V1,
        snapshot_id=UUID4_HEX,
        purpose_kind=PurposeKind.USER_REQUESTED_CAPTURE,
        program_name="some-other-program",
        authorized_at=AUTHORIZED_AT,
    )
    assert not validate_permit(wrong_program, snapshot_id=UUID4_HEX)

    named_not_allowlisted = HoldingsPersistencePermitV1(
        criteria_version=CRITERIA_VERSION_V1,
        snapshot_id=UUID4_HEX,
        purpose_kind=PurposeKind.APPROVED_NAMED_PROGRAM,
        program_name="nonexistent",
        authorized_at=AUTHORIZED_AT,
    )
    assert not validate_permit(named_not_allowlisted, snapshot_id=UUID4_HEX)


def test_id_form_matches_criteria_version():
    """Runtime criteria require the UUID4 form; migration criteria require
    the 64-hex digest form. The forms are not interchangeable."""
    runtime_with_digest = HoldingsPersistencePermitV1(
        criteria_version=CRITERIA_VERSION_V1,
        snapshot_id=LEGACY_DIGEST,
        purpose_kind=PurposeKind.USER_REQUESTED_CAPTURE,
        program_name="user-requested-capture",
        authorized_at=AUTHORIZED_AT,
    )
    assert not validate_permit(runtime_with_digest, snapshot_id=LEGACY_DIGEST)

    migration_with_uuid = HoldingsPersistencePermitV1(
        criteria_version=MIGRATION_CRITERIA_VERSION,
        snapshot_id=UUID4_HEX,
        purpose_kind=PurposeKind.APPROVED_MIGRATION,
        program_name="legacy-factstore-import-v1",
        authorized_at=AUTHORIZED_AT,
    )
    assert not validate_permit(migration_with_uuid, snapshot_id=UUID4_HEX)

    migration_valid = HoldingsPersistencePermitV1(
        criteria_version=MIGRATION_CRITERIA_VERSION,
        snapshot_id=LEGACY_DIGEST,
        purpose_kind=PurposeKind.APPROVED_MIGRATION,
        program_name="legacy-factstore-import-v1",
        authorized_at=AUTHORIZED_AT,
    )
    assert validate_permit(migration_valid, snapshot_id=LEGACY_DIGEST)


def test_field_valid_permit_accepted_by_validator():
    """Documented by design (SDS §3.2): the data validator checks fields,
    not provenance, so an all-correct-fields permit constructed here is
    accepted. The defense is the architecture-tested call graph."""
    fabricated = HoldingsPersistencePermitV1(
        criteria_version=CRITERIA_VERSION_V1,
        snapshot_id=UUID4_HEX,
        purpose_kind=PurposeKind.USER_REQUESTED_CAPTURE,
        program_name="user-requested-capture",
        authorized_at=AUTHORIZED_AT,
    )
    assert validate_permit(fabricated, snapshot_id=UUID4_HEX)


def test_permit_repr_redacts_snapshot_id_and_time():
    permit = authorize_user_request(snapshot_id=UUID4_HEX, clock=fixed_clock)
    rendered = repr(permit)
    assert "snapshot_id=" not in rendered
    assert "authorized_at=" not in rendered
    assert UUID4_HEX not in rendered
    assert "criteria_version=" in rendered
    assert "program_name=" in rendered


def test_invalid_snapshot_id_denied():
    """Runtime minting validates full UUID4 grammar: wrong length, uppercase,
    non-hex, wrong version, and wrong variant are all denied."""
    not_uuid4 = [
        "",
        "short",
        UUID4_HEX.upper(),  # uppercase hex
        UUID4_HEX + "00",  # wrong length
        "16fd27068baf133b82eb8c7fada847da",  # version 1, not 4
        "16fd27068baf433bc2eb8c7fada847da",  # non-RFC-4122 variant
        "16fd27068baf433b82eb8c7fada847dg",  # non-hex
        "5cafa5c45986675183f3185088e415f6995b34ae68fbece140ed80ba9b351ccc",
    ]
    for value in not_uuid4:
        denial = authorize_user_request(snapshot_id=value, clock=fixed_clock)
        assert isinstance(denial, DenialToken), value
        assert denial.reason == "invalid_snapshot_id"
        denial = authorize_named_program(
            snapshot_id=value,
            program_name="nonexistent",
            clock=fixed_clock,
        )
        assert isinstance(denial, DenialToken), value
        assert denial.reason == "invalid_snapshot_id"
