"""Dated pension statements plus later trades, for analysis only.

One private JSON manifest retains normalized imports and source references.
Every read recomputes from a statement; overlapping exports never append the
same trading day twice. The official holdings and execution ledger are untouched.
"""

import json
import re
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from tokmo.programs.pension_analysis_view import load_baseline
from tokmo.values.quant_decisions import instant, number

KST = ZoneInfo("Asia/Seoul")


def day(value):
    if isinstance(value, datetime):
        return value.date() if value.tzinfo is None else value.astimezone(KST).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).replace("/", "-"))


def end_of_day(value):
    return datetime.combine(day(value), time.max, KST)


def read_workbook(path):
    """Reuse workbook validation; a downloaded file need not already be 0600."""
    from tokmo.facts.pension_account_status_excel import (
        _validate_content,
        load_workbook_from_bytes,
        zip_entry_pass,
    )

    path = Path(path)
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("연금 엑셀 파일이 너무 큽니다")
    raw = path.read_bytes()
    zip_entry_pass(raw)
    workbook, sheet = load_workbook_from_bytes(raw)
    try:
        _validate_content(sheet, data_start_row=2, max_rows=1000)
        if sheet.max_column > 32:
            raise ValueError("연금 엑셀 열 수를 확인해 주세요")
        rows = list(sheet.values)
        headers = [str(v).strip() if v is not None else "" for v in rows[0]]
        if len(set(h for h in headers if h)) != len([h for h in headers if h]):
            raise ValueError("엑셀 열 이름이 중복됩니다")
        return raw, [
            dict(zip(headers, row))
            for row in rows[1:]
            if any(v is not None for v in row)
        ]
    finally:
        workbook.close()


def identity(name, identities):
    item = identities.get(name)
    if not isinstance(item, dict) or item.get("kind") not in {"etf", "other"}:
        raise ValueError("종목 식별정보가 필요합니다: " + str(name))
    if item["kind"] == "etf" and not re.fullmatch(
        r"[0-9A-Z]{6}", item.get("symbol", "")
    ):
        raise ValueError("ETF 종목코드를 확인해 주세요")
    return item


def statement_import(raw, rows, identities, *, through, available_at, label):
    end = end_of_day(through)
    if end > instant(available_at):
        raise ValueError(
            "아직 종료되지 않은 거래일을 잔고 반영 완료로 기록할 수 없습니다"
        )
    positions, excluded = [], []
    seen = set()
    for row in rows:
        name = row.get("종목명", row.get("상품명"))
        item = identity(name, identities)
        if item["kind"] == "other":
            excluded.append(name)
            continue
        symbol = item["symbol"]
        if symbol in seen:
            raise ValueError("중복 보유종목입니다")
        seen.add(symbol)
        qty = number(row.get("보유량", row.get("보유수량", row.get("수량"))), minimum=0)
        cost = number(row.get("매입금액"), minimum=0)
        if qty == 0:
            continue
        positions.append(
            dict(
                symbol=symbol,
                name=name,
                quantity=str(qty),
                avg_price=str(cost / qty),
                currency="KRW",
                market_raw="KR",
            )
        )
    if not positions:
        # Empty inventory is valid only when the workbook still contains its
        # non-ETF assets (cash etc.), not when parsing produced no source rows.
        if not excluded:
            raise ValueError("빈 계좌현황 파일입니다")
    digest = sha256(raw).hexdigest()
    return dict(
        id=digest + ":" + day(through).isoformat(),
        source_sha256=digest,
        source_kind="WORKBOOK",
        positions=positions,
        excluded=excluded,
        source_at=datetime.combine(day(through), time.min, KST).isoformat(),
        coverage_through=end.isoformat(),
        recorded_at=instant(available_at).isoformat(),
        label=label,
        basis="BROKER_STATEMENT_DATED",
        current_quantity_confirmed=False,
    )


def transaction_import(
    raw, rows, identities, *, complete_from, complete_through, available_at
):
    first, last = day(complete_from), day(complete_through)
    if first > last or end_of_day(last) > instant(available_at):
        raise ValueError("거래내역 조회기간을 확인해 주세요")
    digest = sha256(raw).hexdigest()
    trades, excluded = [], []
    for index, row in enumerate(rows, 2):
        name = row.get("종목명", row.get("상품명"))
        item = identity(name, identities)
        if item["kind"] == "other":
            excluded.append(dict(row=index, name=name))
            continue
        when = day(row["일자"])
        if not first <= when <= last:
            raise ValueError("거래행이 지정한 조회기간 밖에 있습니다")
        kind = str(row["거래구분"]).strip()
        side = {"ETF매수": "BUY", "ETF매도": "SELL"}.get(kind)
        trades.append(
            dict(
                id=digest + ":" + str(index),
                row=index,
                date=when.isoformat(),
                symbol=item["symbol"],
                name=name,
                currency="KRW",
                kind=kind,
                side=side,
                quantity=str(number(row["수량"], minimum=0)),
                price=str(number(row["단가"], minimum=0)),
                amount=str(number(row["금액"], minimum=0)),
            )
        )
    return dict(
        id=digest,
        source_sha256=digest,
        source_kind="WORKBOOK",
        rows=trades,
        excluded=excluded,
        complete_from=first.isoformat(),
        complete_through=last.isoformat(),
        available_at=instant(available_at).isoformat(),
    )


def new_manifest(account):
    if not isinstance(account, str) or not account.strip():
        raise ValueError("연금 계좌 식별자가 필요합니다")
    return dict(
        schema_version=1, account=account, statements=[], exports=[], resolutions=[]
    )


def seed_statement(annotation_path, *, as_of):
    value = load_baseline(annotation_path, as_of=as_of)
    value.update(
        id=value["snapshot_id"],
        source_kind="VERIFIED_SNAPSHOT",
        annotation=str(annotation_path),
    )
    return value


def add_import(manifest, entry, *, kind):
    result = deepcopy(manifest)
    collection = {"statement": "statements", "transactions": "exports"}[kind]
    old = next((r for r in result[collection] if r["id"] == entry["id"]), None)
    if old:
        # The same source bytes with changed date semantics require an
        # explicit correction, not a silent new interpretation.
        ignored = {"available_at", "recorded_at", "source_path", "comparison"}
        if {k: v for k, v in old.items() if k not in ignored} != {
            k: v for k, v in entry.items() if k not in ignored
        }:
            raise ValueError("같은 원본의 기준·내용이 달라졌습니다")
        return result
    result[collection].append(deepcopy(entry))
    return result


def _covered_trades(manifest, baseline, *, as_of, through):
    first = day(instant(baseline["coverage_through"])) + timedelta(days=1)
    last = day(through)
    exports = [e for e in manifest["exports"] if instant(e["available_at"]) <= as_of]
    selected, gaps = {}, []
    current = first
    while current <= last:
        candidates = [
            e
            for e in exports
            if day(e["complete_from"]) <= current <= day(e["complete_through"])
        ]
        if candidates:
            chosen = max(
                candidates, key=lambda e: (instant(e["available_at"]), e["id"])
            )
            selected[current.isoformat()] = chosen
        else:
            gaps.append(current.isoformat())
        current += timedelta(days=1)
    rows = [
        deepcopy(row)
        for date_key, export in selected.items()
        for row in export["rows"]
        if row["date"] == date_key
    ]
    rows.sort(key=lambda r: (r["date"], r.get("row", 0), r["id"]))
    issues = []
    corrections = [
        r for r in manifest.get("resolutions", []) if instant(r["recorded_at"]) <= as_of
    ]
    resolutions = {
        r["row_id"]: r
        for r in sorted(corrections, key=lambda r: instant(r["recorded_at"]))
    }
    removed = set()
    for row in rows:
        if row.get("side") in {"BUY", "SELL"}:
            continue
        resolution = resolutions.get(row["id"])
        if not resolution:
            issues.append(
                dict(
                    symbol=row["symbol"],
                    row_id=row["id"],
                    reason="거래구분/정정 관계 미확인",
                )
            )
            continue
        if resolution["action"] == "ignore":
            removed.add(row["id"])
            continue
        if resolution["action"] == "replace":
            original = next(
                (t for t in rows if t["id"] == resolution.get("replaces")), None
            )
            if (
                not original
                or original["symbol"] != row["symbol"]
                or original["id"] == row["id"]
            ):
                issues.append(
                    dict(
                        symbol=row["symbol"],
                        row_id=row["id"],
                        reason="정정 대상 원거래를 현재 기간에서 확인하지 못함",
                    )
                )
                continue
            removed.add(original["id"])
        row["side"] = resolution["side"]
    return [r for r in rows if r["id"] not in removed], gaps, issues


def analysis_view(manifest, *, as_of, through=None, baseline=None):
    now = instant(as_of)
    if manifest.get("schema_version") != 1:
        raise ValueError("지원하지 않는 연금 분석 자료입니다")
    statements = [s for s in manifest["statements"] if instant(s["recorded_at"]) <= now]
    if not statements:
        raise ValueError("이 시점에 확보한 연금 보유 기준이 없습니다")
    baseline = deepcopy(
        baseline
        or max(
            statements,
            key=lambda s: (instant(s["coverage_through"]), instant(s["recorded_at"])),
        )
    )
    end = instant(baseline["coverage_through"])
    for export in manifest["exports"]:
        if instant(export["available_at"]) <= now:
            end = max(end, end_of_day(export["complete_through"]))
    if through is not None:
        end = end_of_day(through)
    if end > now or end < instant(baseline["coverage_through"]):
        raise ValueError("분석 거래 반영 시점이 잘못되었습니다")
    trades, gaps, issues = _covered_trades(manifest, baseline, as_of=now, through=end)
    extended = end > instant(baseline["coverage_through"])
    rows = {
        r["symbol"]: dict(r, quantity_status="CALCULATED" if extended else "DATED")
        for r in baseline["positions"]
    }
    blocked = {i["symbol"] for i in issues}
    for trade in trades:
        symbol = trade["symbol"]
        row = rows.setdefault(
            symbol,
            dict(
                symbol=symbol,
                name=trade["name"],
                quantity="0",
                avg_price="0",
                currency="KRW",
                market_raw="KR",
            ),
        )
        if symbol in blocked:
            continue
        before = number(row["quantity"], minimum=0)
        qty = number(trade["quantity"], minimum=0)
        after = before + qty if trade.get("side") == "BUY" else before - qty
        if after < 0:
            issues.append(
                dict(
                    symbol=symbol,
                    row_id=trade["id"],
                    reason="매도수량이 계산 보유량보다 큼",
                )
            )
            blocked.add(symbol)
            continue
        if trade.get("side") == "BUY":
            price = (
                number(trade["price"], minimum=0)
                if trade.get("price") is not None
                else None
            )
            average = (
                number(row["avg_price"], minimum=0)
                if row.get("avg_price") is not None
                else None
            )
            row["avg_price"] = (
                str((before * (average or Decimal(0)) + qty * price) / after)
                if after and price is not None and (not before or average is not None)
                else None
            )
        row["quantity"] = str(after)
    for symbol in blocked:
        rows[symbol]["quantity_status"] = "UNRESOLVED"
    if gaps:
        for row in rows.values():
            row["quantity_status"] = "UNRESOLVED"
        issues.append(dict(symbol=None, reason="거래내역 조회기간 공백", dates=gaps))
    for row in rows.values():
        row.setdefault("quantity_status", "CALCULATED")
    return dict(
        baseline,
        account=manifest["account"],
        positions=[
            r
            for r in rows.values()
            if number(r["quantity"]) > 0 or r["quantity_status"] == "UNRESOLVED"
        ],
        verified_through=end.isoformat(),
        current_quantity_confirmed=False,
        basis="TRANSACTIONS_DERIVED" if extended else baseline["basis"],
        quantity_basis="거래 반영 계산 수량" if extended else "기준일 보유 수량",
        issues=issues,
        transaction_coverage_complete=not gaps,
        execution_ids=[t["id"] for t in trades],
        label="보유 기준 "
        + instant(baseline["coverage_through"]).strftime("%m-%d")
        + " · 거래 반영 "
        + end.strftime("%m-%d"),
    )


def compare_statement(manifest, statement, *, as_of):
    """Compare at the statement cutoff, never against a later trading state."""
    earlier = [
        s
        for s in manifest["statements"]
        if instant(s["coverage_through"]) < instant(statement["coverage_through"])
        and instant(s["recorded_at"]) <= instant(as_of)
    ]
    if not earlier:
        return dict(status="FIRST_BASELINE", differences=[])
    baseline = max(earlier, key=lambda s: instant(s["coverage_through"]))
    previous = analysis_view(
        manifest,
        as_of=as_of,
        through=day(instant(statement["coverage_through"])),
        baseline=baseline,
    )
    expected = {r["symbol"]: r for r in previous["positions"]}
    actual = {r["symbol"]: r for r in statement["positions"]}
    differences = []
    for symbol in sorted(expected.keys() | actual.keys()):
        a = expected.get(symbol, {}).get("quantity", "0")
        b = actual.get(symbol, {}).get("quantity", "0")
        if number(a) != number(b):
            differences.append(dict(symbol=symbol, calculated=a, statement=b))
    return dict(
        status="UNRESOLVED"
        if previous["issues"]
        else "MISMATCH"
        if differences
        else "MATCH",
        differences=differences,
        issues=previous["issues"],
        compared_through=statement["coverage_through"],
    )


def load_analysis(path, *, as_of):
    return analysis_view(json.loads(Path(path).read_text()), as_of=as_of)
