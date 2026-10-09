"""Durable command deduplication and one-position limits, entirely offline."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_order_commands import NOW, buy, oto

from tokmo.programs.order_execution import reconcile_command, submit_order_command
from tokmo.records.order_command_journal import CommandConflict, OrderCommandJournal
from tokmo.values.order_commands import (
    CancelOrder,
    OrderCommand,
    OrderOperation,
    OrderWireResponse,
    ReplaceOrder,
)
from tokmo.values.order_execution import (
    ExecutionPermit,
    RiskEnvelope,
    RiskSnapshot,
    authorize_order,
    command_digest,
)


def journal(tmp_path):
    return OrderCommandJournal(tmp_path / "private" / "orders.sqlite3")


def permit(cmd, mode="live"):
    return ExecutionPermit(
        "personal-test",
        command_digest(cmd),
        mode,
        NOW + timedelta(minutes=1),
        "plan-v1",
    )


def submit(store, cmd=None, key="request-1", handler=None, at=NOW):
    cmd = buy() if cmd is None else cmd

    def transport(req):
        assert store.get(key)["status"] == "SENDING"
        if handler is not None:
            return handler(req)
        return OrderWireResponse(
            200, json.dumps({"result": {"orderId": "broker-1"}}).encode()
        )

    return submit_order_command(
        cmd,
        request_key=key,
        account_scope="personal-test",
        mode="live",
        permit=permit(cmd),
        journal=store,
        token_provider=lambda: "token",
        account_ref_provider=lambda scope: "123",
        transport=transport,
        now=at,
        clock=lambda: at,
    )


def test_submit_and_restart_never_resends(tmp_path):
    store = journal(tmp_path)
    assert submit(store).status == "ACCEPTED"
    reopened = OrderCommandJournal(store.path)
    assert (
        submit(reopened, handler=lambda r: pytest.fail("resent")).status == "ACCEPTED"
    )
    # Dedupe remains local after the broker's ten-minute idempotency window.
    assert reopened.get("request-1")["status"] == "ACCEPTED"
    assert "token" not in store.path.read_bytes().decode("latin1")


def test_different_requests_cannot_share_key_or_create_second_position(tmp_path):
    store = journal(tmp_path)
    submit(store)
    with pytest.raises(CommandConflict):
        submit(
            store, cmd=replace(buy(), payload=replace(buy().payload, price="370000"))
        )
    assert (
        submit(store, cmd=buy("another-key"), key="request-2").code
        == "position_unresolved"
    )


def test_uncertain_request_survives_restart_and_blocks_new_keys(tmp_path):
    store = journal(tmp_path)

    def timeout(req):
        raise TimeoutError()

    assert submit(store, handler=timeout).status == "UNKNOWN"
    assert (
        submit(
            OrderCommandJournal(store.path), handler=lambda r: pytest.fail("resent")
        ).status
        == "UNKNOWN"
    )
    assert submit(store, buy("new-key"), "request-2").status == "NOT_SENT"


def test_mismatched_permit_expires_or_changes_command_without_io(tmp_path):
    store = journal(tmp_path)
    cmd = buy()
    kwargs = dict(
        request_key="one",
        account_scope="personal-test",
        mode="live",
        journal=store,
        token_provider=lambda: pytest.fail("credentials before authorization"),
        account_ref_provider=lambda s: pytest.fail("account before authorization"),
        transport=lambda r: pytest.fail("wire before authorization"),
        now=NOW,
        clock=lambda: NOW,
    )
    for invalid in (
        replace(permit(cmd), mode="paper"),
        replace(permit(cmd), account_scope="other"),
        replace(permit(cmd), valid_until=NOW),
        replace(permit(cmd), digest="wrong"),
    ):
        assert submit_order_command(cmd, permit=invalid, **kwargs).status == "NOT_SENT"
    assert store.get("one") is None


def test_concurrent_claim_is_single_writer(tmp_path):
    store = journal(tmp_path)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: submit(store), range(2)))
    assert any(r.status == "ACCEPTED" for r in results)
    assert store.count() == 1


def test_reconciliation_requires_linked_final_evidence(tmp_path):
    store = journal(tmp_path)
    submit(store)
    with pytest.raises(ValueError):
        reconcile_command(
            journal=store,
            request_key="request-1",
            account_scope="personal-test",
            order_id="wrong",
            status="FILLED",
            filled_quantity="1",
            observed_at=NOW,
        )
    reconcile_command(
        journal=store,
        request_key="request-1",
        account_scope="personal-test",
        order_id="broker-1",
        status="FILLED",
        filled_quantity="1",
        observed_at=NOW,
    )
    assert store.get("request-1")["lifecycle"] == "TERMINAL"


def limits():
    return RiskEnvelope(
        "personal-test",
        "005380",
        "KR",
        Decimal(1),
        Decimal(400000),
        Decimal(7),
        NOW + timedelta(days=10),
        "experiment-v1",
    )


def snapshot(**changes):
    base = RiskSnapshot(
        NOW,
        Decimal(7),
        Decimal(0),
        Decimal(0),
        Decimal(0),
        Decimal(400000),
        Decimal(1000),
        False,
        "personal-test",
        "005380",
        "KR",
        Decimal(400000),
    )
    return replace(base, **changes)


def test_limit_is_position_plus_pending_not_quantity_per_order():
    assert authorize_order(
        oto(), limits(), snapshot(), now=NOW, mode="live"
    ).digest == command_digest(oto())
    for snap in (
        snapshot(pending_buy=Decimal(1)),
        snapshot(experiment_quantity=Decimal(1), total_quantity=Decimal(8)),
        snapshot(unresolved=True),
        snapshot(total_quantity=Decimal(6)),
        snapshot(observed_at=NOW - timedelta(minutes=2)),
        snapshot(available_cash=Decimal(370000)),
        snapshot(funded_amount=Decimal(900000)),
        snapshot(account_scope="other"),
    ):
        with pytest.raises(ValueError):
            authorize_order(buy(), limits(), snap, now=NOW, mode="live")


def test_budget_includes_costs_and_market_buys_have_no_bounded_price():
    with pytest.raises(ValueError):
        authorize_order(
            replace(buy(), payload=replace(buy().payload, price="400000")),
            limits(),
            snapshot(),
            now=NOW,
            mode="live",
        )
    with pytest.raises(ValueError):
        authorize_order(
            replace(
                buy(), payload=replace(buy().payload, order_type="MARKET", price=None)
            ),
            limits(),
            snapshot(),
            now=NOW,
            mode="live",
        )


def test_existing_seven_shares_are_not_sellable_by_experiment():
    cmd = replace(buy(), payload=replace(buy().payload, side="SELL", price="390000"))
    with pytest.raises(ValueError):
        authorize_order(cmd, limits(), snapshot(), now=NOW, mode="live")
    held = snapshot(
        experiment_quantity=Decimal(1),
        total_quantity=Decimal(8),
        available_cash=Decimal(19000),
    )
    assert authorize_order(
        cmd, limits(), held, now=NOW, mode="live"
    ).digest == command_digest(cmd)
    with pytest.raises(ValueError):
        authorize_order(
            cmd, limits(), replace(held, pending_sell=Decimal(1)), now=NOW, mode="live"
        )


def test_credentials_delay_cannot_outlive_permission(tmp_path):
    store = journal(tmp_path)
    result = submit_order_command(
        buy(),
        request_key="delayed",
        account_scope="personal-test",
        mode="live",
        permit=permit(buy()),
        journal=store,
        token_provider=lambda: "token",
        account_ref_provider=lambda s: "123",
        transport=lambda r: pytest.fail("expired permission sent"),
        now=NOW,
        clock=lambda: NOW + timedelta(minutes=2),
    )
    assert result.code == "permit_expired_before_send"
    assert store.get("delayed")["status"] == "NOT_SENT"


def test_new_order_requires_snapshot_collected_after_reconciliation(tmp_path):
    store = journal(tmp_path)
    submit(store)
    later = NOW + timedelta(seconds=5)
    reconcile_command(
        journal=store,
        request_key="request-1",
        account_scope="personal-test",
        order_id="broker-1",
        status="CANCELED",
        filled_quantity="0",
        observed_at=later,
    )
    command = buy("new-client-key")
    stale_permit = authorize_order(
        command, limits(), snapshot(), now=later, mode="live"
    )
    result = submit_order_command(
        command,
        request_key="new-request",
        account_scope="personal-test",
        mode="live",
        permit=stale_permit,
        journal=store,
        token_provider=lambda: pytest.fail("stale account state"),
        account_ref_provider=lambda s: "123",
        transport=lambda r: pytest.fail("stale state sent"),
        now=later,
        clock=lambda: later,
    )
    assert result.code == "snapshot_precedes_reconciliation"


def test_filled_oto_buy_leg_does_not_free_the_reservation(tmp_path):
    store = journal(tmp_path)
    submit(
        store,
        oto(),
        handler=lambda r: OrderWireResponse(
            200, b'{"result":{"conditionalOrderId":"group-1"}}'
        ),
    )
    with pytest.raises(ValueError, match="conditional_terminal"):
        reconcile_command(
            journal=store,
            request_key="request-1",
            account_scope="personal-test",
            order_id="group-1",
            status="FILLED",
            filled_quantity="1",
            observed_at=NOW,
        )
    assert store.get("request-1")["lifecycle"] == "OPEN"


def test_only_owned_latest_order_id_can_be_modified_or_canceled(tmp_path):
    store = journal(tmp_path)
    external = OrderCommand(
        OrderOperation.CANCEL, "KR", "005380", CancelOrder(), "external-id"
    )
    assert submit(store, external).code == "target_not_owned_or_superseded"
    submit(store)
    change = OrderCommand(
        OrderOperation.MODIFY,
        "KR",
        "005380",
        ReplaceOrder("LIMIT", quantity="1", price="370000"),
        "broker-1",
    )
    assert (
        submit(
            store,
            change,
            "modify-1",
            handler=lambda r: OrderWireResponse(
                200, b'{"result":{"orderId":"broker-2"}}'
            ),
        ).order_id
        == "broker-2"
    )
    old_cancel = replace(external, target_id="broker-1")
    assert (
        submit(store, old_cancel, "old-cancel").code == "target_not_owned_or_superseded"
    )
    new_cancel = replace(external, target_id="broker-2")
    assert (
        submit(
            store,
            new_cancel,
            "new-cancel",
            handler=lambda r: OrderWireResponse(
                200, b'{"result":{"orderId":"cancel-3"}}'
            ),
        ).status
        == "ACCEPTED"
    )


def test_expiration_date_is_not_an_intraday_permission_cutoff():
    end = NOW.replace(day=18, hour=1)
    with pytest.raises(ValueError, match="condition_beyond_envelope"):
        authorize_order(
            oto(), replace(limits(), valid_until=end), snapshot(), now=NOW, mode="live"
        )
