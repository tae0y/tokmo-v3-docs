"""Fail-closed cycle gate and durable binding receipts for Quant decision-card
delivery to the existing private tokmo Telegram group (contract §8.5 / §9.3).

Gate: one mode-0600 JSON file with an exact key set that arms exactly ONE
live cycle for ONE explicit chat id. Absence, invalidity, expiry, a purpose
that does not match the cycle, a cycle outside the cap, or an RCA cycle
without a documented defect reference all resolve to OFF. There is no
schedule key and no recurring mode: a gate is consumed by the attempt it
authorizes and must be re-armed by an operator for the next cycle.

Binding receipt: one JSON file per activation id, written BEFORE transport,
recording the visible marker, market, exact report digest, completed
session and the (symbol, name) pairs of every message part so a later user
reply in the same group can be resolved to exact report/market and then
symbol. Provider message ids live in the existing one-attempt receipt
store; this module only adds the binding sidecar. No secret is ever
written here; no SQLite schema or migration is introduced.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re

from tokmo.records.atomic_json import read_atomic_json, write_atomic_json
from tokmo.records.quant_telegram_card_receipts import valid_activation_id

def _configured_group_chat_id():
    """The tokmo group chat id comes from the news delivery route config in the data root;
    nothing about the owner's group is hardcoded in source (no process-environment reads here)."""
    import json
    route = (Path(__file__).resolve().parents[3] / "var" / "data" / "news_delivery_route.json")
    try:
        value = json.loads(route.read_text()).get("chat_id")
    except (OSError, ValueError, AttributeError):
        value = None
    return str(value) if value else "-1000000000000"  # placeholder until a route is configured


TOKMO_GROUP_CHAT_ID = _configured_group_chat_id()
LIVE_SEND_CAP = 3
CYCLE_PURPOSES = {1: "cycle1_render_receive", 2: "cycle2_variation_reply_binding", 3: "rca_confirmation"}
RCA_CYCLE = 3
GROUP_GATE_KEYS = frozenset({"schema_version", "vertical_id", "cycle", "purpose", "chat_id", "mode",
                             "expires_at", "rca_defect_ref", "same_report_ok"})
GATE_MODES = frozenset({"OFF", "LIVE"})
VERTICAL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{3,48}$")
CHAT_ID_RE = re.compile(r"^-?[0-9]{1,20}$")
_DEFECT_REF_MAX = 120
_MAX_GATE_BYTES = 4096
_MAX_BINDING_BYTES = 65536
_OFF = {"mode": "OFF", "vertical_id": None, "cycle": None, "purpose": None, "chat_id": None,
        "activation_id": None, "expires_at": None, "rca_defect_ref": None, "same_report_ok": False}


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be aware")
    return value.astimezone(timezone.utc)


def valid_vertical_id(value) -> bool:
    return type(value) is str and VERTICAL_ID_RE.fullmatch(value) is not None


def valid_chat_id(value) -> bool:
    return type(value) is str and CHAT_ID_RE.fullmatch(value) is not None


def valid_defect_ref(value) -> bool:
    return type(value) is str and 0 < len(value.strip()) == len(value) <= _DEFECT_REF_MAX


def activation_id_for(vertical_id: str, cycle: int) -> str:
    """Distinct per-cycle dedupe identity: ``<vertical_id>-c<cycle>``."""
    if not valid_vertical_id(vertical_id) or type(cycle) is not int or cycle < 1:
        raise ValueError("invalid vertical id or cycle")
    activation_id = f"{vertical_id}-c{cycle}"
    if not valid_activation_id(activation_id):
        raise ValueError("invalid activation id")
    return activation_id


def read_group_activation(path: Path, *, now: datetime) -> dict:
    """Exact-shape gate read; anything unexpected is OFF with a reason."""
    now_utc = _aware(now)
    try:
        raw = Path(path).read_bytes()
        if len(raw) > _MAX_GATE_BYTES:
            return dict(_OFF, reason="gate_too_large")
        value = json.loads(raw)
        if type(value) is not dict or set(value) != GROUP_GATE_KEYS:
            return dict(_OFF, reason="gate_shape")
        if value["schema_version"] != 1 or value["mode"] not in GATE_MODES \
                or type(value["same_report_ok"]) is not bool:
            return dict(_OFF, reason="gate_shape")
        if not valid_vertical_id(value["vertical_id"]):
            return dict(_OFF, reason="gate_vertical_id")
        cycle = value["cycle"]
        if type(cycle) is not int or cycle < 1 or cycle > LIVE_SEND_CAP:
            return dict(_OFF, reason="gate_cycle")
        if value["purpose"] != CYCLE_PURPOSES[cycle]:
            return dict(_OFF, reason="gate_purpose")
        if not valid_chat_id(value["chat_id"]):
            return dict(_OFF, reason="gate_chat_id")
        defect = value["rca_defect_ref"]
        if (cycle == RCA_CYCLE and not valid_defect_ref(defect)) or (cycle != RCA_CYCLE and defect is not None):
            return dict(_OFF, reason="gate_rca_defect_ref")
        expires = datetime.fromisoformat(str(value["expires_at"]).replace("Z", "+00:00"))
        if expires.tzinfo is None or expires.astimezone(timezone.utc) <= now_utc:
            return dict(_OFF, reason="gate_expired")
        if value["mode"] == "OFF":
            return dict(_OFF, reason="gate_off")
        return {"mode": "LIVE", "vertical_id": value["vertical_id"], "cycle": cycle,
                "purpose": value["purpose"], "chat_id": value["chat_id"],
                "activation_id": activation_id_for(value["vertical_id"], cycle),
                "expires_at": expires.astimezone(timezone.utc).isoformat(),
                "rca_defect_ref": defect, "same_report_ok": value["same_report_ok"], "reason": None}
    except FileNotFoundError:
        return dict(_OFF, reason="gate_absent")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return dict(_OFF, reason="gate_invalid")


def write_group_activation(path: Path, *, vertical_id: str, cycle: int, chat_id: str, mode: str,
                           expires_at: datetime, rca_defect_ref: str | None = None,
                           same_report_ok: bool = False) -> None:
    """Operator-only gate write (arm one cycle / disarm); atomic, mode 0600."""
    if mode not in GATE_MODES or not valid_chat_id(chat_id) or type(same_report_ok) is not bool:
        raise ValueError("invalid gate parameters")
    if type(cycle) is not int or cycle < 1 or cycle > LIVE_SEND_CAP:
        raise ValueError("cycle outside live budget")
    if cycle == RCA_CYCLE:
        if not valid_defect_ref(rca_defect_ref):
            raise ValueError("rca cycle requires a documented defect reference")
    elif rca_defect_ref is not None:
        raise ValueError("defect reference only belongs to the rca cycle")
    payload = {"schema_version": 1, "vertical_id": vertical_id, "cycle": cycle,
               "purpose": CYCLE_PURPOSES[cycle], "chat_id": chat_id, "mode": mode,
               "expires_at": _aware(expires_at).isoformat(), "rca_defect_ref": rca_defect_ref,
               "same_report_ok": same_report_ok}
    activation_id_for(vertical_id, cycle)  # validates vertical id shape
    write_atomic_json(Path(path), json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
                      max_bytes=_MAX_GATE_BYTES)


class GroupBindingStore:
    """One immutable binding sidecar per activation id, written before transport."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def path(self, activation_id: str) -> Path:
        if not valid_activation_id(activation_id):
            raise ValueError("invalid activation id")
        return self.root / f"{activation_id}.binding.json"

    def read(self, activation_id: str) -> dict | None:
        path = self.path(activation_id)
        if not path.exists() and not path.is_symlink():
            return None
        return json.loads(read_atomic_json(path, max_bytes=_MAX_BINDING_BYTES))

    def write(self, activation_id: str, *, vertical_id: str, cycle: int, chat_id: str, purpose: str,
              parts: dict, now: datetime, rca_defect_ref: str | None) -> dict:
        """Persist the binding once; a second write for the same id is refused."""
        if self.read(activation_id) is not None:
            raise ValueError("binding already written")
        if not parts or not valid_chat_id(chat_id):
            raise ValueError("binding needs parts and a chat id")
        binding = {"schema_version": 1, "activation_id": activation_id, "vertical_id": vertical_id,
                   "cycle": cycle, "purpose": purpose, "chat_id": chat_id,
                   "rca_defect_ref": rca_defect_ref, "bound_at": _aware(now).isoformat(),
                   "parts": {str(key): {"marker": str(value["marker"]), "market": str(value["market"]),
                                        "report_sha256": str(value["report_sha256"]),
                                        "completed_session_date": str(value["completed_session_date"]),
                                        "symbols": [[str(symbol), str(name)] for symbol, name in value["symbols"]]}
                             for key, value in sorted(parts.items())}}
        write_atomic_json(self.path(activation_id),
                          json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(),
                          max_bytes=_MAX_BINDING_BYTES)
        return binding

    def all(self) -> tuple:
        """Every readable binding, sorted by file name; unreadable files are skipped."""
        found = []
        for path in sorted(self.root.glob("*.binding.json")):
            try:
                binding = json.loads(read_atomic_json(path, max_bytes=_MAX_BINDING_BYTES))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if type(binding) is dict and type(binding.get("parts")) is dict:
                found.append(binding)
        return tuple(found)
