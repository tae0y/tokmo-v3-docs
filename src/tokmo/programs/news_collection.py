"""Sequential NEWS collection dispatch with durable request-start accounting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import os
import sqlite3
from typing import Any, Callable, Mapping, Protocol

from tokmo.records.private_sqlite import (
    MIN_SQLITE_VERSION,
    acquire_immediate_transaction,
    connect_private,
    prepare_private_path,
    private_creation,
    table_is_strict,
)
from tokmo.values.external_news import canonical_utc
from tokmo.values.news_connectors import NewsFetchResult, NewsFetchStatus
from tokmo.values.news_collection_diagnostics import exception_cause


class NewsAdapter(Protocol):
    def fetch(self, request: Any, *, observed_at: datetime) -> NewsFetchResult: ...


@dataclass(frozen=True)
class NewsCollectionJob:
    connector_id: str
    request: object
    target_ref: str
    request_fingerprint: str

    def __post_init__(self) -> None:
        for name in ("connector_id", "target_ref"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip() or len(value) > 500:
                raise ValueError(f"{name} must be non-empty and bounded")
        fingerprint = self.request_fingerprint
        if (type(fingerprint) is not str or len(fingerprint) != 64
                or fingerprint != fingerprint.lower()
                or any(ch not in "0123456789abcdef" for ch in fingerprint)):
            raise ValueError("request_fingerprint must be lowercase sha256")


@dataclass(frozen=True)
class NewsCollectionJobResult:
    connector_id: str
    target_ref: str
    result: NewsFetchResult


@dataclass(frozen=True)
class NewsCollectionResult:
    campaign_id: str
    results: tuple[NewsCollectionJobResult, ...]


Reservation = Callable[[str, str, str, datetime], int]


class NewsCollectionManager:
    def __init__(self, *, adapters: Mapping[str, NewsAdapter], reservation: Reservation):
        self._adapters = dict(adapters)
        self._reservation = reservation

    def collect(
        self, campaign_id: str, jobs: tuple[NewsCollectionJob, ...], *,
        observed_at: datetime,
    ) -> NewsCollectionResult:
        if type(campaign_id) is not str or not campaign_id.strip() or len(campaign_id) > 200:
            raise ValueError("campaign_id must be non-empty and bounded")
        canonical_utc(observed_at, "observed_at")
        outcomes = []
        for job in jobs:
            adapter = self._adapters.get(job.connector_id)
            if adapter is None:
                raise ValueError("unknown connector_id")
            try:
                self._reservation(
                    campaign_id, job.connector_id, job.request_fingerprint,
                    observed_at,
                )
            except Exception as error:
                result = NewsFetchResult(NewsFetchStatus.CONFIG_UNAVAILABLE,
                    failure_class=exception_cause(error), failure_stage="RESERVE")
            else:
                result = adapter.fetch(job.request, observed_at=observed_at)
            outcomes.append(NewsCollectionJobResult(
                job.connector_id, job.target_ref, result,
            ))
        return NewsCollectionResult(campaign_id, tuple(outcomes))


_LEDGER_SCHEMA = """
CREATE TABLE fetch_attempts (
 campaign_id TEXT NOT NULL CHECK(campaign_id<>''),
 connector_id TEXT NOT NULL CHECK(connector_id<>''),
 request_fingerprint TEXT NOT NULL CHECK(
   length(request_fingerprint)=64 AND request_fingerprint=lower(request_fingerprint)
   AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
 sequence INTEGER NOT NULL CHECK(sequence>=1),
 reserved_at TEXT NOT NULL CHECK(length(reserved_at)=32),
 PRIMARY KEY(campaign_id, sequence)
) STRICT;
"""


class FetchAttemptLedger:
    """Private append-only ledger; a reserved start is never refunded."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def bootstrap(self, *, now: datetime) -> None:
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise RuntimeError("SQLite >= 3.37 required")
        canonical_utc(now, "now")
        safety = prepare_private_path(self.path)
        if safety.unsafe:
            raise OSError(safety.reason)
        created = not self.path.exists()
        with private_creation():
            connection = connect_private(self.path, mode="rwc")
        try:
            if created:
                os.chmod(self.path, 0o600)
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if not tables:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(_LEDGER_SCHEMA)
                connection.execute("COMMIT")
            elif tables != [("fetch_attempts",)] or not table_is_strict(
                connection, "fetch_attempts"
            ):
                raise RuntimeError("schema_mismatch")
        finally:
            connection.close()

    def reserve(
        self, campaign_id: str, connector_id: str,
        request_fingerprint: str, reserved_at: datetime,
    ) -> int:
        job = NewsCollectionJob(
            connector_id, object(), "ledger-validation", request_fingerprint,
        )
        del job
        if type(campaign_id) is not str or not campaign_id.strip() or len(campaign_id) > 200:
            raise ValueError("campaign_id must be non-empty and bounded")
        stamp = canonical_utc(reserved_at, "reserved_at")
        safety = prepare_private_path(self.path)
        if safety.unsafe:
            raise OSError(safety.reason)
        connection = connect_private(self.path, mode="rw")
        try:
            if not table_is_strict(connection, "fetch_attempts"):
                raise RuntimeError("schema_mismatch")
            acquire_immediate_transaction(connection)
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM fetch_attempts WHERE campaign_id=?",
                (campaign_id,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO fetch_attempts VALUES(?,?,?,?,?)",
                (campaign_id, connector_id, request_fingerprint, sequence, stamp),
            )
            connection.execute("COMMIT")
            return int(sequence)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def reserve_bounded(
        self, campaign_id: str, connector_id: str,
        request_fingerprint: str, reserved_at: datetime, *, ceiling: int,
    ) -> int:
        if type(ceiling) is not int or ceiling < 1:
            raise ValueError("ceiling must be positive")
        NewsCollectionJob(
            connector_id, object(), "ledger-validation", request_fingerprint,
        )
        if type(campaign_id) is not str or not campaign_id.strip() or len(campaign_id) > 200:
            raise ValueError("campaign_id must be non-empty and bounded")
        stamp = canonical_utc(reserved_at, "reserved_at")
        safety = prepare_private_path(self.path)
        if safety.unsafe:
            raise OSError(safety.reason)
        connection = connect_private(self.path, mode="rw")
        try:
            if not table_is_strict(connection, "fetch_attempts"):
                raise RuntimeError("schema_mismatch")
            acquire_immediate_transaction(connection)
            used = int(connection.execute(
                "SELECT count(*) FROM fetch_attempts WHERE campaign_id=?",
                (campaign_id,),
            ).fetchone()[0])
            if used >= ceiling:
                raise RuntimeError("campaign_ceiling_exhausted")
            sequence = used + 1
            connection.execute(
                "INSERT INTO fetch_attempts VALUES(?,?,?,?,?)",
                (campaign_id, connector_id, request_fingerprint, sequence, stamp),
            )
            connection.execute("COMMIT")
            return sequence
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def list_attempts(self) -> list[tuple[str, str, str, int]]:
        safety = prepare_private_path(self.path)
        if safety.unsafe:
            raise OSError(safety.reason)
        connection = connect_private(self.path, mode="ro")
        try:
            rows = connection.execute(
                "SELECT campaign_id,connector_id,request_fingerprint,sequence "
                "FROM fetch_attempts ORDER BY campaign_id,sequence"
            ).fetchall()
            return [(str(a), str(b), str(c), int(d)) for a, b, c, d in rows]
        finally:
            connection.close()
