"""The AI can propose prices but cannot widen the experiment's envelope."""
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from test_order_execution import limits, snapshot, journal
from test_order_commands import NOW
from tokmo.programs.rotation_experiment import run_rotation_step
from tokmo.programs.order_execution import OrderExecutionClient
from tokmo.values.order_commands import OrderWireResponse
from tokmo.composition import build_order_execution_client


EVIDENCE = {"quote": {"as_of": NOW.isoformat(), "source": "toss", "last": "385000"}}


def decision(**changes):
    data = dict(symbol="005380", action="ENTER", buy_price="380000", sell_price="390000",
                expires_on="2026-09-18", reason="공급된 가격 근거에 따른 시험 제안", evidence_ids=["quote"])
    return json.dumps(dict(data, **changes), ensure_ascii=False)


def run(tmp_path, raw=None, **changes):
    params = dict(experiment_id="test", decision_id="decision-1", envelope=limits(), evidence=EVIDENCE,
                  model_id="fixture-model", model_transport=lambda r: decision() if raw is None else raw,
                  snapshot_provider=snapshot, journal=journal(tmp_path), clock=lambda: NOW)
    params.update(changes)
    return run_rotation_step(**params), params["journal"]


def test_preview_records_model_inputs_and_prices_without_order_submission(tmp_path):
    result, store = run(tmp_path)
    assert result.status == "PREVIEW" and store.count() == 0
    saved = store.decision(result.decision_key)
    assert saved["status"] == "DECIDED" and json.loads(saved["output_json"])["buy_price"] == "380000"
    assert "personal-test" not in saved["input_json"]
    assert json.loads(saved["input_json"])["experiment"]["quantity"] == "0"


@pytest.mark.parametrize("raw", [
    decision(symbol="LMT"), decision(buy_price="NaN"), decision(action="BUY_ALL"),
    decision(evidence_ids=["imaginary"]), decision(sell_price="370000"),
    decision(expires_on="not-a-date"), decision(reason=""),
    decision(action="HOLD"), '{"action":"HOLD","action":"ENTER"}',
])
def test_bad_model_outputs_cannot_become_orders(tmp_path, raw):
    result, store = run(tmp_path, raw)
    assert result.status == "INVALID" and store.count() == 0


@pytest.mark.parametrize("raw", [decision(buy_price="400000", sell_price="410000"),
                                  decision(expires_on="2027-01-01"),
                                  decision(expires_on="2026-09-01")])
def test_valid_model_json_still_obeys_budget_and_expiry(tmp_path, raw):
    assert run(tmp_path, raw)[0].status == "BLOCKED"


def test_hold_and_duplicates_do_not_force_transactions(tmp_path):
    raw = decision(action="HOLD", buy_price=None, sell_price=None, expires_on=None)
    first, store = run(tmp_path, raw)
    assert first.status == "HOLD"
    result, _ = run(tmp_path, raw, journal=store, model_transport=lambda r: pytest.fail("duplicate model call"))
    assert result.status == "EXISTING"


def test_oto_decision_reaches_shared_client_only_after_archiving(tmp_path):
    store = journal(tmp_path)
    calls = []
    def transport(request):
        calls.append(request)
        rows = []
        with store._db() as db:
            rows = db.execute("SELECT status FROM rotation_decisions").fetchall()
        assert rows[0][0] == "DECIDED"
        return OrderWireResponse(200, b'{"result":{"conditionalOrderId":"group-1"}}')
    client = OrderExecutionClient(account_scope="personal-test", mode="paper", journal=store,
                                  token_provider=lambda: "fixture", account_ref_provider=lambda s: "123",
                                  transport=transport, clock=lambda: NOW)
    result, _ = run(tmp_path, journal=store, client=client)
    assert result.execution_status == "ACCEPTED" and len(calls) == 1
    second, _ = run(tmp_path, journal=store, client=client, decision_id="decision-2")
    assert second.execution_status == "NOT_SENT" and len(calls) == 1


def test_unchanged_core_shares_cannot_be_sold(tmp_path):
    raw = decision(action="EXIT", buy_price=None, expires_on=None)
    assert run(tmp_path, raw)[0].status == "BLOCKED"
    held = lambda: snapshot(total_quantity=Decimal("8"), experiment_quantity=Decimal("1"), available_cash=Decimal("19000"))
    assert run(tmp_path, raw, decision_id="exit-2", snapshot_provider=held)[0].status == "PREVIEW"


def test_dated_evidence_and_expired_experiment_block_model(tmp_path):
    for evidence in ({"quote": {}}, {"quote": {"as_of": (NOW + timedelta(hours=1)).isoformat()}}):
        with pytest.raises(ValueError): run(tmp_path, evidence=evidence, model_transport=lambda r: pytest.fail("bad input reached model"))


def test_composition_builds_real_transport_but_is_disabled_by_default(tmp_path):
    called = []
    client = build_order_execution_client(account_scope="personal-test", journal_path=tmp_path / "new" / "journal.sqlite3",
                                         token_provider=lambda: called.append("token"),
                                         account_ref_provider=lambda s: called.append("account"), clock=lambda: NOW)
    assert client.transport.enabled is False and called == [] and client.mode == "live"
