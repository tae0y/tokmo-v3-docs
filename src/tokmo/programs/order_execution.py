"""Shared journal-before-send order execution, with no ambient network."""

import re
from datetime import datetime
from decimal import Decimal

from tokmo.facts.toss_order_commands import (
    CREATE_OPERATIONS,
    encode_order_command,
    execute_order_command,
)
from tokmo.records.order_command_journal import CommandConflict
from tokmo.values.order_commands import ConditionalOrder, OrderCommandResult
from tokmo.values.order_execution import (
    ExecutionPermit,
    aware,
    command_digest,
    command_document,
)


def _saved_result(row):
    status = "UNKNOWN" if row["status"] == "SENDING" else row["status"]
    return OrderCommandResult(status, row["code"], row["order_id"], row["target_id"])


def submit_order_command(
    command,
    *,
    request_key,
    account_scope,
    mode,
    permit,
    journal,
    token_provider,
    account_ref_provider,
    transport,
    now,
    clock,
):
    """Callable from any strategy; never auto-resends, never acquires a token itself."""
    if not isinstance(request_key, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", request_key
    ):
        raise ValueError("request_key")
    try:
        encode_order_command(command)
        digest = command_digest(command)
    except (ValueError, TypeError, ArithmeticError):
        return OrderCommandResult("NOT_SENT", "invalid_request")
    existing = journal.get(request_key)
    if existing is not None:
        if (existing["digest"], existing["account_scope"], existing["mode"]) != (
            digest,
            account_scope,
            mode,
        ):
            raise CommandConflict("request_key_reused")
        return _saved_result(existing)
    if (
        type(permit) is not ExecutionPermit
        or not aware(now)
        or not aware(permit.valid_until)
        or mode not in {"paper", "live"}
        or not permit.plan_version
        or (permit.digest, permit.account_scope, permit.mode)
        != (digest, account_scope, mode)
        or now >= permit.valid_until
    ):
        return OrderCommandResult("NOT_SENT", "permit_invalid")
    if permit.snapshot_at is not None and (
        not aware(permit.snapshot_at)
        or not 0 <= (now - permit.snapshot_at).total_seconds() <= 30
    ):
        return OrderCommandResult("NOT_SENT", "snapshot_expired")
    if (
        isinstance(command.payload, ConditionalOrder)
        and command.payload.expire_date < now.date()
    ):
        return OrderCommandResult("NOT_SENT", "condition_expired")
    if command.operation not in CREATE_OPERATIONS:
        owner = journal.owned_target(
            mode=mode,
            account_scope=account_scope,
            symbol=command.symbol,
            order_id=command.target_id,
        )
        if owner is None or owner["operation"].startswith(
            "CONDITIONAL"
        ) != command.operation.name.startswith("CONDITIONAL"):
            return OrderCommandResult("NOT_SENT", "target_not_owned_or_superseded")
    state, saved = journal.claim(
        request_key=request_key,
        mode=mode,
        account_scope=account_scope,
        symbol=command.symbol,
        operation=command.operation.value,
        target_id=command.target_id,
        client_key=command.payload.client_order_id
        if command.operation in CREATE_OPERATIONS
        else None,
        digest=digest,
        command_json=command_document(command),
        plan_version=permit.plan_version,
        now=now.isoformat(),
        snapshot_at=None
        if permit.snapshot_at is None
        else permit.snapshot_at.isoformat(),
    )
    if state == "EXISTING":
        return _saved_result(saved)
    if state == "BLOCKED":
        return OrderCommandResult("NOT_SENT", "position_unresolved")
    if state == "STALE":
        return OrderCommandResult("NOT_SENT", "snapshot_precedes_reconciliation")
    if state == "TARGET":
        return OrderCommandResult("NOT_SENT", "target_not_owned_or_superseded")
    try:
        token = token_provider()
        account_ref = account_ref_provider(account_scope)
    except Exception:
        result = OrderCommandResult("NOT_SENT", "credentials_unavailable")
    else:
        send_time = clock()
        if (
            not aware(send_time)
            or send_time < now
            or send_time >= permit.valid_until
            or (
                permit.snapshot_at is not None
                and (send_time - permit.snapshot_at).total_seconds() > 30
            )
        ):
            result = OrderCommandResult("NOT_SENT", "permit_expired_before_send")
        else:
            result = execute_order_command(
                command, token=token, account_ref=account_ref, transport=transport
            )
    # Preserve the actual response time so the next account snapshot must be
    # collected after this outcome, including slow network responses.
    finished = clock()
    if not aware(finished) or finished < now:
        finished = now
    journal.settle(request_key, result, now=finished.isoformat())
    return result


class OrderExecutionClient:
    """Reusable assembly with injected authentication and transport."""

    def __init__(
        self,
        *,
        account_scope,
        mode,
        journal,
        token_provider,
        account_ref_provider,
        transport,
        clock,
    ):
        if mode not in {"paper", "live"} or not account_scope:
            raise ValueError("execution_client_identity")
        self.account_scope, self.mode, self.journal = account_scope, mode, journal
        self.token_provider, self.account_ref_provider = (
            token_provider,
            account_ref_provider,
        )
        self.transport, self.clock = transport, clock

    def submit(self, command, *, request_key, permit):
        return submit_order_command(
            command,
            request_key=request_key,
            account_scope=self.account_scope,
            mode=self.mode,
            permit=permit,
            journal=self.journal,
            token_provider=self.token_provider,
            account_ref_provider=self.account_ref_provider,
            transport=self.transport,
            now=self.clock(),
            clock=self.clock,
        )


def reconcile_command(
    *,
    journal,
    request_key,
    account_scope,
    order_id,
    status,
    filled_quantity,
    observed_at,
    group_complete=False,
):
    """Record already-fetched terminal evidence. No inference from ID presence.

    For a conditional group the caller must resolve all linked ordinary orders;
    a single child's FILLED status cannot close an OTO reservation.
    """
    row = journal.get(request_key)
    if (
        row is None
        or not aware(observed_at)
        or observed_at < datetime.fromisoformat(row["created_at"])
    ):
        raise ValueError("terminal_evidence_time")
    quantity = Decimal(filled_quantity)
    if not quantity.is_finite() or quantity < 0:
        raise ValueError("terminal_quantity")
    if row["operation"].startswith("CONDITIONAL"):
        if group_complete is not True or status not in {
            "COMPLETED",
            "EXPIRED",
            "CANCELED",
        }:
            raise ValueError("conditional_terminal_evidence_required")
    elif status not in {"FILLED", "CANCELED", "REJECTED", "REPLACED"}:
        raise ValueError("terminal_evidence_required")
    journal.reconcile(
        request_key,
        account_scope=account_scope,
        order_id=order_id,
        evidence={
            "order_id": order_id,
            "status": status,
            "filled_quantity": filled_quantity,
            "group_complete": group_complete,
        },
        now=observed_at.isoformat(),
    )
