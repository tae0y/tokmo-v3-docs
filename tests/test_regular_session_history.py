"""Stage 3 M2: durable official regular-session history store.

Append-only exact-schema private SQLite rows keyed ``(market, local_date)``
holding only the official calendar verdict (``regular``/``no_regular``), the
admitting calendar attempt identity, and the capture time. Immutable conflict
semantics, 0o600/symlink/hardlink/tamper fail-closed, one-transaction writes,
and a deterministic descending backfill walk (20 sessions / 35 dates). The
production trades session-date source reads only this store and fails
``failed_calendar``-truthfully when the history is insufficient or corrupt.
"""

import os
import sqlite3
import stat
import subprocess
import sys
import threading
from datetime import UTC, date, datetime, timedelta

import pytest

from tokmo.records.regular_session_history import (
    HistorySessionDatesV1,
    RegularSessionHistoryConflictError,
    RegularSessionHistoryCorruptError,
    RegularSessionHistoryError,
    RegularSessionHistoryProvenanceError,
    RegularSessionHistoryStore,
)
from tokmo.values.ops_kernel import CalendarStartReceiptV1
from tokmo.values.session_gate import Market

NOW = datetime(2026, 8, 2, 5, 0, tzinfo=UTC)
SLOT = datetime(2026, 8, 2, 5, 0, tzinfo=UTC)


class _Authority:
    """Recording stub of the kernel's read-only calendar provenance seam."""

    def __init__(self, *, settled=None, refuse_receipts=False):
        self.settled = {} if settled is None else settled
        self.refuse_receipts = refuse_receipts
        self.verified = []
        self.settled_reads = []

    def verify_calendar_history_receipt(self, receipt, *, now):
        self.verified.append((receipt, now))
        if self.refuse_receipts:
            raise ValueError("calendar receipt refused")

    def read_settled_calendar_provenance(self, market, market_local_date):
        self.settled_reads.append((market, market_local_date))
        return self.settled.get((market, market_local_date))


def receipt_for(market, local_date, ordinal=0):
    return CalendarStartReceiptV1(
        "cadence-300s-v1",
        SLOT,
        market,
        local_date,
        ordinal,
        f"calendar:{market.value}:{local_date.isoformat()}:{ordinal}",
        "STARTED",
    )


def store_at(tmp_path, name="history.sqlite3"):
    return RegularSessionHistoryStore(
        tmp_path / "ops" / name,
        clock=lambda: NOW,
    )


def seed(store, market, local_date, session, ordinal=0):
    return store.append_backfilled(
        market,
        local_date,
        session,
        receipt=receipt_for(market, local_date, ordinal),
        authority=_Authority(),
    )


# -- construction is I/O zero; append/read round trip -------------------------------


def test_construction_and_absent_store_reads_perform_no_creation(tmp_path):
    store = store_at(tmp_path)
    assert not (tmp_path / "ops").exists()
    assert store.read(Market.KR, date(2026, 8, 1)) is None
    assert store.next_backfill_target(
        Market.KR,
        date(2026, 8, 2),
    ) == date(2026, 8, 1)
    assert not store.path.exists()  # only an append creates the private file


def test_backfilled_append_round_trips_the_official_fact(tmp_path):
    store = store_at(tmp_path)
    authority = _Authority()
    receipt = receipt_for(Market.KR, date(2026, 7, 31))
    assert (
        store.append_backfilled(
            Market.KR,
            date(2026, 7, 31),
            "regular",
            receipt=receipt,
            authority=authority,
        )
        == "created"
    )
    assert authority.verified and authority.verified[0][0] is receipt
    row = store.read(Market.KR, date(2026, 7, 31))
    assert row["session"] == "regular"
    assert row["source_identity"] == "calendar:KR:2026-07-31:0"
    assert row["captured_at"] == NOW
    assert store.read(Market.US, date(2026, 7, 31)) is None


def test_settled_append_requires_validated_official_provenance(tmp_path):
    store = store_at(tmp_path)
    empty = _Authority()
    with pytest.raises(RegularSessionHistoryProvenanceError):
        store.append_settled(
            Market.KR,
            date(2026, 8, 2),
            "no_regular",
            authority=empty,
        )
    assert store.read(Market.KR, date(2026, 8, 2)) is None  # never forged

    good = _Authority(
        settled={
            (Market.KR, date(2026, 8, 2)): {
                "native_identity": "calendar:KR:2026-08-02:0",
            },
        }
    )
    assert (
        store.append_settled(
            Market.KR,
            date(2026, 8, 2),
            "no_regular",
            authority=good,
        )
        == "created"
    )
    assert store.read(Market.KR, date(2026, 8, 2))["session"] == "no_regular"


def test_refused_receipt_appends_nothing(tmp_path):
    store = store_at(tmp_path)
    with pytest.raises(Exception):
        store.append_backfilled(
            Market.KR,
            date(2026, 7, 30),
            "regular",
            receipt=receipt_for(Market.KR, date(2026, 7, 30)),
            authority=_Authority(refuse_receipts=True),
        )
    assert store.read(Market.KR, date(2026, 7, 30)) is None


def test_receipt_identity_must_match_the_appended_fact(tmp_path):
    store = store_at(tmp_path)
    with pytest.raises(RegularSessionHistoryProvenanceError):
        store.append_backfilled(
            Market.KR,
            date(2026, 7, 30),
            "regular",
            receipt=receipt_for(Market.US, date(2026, 7, 30)),
            authority=_Authority(),
        )
    with pytest.raises(RegularSessionHistoryProvenanceError):
        store.append_backfilled(
            Market.KR,
            date(2026, 7, 30),
            "regular",
            receipt=receipt_for(Market.KR, date(2026, 7, 29)),
            authority=_Authority(),
        )
    assert store.read(Market.KR, date(2026, 7, 30)) is None


# -- immutable conflict semantics ----------------------------------------------------


def test_same_fact_reappend_is_a_duplicate_noop(tmp_path):
    store = store_at(tmp_path)
    assert seed(store, Market.US, date(2026, 7, 31), "regular") == "created"
    assert seed(store, Market.US, date(2026, 7, 31), "regular", 1) == "duplicate"
    assert store.read(Market.US, date(2026, 7, 31))["source_identity"].endswith(":0")


def test_conflicting_fact_fails_closed_and_preserves_the_first_row(tmp_path):
    store = store_at(tmp_path)
    seed(store, Market.US, date(2026, 7, 31), "regular")
    with pytest.raises(RegularSessionHistoryConflictError):
        seed(store, Market.US, date(2026, 7, 31), "no_regular", 1)
    assert store.read(Market.US, date(2026, 7, 31))["session"] == "regular"


def test_invalid_fact_shapes_are_rejected_before_any_write(tmp_path):
    store = store_at(tmp_path)
    with pytest.raises(RegularSessionHistoryError):
        seed(store, Market.KR, date(2026, 7, 31), "holiday")
    with pytest.raises(RegularSessionHistoryError):
        store.append_backfilled(
            "KR",
            date(2026, 7, 31),
            "regular",
            receipt=receipt_for(Market.KR, date(2026, 7, 31)),
            authority=_Authority(),
        )
    with pytest.raises(RegularSessionHistoryError):
        store.append_backfilled(
            Market.KR,
            datetime(2026, 7, 31, tzinfo=UTC),
            "regular",
            receipt=receipt_for(Market.KR, date(2026, 7, 31)),
            authority=_Authority(),
        )
    assert store.read(Market.KR, date(2026, 7, 31)) is None


# -- private-store security ----------------------------------------------------------


def test_store_file_is_private_0600(tmp_path):
    store = store_at(tmp_path)
    seed(store, Market.KR, date(2026, 7, 31), "regular")
    mode = stat.S_IMODE(os.lstat(store.path).st_mode)
    assert mode == 0o600
    parent_mode = stat.S_IMODE(os.lstat(store.path.parent).st_mode)
    assert parent_mode == 0o700


def test_symlinked_store_path_fails_closed(tmp_path):
    real = tmp_path / "elsewhere.sqlite3"
    sqlite3.connect(real).close()
    target = tmp_path / "ops" / "history.sqlite3"
    target.parent.mkdir(mode=0o700, parents=True)
    target.symlink_to(real)
    store = RegularSessionHistoryStore(target, clock=lambda: NOW)
    with pytest.raises(RegularSessionHistoryCorruptError):
        seed(store, Market.KR, date(2026, 7, 31), "regular")
    with pytest.raises(RegularSessionHistoryCorruptError):
        store.read(Market.KR, date(2026, 7, 31))


def test_hardlinked_store_fails_closed(tmp_path):
    store = store_at(tmp_path)
    seed(store, Market.KR, date(2026, 7, 31), "regular")
    os.link(store.path, tmp_path / "hardlink-copy")
    with pytest.raises(RegularSessionHistoryCorruptError):
        store.read(Market.KR, date(2026, 7, 31))


def test_schema_tamper_fails_closed(tmp_path):
    store = store_at(tmp_path)
    seed(store, Market.KR, date(2026, 7, 31), "regular")
    raw = sqlite3.connect(store.path)
    raw.execute("CREATE TABLE injected(x TEXT)")
    raw.commit()
    raw.close()
    with pytest.raises(RegularSessionHistoryCorruptError):
        store.read(Market.KR, date(2026, 7, 31))
    with pytest.raises(RegularSessionHistoryCorruptError):
        seed(store, Market.KR, date(2026, 7, 30), "regular")


def test_noncanonical_row_bytes_fail_closed_on_read(tmp_path):
    store = store_at(tmp_path)
    seed(store, Market.KR, date(2026, 7, 31), "regular")
    raw = sqlite3.connect(store.path)
    raw.execute("UPDATE regular_session_history SET source_identity='forged!'")
    raw.commit()
    raw.close()
    with pytest.raises(RegularSessionHistoryCorruptError):
        store.read(Market.KR, date(2026, 7, 31))


def test_concurrent_appends_of_distinct_dates_all_commit(tmp_path):
    store = store_at(tmp_path)
    errors = []

    def append(day):
        try:
            local = RegularSessionHistoryStore(store.path, clock=lambda: NOW)
            seed(local, Market.KR, date(2026, 7, day), "regular")
        except Exception as error:  # noqa: BLE001 - collected for assertion
            errors.append(error)

    threads = [threading.Thread(target=append, args=(day,)) for day in range(1, 9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    for day in range(1, 9):
        assert store.read(Market.KR, date(2026, 7, day))["session"] == "regular"


def test_repeated_concurrent_fresh_opens_never_observe_partial_schema(tmp_path):
    # Adversarial first-open race: every round starts from an absent file and
    # releases all writers through a barrier at once, so fresh creation,
    # schema bootstrap, and first validation collide as hard as possible.
    for round_number in range(15):
        path = tmp_path / f"round-{round_number}" / "ops" / "history.sqlite3"
        barrier = threading.Barrier(8)
        errors = []

        def append(day, path=path, barrier=barrier, errors=errors):
            try:
                local = RegularSessionHistoryStore(path, clock=lambda: NOW)
                barrier.wait()
                seed(local, Market.KR, date(2026, 7, day), "regular")
            except Exception as error:  # noqa: BLE001 - collected for assertion
                errors.append(error)

        threads = [threading.Thread(target=append, args=(day,)) for day in range(1, 9)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors, errors
        survivor = RegularSessionHistoryStore(path, clock=lambda: NOW)
        for day in range(1, 9):
            assert (
                survivor.read(
                    Market.KR,
                    date(2026, 7, day),
                )["session"]
                == "regular"
            )
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600


def test_cross_process_concurrent_fresh_creation_all_commit(tmp_path):
    # Same first-open race across real processes: each subprocess builds its
    # own store on the shared absent path and appends one distinct date.
    path = tmp_path / "ops" / "history.sqlite3"
    script = (
        "import sys\n"
        "from datetime import date, datetime, timezone\n"
        "from tokmo.records.regular_session_history import"
        " RegularSessionHistoryStore\n"
        "from tokmo.values.ops_kernel import CalendarStartReceiptV1\n"
        "from tokmo.values.session_gate import Market\n"
        "day = int(sys.argv[1])\n"
        "now = datetime(2026, 8, 2, 5, 0, tzinfo=timezone.utc)\n"
        "class Authority:\n"
        "    def verify_calendar_history_receipt(self, receipt, *, now):\n"
        "        pass\n"
        "local_date = date(2026, 7, day)\n"
        "receipt = CalendarStartReceiptV1(\n"
        "    'cadence-300s-v1', now, Market.KR, local_date, 0,\n"
        "    f'calendar:KR:{local_date.isoformat()}:0', 'STARTED')\n"
        "store = RegularSessionHistoryStore(sys.argv[2], clock=lambda: now)\n"
        "print(store.append_backfilled(Market.KR, local_date, 'regular',\n"
        "      receipt=receipt, authority=Authority()))\n"
    )
    environment = dict(os.environ)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    environment["PYTHONPATH"] = os.path.join(root, "src")
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(day), str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            text=True,
        )
        for day in range(1, 7)
    ]
    results = [process.communicate(timeout=60) for process in processes]
    for process, (stdout, stderr) in zip(processes, results):
        assert process.returncode == 0, stderr
        assert stdout.strip() == "created"
    survivor = RegularSessionHistoryStore(path, clock=lambda: NOW)
    for day in range(1, 7):
        assert survivor.read(Market.KR, date(2026, 7, day))["session"] == "regular"


# -- deterministic backfill walk -----------------------------------------------------


def test_backfill_target_is_the_first_missing_historical_date(tmp_path):
    store = store_at(tmp_path)
    anchor = date(2026, 8, 2)
    # anchor itself is owned by the current-date refresh: an empty history
    # targets anchor-1, never anchor.
    assert store.next_backfill_target(Market.KR, anchor) == date(2026, 8, 1)
    seed(store, Market.KR, date(2026, 8, 2), "no_regular")
    seed(store, Market.KR, date(2026, 8, 1), "no_regular")
    assert store.next_backfill_target(Market.KR, anchor) == date(2026, 7, 31)
    seed(store, Market.KR, date(2026, 7, 31), "regular")
    # the walk continues below known facts to the next gap
    assert store.next_backfill_target(Market.KR, anchor) == date(2026, 7, 30)


def test_backfill_walk_stops_at_twenty_regular_sessions(tmp_path):
    store = store_at(tmp_path)
    anchor = date(2026, 8, 2)
    for offset in range(20):
        seed(store, Market.KR, anchor - timedelta(days=offset), "regular")
    assert store.next_backfill_target(Market.KR, anchor) is None


def test_backfill_walk_never_examines_a_thirty_sixth_date(tmp_path):
    store = store_at(tmp_path)
    anchor = date(2026, 8, 2)
    # 35 known facts but fewer than 20 regular: the window is exhausted and
    # the walk truthfully stops rather than inspecting date 36.
    for offset in range(35):
        seed(
            store,
            Market.KR,
            anchor - timedelta(days=offset),
            "regular" if offset < 10 else "no_regular",
        )
    assert store.next_backfill_target(Market.KR, anchor) is None


# -- production trades session-date source -------------------------------------------


def test_reader_returns_descending_official_regular_dates(tmp_path):
    store = store_at(tmp_path)
    anchor = date(2026, 8, 3)
    expected = []
    for offset in range(30):
        day = anchor - timedelta(days=offset)
        session = "regular" if day.weekday() < 5 else "no_regular"
        seed(store, Market.KR, day, session)
        if session == "regular" and len(expected) < 20:
            expected.append(day)
    reader = HistorySessionDatesV1(store)
    result = reader.regular_session_dates(Market.KR, anchor, 20)
    assert result == tuple(expected)
    assert reader.regular_session_dates(Market.KR, anchor, 3) == tuple(expected[:3])


def test_reader_insufficient_history_is_truthful_failed_calendar(tmp_path):
    store = store_at(tmp_path)
    reader = HistorySessionDatesV1(store)
    with pytest.raises(ValueError):
        reader.regular_session_dates(Market.KR, date(2026, 8, 3), 20)
    seed(store, Market.KR, date(2026, 8, 3), "regular")
    # a gap below the anchor is insufficiency, not a guess
    with pytest.raises(ValueError):
        reader.regular_session_dates(Market.KR, date(2026, 8, 3), 2)


def test_reader_rejects_invalid_queries_and_caps_the_walk(tmp_path):
    store = store_at(tmp_path)
    anchor = date(2026, 8, 3)
    # 35 facts, all no_regular: 20 can never be found within the window.
    for offset in range(40):
        seed(store, Market.KR, anchor - timedelta(days=offset), "no_regular")
    reader = HistorySessionDatesV1(store)
    with pytest.raises(ValueError):
        reader.regular_session_dates(Market.KR, anchor, 1)
    with pytest.raises(ValueError):
        reader.regular_session_dates("KR", anchor, 20)
    with pytest.raises(ValueError):
        reader.regular_session_dates(Market.KR, anchor, 0)
    with pytest.raises(ValueError):
        reader.regular_session_dates(Market.KR, "2026-08-03", 20)
