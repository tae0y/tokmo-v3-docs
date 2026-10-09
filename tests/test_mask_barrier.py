"""Stage 2-E: barrier/candidate material carries the exact mask identity.

The barrier binds to one OpenMarketMaskV1: open markets come from the mask
(never re-judged), same-tick material must carry the exact mask digest, and a
fact from a market outside the mask fails closed before any analysis
transition — it is never silently filtered afterwards.
"""

from datetime import UTC, date, datetime

import pytest

from tokmo.programs.analysis_barrier import assemble_analysis_materials
from tokmo.programs.candidate_builder import build_candidates, material_binding_digest
from tokmo.programs.legacy_g2 import legacy_assemble_analysis_materials
from tokmo.values.analysis_barrier import MarketReadiness
from tokmo.values.cadence_policy import cadence_tick_v1
from tokmo.values.material_pack import JobKind, build_material_pack_v1
from tokmo.values.open_market_mask import MaskBindingError, build_open_market_mask
from tokmo.values.portfolio_scope import resolve_account_scope_pattern  # N5
from tokmo.values.receipt_v1 import (
    AttemptStatus,
    AttemptV1,
    CompletenessV1,
    LastSuccessV1,
    MarketFreshness,
    ReceiptV1,
)
from tokmo.values.session_gate import Market, SessionVerdict
from tokmo.values.toss_market_calendar import CalendarSession

NOW = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
TICK = cadence_tick_v1("cadence-300s-v1", NOW)
NEXT_TICK = cadence_tick_v1(
    "cadence-300s-v1", NOW + __import__("datetime").timedelta(seconds=300)
)
SESSION = CalendarSession(
    datetime(2026, 8, 3, 13, 30, tzinfo=UTC),
    datetime(2026, 8, 3, 20, 0, tzinfo=UTC),
)


def mask_for(kr_reason="after_close", us_reason="open", *, tick=None):
    tick = TICK if tick is None else tick
    verdicts = (
        SessionVerdict(Market.KR, kr_reason == "open", kr_reason, SESSION),
        SessionVerdict(Market.US, us_reason == "open", us_reason, SESSION),
    )
    return build_open_market_mask(
        policy_version=tick.policy_version,
        scheduled_for_utc=tick.scheduled_for_utc,
        tick_id=tick.tick_id,
        verdicts=verdicts,
        local_dates={Market.KR: date(2026, 8, 4), Market.US: date(2026, 8, 3)},
    )


US_ONLY = mask_for()


def payload(job):
    if job is JobKind.HOLDINGS:
        return {"accounts": [], "global_inventory": True}
    if job is JobKind.QUOTES:
        return {"markets": {"US": []}}
    return {"accounts": [], "pagination_pending": False}


def pack(job, *, mask_digest, market_partition=("US",), tick_id=None):
    return build_material_pack_v1(
        schema_version=1,
        job=job,
        tick_id=TICK.tick_id if tick_id is None else tick_id,
        market_partition=market_partition,
        payload=payload(job),
        captured_at=NOW,
        source_timestamps={"provider": None, "fetched": None, "stored": None},
        parser_version="fixture-v1",
        mask_digest=mask_digest,
    )


def receipt(job, *, mask_digest, tick_id=None):
    tick_id = TICK.tick_id if tick_id is None else tick_id
    markets = {
        market: MarketFreshness(
            market, "open" if market == "US" else "closed", tick_id, NOW
        )
        for market in ("KR", "US")
    }
    return ReceiptV1(
        1,
        job,
        AttemptV1(tick_id, AttemptStatus.SAVED, "saved", NOW),
        LastSuccessV1(
            tick_id,
            f"packs/{job.value}/{tick_id}.json",
            {JobKind.HOLDINGS: "a", JobKind.QUOTES: "b", JobKind.TRADES: "c"}[job] * 64,
            NOW,
            NOW,
        ),
        markets,
        CompletenessV1(0, 0, False, 0, 0) if job is JobKind.TRADES else None,
        mask_digest=mask_digest,
    )


def inputs(
    *,
    pack_digest=US_ONLY.digest,
    receipt_digest=US_ONLY.digest,
    partitions=None,
    calls=None,
):
    partitions = partitions or {}
    receipts = {job: receipt(job, mask_digest=receipt_digest) for job in JobKind}
    packs = {
        job: pack(
            job, mask_digest=pack_digest, market_partition=partitions.get(job, ("US",))
        )
        for job in JobKind
    }

    def pack_reader(path, expected_sha256):
        job = next(job for job in JobKind if f"/{job.value}/" in path)
        return packs[job]

    return {job: (lambda job=job: receipts[job]) for job in JobKind}, pack_reader


def test_masked_barrier_binds_and_carries_mask_digest():
    readers, pack_reader = inputs()
    result = assemble_analysis_materials(
        tick=TICK,
        mask=US_ONLY,
        receipt_readers=readers,
        pack_reader=pack_reader,
        now=NOW,
    )
    assert result.mask_digest == US_ONLY.digest
    assert result.proceed_markets == frozenset({Market.US})
    assert result.market_readiness[Market.KR] is MarketReadiness.SKIP_CLOSED
    assert result.market_readiness[Market.US] is MarketReadiness.READY


def test_masked_barrier_rejects_tick_mismatch():
    readers, pack_reader = inputs()
    with pytest.raises(MaskBindingError):
        assemble_analysis_materials(
            tick=TICK,
            mask=mask_for(tick=NEXT_TICK),
            receipt_readers=readers,
            pack_reader=pack_reader,
            now=NOW,
        )


def test_masked_barrier_accepts_no_caller_chosen_open_markets():
    # M1: the production barrier has no verdict-reinterpretation parameter at
    # all — a caller-supplied open set is a TypeError, not a soft cross-check.
    readers, pack_reader = inputs()
    with pytest.raises(TypeError):
        assemble_analysis_materials(
            tick=TICK,
            mask=US_ONLY,
            open_markets=frozenset({Market.KR}),
            receipt_readers=readers,
            pack_reader=pack_reader,
            now=NOW,
        )


def test_both_closed_mask_reads_nothing_and_keeps_digest():
    calls = []
    readers = {job: (lambda job=job: calls.append(job)) for job in JobKind}
    result = assemble_analysis_materials(
        tick=TICK,
        mask=mask_for("after_close", "after_close"),
        receipt_readers=readers,
        pack_reader=lambda *a: pytest.fail("no packs"),
        now=NOW,
    )
    assert calls == []
    assert result.materials == {}
    assert result.mask_digest == mask_for("after_close", "after_close").digest


def test_same_tick_pack_with_wrong_or_missing_mask_digest_fails_closed():
    readers, pack_reader = inputs(pack_digest="0" * 64)
    with pytest.raises(MaskBindingError):
        assemble_analysis_materials(
            tick=TICK,
            mask=US_ONLY,
            receipt_readers=readers,
            pack_reader=pack_reader,
            now=NOW,
        )
    readers, pack_reader = inputs(pack_digest=None)
    with pytest.raises(MaskBindingError):
        assemble_analysis_materials(
            tick=TICK,
            mask=US_ONLY,
            receipt_readers=readers,
            pack_reader=pack_reader,
            now=NOW,
        )


def test_same_tick_receipt_with_wrong_mask_digest_fails_closed():
    readers, pack_reader = inputs(receipt_digest="0" * 64)
    with pytest.raises(MaskBindingError):
        assemble_analysis_materials(
            tick=TICK,
            mask=US_ONLY,
            receipt_readers=readers,
            pack_reader=pack_reader,
            now=NOW,
        )


def test_same_tick_material_from_closed_market_fails_closed_not_filtered():
    readers, pack_reader = inputs(
        partitions={JobKind.QUOTES: ("KR", "US")},
    )
    with pytest.raises(MaskBindingError):
        assemble_analysis_materials(
            tick=TICK,
            mask=US_ONLY,
            receipt_readers=readers,
            pack_reader=pack_reader,
            now=NOW,
        )


# -- B4: prior-tick material needs an admissibility proof for the current mask ----


PRIOR_NOW = datetime(2026, 8, 3, 13, 55, tzinfo=UTC)


def prior_pack(job, *, mask_digest, payload_override=None):
    return build_material_pack_v1(
        schema_version=1,
        job=job,
        tick_id=TICK.tick_id - 1,
        market_partition=("US",),
        payload=payload_override if payload_override is not None else payload(job),
        captured_at=PRIOR_NOW,
        source_timestamps={"provider": None, "fetched": None, "stored": None},
        parser_version="fixture-v1",
        mask_digest=mask_digest,
    )


def prior_receipt(job, *, attempt_tick, attempt_digest, success_tick):
    markets = {
        market: MarketFreshness(
            market,
            "open" if market == "US" else "closed",
            success_tick,
            PRIOR_NOW if success_tick != TICK.tick_id else NOW,
        )
        for market in ("KR", "US")
    }
    return ReceiptV1(
        1,
        job,
        AttemptV1(attempt_tick, AttemptStatus.SAVED, "saved", NOW),
        LastSuccessV1(
            success_tick,
            f"packs/{job.value}/{success_tick}.json",
            {JobKind.HOLDINGS: "a", JobKind.QUOTES: "b", JobKind.TRADES: "c"}[job] * 64,
            PRIOR_NOW if success_tick != TICK.tick_id else NOW,
            NOW,
        ),
        markets,
        CompletenessV1(0, 0, False, 0, 0) if job is JobKind.TRADES else None,
        mask_digest=attempt_digest,
    )


CLOSED_KR_TRADE_ROW = {
    "source_record_key": "kr-hist-1",
    "trade_date": "2026-08-03",
    "transaction_type_raw": "SELL",
    "instrument_raw": "005930",
    "quantity": "1",
    "unit_price": "70000",
    "amount": "70000",
    "medium_raw": "APP",
    "currency_raw": "KRW",
    "classified_market": "KR",
    "filled_at": "2026-08-03T05:00:00Z",
}


def mixed_inputs(*, trades_pack, trades_receipt):
    receipts = {
        JobKind.HOLDINGS: receipt(JobKind.HOLDINGS, mask_digest=US_ONLY.digest),
        JobKind.QUOTES: receipt(JobKind.QUOTES, mask_digest=US_ONLY.digest),
        JobKind.TRADES: trades_receipt,
    }
    packs = {
        JobKind.HOLDINGS: pack(JobKind.HOLDINGS, mask_digest=US_ONLY.digest),
        JobKind.QUOTES: pack(JobKind.QUOTES, mask_digest=US_ONLY.digest),
        JobKind.TRADES: trades_pack,
    }

    def pack_reader(path, expected_sha256):
        job = next(job for job in JobKind if f"/{job.value}/" in path)
        return packs[job]

    return {job: (lambda job=job: receipts[job]) for job in JobKind}, pack_reader


def closed_row_trades_payload():
    return {
        "accounts": [
            {
                "account_scope": "toss-acct-" + "c" * 24,
                "inserted": [dict(CLOSED_KR_TRADE_ROW)],
                "duplicates": [],
                "quarantine": [],
                "incomplete": [],
            }
        ],
        "pagination_pending": False,
    }


@pytest.mark.parametrize("historical_digest", [None, "f" * 64])
def test_prior_tick_trades_material_without_current_mask_proof_is_not_admitted(
    historical_digest,
):
    """A prior-tick pack (missing or foreign digest) has no admissibility
    proof for the current mask: it is excluded before candidate creation and
    its closed-market rows can never become candidates."""
    readers, pack_reader = mixed_inputs(
        trades_pack=prior_pack(
            JobKind.TRADES,
            mask_digest=historical_digest,
            payload_override=closed_row_trades_payload(),
        ),
        trades_receipt=prior_receipt(
            JobKind.TRADES,
            attempt_tick=TICK.tick_id,
            attempt_digest=US_ONLY.digest,
            success_tick=TICK.tick_id - 1,
        ),
    )
    result = assemble_analysis_materials(
        tick=TICK,
        mask=US_ONLY,
        receipt_readers=readers,
        pack_reader=pack_reader,
        now=NOW,
    )
    assert JobKind.TRADES not in result.materials
    assert result.material_ticks[JobKind.TRADES] is None
    assert "trades_material_unbound" in result.incidents
    assert JobKind.TRADES in result.stale_jobs[Market.US]
    built = build_candidates(
        result, baselines={}, now=NOW, resolve_scope=resolve_account_scope_pattern
    )
    assert built.candidates == ()  # candidate 0 — the closed KR row is gone


def test_prior_tick_receipt_without_current_mask_proof_is_not_admitted():
    """A receipt whose attempt is a prior tick (collector never ran this
    tick) carries a foreign digest: no admissibility proof, material 0."""
    readers, pack_reader = mixed_inputs(
        trades_pack=prior_pack(JobKind.TRADES, mask_digest="f" * 64),
        trades_receipt=prior_receipt(
            JobKind.TRADES,
            attempt_tick=TICK.tick_id - 1,
            attempt_digest="f" * 64,
            success_tick=TICK.tick_id - 1,
        ),
    )
    result = assemble_analysis_materials(
        tick=TICK,
        mask=US_ONLY,
        receipt_readers=readers,
        pack_reader=pack_reader,
        now=NOW,
    )
    assert JobKind.TRADES not in result.materials
    assert "trades_material_unbound" in result.incidents


def test_prior_tick_holdings_without_proof_yields_candidate_and_analysis_zero():
    receipts = {
        JobKind.HOLDINGS: prior_receipt(
            JobKind.HOLDINGS,
            attempt_tick=TICK.tick_id - 1,
            attempt_digest=None,
            success_tick=TICK.tick_id - 1,
        ),
        JobKind.QUOTES: receipt(JobKind.QUOTES, mask_digest=US_ONLY.digest),
        JobKind.TRADES: receipt(JobKind.TRADES, mask_digest=US_ONLY.digest),
    }
    packs = {
        JobKind.HOLDINGS: prior_pack(JobKind.HOLDINGS, mask_digest=None),
        JobKind.QUOTES: pack(JobKind.QUOTES, mask_digest=US_ONLY.digest),
        JobKind.TRADES: pack(JobKind.TRADES, mask_digest=US_ONLY.digest),
    }

    def pack_reader(path, expected_sha256):
        job = next(job for job in JobKind if f"/{job.value}/" in path)
        return packs[job]

    result = assemble_analysis_materials(
        tick=TICK,
        mask=US_ONLY,
        receipt_readers={job: (lambda job=job: receipts[job]) for job in JobKind},
        pack_reader=pack_reader,
        now=NOW,
    )
    assert JobKind.HOLDINGS not in result.materials
    assert "holdings_material_unbound" in result.incidents
    assert result.market_readiness[Market.US] is MarketReadiness.UNAVAILABLE
    assert result.proceed_markets == frozenset()  # analysis 0
    built = build_candidates(
        result, baselines={}, now=NOW, resolve_scope=resolve_account_scope_pattern
    )
    assert built.candidates == ()  # candidate 0


def test_masked_candidate_builder_rejects_closed_market_trade_row():
    """Defense in depth at the candidate boundary: even if a closed-market
    row reached a masked barrier result, it is a tick contract failure —
    never an EXPLICITLY_DEFERRED candidate."""
    from tokmo.values.analysis_barrier import (
        AnalysisBarrierResultV1,
        VerifiedMaterialV1,
    )

    material = VerifiedMaterialV1(
        JobKind.TRADES,
        TICK.tick_id,
        NOW,
        "c" * 64,
        "d" * 64,
        closed_row_trades_payload(),
        ("US",),
    )
    barrier = AnalysisBarrierResultV1(
        TICK.tick_id,
        {JobKind.TRADES: material},
        {Market.KR: MarketReadiness.SKIP_CLOSED, Market.US: MarketReadiness.READY},
        {market: frozenset() for market in Market},
        {JobKind.HOLDINGS: None, JobKind.QUOTES: None, JobKind.TRADES: TICK.tick_id},
        (),
        frozenset({Market.US}),
        mask_digest=US_ONLY.digest,
    )
    with pytest.raises(MaskBindingError):
        build_candidates(
            barrier, baselines={}, now=NOW, resolve_scope=resolve_account_scope_pattern
        )
    legacy = AnalysisBarrierResultV1(
        TICK.tick_id,
        {JobKind.TRADES: material},
        {Market.KR: MarketReadiness.SKIP_CLOSED, Market.US: MarketReadiness.READY},
        {market: frozenset() for market in Market},
        {JobKind.HOLDINGS: None, JobKind.QUOTES: None, JobKind.TRADES: TICK.tick_id},
        (),
        frozenset({Market.US}),
        mask_digest=None,
    )
    deferred = build_candidates(
        legacy, baselines={}, now=NOW, resolve_scope=resolve_account_scope_pattern
    )
    assert [item.assignment.value for item in deferred.candidates] == [
        "EXPLICITLY_DEFERRED",
    ]


# -- M2: mask local_date + IANA ZoneInfo own masked market-date freshness ---------


def dated_mask(us_local_date, *, us_reason="open"):
    verdicts = (
        SessionVerdict(Market.KR, False, "after_close", SESSION),
        SessionVerdict(Market.US, us_reason == "open", us_reason, SESSION),
    )
    return build_open_market_mask(
        policy_version="cadence-300s-v1",
        scheduled_for_utc=TICK.start_utc,
        tick_id=TICK.tick_id,
        verdicts=verdicts,
        local_dates={Market.KR: date(2026, 8, 4), Market.US: us_local_date},
    )


def dated_inputs(mask, captured_at):
    markets = {
        market: MarketFreshness(
            market,
            "open" if market == "US" else "closed",
            TICK.tick_id,
            captured_at,
        )
        for market in ("KR", "US")
    }
    receipts = {
        job: ReceiptV1(
            1,
            job,
            AttemptV1(TICK.tick_id, AttemptStatus.SAVED, "saved", NOW),
            LastSuccessV1(
                TICK.tick_id,
                f"packs/{job.value}/{TICK.tick_id}.json",
                {JobKind.HOLDINGS: "a", JobKind.QUOTES: "b", JobKind.TRADES: "c"}[job]
                * 64,
                captured_at,
                NOW,
            ),
            markets,
            CompletenessV1(0, 0, False, 0, 0) if job is JobKind.TRADES else None,
            mask_digest=mask.digest,
        )
        for job in JobKind
    }
    packs = {
        job: build_material_pack_v1(
            schema_version=1,
            job=job,
            tick_id=TICK.tick_id,
            market_partition=("US",),
            payload=payload(job),
            captured_at=captured_at,
            source_timestamps={"provider": None, "fetched": None, "stored": None},
            parser_version="fixture-v1",
            mask_digest=mask.digest,
        )
        for job in JobKind
    }

    def pack_reader(path, expected_sha256):
        job = next(job for job in JobKind if f"/{job.value}/" in path)
        return packs[job]

    return {job: (lambda job=job: receipts[job]) for job in JobKind}, pack_reader


def test_masked_freshness_authority_is_the_mask_local_date_not_now():
    """Mask says US local date 2026-08-03; captured instants on that NY date
    are fresh even when the barrier's ``now`` has crossed into the next NY
    date. The mask, not host-now reinterpretation, owns the session date."""
    mask = dated_mask(date(2026, 8, 3))
    captured = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)  # NY 10:00 EDT
    readers, pack_reader = dated_inputs(mask, captured)
    late_now = datetime(2026, 8, 4, 6, 0, tzinfo=UTC)  # NY 02:00 Aug 4
    result = assemble_analysis_materials(
        tick=TICK,
        mask=mask,
        receipt_readers=readers,
        pack_reader=pack_reader,
        now=late_now,
    )
    assert result.market_readiness[Market.US] is MarketReadiness.READY


def test_masked_freshness_rejects_capture_outside_mask_local_date():
    # NY 2026-08-02 23:30 EDT == 2026-08-03T03:30Z: previous NY local date.
    mask = dated_mask(date(2026, 8, 3))
    captured = datetime(2026, 8, 3, 3, 30, tzinfo=UTC)
    readers, pack_reader = dated_inputs(mask, captured)
    result = assemble_analysis_materials(
        tick=TICK,
        mask=mask,
        receipt_readers=readers,
        pack_reader=pack_reader,
        now=NOW,
    )
    assert result.market_readiness[Market.US] is MarketReadiness.SKIP_HOLDINGS_MISSING


def test_masked_freshness_uses_zoneinfo_across_dst_and_kr_boundary():
    """EDT (-4) boundary: 2026-08-04T03:59Z is still NY Aug 3 (23:59 EDT);
    a fixed winter offset (-5) or a KR-style +9 would misdate it."""
    mask = dated_mask(date(2026, 8, 3))
    captured = datetime(2026, 8, 4, 3, 59, tzinfo=UTC)
    readers, pack_reader = dated_inputs(mask, captured)
    result = assemble_analysis_materials(
        tick=TICK,
        mask=mask,
        receipt_readers=readers,
        pack_reader=pack_reader,
        now=NOW,
    )
    assert result.market_readiness[Market.US] is MarketReadiness.READY
    # One minute later NY crosses to Aug 4: no longer the mask's date.
    readers, pack_reader = dated_inputs(
        mask,
        datetime(2026, 8, 4, 4, 0, tzinfo=UTC),
    )
    result = assemble_analysis_materials(
        tick=TICK,
        mask=mask,
        receipt_readers=readers,
        pack_reader=pack_reader,
        now=NOW,
    )
    assert result.market_readiness[Market.US] is MarketReadiness.SKIP_HOLDINGS_MISSING


def test_masked_freshness_non_dst_uses_est_offset():
    """Non-DST check through ZoneInfo: 2026-01-16T04:59Z is NY Jan 15
    23:59 EST (-5). A DST-season offset (-4) would call it Jan 16."""
    kr_session = CalendarSession(
        datetime(2026, 1, 15, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 15, 6, 30, tzinfo=UTC),
    )
    us_session = CalendarSession(
        datetime(2026, 1, 15, 14, 30, tzinfo=UTC),
        datetime(2026, 1, 15, 21, 0, tzinfo=UTC),
    )
    winter_now = datetime(2026, 1, 15, 15, 0, tzinfo=UTC)
    winter_tick = cadence_tick_v1("cadence-300s-v1", winter_now)
    verdicts = (
        SessionVerdict(Market.KR, False, "after_close", kr_session),
        SessionVerdict(Market.US, True, "open", us_session),
    )
    mask = build_open_market_mask(
        policy_version="cadence-300s-v1",
        scheduled_for_utc=winter_tick.start_utc,
        tick_id=winter_tick.tick_id,
        verdicts=verdicts,
        local_dates={Market.KR: date(2026, 1, 15), Market.US: date(2026, 1, 15)},
    )
    markets = {
        market: MarketFreshness(
            market,
            "open" if market == "US" else "closed",
            winter_tick.tick_id,
            winter_now,
        )
        for market in ("KR", "US")
    }
    receipts = {
        job: ReceiptV1(
            1,
            job,
            AttemptV1(winter_tick.tick_id, AttemptStatus.SAVED, "saved", winter_now),
            LastSuccessV1(
                winter_tick.tick_id,
                f"packs/{job.value}/{winter_tick.tick_id}.json",
                {JobKind.HOLDINGS: "a", JobKind.QUOTES: "b", JobKind.TRADES: "c"}[job]
                * 64,
                winter_now,
                winter_now,
            ),
            markets,
            CompletenessV1(0, 0, False, 0, 0) if job is JobKind.TRADES else None,
            mask_digest=mask.digest,
        )
        for job in JobKind
    }
    packs = {
        job: build_material_pack_v1(
            schema_version=1,
            job=job,
            tick_id=winter_tick.tick_id,
            market_partition=("US",),
            payload=payload(job),
            captured_at=winter_now,
            source_timestamps={"provider": None, "fetched": None, "stored": None},
            parser_version="fixture-v1",
            mask_digest=mask.digest,
        )
        for job in JobKind
    }

    def pack_reader(path, expected_sha256):
        job = next(job for job in JobKind if f"/{job.value}/" in path)
        return packs[job]

    result = assemble_analysis_materials(
        tick=winter_tick,
        mask=mask,
        receipt_readers={job: (lambda job=job: receipts[job]) for job in JobKind},
        pack_reader=pack_reader,
        now=winter_now,
    )
    assert result.market_readiness[Market.US] is MarketReadiness.READY


def test_material_binding_digest_and_candidates_carry_mask_identity():
    readers, pack_reader = inputs()
    masked = assemble_analysis_materials(
        tick=TICK,
        mask=US_ONLY,
        receipt_readers=readers,
        pack_reader=pack_reader,
        now=NOW,
    )
    legacy = legacy_assemble_analysis_materials(
        tick=TICK,
        open_markets=frozenset({Market.US}),
        receipt_readers=readers,
        pack_reader=pack_reader,
        now=NOW,
    )
    assert material_binding_digest(masked) != material_binding_digest(legacy)

    built = build_candidates(
        masked, baselines={}, now=NOW, resolve_scope=resolve_account_scope_pattern
    )
    assert built.mask_digest == US_ONLY.digest
    assert built.material_binding_digest == material_binding_digest(masked)
    legacy_built = build_candidates(
        legacy, baselines={}, now=NOW, resolve_scope=resolve_account_scope_pattern
    )
    assert legacy_built.mask_digest is None
