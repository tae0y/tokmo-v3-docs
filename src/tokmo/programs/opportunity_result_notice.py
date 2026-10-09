"""User-visible summary of durably applied PAPER slots; stored decision text only.

Delivery belongs to the caller (Hermes no_agent cron sends nonempty stdout).
The ledger therefore records hand-off to stdout, never a Telegram receipt.
"""

import fcntl
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from tokmo.programs.opportunity_rotation import available
from tokmo.records.quant_decision_reports import atomic_private

ACTIONS = dict(
    ENTER="매수 제안",
    ADD="추가매수 제안",
    HOLD="보유",
    EXIT="매도 제안",
    SWITCH="교체 매도 제안",
    WATCH="관찰",
    CANCEL="주문 취소",
)
ORDER_STATES = (
    ("FILLED", "모의 체결"),
    ("PENDING", "모의 주문 대기"),
    ("EXPIRED", "미체결 만료"),
    ("CANCELLED", "취소"),
)
LIMIT = 3500  # below Telegram's 4096 per message


def won(value):
    amount = int(Decimal(value).quantize(Decimal(1), ROUND_HALF_UP))
    return f"{amount // 10000:,}만원" if amount % 10000 == 0 else f"{amount:,}원"


def _orders(state, assessments, side):
    ids = [
        a["proposal"]["id"]
        for a in assessments
        if a.get("proposal", {}).get("side") == side
    ]
    if not ids:
        return None
    statuses = [state["proposals"].get(i, {}).get("status") for i in ids]
    parts = [
        f"{label} {statuses.count(s)}건" for s, label in ORDER_STATES if s in statuses
    ]
    if "FILLED" not in statuses:
        parts.append("체결 아님")
    return (
        f"{'매수' if side == 'BUY' else '매도'} 제안 {len(ids)}건({', '.join(parts)})"
    )


def render(state, key):
    saved = state["slots"][key]
    if saved["status"] != "DECIDED":
        raise ValueError("only applied DECIDED slots are reported")
    decision = saved["decision"]
    day, rest = key.split(":", 1)
    when = (
        f"{day[5:]} {saved['applied_at'][11:16]} 예외"
        if rest.startswith("official:")
        else f"{day[5:]} {rest}"
    )
    names = {
        c["symbol"]: c.get("name") or c["symbol"] for c in saved["packet"]["candidates"]
    }
    assessments = decision["assessments"]
    trades = [
        t
        for t in (
            _orders(state, assessments, "BUY"),
            _orders(state, assessments, "SELL"),
        )
        if t
    ]
    if not any(t.startswith("매수") for t in trades):
        trades.insert(0, "신규 매수 제안 없음")
    cash = f"현금 {won(state['cash'])}"
    if available(state) != Decimal(state["cash"]):
        cash += f" · 가용 {won(available(state))}"
    labelled = [
        f"{names.get(a['symbol'], a['symbol'])}({ACTIONS[a['action']]})"
        for a in assessments
    ]
    lines = [
        f"[톡모 모의투자 · {when}] 분석 완료 · {' · '.join(trades)} · {cash}",
        "검토: " + (", ".join(labelled) if labelled else "별도 검토 의견 없음"),
        f"현금 판단: {decision['cash_reason']}",
    ]
    reasons = [f"- {label}: {a['reason']}" for label, a in zip(labelled, assessments)]
    text = "\n".join(lines + reasons)
    if len(text) > LIMIT:  # drop whole reasons, never cut a sentence mid-way
        text = "\n".join(lines + ["(종목별 이유는 길어 생략, PAPER 장부에 저장됨)"])
    return text


def drain(paper, ledger, *, now):
    """Return at most one new applied slot's text; first run baselines history silently."""
    state = paper.state()
    decided = sorted(
        (s["applied_at"], k)
        for k, s in state.get("slots", {}).items()
        if s["status"] == "DECIDED"
    )
    ledger = Path(ledger)
    ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ledger.parent / "notice.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None  # another minute's run owns the hand-off
        try:
            if not ledger.exists():
                slots = {k: dict(status="BASELINE", at=now) for _, k in decided}
                atomic_private(ledger, dict(version=1, baseline_at=now, slots=slots))
                return None
            record = json.loads(ledger.read_text())
            key = next((k for _, k in decided if k not in record["slots"]), None)
            if key is None:
                return None
            text = render(state, key)
            # At-most-once: persisted before stdout; the cron owns actual delivery.
            record["slots"][key] = dict(
                status="EMITTED_TO_CRON_STDOUT",
                at=now,
                delivery="cron-owned; Telegram receipt not observed here",
            )
            atomic_private(ledger, record)
            return text
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
