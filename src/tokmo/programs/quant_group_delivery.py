"""Quant decision-card delivery to the existing private tokmo Telegram group.

Product contract 2026-09-03 §8.5 (group conversation, no Inline callbacks)
and §9.3 (default live-feedback budget: cycle 1 + cycle 2 + at most one RCA
confirmation, cap 3, unused RCA reserve never consumed, no recurring
activation). This module reuses the existing decision-card renderer output
and the one-attempt receipt store unchanged: every market keeps its own
single attempt, SENT/FAILED/UNKNOWN consume it, UNKNOWN is never replayed,
and each cycle has a distinct activation identity.

What is added: an explicit-chat cycle gate, a compact visible binding
marker on every part (``🔖 QC<cycle>-<market>-<report sha12>/<part>``), a
durable binding sidecar written before transport, and a read-only reply
resolver that maps a later user reply (reply-to id or copied marker) to the
exact report/market and then to one symbol. Reply *processing* (preview,
approval, any playbook write) is NOT implemented here; ``processed`` is
always False and ``writes`` is always zero.

The transport is an injected plain callable. Nothing here imports a
provider, an order/account/Ledger/playbook path, a clock or the process
environment, and no secret is accepted or persisted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
from types import MappingProxyType

from tokmo.programs.quant_telegram_card import (
    MESSAGE_CHAR_CAP, MarketMessageV1, run_quant_telegram_card_canary,
)
from tokmo.values.instrument_display import display_or_symbol
from tokmo.records.quant_group_delivery_receipts import (
    CYCLE_PURPOSES, LIVE_SEND_CAP, RCA_CYCLE, activation_id_for, valid_chat_id,
    valid_defect_ref,
)

MARKER_RE = re.compile(r"QC(?P<cycle>[1-9])-(?P<market>KR|US)-(?P<sha12>[0-9a-f]{12}|noreport)/(?P<part>[1-9][0-9]*)")
MARKER_PREFIX = "🔖 "
NO_WRITES = MappingProxyType({"playbook_files": 0, "ledger": 0, "order": 0, "account": 0, "provider": 0})
_SYMBOL_RE_CACHE: dict = {}


@dataclass(frozen=True)
class ReplyBindingV1:
    """Resolution result only; never a processed reply."""

    status: str
    activation_id: str | None = None
    cycle: int | None = None
    market: str | None = None
    report_sha256: str | None = None
    completed_session_date: str | None = None
    part_key: str | None = None
    part_status: str | None = None
    symbol: str | None = None
    name: str | None = None
    candidates: tuple = ()
    processed: bool = False
    writes: MappingProxyType = field(default_factory=lambda: NO_WRITES)


# --- visible binding markers -------------------------------------------------

def _chars(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def marker_for(cycle: int, market: str, report_sha256: str, part_index: int) -> str:
    sha12 = report_sha256[:12] if report_sha256 else "noreport"
    return f"QC{cycle}-{market}-{sha12}/{part_index}"


def symbols_in_part(text: str, cards: tuple, *, market: str) -> tuple:
    """(symbol, name) pairs whose exact card heading line occurs in this part."""
    lines = set(text.split("\n"))
    return tuple((symbol, name) for symbol, name in cards
                 if ("• " + display_or_symbol(market, symbol, name) in lines
                     or "• " + name + " (" + symbol + ")" in lines))


def bind_markers(messages: tuple, cards_by_market: dict, *, cycle: int) -> tuple:
    """Append one marker line to every part; return (marked messages, binding parts).

    Fails closed with ``marker_overflow`` when a marked part would exceed the
    Telegram message cap, before any transport."""
    marked, parts = [], {}
    for message in messages:
        cards = tuple(cards_by_market.get(message.market, ()))
        new_parts = []
        for index, text in enumerate(message.parts, 1):
            marker = marker_for(cycle, message.market, message.report_sha256, index)
            body = f"{text}\n\n{MARKER_PREFIX}{marker}"
            if _chars(body) > MESSAGE_CHAR_CAP:
                raise ValueError("marker_overflow")
            new_parts.append(body)
            parts[f"{message.market}:{index}"] = {
                "marker": marker, "market": message.market, "report_sha256": message.report_sha256,
                "completed_session_date": message.completed_session_date,
                "symbols": symbols_in_part(text, cards, market=message.market)}
        marked.append(MarketMessageV1(message.market, message.completed_session_date, message.identity_sha256,
                                      message.report_sha256, tuple(new_parts), message.grounded_sections,
                                      message.unavailable_sections))
    return tuple(marked), parts


# --- budget ------------------------------------------------------------------

def _consumed_cycles(receipts, vertical_id: str) -> dict:
    """{cycle: receipt status} for every cycle of this vertical with a receipt."""
    consumed = {}
    for cycle in range(1, LIVE_SEND_CAP + 1):
        receipt = receipts.read(activation_id_for(vertical_id, cycle))
        if receipt is not None:
            consumed[cycle] = str(receipt.get("status"))
    return consumed


def budget_status(receipts, bindings, *, vertical_id: str) -> dict:
    consumed = _consumed_cycles(receipts, vertical_id)
    planned_complete = 1 in consumed and 2 in consumed
    if RCA_CYCLE in consumed:
        rca = "consumed"
    elif planned_complete:
        rca = "unused"  # never consumed merely to reach the cap
    else:
        rca = "available"
    next_cycle = next((cycle for cycle in (1, 2) if cycle not in consumed), None)
    return {"vertical_id": vertical_id, "cap": LIVE_SEND_CAP, "consumed": len(consumed),
            "remaining": LIVE_SEND_CAP - len(consumed),
            "cycles": {str(cycle): consumed.get(cycle) for cycle in range(1, LIVE_SEND_CAP + 1)},
            "planned_complete": planned_complete, "rca_reserve": rca, "next_cycle": next_cycle}


def admit_cycle(receipts, *, vertical_id: str, cycle, rca_defect_ref) -> str | None:
    """Reason the cycle may not be attempted now, else None."""
    if type(cycle) is not int or cycle < 1 or cycle > LIVE_SEND_CAP:
        return "cap_exhausted"
    consumed = _consumed_cycles(receipts, vertical_id)
    if cycle in consumed:
        return "cycle_consumed"
    if len(consumed) >= LIVE_SEND_CAP:
        return "cap_exhausted"
    if cycle == 2 and 1 not in consumed:
        return "cycle1_not_attempted"
    if cycle == RCA_CYCLE:
        if not (1 in consumed and 2 in consumed):
            return "planned_cycles_incomplete"
        if not valid_defect_ref(rca_defect_ref):
            return "rca_defect_missing"
    return None


# --- one bounded live cycle ----------------------------------------------------

def run_quant_group_delivery(*, activation: dict, chat_id: str, receipts, bindings, messages: tuple,
                             cards_by_market: dict, telegram_send, now: datetime) -> dict:
    """One explicit-chat, one-cycle, one-attempt delivery of the rendered cards.

    Order: gate → explicit chat match → budget admission → markers (cap
    check) → durable binding → existing one-attempt canary (reservation
    before transport, per-market settlement, UNKNOWN permanent)."""
    result = {"off": None, "activation_id": None, "cycle": None, "chat_id": chat_id,
              "rca_defect_ref": None, "markers": {}, "reserved": 0, "sent": 0, "failed": 0,
              "unknown": 0, "skipped": 0, "deduped": {}, "parts": []}
    if activation.get("mode") != "LIVE":
        result["off"] = activation.get("reason") or "mode"
        return result
    if not valid_chat_id(chat_id) or chat_id != activation.get("chat_id"):
        result["off"] = "chat_mismatch"
        return result
    vertical_id, cycle = activation.get("vertical_id"), activation.get("cycle")
    defect = activation.get("rca_defect_ref")
    refusal = admit_cycle(receipts, vertical_id=vertical_id, cycle=cycle, rca_defect_ref=defect)
    if refusal is not None:
        result["off"] = refusal
        return result
    activation_id = activation_id_for(vertical_id, cycle)
    result.update(activation_id=activation_id, cycle=cycle, rca_defect_ref=defect)
    try:
        marked, parts = bind_markers(messages, cards_by_market, cycle=cycle)
    except ValueError as error:
        result["off"] = str(error)
        return result
    # Report dedupe is decided by the inner canary; restrict the binding to
    # the markets it will actually attempt so the sidecar never over-claims.
    attempted = _eligible_markets(receipts, marked, override=bool(activation.get("same_report_ok")))
    parts = {key: value for key, value in parts.items() if value["market"] in attempted}
    if not parts:
        inner = run_quant_telegram_card_canary(
            activation={"mode": "CANARY", "activation_id": activation_id}, receipts=receipts,
            activation_id=activation_id, messages=marked, telegram_send=telegram_send, now=now,
            override_report_dedupe=bool(activation.get("same_report_ok")))
        result.update({key: inner[key] for key in ("off", "deduped")})
        return result
    bindings.write(activation_id, vertical_id=vertical_id, cycle=cycle, chat_id=chat_id,
                   purpose=CYCLE_PURPOSES[cycle], parts=parts, now=now, rca_defect_ref=defect)
    inner = run_quant_telegram_card_canary(
        activation={"mode": "CANARY", "activation_id": activation_id}, receipts=receipts,
        activation_id=activation_id, messages=marked, telegram_send=telegram_send, now=now,
        override_report_dedupe=bool(activation.get("same_report_ok")))
    result.update({key: inner[key] for key in ("off", "reserved", "sent", "failed", "unknown",
                                               "skipped", "deduped", "parts")})
    result["markers"] = {key: value["marker"] for key, value in parts.items()}
    return result


def _eligible_markets(receipts, messages: tuple, *, override: bool) -> set:
    eligible = set()
    for message in messages:
        prior = receipts.report_attempted(message.report_sha256) if message.report_sha256 else None
        if prior is not None and (not override or prior[1] in ("UNKNOWN", "RESERVED")):
            continue
        eligible.add(message.market)
    return eligible


# --- reply resolution (read-only) ---------------------------------------------

def _symbol_pattern(symbol: str):
    pattern = _SYMBOL_RE_CACHE.get(symbol)
    if pattern is None:
        pattern = re.compile(r"(?<![0-9A-Za-z.\-])" + re.escape(symbol) + r"(?![0-9A-Za-z.\-])")
        _SYMBOL_RE_CACHE[symbol] = pattern
    return pattern


def _mentioned(text: str, candidates: tuple) -> tuple:
    return tuple((symbol, name) for symbol, name in candidates
                 if _symbol_pattern(symbol).search(text) or (name and name in text))


def _part_status(receipts, activation_id: str, part_key: str) -> str | None:
    receipt = receipts.read(activation_id)
    if receipt is None:
        return None
    for part in receipt.get("parts", ()):
        if part.get("key") == part_key:
            return part.get("status")
    return None


def _locate_by_reply(receipts, bindings, reply_to_message_id: int) -> tuple | None:
    wanted = str(reply_to_message_id)
    for binding in bindings.all():
        receipt = receipts.read(binding["activation_id"])
        if receipt is None:
            continue
        for part in receipt.get("parts", ()):
            if part.get("provider_message_id") == wanted and part.get("key") in binding["parts"]:
                return binding, part["key"]
    return None


def _locate_by_marker(bindings, text: str) -> tuple | None:
    found = MARKER_RE.search(text)
    if found is None:
        return None
    marker = found.group(0)
    for binding in bindings.all():
        for key, part in binding["parts"].items():
            if part.get("marker") == marker:
                return binding, key
    return None


def resolve_group_reply(*, receipts, bindings, chat_id: str, reply_to_message_id: int | None,
                        text: str) -> ReplyBindingV1:
    """Map a user reply in the group to exact report/market, then one symbol.

    Resolution only: no preview, approval, playbook, Ledger, order or account
    write happens here, and ``processed`` is always False."""
    if type(text) is not str:
        return ReplyBindingV1("unbound")
    located = None
    if type(reply_to_message_id) is int:
        located = _locate_by_reply(receipts, bindings, reply_to_message_id)
    if located is None:
        located = _locate_by_marker(bindings, text)
    if located is None:
        return ReplyBindingV1("unbound")
    binding, key = located
    if not valid_chat_id(chat_id) or chat_id != binding.get("chat_id"):
        return ReplyBindingV1("wrong_chat")
    part = binding["parts"][key]
    candidates = tuple((str(symbol), str(name)) for symbol, name in part.get("symbols", ()))
    common = dict(activation_id=binding["activation_id"], cycle=binding["cycle"], market=part["market"],
                  report_sha256=part["report_sha256"], completed_session_date=part["completed_session_date"],
                  part_key=key, part_status=_part_status(receipts, binding["activation_id"], key),
                  candidates=candidates)
    mentioned = _mentioned(text, candidates)
    if len(mentioned) == 1:
        symbol, name = mentioned[0]
        return ReplyBindingV1("bound", symbol=symbol, name=name, **common)
    if len(mentioned) > 1:
        return ReplyBindingV1("symbol_ambiguous", **common)
    if len(candidates) == 1:
        symbol, name = candidates[0]
        return ReplyBindingV1("bound", symbol=symbol, name=name, **common)
    return ReplyBindingV1("symbol_missing", **common)
