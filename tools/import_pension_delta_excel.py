#!/usr/bin/env python3
"""Offline pension delta XLSX import; --apply is the explicit write gate.

Uses the existing ledger and snapshot-first protocol. No AI, network, orders,
source-file deletion, balance inference, or cumulative row-number fabrication.
"""
import argparse
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from tokmo.facts.pension_excel import (
    _check_landing_file, _zip_entry_pass, _load_workbook_from_bytes,
)
from tokmo.records.pension_delta import (
    DELTA_SOURCE, PENSION_SCOPE, identify_delta, delta_admission_reason,
)
from tokmo.records.trade_ledger_canonical import canonical_decimal_string
from tokmo.records.trade_ledger_store import TradeLedgerStore
from tokmo.records.holdings_snapshots import HoldingsSnapshotStore, holdings_snapshot_store_path
from tokmo.records.pension_revisions import PensionRegistryStore
from tokmo.programs.pension_snapshot_adapter import run_transaction_detail_batch, classify_pension_batch
from tokmo.values.trade_ledger import TradeLedgerEvent

HEADERS = ('일자','거래구분','종목명','수량','단가','금액','거래매체')


def parse(path):
    raw = _check_landing_file(path)
    _zip_entry_pass(raw)
    wb, ws = _load_workbook_from_bytes(raw)
    try:
        if ws.max_column != 7 or not 2 <= ws.max_row <= 10001:
            raise ValueError('invalid_shape')
        if ws.merged_cells.ranges or any(d.hidden for d in ws.row_dimensions.values()) or any(
                d.hidden for d in ws.column_dimensions.values()):
            raise ValueError('hidden_or_merged_cells')
        if tuple(c.value for c in ws[1]) != HEADERS:
            raise ValueError('invalid_headers')
        events = []
        for cells in ws.iter_rows(min_row=2):
            if any(c.data_type == 'f' or c.hyperlink for c in cells):
                raise ValueError('formula_or_link')
            values = [c.value for c in cells]
            when, kind, name, qty, price, amount, channel = values
            if not isinstance(kind, str) or not isinstance(name, str) or not isinstance(channel, str):
                raise ValueError('invalid_text')
            if any(not v or len(v) > 4096 for v in (kind,name,channel)):
                raise ValueError('invalid_text')
            if kind not in {'ETF매수','ETF매도'}:
                raise ValueError('transaction_type_needs_review')
            if isinstance(when, datetime): when = when.date()
            elif isinstance(when, str): when = datetime.strptime(when, '%Y/%m/%d').date()
            if not isinstance(when, date): raise ValueError('invalid_date')
            numbers = []
            for value in (qty,price,amount):
                if isinstance(value, bool) or not isinstance(value,(str,int,float,Decimal)):
                    raise ValueError('invalid_number')
                number = canonical_decimal_string(str(value))
                if number is None or Decimal(number) <= 0: raise ValueError('invalid_number')
                numbers.append(number)
            if Decimal(numbers[0]) % 1: raise ValueError('fractional_quantity')
            events.append(TradeLedgerEvent(
                source=DELTA_SOURCE, account_scope=PENSION_SCOPE, source_record_key='batch',
                event_kind='EXECUTION', supersedes_source_record_key=None, trade_date=when,
                transaction_type_raw=kind, instrument_raw=name, quantity=numbers[0],
                unit_price=numbers[1], amount=numbers[2], medium_raw=channel, currency_raw='KRW'))
        return identify_delta(events), hashlib.sha256(raw).hexdigest()
    finally:
        wb.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--file', type=Path, required=True)
    p.add_argument('--root', type=Path, required=True, help='Data root containing stores/ and toss/')
    p.add_argument('--apply', action='store_true')
    a = p.parse_args()
    try:
        events, digest = parse(a.file)
        ledger_path = a.root / 'stores/trade_ledger.sqlite3'
        if ledger_path.is_file():
            with sqlite3.connect(f'file:{ledger_path.resolve()}?mode=ro', uri=True) as c:
                reason = delta_admission_reason(c, events)
            if reason: raise ValueError(reason)
        classification = classify_pension_batch(ledger_path, events, source=DELTA_SOURCE, account_scope=PENSION_SCOPE)
        if not a.apply:
            print(json.dumps(dict(outcome='PREVIEW', classification=classification, record_count=len(events), source_sha256=digest)))
            return 0
        # Do not create a shadow authority if --root points at the wrong directory.
        if not (a.root/'stores/pension_registry.sqlite3').is_file() or not holdings_snapshot_store_path(a.root).is_file():
            raise ValueError('canonical_stores_missing')
        now = datetime.now(timezone.utc)
        registry = PensionRegistryStore(a.root/'stores/pension_registry.sqlite3', clock=lambda: now)
        before = registry.read_active_revision()
        receipt = run_transaction_detail_batch(
            registry=registry,
            snapshot_store=HoldingsSnapshotStore(holdings_snapshot_store_path(a.root),clock=lambda: now),
            ledger_store=TradeLedgerStore(ledger_path, clock=lambda: now, id_factory=lambda: uuid4().hex),
            ledger_path=ledger_path, events=events, file_sha256=digest,
            importer_version='pension-delta-xlsx-v1', source=DELTA_SOURCE,
            account_scope=PENSION_SCOPE, clock=lambda: now)
        if registry.read_active_revision() != before:
            raise ValueError('holdings_changed')
        print(json.dumps(asdict(receipt), sort_keys=True))
        return 0 if receipt.outcome in {'APPENDED','DUPLICATE_NOOP'} else 2
    except (ValueError, OSError, sqlite3.Error) as error:
        # Never expose raw row values or arbitrary error messages.
        reason = str(error) if str(error) in {'batch_identity_conflict','canonical_stores_missing',
            'invalid_headers','invalid_shape','formula_or_link','transaction_type_needs_review'} else type(error).__name__
        print(json.dumps(dict(outcome='REFUSED', reason=reason)))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
