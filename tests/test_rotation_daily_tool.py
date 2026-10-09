"""Band proposals are files the owner approves; approval rewrites the policy safely."""

import importlib.util
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tokmo.values.rotation_policy import load_policies

TOOL = Path(__file__).resolve().parents[1] / "tools" / "run_hyundai_rotation_daily.py"
EXAMPLE = (
    Path(__file__).resolve().parents[1]
    / "ops"
    / "hyundai-rotation"
    / "experiments.example.json"
)
NOW = datetime(2026, 9, 14, 1, 12, 20, tzinfo=UTC)


@pytest.fixture
def tool():
    spec = importlib.util.spec_from_file_location("rotation_daily_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def state(tmp_path):
    root = tmp_path.resolve() / "state"
    root.mkdir(mode=0o700)
    (root / "experiments.json").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "experiments.json").chmod(0o600)
    return root


class Outcome:
    def __init__(self, proposal):
        self.status, self.reason = "SUBMITTED", "acknowledged"
        self.details = {"day": "2026-09-14", "band_proposal": proposal}


def test_proposal_is_recorded_once_and_approval_rewrites_the_policy(tool, state):
    (policy,) = load_policies((state / "experiments.json").read_text())
    proposal = {"changes": {"sell_min_pct": "1.5"}, "reason": "상승 여력 제한"}
    proposal_id, record = tool.record_proposal(
        state, Outcome(proposal), policy, now=NOW
    )
    assert (
        proposal_id == "20260914-hyundai-005380-2026h2"
        and record["status"] == "PENDING"
    )
    assert tool.record_proposal(state, Outcome(proposal), policy, now=NOW) == (
        proposal_id,
        None,
    )
    assert (
        tool.main(
            ["--state-dir", str(state), "proposals"], environ={}, clock=lambda: NOW
        )
        == 0
    )
    assert (
        tool.main(
            ["--state-dir", str(state), "approve", proposal_id],
            environ={},
            clock=lambda: NOW,
        )
        == 0
    )
    (revised,) = load_policies((state / "experiments.json").read_text())
    assert (
        str(revised.sell_min_pct) == "1.5"
        and revised.plan_version == policy.plan_version
    )
    assert any(p.name.startswith("experiments.json.bak-") for p in state.iterdir())
    assert (
        json.loads((state / "proposals" / f"{proposal_id}.json").read_text())["status"]
        == "APPROVED"
    )
    assert (
        tool.main(
            ["--state-dir", str(state), "approve", proposal_id],
            environ={},
            clock=lambda: NOW,
        )
        == 1
    )


def test_invalid_or_envelope_proposals_never_touch_the_policy(tool, state):
    (policy,) = load_policies((state / "experiments.json").read_text())
    original = (state / "experiments.json").read_text()
    bad_id, _ = tool.record_proposal(
        state,
        Outcome({"changes": {"sell_floor": "100000"}, "reason": "x"}),
        policy,
        now=NOW,
    )
    assert (
        tool.main(
            ["--state-dir", str(state), "approve", bad_id],
            environ={},
            clock=lambda: NOW,
        )
        == 1
    )
    assert (state / "experiments.json").read_text() == original
    envelope = state / "proposals" / "20260915-hyundai-005380-2026h2.json"
    envelope.write_text(
        json.dumps(
            {
                "proposal_id": envelope.stem,
                "experiment_id": policy.experiment_id,
                "status": "PENDING",
                "created_at": NOW.isoformat(),
                "changes": {"protected_quantity": "10"},
                "reason": "수동 매수",
            }
        )
    )
    assert (
        tool.main(
            ["--state-dir", str(state), "approve", envelope.stem],
            environ={},
            clock=lambda: NOW,
        )
        == 1
    )
    assert (state / "experiments.json").read_text() == original
    assert (
        tool.main(
            ["--state-dir", str(state), "reject", bad_id], environ={}, clock=lambda: NOW
        )
        == 0
    )
    assert tool.pending_proposals(state).keys() == {envelope.stem}


def test_manual_fill_books_the_lot_once_and_refuses_bad_inputs(tool, state, capsys):
    from decimal import Decimal

    from tokmo.records.order_command_journal import OrderCommandJournal

    base = [
        "--state-dir",
        str(state),
        "record-fill",
        "--order-id",
        "toss-abc_1",
        "--price",
        "365000",
    ]
    assert (
        tool.main(
            base + ["--side", "BUY", "--commission", "55"],
            environ={},
            clock=lambda: NOW,
        )
        == 0
    )
    book = OrderCommandJournal(state / "orders.sqlite3").experiment_book(
        "hyundai-005380-2026h2", mode="live"
    )
    assert (book["quantity"], book["cash"], book["frozen"]) == (
        Decimal(1),
        Decimal(450000 - 365000 - 55),
        False,
    )
    assert any(
        p.name.endswith("-manual-hyundai-005380-2026h2.json")
        for p in (state / "runs").iterdir()
    )
    # Same broker order id again: idempotent, nothing double-booked.
    assert (
        tool.main(
            base + ["--side", "BUY", "--commission", "55"],
            environ={},
            clock=lambda: NOW,
        )
        == 0
    )
    assert '"applied": "DUPLICATE"' in capsys.readouterr().out
    assert OrderCommandJournal(state / "orders.sqlite3").experiment_book(
        "hyundai-005380-2026h2", mode="live"
    )["quantity"] == Decimal(1)
    # A second lot exceeds the experiment size; an off-tick price and an oversell are refused.
    assert (
        tool.main(
            [
                "--state-dir",
                str(state),
                "record-fill",
                "--order-id",
                "toss-2",
                "--side",
                "BUY",
                "--price",
                "360000",
            ],
            environ={},
            clock=lambda: NOW,
        )
        == 1
    )
    assert (
        tool.main(
            [
                "--state-dir",
                str(state),
                "record-fill",
                "--order-id",
                "toss-3",
                "--side",
                "SELL",
                "--price",
                "400123",
            ],
            environ={},
            clock=lambda: NOW,
        )
        == 1
    )
    assert (
        tool.main(
            [
                "--state-dir",
                str(state),
                "record-fill",
                "--order-id",
                "toss-4",
                "--side",
                "SELL",
                "--price",
                "400000",
                "--quantity",
                "2",
            ],
            environ={},
            clock=lambda: NOW,
        )
        == 1
    )
    # Selling the lot closes the cycle: inventory back to zero, proceeds booked.
    assert (
        tool.main(
            [
                "--state-dir",
                str(state),
                "record-fill",
                "--order-id",
                "toss-5",
                "--side",
                "SELL",
                "--price",
                "400000",
                "--commission",
                "60",
                "--tax",
                "600",
            ],
            environ={},
            clock=lambda: NOW,
        )
        == 0
    )
    book = OrderCommandJournal(state / "orders.sqlite3").experiment_book(
        "hyundai-005380-2026h2", mode="live"
    )
    assert (book["quantity"], book["cash"]) == (
        Decimal(0),
        Decimal(450000 - 365055 + 400000 - 660),
    )


def test_waiting_file_yields_a_same_day_proposal_the_owner_approves_without_touching_the_band(
    tool, state
):
    (policy,) = load_policies((state / "experiments.json").read_text())
    waiting = json.dumps(
        {
            "status": "WAITING_APPROVAL",
            "producer": "astra",
            "analysis_id": "a1",
            "revision": 3,
            "policy_digest": "abc123",
            "decision": {
                "action": "ENTER",
                "buy_price": "345000",
                "sell_price": "470000",
                "quantity": "1",
                "reason": "r",
            },
            "band_proposal": {"buy_floor": "340000", "reason": "34만 지지"},
        }
    )
    proposal_id, record = tool.ensure_today_proposal(
        state, policy, waiting, trade_date="2026-09-16", now=NOW
    )
    assert (
        proposal_id == "20260916-today-hyundai-005380-2026h2"
        and record["scope"] == "today"
        and record["status"] == "PENDING"
    )
    # The bridge binds its approval to the file's revision/analysis_id/policy digest.
    assert (record["revision"], record["analysis_id"], record["policy_digest"]) == (
        3,
        "a1",
        "abc123",
    )
    assert tool.ensure_today_proposal(
        state, policy, waiting, trade_date="2026-09-16", now=NOW
    ) == (proposal_id, None)
    assert tool.ensure_today_proposal(
        state,
        policy,
        json.dumps({"status": "COMPLETE"}),
        trade_date="2026-09-16",
        now=NOW,
    ) == (None, None)
    original = (state / "experiments.json").read_text()
    assert (
        tool.main(
            ["--state-dir", str(state), "approve", proposal_id],
            environ={},
            clock=lambda: NOW,
        )
        == 0
    )
    approved = json.loads((state / "proposals" / f"{proposal_id}.json").read_text())
    assert (
        approved["status"] == "APPROVED"
        and approved["approved_at"]
        and (state / "experiments.json").read_text() == original
    )
    assert [
        r["proposal_id"]
        for r in tool.pending_proposals(state, statuses=("APPROVED",)).values()
    ] == [proposal_id]
    assert "오늘만 승인" in tool.today_proposal_text(proposal_id, record, policy)


def test_run_refuses_a_world_readable_state_dir(tool, state, capsys):
    state.chmod(0o755)
    assert (
        tool.main(
            ["--state-dir", str(state), "run", "--no-model"],
            environ={},
            clock=lambda: NOW,
        )
        == 1
    )
    assert "state_dir_not_private" in capsys.readouterr().out


@pytest.mark.parametrize(
    "reason,producer,expected_notices",
    [
        ("analysis_timeout", "astra", 0),
        ("approval_timeout", "astra", 0),
        ("analysis_failed:input_preparation_failed", "astra", 0),
        ("analysis_expired:approval_timeout", "astra", 0),
        ("decision_file:absent", None, 1),
        ("decision_file:valid_until_passed", "astra", 1),
        ("deadline_before_submit", "astra", 1),
    ],
)
def test_file_runner_leaves_analysis_notices_to_producer_but_reports_own_errors(
    tool, state, monkeypatch, reason, producer, expected_notices
):
    from types import SimpleNamespace

    from tokmo.programs.rotation_daily import DailyOutcome

    notices = []
    monkeypatch.setattr(tool, "notify", lambda env, text: notices.append(text) or True)
    monkeypatch.setattr(
        tool,
        "build_rotation_daily_wiring",
        lambda **kw: SimpleNamespace(journal=None, reads=None, client=None),
    )
    outcome = DailyOutcome(
        "EXPIRED",
        reason,
        {"day": "2026-09-14", "producer": producer, "analysis_id": "a1"},
    )
    monkeypatch.setattr(
        "tokmo.programs.rotation_daily.run_from_decision_file", lambda **kw: outcome
    )
    assert (
        tool.main(
            ["--state-dir", str(state), "run-file", "--market", "KR", "--notify"],
            environ={},
            clock=lambda: NOW,
        )
        == 1
    )
    assert len(notices) == expected_notices


def test_file_runner_mirrors_proposal_without_duplicate_approval_notice(
    tool, state, monkeypatch
):
    from types import SimpleNamespace

    from tokmo.programs.rotation_daily import DailyOutcome

    (policy,) = load_policies((state / "experiments.json").read_text())
    path = tool.decision_path(state, policy, "2026-09-14")
    path.parent.mkdir(mode=0o700)
    path.write_text(
        json.dumps(
            {
                "status": "WAITING_APPROVAL",
                "producer": "astra",
                "analysis_id": "a1",
                "decision": {
                    "action": "ENTER",
                    "buy_price": "345000",
                    "sell_price": "470000",
                    "quantity": "1",
                    "reason": "r",
                },
            }
        )
    )
    notices = []
    monkeypatch.setattr(tool, "notify", lambda env, text: notices.append(text) or True)
    monkeypatch.setattr(
        tool,
        "build_rotation_daily_wiring",
        lambda **kw: SimpleNamespace(journal=None, reads=None, client=None),
    )

    def consume(**kw):
        kw["decision_reader"]()
        return DailyOutcome(
            "EXPIRED",
            "approval_timeout",
            {"day": "2026-09-14", "producer": "astra", "analysis_id": "a1"},
        )

    monkeypatch.setattr("tokmo.programs.rotation_daily.run_from_decision_file", consume)
    assert (
        tool.main(
            ["--state-dir", str(state), "run-file", "--market", "KR", "--notify"],
            environ={},
            clock=lambda: NOW,
        )
        == 1
    )
    assert not notices
    assert next(iter(tool.pending_proposals(state).values()))["scope"] == "today"


def test_record_fill_reclaims_a_fill_the_settlement_filed_as_protected(tool, state):
    """The settlement guesses 'protected' for anything undeclared; declaring it reverses that."""
    from decimal import Decimal

    from tokmo.records.order_command_journal import OrderCommandJournal

    (policy,) = load_policies((state / "experiments.json").read_text())
    journal = OrderCommandJournal(state / "orders.sqlite3")
    journal.initialize_experiment(
        policy.experiment_id, mode="live", envelope=policy.envelope()
    )
    journal.apply_experiment_fill(
        policy.experiment_id,
        mode="live",
        fill_id="manual-seed",
        symbol="005380",
        side="BUY",
        quantity=Decimal(1),
        amount=Decimal(372500),
        costs=Decimal(0),
        observed_at=NOW,
    )
    # Yesterday's settlement read the owner's app sale as a protected-lot trade.
    journal.record_protected_fill(
        policy.experiment_id,
        mode="live",
        fill_id="app-9",
        fill={"side": "SELL", "quantity": "1"},
        recorded_at=NOW,
    )
    journal.reconfigure_protected_quantity(
        policy.experiment_id,
        mode="live",
        envelope=replace(policy.envelope(), protected_quantity=Decimal(6)),
    )
    tool.apply_protected_change(state, policy, Decimal(6), now=NOW)
    assert (
        str(
            load_policies((state / "experiments.json").read_text())[
                0
            ].protected_quantity
        )
        == "6"
    )
    # Declaring it takes the share back for the experiment and restores the rail.
    assert (
        tool.main(
            [
                "--state-dir",
                str(state),
                "record-fill",
                "--order-id",
                "app-9",
                "--side",
                "SELL",
                "--price",
                "382500",
            ],
            environ={},
            clock=lambda: NOW,
        )
        == 0
    )
    (revised,) = load_policies((state / "experiments.json").read_text())
    assert str(revised.protected_quantity) == "7"
    assert (
        journal.protected_fill(policy.experiment_id, mode="live", fill_id="app-9")
        is None
    )
    assert journal.experiment_book(policy.experiment_id, mode="live")[
        "quantity"
    ] == Decimal(0)
    # The journal now agrees with the restored policy, so the next settlement runs.
    journal.initialize_experiment(
        policy.experiment_id, mode="live", envelope=revised.envelope()
    )
