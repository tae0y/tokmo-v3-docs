"""A fixed user-run entry must never execute yesterday's or unfinished plan."""
from dataclasses import asdict
from datetime import timedelta
import importlib.util
import io
import json
from pathlib import Path

import pytest

from test_order_commands import NOW, buy
from test_order_execution import limits, snapshot
from tokmo.programs.order_execution import OrderExecutionClient
from tokmo.records.order_command_journal import OrderCommandJournal
from tokmo.values.order_commands import OrderWireResponse
from tokmo.values.order_execution import command_document


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "hyundai_rotation.py"


@pytest.fixture
def entry():
    spec = importlib.util.spec_from_file_location("hyundai_rotation_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plan(**changes):
    document = dict(schema="hyundai-rotation-plan-v1", plan_id="fixture-20260911-1", status="READY",
        analysis_started_at=(NOW - timedelta(minutes=5)).isoformat(), completed_at=NOW.isoformat(),
        valid_until=(NOW + timedelta(minutes=15)).isoformat(), command=json.loads(command_document(buy())))
    document.update(changes)
    return document


def write_state(tmp_path, document):
    tmp_path.chmod(0o700)
    (tmp_path / "current-plan.json").write_text(json.dumps(document))
    (tmp_path / "execution-context.json").write_text(json.dumps(
        dict(envelope=asdict(limits()), snapshot=asdict(snapshot())), default=str))


def test_preview_needs_no_credentials_or_client(entry, tmp_path, capsys):
    write_state(tmp_path, plan())
    def forbidden(**kwargs): pytest.fail("preview constructed order client")
    assert entry.main(["--state-dir", str(tmp_path)], clock=lambda: NOW, client_factory=forbidden) == 0
    assert "380000" in capsys.readouterr().out
    assert not (tmp_path / "orders.sqlite3").exists()


@pytest.mark.parametrize("changes", [dict(status="ANALYZING"), dict(completed_at=None),
    dict(completed_at=(NOW - timedelta(days=1)).isoformat()), dict(valid_until=NOW.isoformat()),
    dict(completed_at=(NOW + timedelta(seconds=1)).isoformat())])
def test_incomplete_stale_or_expired_plan_cannot_execute(entry, tmp_path, capsys, changes):
    write_state(tmp_path, plan(**changes))
    assert entry.main(["--state-dir", str(tmp_path), "--execute"], clock=lambda: NOW,
                      client_factory=lambda **kwargs: pytest.fail("invalid plan used")) == 2
    assert not (tmp_path / "orders.sqlite3").exists()


@pytest.mark.parametrize("field,value", [("symbol", "005930"), ("market", "US"), ("quantity", "2"), ("order_type", "MARKET")])
def test_fixed_entry_is_hyundai_one_share_limit_only(entry, field, value):
    document = plan()
    if field in {"symbol", "market"}:
        document["command"][field] = value
    else:
        document["command"]["payload"][field] = value
    with pytest.raises(ValueError): entry.load_plan(json.dumps(document), now=NOW)


def test_hold_is_a_successful_no_order_outcome(entry, tmp_path):
    write_state(tmp_path, plan(status="HOLD", command=None))
    assert entry.main(["--state-dir", str(tmp_path), "--execute"], clock=lambda: NOW,
                      client_factory=lambda **kwargs: pytest.fail("HOLD called broker")) == 0


def test_same_file_repeated_execution_cannot_send_twice(entry, tmp_path, monkeypatch):
    write_state(tmp_path, plan())
    calls = []
    def factory(**kwargs):
        def transport(request):
            calls.append(request)
            return OrderWireResponse(200, b'{"result":{"orderId":"fixture-order"}}')
        return OrderExecutionClient(account_scope=kwargs["account_scope"], mode="live",
            journal=OrderCommandJournal(kwargs["journal_path"]), token_provider=kwargs["token_provider"],
            account_ref_provider=kwargs["account_ref_provider"], clock=kwargs["clock"], transport=transport)
    for _ in range(2):
        monkeypatch.setattr(entry.sys, "stdin", io.StringIO('{"token":"fixture-token","account_ref":"123","account_scope":"personal-test"}'))
        assert entry.main(["--state-dir", str(tmp_path), "--execute"], clock=lambda: NOW, client_factory=factory) == 0
    assert len(calls) == 1


def test_context_must_be_refreshed_at_execution_time(entry, tmp_path, monkeypatch):
    write_state(tmp_path, plan())
    monkeypatch.setattr(entry.sys, "stdin", io.StringIO('{"token":"fixture-token","account_ref":"123","account_scope":"personal-test"}'))
    assert entry.main(["--state-dir", str(tmp_path), "--execute"], clock=lambda: NOW + timedelta(minutes=5),
                      client_factory=lambda **kwargs: pytest.fail("stale account snapshot used")) == 2


def test_experiment_deadline_blocks_new_orders(entry):
    after = NOW.replace(year=2027, month=1, day=1)
    document = plan(analysis_started_at=after.isoformat(), completed_at=after.isoformat(),
                    valid_until=(after + timedelta(minutes=10)).isoformat())
    with pytest.raises(ValueError): entry.load_plan(json.dumps(document), now=after)


def test_missing_plan_does_not_reuse_any_previous_file(entry, tmp_path):
    (tmp_path / "yesterday.json").write_text(json.dumps(plan()))
    assert entry.main(["--state-dir", str(tmp_path)], clock=lambda: NOW,
                      client_factory=lambda **kwargs: pytest.fail("fallback file used")) == 2


def test_plan_change_while_receiving_credentials_requires_another_preview(entry, tmp_path, monkeypatch):
    write_state(tmp_path, plan())
    class ChangingInput(io.StringIO):
        def read(self, size=-1):
            (tmp_path / "current-plan.json").write_text(json.dumps(plan(status="ANALYZING")))
            return super().read(size)
    monkeypatch.setattr(entry.sys, "stdin", ChangingInput('{"token":"fixture-token","account_ref":"123","account_scope":"personal-test"}'))
    assert entry.main(["--state-dir", str(tmp_path), "--execute"], clock=lambda: NOW,
                      client_factory=lambda **kwargs: pytest.fail("replaced plan used")) == 2


def test_credentials_for_another_account_cannot_be_used(entry, tmp_path, monkeypatch, capsys):
    write_state(tmp_path, plan())
    monkeypatch.setattr(entry.sys, "stdin", io.StringIO('{"token":"secret-fixture","account_ref":"123","account_scope":"wrong-account"}'))
    assert entry.main(["--state-dir", str(tmp_path), "--execute"], clock=lambda: NOW,
                      client_factory=lambda **kwargs: pytest.fail("wrong account used")) == 2
    captured = capsys.readouterr()
    assert "secret-fixture" not in captured.out + captured.err


def test_today_is_checked_in_seoul_not_utc(entry):
    yesterday = NOW.replace(day=10, hour=14)  # Sep 10 23:00 KST
    today = yesterday + timedelta(hours=2)  # Sep 11 01:00 KST, same UTC date
    document = plan(analysis_started_at=yesterday.isoformat(), completed_at=yesterday.isoformat(),
                    valid_until=(today + timedelta(hours=1)).isoformat())
    with pytest.raises(ValueError, match="not_today"):
        entry.load_plan(json.dumps(document), now=today)
