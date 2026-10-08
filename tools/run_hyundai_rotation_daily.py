#!/usr/bin/env python3
"""Run the unattended rotation cycle once for every configured instrument.

Ambient edges live here only: the wall clock, the process environment (the
stock-bot Telegram credentials exported by the cron wrapper) and the GLM
credential file. ``composition`` builds the broker wiring, ``rotation_daily``
decides. Live sending is off unless the owner-created flag file exists.

Subcommands: ``run`` (default), ``approve <proposal-id>``, ``reject
<proposal-id>``, ``proposals``. A band proposal is written whenever the model
hits the band's edge; approving it rewrites experiments.json (validated,
backed up, atomic) — band fields only, never the risk envelope.

Exit 0 when every instrument ended in an expected state (order accepted,
hold, skip, already done today); exit 1 when any needs a human.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from decimal import Decimal
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SRC = PROJECT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tokmo.composition import build_rotation_daily_wiring
from tokmo.programs.rotation_daily import run_daily
from tokmo.values.rotation_policy import BAND_FIELDS, load_policies, policy_record

STATE_DIR = Path(__file__).resolve().parents[1] / "var" / "state" / "hyundai-rotation"  # relocated from ~/.hermes/state 2026-09-24
ROOT = Path(__file__).resolve().parents[1] / "var" / "data"
ZAI_ENV_FILE = Path.home() / ".hermes" / ".env"
MODEL_ID = "glm-5.3"
MODEL_TIMEOUT_SECONDS = 25
MODEL_MAX_TOKENS = 1024
ENV_TELEGRAM_TOKEN = "TELEGRAM_STOCK_BOT_TOKEN"
ENV_TELEGRAM_CHAT_ID = "TELEGRAM_STOCK_BOT_CHAT_ID"
EXPECTED = {"HOLD", "EXISTING", "PREVIEW", "SKIP"}
APPROVE_COMMAND = f"{PROJECT}/.venv/bin/python {PROJECT}/tools/run_hyundai_rotation_daily.py approve"


def private_dir(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and (info.st_mode & 0o077) == 0


def private_flag(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and (info.st_mode & 0o077) == 0


def write_private(path: Path, text: str, *, exclusive=False):
    """0600 file; either brand new (exclusive) or replaced atomically in place."""
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        return
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(raw, path)
    except BaseException:
        Path(raw).unlink(missing_ok=True)
        raise


def build_model_transport(*, model_id, env_file, timeout_seconds):
    """The NEWS classifier's registered direct GLM route: one bounded POST, no retry."""
    from tokmo.facts.news_http import zai_chat_once
    from tokmo.programs.news_classifier_executor import extract_json_object, read_zai_credential_file

    def transport(request):
        prompt = (request["system"] + "\n\n입력(JSON):\n"
                  + json.dumps(request, ensure_ascii=False, sort_keys=True)
                  + "\n\n위 규칙에 맞는 JSON 객체 하나만 출력해.")
        message = zai_chat_once(prompt, timeout_seconds=timeout_seconds,
                                api_key=read_zai_credential_file(env_file), model=model_id,
                                max_tokens=MODEL_MAX_TOKENS, json_mode=True)
        return json.dumps(extract_json_object(message), ensure_ascii=False)
    return transport


def _side_kr(side):
    return {"BUY": "매수", "SELL": "매도"}.get(side, side)


def _won(value):
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return str(value)


def summary_text(outcome, policy, *, live):
    d = outcome.details
    head = f"[회전매매 {policy.symbol}] "
    decision = d.get("decision") or {}
    reason = decision.get("reason") or outcome.reason
    mode = "" if live else " (드라이런: 실전송 꺼짐)"
    if outcome.status == "SUBMITTED":
        status = d.get("execution_status")
        code = d.get("execution_code")
        payload = (d.get("command") or {}).get("payload") or {}
        if decision.get("action") == "ENTER" and "kind" in payload:
            plan = f"매수 {_won(decision['buy_price'])} → 매도 {_won(decision['sell_price'])} (OTO, 오늘 만료)"
        elif decision.get("action") == "ENTER":
            plan = f"지정가 매수 {_won(decision['buy_price'])} (현재가가 이미 아래라 OTO 대신 당일 일반주문; 매도는 다음 거래일)"
        else:
            plan = f"지정가 매도 {_won(decision.get('sell_price'))} (당일 일반주문)"
        if status == "ACCEPTED":
            return head + f"주문 접수됨{mode}\n{plan}\n판단: {reason}"
        return head + f"주문 미전송/미확정 — {status}/{code}{mode}\n{plan}\n판단: {reason}\n토스 주문내역 확인 필요"
    if outcome.status == "HOLD":
        return head + f"오늘은 관망\n판단: {reason}"
    if outcome.status == "SKIP":
        key, _, rest = outcome.reason.partition(":")
        why = {"market_closed": f"휴장/장외 ({rest})", "outside_regular_session": f"정규장 아님 ({rest})",
               "standing_order_open": "기존 주문이 아직 진행 중", "experiment_expired": "실험 기간 종료",
               "calendar_unknown": "장 운영 정보 없음(cadence 캘린더 저장본 확인)"}.get(key, outcome.reason)
        return head + f"주문 없음 — {why}"
    if outcome.status == "EXISTING":
        return head + "오늘 판단은 이미 기록됨 — 재실행 무시"
    if outcome.status == "HALT":
        return head + f"자동화 중단 — {outcome.reason}\n사람 확인 후 재개 필요"
    return head + f"{outcome.status} — {outcome.reason}\n주문 없음"


def proposal_text(proposal_id, proposal, policy):
    current = policy_record(policy)
    lines = [f"⚠️ [회전매매 {policy.symbol}] 밴드 경계 도달 — 재조정 제안"]
    for key, value in proposal["changes"].items():
        lines.append(f"- {key}: {_won(current[key])} → {_won(value)}")
    lines.append(f"근거: {proposal['reason']}")
    lines.append(f"승인: {APPROVE_COMMAND} {proposal_id}")
    lines.append("(승인 전까지 기존 밴드 유지)")
    return "\n".join(lines)


def notify(environ, text):
    token, chat_id = environ.get(ENV_TELEGRAM_TOKEN), environ.get(ENV_TELEGRAM_CHAT_ID)
    if not token or not chat_id:
        return False
    from tokmo.facts.telegram_reminder_live import send_pension_reminder
    try:
        send_pension_reminder(body_text=text, bot_token=token, chat_id=chat_id)
        return True
    except Exception:
        return False


def write_receipt(state_dir, outcome, policy, *, now, live, notified):
    runs = state_dir / "runs"
    runs.mkdir(mode=0o700, exist_ok=True)
    path = runs / f"{now.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}-{policy.experiment_id}.json"
    record = {"recorded_at": now.isoformat(), "live": live, "status": outcome.status, "reason": outcome.reason,
              "details": outcome.details, "policy": policy_record(policy), "notified": notified}
    write_private(path, json.dumps(record, ensure_ascii=False, indent=2, default=str), exclusive=True)
    return path


def pending_proposals(state_dir, statuses=("PENDING",)):
    folder = state_dir / "proposals"
    if not folder.is_dir():
        return {}
    found = {}
    for path in sorted(folder.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("status") in statuses:
            found[path.stem] = record
    return found


def record_proposal(state_dir, outcome, policy, *, now):
    """Persist today's band proposal unless an identical one is already pending."""
    proposal = outcome.details.get("band_proposal")
    if proposal is None:
        return None, None
    for existing_id, existing in pending_proposals(state_dir).items():
        if existing["experiment_id"] == policy.experiment_id and existing["changes"] == proposal["changes"]:
            return existing_id, None
    folder = state_dir / "proposals"
    folder.mkdir(mode=0o700, exist_ok=True)
    proposal_id = f"{outcome.details['day'].replace('-', '')}-{policy.experiment_id}"
    path = folder / f"{proposal_id}.json"
    if path.exists():
        return proposal_id, None
    record = {"proposal_id": proposal_id, "experiment_id": policy.experiment_id, "status": "PENDING",
              "created_at": now.isoformat(), "changes": proposal["changes"], "reason": proposal["reason"],
              "current": {key: policy_record(policy)[key] for key in BAND_FIELDS}}
    write_private(path, json.dumps(record, ensure_ascii=False, indent=2), exclusive=True)
    return proposal_id, record


def resolve_proposal(state_dir, proposal_id, *, approve, now):
    """Apply (or decline) one pending proposal. Returns (ok, message)."""
    path = state_dir / "proposals" / f"{proposal_id}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "proposal_not_found"
    if record.get("status") != "PENDING":
        return False, "proposal_not_pending:" + str(record.get("status"))
    if not approve:
        record.update(status="REJECTED", resolved_at=now.isoformat())
        write_private(path, json.dumps(record, ensure_ascii=False, indent=2))
        return True, "rejected"
    if record.get("scope") == "today":
        # Same-day approval of the analysis's out-of-band prices: the band itself stays.
        record.update(status="APPROVED", approved_at=now.isoformat(), resolved_at=now.isoformat())
        write_private(path, json.dumps(record, ensure_ascii=False, indent=2))
        return True, "approved_today"
    if any(key not in BAND_FIELDS for key in record["changes"]):
        return False, "envelope_fields_require_manual_edit"
    config_path = state_dir / "experiments.json"
    try:
        text = config_path.read_text(encoding="utf-8")
        document = json.loads(text)
    except (OSError, ValueError):
        return False, "experiments_unreadable"
    targets = [item for item in document.get("experiments", []) if item.get("experiment_id") == record["experiment_id"]]
    if len(targets) != 1:
        return False, "experiment_not_found"
    targets[0].update({key: str(value) for key, value in record["changes"].items()})
    revised = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    try:
        load_policies(revised)
    except ValueError as error:
        return False, "revised_policy_invalid:" + str(error)
    backup = config_path.with_name(f"experiments.json.bak-{now.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}")
    write_private(backup, text, exclusive=True)
    write_private(config_path, revised)
    record.update(status="APPROVED", resolved_at=now.isoformat(), backup=str(backup))
    write_private(path, json.dumps(record, ensure_ascii=False, indent=2))
    return True, "approved"


def apply_protected_change(state_dir, policy, protected_quantity, *, now):
    """Write the moved protected rail back to the config the policy is read from.

    The journal has already moved; this keeps ``experiments.json`` in step so the
    next load matches the book. A failure here is deliberately loud — the two
    disagreeing is what the next settlement refuses to work through.
    """
    config_path = state_dir / "experiments.json"
    try:
        text = config_path.read_text(encoding="utf-8")
        document = json.loads(text)
    except (OSError, ValueError):
        return False, "experiments_unreadable"
    targets = [item for item in document.get("experiments", []) if item.get("experiment_id") == policy.experiment_id]
    if len(targets) != 1:
        return False, "experiment_not_found"
    targets[0]["protected_quantity"] = str(protected_quantity)
    revised = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    try:
        load_policies(revised)
    except ValueError as error:
        return False, "revised_policy_invalid:" + str(error)
    stem = f"experiments.json.bak-{now.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
    for suffix in ("", *(f"-{n}" for n in range(2, 10))):
        backup = config_path.with_name(stem + suffix)
        try:
            write_private(backup, text, exclusive=True)
        except FileExistsError:
            continue                        # a second change within the same second keeps its own copy
        except OSError:
            return False, "experiments_write_failed"
        try:
            write_private(config_path, revised)
        except OSError:
            return False, "experiments_write_failed"
        return True, str(backup)
    return False, "experiments_backup_exhausted"


_ORDER_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def record_manual_fill(state, args, *, now):
    """Book a fill the owner made in the app as the experiment's own lot.

    The runner only ever books its own orders; a manual trade otherwise shows
    up as a protected-quantity mismatch and halts the next day. Recording it
    here (with the broker's order id as the fill identity) lets the cycle
    continue from the true inventory. Numbers are taken verbatim from the
    owner — nothing is estimated.
    """
    from tokmo.records.order_command_journal import CommandConflict, OrderCommandJournal
    from tokmo.values.rotation_policy import align_price

    policies = load_policies((state / "experiments.json").read_text(encoding="utf-8"))
    matches = [p for p in policies if args.experiment_id in (None, p.experiment_id)]
    if len(matches) != 1:
        return False, {"reason": "experiment_ambiguous_or_unknown", "known": [p.experiment_id for p in policies]}
    policy = matches[0]
    if not _ORDER_ID.fullmatch(args.order_id or ""):
        return False, {"reason": "order_id_invalid"}
    try:
        quantity, price = Decimal(args.quantity), Decimal(args.price)
        commission, tax = Decimal(args.commission), Decimal(args.tax)
    except ArithmeticError:
        return False, {"reason": "numbers_invalid"}
    if quantity <= 0 or quantity != quantity.to_integral_value() or price <= 0 or commission < 0 or tax < 0:
        return False, {"reason": "numbers_invalid"}
    if align_price(price, policy.market, args.side) != price:
        return False, {"reason": "price_not_on_tick", "tick_aligned": str(align_price(price, policy.market, args.side))}
    filled_at = datetime.fromisoformat(args.filled_at) if args.filled_at else now
    if filled_at.tzinfo is None or filled_at > now:
        return False, {"reason": "filled_at_invalid"}
    journal = OrderCommandJournal(state / "orders.sqlite3")
    journal.initialize_experiment(policy.experiment_id, mode="live", envelope=policy.envelope())
    # A settlement that already filed this fill under the protected lot guessed
    # wrong; declaring it here takes it back and returns the rail where it was.
    reclaimed = None
    if journal.protected_fill(policy.experiment_id, mode="live", fill_id=args.order_id) is not None:
        restored = policy.protected_quantity + (quantity if args.side == "SELL" else -quantity)
        if restored < 0:
            return False, {"reason": "reclassification_would_make_protected_negative"}
        journal.release_protected_fill(policy.experiment_id, mode="live", fill_id=args.order_id)
        journal.reconfigure_protected_quantity(policy.experiment_id, mode="live",
                                               envelope=replace(policy.envelope(), protected_quantity=restored))
        ok, why = apply_protected_change(state, policy, restored, now=now)
        reclaimed = {"protected_before": str(policy.protected_quantity), "protected_after": str(restored),
                     "policy_file": why}
        if not ok:
            return False, {"reason": "protected_policy_write_failed:" + why, "reclaimed": reclaimed}
        policy = replace(policy, protected_quantity=restored)
    book = journal.experiment_book(policy.experiment_id, mode="live")
    fill_id = "manual-" + args.order_id
    # A repeated broker order id is an idempotent replay (the journal compares the
    # payload); only a genuinely new fill is checked against the inventory limits.
    if journal.experiment_fill(policy.experiment_id, mode="live", fill_id=fill_id) is None:
        if args.side == "BUY" and book["quantity"] + quantity > policy.quantity:
            return False, {"reason": "exceeds_experiment_quantity", "held": str(book["quantity"]), "max": str(policy.quantity)}
        if args.side == "SELL" and quantity > book["quantity"]:
            return False, {"reason": "sell_exceeds_experiment_inventory", "held": str(book["quantity"])}
    try:
        applied = journal.apply_experiment_fill(policy.experiment_id, mode="live", fill_id=fill_id,
                                                symbol=policy.symbol, side=args.side, quantity=quantity,
                                                amount=price * quantity, costs=commission + tax, observed_at=filled_at)
    except CommandConflict as error:
        return False, {"reason": str(error)}
    after = journal.experiment_book(policy.experiment_id, mode="live")
    record = {"kind": "manual_fill", "experiment_id": policy.experiment_id, "order_id": args.order_id, "side": args.side,
              "quantity": str(quantity), "price": str(price), "commission": str(commission), "tax": str(tax),
              "filled_at": filled_at.isoformat(), "applied": applied, "book_after": {k: str(v) for k, v in after.items()},
              "note": args.note}
    if reclaimed:
        record["reclaimed_from_protected"] = reclaimed
    runs = state / "runs"
    runs.mkdir(mode=0o700, exist_ok=True)
    write_private(runs / f"{now.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}-manual-{policy.experiment_id}.json",
                  json.dumps(record, ensure_ascii=False, indent=2))
    return applied != "FROZEN", record


def decision_path(state, policy, trade_date):
    return state / "decisions" / f"{trade_date}-{policy.experiment_id}.json"


def ensure_today_proposal(state, policy, raw, *, trade_date, now):
    """When the analysis waits for approval, give the owner a same-day proposal to approve.

    Returns (proposal_id, record) with record=None when it already existed or
    the file is not waiting. The record is the runner's own approval source;
    the analysis file's own flags are never trusted.
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return None, None
    if type(data) is not dict or data.get("status") != "WAITING_APPROVAL" or type(data.get("decision")) is not dict:
        return None, None
    proposal_id = f"{trade_date.replace('-', '')}-today-{policy.experiment_id}"
    path = state / "proposals" / f"{proposal_id}.json"
    if path.exists():
        return proposal_id, None
    decision = data["decision"]
    # revision/analysis_id/policy_digest mirror the analysis file so the owner's
    # approval bridge can bind its approval to exactly this proposal.
    record = {"proposal_id": proposal_id, "scope": "today", "status": "PENDING",
              "revision": data.get("revision") if type(data.get("revision")) is int else 1,
              "experiment_id": policy.experiment_id, "trade_date": trade_date, "created_at": now.isoformat(),
              "producer": data.get("producer"), "analysis_id": data.get("analysis_id"),
              "policy_digest": data.get("policy_digest"),
              "decision": {k: decision.get(k) for k in ("action", "buy_price", "sell_price", "quantity")},
              "band_proposal": data.get("band_proposal"), "current_band": {k: policy_record(policy)[k] for k in BAND_FIELDS},
              "reason": (data.get("band_proposal") or {}).get("reason") or decision.get("reason") or ""}
    path.parent.mkdir(mode=0o700, exist_ok=True)
    write_private(path, json.dumps(record, ensure_ascii=False, indent=2), exclusive=True)
    return proposal_id, record


def today_proposal_text(proposal_id, record, policy):
    d = record["decision"]
    lines = [f"⚠️ [회전매매 {policy.symbol}] 오늘 제안이 밴드 밖 — 승인 대기 (마감은 제안 알림에 표시된 시각)"]
    lines.append(f"- 제안: {d.get('action')} 매수 {_won(d.get('buy_price'))} / 매도 {_won(d.get('sell_price'))} ({d.get('quantity')}주)")
    band = record["current_band"]
    lines.append(f"- 현재 정책: 매수 {_won(band['buy_floor'])}~{_won(band['buy_ceiling'])}, "
                 f"매도 ≥ 체결가+{band['sell_min_pct']}% (기본 목표 +{band['sell_target_pct']}%)")
    lines.append(f"- 근거: {record['reason']}")
    lines.append(f"오늘만 승인: {APPROVE_COMMAND} {proposal_id}")
    lines.append("(승인 전에는 주문 없음; 밴드 자체는 바뀌지 않음)")
    return "\n".join(lines)


def analysis_owns_notice(outcome):
    """Only the known analysis lifecycle is notified by its producer.

    Missing/invalid files and execution failures remain the runner's duty.
    Notification delivery failures do not change the decision contract.
    """
    return (outcome.details.get("producer") == "astra"
            and bool(outcome.details.get("analysis_id"))
            and outcome.status in {"FAILED", "EXPIRED"}
            and (outcome.reason in {"analysis_timeout", "approval_timeout"}
                 or outcome.reason.startswith(("analysis_failed:", "analysis_expired:"))))


def run_file(args, *, environ, clock, emit):
    """v0.4 execution entry: consume the analysis file, no model, no substitute band."""
    import fcntl
    import time
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from tokmo.programs.rotation_daily import run_from_decision_file

    state = args.state_dir.expanduser()
    if not private_dir(state):
        emit({"status": "HALT", "reason": "state_dir_not_private", "path": str(state)})
        return 1
    try:
        policies = [p for p in load_policies((state / "experiments.json").read_text(encoding="utf-8")) if p.market == args.market]
    except (OSError, ValueError) as error:
        emit({"status": "HALT", "reason": "policy_invalid:" + str(error)})
        return 1
    if not policies:
        emit({"status": "SKIP", "reason": "no_experiments_for_market", "market": args.market})
        return 0
    kst = ZoneInfo("Asia/Seoul")
    now_kst = clock().astimezone(kst)
    hour, minute = (int(part) for part in args.deadline.split(":"))
    deadline = now_kst.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if deadline <= now_kst:
        emit({"status": "SKIP", "reason": "deadline_already_passed", "deadline": deadline.isoformat()})
        return 0
    live = private_flag(state / "live.flag")
    lock_path = state / "run-file.lock"
    lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        emit({"status": "SKIP", "reason": "another_run_in_progress"})
        return 0
    needs_human = False
    try:
        for policy in policies:
            announced = {"proposal": False}

            def reader(policy=policy, announced=announced):
                trade_date = clock().astimezone(kst).date().isoformat()
                path = decision_path(state, policy, trade_date)
                try:
                    raw = path.read_text(encoding="utf-8")
                except OSError:
                    return None
                proposal_id, record = ensure_today_proposal(state, policy, raw, trade_date=trade_date, now=clock())
                # Astra's notice contains the owned approval buttons. Keep the
                # canonical proposal mirror, but do not send a second CLI prompt.
                analysis_notice = record is not None and json.loads(raw).get("producer") == "astra"
                if record is not None and args.notify and not analysis_notice and not announced["proposal"]:
                    announced["proposal"] = notify(environ, today_proposal_text(proposal_id, record, policy))
                return raw

            def approvals():
                return [r for r in pending_proposals(state, statuses=("APPROVED",)).values() if r.get("scope") == "today"]
            try:
                wiring = build_rotation_daily_wiring(project_root=args.project_root, root=args.root, account_scope=policy.account_scope,
                                                    journal_path=state / "orders.sqlite3", clock=clock, live_enabled=live)
                outcome = run_from_decision_file(policy=policy, journal=wiring.journal, reads=wiring.reads, decision_reader=reader,
                                                 approvals_reader=approvals, clock=clock, sleeper=time.sleep, client=wiring.client,
                                                 deadline=deadline, poll_seconds=args.poll_seconds)
            except Exception as error:
                emit({"experiment_id": policy.experiment_id, "status": "HALT", "reason": "wiring_failed:" + type(error).__name__})
                needs_human = True
                continue
            now = clock()
            text = summary_text(outcome, policy, live=live)
            if outcome.details.get("producer"):
                text += f"\n판단 주체: {outcome.details['producer']}"
            if "entry:limit_buy_trigger_already_met" in outcome.details.get("notes", []):
                text += "\n(당일 후속 매도 미예약 — 다음 거래일 분석에서 매도/보류 판단)"
            notice_owner = "analysis" if analysis_owns_notice(outcome) else "runner"
            notified = notify(environ, text) if args.notify and notice_owner == "runner" else False
            receipt = write_receipt(state, outcome, policy, now=now, live=live, notified=notified)
            accepted = outcome.status == "SUBMITTED" and outcome.details.get("execution_status") == "ACCEPTED"
            dry_run = (not live and outcome.status == "SUBMITTED" and outcome.details.get("execution_code") == "transport_disabled")
            ok = accepted or dry_run or outcome.status in {"HOLD", "EXISTING", "SKIP"}
            needs_human = needs_human or not ok
            emit({"experiment_id": policy.experiment_id, "status": outcome.status, "reason": outcome.reason, "ok": ok, "live": live,
                  "producer": outcome.details.get("producer"), "receipt": str(receipt), "notified": notified,
                  "notification_owner": notice_owner,
                  "decision": outcome.details.get("decision"), "execution_status": outcome.details.get("execution_status"),
                  "execution_code": outcome.details.get("execution_code"), "notes": outcome.details.get("notes")})
    finally:
        os.close(lock_fd)
    return 1 if needs_human else 0


def settle(args, *, environ, clock, emit):
    """Book yesterday's fills for one market; no decision, no order (v0.4 A1).

    The cron slot only decides when to try; the calendar store decides whether
    the market's last regular session closed at least ``margin`` ago. One
    settlement per market per calendar day, recorded under ``settlements/``.
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from tokmo.programs.rotation_daily import classify_external_fills, settle_open_rows, settlement_due

    state = args.state_dir.expanduser()
    if not private_dir(state):
        emit({"status": "HALT", "reason": "state_dir_not_private", "path": str(state)})
        return 1
    try:
        policies = [p for p in load_policies((state / "experiments.json").read_text(encoding="utf-8")) if p.market == args.market]
    except (OSError, ValueError) as error:
        emit({"status": "HALT", "reason": "policy_invalid:" + str(error)})
        return 1
    if not policies:
        emit({"status": "SKIP", "reason": "no_experiments_for_market", "market": args.market})
        return 0
    now = clock()
    day = now.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    marker = state / "settlements" / f"{args.market}-{day}.json"
    if marker.exists():
        emit({"status": "SKIP", "reason": "already_settled_today", "marker": str(marker)})
        return 0
    needs_human, records = False, []
    for policy in policies:
        try:
            wiring = build_rotation_daily_wiring(project_root=args.project_root, root=args.root, account_scope=policy.account_scope,
                                                journal_path=state / "orders.sqlite3", clock=clock, live_enabled=False)
            due, why = settlement_due(market=policy.market, now=now, reads=wiring.reads, margin=timedelta(minutes=args.margin_minutes))
            if not due:
                emit({"experiment_id": policy.experiment_id, "status": "SKIP", "reason": why})
                return 0
            result = settle_open_rows(policy=policy, journal=wiring.journal, reads=wiring.reads, now=now)
            if result["status"] == "OK":
                # Whatever the owner traded in the app and never declared is the
                # protected lot's; the rail follows it so the next day can run.
                result["external"] = classify_external_fills(policy=policy, journal=wiring.journal,
                                                             reads=wiring.reads, now=now)
                if result["external"]["status"] == "APPLIED":
                    ok, why = apply_protected_change(state, policy, result["external"]["protected_after"], now=now)
                    result["external"]["policy_file"] = why
                    if not ok:
                        result.update(status="HALT", reason="protected_policy_write_failed:" + why)
                elif result["external"]["status"] == "HALT":
                    result.update(status="HALT", reason="external_fill:" + result["external"]["reason"])
        except Exception as error:
            result = {"experiment_id": policy.experiment_id, "status": "HALT", "reason": "settle_wiring_failed:" + type(error).__name__}
        records.append(result)
        needs_human = needs_human or result["status"] != "OK"
        emit(result)
    marker.parent.mkdir(mode=0o700, exist_ok=True)
    write_private(marker, json.dumps({"market": args.market, "day": day, "at": now.isoformat(), "results": records},
                                     ensure_ascii=False, indent=2, default=str))
    if args.notify:
        lines = [f"[회전매매 정산 {args.market}]"]
        for r in records:
            fills = [n for n in r.get("notes", []) if n.startswith("fill:") or n.startswith("group_gone:")]
            book = r.get("book") or {}
            lines.append(f"{r['experiment_id']}: {r['status']}/{r['reason']}"
                         + (f" 체결 {', '.join(fills)}" if fills else " 체결 없음")
                         + (f" → 실험 재고 {book.get('quantity')}주, 장부 현금 {_won(book.get('cash'))}원" if book else ""))
            external = r.get("external") or {}
            for fill in external.get("classified", []):
                lines.append(f"  · 미신고 체결 {_side_kr(fill['side'])} {fill['quantity']}주 @{_won(fill['price'])}원"
                             f" → 보호물량 거래로 분류")
            if external.get("status") == "APPLIED":
                lines.append(f"  → 보호수량 {external['protected_before']}주 → {external['protected_after']}주"
                             f" (회전용이었다면 record-fill로 정정)")
        if needs_human:
            lines.append("사람 확인 필요")
        notify(environ, "\n".join(lines))
    return 1 if needs_human else 0


def run(args, *, environ, clock, emit):
    state = args.state_dir.expanduser()
    if not private_dir(state):
        emit({"status": "HALT", "reason": "state_dir_not_private", "path": str(state)})
        return 1
    try:
        policies = load_policies((state / "experiments.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        emit({"status": "HALT", "reason": "policy_invalid:" + str(error)})
        return 1
    if args.market:
        policies = tuple(p for p in policies if p.market == args.market)
        if not policies:
            emit({"status": "SKIP", "reason": "no_experiments_for_market", "market": args.market})
            return 0
    forced = None
    if args.force_trade_date:
        from datetime import date as _date
        from tokmo.records.calendar_lookahead import PulseSessionVerdictV1
        from tokmo.values.session_gate import Market
        trade_date = _date.fromisoformat(args.force_trade_date).isoformat()
        forced = lambda market, now: PulseSessionVerdictV1(Market(market), True, "forced_open", trade_date, "regularMarket", None)
    live = args.live or private_flag(state / "live.flag")
    model_transport = (lambda request: None) if args.no_model else build_model_transport(
        model_id=args.model_id, env_file=args.zai_env_file, timeout_seconds=MODEL_TIMEOUT_SECONDS)
    model_id = "default-band-only" if args.no_model else args.model_id

    wiring_by_scope = {}
    needs_human = False
    for policy in policies:
        try:
            if policy.account_scope not in wiring_by_scope:
                wiring_by_scope[policy.account_scope] = build_rotation_daily_wiring(
                    project_root=args.project_root, root=args.root, account_scope=policy.account_scope,
                    journal_path=state / "orders.sqlite3", clock=clock, live_enabled=live)
            wiring = wiring_by_scope[policy.account_scope]
            reads = wiring.reads if forced is None else replace(wiring.reads, session_verdict=forced)
            outcome = run_daily(policy=policy, journal=wiring.journal, reads=reads,
                                model_transport=model_transport, model_id=model_id, clock=clock,
                                client=wiring.client)
        except Exception as error:
            emit({"experiment_id": policy.experiment_id, "status": "HALT", "reason": "wiring_failed:" + type(error).__name__})
            needs_human = True
            continue
        now = clock()
        text = summary_text(outcome, policy, live=live)
        proposal_id, new_proposal = record_proposal(state, outcome, policy, now=now)
        if new_proposal is not None:
            text += "\n\n" + proposal_text(proposal_id, new_proposal, policy)
        notified = notify(environ, text) if args.notify else False
        receipt = write_receipt(state, outcome, policy, now=now, live=live, notified=notified)
        accepted = outcome.status == "SUBMITTED" and outcome.details.get("execution_status") == "ACCEPTED"
        dry_run = (not live and outcome.status == "SUBMITTED"
                   and outcome.details.get("execution_code") == "transport_disabled")
        ok = accepted or dry_run or outcome.status in EXPECTED
        needs_human = needs_human or not ok
        emit({"experiment_id": policy.experiment_id, "status": outcome.status, "reason": outcome.reason,
              "ok": ok, "live": live, "receipt": str(receipt), "notified": notified,
              "decision": outcome.details.get("decision"), "execution_status": outcome.details.get("execution_status"),
              "execution_code": outcome.details.get("execution_code"), "notes": outcome.details.get("notes"),
              "band_proposal": proposal_id})
    return 1 if needs_human else 0


def main(argv=None, *, environ=None, clock=None):
    parser = argparse.ArgumentParser(description="현대차 회전매매 하루 1회 무인 실행")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    sub = parser.add_subparsers(dest="command")
    runner = sub.add_parser("run", help="오늘 사이클 실행 (기본)")
    runner.add_argument("--root", type=Path, default=ROOT)
    runner.add_argument("--project-root", type=Path, default=PROJECT)
    runner.add_argument("--zai-env-file", type=Path, default=ZAI_ENV_FILE)
    runner.add_argument("--model-id", default=MODEL_ID)
    runner.add_argument("--no-model", action="store_true", help="모델 호출 없이 기본 밴드만 적용")
    runner.add_argument("--live", action="store_true", help="실계좌 전송 켜기 (플래그 파일과 동일 효과)")
    runner.add_argument("--notify", action="store_true", help="결과를 텔레그램 주식봇으로 통지")
    runner.add_argument("--market", choices=("KR", "US"), default=None,
                        help="이 시장의 실험만 실행 (크론은 시장별 정규장 시각에 따로 등록)")
    runner.add_argument("--force-trade-date", metavar="YYYY-MM-DD", default=None,
                        help="테스트 전용: 세션 게이트를 '정규장 열림'으로 강제. 운영 상태 디렉터리에서는 거부")
    for name in ("approve", "reject"):
        sub.add_parser(name, help=f"밴드 재조정 제안 {name}").add_argument("proposal_id")
    sub.add_parser("proposals", help="대기 중 제안 목록")
    filer = sub.add_parser("run-file", help="v0.4: 분석 결정 파일을 기다렸다가 집행 (모델·기본 밴드 대체 없음)")
    filer.add_argument("--market", choices=("KR", "US"), required=True)
    filer.add_argument("--root", type=Path, default=ROOT)
    filer.add_argument("--project-root", type=Path, default=PROJECT)
    filer.add_argument("--deadline", default="11:10",
                       help="KST HH:MM, 러너 자체 상한. 승인 대기 결정은 파일의 valid_until(알림 발송+30분)로 먼저 만료되며, 분석 미완은 10:40 마감 처리로 끝난다")
    filer.add_argument("--poll-seconds", type=int, default=15)
    filer.add_argument("--notify", action="store_true")
    settler = sub.add_parser("settle", help="장 종료 후 전일 주문 정산만 수행 (판단·주문 없음)")
    settler.add_argument("--market", choices=("KR", "US"), required=True)
    settler.add_argument("--root", type=Path, default=ROOT)
    settler.add_argument("--project-root", type=Path, default=PROJECT)
    settler.add_argument("--margin-minutes", type=int, default=35, help="정규장 종료 후 이 시간이 지나야 정산")
    settler.add_argument("--notify", action="store_true")
    fill = sub.add_parser("record-fill", help="앱에서 직접 체결한 매매를 실험 장부에 기록")
    fill.add_argument("--order-id", required=True, help="토스 주문번호 (체결 식별자, 중복 기록 방지)")
    fill.add_argument("--side", required=True, choices=("BUY", "SELL"))
    fill.add_argument("--price", required=True, help="체결가 (호가단위 정수)")
    fill.add_argument("--quantity", default="1")
    fill.add_argument("--commission", default="0")
    fill.add_argument("--tax", default="0")
    fill.add_argument("--filled-at", default=None, help="체결 시각 ISO8601(시간대 포함); 생략 시 지금")
    fill.add_argument("--experiment-id", default=None, help="실험이 여러 개일 때 지정")
    fill.add_argument("--note", default="")
    fill.add_argument("--notify", action="store_true")
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(token in {"run", "run-file", "approve", "reject", "proposals", "record-fill", "settle", "-h", "--help"} for token in argv):
        # "run" is the default subcommand; it follows the global --state-dir pair if present.
        position = 2 if argv[:1] == ["--state-dir"] else 0
        argv.insert(position, "run")
    args = parser.parse_args(argv)
    environ = dict(os.environ) if environ is None else environ
    clock = (lambda: datetime.now(timezone.utc)) if clock is None else clock
    emit = lambda payload: print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    state = args.state_dir.expanduser()

    if args.command in ("approve", "reject"):
        if not private_dir(state):
            emit({"status": "HALT", "reason": "state_dir_not_private", "path": str(state)})
            return 1
        ok, message = resolve_proposal(state, args.proposal_id, approve=args.command == "approve", now=clock())
        emit({"proposal_id": args.proposal_id, "ok": ok, "result": message})
        if ok:
            notify(environ, f"[회전매매] 밴드 제안 {args.proposal_id} {'승인 — 다음 실행부터 적용' if message == 'approved' else '거절'}")
        return 0 if ok else 1
    if args.command == "settle":
        return settle(args, environ=environ, clock=clock, emit=emit)
    if args.command == "run-file":
        return run_file(args, environ=environ, clock=clock, emit=emit)
    if args.command == "run" and (args.no_model or args.force_trade_date) and state.resolve() == STATE_DIR.resolve():
        # Rehearsal-only switches: never a default-band or forced-session path in production.
        emit({"status": "HALT", "reason": "rehearsal_options_refused_for_production_state"})
        return 1
    if args.command == "record-fill":
        if not private_dir(state):
            emit({"status": "HALT", "reason": "state_dir_not_private", "path": str(state)})
            return 1
        try:
            ok, record = record_manual_fill(state, args, now=clock())
        except (OSError, ValueError) as error:
            ok, record = False, {"reason": type(error).__name__ + ": " + str(error)[:200]}
        emit({"ok": ok, **record})
        if ok and args.notify:
            book = record["book_after"]
            notify(environ, f"[회전매매 {record['experiment_id']}] 수동 {record['side']} {record['quantity']}주 @{_won(record['price'])} 장부 기록"
                            f" ({record['applied']}) → 실험 재고 {book['quantity']}주, 장부 현금 {_won(book['cash'])}원")
        return 0 if ok else 1
    if args.command == "proposals":
        for proposal_id, record in pending_proposals(state).items():
            emit({"proposal_id": proposal_id, "experiment_id": record["experiment_id"],
                  "changes": record["changes"], "reason": record["reason"], "created_at": record["created_at"]})
        return 0
    return run(args, environ=environ, clock=clock, emit=emit)


if __name__ == "__main__":
    raise SystemExit(main())
