"""Executor-accounted recurring membership acquisition without campaign writes."""

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

SESSION_DATE = date(2026, 8, 13)
NOW = datetime(2026, 8, 13, 20, 1, tzinfo=UTC)
SYMBOLS = ("AAA", "BBB", "CCC", "DDD")


def _context():
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_multifactor_core import MarketV1
    from tokmo.values.quant_recurring import (
        QUANT_RECURRING_PROGRAM_ID,
        QUANT_RECURRING_UNIVERSE_POLICY_VERSION,
        QuantDueContextV1,
        QuantDueDecisionV1,
        QuantDueIdentityV1,
        membership_plan_starts,
    )

    identity = QuantDueIdentityV1(
        QUANT_RECURRING_PROGRAM_ID,
        CADENCE_QUANT_SHADOW_POLICY_V1.policy_version,
        MarketV1.US_NYSE_NASDAQ,
        SESSION_DATE,
        QUANT_RECURRING_UNIVERSE_POLICY_VERSION,
    )
    return QuantDueContextV1(
        QuantDueDecisionV1.DUE,
        identity,
        SESSION_DATE,
        SESSION_DATE,
        SYMBOLS,
        90,
        membership_plan_starts(len(SYMBOLS)),
        CADENCE_QUANT_SHADOW_POLICY_V1.request_limits.membership_starts,
    )


def _bodies():
    from tokmo.facts.quant_recurring_membership import EndpointKindV1

    listed = (
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|"
        "Round Lot Size|ETF|NextShares\n"
        + "\n".join(
            f"{symbol}|{symbol} Common Stock|Q|N|N|100|N|N" for symbol in SYMBOLS[:2]
        )
        + "\nFile Creation Time: 081320260125|||||||\n"
    ).encode()
    other = (
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|"
        "Test Issue|NASDAQ Symbol\n"
        + "\n".join(
            f"{symbol}|{symbol} Common Stock|N|{symbol}|N|100|N|{symbol}"
            for symbol in SYMBOLS[2:]
        )
        + "\nFile Creation Time: 081320260125|||||||\n"
    ).encode()
    registry = json.dumps(
        {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [
                [index + 1, symbol, symbol, "Nasdaq" if index < 2 else "NYSE"]
                for index, symbol in enumerate(SYMBOLS)
            ],
        }
    ).encode()
    return {
        EndpointKindV1.NASDAQ_LISTED: listed,
        EndpointKindV1.OTHER_LISTED: other,
        EndpointKindV1.SEC_REGISTRY: registry,
    }


class _Claim:
    def __init__(self, operation):
        self.operation = operation


class _Executor:
    def __init__(self):
        self.starts = []

    def execute(self, operation, native_identity, callback):
        self.starts.append((operation, native_identity))
        return callback(_Claim(operation))


class _SettlementExecutor:
    def __init__(self):
        self.starts = []
        self.settlements = []

    def execute(self, operation, native_identity, callback):
        self.starts.append((operation, native_identity))
        try:
            outcome = callback(_Claim(operation))
        except Exception:
            self.settlements.append((native_identity, "FAILED"))
            raise
        self.settlements.append((native_identity, "SUCCEEDED"))
        return outcome


def _deps(*, fail_at=None):
    from tokmo.facts.official_membership_live import LiveOutcomeV1
    from tokmo.facts.quant_recurring_membership import EndpointKindV1
    from tokmo.values.official_membership_observation import OfficialSicSnapshotV1

    bodies = _bodies()
    calls = []

    def membership_fetch(*, claim, identity):
        calls.append((claim.operation, identity.endpoint_kind))
        if fail_at is not None and len(calls) == fail_at:
            raise RuntimeError("fixture provider failure")
        if identity.endpoint_kind is EndpointKindV1.SEC_SUBMISSIONS:
            index = int(identity.cik)
            symbol = SYMBOLS[index - 1]
            body = json.dumps(
                {
                    "cik": index,
                    "sic": "3571",
                    "tickers": [symbol],
                    "exchanges": ["Nasdaq" if index < 3 else "NYSE"],
                }
            ).encode()
        else:
            body = bodies[identity.endpoint_kind]
        return LiveOutcomeV1(True, "OK", body, NOW, 200)

    return SimpleNamespace(
        clock=lambda: NOW,
        membership_fetch=membership_fetch,
        sec_user_agent=lambda: "fixture contact@example.test",
        sic_snapshot_loader=lambda: OfficialSicSnapshotV1(
            "sec-sic-html-v1",
            "RECONSTRUCTED_CONTRACT",
            ("3571",),
            "fixture-sec-sic",
        ),
        calls=calls,
    )


def test_seam_a_claims_receipt_then_executes_exactly_seven_membership_wires(tmp_path):
    from tokmo.programs.quant_recurring_shadow import run_quant_membership_acquisition
    from tokmo.records.quant_recurring_receipts import QuantRecurringReceiptStore
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    context = _context()
    executor = _Executor()
    deps = _deps()
    receipts = QuantRecurringReceiptStore(tmp_path / "ops" / "quant_recurring.sqlite3")
    outcome = run_quant_membership_acquisition(
        root=tmp_path,
        deps=deps,
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        context=context,
        receipts=receipts,
    )

    assert outcome.status == "ok"
    assert outcome.membership_starts == 7
    assert outcome.acquisition is not None
    assert tuple(item.symbol for item in outcome.acquisition.issuer_rows) == SYMBOLS
    assert executor.starts == [
        ("MEMBERSHIP_NASDAQ_LISTED", "membership:nasdaqlisted:2026-08-13"),
        ("MEMBERSHIP_OTHER_LISTED", "membership:otherlisted:2026-08-13"),
        ("MEMBERSHIP_SEC_REGISTRY", "membership:registry:2026-08-13"),
        *[
            ("MEMBERSHIP_SEC_ISSUER", f"membership:issuer:2026-08-13:{symbol}")
            for symbol in SYMBOLS
        ],
    ]
    assert receipts.read_due(context.identity.identity_digest).state is (
        QuantTerminalStateV1.CLAIMED
    )
    assert not (tmp_path / "official-membership" / "observations.sqlite3").exists()


def test_seam_a_refuses_a_plan_bound_to_another_date_before_provider_start(
    monkeypatch,
    tmp_path,
):
    import tokmo.programs.quant_recurring_shadow as module
    from tokmo.facts.quant_recurring_membership import membership_operation_plan
    from tokmo.records.quant_recurring_receipts import QuantRecurringReceiptStore
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1

    original = membership_operation_plan(SYMBOLS, date(2026, 8, 14))
    monkeypatch.setattr(
        module, "membership_operation_plan", lambda symbols, session_date: original
    )
    executor = _Executor()
    with pytest.raises(module.QuantRecurringRefusal, match="session_binding"):
        module.run_quant_membership_acquisition(
            root=tmp_path,
            deps=_deps(),
            policy=CADENCE_QUANT_SHADOW_POLICY_V1,
            tick=SimpleNamespace(scheduled_for_utc=NOW),
            executor=executor,
            context=_context(),
            receipts=QuantRecurringReceiptStore(
                tmp_path / "ops" / "quant_recurring.sqlite3"
            ),
        )
    assert executor.starts == []


def test_seam_a_provider_failure_is_terminal_and_never_retried_same_date(tmp_path):
    from tokmo.programs.quant_recurring_shadow import run_quant_membership_acquisition
    from tokmo.records.quant_recurring_receipts import QuantRecurringReceiptStore
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    context = _context()
    receipts = QuantRecurringReceiptStore(tmp_path / "ops" / "quant_recurring.sqlite3")
    executor = _Executor()
    outcome = run_quant_membership_acquisition(
        root=tmp_path,
        deps=_deps(fail_at=2),
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        context=context,
        receipts=receipts,
    )
    assert outcome.status == "failed"
    assert outcome.reason == "source_failure:fixture provider failure"
    assert len(executor.starts) == 2
    assert receipts.read_due(context.identity.identity_digest).state is (
        QuantTerminalStateV1.FAILED
    )
    replay_executor = _Executor()
    replay = run_quant_membership_acquisition(
        root=tmp_path,
        deps=_deps(),
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=replay_executor,
        context=context,
        receipts=receipts,
    )
    assert (replay.status, replay.membership_starts) == ("skipped", 0)
    assert replay_executor.starts == []


def test_membership_not_ok_response_settles_wire_failed(tmp_path):
    from tokmo.facts.official_membership_live import LiveOutcomeV1
    from tokmo.programs.quant_recurring_shadow import run_quant_membership_acquisition
    from tokmo.records.quant_recurring_receipts import QuantRecurringReceiptStore
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1

    deps = _deps()
    deps.membership_fetch = lambda **kwargs: LiveOutcomeV1(
        False, "HTTP_500", None, NOW, 500
    )
    executor = _SettlementExecutor()
    outcome = run_quant_membership_acquisition(
        root=tmp_path,
        deps=deps,
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        context=_context(),
        receipts=QuantRecurringReceiptStore(
            tmp_path / "ops" / "quant_recurring.sqlite3"
        ),
    )

    assert (outcome.status, outcome.reason, outcome.membership_starts) == (
        "failed",
        "source_failure:source_failure",
        1,
    )
    assert executor.settlements == [("membership:nasdaqlisted:2026-08-13", "FAILED")]


def test_mid_plan_budget_exhaustion_is_truthful_settled_only_and_terminal(tmp_path):
    from tokmo.programs.quant_recurring_shadow import run_quant_membership_acquisition
    from tokmo.records.quant_recurring_receipts import QuantRecurringReceiptStore
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.ops_kernel import CadenceBudgetExceeded
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    class BudgetExecutor:
        def __init__(self):
            self.attempts = []
            self.rows = []
            self.refused_identity = None

        def execute(self, operation, native_identity, callback):
            self.attempts.append((operation, native_identity))
            if len(self.rows) == 5:
                self.refused_identity = native_identity
                raise CadenceBudgetExceeded("fixture membership ceiling")
            outcome = callback(_Claim(operation))
            self.rows.append((native_identity, "SUCCEEDED"))
            return outcome

    context = _context()
    deps = _deps()
    executor = BudgetExecutor()
    receipts = QuantRecurringReceiptStore(tmp_path / "ops" / "quant_recurring.sqlite3")
    outcome = run_quant_membership_acquisition(
        root=tmp_path,
        deps=deps,
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        context=context,
        receipts=receipts,
    )

    assert (outcome.status, outcome.reason, outcome.membership_starts) == (
        "failed",
        "membership_budget_exhausted",
        5,
    )
    assert outcome.reason != "source_failure"
    assert len(deps.calls) == len(executor.rows) == 5
    assert executor.refused_identity not in {identity for identity, _ in executor.rows}
    due = receipts.read_due(context.identity.identity_digest)
    assert (due.state, due.reason_code) == (
        QuantTerminalStateV1.FAILED,
        "membership_budget_exhausted",
    )
    assert not (tmp_path / "official-membership" / "observations.sqlite3").exists()

    replay_executor = _Executor()
    replay = run_quant_membership_acquisition(
        root=tmp_path,
        deps=_deps(),
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=replay_executor,
        context=context,
        receipts=receipts,
    )
    assert (replay.status, replay.reason, replay.membership_starts) == (
        "skipped",
        "already_terminal",
        0,
    )
    assert replay_executor.starts == []


def test_seam_a_counts_a_started_callback_even_when_it_raises(tmp_path):
    from tokmo.programs.quant_recurring_shadow import run_quant_membership_acquisition
    from tokmo.records.quant_recurring_receipts import QuantRecurringReceiptStore
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1

    class OpaqueExecutor:
        def __init__(self):
            self.count = 0

        def execute(self, operation, native_identity, callback):
            del native_identity
            self.count += 1
            return callback(_Claim(operation))

    executor = OpaqueExecutor()
    outcome = run_quant_membership_acquisition(
        root=tmp_path,
        deps=_deps(fail_at=2),
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=NOW),
        executor=executor,
        context=_context(),
        receipts=QuantRecurringReceiptStore(
            tmp_path / "ops" / "quant_recurring.sqlite3"
        ),
    )
    assert outcome.status == "failed"
    assert outcome.membership_starts == executor.count == 2
