"""Integrated NEWS dispatch and fetch-attempt accounting contracts."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from tokmo.programs.news_collection import (
    FetchAttemptLedger,
    NewsCollectionJob,
    NewsCollectionManager,
)
from tokmo.values.news_connectors import NewsFetchResult, NewsFetchStatus

NOW = datetime(2026, 8, 2, 10, 0, tzinfo=UTC)


@dataclass
class FakeAdapter:
    result: NewsFetchResult

    def __post_init__(self):
        self.calls = []

    def fetch(self, request, *, observed_at):
        self.calls.append((request, observed_at))
        return self.result


def test_manager_reserves_each_start_before_sequential_adapter_dispatch(tmp_path):
    ledger = FetchAttemptLedger(tmp_path / "fetch-attempts.sqlite")
    ledger.bootstrap(now=NOW)
    first = FakeAdapter(NewsFetchResult(NewsFetchStatus.EMPTY))
    second = FakeAdapter(NewsFetchResult(NewsFetchStatus.TRANSPORT_FAILURE))
    manager = NewsCollectionManager(
        adapters={"first": first, "second": second},
        reservation=ledger.reserve,
    )
    jobs = (
        NewsCollectionJob("first", object(), "feed:first", "a" * 64),
        NewsCollectionJob("second", object(), "feed:second", "b" * 64),
    )

    result = manager.collect("campaign-1", jobs, observed_at=NOW)

    assert [item.connector_id for item in result.results] == ["first", "second"]
    assert [item.result.status for item in result.results] == [
        NewsFetchStatus.EMPTY,
        NewsFetchStatus.TRANSPORT_FAILURE,
    ]
    assert len(first.calls) == len(second.calls) == 1
    assert ledger.list_attempts() == [
        ("campaign-1", "first", "a" * 64, 1),
        ("campaign-1", "second", "b" * 64, 2),
    ]


def test_reservation_failure_preserves_per_source_outcome_and_skips_socket(tmp_path):
    adapter = FakeAdapter(NewsFetchResult(NewsFetchStatus.EMPTY))

    def refuse(*_args, **_kwargs):
        raise RuntimeError("ledger unavailable")

    manager = NewsCollectionManager(adapters={"first": adapter}, reservation=refuse)
    result = manager.collect(
        "campaign-1",
        (NewsCollectionJob("first", object(), "feed:first", "a" * 64),),
        observed_at=NOW,
    )

    assert result.results[0].result.status is NewsFetchStatus.CONFIG_UNAVAILABLE
    assert adapter.calls == []


def test_busy_reservation_keeps_local_storage_cause_without_fetch(tmp_path):
    ledger = FetchAttemptLedger(tmp_path / "ledger.sqlite3")
    ledger.bootstrap(now=NOW)
    adapter = FakeAdapter(NewsFetchResult(NewsFetchStatus.EMPTY))
    manager = NewsCollectionManager(
        adapters={"first": adapter}, reservation=ledger.reserve
    )
    with sqlite3.connect(ledger.path) as held:
        held.execute("BEGIN IMMEDIATE")
        result = (
            manager.collect(
                "test",
                (NewsCollectionJob("first", object(), "feed:first", "a" * 64),),
                observed_at=NOW,
            )
            .results[0]
            .result
        )
    assert result.status is NewsFetchStatus.CONFIG_UNAVAILABLE
    assert result.failure_class == "STORAGE_BUSY"
    assert result.failure_stage == "RESERVE"
    assert adapter.calls == []


def test_fetch_attempt_ledger_is_private_strict_and_does_not_refund_consumed_start(
    tmp_path,
):
    path = tmp_path / "fetch-attempts.sqlite"
    ledger = FetchAttemptLedger(path)
    ledger.bootstrap(now=NOW)
    ledger.reserve("campaign-1", "first", "a" * 64, NOW)

    reopened = FetchAttemptLedger(path)
    reopened.bootstrap(now=NOW)
    reopened.reserve("campaign-1", "first", "a" * 64, NOW)

    assert path.stat().st_mode & 0o777 == 0o600
    assert reopened.list_attempts() == [
        ("campaign-1", "first", "a" * 64, 1),
        ("campaign-1", "first", "a" * 64, 2),
    ]
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='fetch_attempts'"
        ).fetchone()[0]
        assert "STRICT" in ddl


def test_fetch_attempt_ceiling_is_checked_atomically_before_second_reservation(
    tmp_path,
):
    ledger = FetchAttemptLedger(tmp_path / "fetch-attempts.sqlite")
    ledger.bootstrap(now=NOW)

    assert (
        ledger.reserve_bounded(
            "campaign-1",
            "first",
            "a" * 64,
            NOW,
            ceiling=1,
        )
        == 1
    )
    try:
        ledger.reserve_bounded(
            "campaign-1",
            "first",
            "a" * 64,
            NOW,
            ceiling=1,
        )
    except RuntimeError as error:
        assert str(error) == "campaign_ceiling_exhausted"
    else:
        raise AssertionError("second reservation must fail closed")
    assert len(ledger.list_attempts()) == 1
