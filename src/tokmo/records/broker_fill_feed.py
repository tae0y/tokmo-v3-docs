"""Read the pulse's confirmed broker fills without owning or migrating them.

The one-minute pulse already ingests every execution on the account into its
own dialogue store. The rotation runner needs the same facts to tell its own
orders apart from the owner's, so it reads that store strictly read-only: no
schema creation, no migration, no writes. A store that is absent or shaped
differently yields no fills rather than an error, because a missing feed must
never stop a settlement that is otherwise correct.
"""

import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

_COLUMNS = (
    "source_record_key",
    "side",
    "quantity",
    "unit_price",
    "amount",
    "currency",
    "filled_at",
    "symbol",
    "account_scope",
    "state",
)


def _decimal(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() and number > 0 else None


def _instant(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else None


def read_broker_fills(path, *, account_scope, symbol, table="trade_events"):
    """Confirmed fills for one instrument, oldest first.

    Rows missing an id, a positive quantity or price, or an aware fill time are
    dropped: a partial record is not evidence of a trade.
    """
    path = Path(path)
    if path.is_symlink() or not path.exists():
        return []
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        db.row_factory = sqlite3.Row
        try:
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            return []
        if not set(_COLUMNS) <= columns:
            return []
        try:
            rows = db.execute(
                f"SELECT {','.join(_COLUMNS)} FROM {table} WHERE account_scope=? AND symbol=? ORDER BY filled_at",
                (account_scope, symbol),
            ).fetchall()
        except sqlite3.Error:
            return []
    finally:
        db.close()
    fills = []
    for row in rows:
        quantity, price, filled_at = (
            _decimal(row["quantity"]),
            _decimal(row["unit_price"]),
            _instant(row["filled_at"]),
        )
        amount = _decimal(row["amount"]) or (
            quantity * price if quantity and price else None
        )
        if (
            not row["source_record_key"]
            or row["side"] not in {"BUY", "SELL"}
            or quantity is None
            or price is None
            or amount is None
            or filled_at is None
        ):
            continue
        fills.append(
            {
                "fill_id": row["source_record_key"],
                "side": row["side"],
                "quantity": quantity,
                "price": price,
                "amount": amount,
                "currency": row["currency"],
                "filled_at": filled_at,
                "state": row["state"],
            }
        )
    return fills
