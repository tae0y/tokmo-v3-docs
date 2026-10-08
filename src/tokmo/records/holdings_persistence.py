"""Versioned persistence criteria for complete holdings snapshots.

A ``HoldingsPersistencePermitV1`` is typed policy metadata, not a capability
token: a frozen dataclass cannot prove provenance, so the enforced policy
surface is the closed criteria registry in this module plus the
architecture-tested call graph. The permit carries no free-text reason,
request ID, account identifier, symbol, quantity, price, credential,
endpoint, or raw request/response.
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import RFC_4122, UUID

CRITERIA_VERSION_V1 = "holdings-persistence-v1"
MIGRATION_CRITERIA_VERSION = "legacy-factstore-import-v1"
PENSION_CRITERIA_VERSION = "pension-excel-persistence-v1"  # N4


class PurposeKind(Enum):
    USER_REQUESTED_CAPTURE = "USER_REQUESTED_CAPTURE"
    APPROVED_NAMED_PROGRAM = "APPROVED_NAMED_PROGRAM"
    APPROVED_MIGRATION = "APPROVED_MIGRATION"  # migration-only


# The v1 named-program allowlist is empty by design: only an explicit
# user-requested capture can persist until a later approved policy version.
NAMED_PROGRAM_ALLOWLIST_V1: frozenset[str] = frozenset()

# N4: pension named-program allowlist (separate from v1 allowlist).
PENSION_NAMED_PROGRAM_ALLOWLIST: frozenset[str] = frozenset({
    "tokmo-v3-pension-import-v1",
})

MIGRATION_PROGRAM_TOKEN = "legacy-factstore-import-v1"
USER_REQUEST_PROGRAM_NAME = "user-requested-capture"

DENIAL_REASONS = frozenset({"denied", "unknown_program", "invalid_snapshot_id"})

_LEGACY_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_LEGACY_ID_DOMAIN = b"tokmo-holdings-legacy-v1:"


def is_valid_uuid4_hex(value: str) -> bool:
    """True iff value is a 32-char lowercase hex string that decodes as a
    version-4 UUID with RFC 4122 variant. ``UUID(hex=..., version=4)`` forces
    the version/variant bits, so the round-trip equality is what rejects
    inputs whose original bits differ."""
    if len(value) != 32 or value != value.lower():
        return False
    try:
        parsed = UUID(hex=value, version=4)
    except ValueError:
        return False
    return (
        parsed.hex == value
        and parsed.version == 4
        and parsed.variant == RFC_4122
    )


def is_valid_legacy_digest(value: str) -> bool:
    """True iff value is exactly 64 lowercase hex characters."""
    return bool(_LEGACY_DIGEST_RE.fullmatch(value))


@dataclass(frozen=True)
class HoldingsPersistencePermitV1:
    """A snapshot-bound authorization to persist exactly one snapshot.

    ``snapshot_id`` is the sole v1 idempotency identity and is a local opaque
    unique capture string, not an account identity. It and ``authorized_at``
    stay out of ``repr`` so no opaque ID leaks through routine output.
    """

    criteria_version: str
    snapshot_id: str = field(repr=False)
    purpose_kind: PurposeKind
    program_name: str
    authorized_at: datetime = field(repr=False)

    def __post_init__(self) -> None:
        if self.criteria_version not in {CRITERIA_VERSION_V1,
                                         MIGRATION_CRITERIA_VERSION,
                                         PENSION_CRITERIA_VERSION}:
            raise ValueError("criteria_version_outside_contract")
        if not isinstance(self.purpose_kind, PurposeKind):
            raise ValueError("purpose_kind_outside_contract")
        if self.authorized_at.tzinfo is None:
            raise ValueError("authorized_at_naive")


@dataclass(frozen=True)
class DenialToken:
    """Closed fail-closed denial: a reason token and nothing else."""

    reason: str

    def __post_init__(self) -> None:
        if self.reason not in DENIAL_REASONS:
            raise ValueError(f"denial_reason_outside_contract:{self.reason}")


# The closed criteria registry: the only (criteria_version, purpose_kind)
# pairings that may persist, each with its closed program-name set. Changing
# an entry is a reviewed policy change, never a runtime decision.
HOLDINGS_PERSISTENCE_CRITERIA_V1 = {
    (CRITERIA_VERSION_V1, PurposeKind.USER_REQUESTED_CAPTURE):
        frozenset({USER_REQUEST_PROGRAM_NAME}),
    (CRITERIA_VERSION_V1, PurposeKind.APPROVED_NAMED_PROGRAM):
        NAMED_PROGRAM_ALLOWLIST_V1,
    (MIGRATION_CRITERIA_VERSION, PurposeKind.APPROVED_MIGRATION):
        frozenset({MIGRATION_PROGRAM_TOKEN}),
    # N4: pension import criteria.
    (PENSION_CRITERIA_VERSION, PurposeKind.APPROVED_NAMED_PROGRAM):
        PENSION_NAMED_PROGRAM_ALLOWLIST,
}


def validate_permit(
    permit: HoldingsPersistencePermitV1, *, snapshot_id: str,
) -> bool:
    """Field validation of a permit against the snapshot it must bind to.

    Checks fields, not provenance (SDS §3.2): exact snapshot binding, the
    registry pairing with its closed program-name set, and the snapshot_id
    form the criteria version requires (UUID4 hex for runtime,
    64-hex digest for migration). A permit whose fields are all correct is
    accepted regardless of who constructed it; the call graph is the defense.
    """
    if permit.snapshot_id != snapshot_id:
        return False
    allowed_programs = HOLDINGS_PERSISTENCE_CRITERIA_V1.get(
        (permit.criteria_version, permit.purpose_kind))
    if allowed_programs is None or permit.program_name not in allowed_programs:
        return False
    if permit.criteria_version == CRITERIA_VERSION_V1:
        return is_valid_uuid4_hex(permit.snapshot_id)
    if permit.criteria_version == PENSION_CRITERIA_VERSION:
        return is_valid_uuid4_hex(permit.snapshot_id)
    return is_valid_legacy_digest(permit.snapshot_id)


def authorize_user_request(
    *, snapshot_id: str, clock: Callable[[], datetime],
) -> HoldingsPersistencePermitV1 | DenialToken:
    """Mint a v1 permit for one explicit user-requested capture."""
    if not is_valid_uuid4_hex(snapshot_id):
        return DenialToken(reason="invalid_snapshot_id")
    return HoldingsPersistencePermitV1(
        criteria_version=CRITERIA_VERSION_V1,
        snapshot_id=snapshot_id,
        purpose_kind=PurposeKind.USER_REQUESTED_CAPTURE,
        program_name=USER_REQUEST_PROGRAM_NAME,
        authorized_at=clock(),
    )


def authorize_named_program(
    *, snapshot_id: str, program_name: str, clock: Callable[[], datetime],
) -> HoldingsPersistencePermitV1 | DenialToken:
    """Mint a v1 permit for an allowlisted named program.

    The v1 allowlist is empty, so every program — including any development
    probe or diagnostic — is denied here until an approved later version.
    """
    if not is_valid_uuid4_hex(snapshot_id):
        return DenialToken(reason="invalid_snapshot_id")
    if program_name not in NAMED_PROGRAM_ALLOWLIST_V1:
        return DenialToken(reason="unknown_program")
    return HoldingsPersistencePermitV1(
        criteria_version=CRITERIA_VERSION_V1,
        snapshot_id=snapshot_id,
        purpose_kind=PurposeKind.APPROVED_NAMED_PROGRAM,
        program_name=program_name,
        authorized_at=clock(),
    )


def authorize_legacy_migration(
    *, snapshot_id: str, canonical_bytes: bytes,
    clock: Callable[[], datetime],
) -> HoldingsPersistencePermitV1 | DenialToken:
    """Mint the migration-only permit for exactly one legacy capture.

    The permit binds to the domain-separated SHA-256 digest recomputed here
    from the canonical bytes; the caller's ``snapshot_id`` serves only as an
    equality check, so a permit can never bind a capture to a foreign ID.
    Callable only from the offline migration tool, never from composition.
    """
    recomputed = hashlib.sha256(
        _LEGACY_ID_DOMAIN + canonical_bytes).hexdigest()
    if snapshot_id != recomputed:
        return DenialToken(reason="invalid_snapshot_id")
    return HoldingsPersistencePermitV1(
        criteria_version=MIGRATION_CRITERIA_VERSION,
        snapshot_id=recomputed,
        purpose_kind=PurposeKind.APPROVED_MIGRATION,
        program_name=MIGRATION_PROGRAM_TOKEN,
        authorized_at=clock(),
    )


def authorize_pension_import(
    *, snapshot_id: str, program_name: str, clock: Callable[[], datetime],
) -> HoldingsPersistencePermitV1 | DenialToken:
    """Mint a pension-import permit. Only callable from pension_ingestion.py."""
    if not is_valid_uuid4_hex(snapshot_id):
        return DenialToken(reason="invalid_snapshot_id")
    if program_name not in PENSION_NAMED_PROGRAM_ALLOWLIST:
        return DenialToken(reason="unknown_program")
    return HoldingsPersistencePermitV1(
        criteria_version=PENSION_CRITERIA_VERSION,
        snapshot_id=snapshot_id,
        purpose_kind=PurposeKind.APPROVED_NAMED_PROGRAM,
        program_name=program_name,
        authorized_at=clock(),
    )
