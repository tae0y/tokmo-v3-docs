"""Atomic tracer JSON fact snapshot.

Fail-closed: any malformed store yields ``FAILED/store_corrupt`` plus a
diagnostic artifact; the existing file is never overwritten or reset.
"""

import json
import os
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from tokmo.records.durability import fsync_directory
from tokmo.values import (
    KRX_SOURCE,
    OPENDART_SOURCE,
    SEC_SOURCE,
    TOSS_SOURCE,
    WTI_SERIES,
    WTI_SOURCE,
    WTI_UNITS,
    KrxDailyTradeFact,
    OpenDartDisclosureFact,
    SaveResult,
    SaveStatus,
    SecFilingFact,
    TossAccountHoldingsFact,
    WtiFact,
    canonical_decimal,
)

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]

RECORD_FIELDS = frozenset(
    {
        "record_id",
        "source",
        "series",
        "period",
        "value",
        "units",
        "observed_at",
        "stored_at",
        "policy_version",
    }
)
# SEC Tracer 1a record schema; report_date and is_xbrl are nullable.
SEC_RECORD_FIELDS = frozenset(
    {
        "record_id",
        "source",
        "ticker",
        "cik",
        "company_name",
        "accession_number",
        "form",
        "filing_date",
        "report_date",
        "primary_document",
        "is_xbrl",
        "observed_at",
        "stored_at",
        "policy_version",
    }
)
_SEC_STRING_FIELDS = (
    "record_id",
    "source",
    "ticker",
    "cik",
    "company_name",
    "accession_number",
    "form",
    "filing_date",
    "primary_document",
    "observed_at",
    "stored_at",
    "policy_version",
)
# OpenDART Tracer 1a record schema; remark is nullable.
OPENDART_RECORD_FIELDS = frozenset(
    {
        "record_id",
        "source",
        "stock_code",
        "corp_code",
        "corp_name",
        "corp_cls",
        "report_name",
        "receipt_number",
        "filer_name",
        "receipt_date",
        "remark",
        "observed_at",
        "stored_at",
        "policy_version",
    }
)
_OPENDART_STRING_FIELDS = (
    "record_id",
    "source",
    "stock_code",
    "corp_code",
    "corp_name",
    "corp_cls",
    "report_name",
    "receipt_number",
    "filer_name",
    "receipt_date",
    "observed_at",
    "stored_at",
    "policy_version",
)
KRX_RECORD_FIELDS = frozenset(
    {
        "record_id",
        "source",
        "base_date",
        "issue_code",
        "issue_name",
        "market_name",
        "sector_type_name",
        "close_price",
        "change_from_previous",
        "fluctuation_rate",
        "open_price",
        "high_price",
        "low_price",
        "accumulated_volume",
        "accumulated_value",
        "market_cap",
        "listed_shares",
        "observed_at",
        "stored_at",
        "policy_version",
    }
)

# Toss account-current snapshot schema. Identity is the capture instant plus
# the account's position in that capture; no account identifier is ever
# stored, so the ordinal denotes a position and never a durable account key.
TOSS_RECORD_FIELDS = frozenset(
    {
        "record_id",
        "source",
        "captured_at",
        "account_ordinal",
        "account_count",
        "holdings",
        "stored_at",
        "policy_version",
    }
)
TOSS_HOLDING_FIELDS = frozenset(
    {
        "symbol",
        "quantity",
        "avg_price",
        "avg_price_field",
        "currency",
        "market_raw",
        "market_field",
        "name",
        "asset_type",
    }
)
_TOSS_STRING_FIELDS = (
    "record_id",
    "source",
    "captured_at",
    "stored_at",
    "policy_version",
)

# WTI records hold strings only; SEC records may carry None (report_date)
# and bool (is_xbrl); OpenDART records may carry None (remark); Toss records
# carry the ordinal ints and the holdings list.
FactRecord = dict[str, str | bool | int | list | None]

StorableFact = (
    WtiFact
    | SecFilingFact
    | OpenDartDisclosureFact
    | KrxDailyTradeFact
    | TossAccountHoldingsFact
)

MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024


class StoreCorruptError(ValueError):
    pass


class FactStore:
    def __init__(self, path: Path, *, clock: Clock, id_factory: IdFactory) -> None:
        self.path = path
        self.clock = clock
        self.id_factory = id_factory

    def records(self) -> list[FactRecord]:
        """Load all records; raises StoreCorruptError on any malformed shape
        or on any semantically invalid field value."""
        if not self.path.exists():
            return []
        if self.path.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise StoreCorruptError("store_corrupt")
        raw = self.path.read_bytes()
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(
            payload.get("records"), list
        ):
            raise StoreCorruptError("store_corrupt")
        for record in payload["records"]:
            if not isinstance(record, dict):
                raise StoreCorruptError("store_corrupt")
            if set(record) == RECORD_FIELDS:
                if not all(isinstance(record[field], str) for field in RECORD_FIELDS):
                    raise StoreCorruptError("store_corrupt")
                _validate_record_semantics(record)
            elif set(record) == SEC_RECORD_FIELDS:
                _validate_sec_record_semantics(record)
            elif set(record) == OPENDART_RECORD_FIELDS:
                _validate_opendart_record_semantics(record)
            elif set(record) == KRX_RECORD_FIELDS:
                _validate_krx_record(record)
            elif set(record) == TOSS_RECORD_FIELDS:
                _validate_toss_record_semantics(record)
            else:
                raise StoreCorruptError("store_corrupt")
        return payload["records"]

    def save(self, fact: StorableFact, *, policy_version: str) -> SaveResult:
        now = self.clock()
        try:
            records = self.records()
        except (ValueError, OSError):
            self._write_diagnostic(now)
            return SaveResult(SaveStatus.FAILED, None, "store_corrupt", now)

        existing = self._find_same_identity(records, fact)
        if existing is not None:
            if _same_content(existing, fact):
                return SaveResult(
                    SaveStatus.DUPLICATE, existing["record_id"], "duplicate", now
                )
            return SaveResult(
                SaveStatus.CONFLICT, existing["record_id"], "identity_conflict", now
            )

        record_id = self.id_factory()
        records.append(as_record(fact, record_id, now, policy_version))
        try:
            self._replace_snapshot({"records": records})
        except OSError:
            return SaveResult(SaveStatus.FAILED, record_id, "store_write_failed", now)
        return SaveResult(SaveStatus.SAVED, record_id, "saved", now)

    def save_batch(self, facts, *, policy_version: str) -> tuple[SaveResult, ...]:
        """Persist a whole multi-fact capture, or persist none of it.

        A capture spanning several accounts must never reach the store in
        pieces: a store holding some ordinals of a capture would be read back
        later as a complete but smaller portfolio. So the existing records are
        loaded once, every identity decision is made before any mutation, all
        records are appended in memory, and one snapshot replace commits them.
        Any refusal or write failure leaves the previous store exactly as it
        was, and the returned results say per fact what did not happen.
        """
        facts = tuple(facts)
        if not facts:
            return ()
        now = self.clock()
        try:
            records = self.records()
        except (ValueError, OSError):
            self._write_diagnostic(now)
            return tuple(
                SaveResult(SaveStatus.FAILED, None, "store_corrupt", now)
                for _fact in facts
            )

        if len({_identity_of(fact) for fact in facts}) != len(facts):
            return tuple(
                SaveResult(SaveStatus.FAILED, None, "batch_identity_conflict", now)
                for _fact in facts
            )

        decisions = [self._decide(records, fact, now) for fact in facts]
        if any(decision is not None for decision in decisions):
            return tuple(
                decision
                if decision is not None
                else SaveResult(SaveStatus.FAILED, None, "batch_not_applied", now)
                for decision in decisions
            )

        appended = list(records)
        record_ids = []
        for fact in facts:
            record_id = self.id_factory()
            record_ids.append(record_id)
            appended.append(as_record(fact, record_id, now, policy_version))
        try:
            self._replace_snapshot({"records": appended})
        except OSError:
            return tuple(
                SaveResult(SaveStatus.FAILED, record_id, "store_write_failed", now)
                for record_id in record_ids
            )
        return tuple(
            SaveResult(SaveStatus.SAVED, record_id, "saved", now)
            for record_id in record_ids
        )

    def _decide(
        self, records: list[FactRecord], fact: StorableFact, now: datetime
    ) -> SaveResult | None:
        """``None`` means this fact is free to be appended."""
        existing = self._find_same_identity(records, fact)
        if existing is None:
            return None
        if _same_content(existing, fact):
            return SaveResult(
                SaveStatus.DUPLICATE, existing["record_id"], "duplicate", now
            )
        return SaveResult(
            SaveStatus.CONFLICT, existing["record_id"], "identity_conflict", now
        )

    def _find_same_identity(
        self, records: list[FactRecord], fact: StorableFact
    ) -> FactRecord | None:
        keys, identity = _identity_of(fact)
        for record in records:
            if (
                all(key in record for key in keys)
                and tuple(record[key] for key in keys) == identity
            ):
                return record
        return None

    def _replace_snapshot(self, payload: dict) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = json.dumps(payload, separators=(",", ":")).encode()
        temp = self.path.with_name(f".{self.path.name}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, self.path)
        os.chmod(self.path, 0o600)
        fsync_directory(self.path.parent)

    def _write_diagnostic(self, now: datetime) -> None:
        """Best-effort marker; its own failure must not mask the FAILED result."""
        path = self.path.with_name(self.path.name + ".diagnostic.json")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(
                    {"reason": "store_corrupt", "detected_at": now.isoformat()}, handle
                )
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass


def _validate_record_semantics(record: FactRecord) -> None:
    """Every persisted field must carry valid semantic content, not just be a
    string: nonempty IDs/policy, the WTI source/series/units contract, ISO
    dates and timestamps, and a finite Decimal in canonical serialization."""
    if not record["record_id"] or not record["policy_version"]:
        raise StoreCorruptError("store_corrupt")
    if (record["source"], record["series"], record["units"]) != (
        WTI_SOURCE,
        WTI_SERIES,
        WTI_UNITS,
    ):
        raise StoreCorruptError("store_corrupt")
    try:
        date.fromisoformat(record["period"])
        datetime.fromisoformat(record["observed_at"])
        datetime.fromisoformat(record["stored_at"])
        value = Decimal(record["value"])
    except (ValueError, InvalidOperation):
        raise StoreCorruptError("store_corrupt") from None
    if not value.is_finite() or canonical_decimal(value) != record["value"]:
        raise StoreCorruptError("store_corrupt")


def _identity_of(fact: StorableFact) -> tuple[tuple[str, ...], tuple]:
    """The stored key that decides DUPLICATE/CONFLICT for one fact."""
    if isinstance(fact, WtiFact):
        return (
            ("source", "series", "period"),
            (fact.source, fact.series, fact.period.isoformat()),
        )
    if isinstance(fact, SecFilingFact):
        return (
            ("source", "cik", "accession_number"),
            (fact.source, fact.cik, fact.accession_number),
        )
    if isinstance(fact, TossAccountHoldingsFact):
        return (
            ("source", "captured_at", "account_ordinal"),
            (fact.source, fact.captured_at.isoformat(), fact.account_ordinal),
        )
    if isinstance(fact, KrxDailyTradeFact):
        return (
            ("source", "issue_code", "base_date"),
            (fact.source, fact.issue_code, fact.base_date.isoformat()),
        )
    return (
        ("source", "corp_code", "receipt_number"),
        (fact.source, fact.corp_code, fact.receipt_number),
    )


def _same_content(record: FactRecord, fact: StorableFact) -> bool:
    """Semantic content match: numerically equal value and identical units,
    or for filings/disclosures identical non-identity fields."""
    if isinstance(fact, TossAccountHoldingsFact):
        return record["account_count"] == fact.account_count and record[
            "holdings"
        ] == _holding_records(fact)
    if isinstance(fact, OpenDartDisclosureFact):
        return (
            record["stock_code"] == fact.stock_code
            and record["corp_name"] == fact.corp_name
            and record["corp_cls"] == fact.corp_cls
            and record["report_name"] == fact.report_name
            and record["filer_name"] == fact.filer_name
            and record["receipt_date"] == fact.receipt_date.isoformat()
            and record["remark"] == fact.remark
        )
    if isinstance(fact, KrxDailyTradeFact):
        expected = as_record(
            fact,
            record["record_id"],
            datetime.fromisoformat(record["stored_at"]),
            record["policy_version"],
        )
        ignored = {"record_id", "observed_at", "stored_at", "policy_version"}
        return all(
            record[name] == value
            for name, value in expected.items()
            if name not in ignored
        )
    if isinstance(fact, SecFilingFact):
        report_date = fact.report_date.isoformat() if fact.report_date else None
        return (
            record["ticker"] == fact.ticker
            and record["company_name"] == fact.company_name
            and record["form"] == fact.form
            and record["filing_date"] == fact.filing_date.isoformat()
            and record["report_date"] == report_date
            and record["primary_document"] == fact.primary_document
            and record["is_xbrl"] == fact.is_xbrl
        )
    try:
        stored_value = Decimal(record["value"])
    except InvalidOperation:
        return False
    return stored_value == fact.value and record["units"] == fact.units


def as_record(
    fact: StorableFact, record_id: str, now: datetime, policy_version: str
) -> FactRecord:
    if isinstance(fact, TossAccountHoldingsFact):
        return {
            "record_id": record_id,
            "source": fact.source,
            "captured_at": fact.captured_at.isoformat(),
            "account_ordinal": fact.account_ordinal,
            "account_count": fact.account_count,
            "holdings": _holding_records(fact),
            "stored_at": now.isoformat(),
            "policy_version": policy_version,
        }
    common = {
        "record_id": record_id,
        "source": fact.source,
        "observed_at": fact.observed_at.isoformat(),
        "stored_at": now.isoformat(),
        "policy_version": policy_version,
    }
    if isinstance(fact, KrxDailyTradeFact):
        return {
            **common,
            "base_date": fact.base_date.isoformat(),
            "issue_code": fact.issue_code,
            "issue_name": fact.issue_name,
            "market_name": fact.market_name,
            "sector_type_name": fact.sector_type_name,
            "close_price": canonical_decimal(fact.close_price),
            "change_from_previous": canonical_decimal(fact.change_from_previous),
            "fluctuation_rate": canonical_decimal(fact.fluctuation_rate),
            "open_price": canonical_decimal(fact.open_price),
            "high_price": canonical_decimal(fact.high_price),
            "low_price": canonical_decimal(fact.low_price),
            "accumulated_volume": fact.accumulated_volume,
            "accumulated_value": fact.accumulated_value,
            "market_cap": fact.market_cap,
            "listed_shares": fact.listed_shares,
        }
    if isinstance(fact, WtiFact):
        return {
            **common,
            "series": fact.series,
            "period": fact.period.isoformat(),
            "value": canonical_decimal(fact.value),
            "units": fact.units,
        }
    if isinstance(fact, SecFilingFact):
        return {
            **common,
            "ticker": fact.ticker,
            "cik": fact.cik,
            "company_name": fact.company_name,
            "accession_number": fact.accession_number,
            "form": fact.form,
            "filing_date": fact.filing_date.isoformat(),
            "report_date": fact.report_date.isoformat() if fact.report_date else None,
            "primary_document": fact.primary_document,
            "is_xbrl": fact.is_xbrl,
        }
    return {
        **common,
        "stock_code": fact.stock_code,
        "corp_code": fact.corp_code,
        "corp_name": fact.corp_name,
        "corp_cls": fact.corp_cls,
        "report_name": fact.report_name,
        "receipt_number": fact.receipt_number,
        "filer_name": fact.filer_name,
        "receipt_date": fact.receipt_date.isoformat(),
        "remark": fact.remark,
    }


def _holding_records(fact: TossAccountHoldingsFact) -> list[dict]:
    return [
        {
            "symbol": holding.symbol,
            "quantity": holding.quantity,
            "avg_price": holding.avg_price,
            "avg_price_field": holding.avg_price_field,
            "currency": holding.currency,
            "market_raw": holding.market_raw,
            "market_field": holding.market_field,
            "name": holding.name,
            "asset_type": holding.asset_type,
        }
        for holding in fact.holdings
    ]


def _validate_toss_record_semantics(record: dict) -> None:
    """Toss snapshots: nonempty identity strings, an ordinal inside the
    capture's account count, and holdings whose amounts round-trip as
    canonical finite Decimals."""
    for field in _TOSS_STRING_FIELDS:
        if not isinstance(record[field], str) or not record[field]:
            raise StoreCorruptError("store_corrupt")
    if record["source"] != TOSS_SOURCE:
        raise StoreCorruptError("store_corrupt")
    ordinal, count = record["account_ordinal"], record["account_count"]
    if type(ordinal) is not int or type(count) is not int:
        raise StoreCorruptError("store_corrupt")
    if not 1 <= ordinal <= count:
        raise StoreCorruptError("store_corrupt")
    try:
        datetime.fromisoformat(record["captured_at"])
        datetime.fromisoformat(record["stored_at"])
    except ValueError:
        raise StoreCorruptError("store_corrupt") from None
    if not isinstance(record["holdings"], list):
        raise StoreCorruptError("store_corrupt")
    for holding in record["holdings"]:
        _validate_toss_holding(holding)


def _validate_krx_record(record: dict) -> None:
    strings = (
        "record_id",
        "source",
        "base_date",
        "issue_code",
        "issue_name",
        "market_name",
        "sector_type_name",
        "close_price",
        "change_from_previous",
        "fluctuation_rate",
        "open_price",
        "high_price",
        "low_price",
        "observed_at",
        "stored_at",
        "policy_version",
    )
    if any(
        not isinstance(record[name], str)
        or (not record[name] and name != "sector_type_name")
        for name in strings
    ):
        raise StoreCorruptError("store_corrupt")
    if (
        record["source"] != KRX_SOURCE
        or record["issue_code"] != "000660"
        or record["market_name"] != "KOSPI"
    ):
        raise StoreCorruptError("store_corrupt")
    try:
        date.fromisoformat(record["base_date"])
        datetime.fromisoformat(record["observed_at"])
        datetime.fromisoformat(record["stored_at"])
        for name in (
            "close_price",
            "change_from_previous",
            "fluctuation_rate",
            "open_price",
            "high_price",
            "low_price",
        ):
            value = Decimal(record[name])
            if not value.is_finite() or canonical_decimal(value) != record[name]:
                raise ValueError
    except (ValueError, InvalidOperation):
        raise StoreCorruptError("store_corrupt") from None
    for name in (
        "accumulated_volume",
        "accumulated_value",
        "market_cap",
        "listed_shares",
    ):
        if type(record[name]) is not int:
            raise StoreCorruptError("store_corrupt")


def _validate_toss_holding(holding) -> None:
    if not isinstance(holding, dict) or set(holding) != TOSS_HOLDING_FIELDS:
        raise StoreCorruptError("store_corrupt")
    for field in ("symbol", "quantity", "avg_price", "avg_price_field"):
        if not isinstance(holding[field], str) or not holding[field]:
            raise StoreCorruptError("store_corrupt")
    for field in ("currency", "market_raw", "market_field", "name", "asset_type"):
        value = holding[field]
        if value is not None and (not isinstance(value, str) or not value):
            raise StoreCorruptError("store_corrupt")
    if (holding["market_raw"] is None) != (holding["market_field"] is None):
        raise StoreCorruptError("store_corrupt")
    for field in ("quantity", "avg_price"):
        try:
            amount = Decimal(holding[field])
        except InvalidOperation:
            raise StoreCorruptError("store_corrupt") from None
        if (
            not amount.is_finite()
            or amount < 0
            or canonical_decimal(amount) != holding[field]
        ):
            raise StoreCorruptError("store_corrupt")


def _validate_opendart_record_semantics(record: dict) -> None:
    """OpenDART records: nonempty strings everywhere except the nullable
    remark; exact-width numeric identity codes and ISO dates."""
    for field in _OPENDART_STRING_FIELDS:
        if not isinstance(record[field], str) or not record[field]:
            raise StoreCorruptError("store_corrupt")
    if record["source"] != OPENDART_SOURCE:
        raise StoreCorruptError("store_corrupt")
    for field, width in (("stock_code", 6), ("corp_code", 8), ("receipt_number", 14)):
        value = record[field]
        if len(value) != width or not value.isascii() or not value.isdigit():
            raise StoreCorruptError("store_corrupt")
    remark = record["remark"]
    if remark is not None and (not isinstance(remark, str) or not remark):
        raise StoreCorruptError("store_corrupt")
    try:
        date.fromisoformat(record["receipt_date"])
        datetime.fromisoformat(record["observed_at"])
        datetime.fromisoformat(record["stored_at"])
    except ValueError:
        raise StoreCorruptError("store_corrupt") from None


def _validate_sec_record_semantics(record: dict) -> None:
    """SEC records: nonempty strings everywhere except the nullable
    report_date (ISO date or None) and is_xbrl (bool or None)."""
    for field in _SEC_STRING_FIELDS:
        if not isinstance(record[field], str) or not record[field]:
            raise StoreCorruptError("store_corrupt")
    cik = record["cik"]
    if record["source"] != SEC_SOURCE or len(cik) != 10 or not cik.isdigit():
        raise StoreCorruptError("store_corrupt")
    if record["report_date"] is not None and not isinstance(record["report_date"], str):
        raise StoreCorruptError("store_corrupt")
    if record["is_xbrl"] is not None and not isinstance(record["is_xbrl"], bool):
        raise StoreCorruptError("store_corrupt")
    try:
        date.fromisoformat(record["filing_date"])
        if record["report_date"] is not None:
            date.fromisoformat(record["report_date"])
        datetime.fromisoformat(record["observed_at"])
        datetime.fromisoformat(record["stored_at"])
    except ValueError:
        raise StoreCorruptError("store_corrupt") from None
