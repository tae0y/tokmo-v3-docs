import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

from test_portfolio_calculations import (
    NOW as PORT_NOW,
)
from test_portfolio_calculations import (
    PRIVATE_PENSION,
    PRIVATE_PERSONAL,
    account,
    holding,
    public_barrier,
    quote,
    trade,
)
from test_production_cadence import SLOT_US_OPEN, fixture, stage_status

from tokmo.programs import event_analysis_execution as event_module
from tokmo.programs.cadence_fixture_deps import build_fixture_cadence_deps
from tokmo.programs.event_analysis_execution import FixtureEventAnalysisExecutionV1
from tokmo.programs.production_cadence import run_production_cadence_once
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.analysis_contracts import MaterialKindV1, subject_id_for
from tokmo.values.cadence_policy import CADENCE_POLICY_V1
from tokmo.values.portfolio_calculations import PortfolioScope, ScopeUnresolved
from tokmo.values.production_cadence import CadenceOutcome
from tokmo.values.session_gate import Market


def _ids(body):
    return [row["candidate_id"] for row in json.loads(body)["candidates"]]


def test_admitted_event_runs_full_production_cadence_request_path_once_and_replays_zero(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "shadow-root"
    observed = SLOT_US_OPEN.replace(second=1)
    calls = {
        "glm": 0,
        "sol": 0,
        "parse_glm": 0,
        "parse_sol": 0,
        "decision": 0,
        "settlement": 0,
    }

    real_parse_glm = event_module.parse_glm_batch
    real_parse_sol = event_module.parse_sol_batch
    real_decision = event_module._decision
    real_settlement = OpsKernelStore.settle_analysis_result_with_coverage

    def parse_glm(*args, **kwargs):
        calls["parse_glm"] += 1
        return real_parse_glm(*args, **kwargs)

    def parse_sol(*args, **kwargs):
        calls["parse_sol"] += 1
        return real_parse_sol(*args, **kwargs)

    def decision(*args, **kwargs):
        calls["decision"] += 1
        return real_decision(*args, **kwargs)

    def settlement(self, *args, **kwargs):
        calls["settlement"] += 1
        return real_settlement(self, *args, **kwargs)

    monkeypatch.setattr(event_module, "parse_glm_batch", parse_glm)
    monkeypatch.setattr(event_module, "parse_sol_batch", parse_sol)
    monkeypatch.setattr(event_module, "_decision", decision)
    monkeypatch.setattr(
        OpsKernelStore, "settle_analysis_result_with_coverage", settlement
    )

    def glm(body):
        calls["glm"] += 1
        with sqlite3.connect(root / "ops" / "ops_kernel.sqlite3") as connection:
            assert connection.execute(
                "SELECT state FROM analysis_requests"
            ).fetchone() == ("ANALYZING",)
        return json.dumps(
            {
                "schema_version": 1,
                "annotations": [
                    {
                        "candidate_id": candidate_id,
                        "relevance": "PORTFOLIO_DIRECT",
                        "materiality": "HIGH",
                        "urgency": "NOW",
                        "evidence_unknowns": [],
                        "summary": "fixture selected",
                    }
                    for candidate_id in _ids(body)
                ],
            },
            separators=(",", ":"),
        ).encode()

    def sol(body):
        calls["sol"] += 1
        with sqlite3.connect(root / "ops" / "ops_kernel.sqlite3") as connection:
            evidence_id = json.loads(
                connection.execute(
                    "SELECT evidence_refs_json FROM candidate_manifest"
                ).fetchone()[0]
            )[0]
        return json.dumps(
            {
                "schema_version": 1,
                "analyses": [
                    {
                        "candidate_id": candidate_id,
                        "verdict": "ALERT_WORTHY",
                        "proposed_action": "ALERT",
                        "rationale": "fixture verified",
                        "cited_evidence_ids": [evidence_id],
                        "confidence": "1",
                        "disagreements_with_glm": [],
                    }
                    for candidate_id in _ids(body)
                ],
            },
            separators=(",", ":"),
        ).encode()

    deps, probe = build_fixture_cadence_deps(
        fixture=fixture(),
        clock=lambda: observed,
    )
    deps = replace(
        deps,
        analysis_execution=FixtureEventAnalysisExecutionV1(glm, sol),
    )
    first = run_production_cadence_once(
        root=root,
        policy_version=CADENCE_POLICY_V1.policy_version,
        scheduled_for_utc=SLOT_US_OPEN,
        observed_now_utc=observed,
        deps=deps,
    )
    assert first.outcome is CadenceOutcome.COMPLETE, first.stages
    assert stage_status(first, "deltas").status == "ok"
    assert stage_status(first, "admission").status == "ok"
    assert stage_status(first, "analysis_shadow").status == "ok"
    assert calls == {
        "glm": 1,
        "sol": 1,
        "parse_glm": 2,
        "parse_sol": 2,
        "decision": 1,
        "settlement": 1,
    }

    with sqlite3.connect(root / "ops" / "ops_kernel.sqlite3") as connection:
        assert connection.execute("SELECT state FROM analysis_requests").fetchone() == (
            "SUCCEEDED",
        )
        assert connection.execute("SELECT count(*) FROM dispositions").fetchone() == (
            1,
        )
        assert connection.execute(
            "SELECT channel,status FROM alert_outbox"
        ).fetchone() == ("SHADOW", None)
        assert connection.execute(
            "SELECT count(*) FROM analysis_coverage"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM analysis_batches WHERE state='SETTLED' AND exit='OK'"
        ).fetchone() == (2,)

    before = dict(calls)
    second = run_production_cadence_once(
        root=root,
        policy_version=CADENCE_POLICY_V1.policy_version,
        scheduled_for_utc=SLOT_US_OPEN,
        observed_now_utc=observed,
        deps=deps,
    )
    assert second.outcome is CadenceOutcome.DUPLICATE
    assert calls == before
    assert probe.calls  # collection fixture was exercised only by the first tick


def test_ai_safe_personal_pension_barrier_path_uses_typed_payload_and_max_two(tmp_path):
    personal_h, personal_t = account(
        PRIVATE_PERSONAL, holdings=(holding("SYNUS1", "2", "80", "USD", "US"),)
    )
    pension_h, pension_t = account(
        PRIVATE_PENSION, holdings=(holding("SYNUS2", "3", "90", "USD", "US"),)
    )
    barrier = public_barrier(
        tmp_path / "packs",
        holding_accounts=(personal_h, pension_h),
        trade_accounts=(personal_t, pension_t),
        quotes={
            "KR": [],
            "US": [
                quote("SYNUS1", "100", "USD"),
                quote("SYNUS2", "110", "USD"),
                quote("UNHELD", "50", "USD"),
            ],
        },
    )
    store = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    bodies = []

    def glm(body):
        bodies.append(body)
        return json.dumps(
            {
                "schema_version": 1,
                "annotations": [
                    {
                        "candidate_id": cid,
                        "relevance": "PORTFOLIO_DIRECT",
                        "materiality": "HIGH",
                        "urgency": "NOW",
                        "evidence_unknowns": [],
                        "summary": "typed fixture",
                    }
                    for cid in _ids(body)
                ],
            },
            separators=(",", ":"),
        ).encode()

    def sol(body):
        bodies.append(body)
        decoded = json.loads(body)
        return json.dumps(
            {
                "schema_version": 1,
                "analyses": [
                    {
                        "candidate_id": row["candidate_id"],
                        "verdict": "ALERT_WORTHY",
                        "proposed_action": "ALERT",
                        "rationale": "typed fixture",
                        "cited_evidence_ids": [
                            row["payload"]["evidence_refs"][0]["evidence_id"]
                        ],
                        "confidence": "1",
                        "disagreements_with_glm": [],
                    }
                    for row in decoded["candidates"]
                ],
            },
            separators=(",", ":"),
        ).encode()

    resolver = lambda raw: {
        PRIVATE_PERSONAL: PortfolioScope.PERSONAL,
        PRIVATE_PENSION: PortfolioScope.PENSION,
    }.get(raw, ScopeUnresolved.UNRESOLVED)
    outcome = FixtureEventAnalysisExecutionV1(
        glm,
        sol,
        resolve_scope=resolver,
    )(tick=None, barrier=barrier, store=store, now=PORT_NOW)
    assert outcome.selected_count == 2
    assert outcome.suppressed_count == outcome.no_call_settled == 1
    assert len(bodies) == 4
    joined = b"".join(bodies)
    for forbidden in (
        b"account_type_label",
        b"ordinal",
        b"source_record_key",
        b"toss-acct-",
        b"internal_",
        b'"realized_pnl":',
    ):
        assert forbidden not in joined
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM analysis_requests"
        ).fetchone() == (3,)
        assert connection.execute(
            "SELECT count(*) FROM analysis_requests WHERE state='FAILED'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM candidate_manifest WHERE state='TERMINAL'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM analysis_coverage"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM analysis_batches"
        ).fetchone() == (4,)


def test_due_deferred_is_read_per_relevant_market_and_only_returned_suppressed_row_terminalizes(
    tmp_path, monkeypatch
):
    ph, pt = account(
        PRIVATE_PERSONAL, holdings=(holding("SYNUS1", "2", "80", "USD", "US"),)
    )
    barrier = public_barrier(
        tmp_path / "packs",
        holding_accounts=(ph,),
        trade_accounts=(pt,),
        quotes={"KR": [], "US": [quote("UNHELD", "50", "USD")]},
    )
    subject = subject_id_for(MaterialKindV1.QUOTE_STATE, ["US", "UNHELD"])
    observed = {"reads": [], "terminal": []}

    def read_due(self, market, now):
        observed["reads"].append(market)
        return (
            (SimpleNamespace(subject_id=subject, market=Market.US),)
            if market is Market.US
            else ()
        )

    def terminalize(self, role, subject_id, terminal_state, now):
        observed["terminal"].append((subject_id, terminal_state.value))

    monkeypatch.setattr(OpsKernelStore, "read_due_deferred", read_due)
    monkeypatch.setattr(OpsKernelStore, "terminalize_deferred", terminalize)
    store = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    resolver = lambda raw: (
        PortfolioScope.PERSONAL
        if raw == PRIVATE_PERSONAL
        else ScopeUnresolved.UNRESOLVED
    )
    fail_call = lambda body: (_ for _ in ()).throw(
        AssertionError("model call forbidden")
    )
    outcome = FixtureEventAnalysisExecutionV1(
        fail_call, fail_call, resolve_scope=resolver
    )(
        tick=None,
        barrier=barrier,
        store=store,
        now=PORT_NOW,
    )
    assert set(observed["reads"]) == set(barrier.proceed_markets)
    assert observed["terminal"] == [(subject, "TERMINAL")]
    assert outcome.selected_count == 0 and outcome.no_call_settled == 1


def test_unresolved_scope_whole_aborts_before_any_durable_creation(tmp_path):
    ph, pt = account(
        PRIVATE_PERSONAL, holdings=(holding("SYNUS1", "2", "80", "USD", "US"),)
    )
    uh, ut = account(
        "toss-acct-333333333333333333333333",
        holdings=(holding("SYNUS2", "3", "90", "USD", "US"),),
    )
    barrier = public_barrier(
        tmp_path / "packs",
        holding_accounts=(ph, uh),
        trade_accounts=(pt, ut),
        quotes={
            "KR": [],
            "US": [quote("SYNUS1", "100", "USD"), quote("SYNUS2", "110", "USD")],
        },
    )
    store = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    calls = {"model": 0}

    def forbidden(body):
        calls["model"] += 1
        raise AssertionError("whole-abort must not call model")

    resolver = lambda raw: (
        PortfolioScope.PERSONAL
        if raw == PRIVATE_PERSONAL
        else ScopeUnresolved.UNRESOLVED
    )
    outcome = FixtureEventAnalysisExecutionV1(
        forbidden, forbidden, resolve_scope=resolver
    )(
        tick=None,
        barrier=barrier,
        store=store,
        now=PORT_NOW,
    )
    assert outcome.status.value == "NO_CANDIDATE"
    assert (
        outcome.selected_count
        == outcome.suppressed_count
        == outcome.no_call_settled
        == 0
    )
    assert calls["model"] == 0
    with sqlite3.connect(store.path) as connection:
        for table in (
            "analysis_requests",
            "candidate_manifest",
            "subject_baselines",
            "quote_state_history",
            "analysis_batches",
            "analysis_coverage",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (
                0,
            ), table


def test_execution_join_uses_private_key_digest_and_exact_direction(tmp_path):
    first = trade("private-order-A", "US", quantity="1", price="100", amount="100") | {
        "instrument_raw": "SYNUS1",
        "transaction_type_raw": "BUY",
    }
    second = trade("private-order-B", "US", quantity="1", price="100", amount="100") | {
        "instrument_raw": "SYNUS1",
        "transaction_type_raw": "BUYBACK",
    }
    holdings, trades = account(
        PRIVATE_PERSONAL,
        holdings=(holding("SYNUS1", "2", "80", "USD", "US"),),
        inserted=(first, second),
    )
    barrier = public_barrier(
        tmp_path / "packs",
        holding_accounts=(holdings,),
        trade_accounts=(trades,),
        quotes={"KR": [], "US": [quote("SYNUS1", "100", "USD")]},
    )
    bodies = []

    def model(body):
        bodies.append(json.loads(body))
        return json.dumps(
            {
                "decisions": [
                    {
                        "candidate_id": cid,
                        "verdict": "NOT_MATERIAL",
                        "confidence": "0.9",
                        "reason_codes": ["NO_CHANGE"],
                        "evidence_ids": [],
                    }
                    for cid in _ids(body)
                ]
            }
        )

    outcome = FixtureEventAnalysisExecutionV1(
        model, model, resolve_scope=lambda raw: PortfolioScope.PERSONAL
    )(
        tick=None,
        barrier=barrier,
        store=OpsKernelStore(tmp_path / "ops.sqlite3"),
        now=PORT_NOW,
    )
    execution_rows = [
        candidate["payload"]["completed_execution"]
        for body in bodies
        for candidate in body["candidates"]
        if candidate["payload"].get("completed_execution")
    ]
    assert outcome.selected_count == 2
    assert {row["direction"] for row in execution_rows} == {"BUY", "OTHER"}
    encoded = json.dumps(bodies)
    assert "private-order-A" not in encoded and "private-order-B" not in encoded


def test_invalid_execution_grammar_suppresses_only_candidate_and_peer_continues(
    tmp_path,
):
    invalid = trade("private-order-invalid", "US") | {
        "instrument_raw": "Fixture ETF Alpha",
        "transaction_type_raw": "BUY\nDROP",
    }
    holdings, trades = account(
        PRIVATE_PERSONAL,
        holdings=(holding("SYNUS1", "1", "80", "USD", "US"),),
        inserted=(invalid,),
    )
    barrier = public_barrier(
        tmp_path / "packs",
        holding_accounts=(holdings,),
        trade_accounts=(trades,),
        quotes={"KR": [], "US": [quote("SYNUS1", "100", "USD")]},
    )
    calls = []

    def model(body):
        calls.extend(_ids(body))
        return json.dumps(
            {
                "decisions": [
                    {
                        "candidate_id": cid,
                        "verdict": "NOT_MATERIAL",
                        "confidence": "0.9",
                        "reason_codes": ["NO_CHANGE"],
                        "evidence_ids": [],
                    }
                    for cid in _ids(body)
                ]
            }
        )

    outcome = FixtureEventAnalysisExecutionV1(
        model, model, resolve_scope=lambda raw: PortfolioScope.PERSONAL
    )(
        tick=None,
        barrier=barrier,
        store=OpsKernelStore(tmp_path / "ops.sqlite3"),
        now=PORT_NOW,
    )
    assert outcome.admitted_count == 2 and outcome.selected_count == 1
    assert outcome.suppressed_count == outcome.no_call_settled == 1
    assert len(set(calls)) == 1


def _three_quote_barrier(tmp_path):
    holdings, trades = account(
        PRIVATE_PERSONAL,
        holdings=tuple(
            holding(symbol, "1", "80", "USD", "US")
            for symbol in ("SYNUS1", "SYNUS2", "SYNUS3")
        ),
    )
    return public_barrier(
        tmp_path,
        holding_accounts=(holdings,),
        trade_accounts=(trades,),
        quotes={
            "KR": [],
            "US": [
                quote(symbol, str(price), "USD")
                for symbol, price in (("SYNUS1", 100), ("SYNUS2", 101), ("SYNUS3", 102))
            ],
        },
    )


def test_oversize_selected_candidate_promotes_capped_peer(tmp_path, monkeypatch):
    barrier = _three_quote_barrier(tmp_path / "packs")
    real_build = event_module.build_prompt_body
    first = {"candidate_id": None}

    def sized(prompt):
        candidate_id = prompt.segments[-1].candidate_id
        if first["candidate_id"] is None:
            first["candidate_id"] = candidate_id
        return (
            b"x" * (event_module.MODEL_REQUEST_MAX_BYTES + 1)
            if candidate_id == first["candidate_id"]
            else real_build(prompt)
        )

    monkeypatch.setattr(event_module, "build_prompt_body", sized)
    called = []

    def model(body):
        called.extend(_ids(body))
        return json.dumps(
            {
                "decisions": [
                    {
                        "candidate_id": cid,
                        "verdict": "NOT_MATERIAL",
                        "confidence": "0.9",
                        "reason_codes": ["NO_CHANGE"],
                        "evidence_ids": [],
                    }
                    for cid in _ids(body)
                ]
            }
        )

    store = OpsKernelStore(tmp_path / "ops.sqlite3")
    outcome = FixtureEventAnalysisExecutionV1(
        model, model, resolve_scope=lambda raw: PortfolioScope.PERSONAL
    )(tick=None, barrier=barrier, store=store, now=PORT_NOW)
    assert outcome.admitted_count == 3 and outcome.selected_count == 2
    assert outcome.suppressed_count == outcome.no_call_settled == 1
    assert first["candidate_id"] not in called
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM analysis_requests"
        ).fetchone() == (3,)
        assert connection.execute(
            "SELECT count(*) FROM analysis_batches"
        ).fetchone() == (2,)


def test_all_eligible_oversize_is_typed_no_candidate_and_model_zero(
    tmp_path, monkeypatch
):
    barrier = _three_quote_barrier(tmp_path / "packs")
    monkeypatch.setattr(
        event_module,
        "build_prompt_body",
        lambda prompt: b"x" * (event_module.MODEL_REQUEST_MAX_BYTES + 1),
    )
    calls = {"model": 0}
    artifacts = []
    real_settle = OpsKernelStore.settle_analysis_result_with_coverage

    def settle(self, *args, **kwargs):
        artifacts.append(kwargs["result"].artifact_digest)
        return real_settle(self, *args, **kwargs)

    monkeypatch.setattr(OpsKernelStore, "settle_analysis_result_with_coverage", settle)

    def forbidden(body):
        calls["model"] += 1
        raise AssertionError("oversize candidate must not call model")

    store = OpsKernelStore(tmp_path / "ops.sqlite3")
    outcome = FixtureEventAnalysisExecutionV1(
        forbidden, forbidden, resolve_scope=lambda raw: PortfolioScope.PERSONAL
    )(tick=None, barrier=barrier, store=store, now=PORT_NOW)
    assert outcome.status.value == "NO_CANDIDATE"
    assert (
        outcome.admitted_count
        == outcome.suppressed_count
        == outcome.no_call_settled
        == 3
    )
    assert outcome.selected_count == calls["model"] == 0
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM analysis_requests"
        ).fetchone() == (3,)
        assert connection.execute(
            "SELECT count(*) FROM analysis_coverage"
        ).fetchone() == (0,)
    assert artifacts == [None, None, None]
