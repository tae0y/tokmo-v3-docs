"""Offline fixture demonstration. No broker, model, credentials or live option.

Run from the project root: PYTHONPATH=src .venv/bin/python tools/demo_order_experiment.py
The prices and fills below are invented test fixtures, not a recommendation,
a market simulation, or evidence of model performance.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from tokmo.programs.order_execution import OrderExecutionClient, reconcile_command
from tokmo.programs.rotation_experiment import run_rotation_step
from tokmo.records.order_command_journal import OrderCommandJournal
from tokmo.values.order_commands import OrderWireResponse
from tokmo.values.order_execution import RiskEnvelope


def main():
    at = datetime(2026, 9, 11, 1, tzinfo=timezone.utc)
    envelope = RiskEnvelope("offline-fixture", "005380", "KR", Decimal(1), Decimal(400000),
                            Decimal(7), at + timedelta(days=10), "fixture-v1")
    with TemporaryDirectory(prefix="tokmo-order-demo-") as directory:
        # macOS /var is an alias for /private/var; resolve our own temp root.
        store = OrderCommandJournal(Path(directory).resolve() / "private" / "orders.sqlite3")
        store.initialize_experiment("demo", mode="paper", envelope=envelope)
        def snapshot():
            return store.experiment_snapshot("demo", mode="paper", total_quantity=Decimal(7),
                pending_buy=Decimal(0), pending_sell=Decimal(0), fee_buffer=Decimal(1000), observed_at=at)
        client = OrderExecutionClient(account_scope="offline-fixture", mode="paper", journal=store,
            token_provider=lambda: "not-a-real-token", account_ref_provider=lambda scope: "123",
            transport=lambda req: OrderWireResponse(200, b'{"result":{"conditionalOrderId":"fixture-group"}}'),
            clock=lambda: at)
        def model_fixture(request):
            return json.dumps(dict(symbol="005380", action="ENTER", buy_price="380000", sell_price="390000",
                expires_on="2026-09-18", reason="실제 판단이 아닌 오프라인 예제", evidence_ids=["fixture-quote"]), ensure_ascii=False)
        result = run_rotation_step(experiment_id="demo", decision_id="one", envelope=envelope,
            evidence={"fixture-quote": {"as_of": at.isoformat(), "last": "385000", "source": "invented-fixture"}},
            model_id="fixture-no-ai-call", model_transport=model_fixture, snapshot_provider=snapshot,
            journal=store, clock=lambda: at, client=client)
        for key, side, amount in (("fixture-buy", "BUY", "380000"), ("fixture-sell", "SELL", "390000")):
            store.apply_experiment_fill("demo", mode="paper", fill_id=key, symbol="005380", side=side,
                quantity=Decimal(1), amount=Decimal(amount), costs=Decimal(500), observed_at=at)
        reconcile_command(journal=store, request_key="rotation-" + result.decision_key,
            account_scope="offline-fixture", order_id="fixture-group", status="COMPLETED", filled_quantity="1",
            group_complete=True, observed_at=at)
        end = snapshot()
        print(json.dumps({"mode": "OFFLINE_FIXTURE", "real_orders_sent": 0, "real_model_calls": 0,
            "fixture_order_status": result.execution_status, "experiment_quantity": str(end.experiment_quantity),
            "remaining_cash": str(end.available_cash),
            "fixture_net_cash_change": str(end.available_cash - envelope.initial_budget)}, indent=2))


if __name__ == "__main__":
    main()
