"""User decisions and their subsequent reviews, separate from AI reports.

One local SQLite file. User statements are immutable; later choices link to
earlier ones. A review is not a user choice and a choice is not an execution.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

from tokmo.values.quant_decisions import instant, number


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return sha256(encode(value).encode()).hexdigest()[:24]


def validated_checkpoint(value):
    """Supplied KR/KRW or US/USD regular-session one-shot contract."""
    fields = {
        "key",
        "name",
        "direction",
        "currency",
        "threshold",
        "starts_at",
        "expires_at",
        "question",
        "recipient",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("체크포인트 필드를 확인해 주세요")
    if any(
        not isinstance(value[k], str) or not value[k].strip() or len(value[k]) > 1000
        for k in fields - {"threshold"}
    ):
        raise ValueError("체크포인트 문자열이 필요합니다")
    if (
        not re.fullmatch(r"(KR|US):[A-Za-z0-9.-]{1,24}", value["key"])
        or value["direction"] not in {"<", "<=", ">", ">="}
        or value["currency"]
        != {"KR": "KRW", "US": "USD"}.get(value["key"].split(":")[0])
        or value["recipient"] != "TOKMO_GROUP"
    ):
        raise ValueError("지원하지 않는 체크포인트 조건입니다")
    threshold = number(value["threshold"])
    if threshold <= 0 or len(str(value["threshold"])) > 30:
        raise ValueError("양수 가격이 필요합니다")
    if instant(value["starts_at"]) >= instant(value["expires_at"]):
        raise ValueError("만료는 시작 이후입니다")
    return dict(value, threshold=format(threshold, "f"))


def plan_state(connection, choice, *, at):
    row = connection.execute(
        "SELECT value FROM investment_watermarks WHERE source=?",
        ("plan:" + choice["id"],),
    ).fetchone()
    state = json.loads(row[0]) if row else {}
    if "completed_at" in state:
        state["done"] = [
            t
            for t in state.get("done", [])
            if instant(state["completed_at"][t]) <= instant(at)
        ]
    if state.get("cancelled_at") and instant(state["cancelled_at"]) > instant(at):
        state = {
            k: v for k, v in state.items() if k not in {"cancelled_at", "cancel_reason"}
        }
    return state


def plan_active(connection, choice, *, at):
    return not choice.get("plan") or not plan_state(connection, choice, at=at).get(
        "cancelled_at"
    )


def validated_plan(value):
    required = {
        "kind",
        "question",
        "model",
        "reasoning_effort",
        "monitor",
        "review_at",
        "price",
    }
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - required - {"analysis_requests"}
    ):
        raise ValueError("plan 필드를 확인해 주세요")
    if (
        value["kind"] not in {"DECISION", "ANALYSIS"}
        or type(value["monitor"]) is not bool
    ):
        raise ValueError("계획/분석 요청과 지속 감시 여부가 필요합니다")
    for field in ("question", "model"):
        if (
            not isinstance(value[field], str)
            or not value[field].strip()
            or len(value[field]) > 1000
        ):
            raise ValueError("계획 질문/모델 문자열이 필요합니다")
    # "current" needs a human conversation turn; it cannot be resumed by cron.
    if value["model"] == "current":
        raise ValueError("예약에는 실행 가능한 모델 이름이 필요합니다")
    if value["reasoning_effort"] not in {
        "default",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }:
        raise ValueError("추론 강도를 확인해 주세요")
    value = dict(value)
    requests = value.get("analysis_requests", [])
    if not isinstance(requests, list) or len(requests) > 8:
        raise ValueError("분석 가정 목록을 확인해 주세요")
    seen = set()
    for r in requests:
        if (
            not isinstance(r, dict)
            or not {"key", "account", "scope"} <= set(r)
            or set(r)
            - {
                "key",
                "account",
                "scope",
                "quantity",
                "entry_price",
                "exit_price",
                "budget",
                "side",
                "currency",
                "reason",
                "condition",
            }
        ):
            raise ValueError("분석 가정 필드를 확인해 주세요")
        if not all(isinstance(r[k], str) and r[k] for k in ("key", "account", "scope")):
            raise ValueError("분석 가정 대상이 필요합니다")
        if r["key"] in seen:
            raise ValueError("분석 가정 대상 중복")
        seen.add(r["key"])
        for field in ("quantity", "entry_price", "exit_price", "budget"):
            if r.get(field) is not None and (
                number(r[field]) < 0 or field != "budget" and number(r[field]) == 0
            ):
                raise ValueError("분석 가정 금액/수량을 확인해 주세요")
        if r.get("side", "NEW_BUY") not in {"NEW_BUY", "SELL"}:
            raise ValueError("분석 가정 방향 오류")
    if value["review_at"] is not None:
        value["review_at"] = instant(value["review_at"]).isoformat()
    if value["price"] is not None:
        value["price"] = validated_checkpoint(value["price"])
    if not value["monitor"] and value["review_at"] is None and value["price"] is None:
        raise ValueError("예약 시각/가격 또는 지속 감시가 필요합니다")
    return value


def read_news_context(path, *, at, limit=24):
    """Brief choice/confirmed-delivery context; read-only even if DB is absent.

    No account identifier, cash or computed trading option is needed by NEWS.
    Unsent or uncertain reviews never become previously delivered knowledge.
    """
    from zoneinfo import ZoneInfo

    def news_time(value):
        return instant(value).astimezone(ZoneInfo("Asia/Seoul")).isoformat()

    path = Path(path)
    if not path.is_file():
        return {"status": "NOT_AVAILABLE", "choices": [], "delivered_reviews": []}
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        conn.execute("BEGIN")
        choices = [
            json.loads(r[0])
            for r in conn.execute(
                "SELECT payload FROM investment_choices ORDER BY rowid DESC"
            )
        ]
        choices = [c for c in choices if instant(c["recorded_at"]) <= instant(at)]
        replaced = {c.get("supersedes") for c in choices}
        active = [
            c
            for c in choices
            if c["id"] not in replaced and plan_active(conn, c, at=at)
        ]
        selected = active[:limit]
        rows = []
        delivered = []
        for c in selected:
            i = c["interpretation"]
            rows.append(
                {
                    "id": c["id"],
                    "scope": i["scope"],
                    "keys": i["keys"],
                    "action": i["action"],
                    "phase": i["phase"],
                    "summary": i["summary"][:240],
                    "reason_quotes": [q[:200] for q in i["reason_quotes"][:2]],
                    "condition_quotes": [q[:200] for q in i["condition_quotes"][:2]],
                    "recorded_at": news_time(c["recorded_at"]),
                }
            )
            if c.get("plan"):
                state = plan_state(conn, c, at=at)
                rows[-1]["plan"] = {
                    k: c["plan"][k]
                    for k in ("kind", "question", "review_at", "monitor")
                }
                rows[-1]["plan"]["completed_triggers"] = state.get("done", [])
            count = 0
            for payload, receipt_at in conn.execute(
                "SELECT payload,delivered_at FROM investment_reviews WHERE choice_id IN (?,?) AND delivery_state='SENT' AND receipt IS NOT NULL ORDER BY julianday(delivered_at) DESC,rowid DESC",
                (c["id"], c.get("supersedes")),
            ):
                review = json.loads(payload)
                if instant(receipt_at) > instant(at) or instant(
                    review["reviewed_at"]
                ) > instant(at):
                    continue
                a = review["assessment"]
                delivered.append(
                    {
                        "id": review["id"],
                        "choice_id": review["choice_id"],
                        "delivered_at": news_time(receipt_at),
                        "comparison": a["comparison"],
                        "new_facts": a["new_facts"],
                        "summary": a["analysis"]["summary"]
                        if a.get("analysis")
                        else None,
                    }
                )
                count += 1
                if count >= 5:
                    break
        # NEWS also remembers more than the most recent choice follow-up.
        # Keep this prompt view small; the complete receipts stay in the store.
        delivered = sorted(
            {r["id"]: r for r in delivered}.values(),
            key=lambda r: instant(r["delivered_at"]),
            reverse=True,
        )[:12]
        return {
            "status": "TRUNCATED" if len(active) > limit else "OK",
            "total_choices": len(active),
            "choices": rows,
            "delivered_reviews": delivered,
        }
    finally:
        conn.close()


class InvestmentFollowupStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10)
        path.chmod(0o600)
        self.connection.executescript("""
          CREATE TABLE IF NOT EXISTS investment_choices (
            id TEXT PRIMARY KEY, source_id TEXT UNIQUE NOT NULL,
            recorded_at TEXT NOT NULL, supersedes TEXT, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS investment_reviews (
            id TEXT PRIMARY KEY, choice_id TEXT NOT NULL, reviewed_at TEXT NOT NULL,
            input_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL,
            delivery_state TEXT NOT NULL, deliver_after TEXT,
            receipt TEXT, delivered_at TEXT);
          CREATE TABLE IF NOT EXISTS investment_calls (
            id TEXT PRIMARY KEY, started_at TEXT NOT NULL, status TEXT NOT NULL,
            request TEXT NOT NULL, response TEXT, usage TEXT, error TEXT);
          CREATE TABLE IF NOT EXISTS investment_watermarks (
            source TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS investment_inputs (
            source_id TEXT PRIMARY KEY, raw_text TEXT NOT NULL,
            received_at TEXT NOT NULL, payload TEXT NOT NULL);
        """)

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            yield

    def choice(self, choice_id):
        row = self.connection.execute(
            "SELECT payload FROM investment_choices WHERE id=?", (choice_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def input_record(self, source_id):
        row = self.connection.execute(
            "SELECT payload FROM investment_inputs WHERE source_id=?", (source_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def record_input(self, *, source_id, text, at, result, attempt, references=()):
        with self.transaction():
            previous = self.input_record(source_id)
            if previous and previous["raw_text"] != text:
                raise ValueError("동일 입력 식별자의 원문이 다릅니다")
            row = previous or {
                "source_id": source_id,
                "raw_text": text,
                "received_at": instant(at).isoformat(),
                "history": [],
                "references": list(references),
            }
            entry = {
                "at": instant(at).isoformat(),
                "attempt": attempt,
                "result": result,
            }
            if (
                not row["history"]
                or row["history"][-1]["result"] != result
                or row["history"][-1]["attempt"] != attempt
            ):
                row["history"].append(entry)
            row.update(result=result, attempt=attempt)
            self.connection.execute(
                "INSERT INTO investment_inputs VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET payload=excluded.payload",
                (source_id, text, row["received_at"], encode(row)),
            )
            return result

    def choices(self, *, at, active=True):
        rows = [
            json.loads(r[0])
            for r in self.connection.execute(
                "SELECT payload FROM investment_choices ORDER BY recorded_at,id"
            )
        ]
        rows = [r for r in rows if instant(r["recorded_at"]) <= instant(at)]
        replaced = {r.get("supersedes") for r in rows}
        return [
            r
            for r in rows
            if not active
            or r["id"] not in replaced
            and plan_active(self.connection, r, at=at)
        ]

    def record_choice(
        self,
        *,
        raw_text,
        interpretation,
        source_id,
        at,
        references=(),
        checkpoint=None,
        plan=None,
    ):
        """Interpretation is validated by the input program, never a trade command."""
        if not source_id or not raw_text.strip():
            raise ValueError("사용자 원문과 입력 식별자가 필요합니다")
        when = instant(at).isoformat()
        if plan is not None:
            plan = validated_plan(plan)
            if checkpoint is not None:
                raise ValueError("계획의 price 필드를 사용하세요")
        if checkpoint is not None:
            checkpoint = validated_checkpoint(checkpoint)
            if interpretation["keys"] != [checkpoint["key"]]:
                raise ValueError("체크포인트 종목이 다릅니다")
        with self.transaction():
            row = self.connection.execute(
                "SELECT payload FROM investment_choices WHERE source_id=?", (source_id,)
            ).fetchone()
            if row:
                old = json.loads(row[0])
                if old["raw_text"] != raw_text:
                    raise ValueError("동일 입력 식별자의 원문이 다릅니다")
                if checkpoint is not None and (
                    old.get("checkpoint") != checkpoint
                    or old["interpretation"] != interpretation
                ):
                    raise ValueError("동일 입력 식별자의 승인 조건이 다릅니다")
                if (plan is not None or old.get("plan") is not None) and (
                    old.get("plan") != plan or old["interpretation"] != interpretation
                ):
                    raise ValueError("동일 입력 식별자의 계획이 다릅니다")
                return old
            previous_id = interpretation.get("supersedes")
            if previous_id:
                previous = self.choice(previous_id)
                if not previous or instant(previous["recorded_at"]) > instant(at):
                    raise ValueError("이전 선택을 확인할 수 없습니다")
                if any(
                    previous["interpretation"][k] != interpretation[k]
                    for k in ("account", "scope")
                ):
                    raise ValueError("다른 계좌의 선택을 바꿀 수 없습니다")
                if self.connection.execute(
                    "SELECT 1 FROM investment_choices WHERE supersedes=?",
                    (previous_id,),
                ).fetchone():
                    raise ValueError(
                        "이미 변경된 선택입니다. 최신 선택을 읽고 다시 정리해 주세요"
                    )
            value = dict(
                id="choice-" + digest(source_id),
                source_id=source_id,
                raw_text=raw_text,
                interpretation=interpretation,
                recorded_at=when,
                references=list(references),
                supersedes=previous_id,
                execution_status="USER_REPORTED"
                if interpretation["phase"] == "REPORTED_EXECUTED"
                else "NOT_VERIFIED",
            )
            if checkpoint is not None:
                value["checkpoint"] = checkpoint
            if plan is not None:
                value["plan"] = plan
            self.connection.execute(
                "INSERT INTO investment_choices VALUES(?,?,?,?,?)",
                (value["id"], source_id, when, previous_id, encode(value)),
            )
            return value

    def checkpoint_state(self, choice, *, at):
        cp = validated_checkpoint(choice["checkpoint"])
        state = self.watermark("checkpoint:" + choice["id"], {})
        status = state.get("status", "NOT_CHECKED")
        if state.get("cancelled_at"):
            status = "CANCELLED"
        elif instant(at) >= instant(cp["expires_at"]):
            status = "EXPIRED"
        elif instant(at) < instant(cp["starts_at"]):
            status = "NOT_STARTED"
        elif choice["id"] not in {c["id"] for c in self.choices(at=at)}:
            status = "SUPERSEDED"
        review = (
            self.latest_review(choice["id"], at=at) if state.get("review_id") else None
        )
        return dict(
            state,
            choice_id=choice["id"],
            status=status,
            checkpoint=cp,
            expires_at=cp["expires_at"],
            cadence_seconds=300,
            last_checked_at=state.get("last_checked_at"),
            delivery_state=review["delivery_state"] if review else None,
            delivered_at=review["delivered_at"] if review else None,
        )

    def checkpoint_open(self, choice_id, *, at):
        choice = self.choice(choice_id)
        if not choice:
            return False
        if "checkpoint" not in choice:
            return True
        try:
            cp = validated_checkpoint(choice["checkpoint"])
            return instant(cp["starts_at"]) <= instant(at) < instant(
                cp["expires_at"]
            ) and not self.watermark("checkpoint:" + choice_id, {}).get("cancelled_at")
        except (ValueError, TypeError, KeyError):
            return False

    def cancel_checkpoint(self, choice_id, *, at, reason):
        choice = self.choice(choice_id)
        if (
            not choice
            or "checkpoint" not in choice
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise ValueError("체크포인트와 취소 근거가 필요합니다")
        state = self.watermark("checkpoint:" + choice_id, {})
        if not state.get("cancelled_at"):
            state.update(
                status="CANCELLED",
                cancelled_at=instant(at).isoformat(),
                cancel_reason=reason,
            )
            self.set_watermark("checkpoint:" + choice_id, state)
        return self.checkpoint_state(choice, at=at)

    def latest_review(self, choice_id, *, at, delivered=False):
        query = "SELECT payload,delivery_state,delivered_at FROM investment_reviews WHERE choice_id=? ORDER BY rowid DESC"
        for payload, state, delivered_at in self.connection.execute(
            query, (choice_id,)
        ):
            row = json.loads(payload)
            if instant(row["reviewed_at"]) <= instant(at) and (
                not delivered
                or state == "SENT"
                and instant(delivered_at) <= instant(at)
            ):
                return dict(row, delivery_state=state, delivered_at=delivered_at)
        return None

    def review_by_input(self, input_key):
        row = self.connection.execute(
            "SELECT payload FROM investment_reviews WHERE input_key=?", (input_key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_review(self, value):
        with self.transaction():
            existing = self.review_by_input(value["input_key"])
            if existing:
                return existing
            # Replace only unattempted pending advice; preserve the previous body
            # and explicit supersession. Never erase an uncertain transport result.
            if value["assessment"]["notify"]:
                self.connection.execute(
                    "UPDATE investment_reviews SET delivery_state='SUPERSEDED' WHERE choice_id=? AND delivery_state IN ('PENDING','RETRY','FAILED')",
                    (value["choice_id"],),
                )
            elif value["assessment"].get("pending_action") == "CANCEL":
                self.connection.execute(
                    "UPDATE investment_reviews SET delivery_state='CANCELLED' WHERE choice_id=? AND delivery_state IN ('PENDING','RETRY','FAILED') AND COALESCE(json_extract(payload,'$.input.trigger.reason'),'')!='USER_PLAN'",
                    (value["choice_id"],),
                )
            state = "PENDING" if value["assessment"]["notify"] else "SILENT"
            self.connection.execute(
                "INSERT INTO investment_reviews VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    value["id"],
                    value["choice_id"],
                    value["reviewed_at"],
                    value["input_key"],
                    encode(value),
                    state,
                    value.get("deliver_after"),
                    None,
                    None,
                ),
            )
            return value

    def pending(self, *, at):
        from zoneinfo import ZoneInfo

        quiet = not 8 <= instant(at).astimezone(ZoneInfo("Asia/Seoul")).hour < 22
        active = {r["id"] for r in self.choices(at=at)}
        return [
            json.loads(r[0])
            for r in self.connection.execute(
                "SELECT payload,deliver_after FROM investment_reviews WHERE delivery_state IN ('PENDING','RETRY') ORDER BY rowid"
            )
            if json.loads(r[0])["choice_id"] in active
            and instant(r[1]) <= instant(at)
            and self.checkpoint_open(json.loads(r[0])["choice_id"], at=at)
            and (not quiet or json.loads(r[0])["assessment"]["urgency"] == "IMMEDIATE")
        ]

    def claim(self, review_id, *, at):
        with self.transaction():
            row = self.connection.execute(
                "SELECT choice_id FROM investment_reviews WHERE id=?", (review_id,)
            ).fetchone()
            if (
                not row
                or row[0] not in {c["id"] for c in self.choices(at=at)}
                or not self.checkpoint_open(row[0], at=at)
            ):
                return False
            if not self.connection.execute(
                "UPDATE investment_reviews SET delivery_state='SENDING' WHERE id=? AND delivery_state IN ('PENDING','RETRY') AND julianday(deliver_after)<=julianday(?)",
                (review_id, instant(at).isoformat()),
            ).rowcount:
                return False
            meta = self.watermark("delivery:" + review_id, {})
            meta["tries"] = meta.get("tries", 0) + 1
            meta.update(definitely_not_sent=False, reason=None, retry_at=None)
            meta.setdefault("attempts", []).append(
                {"at": instant(at).isoformat(), "status": "SENDING"}
            )
            self._delivery_meta(review_id, meta)
            return True

    def uncertain_deliveries(self):
        return [
            {"id": row[0], "status": row[1]}
            for row in self.connection.execute(
                "SELECT id,delivery_state FROM investment_reviews WHERE delivery_state IN ('SENDING','UNKNOWN') ORDER BY rowid"
            )
        ]

    def settle(self, review_id, *, receipt=None, at=None):
        if receipt is not None and (
            not isinstance(receipt, str) or not receipt.strip()
        ):
            raise ValueError("빈 발송 확인값입니다")
        with self.transaction():
            if not self.connection.execute(
                "UPDATE investment_reviews SET delivery_state=?,receipt=?,delivered_at=? WHERE id=? AND delivery_state='SENDING'",
                (
                    "SENT" if receipt else "UNKNOWN",
                    receipt,
                    instant(at).isoformat() if receipt else None,
                    review_id,
                ),
            ).rowcount:
                raise ValueError("발송 상태가 일치하지 않습니다")
            meta = self.watermark("delivery:" + review_id, {})
            if meta.get("attempts"):
                meta["attempts"][-1].update(
                    status="SENT" if receipt else "UNKNOWN", receipt=receipt
                )
                self._delivery_meta(review_id, meta)

    def _delivery_meta(self, review_id, meta):
        # Called inside the surrounding transaction; do not commit it early.
        self.connection.execute(
            "INSERT INTO investment_watermarks VALUES(?,?) ON CONFLICT(source) DO UPDATE SET value=excluded.value",
            ("delivery:" + review_id, encode(meta)),
        )

    def fail_delivery(
        self, review_id, *, reason, retry_at=None, definitely_not_sent=False
    ):
        state = "RETRY" if retry_at else "FAILED"
        with self.transaction():
            if not self.connection.execute(
                "UPDATE investment_reviews SET delivery_state=?,deliver_after=COALESCE(?,deliver_after) WHERE id=? AND delivery_state='SENDING'",
                (state, instant(retry_at).isoformat() if retry_at else None, review_id),
            ).rowcount:
                raise ValueError("발송 상태가 일치하지 않습니다")
            meta = self.watermark("delivery:" + review_id, {})
            meta.update(
                reason=reason,
                definitely_not_sent=definitely_not_sent,
                retry_at=instant(retry_at).isoformat() if retry_at else None,
            )
            if meta.get("attempts"):
                meta["attempts"][-1].update(status=state, reason=reason)
            self._delivery_meta(review_id, meta)

    def delivery_issues(self):
        rows = self.connection.execute(
            "SELECT id,delivery_state FROM investment_reviews WHERE delivery_state IN ('FAILED','RETRY','UNKNOWN','SENDING') ORDER BY rowid"
        )
        return [
            dict(
                id=key,
                status=state,
                **{
                    k: v
                    for k, v in self.watermark("delivery:" + key, {}).items()
                    if k in {"tries", "reason", "retry_at"}
                },
            )
            for key, state in rows
        ]

    def resolve_delivery(
        self,
        review_id,
        *,
        action,
        reason,
        at,
        receipt=None,
        delivered_at=None,
        confirmed_not_sent=False,
    ):
        """Operator conclusion is audited; an UNKNOWN is never silently resent."""
        if (
            action not in {"sent", "requeue", "cancel"}
            or not reason
            or not reason.strip()
        ):
            raise ValueError("처리 방식과 확인 근거가 필요합니다")
        with self.transaction():
            row = self.connection.execute(
                "SELECT choice_id,delivery_state,reviewed_at,receipt FROM investment_reviews WHERE id=?",
                (review_id,),
            ).fetchone()
            if not row:
                raise ValueError("후속 전달 기록을 찾을 수 없습니다")
            if row[1] == "SENT" and action == "sent" and row[3] == receipt:
                return {"id": review_id, "status": "SENT", "unchanged": True}
            if row[1] not in {"UNKNOWN", "SENDING", "FAILED", "RETRY"}:
                raise ValueError("복구 대상 상태가 아닙니다")
            meta = self.watermark("delivery:" + review_id, {})
            if action == "sent":
                if (
                    not receipt
                    or not str(receipt).strip()
                    or not delivered_at
                    or not instant(row[2]) <= instant(delivered_at) <= instant(at)
                ):
                    raise ValueError("실제 전달 확인값과 유효한 전달 시각이 필요합니다")
                self.connection.execute(
                    "UPDATE investment_reviews SET delivery_state='SENT',receipt=?,delivered_at=? WHERE id=?",
                    (str(receipt), instant(delivered_at).isoformat(), review_id),
                )
            elif action == "requeue":
                if row[0] not in {c["id"] for c in self.choices(at=at)}:
                    raise ValueError("이미 바뀐 선택의 알림은 재전송하지 않습니다")
                if not self.checkpoint_open(row[0], at=at):
                    raise ValueError("취소/만료된 체크포인트는 재전송하지 않습니다")
                if not (confirmed_not_sent or meta.get("definitely_not_sent")):
                    raise ValueError("실제 미전달 확인이 필요합니다")
                self.connection.execute(
                    "UPDATE investment_reviews SET delivery_state='PENDING',deliver_after=? WHERE id=?",
                    (instant(at).isoformat(), review_id),
                )
                meta["tries"] = 0
            else:
                self.connection.execute(
                    "UPDATE investment_reviews SET delivery_state='CANCELLED' WHERE id=?",
                    (review_id,),
                )
            meta["retry_at"] = None
            meta.setdefault("resolutions", []).append(
                {
                    "at": instant(at).isoformat(),
                    "action": action,
                    "reason": reason,
                    "previous_state": row[1],
                    "receipt": receipt,
                    "delivered_at": instant(delivered_at).isoformat()
                    if delivered_at
                    else None,
                    "confirmed_not_sent": confirmed_not_sent,
                }
            )
            self._delivery_meta(review_id, meta)
            return {
                "id": review_id,
                "status": {"sent": "SENT", "requeue": "PENDING", "cancel": "CANCELLED"}[
                    action
                ],
            }

    def call(self, key, prompt, model, *, at, validator, correction=None):
        """Persist before calling. One correction for bad structure, no retry loop.

        This journal never repeats a failed call. The worker may provide a new
        key within its persisted, bounded transient-retry policy. ``correction``
        builds the one follow-up prompt from the error and the rejected answer;
        without it the whole original prompt is resent, which for a large packet
        costs as much as the review itself.
        """
        for attempt in range(2):
            call_id = key + ":" + str(attempt)
            with self.transaction():
                row = self.connection.execute(
                    "SELECT status,response,request FROM investment_calls WHERE id=?",
                    (call_id,),
                ).fetchone()
                if row and row[2] != prompt:
                    raise ValueError("저장된 호출 입력이 다릅니다")
                if row and row[0] not in {"RETURNED", "OK", "INVALID"}:
                    raise ValueError(
                        "이전 모델 호출 미완료 또는 실패: 명시적 재시도 필요"
                    )
                if not row:
                    self.connection.execute(
                        "INSERT INTO investment_calls VALUES(?,?,'STARTED',?,NULL,NULL,NULL)",
                        (call_id, instant(at).isoformat(), prompt),
                    )
            if row:
                raw = json.loads(row[1])
            else:
                try:
                    raw = model(prompt)
                    calls = getattr(model, "calls", [])
                    usage = calls[-1].get("usage", {}) if calls else {}
                    with self.connection:
                        self.connection.execute(
                            "UPDATE investment_calls SET status='RETURNED',response=?,usage=? WHERE id=?",
                            (encode(raw), encode(usage), call_id),
                        )
                except Exception as error:
                    with self.connection:
                        self.connection.execute(
                            "UPDATE investment_calls SET status='ERROR',error=? WHERE id=?",
                            (type(error).__name__, call_id),
                        )
                    raise
            try:
                value = validator(raw)
                with self.connection:
                    self.connection.execute(
                        "UPDATE investment_calls SET status='OK' WHERE id=?", (call_id,)
                    )
                return value
            except (ValueError, KeyError, TypeError) as error:
                with self.connection:
                    self.connection.execute(
                        "UPDATE investment_calls SET status='INVALID',error=? WHERE id=?",
                        (str(error)[:240], call_id),
                    )
                if attempt:
                    raise
                if correction is not None:
                    prompt = correction(str(error)[:240], raw)
                else:
                    prompt += (
                        "\n형식/근거 오류를 한 번 수정하세요: "
                        + str(error)[:240]
                        + "\n이전 응답(자료):\n"
                        + encode(raw)[:16000]
                    )

    def watermark(self, source, default=None):
        row = self.connection.execute(
            "SELECT value FROM investment_watermarks WHERE source=?", (source,)
        ).fetchone()
        return json.loads(row[0]) if row else default

    def set_watermark(self, source, value):
        with self.connection:
            self.connection.execute(
                "INSERT INTO investment_watermarks VALUES(?,?) ON CONFLICT(source) DO UPDATE SET value=excluded.value",
                (source, encode(value)),
            )
