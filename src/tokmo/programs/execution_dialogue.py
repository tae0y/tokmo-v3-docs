"""Trade-event evaluation and stock-bot dialogue pipeline (Plan §19–§22).

G3-off by default: with ``activation_enabled=False`` nothing is ever sent
and nothing is polled — outbox rows accumulate as PENDING evidence. The AI
is consulted only when deterministic canonical material is sufficient and
its output type structurally cannot carry a guessed trade reason; its prose
surfaces only as the question's 추천 문구, which reaches the trade-history
layer solely when the user explicitly adopts it (예).

The 2026-08-21 user-approved UX contract abolishes the second confirmation.
Submitting a reason — 예, the 추가입력 prompt, or a direct natural reply —
saves it immediately under the same validator/durability gates and answers
with one receipt bubble carrying exactly one 수정 button. A failed save
never produces that receipt: the pending bytes survive, the retry is
bounded, and the user is told the truth. 수정 never rewrites or deletes the
prior USER_RAW block — it appends a linked correction entry, and the latest
correction is the current interpretation.

Old in-flight preview bubbles (확인/취소/수정) keep working exactly as they
did; nothing enqueues a new one. Callback queries bind to their exact
source message; callback_data is short fixed grammar. Every user-facing
string is masked: no account scope, native ref, token, or filesystem path.
"""

from dataclasses import dataclass
from functools import wraps
from contextlib import contextmanager, closing
import fcntl
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from tokmo.records.durability import fsync_directory
from tokmo.records.execution_dialogue_store import (
    DialogueStoreError,
    ExecutionDialogueStore,
)
from tokmo.records.playbook_identity import (
    instrument_document_path,
    read_instrument_name,
)
from tokmo.programs.trade_reason_seam import LedgerTradeV1, _render_new_document, reason_rejection
from tokmo.values.instrument_display import display_or_symbol
from tokmo.values.trade_event import (
    TradeEventEvaluationV1,
    TradeEventState,
    TradeEventTransitionError,
    dialogue_dedupe_key,
    dialogue_instance_dedupe_key,
    reason_append_id,
)

EVALUATION_POLICY_VERSION = "pulse-dialogue-eval-v1"
# The outbox dedupe key is per-event identity, not a message-format
# contract, so the confirmed-UX change does NOT bump it: bumping would only
# churn keys while leaving old in-flight bubbles bound to their own rows.
# The flow contract itself is versioned separately and independently.
PROMPT_VERSION = "v1"
DIALOGUE_UI_CONTRACT_VERSION = "trade-reason-receipt-v1"
# A save that keeps failing must not spin forever. After this many settled
# attempts the sweep stops retrying, tells the user the truth exactly once,
# and keeps the pending bytes intact — a fresh submission resumes.
PENDING_APPEND_MAX_ATTEMPTS = 8
_REASON_MARKER = "추가:".encode()
_DECLINE_TEXT = "추가하지 않음"
_CONFIRM_TEXTS = frozenset({"확인", "추가 확정"})
_KST = "Asia/Seoul"
_LAYER3_HEADING = "## 3층 — 매매이력"
_NO_RECOMMENDATION_TEXT = "관련 정보가 부족해 추천문구 없음"
_CURRENCY_SYMBOLS = {"USD": "$", "KRW": "₩"}

# Inline-keyboard grammar: callback_data is short, deterministic, and never
# carries raw user text, account identity, a path, or a secret. Exactly one
# button row per bubble (confirmed UI contract).
from tokmo.values.callback_registry import (CB_PREVIEW_CANCEL,CB_PREVIEW_EDIT,CB_PREVIEW_OK,
    CB_QUESTION_INPUT,CB_QUESTION_NONE,CB_QUESTION_YES,CB_RECEIPT_EDIT,NEWS_FEEDBACK_PREFIX)
QUESTION_KEYBOARD = {"inline_keyboard": [[
    {"text": "예", "callback_data": CB_QUESTION_YES},
    {"text": "입력안함", "callback_data": CB_QUESTION_NONE},
    {"text": "추가입력", "callback_data": CB_QUESTION_INPUT},
]]}
# Retained for old in-flight bubbles only; nothing enqueues a new preview.
PREVIEW_KEYBOARD = {"inline_keyboard": [[
    {"text": "확인", "callback_data": CB_PREVIEW_OK},
    {"text": "취소", "callback_data": CB_PREVIEW_CANCEL},
    {"text": "수정", "callback_data": CB_PREVIEW_EDIT},
]]}
# The receipt is an acknowledgement, not a question: one button, no 확인.
RECEIPT_KEYBOARD = {"inline_keyboard": [[
    {"text": "수정", "callback_data": CB_RECEIPT_EDIT},
]]}
_KEYBOARDS_BY_PROMPT_KIND = {
    "trade_reason_question": QUESTION_KEYBOARD,
    "conditional_fill_question": QUESTION_KEYBOARD,
    "trade_reason_preview": PREVIEW_KEYBOARD,
    "trade_reason_receipt": RECEIPT_KEYBOARD,
    "trade_reason_pending_receipt": RECEIPT_KEYBOARD,
    "trade_reason_input_prompt": {"force_reply": True, "input_field_placeholder": "매매 사유를 입력해 주세요"},
}
_RECEIPT_KINDS = ("trade_reason_receipt", "trade_reason_pending_receipt")
_QUESTION_KINDS = ("trade_reason_question", "conditional_fill_question")
_PREVIEW_QUESTION = "아래 원문을 플레이북 매매이력에 그대로 추가할까요?"
_RECEIPT_SAVED_LINE = "플레이북에 매매 사유를 저장했어요."
_RECEIPT_PENDING_LINE = (
    "수량/가격 원장 확정 대기 중입니다. 확정되면 아래 내용을 그대로 "
    "입력합니다.")
_SAVE_FAILED_NOTICE = (
    "지금은 저장하지 못했습니다. 입력하신 원문은 그대로 보관돼 있습니다. "
    "같은 내용을 다시 입력하지 않아도 됩니다.")
_INPUT_PROMPT_TEXT = (
    "이 메시지에 답장(reply)으로 남길 이유나 맥락을 자연어로 입력해 "
    "주세요. 입력한 원문은 그대로 저장되고, 저장 결과를 다시 알려 "
    "드립니다.")
_NO_RECOMMENDATION_GUIDE = (
    "추천문구가 없어 바로 추가할 원문이 없습니다. 질문의 '추가입력' "
    "버튼으로 직접 입력해 주세요.")


@dataclass(frozen=True)
class DialogueStageResult:
    status: str
    registered: int
    duplicates: int
    enqueued: int
    ai_calls: int
    send_calls: int


@dataclass(frozen=True)
class InboundStageResult:
    status: str
    polls: int
    updates_seen: int
    previews_enqueued: int
    reconfirms_enqueued: int
    appends: int
    append_failures: int
    send_calls: int
    input_prompts_enqueued: int = 0
    receipts_enqueued: int = 0
    failure_reasons: tuple[str, ...] = ()


@contextmanager
def dialogue_processing_lock(store):
    """Share only dialogue ownership with Pulse, never its collection lease."""
    path = store.path.with_suffix(".processing.lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _serialized_dialogue(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with dialogue_processing_lock(kwargs["dialogue_store"]):
            return function(*args, **kwargs)
    return wrapped


def _ledger_reference(store, event):
    """Resolve physical Ledger row read-only; no account/provider request."""
    path = store.path.with_name("trade_ledger.sqlite3").resolve()
    if not path.exists():
        # Offline callers may have only the dialogue store; label honestly.
        return "execution_dialogue.trade_events#" + event["trade_event_id"]
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            "SELECT event_id,instrument_raw,transaction_type_raw,quantity,"
            "unit_price,amount,currency_raw FROM trade_ledger_events "
            "WHERE source=? AND account_scope=? AND source_record_key=?",
            (event["source"], event["account_scope"], event["source_record_key"])
        ).fetchall()
    if len(rows) != 1:
        raise ValueError("ledger_match_missing")
    row = rows[0]
    if (row[1:3] != (event["symbol"], event["side"])
            or row[6] != event["currency"]
            or any(Decimal(row[i]) != Decimal(event[field]) for i, field in
                   ((3, "quantity"), (4, "unit_price"), (5, "amount")))):
        raise ValueError("ledger_match_changed")
    return "trade_ledger_events#" + row[0]


def extract_reason_payload(raw: bytes) -> "bytes | None":
    """Payload bytes after the explicit ``추가:`` marker; else None.

    The marker (plus one optional following ASCII space) is grammar; every
    byte after it is the user's own text and is preserved exactly.
    """
    if type(raw) is not bytes or not raw.startswith(_REASON_MARKER):
        return None
    payload = raw[len(_REASON_MARKER):]
    if payload.startswith(b" "):
        payload = payload[1:]
    return payload if payload else None


def _side_kr(side: str) -> str:
    return {"BUY": "매수", "SELL": "매도"}.get(side, side)


def _kst(value: datetime) -> str:
    return value.astimezone(ZoneInfo(_KST)).strftime("%Y-%m-%d %H:%M")


def _money(currency: str, value: str) -> str:
    """Two-decimal money, third decimal ROUND_HALF_UP (confirmed contract)."""
    quantized = Decimal(value).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)
    symbol = _CURRENCY_SYMBOLS.get(currency)
    return f"{symbol}{quantized}" if symbol else f"{currency} {quantized}"


def instrument_display(playbook_root, *, account_scope: str, market: str,
                       symbol: str) -> str:
    """The one user-facing instrument rendering (2026-08-21 contract).

    The name comes from the canonical playbook document — the very record a
    trade reason is appended to — so no renderer makes a provider call and
    no second identity source exists. An unavailable name fails soft to the
    canonical symbol; nothing is ever translated or guessed.
    """
    return display_or_symbol(
        market, symbol,
        read_instrument_name(playbook_root, account_scope=account_scope,
                             market=market, symbol=symbol))


def build_trade_question_message(event, *, evaluation, playbook_root,
                                 capture_path=None) -> str:
    """The user-confirmed question contract (2026-08-19).

    Name-first title, execution side, quantity(ledger-raw)/money(2dp HALF_UP),
    KST times without suffix, the 추천 문구 header, the recommendation
    bullet (or the fixed no-recommendation line), and the append question.
    The header carries the instrument display form; the root itself is only
    ever read, never rendered. The inline keyboard is attached at send
    time, never rendered as text.
    """
    times = event.times
    from tokmo.records.order_alert_identity import read_order_alert_name
    from tokmo.values.instrument_display import order_alert_title
    display = order_alert_title(
        event.classified_market, event.symbol,
        read_order_alert_name(playbook_root, account_scope=event.account_scope,
            market=event.classified_market, symbol=event.symbol,
            capture_path=capture_path))
    recommendation = None if evaluation is None else evaluation.prose
    return "\n".join([
        display,
        f"{_side_kr(event.side)} 체결 확인",
        (f"{event.filled_quantity}주  "
         f"{_money(event.currency, event.average_filled_price)} = "
         f"{_money(event.currency, event.filled_amount)}"),
        f"체결 {_kst(times.filled_at)} / 확인 {_kst(times.discovered_at)}",
        "추천 문구",
        (_NO_RECOMMENDATION_TEXT if recommendation is None
         else f"- {recommendation}"),
        "이 메시지에 바로 답장하면 매매 사유를 저장해요. (추가입력 버튼 없이 가능)",
    ])


def build_reason_receipt_message(*, when: datetime, display: str,
                                 payload: bytes, pending: bool = False) -> str:
    """The user-confirmed receipt contract (2026-08-21).

    Four parts and nothing else: the trade's KST instant with the instrument
    display, the fixed statement line, a ``---`` separator, and the user's
    own bytes verbatim. ``pending=True`` swaps only the statement line for
    the truthful pre-ledger wording — it never claims a save that has not
    happened.
    """
    statement = _RECEIPT_PENDING_LINE if pending else _RECEIPT_SAVED_LINE
    body = payload.decode("utf-8", errors="replace")
    return f"{_kst(when)} / {display}\n{statement}\n---\n{body}"


def _flush_outbox(store: ExecutionDialogueStore, sender, *,
                  activation_enabled: bool,
                  send_eligible_after: datetime, limit: int | None = None) -> int:
    """Send PENDING rows once each; UNKNOWN/FAILED rows are never retried."""
    if not activation_enabled or sender is None:
        return 0
    if (not isinstance(send_eligible_after, datetime)
            or send_eligible_after.tzinfo is None
            or send_eligible_after.utcoffset() is None):
        return 0
    try:
        cutoff = send_eligible_after.astimezone(timezone.utc)
        rows = store.list_outbox(
            status="PENDING", send_eligible_after=cutoff)
    except Exception:
        return 0
    send_calls = 0
    # Also exclude legacy queued submission notices, before applying the
    # batch limit so a retained row cannot starve actual executions.
    rows = [row for row in rows
            if row["prompt_kind"] != "conditional_fill_question"]
    for row in rows[:limit]:
        send_calls += 1
        markup = _KEYBOARDS_BY_PROMPT_KIND.get(row["prompt_kind"])
        try:
            if markup is None:
                message_id = sender(row["message_text"])
            else:
                message_id = sender(row["message_text"], reply_markup=markup)
        except TimeoutError:
            store.mark_outbox(row["dedupe_key"], "UNKNOWN")
            continue
        except Exception:
            store.mark_outbox(row["dedupe_key"], "FAILED")
            continue
        store.mark_outbox(row["dedupe_key"], "SENT",
                          telegram_message_id=message_id)
        if row["prompt_kind"] == "trade_reason_question":
            try:
                store.transition_event(
                    row["trade_event_id"], TradeEventState.SENT)
            except Exception:
                pass
    return send_calls


def _material_sufficient(event, quote, playbook_root) -> bool:
    if quote is None or playbook_root is None:
        return False
    symbol_path = instrument_document_path(
        playbook_root, account_scope=event.account_scope,
        market=event.classified_market, symbol=event.symbol)
    return symbol_path is not None and symbol_path.exists()


@_serialized_dialogue
def run_execution_dialogue(*, new_events, dialogue_store: ExecutionDialogueStore,
                           quote_reader: Callable, playbook_root,
                           ai_evaluator: "Callable | None",
                           sender: "Callable | None",
                           activation_enabled: bool,
                           send_eligible_after: datetime,
                           clock: Callable[[], datetime],
                           now: datetime) -> DialogueStageResult:
    del clock, now
    registered = 0
    duplicates = 0
    enqueued = 0
    ai_calls = 0
    for event in new_events:
        # Conditional merge lookup (conditional SDS §4.3) happens before the
        # regular-surface registration: primary key first (probe-confirmed
        # triggered_order_id == source_record_key), then the defensive
        # scope+symbol+±25h fallback with the 0/1/2+ dispositions.
        merge_alert = dialogue_store.find_alert_by_triggered_order(
            event.source_record_key)
        outcome = dialogue_store.register_event(
            event.trade_event_id,
            source="toss-orders-v1",
            account_scope=event.account_scope,
            source_record_key=event.source_record_key,
            symbol=event.symbol, side=event.side,
            quantity=event.filled_quantity,
            unit_price=event.average_filled_price,
            amount=event.filled_amount, currency=event.currency,
            classified_market=event.classified_market,
            times=event.times,
        )
        if outcome == "duplicate":
            duplicates += 1
            continue
        registered += 1
        if merge_alert is None:
            candidates = dialogue_store.find_unmigrated_alerts_in_window(
                account_scope=event.account_scope, symbol=event.symbol,
                center=event.times.filled_at)
            if len(candidates) == 1:
                merge_alert = candidates[0]
        if merge_alert is not None:
            # Submission was not a fill. Preserve pending reasons/reply links,
            # then enqueue this first evidence-backed execution exactly once.
            dialogue_store.backfill_migrated_event(
                merge_alert["conditional_alert_id"], event.trade_event_id)
        try:
            quote = quote_reader(event.symbol)
        except Exception:
            quote = None
        evaluation = None
        if (_material_sufficient(event, quote, playbook_root)
                and ai_evaluator is not None):
            payload = {
                "trade_event_id": event.trade_event_id,
                "symbol": event.symbol,
                "side": event.side,
                "quantity": event.filled_quantity,
                "unit_price": event.average_filled_price,
                "amount": event.filled_amount,
                "currency": event.currency,
                "classified_market": event.classified_market,
                "filled_at": event.times.filled_at.isoformat(),
                "discovered_at": event.times.discovered_at.isoformat(),
                "quote": quote,
            }
            ai_calls += 1
            try:
                candidate = ai_evaluator(payload)
            except Exception:
                candidate = None
            if isinstance(candidate, TradeEventEvaluationV1):
                evaluation = candidate
        if evaluation is not None:
            # The 예 button later adopts these exact bytes into the preview.
            dialogue_store.record_question_recommendation(
                event.trade_event_id, evaluation.prose.encode())
        message = build_trade_question_message(
            event, evaluation=evaluation, playbook_root=playbook_root,
            capture_path=dialogue_store.path.parent.parent
            / "ops/quant-inputs/latest.json")
        dialogue_store.transition_event(
            event.trade_event_id, TradeEventState.EVALUATED,
            evaluation_policy_version=EVALUATION_POLICY_VERSION)
        dialogue_store.transition_event(
            event.trade_event_id, TradeEventState.ENQUEUED)
        key = dialogue_dedupe_key(
            event.trade_event_id, "trade_reason_question", PROMPT_VERSION)
        if dialogue_store.enqueue_outbox(
                key, trade_event_id=event.trade_event_id,
                prompt_kind="trade_reason_question",
                prompt_version=PROMPT_VERSION,
                message_text=message) == "created":
            enqueued += 1
    send_calls = _flush_outbox(dialogue_store, sender,
                               activation_enabled=activation_enabled,
                               send_eligible_after=send_eligible_after)
    # Activation on with no sender composed (env absent): only the send
    # step is unavailable. Registration and enqueue above already ran, so
    # the once-only ``new_events`` are durably preserved as PENDING rows.
    status = ("send_unavailable"
              if activation_enabled and sender is None else "saved")
    return DialogueStageResult(
        status, registered, duplicates, enqueued, ai_calls, send_calls)


def _unbound_key(message_id: int) -> str:
    pseudo = hashlib.sha256(
        f"unbound-reconfirm-{message_id}".encode()).hexdigest()
    return dialogue_dedupe_key(pseudo, "trade_reason_reconfirm",
                               PROMPT_VERSION)


def _entry_id(event: dict, inbound_message_id: int) -> str:
    return (f"trade-{event['trade_event_id'][:8]}-{event['symbol'].lower()}"
            f"-{event['side'].lower()}-{inbound_message_id}")


def _render_entry(event: dict, payload: bytes, *, entry_id: str,
                  now: datetime, supersedes: "str | None" = None) -> str:
    recorded = now.astimezone(ZoneInfo(_KST))
    event_date = event["filled_at"].astimezone(ZoneInfo(_KST)).date()
    summary = (f"{event['side']} {event['quantity']} {event['symbol']} @ "
               f"{event['currency']} {event['unit_price']} = "
               f"{event['amount']}, filled "
               f"{event['filled_at'].isoformat()}")
    heading = (f"### {recorded.date().isoformat()} — "
               f"{_side_kr(event['side'])} ({event['quantity']}주 @ "
               f"{event['currency']} {event['unit_price']})")
    body = payload.decode("utf-8", errors="strict")
    # A correction is an appended, linked record — never a rewrite. The
    # superseded entry keeps its own USER_RAW block untouched; an entry is
    # superseded exactly when a later entry names it here, and the newest
    # entry (top of layer 3) is the current interpretation.
    correction = ("" if supersedes is None
                  else f"- `supersedes_entry_id`: `{supersedes}`\n")
    return (
        f'<a id="entry-{entry_id}"></a>\n'
        f"{heading}\n\n"
        f"- `entry_id`: `{entry_id}`\n"
        f"- `source_kind`: `user_trade_reason`\n"
        f"{correction}"
        f"- `source_ref`: `{event.get('ledger_reference', 'execution_dialogue.trade_events#' + event['trade_event_id'])}`\n"
        f"- `event_date`: `{event_date.isoformat()}`\n"
        f"- `recorded_date`: `{recorded.date().isoformat()}`\n"
        f"- `source_snapshot_date`: `{recorded.date().isoformat()}`\n"
        f"- `source_snapshot_basis`: `v3_trade_ledger_event`\n"
        f"- `v3_trade_event_id`: `{event.get('ledger_reference', '#' + event['trade_event_id']).split('#', 1)[1]}`\n"
        f"- `dialogue_trade_event_id`: `{event['trade_event_id']}`\n"
        f"- `trade_summary`: `{summary}`\n"
        f"- `imported_at`: `{recorded.isoformat()}`\n\n"
        f'<!-- USER_RAW_START entry_id="{entry_id}" -->\n'
        f"{body}\n"
        f'<!-- USER_RAW_END entry_id="{entry_id}" -->\n'
    )


def _write_playbook_bytes_durable(path: Path, data: bytes) -> None:
    """Same-directory private temp, fsync, atomic replace, parent fsync.

    The canonical playbook file carries the recovery anchor, so the bytes
    must be on disk before the rename is published — a crash leaves either
    the complete old file or the complete new one, never a torn write.
    """
    mode = stat.S_IMODE(os.stat(path).st_mode) if path.exists() else 0o644
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    fsync_directory(path.parent)


def _perform_append(*, dialogue_store: ExecutionDialogueStore, event: dict,
                    payload: bytes, inbound_message_id: int,
                    playbook_root, playbook_validator: Callable,
                    now: datetime) -> str:
    """Validator-guarded, byte-for-byte, top-of-layer-3 append (§22.2).

    Durable exactly-once: the deterministic entry anchor inside the
    canonical file is the recovery truth. The reason_appends claim is
    recorded only AFTER the updated bytes are atomically replaced, read
    back, and validator-approved; a file already carrying the anchor
    settles the claim idempotently without a second write. Every failure
    returns a closed token (no path, no payload) and leaves the pending
    row to the caller, so a retry converges on exactly one entry.
    """
    if playbook_root is None:
        return "playbook_missing"
    root = Path(playbook_root)
    path = instrument_document_path(
        root, account_scope=event["account_scope"],
        market=event["classified_market"], symbol=event["symbol"])
    if path is None or not root.is_dir():
        return "playbook_missing"
    rejection = reason_rejection(payload)
    if rejection:
        return "invalid_reason"
    try:
        event = {**event, "ledger_reference": _ledger_reference(dialogue_store, event)}
    except (ValueError, sqlite3.Error, OSError):
        return "ledger_match_failed"
    creating = not path.exists()
    try:
        original_bytes = None if creating else path.read_bytes()
        original = "" if creating else original_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return "read_failed"
    entry_id = _entry_id(event, inbound_message_id)
    anchor = f'<a id="entry-{entry_id}"></a>'
    append_id = reason_append_id(
        event["trade_event_id"], inbound_message_id)
    if anchor in original:
        # A prior attempt already landed these exact bytes durably (crash
        # between replace and claim): settle the claim and never rewrite —
        # but only once the whole canonical document is validator-approved.
        # A leftover anchor from a rejected write whose restore failed is
        # not proof of success; settling it would answer the event with a
        # document the validator still rejects.
        try:
            problems = playbook_validator(root)
        except Exception:
            return "recovery_validator_failed"
        if problems:
            return "recovery_validator_failed"
        try:
            dialogue_store.record_reason_append(
                append_id, trade_event_id=event["trade_event_id"],
                inbound_message_id=inbound_message_id)
        except Exception:
            return "claim_failed"
        return "appended"
    if playbook_validator(root):
        return "validator_failed_before"
    if not creating and _LAYER3_HEADING not in original:
        return "layer3_heading_missing"
    # A later submission for the same event corrects the earlier one. The
    # link is derived from the durable claim, so a retry of the same append
    # renders the identical bytes and converges on one entry.
    supersedes = None
    try:
        prior = dialogue_store.read_latest_reason_append(
            event["trade_event_id"])
    except Exception:
        prior = None
    if prior is not None \
            and prior["inbound_message_id"] != inbound_message_id:
        supersedes = _entry_id(event, prior["inbound_message_id"])
    entry = _render_entry(event, payload, entry_id=entry_id, now=now,
                          supersedes=supersedes)
    if creating:
        trade = LedgerTradeV1(
            event["trade_event_id"], event["account_scope"],
            event["classified_market"], event["symbol"], event["side"],
            event["quantity"], event["unit_price"], event["amount"],
            event["currency"], event["filled_at"].date(), event["filled_at"])
        updated = _render_new_document(
            trade, entry=entry, entry_id=entry_id, instrument_name=None,
            hermes_interpretation=None, user_thesis=None,
            day=now.astimezone(ZoneInfo(_KST)).date().isoformat())
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        head, _, tail = original.partition(_LAYER3_HEADING)
        tail_lines = tail.lstrip("\n")
        updated = f"{head}{_LAYER3_HEADING}\n\n{entry}"
        if tail_lines:
            updated += f"\n{tail_lines}"
    updated_bytes = updated.encode("utf-8")
    try:
        _write_playbook_bytes_durable(path, updated_bytes)
    except OSError:
        return "write_failed"
    try:
        written = path.read_bytes()
    except OSError:
        return "write_unverified"
    if written != updated_bytes or anchor.encode() not in written:
        return "write_unverified"
    if playbook_validator(root):
        try:
            if creating:
                path.unlink()
                fsync_directory(path.parent)
                restored = None
            else:
                _write_playbook_bytes_durable(path, original_bytes)
                restored = path.read_bytes()
        except OSError:
            return "restore_uncertain"
        if restored != original_bytes or playbook_validator(root):
            return "restore_uncertain"
        return "validator_failed_after"
    try:
        claim = dialogue_store.record_reason_append(
            append_id, trade_event_id=event["trade_event_id"],
            inbound_message_id=inbound_message_id)
    except Exception:
        return "claim_failed"
    # "duplicate" here repairs claim-first residue: the bytes now exist.
    del claim
    return "appended"


@_serialized_dialogue
def consume_inbound(*, dialogue_store: ExecutionDialogueStore,
                    updates_provider: Callable, playbook_root,
                    playbook_validator: Callable,
                    sender: "Callable | None" = None,
                    callback_ack: "Callable | None" = None,
                    news_callback_handler: "Callable | None" = None,
                    activation_enabled: bool,
                    send_eligible_after: datetime,
                    clock: Callable[[], datetime],
                    now: datetime, retry_pending: bool = True,
                    outbox_limit: int | None = None) -> InboundStageResult:
    """One bounded getUpdates consumption pass (§22.3): ≤1 poll per tick.

    Two phases: the bounded sweep settles bytes the user already submitted
    (on minute sweeps: a failed save from an earlier pass, or a conditional-fill reason whose
    ledger event has since migrated), then the updates are dispatched. Both
    phases share one settle path, so every route through the UI converges
    on the same exactly-once append.
    """
    del clock
    if not activation_enabled:
        return InboundStageResult("skipped", 0, 0, 0, 0, 0, 0, 0, 0, 0)
    previews = 0
    reconfirms = 0
    appends = 0
    append_failures = 0
    input_prompts = 0
    receipts = 0
    failure_reasons = []

    def _reconfirm(message_id, event_id=None, note=""):
        nonlocal reconfirms
        if event_id is None:
            key = _unbound_key(message_id)
            bound = "0" * 64
        else:
            key = dialogue_instance_dedupe_key(
                event_id, "trade_reason_reconfirm", PROMPT_VERSION, str(message_id))
            bound = event_id
        text = (note or "어느 체결에 대한 답인지 확인이 필요합니다. 봇 질문 "
                "메시지에 답장(reply)으로 다시 보내 주세요.")
        if dialogue_store.enqueue_outbox(
                key, trade_event_id=bound,
                prompt_kind="trade_reason_reconfirm",
                prompt_version=PROMPT_VERSION,
                message_text=text) == "created":
            reconfirms += 1

    def _receipt_key(event_id, provider_update_id, *, pending):
        kind = ("trade_reason_pending_receipt" if pending
                else "trade_reason_receipt")
        return dialogue_instance_dedupe_key(
            event_id, kind, PROMPT_VERSION, str(provider_update_id))

    def _enqueue_receipt(*, event_id, provider_update_id, when, display,
                         payload, pending):
        nonlocal receipts
        kind = ("trade_reason_pending_receipt" if pending
                else "trade_reason_receipt")
        text = build_reason_receipt_message(
            when=when, display=display, payload=payload, pending=pending)
        if dialogue_store.enqueue_outbox(
                _receipt_key(event_id, provider_update_id, pending=pending),
                trade_event_id=event_id, prompt_kind=kind,
                prompt_version=PROMPT_VERSION,
                message_text=text) == "created":
            receipts += 1

    def _open_input_prompt(event_id, update_id):
        nonlocal input_prompts
        key = dialogue_instance_dedupe_key(
            event_id, "trade_reason_input_prompt", PROMPT_VERSION,
            str(update_id))
        if dialogue_store.enqueue_outbox(
                key, trade_event_id=event_id,
                prompt_kind="trade_reason_input_prompt",
                prompt_version=PROMPT_VERSION,
                message_text=_INPUT_PROMPT_TEXT) == "created":
            input_prompts += 1

    def _close_no_append(event_id):
        try:
            dialogue_store.transition_event(
                event_id, TradeEventState.ANSWERED)
        except (TradeEventTransitionError, DialogueStoreError):
            # A conditional-fill question uses its alert id until the regular
            # order migrates. Declining it is still a valid terminal UI action
            # even though no canonical trade event exists yet. A correction of
            # an already-ANSWERED event lands here too: the terminal state is
            # unchanged and the appended entry is the whole record.
            pass

    def _resolve_payload(pending):
        payload = pending["payload"]
        if payload is not None:
            return payload
        # Pre-button pending rows carry no payload: re-read the bytes from
        # the recorded inbound '추가:' message.
        original = dialogue_store.read_inbound(pending["provider_update_id"])
        return None if original is None else extract_reason_payload(
            original["raw_text"])

    def _settle_pending(pending, *, notify_ledger_wait):
        """The single exactly-once settle path for submitted bytes."""
        nonlocal appends, append_failures
        event_id = pending["trade_event_id"]
        payload = _resolve_payload(pending)
        if payload is None:
            append_failures += 1
            dialogue_store.delete_pending_append(event_id)
            return
        event = dialogue_store.read_event(event_id)
        if event is None:
            # Conditional fills intentionally precede the canonical regular
            # order. Keep the user's bytes durable; migration atomically
            # rebinds this pending row to the real event, after which the
            # sweep settles it without any further user action.
            if dialogue_store.read_conditional_alert(event_id) is not None:
                if notify_ledger_wait:
                    _reconfirm(
                        pending["inbound_message_id"], event_id,
                        note=("수량/가격 원장 확정 대기 중입니다. 원장 반영 "
                              "후 입력하신 원문이 자동으로 저장됩니다."))
                return
            append_failures += 1
            dialogue_store.delete_pending_append(event_id)
            return
        result = _perform_append(
            dialogue_store=dialogue_store, event=event, payload=payload,
            inbound_message_id=pending["inbound_message_id"],
            playbook_root=playbook_root,
            playbook_validator=playbook_validator, now=now)
        if result != "appended":
            # Closed failure token: the pending row survives so a later retry
            # converges through the deterministic anchor. No receipt is ever
            # sent for a reason that was not saved.
            append_failures += 1
            attempts = dialogue_store.bump_pending_append_attempt(event_id)
            failure_reasons.append(result)
            _reconfirm(pending["inbound_message_id"], event_id,
                       note=_SAVE_FAILED_NOTICE)
            return
        appends += 1
        # Receipt first, pending row second: if the enqueue fails the row
        # survives and the next sweep converges through the anchor onto the
        # same receipt key (a duplicate no-op). The reverse order could drop
        # the acknowledgement of an append that did happen.
        _enqueue_receipt(
            event_id=event_id,
            provider_update_id=pending["provider_update_id"],
            when=event["filled_at"],
            display=instrument_display(
                playbook_root, account_scope=event["account_scope"],
                market=event["classified_market"], symbol=event["symbol"]),
            payload=payload, pending=False)
        dialogue_store.delete_pending_append(event_id)
        _close_no_append(event_id)

    def _save_reason(event_id, payload, update_id, inbound_message_id):
        """Immediate save (2026-08-21): no second confirmation exists.

        The bytes become durable BEFORE the append is attempted, so a crash
        at any point leaves them recoverable and the bounded sweep converges
        on exactly one entry.
        """
        alert = (None if dialogue_store.read_event(event_id) is not None
                 else dialogue_store.read_conditional_alert(event_id))
        dialogue_store.create_pending_append(
            trade_event_id=event_id, provider_update_id=update_id,
            inbound_message_id=inbound_message_id,
            preview_dedupe_key=_receipt_key(
                event_id, update_id, pending=alert is not None),
            payload=payload, save_intent=True)
        if alert is not None:
            # Pre-ledger: no trade exists to save against, and none is
            # fabricated. The receipt says exactly that and still offers 수정.
            _enqueue_receipt(
                event_id=event_id, provider_update_id=update_id,
                when=alert["detected_at"],
                display=instrument_display(
                    playbook_root, account_scope=alert["account_scope"],
                    market=alert["market_class"], symbol=alert["symbol"]),
                payload=payload, pending=True)
            return
        _settle_pending(dialogue_store.read_pending_append(event_id),
                        notify_ledger_wait=False)

    # Phase 1: bounded sweep of already-submitted bytes.
    for pending in (dialogue_store.list_pending_appends() if retry_pending else ()):
        if not pending["save_intent"]:
            continue  # legacy preview handshake: waits for its own 확인
        if pending["attempts"] >= PENDING_APPEND_MAX_ATTEMPTS:
            continue  # bounded: the bytes stay, the retrying stops
        if dialogue_store.read_event(pending["trade_event_id"]) is None:
            continue  # pre-ledger conditional fill: nothing to append to yet
        _settle_pending(pending, notify_ledger_wait=False)

    # Phase 2: one bounded poll.
    offset = dialogue_store.read_inbound_cursor()
    updates = tuple(updates_provider(offset))
    for update in updates:
        update_id = update["update_id"]
        if update.get("ignored") is True:
            dialogue_store.advance_inbound_cursor(update_id + 1)
            continue
        if update.get("callback") is True:
            # Bounded spinner ack first; its failure never aborts the
            # dispatch and never becomes a canonical write.
            if callback_ack is not None:
                try:
                    callback_ack(update["callback_query_id"])
                except Exception:
                    pass
            message_id = update["message_id"]
            data = update["data"]
            outcome = dialogue_store.record_inbound(
                provider_update_id=update_id, message_id=message_id,
                reply_to_message_id=message_id, raw_text=data.encode(),
                next_offset=update_id + 1)
            if outcome == "duplicate":
                continue
            if data.startswith(NEWS_FEEDBACK_PREFIX):
                if news_callback_handler is not None:
                    news_callback_handler(update)
                continue
            # Exact source-message binding: buttons act only on the very
            # outbox bubble they were attached to.
            bound = dialogue_store.find_outbox_by_telegram_message(message_id)
            if bound is None:
                continue
            event_id = bound["trade_event_id"]
            if bound["prompt_kind"] in _QUESTION_KINDS:
                if data == CB_QUESTION_NONE:
                    _close_no_append(event_id)
                elif data == CB_QUESTION_INPUT:
                    _open_input_prompt(event_id, update_id)
                elif data == CB_QUESTION_YES:
                    recommendation = \
                        dialogue_store.read_question_recommendation(event_id)
                    if recommendation is None:
                        _reconfirm(message_id, event_id,
                                   note=_NO_RECOMMENDATION_GUIDE)
                    else:
                        # Adoption is a submission: it saves immediately.
                        _save_reason(event_id, recommendation, update_id,
                                     message_id)
                # Any other q1: token (an older grammar) is safely ignored.
            elif bound["prompt_kind"] in _RECEIPT_KINDS:
                if data == CB_RECEIPT_EDIT:
                    # 수정 never touches what is already recorded: it only
                    # opens a fresh input whose bytes append as a correction.
                    _open_input_prompt(event_id, update_id)
            elif bound["prompt_kind"] == "trade_reason_preview":
                # Abolished flow; still honoured for bubbles already sent.
                pending = dialogue_store.find_pending_append_by_preview(
                    bound["dedupe_key"])
                if data == CB_PREVIEW_OK:
                    if pending is not None:
                        _settle_pending(pending, notify_ledger_wait=True)
                elif data == CB_PREVIEW_CANCEL:
                    if pending is not None:
                        dialogue_store.delete_pending_append(
                            pending["trade_event_id"])
                elif data == CB_PREVIEW_EDIT:
                    if pending is not None:
                        dialogue_store.delete_pending_append(
                            pending["trade_event_id"])
                    _open_input_prompt(event_id, update_id)
            continue
        message_id = update["message_id"]
        reply_to = update.get("reply_to_message_id")
        raw = update["text"].encode() if isinstance(update["text"], str) \
            else update["text"]
        outcome = dialogue_store.record_inbound(
            provider_update_id=update_id, message_id=message_id,
            reply_to_message_id=reply_to, raw_text=raw,
            next_offset=update_id + 1)
        if outcome == "duplicate":
            continue
        if reply_to is None:
            _reconfirm(message_id)
            continue
        bound = dialogue_store.find_outbox_by_telegram_message(reply_to)
        if bound is None:
            _reconfirm(message_id)
            continue
        event_id = bound["trade_event_id"]
        text = raw.decode("utf-8", errors="replace")
        if (bound["prompt_kind"] == "trade_reason_input_prompt"
                or bound["prompt_kind"] in _QUESTION_KINDS):
            if text == _DECLINE_TEXT:
                _close_no_append(event_id)
                continue
            if reason_rejection(raw):
                _reconfirm(message_id, event_id, note="저장할 매매 사유를 글로 입력해 주세요.")
                continue
            # The exact reply to the input prompt IS the raw payload: the
            # whole message, byte for byte, no marker grammar required.
            payload = (extract_reason_payload(raw) or raw) if bound["prompt_kind"] in _QUESTION_KINDS else raw
            _save_reason(event_id, payload, update_id, message_id)
            continue
        if bound["prompt_kind"] == "trade_reason_preview":
            pending = dialogue_store.find_pending_append_by_preview(
                bound["dedupe_key"])
            if pending is None or text not in _CONFIRM_TEXTS:
                _reconfirm(message_id, event_id)
                continue
            _settle_pending(pending, notify_ledger_wait=True)
            continue
        # Question (or reconfirm/receipt) binding.
        if text == _DECLINE_TEXT:
            _close_no_append(event_id)
            continue
        payload = extract_reason_payload(raw)
        if payload is None:
            _reconfirm(
                message_id, event_id,
                note=("플레이북에 남기려면 '추가: …' 형식으로, 남기지 "
                      "않으려면 '추가하지 않음'으로 답해 주세요."))
            continue
        _save_reason(event_id, payload, update_id, message_id)

    send_calls = _flush_outbox(dialogue_store, sender,
                               activation_enabled=activation_enabled,
                               send_eligible_after=send_eligible_after, limit=outbox_limit)
    return InboundStageResult(
        "saved" if not append_failures else "degraded", 1, len(updates), previews, reconfirms, appends,
        append_failures, send_calls, input_prompts, receipts, tuple(failure_reasons))
