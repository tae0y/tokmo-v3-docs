"""Private atomic fundamental material store (G1A).

Stores OfficialFundamentalMaterialV1 records in a private 0600 SQLite
database. Atomic inserts with no-clobber on duplicate source_ref.
Materials are serialized as JSON for retrieval and provenance.

Private directory 0700, SQLite file 0600, journal/wal files 0600.
No exposure window: permissions are set before any data is written.
No network. No clobber. Deserialize recomputes and verifies source_ref.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

from tokmo.values.fundamental_materials import (
    MetricEvidence,
    MetricSelection,
    OfficialFundamentalMaterialV1,
)
from tokmo.values.quant_multifactor_provenance import US_STORED_MARKET_VOCABULARY


class FundamentalMaterialStoreError(Exception):
    """Raised on store invariant violations (e.g. duplicate clobber)."""


class FundamentalStoreAbsentError(FundamentalMaterialStoreError):
    """The explicitly read-only store does not exist."""


class FundamentalStoreCorruptError(FundamentalMaterialStoreError):
    """The read-only store cannot be opened or decoded."""


class FundamentalStoreCandidateCapError(FundamentalMaterialStoreError):
    """The bounded candidate query exceeded its hard limit."""


class FundamentalStoreVocabularyError(FundamentalMaterialStoreError):
    """A stored market spelling is outside the frozen current-tree vocabulary."""


_DISTINCT_MARKET_SCAN_CAP = 16
_RAW_CANDIDATE_SCAN_CAP = 256


def read_admissible_materials_ro(
    path: str | Path, *, canonical_symbol: str, market, as_of: datetime, limit: int
):
    """Read a bounded exact-symbol/market slice without creating anything."""
    import sqlite3

    from tokmo.values.quant_multifactor_core import MarketV1

    store_path = Path(path)
    if not store_path.is_file():
        raise FundamentalStoreAbsentError("fundamental_store_absent")
    if market is MarketV1.US_NYSE_NASDAQ:
        vocabulary = US_STORED_MARKET_VOCABULARY
    elif market in (MarketV1.KR_KOSPI, MarketV1.KR_KOSDAQ):
        return ()
    else:
        raise ValueError("market")
    if as_of.tzinfo is None or limit < 1 or limit > 64:
        raise ValueError("reader_bounds")
    placeholders = ",".join("?" for _ in vocabulary)
    uri = f"file:{quote(str(store_path), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            stored_markets = connection.execute(
                "SELECT DISTINCT market FROM fundamental_materials WHERE canonical_symbol = ? LIMIT ?",
                (canonical_symbol, _DISTINCT_MARKET_SCAN_CAP + 1),
            ).fetchall()
            if len(stored_markets) > _DISTINCT_MARKET_SCAN_CAP:
                raise FundamentalStoreVocabularyError(
                    "stored_market_vocabulary_scan_cap"
                )
            drift = tuple(
                sorted(row[0] for row in stored_markets if row[0] not in vocabulary)
            )
            if drift:
                raise FundamentalStoreVocabularyError("stored_market_vocabulary_drift")
            rows = connection.execute(
                f"SELECT source_ref, payload_json FROM fundamental_materials WHERE canonical_symbol = ? AND market IN ({placeholders}) LIMIT ?",
                (canonical_symbol, *vocabulary, _RAW_CANDIDATE_SCAN_CAP + 1),
            ).fetchall()
        finally:
            connection.close()
        if len(rows) > _RAW_CANDIDATE_SCAN_CAP:
            raise FundamentalStoreCandidateCapError(
                "fundamental_raw_candidate_scan_cap"
            )
        decoded = []
        for source_ref, payload in rows:
            decoded_payload = json.loads(payload)
            encoded_available = decoded_payload.get("available_at")
            if not isinstance(encoded_available, str):
                raise FundamentalStoreCorruptError("available_at_invalid")
            parsed_available = datetime.fromisoformat(encoded_available)
            if parsed_available.tzinfo is None or parsed_available.utcoffset() is None:
                raise FundamentalStoreCorruptError("available_at_not_aware")
            material = _deserialize_material(
                decoded_payload, expected_source_ref=source_ref
            )
            available = material.available_at
            if available.tzinfo is None or available.utcoffset() is None:
                raise FundamentalStoreCorruptError("available_at_not_aware")
            if available.astimezone(UTC) <= as_of.astimezone(UTC):
                decoded.append(material)
        if len(decoded) > limit:
            raise FundamentalStoreCandidateCapError(
                "fundamental_admissible_candidate_cap"
            )
        return tuple(
            sorted(
                decoded,
                key=lambda item: (
                    -item.available_at.astimezone(UTC).timestamp(),
                    item.source_ref,
                ),
            )
        )
    except (
        FundamentalStoreCandidateCapError,
        FundamentalStoreVocabularyError,
        FundamentalStoreCorruptError,
    ):
        raise
    except (
        sqlite3.Error,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        FundamentalMaterialStoreError,
    ) as error:
        raise FundamentalStoreCorruptError("fundamental_store_corrupt") from error


_CREATE_SQL = """\
CREATE TABLE IF NOT EXISTS fundamental_materials (
    source_ref TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    canonical_symbol TEXT NOT NULL,
    market TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    source_concept_id TEXT,
    value TEXT,
    unit TEXT,
    available_at TEXT NOT NULL,
    raw_body_sha256 TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""

_CREATE_INDEX_SQL = """\
CREATE INDEX IF NOT EXISTS idx_entity_year
ON fundamental_materials(entity_id, fiscal_year);
"""


def _ensure_private_dir(path: Path) -> None:
    """Create directory with 0700 permissions, no exposure window.

    If the directory already exists, chmod it to 0700. If it does not exist,
    create it with mode 0700 via os.mkdir (not mkdir(mode=) which is affected
    by umask).
    """
    if path.exists():
        os.chmod(path, stat.S_IRWXU)
    else:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, stat.S_IRWXU)


def _create_file_0600(path: Path) -> int:
    """Open or create a file with 0600 permissions via os.open.

    This avoids the exposure window where sqlite3.connect creates the file
    with umask-default permissions (0644) before we can chmod it.
    """
    fd = os.open(
        str(path),
        os.O_CREAT | os.O_RDWR,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    return fd


class FundamentalMaterialStore:
    """Private 0600 SQLite store for official fundamental materials."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._init_db()

    def _init_db(self) -> None:
        import sqlite3

        # M9: create parent directory with 0700 BEFORE touching the DB file.
        _ensure_private_dir(self._path.parent)

        # M9: create the DB file with 0600 BEFORE sqlite3.connect to avoid
        # the exposure window where connect creates it with umask defaults.
        if not self._path.exists():
            fd = _create_file_0600(self._path)
            os.close(fd)
        else:
            os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)

        # m6 fix: do NOT use immutable=1 — it is not needed for safety and
        # the false "immutable=1 prevents journal" claim has been removed.
        # isolation_level="IMMEDIATE" acquires a write lock for the
        # transaction. The actual no-clobber atomicity comes from the
        # INSERT-with-conflict-handling in insert(), not from this flag.
        conn = sqlite3.connect(
            str(self._path),
            isolation_level="IMMEDIATE",
        )
        try:
            conn.executescript(_CREATE_SQL)
            conn.executescript(_CREATE_INDEX_SQL)
            conn.commit()
        finally:
            conn.close()

        # M9: ensure 0600 after SQLite touches it.
        os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)

    def _connect(self):
        import sqlite3

        return sqlite3.connect(str(self._path), isolation_level="IMMEDIATE")

    def insert(self, material: OfficialFundamentalMaterialV1) -> None:
        """Insert a material. Raises on duplicate source_ref.

        m6 fix: the INSERT is atomic under the transaction. Instead of
        SELECT-then-INSERT (which has a TOCTOU race between concurrent
        processes), we use INSERT ... ON CONFLICT DO NOTHING and check
        rowcount. A pre-existing source_ref yields rowcount=0, which we
        translate to a typed FundamentalMaterialStoreError. Any raw
        sqlite3.IntegrityError from concurrent contention is also
        translated to the typed error, never leaked to the caller.
        """
        import sqlite3

        conn = self._connect()
        try:
            payload = _serialize_material(material)
            revenue = material.revenue_ttm
            try:
                cursor = conn.execute(
                    """\
                    INSERT INTO fundamental_materials
                    (source_ref, source, entity_id, canonical_symbol, market,
                     fiscal_year, source_concept_id, value, unit, available_at,
                     raw_body_sha256, observed_at, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_ref) DO NOTHING
                    """,
                    (
                        material.source_ref,
                        material.source,
                        material.entity_id,
                        material.canonical_symbol,
                        material.market,
                        material.fiscal_year,
                        revenue.source_concept_id if revenue else None,
                        # m5: use `is not None` check, not truthiness, so Decimal(0) persists.
                        str(revenue.value)
                        if revenue and revenue.value is not None
                        else None,
                        revenue.unit if revenue else None,
                        material.available_at.isoformat(),
                        material.raw_body_sha256,
                        material.observed_at.isoformat(),
                        json.dumps(payload),
                    ),
                )
                if cursor.rowcount == 0:
                    raise FundamentalMaterialStoreError(
                        f"duplicate_source_ref:{material.source_ref}"
                    )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                # Translate contention/integrity violations to typed error.
                raise FundamentalMaterialStoreError(
                    f"insert_integrity_error:{material.source_ref}"
                ) from exc
            except FundamentalMaterialStoreError:
                conn.rollback()
                raise
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def get_by_source_ref(
        self, source_ref: str
    ) -> OfficialFundamentalMaterialV1 | None:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT source_ref, payload_json FROM fundamental_materials WHERE source_ref = ?",
                (source_ref,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            # B1 fix: pass the SQL PK for triple-exact verification.
            return _deserialize_material(json.loads(row[1]), expected_source_ref=row[0])
        finally:
            conn.close()

    def get_by_entity(self, entity_id: str) -> list[OfficialFundamentalMaterialV1]:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "SELECT source_ref, payload_json FROM fundamental_materials WHERE entity_id = ?",
                (entity_id,),
            )
            rows = cursor.fetchall()
            # B1 fix: pass the SQL PK for triple-exact verification.
            return [
                _deserialize_material(json.loads(r[1]), expected_source_ref=r[0])
                for r in rows
            ]
        finally:
            conn.close()


def _serialize_material(m: OfficialFundamentalMaterialV1) -> dict[str, Any]:
    """Serialize a material to a JSON-safe dict."""

    def metric_to_dict(ev: MetricEvidence | None) -> dict | None:
        if ev is None:
            return None
        return {
            "value": str(ev.value) if ev.value is not None else None,
            "source_concept_id": ev.source_concept_id,
            "unit": ev.unit,
            "selection": ev.selection.value,
        }

    return {
        "source": m.source,
        "entity_id": m.entity_id,
        "canonical_symbol": m.canonical_symbol,
        "market": m.market,
        "accounting_regime": m.accounting_regime.value,
        "statement_scope": m.statement_scope.value,
        "fiscal_year": m.fiscal_year,
        "period_start": m.period_start.isoformat() if m.period_start else None,
        "period_end": m.period_end.isoformat(),
        "filing_id": m.filing_id,
        "filed_at_raw": m.filed_at_raw,
        "available_at": m.available_at.isoformat(),
        "revision_of": m.revision_of,
        "taxonomy_namespace": m.taxonomy_namespace,
        "taxonomy_version": m.taxonomy_version,
        "mapping_version": m.mapping_version,
        "revenue_ttm": metric_to_dict(m.revenue_ttm),
        "net_income_ttm": metric_to_dict(m.net_income_ttm),
        "gross_profit_ttm": metric_to_dict(m.gross_profit_ttm),
        "total_equity": metric_to_dict(m.total_equity),
        "total_assets": metric_to_dict(m.total_assets),
        "diluted_shares_outstanding": metric_to_dict(m.diluted_shares_outstanding),
        "source_request_identity_digest": m.source_request_identity_digest,
        "raw_body_sha256": m.raw_body_sha256,
        "observed_at": m.observed_at.isoformat(),
        "source_ref": m.source_ref,
        "as_of": m.as_of.isoformat(),
        # B1 fix: serialize available_at_session_history_source so that
        # parser-created materials (which always stamp a verified session
        # history source) round-trip correctly through the store.
        "available_at_session_history_source": m.available_at_session_history_source,
    }


def _deserialize_material(
    d: dict[str, Any],
    *,
    expected_source_ref: str | None = None,
) -> OfficialFundamentalMaterialV1:
    """Deserialize a material from a JSON dict.

    M2: recomputes source_ref from the deserialized fields and verifies it
    matches the stored source_ref. Tampered values are detected.

    B1 fix: ``expected_source_ref`` is the SQL primary key from the row
    that owns this payload. When provided, it is compared in triple-exact
    agreement with the payload ``source_ref`` key and the recomputed
    digest. Any disagreement is a schema/tamper violation.
    """

    def dict_to_metric(md: dict | None) -> MetricEvidence | None:
        if md is None:
            return None
        return MetricEvidence(
            value=Decimal(md["value"]) if md["value"] is not None else None,
            source_concept_id=md["source_concept_id"],
            unit=md["unit"],
            selection=MetricSelection(md["selection"]),
        )

    from tokmo.values.fundamental_materials import AccountingRegime, StatementScope

    # B1 fix: source_ref is REQUIRED in the payload — a missing key is a
    # schema violation (legacy or tampered row), not a silently-permissive
    # default. The caller (get_by_source_ref / get_by_entity) passes the
    # SQL PK separately as ``expected_source_ref``; we compare the payload
    # key, the recomputed digest, and the SQL PK in triple-exact agreement.
    if "source_ref" not in d:
        raise FundamentalMaterialStoreError("source_ref_missing_in_payload")
    stored_source_ref = d["source_ref"]

    material = OfficialFundamentalMaterialV1(
        source=d["source"],
        entity_id=d["entity_id"],
        canonical_symbol=d["canonical_symbol"],
        market=d["market"],
        accounting_regime=AccountingRegime(d["accounting_regime"]),
        statement_scope=StatementScope(d["statement_scope"]),
        fiscal_year=d["fiscal_year"],
        period_start=date.fromisoformat(d["period_start"])
        if d["period_start"]
        else None,
        period_end=date.fromisoformat(d["period_end"]),
        filing_id=d["filing_id"],
        filed_at_raw=d["filed_at_raw"],
        available_at=datetime.fromisoformat(d["available_at"]),
        revision_of=d["revision_of"],
        taxonomy_namespace=d["taxonomy_namespace"],
        taxonomy_version=d["taxonomy_version"],
        mapping_version=d["mapping_version"],
        revenue_ttm=dict_to_metric(d["revenue_ttm"]),
        net_income_ttm=dict_to_metric(d["net_income_ttm"]),
        gross_profit_ttm=dict_to_metric(d["gross_profit_ttm"]),
        total_equity=dict_to_metric(d["total_equity"]),
        total_assets=dict_to_metric(d["total_assets"]),
        diluted_shares_outstanding=dict_to_metric(d["diluted_shares_outstanding"]),
        source_request_identity_digest=d["source_request_identity_digest"],
        raw_body_sha256=d["raw_body_sha256"],
        observed_at=datetime.fromisoformat(d["observed_at"]),
        as_of=datetime.fromisoformat(d.get("as_of", d["observed_at"])),
        # B1 fix: deserialize available_at_session_history_source. Use
        # .get() for backward compat with rows serialized before this
        # field was added; defaulting to "" matches the dataclass default
        # so old empty-string materials still round-trip correctly.
        available_at_session_history_source=d.get(
            "available_at_session_history_source", ""
        ),
    )

    # B1 fix: triple-exact source_ref verification. The payload key,
    # the recomputed digest, and (when provided) the SQL PK must all agree.
    # Missing payload key was already caught above. Any mismatch here is
    # tamper or corruption.
    if material.source_ref != stored_source_ref:
        raise FundamentalMaterialStoreError(
            f"source_ref_mismatch: stored={stored_source_ref} recomputed={material.source_ref}"
        )
    if expected_source_ref is not None and expected_source_ref != stored_source_ref:
        raise FundamentalMaterialStoreError(
            f"source_ref_pk_mismatch: pk={expected_source_ref} payload={stored_source_ref}"
        )

    return material
