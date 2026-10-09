"""Rotation policy: the user's standing plan plus the model's leeway (v0.6).

Buying is bounded by a price band the user approved in advance. Selling is
bounded by a return on the lot's *actual* fill price: at least
``sell_min_pct`` (before costs), with ``sell_target_pct`` as the default
goal and no upper cap. The model may move today's prices inside those
rules; the GLM path clamps and falls back, the file path never does. The
policy can therefore never widen the experiment.
"""

import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from tokmo.values.order_execution import RiskEnvelope, aware

POLICY_SCHEMA = "rotation-policies-v1"
_IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,60}")
_DECISION_KEYS = {
    "symbol",
    "action",
    "buy_price",
    "sell_price",
    "expires_on",
    "reason",
    "evidence_ids",
}
# KRX price steps (won): upper bound exclusive -> tick.
_KR_TICKS = (
    (Decimal(2000), Decimal(1)),
    (Decimal(5000), Decimal(5)),
    (Decimal(20000), Decimal(10)),
    (Decimal(50000), Decimal(50)),
    (Decimal(200000), Decimal(100)),
    (Decimal(500000), Decimal(500)),
)
_KR_TOP_TICK = Decimal(1000)
_HUNDRED = Decimal(100)


def tick_size(price, market):
    if market != "KR":
        return Decimal("0.01") if price >= 1 else Decimal("0.0001")
    for bound, tick in _KR_TICKS:
        if price < bound:
            return tick
    return _KR_TOP_TICK


def align_price(price, market, side):
    """Snap to the exchange tick: buys round down, sells round up."""
    tick = tick_size(price, market)
    rounding = ROUND_FLOOR if side == "BUY" else ROUND_CEILING
    return (price / tick).quantize(Decimal(1), rounding=rounding) * tick


@dataclass(frozen=True)
class RotationPolicy:
    experiment_id: str
    account_scope: str
    symbol: str
    market: str
    quantity: Decimal
    initial_budget: Decimal
    protected_quantity: Decimal
    fee_buffer: Decimal
    default_buy: Decimal
    buy_floor: Decimal
    buy_ceiling: Decimal
    sell_target_pct: Decimal
    sell_min_pct: Decimal
    valid_until: datetime
    plan_version: str

    def __post_init__(self):
        for name in ("experiment_id", "plan_version"):
            if not isinstance(getattr(self, name), str) or not _IDENTITY.fullmatch(
                getattr(self, name)
            ):
                raise ValueError("policy_identity")
        if not self.account_scope or not self.symbol or self.market not in {"KR", "US"}:
            raise ValueError("policy_instrument")
        numbers = (
            self.quantity,
            self.initial_budget,
            self.protected_quantity,
            self.fee_buffer,
            self.default_buy,
            self.buy_floor,
            self.buy_ceiling,
            self.sell_target_pct,
            self.sell_min_pct,
        )
        if any(
            not isinstance(v, Decimal) or not v.is_finite() or v < 0 for v in numbers
        ):
            raise ValueError("policy_numbers")
        if (
            self.quantity < 1
            or self.quantity != self.quantity.to_integral_value()
            or self.initial_budget <= 0
        ):
            raise ValueError("policy_numbers")
        if not self.buy_floor <= self.default_buy <= self.buy_ceiling:
            raise ValueError("policy_band_order")
        if not 0 < self.sell_min_pct <= self.sell_target_pct <= _HUNDRED:
            raise ValueError("policy_sell_pct")
        if not aware(self.valid_until):
            raise ValueError("policy_expiry")

    def envelope(self):
        return RiskEnvelope(
            self.account_scope,
            self.symbol,
            self.market,
            self.quantity,
            self.initial_budget,
            self.protected_quantity,
            self.valid_until,
            self.plan_version,
        )

    def sell_floor_for(self, basis):
        """Lowest acceptable sell: basis × (1 + min%) rounded up to the tick."""
        return align_price(
            basis * (1 + self.sell_min_pct / _HUNDRED), self.market, "SELL"
        )

    def sell_target_for(self, basis):
        return align_price(
            basis * (1 + self.sell_target_pct / _HUNDRED), self.market, "SELL"
        )

    def document(self):
        """Model-facing statement of the rules; contains no account identity."""
        return {
            "default_buy": str(self.default_buy),
            "buy_bounds": [str(self.buy_floor), str(self.buy_ceiling)],
            "sell_target_pct": str(self.sell_target_pct),
            "sell_min_pct": str(self.sell_min_pct),
            "sell_rule": "sell_price >= actual fill price × (1 + sell_min_pct/100), rounded up to the tick; "
            "sell_target_pct is the default goal, not a cap",
            "quantity": str(self.quantity),
        }


def load_policies(raw):
    """Parse the operator-written policy file. Duplicate keys and NaN are refused."""

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    def reject_constant(name):
        raise ValueError("policy_numbers")

    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 65536:
        raise ValueError("policy_document")
    data = json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
    if (
        type(data) is not dict
        or set(data) != {"schema", "experiments"}
        or data["schema"] != POLICY_SCHEMA
        or type(data["experiments"]) is not list
        or not data["experiments"]
    ):
        raise ValueError("policy_document")
    fields = {f for f in RotationPolicy.__dataclass_fields__}
    decimals = fields - {
        "experiment_id",
        "account_scope",
        "symbol",
        "market",
        "plan_version",
        "valid_until",
    }
    policies = []
    for item in data["experiments"]:
        if type(item) is not dict or set(item) != fields:
            raise ValueError("policy_fields")
        values = dict(item)
        for name in decimals:
            if not isinstance(values[name], str):
                raise ValueError("policy_numbers")
            values[name] = Decimal(values[name])
        if not isinstance(values["valid_until"], str):
            raise ValueError("policy_expiry")
        values["valid_until"] = datetime.fromisoformat(values["valid_until"])
        policies.append(RotationPolicy(**values))
    if len({(p.account_scope, p.symbol, p.market) for p in policies}) != len(
        policies
    ) or len({p.experiment_id for p in policies}) != len(policies):
        raise ValueError("policy_duplicate_instrument")
    return tuple(policies)


def _price(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{1,16}(?:\.[0-9]{1,4})?", value
    ):
        return None
    number = Decimal(value)
    return number if number > 0 else None


def _clamp(value, floor, ceiling):
    return min(max(value, floor), ceiling)


BAND_FIELDS = (
    "default_buy",
    "buy_floor",
    "buy_ceiling",
    "sell_target_pct",
    "sell_min_pct",
)
_PROPOSAL_MARGIN = Decimal("0.02")


def apply_band_changes(policy, changes):
    """The policy with band fields replaced; raises ValueError if it would not validate."""
    if (
        type(changes) is not dict
        or not changes
        or any(k not in BAND_FIELDS for k in changes)
    ):
        raise ValueError("band_fields")
    values = {}
    for key, value in changes.items():
        number = _price(value if isinstance(value, str) else str(value))
        if number is None:
            raise ValueError("band_price")
        values[key] = number
    return RotationPolicy(**{**asdict(policy), **values})


def band_proposal(policy, *, explicit, model_buy, model_sell, basis, reason):
    """A user-reviewable policy revision, or None. Never applied here.

    The model's explicit ``band_proposal`` wins when it validates; otherwise a
    buy that had to be clamped proposes moving that edge just past it, and a
    sell under the minimum return proposes the lower minimum it implied.
    """
    changes = {}
    if type(explicit) is dict and explicit:
        candidate = {k: v for k, v in explicit.items() if k in BAND_FIELDS}
        proposal_reason = (
            explicit.get("reason")
            if isinstance(explicit.get("reason"), str)
            else reason
        )
        try:
            apply_band_changes(policy, candidate)
            changes, reason = candidate, proposal_reason
        except ValueError:
            changes = {}
    if not changes:
        if model_buy is not None and model_buy < policy.buy_floor:
            changes["buy_floor"] = str(
                align_price(model_buy * (1 - _PROPOSAL_MARGIN), policy.market, "BUY")
            )
        if model_buy is not None and model_buy > policy.buy_ceiling:
            changes["buy_ceiling"] = str(
                align_price(model_buy * (1 + _PROPOSAL_MARGIN), policy.market, "BUY")
            )
        if (
            model_sell is not None
            and basis is not None
            and model_sell < policy.sell_floor_for(basis)
        ):
            implied = ((model_sell / basis - 1) * _HUNDRED).quantize(
                Decimal("0.1"), rounding=ROUND_FLOOR
            )
            if implied >= Decimal("0.5"):
                changes["sell_min_pct"] = str(implied)
        if not changes:
            return None
        try:
            apply_band_changes(policy, changes)
        except ValueError:
            return None
    return {
        "changes": {k: str(v) for k, v in changes.items()},
        "reason": reason if isinstance(reason, str) and reason else "정책 경계 도달",
    }


def bound_decision(
    raw,
    *,
    policy,
    today,
    holding,
    fallback_evidence_id,
    evidence_ids=None,
    cost_basis=None,
):
    """Fold the model's answer into the policy (GLM path). Returns (decision_json, note, proposal).

    ``holding`` is whether the experiment already owns its lot: then only an
    EXIT (or HOLD) is meaningful and the sell floor is derived from
    ``cost_basis`` (the lot's actual fill price); otherwise only an ENTER (or
    HOLD) and the floor derives from the buy price. A held lot without a
    known fill price is a HOLD, never an estimate.
    """
    if not isinstance(today, date) or isinstance(today, datetime):
        raise ValueError("decision_day")
    if holding and cost_basis is None:
        decision = {
            "symbol": policy.symbol,
            "action": "HOLD",
            "buy_price": None,
            "sell_price": None,
            "expires_on": None,
            "reason": "실험 물량의 실제 체결가를 확인할 수 없어 매도 기준가를 계산하지 않고 보류했어.",
            "evidence_ids": [fallback_evidence_id],
        }
        return json.dumps(decision, ensure_ascii=False), "cost_basis_unknown", None
    note = "model"
    data = None
    explicit_proposal = None
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
    if type(data) is dict:
        explicit_proposal = data.pop("band_proposal", None)
    if type(data) is not dict or set(data) != _DECISION_KEYS:
        data, note = None, "model_unusable_default_band"
    reason = (
        data["reason"]
        if data and isinstance(data.get("reason"), str) and data["reason"]
        else None
    )
    citations = (
        data["evidence_ids"]
        if data and type(data.get("evidence_ids")) is list and data["evidence_ids"]
        else None
    )
    if citations is not None and evidence_ids is not None:
        # Models cite section names ("experiment", "limits") alongside real
        # evidence ids; a stray citation must not void an otherwise sound call.
        kept = [c for c in citations if isinstance(c, str) and c in evidence_ids]
        if len(kept) != len(citations):
            note = "model_citations_trimmed"
        citations = kept or [fallback_evidence_id]
    if reason is None or citations is None:
        note = (
            "model_unusable_default_band"
            if data is None
            else "model_reason_missing_default_band"
        )
        data = None
    action = data["action"] if data else None
    model_reason = reason
    if data and action == "HOLD":
        decision = {
            "symbol": policy.symbol,
            "action": "HOLD",
            "buy_price": None,
            "sell_price": None,
            "expires_on": None,
            "reason": reason,
            "evidence_ids": citations,
        }
        proposal = band_proposal(
            policy,
            explicit=explicit_proposal,
            model_buy=None,
            model_sell=None,
            basis=cost_basis,
            reason=reason,
        )
        return json.dumps(decision, ensure_ascii=False), note, proposal
    if data and action not in ({"EXIT"} if holding else {"ENTER"}):
        note = "model_action_mismatch_default_band"
        data = None
    buy = _price(data["buy_price"]) if data else None
    sell = _price(data["sell_price"]) if data else None
    if data is None or (not holding and buy is None) or sell is None:
        if data is not None:
            note = "model_price_missing_default_band"
            reason = "모델 가격이 없어 기본 밴드를 적용했어."
        else:
            reason = "모델 판단을 쓸 수 없어 기본 밴드를 적용했어."
        citations = [fallback_evidence_id]
        basis = (
            cost_basis
            if holding
            else align_price(policy.default_buy, policy.market, "BUY")
        )
        buy = None if holding else basis
        sell = policy.sell_target_for(basis)
        proposal = None
    else:
        if not holding:
            clamped = _clamp(buy, policy.buy_floor, policy.buy_ceiling)
            if clamped != buy:
                note = "model_price_clamped"
            buy = align_price(clamped, policy.market, "BUY")
        basis = cost_basis if holding else buy
        proposal = band_proposal(
            policy,
            explicit=explicit_proposal,
            model_buy=_price(data["buy_price"]),
            model_sell=sell,
            basis=basis,
            reason=model_reason,
        )
        floor = policy.sell_floor_for(basis)
        sell = align_price(sell, policy.market, "SELL")
        if sell < floor:
            note = "model_sell_below_minimum_raised"
            sell = floor
    if holding:
        decision = {
            "symbol": policy.symbol,
            "action": "EXIT",
            "buy_price": None,
            "sell_price": str(sell),
            "expires_on": None,
            "reason": reason,
            "evidence_ids": citations,
        }
    else:
        decision = {
            "symbol": policy.symbol,
            "action": "ENTER",
            "buy_price": str(buy),
            "sell_price": str(sell),
            "expires_on": today.isoformat(),
            "reason": reason,
            "evidence_ids": citations,
        }
    return json.dumps(decision, ensure_ascii=False), note, proposal


def policy_record(policy):
    """Receipt-safe copy: Decimals and the expiry as strings."""
    return json.loads(json.dumps(asdict(policy), default=str, sort_keys=True))
