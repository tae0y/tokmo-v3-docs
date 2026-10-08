"""Atomic JSON store for SEC/OpenDART disclosure review history.

One store instance owns one source file. The caller (an approved disclosure-
check program) supplies the typed fact plus explicit ``reviewed_at``; records
performs strict validation, duplicate/conflict detection, and atomic file
replacement. This is a single-writer, low-volume personal-Mac design.

If concurrent writers become a real requirement, replace this JSON adapter
with SQLite instead of adding lock/lease/retry machinery.
"""

import contextlib
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from tokmo.records.durability import fsync_directory
from tokmo.values import (
    OPENDART_SOURCE,
    SEC_SOURCE,
    OpenDartDisclosureFact,
    SaveStatus,
    SecFilingFact,
)

__all__ = ["DisclosureReviewReceipt", "DisclosureReviewStore"]

SCHEMA_VERSION = 1
MAX_STORE_BYTES = 16 * 1024 * 1024

SEC_RECORD_FIELDS = frozenset({
    "source", "ticker", "cik", "company_name",
    "accession_number", "form", "filing_date", "report_date",
    "primary_document", "is_xbrl", "observed_at", "reviewed_at",
})

OPENDART_RECORD_FIELDS = frozenset({
    "source", "stock_code", "corp_code", "corp_name",
    "corp_cls", "report_name", "receipt_number", "filer_name",
    "receipt_date", "remark", "observed_at", "reviewed_at",
})

_TOP_LEVEL_FIELDS = frozenset({"schema_version", "source", "records"})

# Nullable fields per source.
_SEC_NULLABLE = frozenset({"report_date", "is_xbrl"})
_OPENDART_NULLABLE = frozenset({"remark"})


@dataclass(frozen=True)
class DisclosureReviewReceipt:
    """Outcome of an append_review call.

    - SAVED: ``effective_reviewed_at`` is the caller-supplied value persisted.
    - DUPLICATE: it is the existing first stored ``reviewed_at``.
    - CONFLICT: it is the existing first stored ``reviewed_at``; no mutation.
    - FAILED: it is ``None``.
    """

    status: SaveStatus
    source_ref: str = field(repr=False)
    reason: str = field(repr=False)
    effective_reviewed_at: datetime | None = field(repr=False)


class DisclosureReviewStore:
    """Source-specific atomic JSON review-history store."""

    def __init__(self, path: Path, *, source: str) -> None:
        if source not in (SEC_SOURCE, OPENDART_SOURCE):
            raise ValueError(f"unsupported_source:{source}")
        self.path = path
        self.source = source

    # -- public API ---------------------------------------------------------

    def append_review(
        self,
        fact: SecFilingFact | OpenDartDisclosureFact,
        *,
        reviewed_at: datetime,
    ) -> DisclosureReviewReceipt:
        source_ref = getattr(fact, "source_ref", fact.__class__.__name__)
        error = self._validate_input(fact, reviewed_at)
        if error is not None:
            return DisclosureReviewReceipt(
                SaveStatus.FAILED, source_ref, error, None)

        try:
            records = self._load()
        except (ValueError, OSError):
            return DisclosureReviewReceipt(
                SaveStatus.FAILED, source_ref, "store_corrupt", None)

        existing = self._find_identity(records, fact)
        if existing is not None:
            if self._content_matches(existing, fact):
                first_reviewed = _parse_ts(existing["reviewed_at"])
                return DisclosureReviewReceipt(
                    SaveStatus.DUPLICATE, source_ref, "duplicate",
                    first_reviewed)
            first_reviewed = _parse_ts(existing["reviewed_at"])
            return DisclosureReviewReceipt(
                SaveStatus.CONFLICT, source_ref, "identity_conflict",
                first_reviewed)

        new_record = self._fact_to_record(fact, reviewed_at)
        try:
            if self.source == SEC_SOURCE:
                _validate_sec_record(new_record)
            else:
                _validate_opendart_record(new_record)
        except ValueError:
            return DisclosureReviewReceipt(
                SaveStatus.FAILED, source_ref, "invalid_fact", None)
        records.append(new_record)
        try:
            self._atomic_write(records)
        except OSError:
            return DisclosureReviewReceipt(
                SaveStatus.FAILED, source_ref, "store_write_failed", None)

        return DisclosureReviewReceipt(
            SaveStatus.SAVED, source_ref, "saved", reviewed_at)

    # -- validation ---------------------------------------------------------

    def _validate_input(
        self, fact: SecFilingFact | OpenDartDisclosureFact,
        reviewed_at: datetime,
    ) -> str | None:
        if not isinstance(reviewed_at, datetime):
            return "invalid_review"
        if reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
            return "invalid_review"

        if self.source == SEC_SOURCE:
            if not isinstance(fact, SecFilingFact):
                return "invalid_fact"
            if fact.source != SEC_SOURCE:
                return "invalid_fact"
            return self._validate_sec_fact(fact, reviewed_at)
        else:
            if not isinstance(fact, OpenDartDisclosureFact):
                return "invalid_fact"
            if fact.source != OPENDART_SOURCE:
                return "invalid_fact"
            return self._validate_dart_fact(fact, reviewed_at)

    @staticmethod
    def _validate_aware(dt: datetime) -> bool:
        return dt.tzinfo is not None and dt.utcoffset() is not None

    def _validate_sec_fact(
        self, fact: SecFilingFact, reviewed_at: datetime,
    ) -> str | None:
        if not self._validate_aware(fact.observed_at):
            return "invalid_fact"
        if reviewed_at < fact.observed_at:
            return "invalid_review"
        if len(fact.cik) != 10 or not fact.cik.isdigit():
            return "invalid_fact"
        return None

    def _validate_dart_fact(
        self, fact: OpenDartDisclosureFact, reviewed_at: datetime,
    ) -> str | None:
        if not self._validate_aware(fact.observed_at):
            return "invalid_fact"
        if reviewed_at < fact.observed_at:
            return "invalid_review"
        if len(fact.stock_code) != 6 or not fact.stock_code.isdigit():
            return "invalid_fact"
        if len(fact.corp_code) != 8 or not fact.corp_code.isdigit():
            return "invalid_fact"
        if len(fact.receipt_number) != 14 or not fact.receipt_number.isdigit():
            return "invalid_fact"
        return None

    # -- load / validate existing store ------------------------------------

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        if self.path.stat().st_size > MAX_STORE_BYTES:
            raise ValueError("store_corrupt")
        raw = self.path.read_bytes()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("store_corrupt")
        if set(payload) != _TOP_LEVEL_FIELDS:
            raise ValueError("store_corrupt")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("store_corrupt")
        if payload.get("source") != self.source:
            raise ValueError("store_corrupt")
        records = payload.get("records")
        if not isinstance(records, list):
            raise ValueError("store_corrupt")
        expected = SEC_RECORD_FIELDS if self.source == SEC_SOURCE \
            else OPENDART_RECORD_FIELDS
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("store_corrupt")
            if set(record) != expected:
                raise ValueError("store_corrupt")
            if self.source == SEC_SOURCE:
                _validate_sec_record(record)
            else:
                _validate_opendart_record(record)
        return records

    # -- identity and content matching -------------------------------------

    def _find_identity(
        self, records: list[dict],
        fact: SecFilingFact | OpenDartDisclosureFact,
    ) -> dict | None:
        if self.source == SEC_SOURCE and isinstance(fact, SecFilingFact):
            for r in records:
                if (r["cik"] == fact.cik
                        and r["accession_number"] == fact.accession_number):
                    return r
        elif self.source == OPENDART_SOURCE and isinstance(
                fact, OpenDartDisclosureFact):
            for r in records:
                if (r["corp_code"] == fact.corp_code
                        and r["receipt_number"] == fact.receipt_number):
                    return r
        return None

    @staticmethod
    def _content_matches(record: dict, fact) -> bool:
        """Compare non-identity metadata, excluding observed_at/reviewed_at."""
        if isinstance(fact, SecFilingFact):
            return (
                record["ticker"] == fact.ticker
                and record["company_name"] == fact.company_name
                and record["form"] == fact.form
                and record["filing_date"] == fact.filing_date.isoformat()
                and record["report_date"] == (
                    fact.report_date.isoformat() if fact.report_date else None)
                and record["primary_document"] == fact.primary_document
                and record["is_xbrl"] == fact.is_xbrl
            )
        return (
            record["stock_code"] == fact.stock_code
            and record["corp_name"] == fact.corp_name
            and record["corp_cls"] == fact.corp_cls
            and record["report_name"] == fact.report_name
            and record["filer_name"] == fact.filer_name
            and record["receipt_date"] == fact.receipt_date.isoformat()
            and record["remark"] == fact.remark
        )

    # -- serialisation ------------------------------------------------------

    def _fact_to_record(
        self, fact: SecFilingFact | OpenDartDisclosureFact,
        reviewed_at: datetime,
    ) -> dict:
        if isinstance(fact, SecFilingFact):
            return {
                "source": fact.source,
                "ticker": fact.ticker,
                "cik": fact.cik,
                "company_name": fact.company_name,
                "accession_number": fact.accession_number,
                "form": fact.form,
                "filing_date": fact.filing_date.isoformat(),
                "report_date": (fact.report_date.isoformat()
                                if fact.report_date else None),
                "primary_document": fact.primary_document,
                "is_xbrl": fact.is_xbrl,
                "observed_at": _canonical_ts(fact.observed_at),
                "reviewed_at": _canonical_ts(reviewed_at),
            }
        return {
            "source": fact.source,
            "stock_code": fact.stock_code,
            "corp_code": fact.corp_code,
            "corp_name": fact.corp_name,
            "corp_cls": fact.corp_cls,
            "report_name": fact.report_name,
            "receipt_number": fact.receipt_number,
            "filer_name": fact.filer_name,
            "receipt_date": fact.receipt_date.isoformat(),
            "remark": fact.remark,
            "observed_at": _canonical_ts(fact.observed_at),
            "reviewed_at": _canonical_ts(reviewed_at),
        }

    # -- atomic write -------------------------------------------------------

    def _atomic_write(self, records: list[dict]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "source": self.source,
            "records": records,
        }
        data = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.tmp")
        try:
            fd = os.open(
                temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb", closefd=False) as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temp, self.path)
            # Temp file was created with O_CREAT mode 0600 and fchmod'd;
            # os.replace preserves the inode mode, so no post-replace chmod.
            #
            # If fsync_directory fails here the canonical file is already
            # visible on disk (os.replace succeeded), but the directory
            # entry may not be durable across a crash.  The caller sees
            # FAILED/store_write_failed, yet a retry will correctly resolve
            # to DUPLICATE because the record is present and valid.
            fsync_directory(self.path.parent)
        except OSError:
            # Best-effort remove our temp file on pre-replace failure.
            with contextlib.suppress(OSError):
                temp.unlink(missing_ok=True)
            raise


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _canonical_ts(dt: datetime) -> str:
    """Canonical UTC with microseconds and +00:00 suffix."""
    utc = dt.astimezone(timezone.utc)
    return utc.isoformat(timespec="microseconds")


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _validate_sec_record(record: dict) -> None:
    """Strict semantic validation of a stored SEC review record."""
    _check_nonempty_str(record, SEC_RECORD_FIELDS - _SEC_NULLABLE)
    cik = record["cik"]
    if record["source"] != SEC_SOURCE or len(cik) != 10 or not cik.isdigit():
        raise ValueError("store_corrupt")
    if record["report_date"] is not None:
        if not isinstance(record["report_date"], str):
            raise ValueError("store_corrupt")
        date.fromisoformat(record["report_date"])
    if record["is_xbrl"] is not None and not isinstance(record["is_xbrl"], bool):
        raise ValueError("store_corrupt")
    date.fromisoformat(record["filing_date"])
    _check_canonical_utc(record["observed_at"])
    _check_canonical_utc(record["reviewed_at"])


def _validate_opendart_record(record: dict) -> None:
    """Strict semantic validation of a stored OpenDART review record."""
    _check_nonempty_str(record, OPENDART_RECORD_FIELDS - _OPENDART_NULLABLE)
    if record["source"] != OPENDART_SOURCE:
        raise ValueError("store_corrupt")
    for field, width in (
        ("stock_code", 6), ("corp_code", 8), ("receipt_number", 14),
    ):
        v = record[field]
        if len(v) != width or not v.isascii() or not v.isdigit():
            raise ValueError("store_corrupt")
    remark = record["remark"]
    if remark is not None and (not isinstance(remark, str) or not remark):
        raise ValueError("store_corrupt")
    date.fromisoformat(record["receipt_date"])
    _check_canonical_utc(record["observed_at"])
    _check_canonical_utc(record["reviewed_at"])


def _check_nonempty_str(record: dict, fields) -> None:
    for f in fields:
        v = record[f]
        if not isinstance(v, str) or not v:
            raise ValueError("store_corrupt")


def _check_canonical_utc(ts_str: str) -> None:
    """Ensure the stored timestamp is aware UTC with +00:00.

    Re-canonicalise the parsed datetime using the same ``timespec="microseconds"``
    rule as ``_canonical_ts`` and compare.  This catches offsets other than
    ``+00:00`` (e.g. ``+09:00``) and, by using the same canonical function,
    ensures the stored string is byte-identical to what ``_canonical_ts``
    would produce.
    """
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("store_corrupt")
    canonical = dt.astimezone(timezone.utc).isoformat(timespec="microseconds")
    if canonical != ts_str:
        raise ValueError("store_corrupt")
