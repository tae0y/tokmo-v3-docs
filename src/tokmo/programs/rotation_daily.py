"""One unattended rotation cycle per instrument per day: settle, decide, submit.

Order of work is fixed. Yesterday's reservation is settled from the broker's
own detail reads before anything new is considered; an unresolved or still
open reservation ends the day without a model call or an order. The model is
then asked once (idempotent per day) and its answer is folded into the user's
band policy, after which the existing reviewed risk gate and journal decide
whether exactly one command goes out. Nothing here retries.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from tokmo.facts.toss_order_commands import encode_order_command
from tokmo.programs.order_execution import reconcile_command
from tokmo.programs.rotation_experiment import run_rotation_step
from tokmo.records.order_command_journal import CommandConflict
from tokmo.values.order_commands import (
    Condition,
    ConditionalOrder,
    OrderCommand,
    OrderOperation,
    RegularOrder,
)
from tokmo.values.order_execution import authorize_order, aware, command_document
from tokmo.values.rotation_policy import RotationPolicy, bound_decision
from tokmo.values.session_gate import SessionKind

REGULAR_SESSION = SessionKind.REGULAR_MARKET.value
OPEN_REGULAR = frozenset(
    {"PENDING", "PARTIAL_FILLED", "PENDING_CANCEL", "PENDING_REPLACE"}
)
TERMINAL_REGULAR = frozenset({"FILLED", "CANCELED", "REJECTED", "REPLACED"})
TERMINAL_CONDITIONAL = frozenset({"COMPLETED", "EXPIRED", "CANCELED"})
POLICY_EVIDENCE_ID = "policy"
# Seconds within the minute during which a run may start: the shared bearer is
# re-issued by the 60-second pulse at :00 and a fresh issue invalidates the
# previous one, so the cycle must sit between two pulse ticks.
DEFAULT_SEND_WINDOW = (12, 40)


@dataclass(frozen=True)
class RotationReads:
    """Broker reads; each returns None (never raises) on any failure.

    ``session_verdict(market, now_utc)`` must come from the durable calendar
    lookahead store, never from a wire: it is consulted before any token.
    """

    order_detail: Callable
    conditional_detail: Callable
    holdings_quantity: Callable
    session_verdict: Callable
    evidence: Callable
    cash_available: Callable = lambda currency: (
        None
    )  # brokerage buying power from the stored pulse capture
    # The provider drops cancelled groups (detail -> 400). The lookup scans the
    # OPEN/CLOSED group lists: a group object, "GONE" when absent from both,
    # None when the lists could not be read.
    conditional_lookup: Callable = lambda symbol, order_id: None
    # (in_regular_session, last_regular_end_utc_or_None) from the calendar store.
    regular_session_state: Callable = lambda market, now: (False, None)
    # Confirmed broker fills for one instrument from the pulse's own store.
    external_fills: Callable = lambda symbol: ()


@dataclass(frozen=True)
class DailyOutcome:
    status: str
    reason: str
    details: dict


def _decimal(value):
    return Decimal(value) if isinstance(value, str) else None


def _apply_child_fill(journal, policy, mode, child, now):
    quantity = _decimal(child.execution.filled_quantity) or Decimal(0)
    if quantity <= 0:
        return None
    amount = _decimal(child.execution.filled_amount)
    average = _decimal(child.execution.average_filled_price)
    if amount is None and average is not None:
        amount = average * quantity
    if amount is None:
        raise ValueError("fill_amount_unavailable")
    costs = (_decimal(child.execution.commission) or Decimal(0)) + (
        _decimal(child.execution.tax) or Decimal(0)
    )
    observed = child.execution.filled_at if aware(child.execution.filled_at) else now
    return journal.apply_experiment_fill(
        policy.experiment_id,
        mode=mode,
        fill_id=child.order_id,
        symbol=policy.symbol,
        side=child.side,
        quantity=quantity,
        amount=amount,
        costs=costs,
        observed_at=observed,
    )


def _settle_row(row, *, journal, policy, reads, now, mode, notes):
    if row["status"] in {"SENDING", "UNKNOWN"}:
        return "HALT", "unresolved_command:" + row["request_key"]
    if row["status"] != "ACCEPTED" or not row["order_id"]:
        return "HALT", "journal_state:" + row["request_key"]
    order_id = row["order_id"]
    if row["operation"].startswith("CONDITIONAL"):
        detail = reads.conditional_detail(order_id)
        if detail is None:
            detail = reads.conditional_lookup(policy.symbol, order_id)
            if detail is None:
                return "HALT", "conditional_read_failed"
            if detail == "GONE":
                # Cancelled by the provider or the owner: nothing left to fill.
                reconcile_command(
                    journal=journal,
                    request_key=row["request_key"],
                    account_scope=policy.account_scope,
                    order_id=order_id,
                    status="CANCELED",
                    filled_quantity="0",
                    observed_at=now,
                    group_complete=True,
                )
                notes.append("group_gone:" + order_id)
                return "CLOSED", "group:GONE"
        filled, pending_children = Decimal(0), 0
        for leg in (detail.first, detail.second):
            child_id = getattr(leg, "triggered_order_id", None)
            if not isinstance(child_id, str):
                continue
            child = reads.order_detail(child_id)
            if child is None:
                return "HALT", "child_read_failed"
            if child.status in OPEN_REGULAR:
                pending_children += 1
                continue
            if child.status not in TERMINAL_REGULAR:
                return "HALT", "child_status:" + str(child.status)
            applied = _apply_child_fill(journal, policy, mode, child, now)
            if applied is not None:
                notes.append("fill:" + child.side + ":" + applied)
                filled += Decimal(child.execution.filled_quantity)
        if pending_children:
            return "OPEN", "triggered_order_pending"
        if detail.status not in TERMINAL_CONDITIONAL:
            return "OPEN", "group:" + str(detail.status)
        reconcile_command(
            journal=journal,
            request_key=row["request_key"],
            account_scope=policy.account_scope,
            order_id=order_id,
            status=detail.status,
            filled_quantity=str(filled),
            observed_at=now,
            group_complete=True,
        )
        return "CLOSED", "group:" + detail.status
    child = reads.order_detail(order_id)
    if child is None:
        return "HALT", "order_read_failed"
    if child.status in OPEN_REGULAR:
        return "OPEN", "order:" + child.status
    if child.status not in TERMINAL_REGULAR:
        return "HALT", "order_status:" + str(child.status)
    applied = _apply_child_fill(journal, policy, mode, child, now)
    if applied is not None:
        notes.append("fill:" + child.side + ":" + applied)
    reconcile_command(
        journal=journal,
        request_key=row["request_key"],
        account_scope=policy.account_scope,
        order_id=order_id,
        status=child.status,
        filled_quantity=child.execution.filled_quantity,
        observed_at=now,
    )
    return "CLOSED", "order:" + child.status


def _reference_price(evidence):
    """The quote's last price when the evidence carries a usable one, else None."""
    quote = evidence.get("quote")
    value = quote.get("last_price") if type(quote) is dict else None
    if not isinstance(value, str):
        return None
    try:
        price = Decimal(value)
    except ArithmeticError:
        return None
    return price if price.is_finite() and price > 0 else None


def settle_open_rows(*, policy, journal, reads, now, mode="live"):
    """Book fills and close finished reservations; no decision, no order.

    The evening/morning settlement job and the pre-execution re-check both use
    this. Returns a plain dict so callers can persist it as a receipt.
    """
    if (
        type(policy) is not RotationPolicy
        or type(reads) is not RotationReads
        or not aware(now)
    ):
        raise ValueError("settlement_inputs")
    notes = []
    result = {
        "experiment_id": policy.experiment_id,
        "symbol": policy.symbol,
        "notes": notes,
    }
    try:
        journal.initialize_experiment(
            policy.experiment_id, mode=mode, envelope=policy.envelope()
        )
    except CommandConflict:
        result.update(
            status="HALT", reason="experiment_reconfiguration_requires_review"
        )
        return result
    remaining = 0
    for row in journal.open_commands(
        mode=mode, account_scope=policy.account_scope, symbol=policy.symbol
    ):
        try:
            state, why = _settle_row(
                row,
                journal=journal,
                policy=policy,
                reads=reads,
                now=now,
                mode=mode,
                notes=notes,
            )
        except (ValueError, ArithmeticError, CommandConflict) as error:
            result.update(status="HALT", reason="settle_failed:" + str(error))
            return result
        notes.append("settle:" + state + ":" + why)
        if state == "HALT":
            result.update(status="HALT", reason=why)
            return result
        if state == "OPEN":
            remaining += 1
    book = journal.experiment_book(policy.experiment_id, mode=mode)
    result.update(
        status="OK",
        reason="settled",
        open_remaining=remaining,
        book={k: str(v) for k, v in book.items()} if book else None,
    )
    return result


def classify_external_fills(*, policy, journal, reads, now, mode="live"):
    """Attribute fills the runner did not place to the owner's protected lot.

    Intent is not in the fill: a one-share sell looks identical whether it closed
    a rotation or trimmed the long-term position. ``record-fill`` is the only way
    to declare a trade as the experiment's, so whatever is still unclaimed when
    settlement runs belongs to the protected lot, and the rail moves with it.

    The move is applied only when the resulting split equals the account's real
    holdings, so a miscount halts instead of silently shifting the rail that
    keeps the automation off the owner's shares. Fills are remembered before the
    rail moves: a crash in between leaves the next settlement refusing to
    reconcile rather than counting the same fill twice.
    """
    if (
        type(policy) is not RotationPolicy
        or type(reads) is not RotationReads
        or not aware(now)
    ):
        raise ValueError("classification_inputs")
    result = {
        "experiment_id": policy.experiment_id,
        "classified": [],
        "protected_before": str(policy.protected_quantity),
    }
    try:
        fills = list(reads.external_fills(policy.symbol) or ())
    except Exception:
        return {**result, "status": "SKIP", "reason": "fill_feed_unreadable"}
    watermark = journal.experiment_watermark(
        policy.experiment_id,
        mode=mode,
        account_scope=policy.account_scope,
        symbol=policy.symbol,
    )
    if watermark is None:
        return {**result, "status": "SKIP", "reason": "experiment_has_no_history"}
    known = journal.known_order_ids(
        mode=mode, account_scope=policy.account_scope, symbol=policy.symbol
    )
    fresh, delta = [], Decimal(0)
    for fill in fills:
        fill_id = fill["fill_id"]
        if fill["filled_at"].isoformat() < watermark or fill_id in known:
            continue
        if (
            journal.experiment_fill(
                policy.experiment_id, mode=mode, fill_id="manual-" + fill_id
            )
            is not None
        ):
            continue  # the owner declared it as the experiment's
        if (
            journal.protected_fill(policy.experiment_id, mode=mode, fill_id=fill_id)
            is not None
        ):
            continue  # already attributed on an earlier day
        fresh.append(fill)
        delta += fill["quantity"] if fill["side"] == "BUY" else -fill["quantity"]
    if not fresh:
        return {
            **result,
            "status": "NONE",
            "reason": "no_unclaimed_fills",
            "protected_after": str(policy.protected_quantity),
        }
    revised = policy.protected_quantity + delta
    summary = [
        {
            "fill_id": f["fill_id"],
            "side": f["side"],
            "quantity": str(f["quantity"]),
            "price": str(f["price"]),
            "filled_at": f["filled_at"].isoformat(),
        }
        for f in fresh
    ]
    result["classified"] = summary
    if revised < 0:
        return {
            **result,
            "status": "HALT",
            "reason": "protected_quantity_negative",
            "protected_after": str(revised),
        }
    book = journal.experiment_book(policy.experiment_id, mode=mode)
    experiment = book["quantity"] if book else Decimal(0)
    holdings = reads.holdings_quantity(policy.symbol)
    if holdings is None:
        return {
            **result,
            "status": "HALT",
            "reason": "holdings_unavailable",
            "protected_after": str(revised),
        }
    if Decimal(holdings) != revised + experiment:
        return {
            **result,
            "status": "HALT",
            "reason": "holdings_disagree_with_classification",
            "protected_after": str(revised),
            "holdings": str(holdings),
            "experiment_quantity": str(experiment),
        }
    for fill in fresh:
        journal.record_protected_fill(
            policy.experiment_id,
            mode=mode,
            fill_id=fill["fill_id"],
            fill={k: str(v) for k, v in fill.items()},
            recorded_at=now,
        )
    envelope = replace(policy.envelope(), protected_quantity=revised)
    journal.reconfigure_protected_quantity(
        policy.experiment_id, mode=mode, envelope=envelope
    )
    return {
        **result,
        "status": "APPLIED",
        "reason": "protected_quantity_moved",
        "protected_after": str(revised),
        "holdings": str(holdings),
        "experiment_quantity": str(experiment),
    }


def settlement_due(*, market, now, reads, margin):
    """True when no regular session is running and the last close is at least ``margin`` ago."""
    in_regular, last_end = reads.regular_session_state(market, now)
    if in_regular:
        return False, "regular_session_in_progress"
    if last_end is not None and now < last_end + margin:
        return False, "close_margin_not_elapsed"
    return True, "due"


def _pending(rows):
    buy = sell = Decimal(0)
    for row in rows:
        if row["status"] != "ACCEPTED":
            continue
        payload = json.loads(row["command_json"])["payload"]
        quantity = Decimal(payload["quantity"])
        if row["operation"].startswith("CONDITIONAL") or payload.get("side") == "BUY":
            buy += quantity
        else:
            sell += quantity
    return buy, sell


def run_daily(
    *,
    policy,
    journal,
    reads,
    model_transport,
    model_id,
    clock,
    client,
    mode="live",
    send_window=DEFAULT_SEND_WINDOW,
):
    """Settle, decide and submit for one instrument. Never raises for broker state."""
    if type(policy) is not RotationPolicy or type(reads) is not RotationReads:
        raise ValueError("rotation_inputs")
    now = clock()
    if not aware(now):
        raise ValueError("rotation_clock")
    notes = []
    detail = {
        "experiment_id": policy.experiment_id,
        "symbol": policy.symbol,
        "notes": notes,
    }
    # This fallback path decides one full lot at a time. Accumulating a
    # multi-share lot needs the analysis handoff, which re-checks the share
    # count against the book; refuse loudly rather than half-support it.
    if policy.quantity != 1:
        return DailyOutcome("HALT", "model_path_supports_one_share_only", detail)
    if now >= policy.valid_until:
        return DailyOutcome("SKIP", "experiment_expired", detail)
    # Every broker read below needs the bearer, so the window guard comes first.
    if not (send_window[0] <= now.second <= send_window[1]):
        return DailyOutcome("HALT", "outside_send_window", detail)
    # Session gate from the cadence-collected calendar store: no token, no
    # wire. Outside the market's regular session nothing below runs at all.
    verdict = reads.session_verdict(policy.market, now)
    if verdict is None:
        return DailyOutcome("SKIP", "calendar_unknown", detail)
    detail["session"] = {
        "open": verdict.open,
        "reason": verdict.reason,
        "trade_date": verdict.trade_date,
        "kind": verdict.session_kind,
    }
    if not verdict.open:
        return DailyOutcome("SKIP", "market_closed:" + verdict.reason, detail)
    if verdict.session_kind != REGULAR_SESSION:
        return DailyOutcome(
            "SKIP", "outside_regular_session:" + str(verdict.session_kind), detail
        )
    today = date.fromisoformat(verdict.trade_date)
    detail["day"] = today.isoformat()
    try:
        journal.initialize_experiment(
            policy.experiment_id, mode=mode, envelope=policy.envelope()
        )
    except CommandConflict:
        return DailyOutcome(
            "HALT", "experiment_reconfiguration_requires_review", detail
        )
    for row in journal.open_commands(
        mode=mode, account_scope=policy.account_scope, symbol=policy.symbol
    ):
        try:
            state, why = _settle_row(
                row,
                journal=journal,
                policy=policy,
                reads=reads,
                now=now,
                mode=mode,
                notes=notes,
            )
        except (ValueError, ArithmeticError, CommandConflict) as error:
            return DailyOutcome("HALT", "settle_failed:" + str(error), detail)
        notes.append("settle:" + state + ":" + why)
        if state == "HALT":
            return DailyOutcome("HALT", why, detail)
        if state == "OPEN":
            return DailyOutcome("SKIP", "standing_order_open", detail)
    book = journal.experiment_book(policy.experiment_id, mode=mode)
    if book is None or book["frozen"]:
        return DailyOutcome("HALT", "experiment_frozen", detail)
    holding = book["quantity"] >= policy.quantity
    detail["holding"] = holding
    detail["experiment_cash"] = str(book["cash"])
    cost_basis = (
        journal.experiment_lot_cost(policy.experiment_id, mode=mode)
        if holding
        else None
    )
    if cost_basis is not None:
        detail["cost_basis"] = str(cost_basis)
    # One decision per instrument per day. A rerun (manual retry, double
    # launch) must neither call the model nor read the broker again.
    decision_id = today.strftime("%Y%m%d")
    decision_key = hashlib.sha256(
        (policy.experiment_id + ":" + decision_id).encode()
    ).hexdigest()
    if journal.decision(decision_key) is not None:
        detail["decision_key"] = decision_key
        return DailyOutcome("EXISTING", "decision_already_recorded", detail)

    def snapshot_provider():
        total = reads.holdings_quantity(policy.symbol)
        if total is None:
            raise ValueError("holdings_unavailable")
        pending_buy, pending_sell = _pending(
            journal.open_commands(
                mode=mode, account_scope=policy.account_scope, symbol=policy.symbol
            )
        )
        return journal.experiment_snapshot(
            policy.experiment_id,
            mode=mode,
            total_quantity=total,
            pending_buy=pending_buy,
            pending_sell=pending_sell,
            fee_buffer=policy.fee_buffer,
            observed_at=clock(),
        )

    def bounded_transport(request):
        try:
            raw = model_transport(request)
        except Exception:
            raw = None
        decided, note, proposal = bound_decision(
            raw,
            policy=policy,
            today=today,
            holding=holding,
            fallback_evidence_id=POLICY_EVIDENCE_ID,
            evidence_ids=set(evidence),
            cost_basis=cost_basis,
        )
        notes.append("model:" + note)
        decision = json.loads(decided)
        if decision["action"] == "ENTER":
            # The risk gate budgets experiment cash; the broker needs real
            # cash. A buy the account cannot fund becomes a recorded HOLD.
            currency = "KRW" if policy.market == "KR" else "USD"
            cash = reads.cash_available(currency)
            need = Decimal(decision["buy_price"]) * policy.quantity + policy.fee_buffer
            if cash is None:
                notes.append("cash:unknown")
            else:
                detail["cash_available"] = str(cash)
                if cash < need:
                    notes.append("cash:insufficient")
                    decision = {
                        "symbol": policy.symbol,
                        "action": "HOLD",
                        "buy_price": None,
                        "sell_price": None,
                        "expires_on": None,
                        "evidence_ids": [POLICY_EVIDENCE_ID],
                        "reason": f"예수금 부족: 주문가능 {cash:,.0f}{currency} < 필요 {need:,.0f}{currency} (매수 {decision['buy_price']} + 수수료 여유)",
                    }
                    decided = json.dumps(decision, ensure_ascii=False)
        detail["decision"] = decision
        if proposal is not None:
            detail["band_proposal"] = proposal
        return decided

    evidence = reads.evidence(policy)
    if type(evidence) is not dict:
        return DailyOutcome("HALT", "evidence_unavailable", detail)
    evidence = dict(evidence)
    evidence[POLICY_EVIDENCE_ID] = {"as_of": now.isoformat(), **policy.document()}
    reference_price = _reference_price(evidence)
    try:
        result = run_rotation_step(
            experiment_id=policy.experiment_id,
            decision_id=decision_id,
            envelope=policy.envelope(),
            evidence=evidence,
            model_id=model_id,
            model_transport=bounded_transport,
            snapshot_provider=snapshot_provider,
            journal=journal,
            clock=clock,
            client=client,
            policy=policy.document(),
            reference_price=reference_price,
        )
    except (ValueError, ArithmeticError, CommandConflict) as error:
        return DailyOutcome("HALT", "step_failed:" + str(error), detail)
    detail["decision_key"] = result.decision_key
    if result.command is not None:
        detail["command"] = json.loads(command_document(result.command))
        if (
            result.status != "HOLD"
            and detail.get("decision", {}).get("action") == "ENTER"
            and "kind" not in detail["command"]["payload"]
        ):
            notes.append("entry:limit_buy_trigger_already_met")
    if result.execution_status is not None:
        detail["execution_status"] = result.execution_status
        row = journal.get("rotation-" + result.decision_key)
        if row is not None:
            detail["execution_code"] = row["code"]
            detail["order_id"] = row["order_id"]
    return DailyOutcome(result.status, result.reason, detail)


def _record_day(journal, *, decision_key, policy, producer, status, payload, now):
    """Pin the day's final outcome so a rerun can never reopen it (one decision per day)."""
    document = json.dumps(
        {
            "producer": producer,
            "trade_date": payload.get("trade_date"),
            "status": status,
            "decision": payload.get("decision"),
            "reason": payload.get("reason"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    if journal.begin_decision(
        decision_key=decision_key,
        experiment_id=policy.experiment_id,
        model_id=producer,
        input_json=document,
        now=now.isoformat(),
    ):
        journal.finish_decision(
            decision_key, status=status, output_json=document, now=now.isoformat()
        )


def _wait_for_window(*, clock, sleeper, send_window, deadline):
    """Sleep until the seconds-of-minute window; False if the deadline arrives first."""
    while True:
        now = clock()
        if now >= deadline:
            return False
        second = now.second
        if send_window[0] <= second <= send_window[1]:
            return True
        wait = (
            (60 - second + send_window[0])
            if second > send_window[1]
            else (send_window[0] - second)
        )
        sleeper(min(wait, max(0, (deadline - now).total_seconds())))


def run_from_decision_file(
    *,
    policy,
    journal,
    reads,
    decision_reader,
    approvals_reader,
    clock,
    sleeper,
    client,
    deadline,
    poll_seconds=15,
    mode="live",
    send_window=DEFAULT_SEND_WINDOW,
):
    """v0.4 handoff: consume the analysis file, never analyse, never substitute.

    Waits from the start until the file is consumable or ``deadline`` (10:40
    KST); SKIPPED/FAILED/EXPIRED/invalid end the day at once. A same-day approval
    from the runner's own proposals store is the only way an out-of-band
    price is accepted. Broker credentials are used only after the decision is
    accepted: re-settle, fresh holdings, authorization, one submit.
    """
    from tokmo.values.rotation_decision_file import (
        DecisionFileError,
        parse_decision_file,
        today_approval_matches,
    )

    if type(policy) is not RotationPolicy or type(reads) is not RotationReads:
        raise ValueError("rotation_inputs")
    now = clock()
    if not aware(now) or not aware(deadline):
        raise ValueError("rotation_clock")
    notes = []
    detail = {
        "experiment_id": policy.experiment_id,
        "symbol": policy.symbol,
        "notes": notes,
        "producer": None,
    }
    if now >= policy.valid_until:
        return DailyOutcome("SKIP", "experiment_expired", detail)
    verdict = reads.session_verdict(policy.market, now)
    if verdict is None:
        return DailyOutcome("SKIP", "calendar_unknown", detail)
    detail["session"] = {
        "open": verdict.open,
        "reason": verdict.reason,
        "trade_date": verdict.trade_date,
        "kind": verdict.session_kind,
    }
    if not verdict.open or verdict.session_kind != REGULAR_SESSION:
        return DailyOutcome(
            "SKIP",
            "market_closed:"
            + str(verdict.reason if not verdict.open else verdict.session_kind),
            detail,
        )
    today = date.fromisoformat(verdict.trade_date)
    detail["day"] = today.isoformat()
    decision_id = today.strftime("%Y%m%d")
    decision_key = hashlib.sha256(
        (policy.experiment_id + ":" + decision_id).encode()
    ).hexdigest()
    detail["decision_key"] = decision_key
    if journal.decision(decision_key) is not None:
        return DailyOutcome("EXISTING", "decision_already_recorded", detail)

    def end_day(status, reason, producer="handoff", payload=None):
        _record_day(
            journal,
            decision_key=decision_key,
            policy=policy,
            producer=producer,
            status=status,
            payload={
                "trade_date": today.isoformat(),
                "reason": reason,
                **(payload or {}),
            },
            now=clock(),
        )
        detail["outcome_recorded"] = status
        return DailyOutcome(status, reason, detail)

    # The sell floor derives from the held lot's actual fill price (journal only).
    book_now = journal.experiment_book(policy.experiment_id, mode=mode)
    held_now = book_now is not None and book_now["quantity"] > 0
    cost_basis = (
        journal.experiment_lot_cost(policy.experiment_id, mode=mode)
        if held_now
        else None
    )
    if cost_basis is not None:
        detail["cost_basis"] = str(cost_basis)
        detail["sell_floor"] = str(policy.sell_floor_for(cost_basis))

    # --- wait for a consumable file (no broker, no model) --------------------
    document = None
    while True:
        now = clock()
        raw = decision_reader()
        if raw is not None:
            try:
                document = parse_decision_file(
                    raw, policy=policy, now=now, trade_date=today, cost_basis=cost_basis
                )
            except DecisionFileError as error:
                notes.append("decision_file:" + error.code)
                if error.code in {"valid_until_passed", "stale"}:
                    reason = (
                        "approval_timeout"
                        if document is not None
                        and document.status == "WAITING_APPROVAL"
                        else "decision_file:" + error.code
                    )
                    return end_day(
                        "EXPIRED",
                        reason,
                        producer=document.producer if document else "handoff",
                    )
                return end_day("FAILED", "decision_file:invalid:" + error.code)
            detail["producer"], detail["analysis_id"] = (
                document.producer,
                document.analysis_id,
            )
            detail["analysis_summary"] = document.analysis_summary
            if document.status == "SKIPPED":
                return end_day(
                    "SKIP",
                    "analysis_skipped:market_holiday",
                    producer=document.producer,
                )
            if document.status in {"FAILED", "EXPIRED"}:
                return end_day(
                    document.status,
                    "analysis_"
                    + document.status.lower()
                    + ":"
                    + str(document.failure_code or document.failure_reason or ""),
                    producer=document.producer,
                )
            if document.status == "COMPLETE":
                if document.in_band:
                    break
                if document.in_band is None:
                    # EXIT without a known fill price: a held lot cannot be
                    # priced, an unheld one fails the inventory check below.
                    if held_now:
                        return end_day(
                            "FAILED", "cost_basis_unknown", producer=document.producer
                        )
                    break
                approval = next(
                    (
                        r
                        for r in approvals_reader()
                        if today_approval_matches(
                            r,
                            decision=document.decision,
                            policy=policy,
                            trade_date=today,
                        )
                    ),
                    None,
                )
                if approval is not None:
                    detail["approval"] = {
                        k: approval.get(k)
                        for k in ("proposal_id", "approved_at", "revision")
                    }
                    break
                return end_day(
                    "FAILED",
                    "out_of_band_without_today_approval",
                    producer=document.producer,
                )
            if document.status == "WAITING_APPROVAL":
                if document.band_proposal is not None:
                    detail["band_proposal"] = document.band_proposal
                approval = next(
                    (
                        r
                        for r in approvals_reader()
                        if today_approval_matches(
                            r,
                            decision=document.decision,
                            policy=policy,
                            trade_date=today,
                        )
                    ),
                    None,
                )
                if approval is not None:
                    detail["approval"] = {
                        k: approval.get(k)
                        for k in ("proposal_id", "approved_at", "revision")
                    }
                    break
        if now >= deadline:
            reason = (
                "approval_timeout"
                if document is not None and document.status == "WAITING_APPROVAL"
                else "analysis_timeout"
                if document is not None
                else "decision_file:absent"
            )
            return end_day(
                "EXPIRED", reason, producer=document.producer if document else "handoff"
            )
        sleeper(min(poll_seconds, max(0, (deadline - now).total_seconds())))

    decision = document.decision
    detail["decision"] = decision
    if decision["action"] == "HOLD":
        return end_day(
            "HOLD",
            decision["reason"],
            producer=document.producer,
            payload={"decision": decision},
        )

    # --- broker phase: inside the token-safe window, before the deadline ----
    if not _wait_for_window(
        clock=clock, sleeper=sleeper, send_window=send_window, deadline=deadline
    ):
        return end_day(
            "EXPIRED", "deadline_before_send_window", producer=document.producer
        )
    now = clock()
    settled = settle_open_rows(
        policy=policy, journal=journal, reads=reads, now=now, mode=mode
    )
    notes.extend("presend:" + n for n in settled["notes"])
    if settled["status"] != "OK":
        return end_day("HALT", settled["reason"], producer=document.producer)
    if settled["open_remaining"]:
        return end_day("SKIP", "standing_order_open", producer=document.producer)
    book = journal.experiment_book(policy.experiment_id, mode=mode)
    if book is None or book["frozen"]:
        return end_day("HALT", "experiment_frozen", producer=document.producer)
    # A multi-share experiment fills over several days: what it may do today is
    # decided by how much of the capacity is still empty, not by a yes/no.
    held = book["quantity"]
    remaining = policy.quantity - held
    holding = remaining <= 0
    detail["holding"], detail["experiment_cash"] = holding, str(book["cash"])
    detail["held"], detail["remaining"] = str(held), str(remaining)
    if (decision["action"] == "ENTER") == holding:
        return end_day(
            "FAILED",
            f"inventory_mismatch:action={decision['action']},held={held}",
            producer=document.producer,
        )
    # The file may name any part of the capacity; only here is the book known.
    wanted = remaining if decision["action"] == "ENTER" else held
    if Decimal(decision["quantity"]) != wanted:
        return end_day(
            "FAILED",
            f"quantity_mismatch:file={decision['quantity']},expected={wanted}",
            producer=document.producer,
        )
    if decision["action"] == "EXIT":
        # The pre-send settlement may have booked the lot just now; judge the
        # sell floor against the actual fill price it recorded.
        basis = journal.experiment_lot_cost(policy.experiment_id, mode=mode)
        if basis is None:
            return end_day("FAILED", "cost_basis_unknown", producer=document.producer)
        floor = policy.sell_floor_for(basis)
        detail["cost_basis"], detail["sell_floor"] = str(basis), str(floor)
        if Decimal(decision["sell_price"]) < floor and "approval" not in detail:
            return end_day(
                "FAILED",
                f"sell_below_minimum:{decision['sell_price']}<{floor}",
                producer=document.producer,
            )
    if decision["action"] == "ENTER":
        currency = "KRW" if policy.market == "KR" else "USD"
        cash = reads.cash_available(currency)
        need = Decimal(decision["buy_price"]) * wanted + policy.fee_buffer
        if cash is None:
            notes.append("cash:unknown")
        else:
            detail["cash_available"] = str(cash)
            if cash < need:
                return end_day(
                    "HOLD",
                    f"예수금 부족: 주문가능 {cash:,.0f}{currency} < 필요 {need:,.0f}{currency}",
                    producer=document.producer,
                )
    evidence = reads.evidence(policy)
    reference = _reference_price(evidence if type(evidence) is dict else {})
    if reference is not None:
        detail["reference_price"] = str(reference)
    key = "rot-" + decision_key[:32]
    quantity = str(wanted)
    # Topping up an existing lot buys only. Pairing a sell leg with it would
    # round-trip the new share instead of averaging the lot down, and the exit
    # belongs to the whole averaged lot on a later day.
    top_up = decision["action"] == "ENTER" and held > 0
    if decision["action"] == "ENTER" and (
        top_up
        or (reference is not None and reference <= Decimal(decision["buy_price"]))
    ):
        command = OrderCommand(
            OrderOperation.CREATE,
            policy.market,
            policy.symbol,
            RegularOrder(
                "BUY",
                "LIMIT",
                quantity=quantity,
                price=decision["buy_price"],
                client_order_id=key,
            ),
        )
        notes.append(
            "entry:top_up_limit_buy"
            if top_up
            else "entry:limit_buy_trigger_already_met"
        )
    elif decision["action"] == "ENTER":
        command = OrderCommand(
            OrderOperation.CONDITIONAL_CREATE,
            policy.market,
            policy.symbol,
            ConditionalOrder(
                "OTO",
                quantity,
                "LIMIT",
                today,
                Condition("BUY", decision["buy_price"], decision["buy_price"]),
                Condition("SELL", decision["sell_price"], decision["sell_price"]),
                key,
            ),
        )
    else:
        command = OrderCommand(
            OrderOperation.CREATE,
            policy.market,
            policy.symbol,
            RegularOrder(
                "SELL",
                "LIMIT",
                quantity=quantity,
                price=decision["sell_price"],
                client_order_id=key,
            ),
        )
    _record_day(
        journal,
        decision_key=decision_key,
        policy=policy,
        producer=document.producer,
        status="DECIDED",
        payload={
            "trade_date": today.isoformat(),
            "decision": decision,
            "reason": decision["reason"],
        },
        now=clock(),
    )
    detail["outcome_recorded"] = "DECIDED"
    try:
        encode_order_command(command)
        total = reads.holdings_quantity(policy.symbol)
        if total is None:
            raise ValueError("holdings_unavailable")
        snapshot = journal.experiment_snapshot(
            policy.experiment_id,
            mode=mode,
            total_quantity=total,
            pending_buy=Decimal(0),
            pending_sell=Decimal(0),
            fee_buffer=policy.fee_buffer,
            observed_at=clock(),
        )
        permit = authorize_order(
            command, policy.envelope(), snapshot, now=clock(), mode=mode
        )
    except (ValueError, TypeError, ArithmeticError) as error:
        detail["command"] = json.loads(command_document(command))
        return DailyOutcome("BLOCKED", "risk_check_rejected:" + str(error), detail)
    detail["command"] = json.loads(command_document(command))
    if clock() >= deadline:
        return DailyOutcome("EXPIRED", "deadline_before_submit", detail)
    outcome = client.submit(
        command, request_key="rotation-" + decision_key, permit=permit
    )
    detail["execution_status"], detail["execution_code"], detail["order_id"] = (
        outcome.status,
        outcome.code,
        outcome.order_id,
    )
    return DailyOutcome("SUBMITTED", outcome.code, detail)


@dataclass(frozen=True)
class RotationWiring:
    """What composition hands the daily runner: shared journal, client, reads."""

    journal: object
    client: object
    reads: RotationReads
