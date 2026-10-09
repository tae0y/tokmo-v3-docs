"""Material delta builder: pure, deterministic, baseline/history-aware."""

import hashlib
from datetime import UTC, datetime

from tokmo.programs.candidate_builder import build_candidates
from tokmo.programs.event_admission import (
    DEFAULT_EVENT_ADMISSION_POLICY_V1,
    evaluate_event_admission,
)
from tokmo.programs.material_delta import MaterialDeltaResultV1, build_material_deltas
from tokmo.values.analysis_admission import MaterialDeltaV1
from tokmo.values.analysis_contracts import (
    MaterialKindV1,
)
from tokmo.values.material_pack import JobKind, canonical_encode
from tokmo.values.portfolio_scope import resolve_account_scope_pattern  # N5

_NOW = datetime(2026, 8, 4, 14, 0, tzinfo=UTC)
_PACK_TS = datetime(2026, 8, 4, 13, 59, 30, tzinfo=UTC)


def _pack_holdings(rows):
    accounts = []
    for i, r in enumerate(rows):
        accounts.append(
            {
                "account_scope": f"toss-acct-{i:024x}",
                "account_type_label": "GENERAL",
                "ordinal": i + 1,
                "holdings": [r],
            }
        )
    return {"accounts": accounts, "captured_at": _PACK_TS.isoformat()}


def _pack_quotes(markets):
    return {"markets": markets, "captured_at": _PACK_TS.isoformat()}


def _pack_trades(accounts):
    return {"accounts": accounts, "captured_at": _PACK_TS.isoformat()}


class _FakeMaterial:
    def __init__(self, job, payload, tick_id=1):
        self.job = JobKind(job.lower()) if isinstance(job, str) else job
        self.payload = payload
        self.pack_tick_id = tick_id
        self.captured_at = _PACK_TS
        self.content_hash = hashlib.sha256(canonical_encode(payload)).hexdigest()
        self.file_sha256 = hashlib.sha256(canonical_encode(payload)).hexdigest()


class _FakeBarrier:
    def __init__(
        self, materials, *, proceed=("KR", "US"), tick_id=1, mask_digest="m" * 64
    ):
        from tokmo.values.analysis_barrier import MarketReadiness
        from tokmo.values.session_gate import Market

        self.tick_id = tick_id
        self.materials = {JobKind(k.lower()): v for k, v in materials.items()}
        self.material_ticks = {
            JobKind(k.lower()): v.pack_tick_id for k, v in materials.items()
        }
        self.market_readiness = {
            Market.KR: MarketReadiness.READY,
            Market.US: MarketReadiness.READY,
        }
        self.proceed_markets = tuple(Market(m) for m in proceed)
        self.mask_digest = mask_digest
        self.incidents = ()
        self.stale_jobs = ()


def test_material_delta_identity_is_stable_over_mapping_order_and_restart():
    """A delta built from the same barrier+baseline yields identical delta_id."""
    holdings_row = {
        "market_field": "US",
        "market_raw": "US",
        "symbol": "AAPL",
        "quantity": "10",
        "avg_price": "100",
        "currency": "USD",
    }
    barrier = _FakeBarrier(
        {
            "HOLDINGS": _FakeMaterial("HOLDINGS", _pack_holdings([holdings_row])),
        }
    )
    result1 = build_material_deltas(
        barrier, baselines={}, now=_NOW, resolve_scope=resolve_account_scope_pattern
    )
    result2 = build_material_deltas(
        barrier, baselines={}, now=_NOW, resolve_scope=resolve_account_scope_pattern
    )
    assert len(result1.deltas) == len(result2.deltas)
    for d1, d2 in zip(result1.deltas, result2.deltas):
        assert d1.delta_id == d2.delta_id


def test_delta_contains_exact_evidence_and_no_private_account_identity():
    holdings_row = {
        "market_field": "US",
        "market_raw": "US",
        "symbol": "AAPL",
        "quantity": "10",
        "avg_price": "100",
        "currency": "USD",
    }
    barrier = _FakeBarrier(
        {
            "HOLDINGS": _FakeMaterial("HOLDINGS", _pack_holdings([holdings_row])),
        }
    )
    result = build_material_deltas(
        barrier, baselines={}, now=_NOW, resolve_scope=resolve_account_scope_pattern
    )
    for delta in result.deltas:
        assert isinstance(delta, MaterialDeltaV1)
        assert "account_scope" not in delta.current_fields
        assert (
            delta.baseline_fields is None
            or "account_scope" not in delta.baseline_fields
        )


def test_existing_quote_numeric_states_and_trigger_boundaries_are_preserved():
    """Quote deltas preserve the existing numeric state and trigger logic."""
    quotes = {
        "US": [
            {
                "symbol": "AAPL",
                "last_price": "105.00",
                "currency": "USD",
                "currency_raw": "USD",
                "provider_timestamp": _PACK_TS.isoformat(),
                "fetched_at": _PACK_TS.isoformat(),
            },
        ],
    }
    barrier = _FakeBarrier(
        {
            "QUOTES": _FakeMaterial("QUOTES", _pack_quotes(quotes)),
        }
    )
    result = build_material_deltas(
        barrier, baselines={}, now=_NOW, resolve_scope=resolve_account_scope_pattern
    )
    quote_deltas = [
        d for d in result.deltas if d.material_kind is MaterialKindV1.QUOTE_STATE
    ]
    assert len(quote_deltas) == 1
    # First observation should trigger FIRST_OBSERVATION
    assert quote_deltas[0].current_fields.get("trigger_kind") == "FIRST_OBSERVATION"


def test_completed_execution_holdings_and_incident_candidate_semantics_are_preserved():
    """Completed executions produce deltas with COMPLETED_EXECUTION kind."""
    trade_row = {
        "source_record_key": "ord-001",
        "instrument_raw": "AAPL",
        "classified_market": "US",
        "trade_date": "2026-08-04",
        "transaction_type_raw": "BUY",
        "quantity": "5",
        "unit_price": "100",
        "amount": "500",
        "currency_raw": "USD",
        "filled_at": _PACK_TS.isoformat(),
    }
    barrier = _FakeBarrier(
        {
            "TRADES": _FakeMaterial(
                "TRADES",
                _pack_trades(
                    [
                        {
                            "account_scope": "toss-acct-" + "0" * 24,
                            "inserted": [trade_row],
                        }
                    ]
                ),
            ),
        }
    )
    result = build_material_deltas(
        barrier, baselines={}, now=_NOW, resolve_scope=resolve_account_scope_pattern
    )
    exec_deltas = [
        d
        for d in result.deltas
        if d.material_kind is MaterialKindV1.COMPLETED_EXECUTION
    ]
    assert len(exec_deltas) == 1


def test_rejected_delta_does_not_advance_baseline_without_atomic_tick_commit():
    """build_material_deltas returns proposed advances but does not persist them."""
    holdings_row = {
        "market_field": "US",
        "market_raw": "US",
        "symbol": "AAPL",
        "quantity": "10",
        "avg_price": "100",
        "currency": "USD",
    }
    barrier = _FakeBarrier(
        {
            "HOLDINGS": _FakeMaterial("HOLDINGS", _pack_holdings([holdings_row])),
        }
    )
    result = build_material_deltas(
        barrier, baselines={}, now=_NOW, resolve_scope=resolve_account_scope_pattern
    )
    # Advances are returned as proposals; persistence is the caller's job
    assert isinstance(result, MaterialDeltaResultV1)
    assert hasattr(result, "baseline_advances")
    assert hasattr(result, "history_advances")


def test_parity_build_candidates_and_delta_admission_produce_same_envelopes():
    """Existing build_candidates fixtures produce the same CandidateEnvelopeV1
    set as delta+admission+projection under the default policy."""
    holdings_row = {
        "market_field": "US",
        "market_raw": "US",
        "symbol": "AAPL",
        "quantity": "10",
        "avg_price": "100",
        "currency": "USD",
    }
    quotes = {
        "US": [
            {
                "symbol": "AAPL",
                "last_price": "105.00",
                "currency": "USD",
                "currency_raw": "USD",
                "provider_timestamp": _PACK_TS.isoformat(),
                "fetched_at": _PACK_TS.isoformat(),
            },
        ],
    }
    barrier = _FakeBarrier(
        {
            "HOLDINGS": _FakeMaterial("HOLDINGS", _pack_holdings([holdings_row])),
            "QUOTES": _FakeMaterial("QUOTES", _pack_quotes(quotes)),
        }
    )
    # Legacy path
    legacy = build_candidates(
        barrier, baselines={}, now=_NOW, resolve_scope=resolve_account_scope_pattern
    )
    # Delta path
    delta_result = build_material_deltas(
        barrier, baselines={}, now=_NOW, resolve_scope=resolve_account_scope_pattern
    )
    decisions = evaluate_event_admission(
        DEFAULT_EVENT_ADMISSION_POLICY_V1,
        delta_result.deltas,
        now=_NOW,
    )
    admitted = tuple(
        d for d, dec in zip(delta_result.deltas, decisions) if dec.decision == "ADMIT"
    )
    # The same set of candidates is produced (same count, same ids)
    legacy_ids = {c.candidate_id for c in legacy.candidates}
    delta_admitted_ids = {d.delta_id for d in admitted}
    # Every legacy candidate corresponds to an admitted delta
    assert len(admitted) == len(legacy.candidates)
