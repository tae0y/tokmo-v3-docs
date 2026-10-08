"""Transport-neutral trade-reason seam for the tokmo-group flow (Slice 0).

Product contract 2026-09-03 §8: forwarded/copied Stock-bot alert text is a
matching *hint* only; trade truth is the read-only canonical Ledger. The
seam binds exactly one ``v3_trade_event_id`` (zero or many candidates fail
closed with write 0), renders a truthful preview, and only an explicit
approval call — bound by token to the preview digest, the current file
digest, the trade id and the reason digest — writes either one Layer-3
append to an existing instrument playbook or one newly created canonical
instrument playbook carrying the first Layer-3 entry.

Nothing here polls, sends, schedules, calls an AI, or touches an order,
account, provider or Ledger write path: the module imports no transport
and its only write is the playbook file, guarded by the injected tree
validator and an exact byte read-back with rollback.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from tokmo.records.durability import fsync_directory
from tokmo.records.playbook_identity import (
    instrument_document_path,
    playbook_scope_dir,
)
from tokmo.values.material_pack import canonical_encode
from tokmo.values.session_gate import MARKET_TIMEZONE
from tokmo.values.trade_event import trade_event_id

APPROVAL_VERSION = "tokmo-reason-approval-v1"
LEDGER_SOURCE = "toss-orders-v1"
KST = ZoneInfo("Asia/Seoul")
_MARKET_TZ = {market.value: zone for market, zone in MARKET_TIMEZONE.items()}
_SIDES = ("BUY", "SELL")
_SIDE_KR = {"BUY": "매수", "SELL": "매도", "매수": "BUY", "매도": "SELL"}
_CURRENCY_SYMBOLS = {"$": "USD", "₩": "KRW"}
_MARKET_BY_CURRENCY = {"KRW": "KR", "USD": "US"}
LAYER3_HEADING = "## 3층 — 매매이력"
HERMES_START, HERMES_END = "<!-- HERMES_CURRENT_START -->", "<!-- HERMES_CURRENT_END -->"
THESIS_START, THESIS_END = "<!-- USER_THESIS_START -->", "<!-- USER_THESIS_END -->"
ANCHORS_START, ANCHORS_END = "<!-- HERMES_ANCHORS_START -->", "<!-- HERMES_ANCHORS_END -->"
RESERVED_MARKERS = ("<!-- USER_RAW_", "<!-- HERMES_CURRENT_", "<!-- USER_THESIS_",
                    "<!-- HERMES_ANCHORS_")
REASON_MAX_BYTES = 16384
HINT_MAX_CHARS = 8192
NO_INTERPRETATION = "헤르 해석 없음 — 이 시점에 해석이 제공되지 않았다."
NO_THESIS = ("2층(사용자의 원문) 없음 — 종목 투자 계획 원문이 제공되지 않았고, "
             "매매 사유만으로 원문을 만들지 않는다.")
NO_NAME = "종목명 canonical 미확인 — front matter에 instrument_name을 기록하지 않았다."
LAYER3_PREAMBLE = ("> 체결된 거래에 대한 생각을 남긴다. **append-only** — 새 항목은 이 제목 바로 아래\n"
                   "> 최상단에 추가하고, 커밋 후 수정·삭제하지 않는다. 원문 그대로 저장한다.")
HERMES_PREAMBLE = ("> 2층 원문과 종합적인 상황을 보고 헤르가 AI로서 내린 판단이다. 사용자의 생각과 다르면\n"
                   "> 수정을 요청할 수 있지만, 여기 저장되는 것은 전적으로 헤르의 판단이다.\n"
                   "> **최종본만 유지한다.** 이전 판은 Git 이력에서 본다.")
THESIS_PREAMBLE = ("> 입력·교정 규칙은 [플레이북 정본](../../README.md)을 따른다.\n"
                   "> **최종본만 유지한다.** 이전 판은 Git 이력에서 본다.")
NO_WRITES = MappingProxyType({"playbook_files": 0, "ledger": 0, "order": 0,
                              "account": 0, "provider": 0})
_HINT_DISCRIMINATORS = ("symbol", "side", "quantity", "unit_price", "filled_kst")
_HEADER_RE = re.compile(r"^(?P<display>.+?)\s+(?P<side>매수|매도)\s+확인$")
_DISPLAY_RE = re.compile(r"^(?P<left>[^()]+)\((?P<right>[^()]+)\)$")
_KR_CODE_RE = re.compile(r"[0-9][0-9A-Z]{5}")
_US_TICKER_RE = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")
_MONEY_RE = re.compile(r"^(?P<qty>[0-9][0-9.,]*)주\s+(?P<cur>[$₩]|[A-Z]{3} )?"
                       r"(?P<price>[0-9][0-9.,]*)\s*=")
_FILL_RE = re.compile(r"^체결\s+(?P<kst>\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
_SOURCE_RE = re.compile(r"^(?:Forwarded from|전달된 메시지|From:)\s*(?P<label>.+)$")
_ANCHOR_RE = re.compile(r'<a id="entry-([^"]+)"></a>')
_SUMMARY_RE = re.compile(r"- `trade_summary`: `(BUY|SELL) ")


# --- values -----------------------------------------------------------------

@dataclass(frozen=True)
class LedgerTradeV1:
    """Read-only projection of one canonical Ledger execution."""

    trade_event_id: str
    account_scope: str
    market: str
    symbol: str
    side: str
    quantity: str
    unit_price: str
    amount: str
    currency: str
    trade_date: date
    filled_at: datetime | None = None

    def __post_init__(self):
        if type(self.trade_event_id) is not str \
                or re.fullmatch(r"[0-9a-f]{64}", self.trade_event_id) is None:
            raise ValueError("invalid trade event id")
        if self.market not in _MARKET_TZ or self.side not in _SIDES:
            raise ValueError("invalid ledger trade market or side")
        for value in (self.account_scope, self.symbol, self.currency):
            if type(value) is not str or not value:
                raise ValueError("invalid ledger trade identity")
        for value in (self.quantity, self.unit_price, self.amount):
            if _decimal(value) is None:
                raise ValueError("invalid ledger trade decimal")
        if type(self.trade_date) is not date:
            raise ValueError("invalid ledger trade date")
        if self.filled_at is not None and (
                not isinstance(self.filled_at, datetime)
                or self.filled_at.utcoffset() is None):
            raise ValueError("filled_at must be aware or None")

    def __repr__(self) -> str:
        return "<LedgerTradeV1 REDACTED>"


@dataclass(frozen=True)
class AlertHintV1:
    """Non-authoritative candidate fields parsed from forwarded text."""

    symbol: str | None = None
    side: str | None = None
    quantity: str | None = None
    unit_price: str | None = None
    currency: str | None = None
    filled_kst: str | None = None
    source_label: str | None = None


@dataclass(frozen=True)
class MatchResultV1:
    status: str
    trade: LedgerTradeV1 | None
    candidates: tuple
    missing_discriminators: tuple
    writes: MappingProxyType = field(default_factory=lambda: NO_WRITES)


@dataclass(frozen=True)
class ReasonPreviewV1:
    status: str
    text: str
    action: str | None = None
    trade: LedgerTradeV1 | None = None
    relative_path: str | None = None
    file_digest: str | None = None
    entry_id: str | None = None
    reason_digest: str | None = None
    episode_proposal: str | None = None
    episode_decision: str = "new"
    document: bytes | None = None
    approval_token: str | None = None
    writes: MappingProxyType = field(default_factory=lambda: NO_WRITES)


@dataclass(frozen=True)
class ApplyResultV1:
    status: str
    relative_path: str | None
    entry_id: str | None
    bytes_written: int
    writes: MappingProxyType = field(default_factory=lambda: NO_WRITES)


# --- ledger read (adapter over the canonical store, read-only) --------------

def ledger_trades_from_store(store, *, account_scope: str) -> tuple:
    """Project the canonical Toss-orders Ledger scope into LedgerTradeV1."""
    events = store.read_scope(source=LEDGER_SOURCE, account_scope=account_scope)
    superseded = {event.supersedes_source_record_key for event in events
                  if event.supersedes_source_record_key is not None}
    trades = []
    for event in events:
        market = _MARKET_BY_CURRENCY.get(event.currency_raw)
        if event.event_kind != "EXECUTION" or market is None \
                or event.transaction_type_raw not in _SIDES \
                or event.source_record_key in superseded:
            continue
        trades.append(LedgerTradeV1(
            trade_event_id(LEDGER_SOURCE, account_scope, event.source_record_key),
            account_scope, market, event.instrument_raw,
            event.transaction_type_raw, event.quantity, event.unit_price,
            event.amount, event.currency_raw, event.trade_date, None))
    return tuple(trades)


# --- hint parsing and matching ----------------------------------------------

def _decimal(value) -> Decimal | None:
    if type(value) is not str:
        return None
    try:
        parsed = Decimal(value.replace(",", ""))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _symbol_from_display(display: str) -> str | None:
    display = display.strip()
    pair = _DISPLAY_RE.fullmatch(display)
    if pair is None:
        return display if (_KR_CODE_RE.fullmatch(display)
                           or _US_TICKER_RE.fullmatch(display)) else None
    left, right = pair.group("left").strip(), pair.group("right").strip()
    if _KR_CODE_RE.fullmatch(right):
        return right
    if _US_TICKER_RE.fullmatch(left):
        return left
    return None


def parse_alert_hint(text) -> AlertHintV1:
    """Parse the Stock-bot alert shape; every field is a hint, never truth."""
    if type(text) is not str:
        return AlertHintV1()
    fields = {}
    for line in text[:HINT_MAX_CHARS].splitlines():
        line = line.strip()
        header = _HEADER_RE.match(line)
        if header and "symbol" not in fields:
            fields["symbol"] = _symbol_from_display(header.group("display"))
            fields["side"] = _SIDE_KR[header.group("side")]
            continue
        money = _MONEY_RE.match(line)
        if money and "quantity" not in fields:
            fields["quantity"] = money.group("qty").replace(",", "")
            fields["unit_price"] = money.group("price").replace(",", "")
            currency = (money.group("cur") or "").strip()
            fields["currency"] = _CURRENCY_SYMBOLS.get(currency, currency or None)
            continue
        fill = _FILL_RE.match(line)
        if fill and "filled_kst" not in fields:
            fields["filled_kst"] = fill.group("kst")
            continue
        source = _SOURCE_RE.match(line)
        if source and "source_label" not in fields:
            fields["source_label"] = source.group("label").strip()[:64]
    return AlertHintV1(**fields)


def _money_equal(hint: str, ledger: str) -> bool:
    wanted, actual = _decimal(hint), _decimal(ledger)
    if wanted is None or actual is None:
        return False
    return wanted == actual or wanted == actual.quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)


def _fill_equal(hint_kst: str, trade: LedgerTradeV1) -> bool:
    try:
        instant = datetime.strptime(hint_kst, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    except ValueError:
        return False
    if trade.filled_at is not None:
        return trade.filled_at.astimezone(KST).strftime("%Y-%m-%d %H:%M") == hint_kst
    return instant.astimezone(ZoneInfo(_MARKET_TZ[trade.market])).date() == trade.trade_date


def sanitize_candidate(trade: LedgerTradeV1) -> dict:
    """User-showable candidate: no account scope, no full identity hash."""
    return {
        "trade_event_id_prefix": trade.trade_event_id[:8],
        "scope": playbook_scope_dir(trade.account_scope) or "unmapped",
        "market": trade.market, "symbol": trade.symbol, "side": trade.side,
        "quantity": trade.quantity, "unit_price": trade.unit_price,
        "currency": trade.currency, "trade_date": trade.trade_date.isoformat(),
    }


def match_ledger_trade(hint: AlertHintV1, trades: Iterable[LedgerTradeV1], *,
                       account_scope: str | None = None,
                       market: str | None = None) -> MatchResultV1:
    """Exactly one Ledger trade or fail closed (write 0, sanitized candidates).

    Trusted arguments (account scope, market) narrow first; hint fields
    narrow only when present. Missing discriminators name the hint fields
    that would separate the remaining candidates.
    """
    candidates = [trade for trade in trades
                  if (account_scope is None or trade.account_scope == account_scope)
                  and (market is None or trade.market == market)]
    tests = (
        ("symbol", lambda t: t.symbol.upper() == hint.symbol.upper()),
        ("side", lambda t: t.side == hint.side),
        ("quantity", lambda t: _money_equal(hint.quantity, t.quantity)),
        ("unit_price", lambda t: _money_equal(hint.unit_price, t.unit_price)),
        ("filled_kst", lambda t: _fill_equal(hint.filled_kst, t)),
    )
    for name, test in tests:
        if getattr(hint, name) is not None:
            candidates = [trade for trade in candidates if test(trade)]
    missing = [name for name in _HINT_DISCRIMINATORS if getattr(hint, name) is None]
    if len({trade.account_scope for trade in candidates}) > 1:
        missing.insert(0, "account_scope")
    if len({trade.market for trade in candidates}) > 1:
        missing.insert(0, "market")
    if len(candidates) == 1:
        return MatchResultV1("matched", candidates[0], (), ())
    status = "no_match" if not candidates else "ambiguous"
    return MatchResultV1(status, None,
                         tuple(sanitize_candidate(trade) for trade in candidates),
                         tuple(missing))


# --- reason bytes ------------------------------------------------------------

def reason_rejection(raw) -> str | None:
    """Closed rejection token, or None when the bytes may be stored verbatim."""
    if type(raw) is not bytes:
        return "reason_not_bytes"
    if not raw.strip():
        return "reason_empty"
    if len(raw) > REASON_MAX_BYTES:
        return "reason_too_long"
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return "reason_undecodable"
    if any((ord(ch) < 32 and ch not in "\n\t") or ch == "\x7f" for ch in text):
        return "reason_control_chars"
    if any(marker in text for marker in RESERVED_MARKERS):
        return "reason_reserved_marker"
    return None


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _approval_token(*, trade_id: str, reason_digest: str, preview_digest: str,
                    file_digest: str, action: str, episode_decision: str) -> str:
    return _digest(canonical_encode([
        APPROVAL_VERSION, trade_id, reason_digest, preview_digest,
        file_digest, action, episode_decision]))


# --- rendering ---------------------------------------------------------------

def entry_id_for(trade: LedgerTradeV1) -> str:
    """README recipe: trade-<event_id 앞 8자>-<종목소문자>-<buy|sell>."""
    return f"trade-{trade.trade_event_id[:8]}-{trade.symbol.lower()}-{trade.side.lower()}"


def _event_date(trade: LedgerTradeV1) -> date:
    if trade.filled_at is None:
        return trade.trade_date
    return trade.filled_at.astimezone(KST).date()


def _trade_summary(trade: LedgerTradeV1) -> str:
    filled = (trade.trade_date.isoformat() if trade.filled_at is None
              else trade.filled_at.isoformat())
    return (f"{trade.side} {trade.quantity} {trade.symbol} @ {trade.currency} "
            f"{trade.unit_price} = {trade.amount}, filled {filled}")


def render_entry(trade: LedgerTradeV1, reason: bytes, *, entry_id: str,
                 now: datetime) -> str:
    recorded = now.astimezone(KST)
    day = recorded.date().isoformat()
    return (
        f'<a id="entry-{entry_id}"></a>\n'
        f"### {day} — {_SIDE_KR[trade.side]} ({trade.quantity}주 @ "
        f"{trade.currency} {trade.unit_price})\n\n"
        f"- `entry_id`: `{entry_id}`\n"
        f"- `source_kind`: `user_trade_reason`\n"
        f"- `source_ref`: `trade_ledger_events#{trade.trade_event_id}`\n"
        f"- `event_date`: `{_event_date(trade).isoformat()}`\n"
        f"- `recorded_date`: `{day}`\n"
        f"- `source_snapshot_date`: `{day}`\n"
        f"- `source_snapshot_basis`: `v3_trade_ledger_event`\n"
        f"- `v3_trade_event_id`: `{trade.trade_event_id}`\n"
        f"- `trade_summary`: `{_trade_summary(trade)}`\n"
        f"- `imported_at`: `{recorded.isoformat()}`\n\n"
        f'<!-- USER_RAW_START entry_id="{entry_id}" -->\n'
        f"{reason.decode('utf-8')}\n"
        f'<!-- USER_RAW_END entry_id="{entry_id}" -->\n'
    )


def extract_raw_bytes(document: bytes, entry_id: str) -> bytes | None:
    """Exact USER_RAW payload bytes, or None unless both markers occur once."""
    start = f'<!-- USER_RAW_START entry_id="{entry_id}" -->\n'.encode()
    end = f'\n<!-- USER_RAW_END entry_id="{entry_id}" -->'.encode()
    if document.count(start) != 1 or document.count(end) != 1:
        return None
    return document.split(start, 1)[1].split(end, 1)[0]


def _splice_entry(original: str, entry: str) -> str:
    """Insert directly below the Layer-3 heading (after its quote preamble)."""
    head, _, tail = original.partition(LAYER3_HEADING)
    lines = tail.split("\n")
    index = 1
    while index < len(lines) and lines[index] == "":
        index += 1
    if index < len(lines) and lines[index].startswith(">"):
        while index < len(lines) and lines[index].startswith(">"):
            index += 1
    else:
        index = 1
    preamble = "\n".join(lines[:index])
    rest = "\n".join(lines[index:]).lstrip("\n")
    updated = f"{head}{LAYER3_HEADING}{preamble}\n\n{entry}"
    return updated + (f"\n{rest}" if rest else "")


def _render_new_document(trade: LedgerTradeV1, *, entry: str, entry_id: str,
                         instrument_name: str | None,
                         hermes_interpretation: str | None,
                         user_thesis: bytes | None, day: str) -> str:
    scope = playbook_scope_dir(trade.account_scope)
    name = instrument_name or trade.symbol
    front = [
        "---", "schema_version: 1", "document_kind: instrument",
        f'playbook_id: "{scope}:{trade.market}:{trade.symbol}"',
        f"portfolio_scope: {scope}", f"market: {trade.market}",
        f'instrument_code: "{trade.symbol}"',
    ]
    if instrument_name:
        front.append(f'instrument_name: "{instrument_name}"')
    front += ["summary_version: 0", 'summary_as_of: "unreviewed"',
              'source_cutoff: "unreviewed"']
    if user_thesis is not None:
        front.append(f'thesis_as_of: "{day}"')
    front.append("---")
    hermes = [f"**{day} 기준 — 입력된 헤르 해석(대화에서 제공, AI 자동 생성 아님).**\n\n"
              f"{hermes_interpretation}" if hermes_interpretation
              else f"**{day} 기준.** {NO_INTERPRETATION}"]
    if user_thesis is None:
        hermes.append(NO_THESIS)
    if not instrument_name:
        hermes.append(NO_NAME)
    body = [
        "\n".join(front), f"# {name}",
        f"{HERMES_START}\n## 1층 — 헤르의 생각\n\n{HERMES_PREAMBLE}\n\n"
        + "\n\n".join(hermes) + f"\n{HERMES_END}",
    ]
    if user_thesis is not None:
        body.append(f"{THESIS_START}\n## 2층 — 사용자의 원문\n\n{THESIS_PREAMBLE}\n\n"
                    f"- `recorded_date`: `{day}`\n\n{user_thesis.decode('utf-8')}\n"
                    f"{THESIS_END}")
    else:
        # The validator requires the anchor index whenever no user thesis
        # section exists; it lists the entry, it never invents a thesis.
        body.append(f"{ANCHORS_START}\n## 핵심 투자 이유와 근거\n\n"
                    f"- **검토할 원문:** [`{entry_id}`](#entry-{entry_id})\n{ANCHORS_END}")
    body.append(f"{LAYER3_HEADING}\n\n{LAYER3_PREAMBLE}\n\n{entry}")
    return "\n\n".join(body)


def _text_rejection(value, *, name: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value.strip() or len(value) > 4096 \
            or (name == "instrument_name" and '"' in value):
        return f"{name}_invalid"
    if any(marker in value for marker in RESERVED_MARKERS) \
            or any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
        return f"{name}_invalid"
    return None


def _layer3_entries(text: str) -> tuple:
    """(entry_id, side) pairs in document order below the Layer-3 heading."""
    layer3 = text.split(LAYER3_HEADING, 1)[1]
    ids = _ANCHOR_RE.findall(layer3)
    sides = _SUMMARY_RE.findall(layer3)
    return tuple(zip(ids, sides + [None] * (len(ids) - len(sides))))


def _rejected(status: str) -> ReasonPreviewV1:
    return ReasonPreviewV1(status=status, text=f"미리보기 불가: {status} (쓰기 0)")


def build_reason_preview(*, playbook_root, trade: LedgerTradeV1, reason: bytes,
                         now: datetime, hermes_interpretation: str | None,
                         user_thesis: bytes | None, instrument_name: str | None,
                         episode_decision: str = "new",
                         closed_episode_entry_ids: frozenset = frozenset()
                         ) -> ReasonPreviewV1:
    """Render what an approval would write; performs no write itself."""
    rejection = (reason_rejection(reason)
                 or _text_rejection(hermes_interpretation, name="interpretation")
                 or _text_rejection(instrument_name, name="instrument_name"))
    if rejection is None and user_thesis is not None:
        thesis_problem = reason_rejection(user_thesis)
        rejection = None if thesis_problem is None else "thesis_" + thesis_problem
    if rejection:
        return _rejected(rejection)
    if not isinstance(trade, LedgerTradeV1) or not isinstance(now, datetime) \
            or now.utcoffset() is None:
        return _rejected("preview_input_invalid")
    root = Path(playbook_root)
    path = instrument_document_path(root, account_scope=trade.account_scope,
                                    market=trade.market, symbol=trade.symbol)
    if path is None:
        return _rejected("path_unresolvable")
    relative = path.relative_to(root).as_posix()
    entry_id = entry_id_for(trade)
    entry = render_entry(trade, reason, entry_id=entry_id, now=now)
    day = now.astimezone(KST).date().isoformat()
    existing_entries: tuple = ()
    if path.exists():
        try:
            original_bytes = path.read_bytes()
            original = original_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return _rejected("playbook_unreadable")
        if original.count(LAYER3_HEADING) != 1:
            return _rejected("layer3_heading_missing")
        if f'<a id="entry-{entry_id}"></a>' in original:
            recorded = extract_raw_bytes(original_bytes, entry_id)
            return _rejected("already_recorded" if recorded == reason
                             else "entry_conflict")
        existing_entries = _layer3_entries(original)
        action, file_digest = "append", _digest(original_bytes)
        document = _splice_entry(original, entry).encode("utf-8")
    else:
        action, file_digest = "create", "absent"
        document = _render_new_document(
            trade, entry=entry, entry_id=entry_id, instrument_name=instrument_name,
            hermes_interpretation=hermes_interpretation, user_thesis=user_thesis,
            day=day).encode("utf-8")
    latest = existing_entries[0] if existing_entries else None
    proposal = (f"same_episode_candidate:{latest[0]}"
                if latest and latest[1] == trade.side else "new_episode")
    if episode_decision != "new":
        target = episode_decision.removeprefix("join:")
        if not episode_decision.startswith("join:") or not target:
            return _rejected("episode_decision_invalid")
        if target in closed_episode_entry_ids:
            return _rejected("episode_closed_refused")
        if target not in {item[0] for item in existing_entries}:
            return _rejected("episode_target_missing")
    reason_digest = _digest(reason)
    text = _preview_text(
        trade=trade, action=action, relative=relative, entry_id=entry_id,
        reason=reason, reason_digest=reason_digest,
        hermes_interpretation=hermes_interpretation, user_thesis=user_thesis,
        existing_entries=existing_entries, proposal=proposal,
        episode_decision=episode_decision)
    token = _approval_token(
        trade_id=trade.trade_event_id, reason_digest=reason_digest,
        preview_digest=_digest(text.encode("utf-8")), file_digest=file_digest,
        action=action, episode_decision=episode_decision)
    return ReasonPreviewV1(
        status="ready", text=text, action=action, trade=trade,
        relative_path=relative, file_digest=file_digest, entry_id=entry_id,
        reason_digest=reason_digest, episode_proposal=proposal,
        episode_decision=episode_decision, document=document,
        approval_token=token)


def _preview_text(*, trade, action, relative, entry_id, reason, reason_digest,
                  hermes_interpretation, user_thesis, existing_entries,
                  proposal, episode_decision) -> str:
    creating = action == "create"
    interpretation = (f"헤르 해석(입력값, AI 미호출): {hermes_interpretation}"
                      if hermes_interpretation else NO_INTERPRETATION)
    if creating:
        thesis_line = (f"2층 원문: 사용자 원문 {len(user_thesis)} bytes 그대로 기록"
                       if user_thesis is not None
                       else "2층 원문: 없음 → USER_THESIS 구역을 만들지 않음(매매 사유를 원문으로 승격하지 않음)")
        layer1_line = "1층: 위 해석을 헤르 해석 라벨로 기록"
        match_line = "플레이북 대조: 파일 없음 → 신규 생성 제안"
    else:
        thesis_line = "2층 원문: 변경 없음"
        layer1_line = "1층: 변경 없음(해석은 미리보기 전용 — 투자 계획 변경이 아님)"
        latest = existing_entries[0] if existing_entries else None
        match_line = (f"플레이북 대조: 기존 3층 항목 {len(existing_entries)}건, "
                      + (f"최신 {latest[0]} 방향 {'같음' if latest[1] == trade.side else '다름'}"
                         if latest else "최신 항목 없음"))
    return "\n".join([
        f"[플레이북 매매사유 미리보기 — {'신규 파일 생성' if creating else '기존 파일에 추가'}]",
        f"대상: {relative}",
        f"거래(원장 정본): {_trade_summary(trade)}",
        f"v3_trade_event_id: {trade.trade_event_id}",
        f"항목: {entry_id}",
        f"사유 원문(바이트 그대로, {len(reason)} bytes, sha256 {reason_digest[:12]}):",
        "---", reason.decode("utf-8"), "---",
        interpretation, layer1_line, thesis_line, match_line,
        f"에피소드 제안: {proposal} / 결정: {episode_decision} (확인 전 자동 묶음 없음)",
        "쓰기 범위: 플레이북 파일 1 · 원장 0 · 주문 0 · 계좌 0 · 프로바이더 0",
        "승인 전에는 아무것도 쓰지 않는다.",
    ])


# --- apply -------------------------------------------------------------------

def _write_durable(path: Path, data: bytes, mode: int) -> None:
    """Same-directory temp, fsync, atomic replace, parent fsync."""
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


def _rollback(path: Path, original: bytes | None, mode: int) -> bool:
    try:
        if original is None:
            path.unlink()
            fsync_directory(path.parent)
            return not path.exists()
        _write_durable(path, original, mode)
        return path.read_bytes() == original
    except OSError:
        return False


def apply_approved_reason(*, playbook_root, preview: ReasonPreviewV1,
                          approval_token: str, ledger_trades: Iterable[LedgerTradeV1],
                          playbook_validator: Callable) -> ApplyResultV1:
    """The one write, gated by token, ledger re-read, validator and read-back.

    Stale file bytes, a changed ledger row, a mismatched token, or a
    validator/read-back failure all end with the pre-write bytes in place
    (or the new file removed) and ``bytes_written == 0``.
    """
    def failed(status):
        return ApplyResultV1(status, preview.relative_path, preview.entry_id, 0)

    if preview.status != "ready" or preview.document is None or preview.trade is None:
        return failed("preview_not_ready")
    expected = _approval_token(
        trade_id=preview.trade.trade_event_id, reason_digest=preview.reason_digest,
        preview_digest=_digest(preview.text.encode("utf-8")),
        file_digest=preview.file_digest, action=preview.action,
        episode_decision=preview.episode_decision)
    if type(approval_token) is not str \
            or not hmac.compare_digest(approval_token, expected) \
            or not hmac.compare_digest(preview.approval_token or "", expected):
        return failed("approval_mismatch")
    if not any(trade == preview.trade for trade in ledger_trades):
        return failed("ledger_stale")
    root = Path(playbook_root)
    path = instrument_document_path(root, account_scope=preview.trade.account_scope,
                                    market=preview.trade.market,
                                    symbol=preview.trade.symbol)
    if path is None or path.relative_to(root).as_posix() != preview.relative_path \
            or not root.is_dir():
        return failed("path_unresolvable")
    reason = extract_raw_bytes(preview.document, preview.entry_id)
    if reason is None or _digest(reason) != preview.reason_digest:
        return failed("preview_document_invalid")
    original: bytes | None = None
    mode = 0o644
    if path.exists():
        try:
            original = path.read_bytes()
        except OSError:
            return failed("playbook_unreadable")
        if extract_raw_bytes(original, preview.entry_id) == reason:
            return failed("already_recorded")
        mode = stat.S_IMODE(path.stat().st_mode)
    current_digest = "absent" if original is None else _digest(original)
    if current_digest != preview.file_digest:
        return failed("stale_file")
    if playbook_validator(root):
        return failed("validator_failed_before")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_durable(path, preview.document, mode)
    except OSError:
        # The replace is atomic: the target is either untouched or complete.
        try:
            current = path.read_bytes() if path.exists() else None
        except OSError:
            return failed("restore_uncertain")
        if current == original or _rollback(path, original, mode):
            return failed("write_failed")
        return failed("restore_uncertain")
    try:
        written = path.read_bytes()
    except OSError:
        return failed("readback_failed" if _rollback(path, original, mode)
                      else "restore_uncertain")
    if written != preview.document or extract_raw_bytes(written, preview.entry_id) != reason:
        return failed("readback_failed" if _rollback(path, original, mode)
                      else "restore_uncertain")
    if playbook_validator(root):
        restored = _rollback(path, original, mode) and not playbook_validator(root)
        return failed("validator_failed_after" if restored else "restore_uncertain")
    return ApplyResultV1(
        "applied", preview.relative_path, preview.entry_id, len(written),
        MappingProxyType({**NO_WRITES, "playbook_files": 1}))
