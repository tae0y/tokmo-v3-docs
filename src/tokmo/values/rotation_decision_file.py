"""The analysis→execution handoff file (``rotation-decision-file-v1``).

The analysis side (a separate model job) writes one file per instrument and
trade date; the runner consumes it only when its status is COMPLETE (or
WAITING_APPROVAL with the runner's own same-day approval). Everything the
runner can check without the broker is checked here: exact shape, identity,
times, prices on the exchange tick, quantity, amounts and the band. Nothing
is corrected — an invalid file is a failed day, never a substituted price.
"""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from tokmo.values.order_execution import aware
from tokmo.values.rotation_policy import BAND_FIELDS, RotationPolicy, align_price

SCHEMA = "rotation-decision-file-v1"
STATUSES = frozenset(
    {"RUNNING", "COMPLETE", "WAITING_APPROVAL", "FAILED", "EXPIRED", "SKIPPED"}
)
CONSUMABLE = frozenset({"COMPLETE", "WAITING_APPROVAL"})
REQUIRED_KEYS = frozenset(
    {
        "schema",
        "status",
        "experiment_id",
        "account_scope",
        "trade_date",
        "analysis_id",
        "producer",
        "started_at",
        "produced_at",
        "data_as_of",
        "valid_until",
        "decision",
        "band_proposal",
        "analysis_summary",
        "failure_reason",
    }
)
OPTIONAL_KEYS = frozenset(
    {"proposal_id", "revision", "report_id", "policy_digest", "failure_code"}
)
DECISION_KEYS = frozenset(
    {
        "symbol",
        "action",
        "buy_price",
        "sell_price",
        "expires_on",
        "quantity",
        "total_amount",
        "reason",
        "evidence_ids",
    }
)
_IDENTITY = re.compile(r"[A-Za-z0-9_.:-]{1,120}")
_PRICE = re.compile(r"[0-9]{1,16}(?:\.[0-9]{1,4})?")
MAX_BYTES = 65536
MAX_AGE_SECONDS = 2 * 3600


class DecisionFileError(ValueError):
    """``code`` is stable for receipts; the message is for humans."""

    def __init__(self, code, message=""):
        super().__init__(code if not message else f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class DecisionFile:
    status: str
    trade_date: date
    analysis_id: str
    producer: str
    produced_at: datetime
    decision: dict | None
    band_proposal: dict | None
    analysis_summary: str
    failure_reason: str | None
    failure_code: str | None
    proposal_id: str | None
    revision: int | None
    in_band: bool | None


def _time(data, key):
    value = data.get(key)
    if not isinstance(value, str):
        raise DecisionFileError("time_missing", key)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise DecisionFileError("time_invalid", key)
    if not aware(parsed):
        raise DecisionFileError("time_naive", key)
    return parsed


def _price(value, key):
    if not isinstance(value, str) or not _PRICE.fullmatch(value):
        raise DecisionFileError("price_invalid", key)
    number = Decimal(value)
    if number <= 0:
        raise DecisionFileError("price_invalid", key)
    return number


def _decision(raw, *, policy, trade_date, cost_basis):
    if type(raw) is not dict or set(raw) != DECISION_KEYS:
        raise DecisionFileError("decision_shape")
    if raw["symbol"] != policy.symbol:
        raise DecisionFileError("decision_symbol")
    action = raw["action"]
    if action not in {"ENTER", "EXIT", "HOLD"}:
        raise DecisionFileError("decision_action")
    if not isinstance(raw["reason"], str) or not 1 <= len(raw["reason"]) <= 1200:
        raise DecisionFileError("decision_reason")
    ids = raw["evidence_ids"]
    if (
        type(ids) is not list
        or not ids
        or any(not isinstance(i, str) or not i.strip() for i in ids)
    ):
        raise DecisionFileError("decision_evidence")
    if not isinstance(raw["quantity"], str) or not raw["quantity"].isdigit():
        raise DecisionFileError("decision_quantity")
    # A multi-share experiment fills and empties over several days, so the file
    # may name any part of the capacity. Which part is right today depends on
    # the book, which only the runner holds: it checks the exact share count
    # against the inventory it re-settles immediately before sending.
    quantity = Decimal(raw["quantity"])
    if not 0 < quantity <= policy.quantity:
        raise DecisionFileError("decision_quantity")
    buy = (
        _price(raw["buy_price"], "buy_price") if raw["buy_price"] is not None else None
    )
    sell = (
        _price(raw["sell_price"], "sell_price")
        if raw["sell_price"] is not None
        else None
    )
    for side, price in (("BUY", buy), ("SELL", sell)):
        if price is not None and align_price(price, policy.market, side) != price:
            raise DecisionFileError("price_off_tick", side)
    if action == "ENTER":
        if buy is None or sell is None or raw["expires_on"] != trade_date.isoformat():
            raise DecisionFileError("enter_fields")
        if sell <= buy:
            raise DecisionFileError("enter_spread")
        expected = buy * quantity
        # Before the fill only the limit price is known: the sell leg must
        # clear the minimum return over it (v0.6 A6; no upper cap).
        in_band = (
            policy.buy_floor <= buy <= policy.buy_ceiling
            and sell >= policy.sell_floor_for(buy)
        )
    elif action == "EXIT":
        if buy is not None or sell is None or raw["expires_on"] is not None:
            raise DecisionFileError("exit_fields")
        expected = sell * quantity
        # The floor derives from the lot's actual fill price; without it the
        # band cannot be judged (None), never estimated.
        in_band = (
            None if cost_basis is None else sell >= policy.sell_floor_for(cost_basis)
        )
    else:
        if buy is not None or sell is not None or raw["expires_on"] is not None:
            raise DecisionFileError("hold_fields")
        expected, in_band = None, True
    if expected is None:
        if raw["total_amount"] is not None:
            raise DecisionFileError("total_amount")
    elif _price(raw["total_amount"], "total_amount") != expected:
        raise DecisionFileError("total_amount")
    return dict(raw), in_band


def _band_proposal(raw):
    if raw is None:
        return None
    if (
        type(raw) is not dict
        or "reason" not in raw
        or not isinstance(raw["reason"], str)
        or not raw["reason"]
    ):
        raise DecisionFileError("band_proposal_shape")
    changes = {k: v for k, v in raw.items() if k != "reason"}
    if not changes or any(k not in BAND_FIELDS for k in changes):
        raise DecisionFileError("band_proposal_fields")
    for key, value in changes.items():
        _price(value, key)
    return {"changes": {k: str(v) for k, v in changes.items()}, "reason": raw["reason"]}


def parse_decision_file(raw, *, policy, now, trade_date, cost_basis=None):
    """Validate one handoff document for ``policy`` on ``trade_date``.

    Raises ``DecisionFileError`` with a stable code. Non-consumable statuses
    (RUNNING/FAILED/EXPIRED/SKIPPED) are returned with ``decision=None`` so the caller
    can wait or end the day; the freshness rule applies to consumable ones.
    """

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DecisionFileError("duplicate_key", key)
            result[key] = value
        return result

    def reject_constant(name):
        raise DecisionFileError("number_constant", name)

    if (
        type(policy) is not RotationPolicy
        or not aware(now)
        or not isinstance(trade_date, date)
        or isinstance(trade_date, datetime)
    ):
        raise ValueError("decision_file_inputs")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_BYTES:
        raise DecisionFileError("document_size")
    try:
        data = json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
    except json.JSONDecodeError as error:
        raise DecisionFileError("json_invalid", str(error)[:80])
    if (
        type(data) is not dict
        or not REQUIRED_KEYS <= set(data)
        or not set(data) <= REQUIRED_KEYS | OPTIONAL_KEYS
    ):
        raise DecisionFileError("document_shape")
    if data["schema"] != SCHEMA:
        raise DecisionFileError("schema")
    if data["status"] not in STATUSES:
        raise DecisionFileError("status")
    if (
        data["experiment_id"] != policy.experiment_id
        or data["account_scope"] != policy.account_scope
    ):
        raise DecisionFileError("identity")
    if data["trade_date"] != trade_date.isoformat():
        raise DecisionFileError("trade_date")
    for key in ("analysis_id", "producer"):
        if not isinstance(data[key], str) or not _IDENTITY.fullmatch(data[key]):
            raise DecisionFileError("identity", key)
    started, produced, as_of, valid_until = (
        _time(data, k)
        for k in ("started_at", "produced_at", "data_as_of", "valid_until")
    )
    if produced > now or started > produced or as_of > produced:
        raise DecisionFileError("time_order")
    status = data["status"]
    if status == "SKIPPED" and any(
        data.get(key) is not None
        for key in (
            "decision",
            "band_proposal",
            "proposal_id",
            "failure_reason",
            "failure_code",
        )
    ):
        raise DecisionFileError("skipped_payload")
    consumable = status in CONSUMABLE
    if consumable and (now - produced).total_seconds() > MAX_AGE_SECONDS:
        raise DecisionFileError("stale")
    if consumable and now >= valid_until:
        raise DecisionFileError("valid_until_passed")
    for key in ("analysis_summary",):
        if not isinstance(data[key], str):
            raise DecisionFileError("summary")
    if data["failure_reason"] is not None and not isinstance(
        data["failure_reason"], str
    ):
        raise DecisionFileError("failure_reason")
    proposal_id = data.get("proposal_id")
    revision = data.get("revision")
    if proposal_id is not None and (
        not isinstance(proposal_id, str) or not _IDENTITY.fullmatch(proposal_id)
    ):
        raise DecisionFileError("proposal_id")
    if revision is not None and (type(revision) is not int or revision < 0):
        raise DecisionFileError("revision")
    decision, in_band = (None, None)
    if consumable:
        decision, in_band = _decision(
            data["decision"],
            policy=policy,
            trade_date=trade_date,
            cost_basis=cost_basis,
        )
    elif data["decision"] is not None and type(data["decision"]) is not dict:
        raise DecisionFileError("decision_shape")
    return DecisionFile(
        status=status,
        trade_date=trade_date,
        analysis_id=data["analysis_id"],
        producer=data["producer"],
        produced_at=produced,
        decision=decision,
        band_proposal=_band_proposal(data["band_proposal"]),
        analysis_summary=data["analysis_summary"],
        failure_reason=data["failure_reason"],
        failure_code=data.get("failure_code")
        if isinstance(data.get("failure_code"), str)
        else None,
        proposal_id=proposal_id,
        revision=revision,
        in_band=in_band,
    )


def today_approval_matches(record, *, decision, policy, trade_date):
    """A same-day approval is only good for exactly this decision on this day."""
    if (
        type(record) is not dict
        or record.get("scope") != "today"
        or record.get("status") != "APPROVED"
    ):
        return False
    if (
        record.get("experiment_id") != policy.experiment_id
        or record.get("trade_date") != trade_date.isoformat()
    ):
        return False
    approved = record.get("decision")
    if type(approved) is not dict:
        return False
    return all(
        approved.get(k) == decision.get(k)
        for k in ("action", "buy_price", "sell_price", "quantity")
    )
