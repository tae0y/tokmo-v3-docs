"""Canonical private store for official-membership observations."""

from __future__ import annotations

from dataclasses import fields
from datetime import date, datetime, timezone
import os
from pathlib import Path
import sqlite3
import time

from tokmo.records.canonical_observation_registry import (
    OFFICIAL_MEMBERSHIP_OBSERVATION_FAMILY,
    require_registered_family,
)
from tokmo.records.durability import fsync_directory
from tokmo.records.private_sqlite import (
    MIN_SQLITE_VERSION,
    acquire_immediate_transaction,
    assert_no_sidecars_after_close,
    assert_private_sidecars_while_open,
    connect_private,
    prepare_private_path,
    private_creation,
    table_is_strict,
)
from tokmo.values.official_membership_observation import (
    ListingObservationV1,
    ListingVenueV1,
    MembershipMaterialV1,
    MembershipPolicyV1,
    MembershipSectorV1,
    ObservationInstantV1,
    ObservationSourceKindV1,
    OfficialMembershipAcquisitionV1,
    OfficialMembershipStoreReceiptV1,
    OfficialSicSnapshotV1,
    RequestIdentityV1,
    SecIssuerObservationV1,
    SecRegistryObservationV1,
    SecurityClassV1,
    SourceReceiptLinkV1,
)

_DDL = (
    """CREATE TABLE storage_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE acquisitions (
        acquisition_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        acquisition_json BLOB NOT NULL,
        stored_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE payloads (
        acquisition_id TEXT NOT NULL REFERENCES acquisitions(acquisition_id)
            ON DELETE RESTRICT,
        source_kind TEXT NOT NULL,
        subject_key TEXT NOT NULL,
        request_identity_json BLOB NOT NULL,
        started_count INTEGER NOT NULL,
        observed_at TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        payload_bytes BLOB NOT NULL,
        PRIMARY KEY (acquisition_id, source_kind, subject_key)
    ) STRICT""",
    """CREATE TABLE materials (
        acquisition_id TEXT NOT NULL REFERENCES acquisitions(acquisition_id)
            ON DELETE RESTRICT,
        symbol TEXT NOT NULL,
        material_json BLOB NOT NULL,
        PRIMARY KEY (acquisition_id, symbol)
    ) STRICT""",
    """CREATE TABLE receipts (
        acquisition_id TEXT PRIMARY KEY REFERENCES acquisitions(acquisition_id)
            ON DELETE RESTRICT,
        receipt_json BLOB NOT NULL
    ) STRICT""",
)
_METADATA = {
    ("official_membership_schema_version", "1"),
    ("official_membership_adapter", "tokmo-official-membership-observation-v1"),
}
_LEGACY_READONLY_METADATA = {
    ("official_membership_schema_version", "0"),
    ("official_membership_adapter", "tokmo-official-membership-observation-v1"),
}


def _plain(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, MembershipPolicyV1 | ListingVenueV1
                  | MembershipSectorV1 | ObservationSourceKindV1):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {
            field.name: _plain(getattr(value, field.name))
            for field in fields(value)
        }
    return value


def _canonical_json(value) -> bytes:
    from tokmo.values.material_pack import canonical_encode

    return canonical_encode(_plain(value))


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decode_datetime(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _decode_date(text: str) -> date:
    return date.fromisoformat(text)


def _decode_request_identity(data: dict) -> RequestIdentityV1:
    return RequestIdentityV1(
        method=data["method"],
        host=data["host"],
        path_template=data["path_template"],
        source_schema_version=data["source_schema_version"],
    )


def _decode_observation_instant(data: dict) -> ObservationInstantV1:
    return ObservationInstantV1(
        ObservationSourceKindV1(data["source_kind"]),
        _decode_datetime(data["observed_at"]),
    )


def _decode_listing(data: dict) -> ListingObservationV1:
    return ListingObservationV1(
        symbol=data["symbol"],
        source_kind=ObservationSourceKindV1(data["source_kind"]),
        venue=ListingVenueV1(data["venue"]),
        venue_token=data["venue_token"],
        security_name_hash=data["security_name_hash"],
        security_class=SecurityClassV1(data["security_class"]),
        observed_at=_decode_datetime(data["observed_at"]),
        directory_creation_time=_decode_datetime(data["directory_creation_time"]),
        body_sha256=data["body_sha256"],
        row_sha256=data["row_sha256"],
        source_schema_version=data["source_schema_version"],
        test_issue_token=data["test_issue_token"],
        financial_status_token=data["financial_status_token"],
        etf_token=data["etf_token"],
    )


def _decode_registry(data: dict) -> SecRegistryObservationV1:
    return SecRegistryObservationV1(
        symbol=data["symbol"],
        cik=data["cik"],
        exchange_token=data["exchange_token"],
        name_hash=data["name_hash"],
        observed_at=_decode_datetime(data["observed_at"]),
        body_sha256=data["body_sha256"],
        row_sha256=data["row_sha256"],
        source_schema_version=data["source_schema_version"],
    )


def _decode_issuer(data: dict) -> SecIssuerObservationV1:
    return SecIssuerObservationV1(
        cik=data["cik"],
        symbol=data["symbol"],
        exchange_token=data["exchange_token"],
        sic_code=data["sic_code"],
        observed_at=_decode_datetime(data["observed_at"]),
        body_sha256=data["body_sha256"],
        source_schema_version=data["source_schema_version"],
    )


def _decode_sic_snapshot(data: dict) -> OfficialSicSnapshotV1:
    return OfficialSicSnapshotV1(
        snapshot_version=data["snapshot_version"],
        classification=data["classification"],
        codes=tuple(data["codes"]),
        documentation_ref=data["documentation_ref"],
    )


def _decode_acquisition(data: dict) -> OfficialMembershipAcquisitionV1:
    return OfficialMembershipAcquisitionV1(
        acquisition_id=data["acquisition_id"],
        membership_policy=MembershipPolicyV1(data["membership_policy"]),
        security_policy_version=data["security_policy_version"],
        source_contract_version=data["source_contract_version"],
        sic_snapshot=_decode_sic_snapshot(data["sic_snapshot"]),
        listings=tuple(_decode_listing(item) for item in data["listings"]),
        registry_rows=tuple(
            _decode_registry(item) for item in data["registry_rows"]
        ),
        issuer_rows=tuple(_decode_issuer(item) for item in data["issuer_rows"]),
    )


def _decode_material(data: dict) -> MembershipMaterialV1:
    return MembershipMaterialV1(
        symbol=data["symbol"],
        cik=data["cik"],
        venue=ListingVenueV1(data["venue"]),
        sector=MembershipSectorV1(data["sector"]),
        listed_at_as_of=data["listed_at_as_of"],
        membership_as_of=_decode_date(data["membership_as_of"]),
        acquisition_id=data["acquisition_id"],
        membership_policy=MembershipPolicyV1(data["membership_policy"]),
        membership_holdings_digest=data["membership_holdings_digest"],
        security_policy_version=data["security_policy_version"],
        sic_snapshot_version=data["sic_snapshot_version"],
        sic_code=data["sic_code"],
        observation_instants=tuple(
            _decode_observation_instant(item)
            for item in data["observation_instants"]
        ),
        listing_source_ref=data["listing_source_ref"],
        registry_source_ref=data["registry_source_ref"],
        issuer_source_ref=data["issuer_source_ref"],
        sic_snapshot_ref=data["sic_snapshot_ref"],
    )


def _decode_source_receipt(data: dict) -> SourceReceiptLinkV1:
    return SourceReceiptLinkV1(
        source_kind=ObservationSourceKindV1(data["source_kind"]),
        subject_key=data["subject_key"],
        request_identity=_decode_request_identity(data["request_identity"]),
        started_count=data["started_count"],
        observed_at=_decode_datetime(data["observed_at"]),
        payload_sha256=data["payload_sha256"],
    )


def _decode_receipt(data: dict) -> OfficialMembershipStoreReceiptV1:
    return OfficialMembershipStoreReceiptV1(
        status=data["status"],
        acquisition_id=data["acquisition_id"],
        reason=data["reason"],
        promoted_symbols=tuple(data["promoted_symbols"]),
        stored_at=_decode_datetime(data["stored_at"]),
        source_receipts=tuple(
            _decode_source_receipt(item) for item in data["source_receipts"]
        ),
    )


class OfficialMembershipObservationStore:
    def __init__(
        self,
        path: Path,
        *,
        family_name: str = OFFICIAL_MEMBERSHIP_OBSERVATION_FAMILY,
        stamp_factory=None,
    ) -> None:
        require_registered_family(
            family_name,
            adapter_type=type(self).__name__,
        )
        self._path = Path(path)
        self._stamp_factory = (
            stamp_factory
            if stamp_factory is not None
            else lambda: datetime.fromtimestamp(time.time(), tz=timezone.utc)
        )

    def _initialize(self, connection: sqlite3.Connection) -> None:
        for statement in _DDL:
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO storage_metadata(key, value) VALUES(?, ?)",
            tuple(sorted(_METADATA)),
        )

    def _validate(
        self,
        connection: sqlite3.Connection,
        *,
        writable: bool,
    ) -> None:
        if not table_is_strict(connection, "storage_metadata"):
            raise ValueError("schema_mismatch")
        metadata = set(connection.execute("SELECT key, value FROM storage_metadata"))
        if metadata == _METADATA:
            return
        if not writable and metadata == _LEGACY_READONLY_METADATA:
            return
        raise ValueError("schema_mismatch")

    def _open_readonly(self) -> sqlite3.Connection:
        safety = prepare_private_path(self._path)
        if safety.unsafe:
            raise ValueError("private_path")
        return connect_private(self._path, mode="ro")

    def _close_write(self, connection: sqlite3.Connection) -> None:
        connection.close()
        fsync_directory(self._path.parent)
        if assert_no_sidecars_after_close(self._path):
            raise ValueError("sqlite_sidecar_present")

    def _rollback_if_open(self, connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            connection.execute("ROLLBACK")

    def _open(self) -> tuple[sqlite3.Connection, bool]:
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise ValueError("sqlite_version")
        safety = prepare_private_path(self._path)
        if safety.unsafe:
            raise ValueError("private_path")
        new_file = not os.path.lexists(self._path)
        connection = None
        try:
            if new_file:
                with private_creation():
                    connection = connect_private(self._path)
                    os.chmod(self._path, 0o600)
            else:
                connection = connect_private(self._path)
            if new_file:
                self._initialize(connection)
                assert_private_sidecars_while_open(self._path)
                fsync_directory(self._path.parent)
            self._validate(connection, writable=True)
            return connection, new_file
        except BaseException:
            if connection is not None:
                connection.close()
            raise

    def _build_receipt(
        self,
        *,
        status: str,
        acquisition_id: str,
        reason: str,
        promoted_symbols: tuple[str, ...],
        payloads,
        stored_at: datetime,
    ) -> OfficialMembershipStoreReceiptV1:
        return OfficialMembershipStoreReceiptV1(
            status=status,
            acquisition_id=acquisition_id,
            reason=reason,
            promoted_symbols=tuple(sorted(promoted_symbols)),
            stored_at=stored_at,
            source_receipts=tuple(sorted((
                SourceReceiptLinkV1(
                    source_kind=item.source_kind,
                    subject_key=item.subject_key,
                    request_identity=item.request_identity,
                    started_count=item.started_count,
                    observed_at=item.observed_at,
                    payload_sha256=item.payload_sha256,
                ) for item in payloads
            ), key=lambda item: (item.source_kind.value, item.subject_key))),
        )

    def _existing_state(self, connection: sqlite3.Connection, acquisition_id: str):
        acquisition_row = connection.execute(
            "SELECT status, acquisition_json FROM acquisitions WHERE acquisition_id=?",
            (acquisition_id,),
        ).fetchone()
        if acquisition_row is None:
            return None
        payload_rows = tuple(connection.execute(
            "SELECT source_kind, subject_key, request_identity_json, started_count, "
            "observed_at, payload_sha256, payload_bytes "
            "FROM payloads WHERE acquisition_id=? ORDER BY source_kind, subject_key",
            (acquisition_id,),
        ))
        material_rows = tuple(connection.execute(
            "SELECT symbol, material_json FROM materials WHERE acquisition_id=? "
            "ORDER BY symbol",
            (acquisition_id,),
        ))
        receipt_row = connection.execute(
            "SELECT receipt_json FROM receipts WHERE acquisition_id=?",
            (acquisition_id,),
        ).fetchone()
        return acquisition_row, payload_rows, material_rows, receipt_row

    def append_success(
        self,
        *,
        acquisition: OfficialMembershipAcquisitionV1,
        payloads: tuple,
        materials: tuple[MembershipMaterialV1, ...],
    ) -> OfficialMembershipStoreReceiptV1:
        if not materials:
            raise ValueError("materials")
        if any(item.acquisition_id != acquisition.acquisition_id for item in materials):
            raise ValueError("materials")
        if any(item.acquisition_id != acquisition.acquisition_id for item in payloads):
            raise ValueError("payloads")
        stored_at = self._stamp_factory()
        desired_receipt = self._build_receipt(
            status="SAVED",
            acquisition_id=acquisition.acquisition_id,
            reason="",
            promoted_symbols=tuple(item.symbol for item in materials),
            payloads=payloads,
            stored_at=stored_at,
        )
        acquisition_json = _canonical_json(acquisition)
        payload_json_rows = tuple(
            (
                item.source_kind.value,
                item.subject_key,
                _canonical_json(item.request_identity),
                item.started_count,
                _utc(item.observed_at),
                item.payload_sha256,
                item.payload_bytes,
            )
            for item in sorted(payloads, key=lambda item: (item.source_kind.value, item.subject_key))
        )
        material_json_rows = tuple(
            (item.symbol, _canonical_json(item))
            for item in sorted(materials, key=lambda item: item.symbol)
        )
        receipt_json = _canonical_json(desired_receipt)
        connection, _ = self._open()
        try:
            acquire_immediate_transaction(connection)
            existing = self._existing_state(connection, acquisition.acquisition_id)
            if existing is not None:
                status, existing_acquisition = existing[0]
                same = (
                    status == "SUCCEEDED"
                    and existing_acquisition == acquisition_json
                    and tuple(
                        (row[0], row[1], row[2], row[3], row[4], row[5], row[6])
                        for row in existing[1]
                    ) == payload_json_rows
                    and tuple((row[0], row[1]) for row in existing[2]) == material_json_rows
                )
                connection.execute("ROLLBACK")
                return self._build_receipt(
                    status="DUPLICATE" if same else "CONFLICT",
                    acquisition_id=acquisition.acquisition_id,
                    reason="duplicate" if same else "conflict",
                    promoted_symbols=tuple(item.symbol for item in materials),
                    payloads=payloads,
                    stored_at=stored_at,
                )
            connection.execute(
                "INSERT INTO acquisitions VALUES (?, 'SUCCEEDED', ?, ?)",
                (acquisition.acquisition_id, acquisition_json, _utc(stored_at)),
            )
            connection.executemany(
                "INSERT INTO payloads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        acquisition.acquisition_id,
                        source_kind,
                        subject_key,
                        request_identity_json,
                        started_count,
                        observed_at,
                        payload_sha256,
                        payload_bytes,
                    )
                    for (
                        source_kind,
                        subject_key,
                        request_identity_json,
                        started_count,
                        observed_at,
                        payload_sha256,
                        payload_bytes,
                    ) in payload_json_rows
                ),
            )
            connection.executemany(
                "INSERT INTO materials VALUES (?, ?, ?)",
                (
                    (acquisition.acquisition_id, symbol, material_json)
                    for symbol, material_json in material_json_rows
                ),
            )
            connection.execute(
                "INSERT INTO receipts VALUES (?, ?)",
                (acquisition.acquisition_id, receipt_json),
            )
            assert_private_sidecars_while_open(self._path)
            connection.execute("COMMIT")
            return desired_receipt
        except BaseException:
            self._rollback_if_open(connection)
            raise
        finally:
            self._close_write(connection)

    def append_failure(
        self,
        *,
        acquisition: OfficialMembershipAcquisitionV1,
        payloads: tuple,
        reason: str,
        detail: str,
    ) -> OfficialMembershipStoreReceiptV1:
        del detail
        if any(item.acquisition_id != acquisition.acquisition_id for item in payloads):
            raise ValueError("payloads")
        stored_at = self._stamp_factory()
        receipt = self._build_receipt(
            status="SAVED",
            acquisition_id=acquisition.acquisition_id,
            reason=reason,
            promoted_symbols=(),
            payloads=payloads,
            stored_at=stored_at,
        )
        acquisition_json = _canonical_json(acquisition)
        payload_json_rows = tuple(
            (
                item.source_kind.value,
                item.subject_key,
                _canonical_json(item.request_identity),
                item.started_count,
                _utc(item.observed_at),
                item.payload_sha256,
                item.payload_bytes,
            )
            for item in sorted(payloads, key=lambda item: (item.source_kind.value, item.subject_key))
        )
        receipt_json = _canonical_json(receipt)
        connection, _ = self._open()
        try:
            acquire_immediate_transaction(connection)
            existing = self._existing_state(connection, acquisition.acquisition_id)
            if existing is not None:
                status, existing_acquisition = existing[0]
                same = (
                    status == "FAILED"
                    and existing_acquisition == acquisition_json
                    and tuple(
                        (row[0], row[1], row[2], row[3], row[4], row[5], row[6])
                        for row in existing[1]
                    ) == payload_json_rows
                    and not existing[2]
                )
                connection.execute("ROLLBACK")
                return self._build_receipt(
                    status="DUPLICATE" if same else "CONFLICT",
                    acquisition_id=acquisition.acquisition_id,
                    reason="duplicate" if same else "conflict",
                    promoted_symbols=(),
                    payloads=payloads,
                    stored_at=stored_at,
                )
            connection.execute(
                "INSERT INTO acquisitions VALUES (?, 'FAILED', ?, ?)",
                (acquisition.acquisition_id, acquisition_json, _utc(stored_at)),
            )
            connection.executemany(
                "INSERT INTO payloads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        acquisition.acquisition_id,
                        source_kind,
                        subject_key,
                        request_identity_json,
                        started_count,
                        observed_at,
                        payload_sha256,
                        payload_bytes,
                    )
                    for (
                        source_kind,
                        subject_key,
                        request_identity_json,
                        started_count,
                        observed_at,
                        payload_sha256,
                        payload_bytes,
                    ) in payload_json_rows
                ),
            )
            connection.execute(
                "INSERT INTO receipts VALUES (?, ?)",
                (acquisition.acquisition_id, receipt_json),
            )
            assert_private_sidecars_while_open(self._path)
            connection.execute("COMMIT")
            return receipt
        except BaseException:
            self._rollback_if_open(connection)
            raise
        finally:
            self._close_write(connection)

    def read_acquisition(
        self,
        acquisition_id: str,
    ) -> OfficialMembershipAcquisitionV1 | None:
        if not self._path.exists():
            return None
        connection = self._open_readonly()
        try:
            self._validate(connection, writable=False)
            row = connection.execute(
                "SELECT acquisition_json FROM acquisitions WHERE acquisition_id=?",
                (acquisition_id,),
            ).fetchone()
            if row is None:
                return None
            import json

            return _decode_acquisition(json.loads(row[0].decode("utf-8")))
        finally:
            connection.close()

    def read_material(
        self,
        acquisition_id: str,
        symbol: str,
        *,
        require_policy: str | None = None,
    ) -> MembershipMaterialV1 | None:
        if not self._path.exists():
            return None
        connection = self._open_readonly()
        try:
            self._validate(connection, writable=False)
            row = connection.execute(
                "SELECT material_json FROM materials WHERE acquisition_id=? AND symbol=?",
                (acquisition_id, symbol),
            ).fetchone()
            if row is None:
                return None
            import json

            material = _decode_material(json.loads(row[0].decode("utf-8")))
            if require_policy is not None and material.membership_policy.value != require_policy:
                return None
            return material
        finally:
            connection.close()

    def read_materials(self, acquisition_id: str) -> tuple[MembershipMaterialV1, ...]:
        if not self._path.exists():
            return ()
        connection = self._open_readonly()
        try:
            self._validate(connection, writable=False)
            import json

            rows = connection.execute(
                "SELECT material_json FROM materials WHERE acquisition_id=? ORDER BY symbol",
                (acquisition_id,),
            ).fetchall()
            return tuple(
                _decode_material(json.loads(row[0].decode("utf-8")))
                for row in rows
            )
        finally:
            connection.close()

    def read_receipt(
        self,
        acquisition_id: str,
    ) -> OfficialMembershipStoreReceiptV1 | None:
        if not self._path.exists():
            return None
        connection = self._open_readonly()
        try:
            self._validate(connection, writable=False)
            row = connection.execute(
                "SELECT receipt_json FROM receipts WHERE acquisition_id=?",
                (acquisition_id,),
            ).fetchone()
            if row is None:
                return None
            import json

            return _decode_receipt(json.loads(row[0].decode("utf-8")))
        finally:
            connection.close()
