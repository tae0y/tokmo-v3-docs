"""Read existing settlement/calendar/journal records without initializing them."""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tokmo.records.order_command_journal import OrderCommandJournal
from tokmo.values.quant_decisions import instant

KST = ZoneInfo("Asia/Seoul")


class ConfirmedMarketClosure(Exception):
    """Normal non-execution backed by today's official full-day records."""

    def __init__(self, market, trade_date, source_identity):
        super().__init__("market_holiday")
        self.market = market
        self.trade_date = trade_date
        self.source_identity = source_identity


@contextmanager
def read_database(path):
    path = Path(path).absolute()
    if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("read_store_missing_or_unsafe")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db
    finally:
        db.close()


class _ReadOnlyJournal(OrderCommandJournal):
    """Use the runner's existing read methods inside one SQLite snapshot."""

    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def _db(self):
        yield self.connection


def _confirmed_closure(data_root, market, days, now):
    from tokmo.records.calendar_cache import CALENDAR_PARSER_VERSION, CalendarCacheStore
    from tokmo.records.regular_session_history import RegularSessionHistoryStore
    from tokmo.values.session_gate import MARKET_TIMEZONE

    zone = ZoneInfo(MARKET_TIMEZONE[market])
    today = now.astimezone(zone).date()
    path = Path(data_root) / "ops/regular_session_history.sqlite3"
    if not path.exists() and not path.is_symlink():
        return None
    with read_database(path) as db:
        RegularSessionHistoryStore._validate(db)
        row = db.execute(
            "SELECT * FROM regular_session_history WHERE market=? AND local_date=?",
            (market.value, today.isoformat()),
        ).fetchone()
        fact = RegularSessionHistoryStore._row(row) if row else None
    if fact is None or fact["session"] != "no_regular":
        return None
    # A missing regular session alone cannot establish an entirely closed day.
    # Pair the admitted fact with the current cache's explicit empty sessions.
    path = Path(data_root) / "ops/calendar_cache.json"
    if any(p.is_symlink() for p in (path, *path.absolute().parents)):
        raise ValueError("calendar_path_unsafe")
    cache = CalendarCacheStore(path, clock=lambda: now).read()
    entry = None if cache is None else (cache.kr if market.value == "KR" else cache.us)

    def fresh(at):
        at = instant(at)
        return at <= now and at.astimezone(zone).date() == today

    if (
        entry is None
        or entry.trade_date != today.isoformat()
        or entry.source_trade_date != entry.trade_date
        or entry.parser_version != CALENDAR_PARSER_VERSION
        or entry.sessions
        or not fresh(entry.fetched_at)
        or not fresh(fact["captured_at"])
        or not fact["source_identity"].startswith(f"calendar:{market.value}:{today}:")
    ):
        raise ValueError("calendar_holiday_unconfirmed")
    start = datetime.combine(today, time.min, zone)
    end = start + timedelta(days=1)
    if not days or any(
        d.trade_date == today.isoformat()
        or any(s.start_utc < end and start < s.end_utc for s in d.sessions)
        for d in days
    ):
        raise ValueError("calendar_holiday_conflict")
    return ConfirmedMarketClosure(market.value, today, fact["source_identity"])


def saved_market_day(data_root, *, now, market="KR"):
    from tokmo.records.calendar_lookahead import (
        _day_from_payload,
        judge_lookahead_session,
    )
    from tokmo.values.session_gate import Market

    market = Market(market)
    path = Path(data_root) / "ops/calendar_lookahead.sqlite3"
    with read_database(path) as db:
        rows = db.execute(
            "SELECT trade_date,payload_json FROM calendar_lookahead WHERE market=? ORDER BY trade_date",
            (market.value,),
        ).fetchall()
    days = tuple(_day_from_payload(row["payload_json"].encode()) for row in rows)
    if any(
        d.market is not market or d.trade_date != r["trade_date"]
        for d, r in zip(days, rows)
    ):
        raise ValueError("calendar_identity_mismatch")
    verdict = judge_lookahead_session(market, days, now.astimezone(UTC))
    if verdict.reason not in {"open", "closed_between_sessions", "lookahead_exhausted"}:
        raise ValueError("calendar_closed_or_unknown:" + str(verdict.reason))
    closed = _confirmed_closure(data_root, market, days, now)
    if closed is not None:
        raise closed
    if not verdict.open or verdict.session_kind != "regularMarket":
        raise ValueError("calendar_closed_or_unknown:" + str(verdict.reason))
    today = next((d for d in days if d.trade_date == verdict.trade_date), None)
    if today is None:
        raise ValueError("calendar_day_missing")
    regular = next(s for s in today.sessions if s.kind == "regularMarket")
    endings = [
        s.end_utc
        for d in days
        for s in d.sessions
        if d.trade_date < today.trade_date and s.end_utc < now
    ]
    return dict(
        trade_date=today.trade_date,
        regular_open_at=regular.start_utc,
        last_closed_at=max(endings) if endings else None,
    )


def read_rotation_context(
    state_root, policy, *, now, regular_open_at, last_closed_at=None
):
    root = Path(state_root)
    records = []
    for path in (root / "settlements").glob(f"{policy.market}-*.json"):
        if path.is_symlink():
            raise ValueError("settlement_path_unsafe")
        value = json.loads(path.read_text())
        at = instant(value["at"])
        if at > now:
            continue
        if value.get("market") != policy.market:
            raise ValueError("settlement_market")
        matching = [
            r
            for r in value.get("results", [])
            if r.get("experiment_id") == policy.experiment_id
        ]
        if len(matching) == 1:
            records.append((at, matching[0]))
    if not records:
        raise ValueError("settlement_missing")
    settled, result = max(records, key=lambda item: item[0])
    if result.get("status") != "OK":
        raise ValueError("settlement_failed")
    # The installed job settles before today's open. A previous evening job is
    # also accepted when the saved calendar actually supplies its final close.
    morning = (
        settled.astimezone(KST).date() == now.astimezone(KST).date()
        and settled < regular_open_at
    )
    if not morning and (last_closed_at is None or settled <= last_closed_at):
        raise ValueError("settlement_stale")
    with read_database(root / "orders.sqlite3") as db:
        journal = _ReadOnlyJournal(db)
        row = db.execute(
            "SELECT config_json FROM experiment_books WHERE mode=? AND experiment_id=?",
            ("live", policy.experiment_id),
        ).fetchone()
        if row is None:
            raise ValueError("experiment_book_missing")
        expected = json.loads(json.dumps(asdict(policy.envelope()), default=str))
        if json.loads(row[0]) != expected:
            raise ValueError("experiment_configuration_mismatch")
        book = journal.experiment_book(policy.experiment_id, mode="live")
        basis = journal.experiment_lot_cost(policy.experiment_id, mode="live")
        opened = journal.open_commands(
            mode="live", account_scope=policy.account_scope, symbol=policy.symbol
        )
        fills = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT fill_json FROM experiment_fills WHERE mode=? AND experiment_id=?",
                ("live", policy.experiment_id),
            )
        ]
        if any(instant(f["observed_at"]) > settled for f in fills):
            raise ValueError("inventory_changed_after_settlement")
    return dict(
        quantity=str(book["quantity"]),
        cash=str(book["cash"]),
        frozen=book["frozen"],
        cost_basis=str(basis) if basis is not None else None,
        settled_at=settled.isoformat(),
        last_closed_at=last_closed_at.isoformat() if last_closed_at else None,
        regular_open_at=regular_open_at.isoformat(),
        settled_before_today_open=morning,
        open_commands=[
            {k: r[k] for k in ("status", "operation", "updated_at")} for r in opened
        ],
        fills=sorted(fills, key=lambda f: f["observed_at"])[-10:],
    )
