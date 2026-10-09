"""G2-C: one-shot scripted analysis shadow over the stored G2 MaterialPacks.

Three-acceptance vertical slice:

1. A separate one-shot G2-C entrypoint drives the *existing*
   barrier_runner→analysis_runner composition against the G2 shadow root,
   while the G2-B data-only path stays byte-for-byte data-only.
2. Injected scripted GLM/SOL bytes carry a real candidate through the
   conditional-SOL screen and the validator into a SHADOW disposition, with
   zero live network / Telegram / order-write / cron surface.
3. The report records candidate_count, glm_calls, sol_calls, disposition
   counts, and total/glm/sol durations.
"""

import ast
import hashlib
import inspect
import json
import os
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from tokmo.programs import g2b_canary_runner
from tokmo.programs.candidate_builder import build_candidates
from tokmo.programs.g2a_shadow_runner import shadow_root
from tokmo.programs.g2c_analysis_shadow import run_c_analysis_shadow
from tokmo.programs.legacy_g2 import (
    legacy_assemble_analysis_materials as assemble_analysis_materials,
)
from tokmo.records.material_pack_store import MaterialPackWriter
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.records.receipt_store import ReceiptStore
from tokmo.values.material_pack import JobKind, build_material_pack_v1
from tokmo.values.ops_kernel import CandidateAssignment
from tokmo.values.ops_tick import tick_id_from_utc
from tokmo.values.portfolio_scope import resolve_account_scope_pattern  # N5
from tokmo.values.receipt_v1 import (
    AttemptStatus,
    AttemptV1,
    CompletenessV1,
    LastSuccessV1,
    MarketFreshness,
    ReceiptV1,
)
from tokmo.values.session_gate import Market

UTC_DAY = date(2026, 8, 3)
NOW = datetime(2026, 8, 3, 1, 0, tzinfo=UTC)
TICK = tick_id_from_utc(NOW)
ACCOUNT = "toss-acct-" + "a" * 24
KR_ONLY = frozenset({Market.KR})


class SteppingClock:
    """Aware-UTC clock advancing one second per call, starting at NOW."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        value = NOW + timedelta(seconds=self.calls)
        self.calls += 1
        return value


def _iso(value):
    return value.isoformat()


_PAYLOADS = {
    JobKind.HOLDINGS: {
        "accounts": [
            {
                "account_scope": ACCOUNT,
                "account_type_label": "일반",
                "ordinal": 1,
                "holdings": [
                    {
                        "symbol": "005930",
                        "quantity": "2",
                        "avg_price": "10",
                        "currency": "KRW",
                        "market_raw": "KR",
                        "market_field": "KR",
                    }
                ],
            }
        ],
        "global_inventory": True,
    },
    JobKind.QUOTES: {
        "markets": {
            "KR": [
                {
                    "symbol": "005930",
                    "last_price": "11",
                    "currency_raw": "KRW",
                    "provider_timestamp": None,
                    "fetched_at": _iso(NOW),
                    "stored_at": _iso(NOW),
                }
            ]
        },
    },
    JobKind.TRADES: {
        "accounts": [
            {
                "account_scope": ACCOUNT,
                "inserted": [
                    {
                        "source_record_key": "trade-1",
                        "trade_date": "2026-08-03",
                        "transaction_type_raw": "BUY",
                        "instrument_raw": "005930",
                        "quantity": "1",
                        "unit_price": "11",
                        "amount": "11",
                        "medium_raw": "M",
                        "currency_raw": "KRW",
                        "classified_market": "KR",
                        "filled_at": _iso(NOW),
                    }
                ],
                "duplicates": [],
                "quarantine": [],
                "incomplete": [],
            }
        ],
        "pagination_pending": False,
    },
}


def _seed_shadow_materials(home: Path) -> Path:
    """Store packs + receipts under the G2 shadow root exactly as G2-B does."""
    root = shadow_root(home, UTC_DAY)
    current = home
    for part in (root / "ops" / "receipts").relative_to(home).parts:
        current = current / part
        current.mkdir(mode=0o700, exist_ok=True)
        os.chmod(current, 0o700)
    packs = MaterialPackWriter(root / "ops", clock=lambda: NOW)
    for job, payload in _PAYLOADS.items():
        pack = build_material_pack_v1(
            schema_version=1,
            job=job,
            tick_id=TICK.tick_id,
            market_partition=("KR",),
            payload=payload,
            captured_at=NOW,
            source_timestamps={
                "provider": None,
                "fetched": _iso(NOW),
                "stored": _iso(NOW),
            },
            parser_version="material-pack-v1",
        )
        path = packs.write(pack)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        receipt = ReceiptV1(
            1,
            job,
            AttemptV1(TICK.tick_id, AttemptStatus.SAVED, "saved", NOW),
            LastSuccessV1(TICK.tick_id, str(path), digest, NOW, NOW),
            {"KR": MarketFreshness("KR", "fresh", TICK.tick_id, NOW)},
            CompletenessV1(1, 0, False, 0, 0) if job is JobKind.TRADES else None,
        )
        ReceiptStore(
            root / "ops" / "receipts" / f"{job.value}.json",
            clock=lambda: NOW,
        ).replace(receipt)
    return root


def _expected_candidates(root: Path):
    """Reconstruct the exact candidates the stored packs must yield."""
    packs = MaterialPackWriter(root / "ops", clock=lambda: NOW)
    receipts = {
        job: ReceiptStore(
            root / "ops" / "receipts" / f"{job.value}.json",
            clock=lambda: NOW,
        )
        for job in JobKind
    }
    barrier = assemble_analysis_materials(
        tick=TICK,
        open_markets=KR_ONLY,
        receipt_readers={job: receipts[job].read for job in JobKind},
        pack_reader=lambda path, digest: packs.read_and_verify(
            Path(path),
            digest,
        ),
        now=TICK.start_utc,
    )
    return build_candidates(
        barrier,
        baselines={},
        now=TICK.start_utc,
        resolve_scope=resolve_account_scope_pattern,
    ).candidates


def _glm_screen_all_none(body):
    return json.dumps(
        {
            "schema_version": 1,
            "annotations": [
                {
                    "candidate_id": row["candidate_id"],
                    "relevance": "NONE",
                    "materiality": "LOW",
                    "urgency": "NONE",
                    "evidence_unknowns": [],
                    "summary": "screened",
                }
                for row in json.loads(body)["candidates"]
            ],
        },
        separators=(",", ":"),
    ).encode()


def _sol_alert_citing(evidence_by_candidate):
    def stub(body):
        return json.dumps(
            {
                "schema_version": 1,
                "analyses": [
                    {
                        "candidate_id": row["candidate_id"],
                        "verdict": "ALERT_WORTHY",
                        "proposed_action": "ALERT",
                        "rationale": "verified",
                        "cited_evidence_ids": [
                            evidence_by_candidate[row["candidate_id"]],
                        ],
                        "confidence": "1",
                        "disagreements_with_glm": [],
                    }
                    for row in json.loads(body)["candidates"]
                ],
            },
            separators=(",", ":"),
        ).encode()

    return stub


def _run(home, *, glm_call, sol_call):
    return run_c_analysis_shadow(
        home=home,
        utc_day=UTC_DAY,
        open_markets=KR_ONLY,
        glm_call=glm_call,
        sol_call=sol_call,
        clock=SteppingClock(),
    )


def test_one_shot_entrypoint_runs_existing_barrier_then_analysis_and_g2b_stays_data_only(
    tmp_path,
):
    root = _seed_shadow_materials(tmp_path)
    evidence = {
        row.candidate_id: row.evidence_refs[0].evidence_id
        for row in _expected_candidates(root)
    }
    report = _run(
        tmp_path,
        glm_call=_glm_screen_all_none,
        sol_call=_sol_alert_citing(evidence),
    )
    assert report.run_state == "COMPLETED"
    assert report.run_id == f"g2a-a-{UTC_DAY.isoformat()}"
    assert report.tick_id == TICK.tick_id

    # The run went through the existing Slice E pipeline against the shadow
    # kernel: sealed manifest, settled batches, dispositions for every
    # candidate the stored packs yield.
    store = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")
    assert store.read_manifest_summary(TICK.tick_id).state.value == "SEALED"
    assert len(store.read_dispositions_for_tick(TICK.tick_id)) == 2

    # G2-B stays data-only: its stage order still truncates before the
    # barrier/analysis stages and its module imports no analysis stage.
    assert g2b_canary_runner._B_STAGE_ORDER == (
        "calendar_provider",
        "session_gate",
        "holdings_runner",
        "quotes_runner",
        "trades_runner",
    )
    imports = {
        node.module
        for node in ast.walk(ast.parse(inspect.getsource(g2b_canary_runner)))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(
        "analysis_barrier" in name or "analysis_pipeline" in name for name in imports
    )


def test_scripted_glm_sol_flow_reaches_validated_shadow_disposition_with_zero_live_surface(
    tmp_path,
):
    root = _seed_shadow_materials(tmp_path)
    candidates = _expected_candidates(root)
    critical = next(
        row for row in candidates if row.assignment is CandidateAssignment.CRITICAL
    )
    evidence = {
        row.candidate_id: row.evidence_refs[0].evidence_id for row in candidates
    }
    glm_bodies, sol_bodies = [], []

    def glm(body):
        glm_bodies.append(body)
        return _glm_screen_all_none(body)

    scripted_sol = _sol_alert_citing(evidence)

    def sol(body):
        sol_bodies.append(body)
        return scripted_sol(body)

    # The suite-wide autouse fixture already fails any socket open, so the
    # whole run below is proven network-zero; the live provider factories the
    # entrypoint wires are refuse-on-touch, so no order/data provider can even
    # be constructed.
    report = _run(tmp_path, glm_call=glm, sol_call=sol)
    assert report.run_state == "COMPLETED"

    # Conditional SOL: GLM saw every candidate, screened the quote candidate
    # to NONE/LOW, and only the CRITICAL completed-execution candidate
    # reached SOL.
    assert {row["candidate_id"] for row in json.loads(glm_bodies[0])["candidates"]} == {
        row.candidate_id for row in candidates
    }
    assert len(sol_bodies) == 1
    assert [row["candidate_id"] for row in json.loads(sol_bodies[0])["candidates"]] == [
        critical.candidate_id
    ]

    # Redaction reuse: account identity never crosses the model boundary.
    assert "account_scope" not in glm_bodies[0].decode()
    assert ACCOUNT not in glm_bodies[0].decode()
    assert ACCOUNT not in sol_bodies[0].decode()

    # The SEND survived the validator with its cited fresh evidence; the
    # screened quote candidate settled as SKIP.
    store = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")
    by_action = {
        row.action.value: row for row in store.read_dispositions_for_tick(TICK.tick_id)
    }
    assert set(by_action) == {"SEND", "SKIP"}
    assert by_action["SEND"].candidate_id == critical.candidate_id
    assert by_action["SEND"].reason_code == "evidence_verified_send"
    assert by_action["SEND"].cited_evidence_ids == (evidence[critical.candidate_id],)
    assert by_action["SKIP"].reason_code == "glm_screened_none"

    # Durable batch rows settled OK, and the only alert surface is SHADOW —
    # no Telegram row, no order-write channel, nothing scheduled.
    batches = store.read_analysis_batches(TICK.tick_id)
    assert [row["provider"] for row in batches] == ["GLM", "SOL"]
    assert all(row["state"] == "SETTLED" and row["exit"] == "OK" for row in batches)
    connection = sqlite3.connect(root / "ops" / "ops_kernel.sqlite3")
    try:
        channels = [
            row[0] for row in connection.execute("SELECT channel FROM alert_outbox")
        ]
    finally:
        connection.close()
    assert channels == ["SHADOW"]


def test_report_records_candidate_model_call_disposition_and_duration_metrics(tmp_path):
    root = _seed_shadow_materials(tmp_path)
    evidence = {
        row.candidate_id: row.evidence_refs[0].evidence_id
        for row in _expected_candidates(root)
    }
    report = _run(
        tmp_path,
        glm_call=_glm_screen_all_none,
        sol_call=_sol_alert_citing(evidence),
    )
    assert report.utc_day == UTC_DAY.isoformat()
    assert report.candidate_count == 2
    assert report.glm_calls == 1
    assert report.sol_calls == 1
    assert dict(report.disposition_counts) == {"SEND": 1, "SKIP": 1}
    assert report.glm_seconds > 0
    assert report.sol_seconds > 0
    assert report.total_seconds >= report.glm_seconds + report.sol_seconds
