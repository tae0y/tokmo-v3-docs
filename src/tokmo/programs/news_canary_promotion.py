"""Deterministic NEWS canary auto-promotion controller (cron watchdog).

Runs every few minutes until the canary window closes and converts the
durable canary evidence into exactly one terminal gate transition:

- attempt settled SENT with a complete post-cutoff provider receipt, a
  meaningful body, and the durable two-slot zero-duplicate proof →
  atomically rewrite the gate to a fresh TELEGRAM_LIVE activation whose
  predecessor is the canary and whose window is exactly the production
  live maximum (7 days from the promotion instant);
- window expired with no attempt, a FAILED/UNKNOWN settle, a stale
  RESERVED row, a broken receipt, a rejected body, or a refused
  duplicate proof → atomically rewrite the same gate fail-closed OFF.

Everything else is a silent wait. Zero model calls and zero network:
every judgment reads durable state through the production parsers
(``read_news_canary_state``, ``read_news_gate``,
``record_canary_duplicate_proof``), historical pre-cutoff outbox rows
never veto, and the body check is a bounded fail-closed last-mile
denylist over vocabulary the production renderer already owns — never
broad NLP. The only writes are the controller's own gate rewrite
(atomic replace, mode 0600, fsync, byte backup) and the durable proof
row the production seam records.

Cron ``no_agent`` stdout semantics: empty stdout and exit 0 on every
wait or no-op; exactly one concise Korean line on a terminal transition
(promotion or OFF); non-zero only when the controller itself is broken
(including a failed post-write readback, which restores the prior gate
bytes). A single non-blocking flock guarantees one instance; nothing
here schedules anything.
"""

import argparse
import fcntl
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tokmo.programs.news_alert_delivery import live_delivery_authorized
from tokmo.programs.news_canary_readback import read_news_canary_state
from tokmo.programs.news_live_control import (
    NewsLiveControlError,
    record_canary_duplicate_proof,
)
from tokmo.programs.news_topic_discovery import substantive_statement
from tokmo.programs.pulse_production_deps import (
    NEWS_GATE_MAX_BYTES,
    NEWS_LIVE_WINDOW_MAX_SECONDS,
    read_news_gate,
)
from tokmo.values.news_digest import FORBIDDEN_DIGEST_MARKERS
from tokmo.values.ops_kernel import created_on_or_after

CONTROLLER_ID = "news_canary_promotion"
LOCK_NAME = ".news_canary_promotion.lock"
GATE_NAME = "news_activation.json"

# A canary reservation precedes the transport start by design and settles
# within seconds; a RESERVED row this old is a crashed attempt, never an
# in-flight one. Conservative bound: recent RESERVED always waits.
RESERVED_STALE_SECONDS = 1800

# Fail-closed last-mile body denylist bounds (approval: the production
# admission/renderer owns semantic quality; this only refuses the known
# degenerate shapes at the final promotion judgment).
MIN_BODY_CHARS = 20
_ROUTING_MARKERS = (
    "NEWS_EVENT",
    "NEWS_OPS",
    "alert_id",
    "dedupe_key",
    "body_evidence",
    "provider_message_id",
    "PENDING",
    "ATTEMPTING",
    "UNRESOLVED",
)
# Renderer/digest boilerplate vocabulary: stripped before asking whether
# any substantive content token remains (counts drop out in tokenization).
_BOILERPLATE_FRAGMENTS = (
    "뉴스 동향",
    "긴급 NEWS",
    "포트폴리오 직접 관련",
    "전체 뉴스 빈도에서 발견",
    "사건",
    "근거",
    "기사",
    "출처",
    "판정",
    "대상",
    "알림",
    "속보",
    "업데이트",
)

_WAIT = {"action": "wait", "line": None, "exit_code": 0}
_PROOF_WAIT_MARKER = "fewer than two completed pulse slots"


def _read_only_rows(path, query, parameters=()):
    if not Path(path).exists():
        return None
    try:
        connection = sqlite3.connect(
            f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=0.25
        )
    except sqlite3.Error:
        return None
    try:
        return connection.execute(query, parameters).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()


class _ReadOnlyProofLedger:
    """Mode=ro ledger reads for the post-write readback judgment."""

    def __init__(self, path):
        self._path = Path(path)

    def _one(self, query, columns, parameters):
        rows = _read_only_rows(self._path, query, parameters)
        return None if not rows else dict(zip(columns, rows[0]))

    def canary_attempt(self, activation_id):
        return self._one(
            "SELECT activation_id,ops_alert_id,reserved_at,outcome,"
            "settled_at FROM canary_attempts WHERE activation_id=?",
            ("activation_id", "ops_alert_id", "reserved_at", "outcome", "settled_at"),
            (activation_id,),
        )

    def canary_slot_proof(self, activation_id):
        return self._one(
            "SELECT activation_id,settled_outcome,slots_json,"
            "transport_starts,recorded_at FROM canary_slot_proofs"
            " WHERE activation_id=?",
            (
                "activation_id",
                "settled_outcome",
                "slots_json",
                "transport_starts",
                "recorded_at",
            ),
            (activation_id,),
        )

    def live_trip(self, activation_id):
        return self._one(
            "SELECT activation_id,reason,tripped_at FROM live_delivery_trips"
            " WHERE activation_id=?",
            ("activation_id", "reason", "tripped_at"),
            (activation_id,),
        )


# ------------------------------------------------------- body denylist


def _heading_subject(heading):
    if heading.startswith("🚨 긴급 NEWS · "):
        return heading[len("🚨 긴급 NEWS · ") :].strip()
    if heading.startswith("📰 ") and heading.endswith(" 뉴스 동향"):
        return heading[len("📰 ") : -len(" 뉴스 동향")].strip()
    return None


def body_rejection(text):
    """Return a rejection slug for a degenerate alert body, else None.

    Bounded fail-closed denylist over shapes production already forbids
    (internal JSON, routing labels, digest boilerplate, count-only text,
    a generic function-word subject) — never a semantic quality model."""
    if type(text) is not str:
        return "body_not_text"
    stripped = text.strip()
    if len(stripped) < MIN_BODY_CHARS:
        return "body_too_short"
    if stripped.startswith(("{", "[")):
        return "body_internal_json"
    if any(marker in stripped for marker in _ROUTING_MARKERS):
        return "body_routing_label"
    if any(marker in stripped for marker in FORBIDDEN_DIGEST_MARKERS):
        return "body_boilerplate"
    heading = stripped.splitlines()[0].strip()
    subject = _heading_subject(heading)
    if subject is not None and not substantive_statement(subject):
        return "body_generic_subject"
    residue = stripped
    for fragment in _BOILERPLATE_FRAGMENTS:
        residue = residue.replace(fragment, " ")
    if not substantive_statement(residue):
        return "body_count_only"
    return None


# ------------------------------------------------------- atomic gate IO


def _atomic_write_bytes(path, data, *, mode=0o600):
    """Same-directory temp file, 0600, fsync, atomic replace, dir fsync."""
    path = Path(path)
    temp = path.parent / f".{path.name}.tmp-{os.getpid()}"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        try:
            os.write(fd, data)
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _rewrite_gate(root, payload, now):
    """Back up the current gate bytes, then atomically install ``payload``.

    Returns the prior bytes so a failed readback can restore them."""
    gate = Path(root) / GATE_NAME
    original = gate.read_bytes()
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    _atomic_write_bytes(gate.parent / f"{GATE_NAME}.bak-{stamp}", original)
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1).encode(
        "utf-8"
    )
    if len(data) > NEWS_GATE_MAX_BYTES:
        raise ValueError("gate payload exceeds the parser byte bound")
    _atomic_write_bytes(gate, data)
    return original


def _off_payload(root, reason, now):
    """The same gate rewritten fail-closed OFF: original approved keys are
    preserved, ``enabled`` drops to false and delivery to OFF, and the
    terminal reason/instant are recorded in place."""
    payload = json.loads((Path(root) / GATE_NAME).read_text("utf-8"))
    payload.update(
        {
            "enabled": False,
            "delivery_mode": "OFF",
            "off_reason": reason,
            "off_at": now.astimezone(UTC).isoformat(),
            "off_by": CONTROLLER_ID,
        }
    )
    return payload


def _live_gate_payload(canary_id, now):
    cutoff = now.astimezone(UTC)
    live_id = f"news-live-{cutoff:%Y%m%dT%H%M%SZ}-auto"
    window_end = cutoff + timedelta(seconds=NEWS_LIVE_WINDOW_MAX_SECONDS)
    return live_id, {
        "enabled": True,
        "producer_mode": "SHADOW",
        "delivery_mode": "TELEGRAM_LIVE",
        "activation_id": live_id,
        "predecessor_activation_id": canary_id,
        "cutoff": cutoff.isoformat(),
        "window_end": window_end.isoformat(),
        "approved_by": "woong",
        "promoted_at": cutoff.isoformat(),
        "promoted_by": CONTROLLER_ID,
    }


# --------------------------------------------------------- judgments


def _fail_closed_off(root, canary_id, reason, now):
    _rewrite_gate(root, _off_payload(root, reason, now), now)
    return {
        "action": "off",
        "reason": reason,
        "line": f"⛔ NEWS 카나리 자동 OFF: {reason} · {canary_id}",
        "exit_code": 0,
    }


def _inspect_sent_receipt(root, attempt, cutoff):
    """Contract §4: the canonical outbox row must be the canary's own
    complete post-cutoff NEWS_EVENT Telegram SENT receipt with a
    meaningful body. Returns None (accept), "WAIT" (store unreadable this
    tick — retry, never guess), or a fail-closed rejection reason."""
    rows = _read_only_rows(
        Path(root) / "ops" / "ops_kernel.sqlite3",
        "SELECT channel,origin,status,created_at,attempted_at,delivered_at,"
        "provider_message_id,body_evidence_json FROM alert_outbox"
        " WHERE alert_id=?",
        (attempt["ops_alert_id"],),
    )
    if rows is None:
        return "WAIT"
    if not rows:
        return "canary_outbox_row_missing"
    (
        channel,
        origin,
        status,
        created_at,
        attempted_at,
        delivered_at,
        provider_message_id,
        body_json,
    ) = rows[0]
    if channel != "TELEGRAM":
        return "canary_outbox_channel_mismatch"
    if origin != "NEWS_EVENT":
        return "canary_outbox_origin_mismatch"
    if status != "SENT":
        return "canary_outbox_status_not_sent"
    if not attempted_at or not delivered_at or not provider_message_id:
        return "canary_outbox_receipt_incomplete"
    if not created_on_or_after(created_at, cutoff):
        return "canary_outbox_pre_cutoff"
    try:
        body_evidence = json.loads(body_json)
    except (TypeError, ValueError):
        return "canary_body_rejected:body_unparseable"
    if type(body_evidence) is not list or not body_evidence:
        return "canary_body_rejected:body_unparseable"
    rejection = body_rejection(body_evidence[0])
    if rejection is not None:
        return f"canary_body_rejected:{rejection}"
    return None


def _promote(root, canary_id, now):
    """Contract §5/§6: install the successor TELEGRAM_LIVE gate and prove
    it back through the production parser before claiming anything."""
    live_id, payload = _live_gate_payload(canary_id, now)
    original = _rewrite_gate(root, payload, now)
    try:
        delivery = read_news_gate(root).delivery
        ledger = _ReadOnlyProofLedger(Path(root) / "stores" / "news_episodes.sqlite3")
        authorized, off_reason = live_delivery_authorized(ledger, delivery, now)
    except Exception:
        _atomic_write_bytes(Path(root) / GATE_NAME, original)
        raise
    if not (
        delivery.mode == "TELEGRAM_LIVE"
        and delivery.activation_id == live_id
        and delivery.predecessor_activation_id == canary_id
        and authorized
    ):
        detail = off_reason or f"gate_mode_{delivery.mode}"
        _atomic_write_bytes(Path(root) / GATE_NAME, original)
        return {
            "action": "broken",
            "reason": detail,
            "line": (
                f"❗ NEWS 카나리 승격 검증 실패({detail}) · 이전 게이트 복원 완료"
            ),
            "exit_code": 1,
        }
    return {
        "action": "promoted",
        "reason": None,
        "line": (
            f"✅ NEWS 카나리 승격: {canary_id} → TELEGRAM_LIVE"
            f" {live_id} · 라이브 종료 {payload['window_end']}"
        ),
        "exit_code": 0,
    }


def run_canary_promotion(root, *, now):
    root = Path(root)
    state = read_news_canary_state(root, now=now)
    if state["delivery_mode"] != "TELEGRAM_CANARY":
        # already OFF, already LIVE, or a malformed/absent gate: the
        # controller owns only the bound canary and stays silent.
        return dict(_WAIT, action="noop")
    canary_id = state["activation_id"]
    cutoff = datetime.fromisoformat(state["cutoff"])
    window_end = datetime.fromisoformat(state["window_end"])
    attempt = state["attempt"]
    if attempt is None:
        if now < window_end:
            return _WAIT
        return _fail_closed_off(
            root, canary_id, "canary_expired_no_eligible_event", now
        )
    outcome = attempt["outcome"]
    if outcome in ("FAILED", "UNKNOWN"):
        return _fail_closed_off(
            root, canary_id, f"canary_attempt_{outcome.lower()}", now
        )
    if outcome == "RESERVED":
        try:
            reserved_at = datetime.fromisoformat(attempt["reserved_at"])
            if reserved_at.tzinfo is None:
                raise ValueError("naive reserved_at")
        except (TypeError, ValueError):
            return _fail_closed_off(
                root, canary_id, "canary_attempt_reserved_unparseable", now
            )
        if now - reserved_at < timedelta(seconds=RESERVED_STALE_SECONDS):
            return _WAIT
        return _fail_closed_off(root, canary_id, "canary_attempt_reserved_stale", now)
    # outcome == SENT: last-mile receipt/body judgment before the proof
    rejection = _inspect_sent_receipt(root, attempt, cutoff)
    if rejection == "WAIT":
        return _WAIT
    if rejection is not None:
        return _fail_closed_off(root, canary_id, rejection, now)
    try:
        record_canary_duplicate_proof(root=root, activation_id=canary_id, now=now)
    except NewsLiveControlError as error:
        if _PROOF_WAIT_MARKER in str(error):
            return _WAIT  # two post-settle slots not yet observed
        return _fail_closed_off(root, canary_id, "canary_proof_refused", now)
    return _promote(root, canary_id, now)


# --------------------------------------------------------------- CLI


def _acquire_lock(root):
    """Single-instance non-blocking flock; None means another holder."""
    fd = os.open(Path(root) / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def main(argv=None, *, now_provider=None):
    parser = argparse.ArgumentParser(
        description="Deterministic NEWS canary auto-promotion watchdog."
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--now", help="aware ISO-8601 judgment instant")
    arguments = parser.parse_args(argv)
    if arguments.now is not None:
        try:
            now = datetime.fromisoformat(arguments.now.replace("Z", "+00:00"))
        except ValueError:
            parser.error("--now must be ISO-8601")
    elif now_provider is not None:
        try:
            now = now_provider()
        except Exception as error:
            parser.error(f"now_provider failed: {type(error).__name__}")
    else:
        parser.error("--now is required when now_provider is absent")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        parser.error("judgment time must be timezone-aware")
    try:
        lock_fd = _acquire_lock(Path(arguments.root))
    except OSError as error:
        print(f"{CONTROLLER_ID}: broken lock: {error}", file=sys.stderr)
        return 2
    if lock_fd is None:
        return 0  # another instance holds the tick; silent by contract
    try:
        outcome = run_canary_promotion(Path(arguments.root), now=now)
    except Exception as error:  # broken controller, never a routine wait
        print(f"{CONTROLLER_ID}: broken: {error}", file=sys.stderr)
        return 2
    finally:
        os.close(lock_fd)
    if outcome["line"] is not None:
        print(outcome["line"])
    return outcome["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
