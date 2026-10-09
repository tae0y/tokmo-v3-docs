"""Pension trade records -> unified trade_ledger ingestion."""

from __future__ import annotations

import itertools
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from tokmo.programs.pension_legacy_migration import PensionTradeRecordV1
from tokmo.programs.pension_trade_ingestion import (
    PENSION_TRADE_SCOPE,
    PENSION_TRADE_SOURCE,
    build_pension_trade_events,
    ingest_pension_trades,
)
from tokmo.records.trade_ledger_store import TradeLedgerStore
from tokmo.values.trade_ledger import ImportAuditMeta, TradeLedgerEvent

_KST = ZoneInfo("Asia/Seoul")
_ID = itertools.count(1)


def ptrade(
    raw="매수",
    direction="BUY",
    *,
    qty=10,
    price="1000",
    amount="10000",
    name="KODEX 200",
    medium="미래에셋증권 통합앱",
    day=13,
):
    return PensionTradeRecordV1(
        trade_date=datetime(2026, 7, day, tzinfo=_KST),
        product_name=name,
        trade_type=direction,
        transaction_type_raw=raw,
        amount=Decimal(amount),
        balance_quantity=100,
        trade_quantity=qty,
        unit_price=Decimal(price),
        trade_medium=medium,
    )


def make_store(path):
    return TradeLedgerStore(
        path,
        clock=lambda: datetime(2026, 8, 5, tzinfo=UTC),
        id_factory=lambda: f"pt-id-{next(_ID):08d}",
    )


def test_build_maps_fields_and_sequential_keys():
    events = build_pension_trade_events(
        [
            ptrade("매수", "BUY"),
            ptrade("매도", "SELL", name="SOL 화장품TOP3플러스"),
        ]
    )
    assert [e.source_record_key for e in events] == ["row-000001", "row-000002"]
    assert all(e.source == PENSION_TRADE_SOURCE for e in events)
    assert all(e.account_scope == PENSION_TRADE_SCOPE for e in events)
    e0 = events[0]
    assert e0.event_kind == "EXECUTION"
    assert e0.transaction_type_raw == "매수"
    assert e0.instrument_raw == "KODEX 200"
    assert e0.trade_date == date(2026, 7, 13)
    assert e0.medium_raw == "미래에셋증권 통합앱"
    # Toss-only fields left None, never fabricated; currency is the KRW constant.
    assert e0.currency_raw == "KRW"
    assert e0.settlement_date is None and e0.fee is None and e0.tax is None
    assert events[1].instrument_raw == "SOL 화장품TOP3플러스"


def test_correction_rows_are_correction_kind():
    events = build_pension_trade_events(
        [
            ptrade("매수정정1", "BUY"),
            ptrade("매도정정", "SELL"),
            ptrade("매수", "BUY"),
        ]
    )
    assert [e.event_kind for e in events] == [
        "CORRECTION",
        "CORRECTION",
        "EXECUTION",
    ]


def test_decimals_are_canonicalized():
    events = build_pension_trade_events(
        [
            ptrade("매수", "BUY", qty=3, price="1000.50", amount="10000.00"),
        ]
    )
    assert events[0].quantity == "3"
    assert events[0].unit_price == "1000.5"
    assert events[0].amount == "10000"


def test_ingest_saves_full_history_and_is_idempotent(tmp_path):
    path = tmp_path / "trade_ledger.sqlite3"
    store = make_store(path)
    trades = [ptrade("매수", "BUY") for _ in range(5)]

    first = ingest_pension_trades(trades, store, file_sha256="ab" * 32)
    assert first.status == "SAVED"
    assert first.inserted_count == 5 and first.new_candidate_count == 5
    assert first.duplicate_count == 0

    # Re-submitting the same complete history is a no-op set of duplicates.
    again = ingest_pension_trades(trades, store, file_sha256="ab" * 32)
    assert again.status == "SAVED"
    assert again.inserted_count == 0 and again.duplicate_count == 5

    rows = (
        sqlite3.connect(path)
        .execute(
            "SELECT count(*) FROM trade_ledger_events WHERE source=?",
            (PENSION_TRADE_SOURCE,),
        )
        .fetchone()[0]
    )
    assert rows == 5


def test_pension_and_toss_coexist_isolated_by_scope(tmp_path):
    path = tmp_path / "trade_ledger.sqlite3"
    store = make_store(path)
    ingest_pension_trades(
        [ptrade("매수", "BUY"), ptrade("매도", "SELL")], store, file_sha256="ab" * 32
    )

    toss_event = TradeLedgerEvent(
        source="toss-orders-v1",
        account_scope="toss-acct-" + "a" * 24,
        source_record_key="toss-key-0001",
        event_kind="EXECUTION",
        supersedes_source_record_key=None,
        trade_date=date(2026, 8, 1),
        transaction_type_raw="매수",
        instrument_raw="005930",
        quantity="1",
        unit_price="70000",
        amount="70000",
        medium_raw="TossApp",
    )
    toss = store.append_batch(
        (toss_event,),
        import_audit=ImportAuditMeta("cd" * 32, "toss-orders-sync-v1"),
    )
    assert toss.status == "SAVED" and toss.inserted_count == 1

    grouped = dict(
        sqlite3.connect(path)
        .execute("SELECT source, count(*) FROM trade_ledger_events GROUP BY source")
        .fetchall()
    )
    assert grouped == {"retirement_excel_v1": 2, "toss-orders-v1": 1}
