import ast
import sqlite3
import stat
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tokmo.records import latest_quotes
from tokmo.records.latest_quotes import (
    LatestQuoteStore,
    LatestQuoteStoreReadError,
)
from tokmo.values import TossCurrentPriceFact

NOW = datetime(2026, 7, 28, 1, 2, 3, tzinfo=UTC)


def fact(
    symbol="FixtureA",
    *,
    source="fixture-source",
    price="12.5",
    currency="XTS",
    timestamp=None,
    fetched_at=NOW,
):
    return TossCurrentPriceFact(
        source=source,
        symbol=symbol,
        last_price=price,
        currency_raw=currency,
        timestamp=timestamp,
        fetched_at=fetched_at,
    )


def test_fresh_store_creates_exact_strict_versioned_schema_and_reopens(tmp_path):
    path = tmp_path / "private" / "quotes.sqlite3"
    store = LatestQuoteStore(path, clock=lambda: NOW)

    assert store.save_batch(()).reason == "invalid_batch"
    assert not path.exists()

    receipt = store.save_batch((fact(),))
    assert receipt.status == "SAVED"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    with sqlite3.connect(path) as connection:
        table_list = {row[1]: row for row in connection.execute("PRAGMA table_list")}
        assert table_list["latest_quotes"][5] == 1
        assert table_list["storage_metadata"][5] == 1
        assert connection.execute(
            "SELECT value FROM storage_metadata WHERE key=?",
            ("latest_quote_schema_version",),
        ).fetchone() == ("1",)

    reopened = LatestQuoteStore(path, clock=lambda: NOW)
    assert reopened.save_batch((fact(),)).status == "SAVED"


def test_insert_read_order_and_update_round_trip_latest_only(tmp_path):
    path = tmp_path / "private" / "quotes.sqlite3"
    later = datetime(2026, 7, 28, 2, 3, 4, tzinfo=UTC)
    store = LatestQuoteStore(path, clock=lambda: NOW)
    original = (fact("FixtureA"), fact("FixtureB", price="2"))

    receipt = store.save_batch(original)
    assert (receipt.inserted_count, receipt.updated_count) == (2, 0)
    assert store.read_latest(
        source="fixture-source", symbols=("FixtureB", "FixtureA")
    ) == (original[1], original[0])

    changed = (
        fact(
            "FixtureA",
            price="7.25",
            currency="FUTURE",
            timestamp=later,
            fetched_at=later,
        ),
        fact("FixtureB", price="2", fetched_at=later),
    )
    updated = LatestQuoteStore(path, clock=lambda: later).save_batch(changed)

    assert (updated.inserted_count, updated.updated_count) == (0, 2)
    assert (
        LatestQuoteStore(path, clock=lambda: later).read_latest(
            source="fixture-source", symbols=("FixtureA", "FixtureB")
        )
        == changed
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM latest_quotes").fetchone() == (
            2,
        )


@pytest.mark.parametrize(
    "batch",
    [
        (),
        [fact()],
        (fact("FixtureA"), fact("FixtureA")),
        (fact("Bad_Symbol"),),
        (fact(price="01"),),
        (fact(currency=""),),
        (fact(fetched_at=datetime(2026, 7, 28)),),
        (
            fact("FixtureA"),
            TossCurrentPriceFact(
                source="other-source",
                symbol="FixtureB",
                last_price="1",
                currency_raw="XTS",
                timestamp=None,
                fetched_at=NOW,
            ),
        ),
    ],
)
def test_invalid_whole_batch_never_mutates_existing_store(tmp_path, batch):
    path = tmp_path / "private" / "quotes.sqlite3"
    store = LatestQuoteStore(path, clock=lambda: NOW)
    assert store.save_batch((fact(),)).status == "SAVED"
    before = path.read_bytes()

    receipt = store.save_batch(batch)

    assert (receipt.status, receipt.inserted_count, receipt.updated_count) == (
        "FAILED",
        0,
        0,
    )
    assert receipt.reason == "invalid_batch"
    assert path.read_bytes() == before


def test_batch_size_boundaries_accept_200_and_reject_201_without_mutation(
    tmp_path,
):
    path = tmp_path / "private" / "quotes.sqlite3"
    store = LatestQuoteStore(path, clock=lambda: NOW)
    maximum = tuple(fact(f"Fixture{index}") for index in range(200))

    saved = store.save_batch(maximum)
    before = path.read_bytes()
    too_many = maximum + (fact("FixtureOverflow"),)
    rejected = store.save_batch(too_many)

    assert (saved.status, saved.inserted_count, saved.updated_count) == (
        "SAVED",
        200,
        0,
    )
    assert (
        rejected.status,
        rejected.inserted_count,
        rejected.updated_count,
        rejected.reason,
    ) == ("FAILED", 0, 0, "invalid_batch")
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "oversized",
    [
        fact(source="s" * 129),
        fact(symbol="S" * 65),
        fact(price="1" * 31),
        fact(currency="c" * 65),
    ],
)
def test_oversized_private_fields_are_invalid_without_creating_file(
    tmp_path, oversized
):
    path = tmp_path / "private" / "quotes.sqlite3"

    receipt = LatestQuoteStore(path, clock=lambda: NOW).save_batch((oversized,))

    assert (
        receipt.status,
        receipt.inserted_count,
        receipt.updated_count,
        receipt.reason,
    ) == ("FAILED", 0, 0, "invalid_batch")
    assert not path.exists()


def test_naive_clock_is_invalid_without_creating_file(tmp_path):
    path = tmp_path / "private" / "quotes.sqlite3"
    store = LatestQuoteStore(path, clock=lambda: datetime(2026, 7, 28, 1, 2, 3))

    receipt = store.save_batch((fact(),))

    assert (
        receipt.status,
        receipt.inserted_count,
        receipt.updated_count,
        receipt.reason,
    ) == ("FAILED", 0, 0, "invalid_batch")
    assert not path.exists()


def test_missing_read_does_not_create_and_errors_are_closed(tmp_path):
    path = tmp_path / "private" / "quotes.sqlite3"
    store = LatestQuoteStore(path, clock=lambda: NOW)

    with pytest.raises(LatestQuoteStoreReadError) as captured:
        store.read_latest(source="fixture-source", symbols=("FixtureA",))

    assert captured.value.reason == "store_read_failed"
    assert captured.value.args == ("store_read_failed",)
    assert str(captured.value) == "store_read_failed"
    assert captured.value.__cause__ is None
    assert not path.exists()

    assert store.save_batch((fact(),)).status == "SAVED"
    with pytest.raises(LatestQuoteStoreReadError) as missing:
        store.read_latest(source="fixture-source", symbols=("FixtureB",))
    assert missing.value.reason == "missing_latest_quote"


def test_old_sqlite_gate_and_unsafe_existing_parent_fail_without_initialization(
    tmp_path, monkeypatch
):
    old_path = tmp_path / "old" / "quotes.sqlite3"
    monkeypatch.setattr(latest_quotes.sqlite3, "sqlite_version_info", (3, 36, 0))
    receipt = LatestQuoteStore(old_path, clock=lambda: NOW).save_batch((fact(),))
    assert receipt.reason == "schema_mismatch"
    assert not old_path.exists()

    monkeypatch.setattr(latest_quotes.sqlite3, "sqlite_version_info", (3, 53, 2))
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    unsafe.chmod(0o755)
    unsafe_path = unsafe / "quotes.sqlite3"
    receipt = LatestQuoteStore(unsafe_path, clock=lambda: NOW).save_batch((fact(),))
    assert receipt.reason == "store_write_failed"
    assert not unsafe_path.exists()


def test_nonempty_corrupt_store_is_preserved_and_sanitized(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "quotes.sqlite3"
    original = b"fixture-corrupt-database"
    path.write_bytes(original)
    path.chmod(0o600)
    store = LatestQuoteStore(path, clock=lambda: NOW)

    receipt = store.save_batch((fact(),))
    assert receipt.reason == "store_corrupt"
    assert path.read_bytes() == original
    with pytest.raises(LatestQuoteStoreReadError) as captured:
        store.read_latest(source="fixture-source", symbols=("FixtureA",))
    assert captured.value.reason == "store_corrupt"

    rendered = repr(receipt) + repr(store) + repr(captured.value)
    for private_value in (
        "FixtureA",
        "12.5",
        "XTS",
        str(path),
        "SELECT",
        "file is not a database",
    ):
        assert private_value not in rendered


def test_existing_strict_tables_without_required_checks_are_schema_mismatch(
    tmp_path,
):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "quotes.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE latest_quotes (
                source TEXT NOT NULL,
                symbol TEXT NOT NULL,
                last_price TEXT NOT NULL,
                currency_raw TEXT NOT NULL,
                provider_timestamp TEXT,
                fetched_at TEXT NOT NULL,
                stored_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                PRIMARY KEY (source, symbol)
            ) STRICT;
            CREATE TABLE storage_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) STRICT;
            INSERT INTO storage_metadata VALUES
                ('latest_quote_schema_version', '1');
            """
        )
    path.chmod(0o600)

    receipt = LatestQuoteStore(path, clock=lambda: NOW).save_batch((fact(),))

    assert receipt.reason == "schema_mismatch"


def test_lock_is_bounded_and_leaves_original_latest_value_unchanged(tmp_path):
    path = tmp_path / "private" / "quotes.sqlite3"
    store = LatestQuoteStore(path, clock=lambda: NOW)
    original = fact()
    assert store.save_batch((original,)).status == "SAVED"

    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        receipt = LatestQuoteStore(path, clock=lambda: NOW).save_batch(
            (fact(price="7"),)
        )
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert receipt.reason == "store_locked"
    assert time.monotonic() - started < 3.0
    assert store.read_latest(source="fixture-source", symbols=("FixtureA",)) == (
        original,
    )


def test_read_is_read_only_while_another_connection_holds_begin_immediate(
    tmp_path,
):
    path = tmp_path / "private" / "quotes.sqlite3"
    store = LatestQuoteStore(path, clock=lambda: NOW)
    original = fact()
    assert store.save_batch((original,)).status == "SAVED"
    before = path.read_bytes()
    journal = path.with_name(path.name + "-journal")

    blocker = sqlite3.connect(path, isolation_level=None)
    assert blocker.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        read_back = store.read_latest(source="fixture-source", symbols=("FixtureA",))
        assert not journal.exists()
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert read_back == (original,)
    assert path.read_bytes() == before
    assert not journal.exists()


def test_save_path_oserror_returns_closed_write_failure(tmp_path):
    ancestor = tmp_path / "not-a-directory"
    ancestor.write_bytes(b"fixture")
    path = ancestor / "private" / "quotes.sqlite3"

    receipt = LatestQuoteStore(path, clock=lambda: NOW).save_batch((fact(),))

    assert (
        receipt.status,
        receipt.inserted_count,
        receipt.updated_count,
        receipt.reason,
    ) == ("FAILED", 0, 0, "store_write_failed")
    assert str(path) not in repr(receipt)


@pytest.mark.parametrize(
    "column,value",
    [
        ("last_price", "01"),
        ("fetched_at", "2026-07-28T01:02:03"),
        ("provider_timestamp", "2026-07-28T01:02:03"),
    ],
)
def test_read_semantically_revalidates_manually_malformed_rows(tmp_path, column, value):
    path = tmp_path / "private" / "quotes.sqlite3"
    store = LatestQuoteStore(path, clock=lambda: NOW)
    assert store.save_batch((fact(),)).status == "SAVED"
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"UPDATE latest_quotes SET {column}=? WHERE source=? AND symbol=?",
            (value, "fixture-source", "FixtureA"),
        )

    with pytest.raises(LatestQuoteStoreReadError) as captured:
        store.read_latest(source="fixture-source", symbols=("FixtureA",))

    assert captured.value.reason == "store_corrupt"
    assert captured.value.__cause__ is None


def test_rollback_journal_is_private_during_transaction_and_removed_after_close(
    tmp_path,
):
    path = tmp_path / "private" / "quotes.sqlite3"
    assert (
        LatestQuoteStore(path, clock=lambda: NOW).save_batch((fact(),)).status
        == "SAVED"
    )
    journal = path.with_name(path.name + "-journal")
    connection = sqlite3.connect(path, isolation_level=None)
    prior = stat.S_IMODE(path.stat().st_mode)
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE latest_quotes SET stored_at=? WHERE source=?",
        ("2026-07-28T02:03:04+00:00", "fixture-source"),
    )
    assert journal.exists()
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    connection.execute("COMMIT")
    connection.close()

    assert prior == 0o600
    assert not journal.exists()


class FailingConnection:
    def __init__(self, connection, failure_marker):
        self.connection = connection
        self.failure_marker = failure_marker

    def execute(self, sql, parameters=()):
        markers = (
            self.failure_marker
            if isinstance(self.failure_marker, tuple)
            else (self.failure_marker,)
        )
        if any(marker in sql for marker in markers):
            raise sqlite3.OperationalError("fixture injected failure")
        return self.connection.execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.connection.executemany(sql, parameters)

    def close(self):
        self.connection.close()


def test_forced_commit_failure_rolls_back_entire_update(tmp_path, monkeypatch):
    path = tmp_path / "private" / "quotes.sqlite3"
    original = (fact("FixtureA"), fact("FixtureB", price="2"))
    store = LatestQuoteStore(path, clock=lambda: NOW)
    assert store.save_batch(original).status == "SAVED"
    real_connect = sqlite3.connect

    def failing_connect(*args, **kwargs):
        return FailingConnection(real_connect(*args, **kwargs), "COMMIT")

    monkeypatch.setattr(latest_quotes.sqlite3, "connect", failing_connect)
    receipt = store.save_batch(
        (fact("FixtureA", price="3"), fact("FixtureB", price="4"))
    )
    monkeypatch.setattr(latest_quotes.sqlite3, "connect", real_connect)

    assert receipt.reason == "store_write_failed"
    assert (
        store.read_latest(source="fixture-source", symbols=("FixtureA", "FixtureB"))
        == original
    )


def test_failure_between_ddl_statements_rolls_back_and_clean_reopen_saves(
    tmp_path, monkeypatch
):
    path = tmp_path / "private" / "quotes.sqlite3"
    real_connect = sqlite3.connect

    def failing_connect(*args, **kwargs):
        return FailingConnection(
            real_connect(*args, **kwargs), "CREATE TABLE storage_metadata"
        )

    monkeypatch.setattr(latest_quotes.sqlite3, "connect", failing_connect)
    first = LatestQuoteStore(path, clock=lambda: NOW).save_batch((fact(),))
    monkeypatch.setattr(latest_quotes.sqlite3, "connect", real_connect)
    assert path.stat().st_size == 0
    reopened = LatestQuoteStore(path, clock=lambda: NOW).save_batch((fact(),))

    assert first.reason == "store_write_failed"
    assert reopened.status == "SAVED"


def test_initialization_rollback_failure_returns_closed_write_failure(
    tmp_path, monkeypatch
):
    path = tmp_path / "private" / "quotes.sqlite3"
    real_connect = sqlite3.connect

    def failing_connect(*args, **kwargs):
        return FailingConnection(
            real_connect(*args, **kwargs),
            ("CREATE TABLE storage_metadata", "ROLLBACK"),
        )

    monkeypatch.setattr(latest_quotes.sqlite3, "connect", failing_connect)

    receipt = LatestQuoteStore(path, clock=lambda: NOW).save_batch((fact(),))

    assert (
        receipt.status,
        receipt.inserted_count,
        receipt.updated_count,
        receipt.reason,
    ) == ("FAILED", 0, 0, "store_write_failed")
    assert "fixture injected failure" not in repr(receipt)


def test_consumer_read_preserves_store_locked_reason(monkeypatch, tmp_path):
    store = LatestQuoteStore(tmp_path / "quotes.sqlite3", clock=lambda: NOW)
    monkeypatch.setattr(store, "read_latest", lambda **_kwargs: (fact(),))

    def locked_connect():
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error

    monkeypatch.setattr(store, "_connect", locked_connect)
    with pytest.raises(LatestQuoteStoreReadError) as captured:
        store.read_latest_with_stored_at(
            source="fixture-source",
            symbols=("FixtureA",),
        )
    assert captured.value.reason == "store_locked"


def test_connection_pragmas_and_pilot_import_boundaries(tmp_path):
    path = tmp_path / "private" / "quotes.sqlite3"
    assert (
        LatestQuoteStore(path, clock=lambda: NOW).save_batch((fact(),)).status
        == "SAVED"
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)

    root = Path(__file__).parents[1]
    records_path = root / "src/tokmo/records/latest_quotes.py"
    program_path = root / "src/tokmo/programs/current_price_storage.py"

    def imports(path):
        result = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                result.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                result.add(node.module)
        return result

    assert not any(name.startswith("tokmo.facts") for name in imports(records_path))
    forbidden = (
        "live",
        "transport",
        "calendar",
        "holding",
        "portfolio",
        "alert",
        "credential",
    )
    program_text = program_path.read_text().lower()
    assert not any(token in program_text for token in forbidden)
