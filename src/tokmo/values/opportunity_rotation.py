"""PAPER-only KR cash vocabulary; fees are explicit assumptions, never broker rates."""

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from hashlib import sha256
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
INITIAL = Decimal(2000000)
PRINCIPAL_CAP = Decimal(1000000)
BENCHMARK = "069500"
START_DATE = "2026-09-28"


def instant(value):
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
        if (
            not isinstance(result, datetime)
            or result.tzinfo is None
            or result.utcoffset() is None
        ):
            raise ValueError("aware timestamp required")
        return result.astimezone(KST)
    except (TypeError, AttributeError) as exc:
        raise ValueError("aware timestamp required") from exc


def number(value):
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("exact decimal required")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    if (
        not result.is_finite()
        or abs(result) > Decimal("1e20")
        or result.as_tuple().exponent < -12
    ):
        raise ValueError("decimal out of range")
    return result


def won(value, *, positive=False):
    result = number(value)
    if result != result.to_integral_value() or result < 0 or (positive and result == 0):
        raise ValueError("whole nonnegative KRW required")
    return result


def text_amount(value):
    value = Decimal(value)
    return (
        format(value, "f").rstrip("0").rstrip(".")
        if "." in format(value, "f")
        else format(value, "f")
    )


def ratio(value):
    """Dimensionless results, including older long Decimal state, use 12 places."""
    value = Decimal(value)
    if not value.is_finite() or abs(value) > Decimal("1e20"):
        raise ValueError("ratio out of range")
    return text_amount(
        value.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_UP)
    )


def rounded(value):
    return value.quantize(Decimal(1), rounding=ROUND_HALF_UP)


def digest(value):
    return sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def require_text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("nonempty text required")
    return value


def symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9A-Z]{6}", value):
        raise ValueError("KR symbol required")
    return value


def fields(value, required, optional=()):
    if (
        not isinstance(value, dict)
        or set(value) - set(required) - set(optional)
        or set(required) - set(value)
    ):
        raise ValueError("unexpected or missing fields")


@dataclass(frozen=True)
class Costs:
    label: str
    buy_rate: str
    sell_rate: str
    stock_tax_rate: str
    benchmark_tax_rate: str

    def __post_init__(self):
        require_text(self.label)
        for key, value in asdict(self).items():
            if key != "label" and not Decimal(0) <= number(value) < Decimal(1):
                raise ValueError("invalid assumed rate")

    def buy(self, principal):
        return rounded(principal * number(self.buy_rate))

    def sell(self, proceeds, *, benchmark=False):
        return (
            rounded(proceeds * number(self.sell_rate)),
            rounded(
                proceeds
                * number(self.benchmark_tax_rate if benchmark else self.stock_tax_rate)
            ),
        )


def validate_session(value):
    fields(value, ("date", "open", "close", "source"))
    start, end = instant(value["open"]), instant(value["close"])
    if (
        start.date().isoformat() != value["date"]
        or end.date() != start.date()
        or start >= end
    ):
        raise ValueError("invalid official regular session")
    require_text(value["source"])
    return start, end


def final_bar_at(session):
    """Toss FAQ21/22: the closing auction belongs to its containing END bar.

    This is a provider label, not an extension of the official trading session.
    """
    _, closing = validate_session(session)
    return closing.replace(second=0, microsecond=0) + timedelta(minutes=1)


PROPOSAL_FIELDS = (
    "id",
    "symbol",
    "side",
    "trigger",
    "price",
    "quantity",
    "proposed_at",
    "reason",
    "expected_move",
    "invalidation",
    "review_at",
    "assumed_exit_price",
    "target",
    "weak_reason",
)


def validate_proposal(value):
    fields(value, PROPOSAL_FIELDS, ("protection", "stop_price"))
    for key in ("id", "reason", "expected_move", "invalidation"):
        require_text(value[key])
    symbol(value["symbol"])
    if value["side"] not in ("BUY", "SELL") or value["trigger"] not in (
        "BELOW",
        "ABOVE",
    ):
        raise ValueError("side or trigger invalid")
    if value["side"] == "BUY" and value["trigger"] != "BELOW":
        raise ValueError("buy requires downward limit reach")
    if type(value["quantity"]) is not int or value["quantity"] <= 0:
        raise ValueError("positive integer shares required")
    price = won(value["price"], positive=True)
    exit_price = won(value["assumed_exit_price"], positive=True)
    if value["side"] == "BUY" and exit_price >= price:
        raise ValueError("buy invalidation price must be below entry")
    if instant(value["review_at"]) <= instant(value["proposed_at"]):
        raise ValueError("review must follow proposal")
    if value["target"] not in ("NORMAL", "WEAK", "NONE"):
        raise ValueError("target preference invalid")
    if value["target"] == "WEAK":
        require_text(value["weak_reason"])
    elif value["weak_reason"] is not None:
        raise ValueError("weak reason only applies to weak target")

    if "protection" in value:
        protection = value["protection"]
        fields(protection, ("target_price", "stop_price"))
        if value["side"] != "BUY" or not won(
            protection["stop_price"], positive=True
        ) < price < won(protection["target_price"], positive=True):
            raise ValueError("buy protection requires stop < entry < target")
    if "stop_price" in value:
        if (
            value["side"] != "SELL"
            or value["trigger"] != "ABOVE"
            or won(value["stop_price"], positive=True) >= price
        ):
            raise ValueError("alternative sell stop must be below target")
