from datetime import datetime, timedelta, timezone
from dataclasses import FrozenInstanceError
from types import MappingProxyType

import pytest

from tokmo.programs.legacy_g2 import (
    _legacy_local_date as _local_date,
    legacy_assemble_analysis_materials as assemble_analysis_materials,
)
from tokmo.records.material_pack_store import PackCorruptError, PackHashMismatchError
from tokmo.records.receipt_store import ReceiptCorruptError
from tokmo.values.analysis_barrier import MarketReadiness
from tokmo.values.material_pack import JobKind, build_material_pack_v1
from tokmo.values.ops_tick import tick_id_from_utc
from tokmo.values.receipt_v1 import (
    AttemptStatus, AttemptV1, CompletenessV1, LastSuccessV1,
    MarketFreshness, ReceiptV1,
)
from tokmo.values.session_gate import Market


NOW = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
TICK = tick_id_from_utc(NOW)


def payload(job):
    if job is JobKind.HOLDINGS:
        return {"accounts": [], "global_inventory": True}
    if job is JobKind.QUOTES:
        return {"markets": {"KR": [], "US": []}}
    return {"accounts": [], "pagination_pending": False}


def pack(job, tick_id, captured_at=NOW, market_partition=("KR", "US")):
    return build_material_pack_v1(
        schema_version=1, job=job, tick_id=tick_id,
        market_partition=market_partition, payload=payload(job),
        captured_at=captured_at,
        source_timestamps={"provider": None, "fetched": None, "stored": None},
        parser_version="fixture-v1",
    )


def receipt(job, tick_id, captured_at=NOW, *, attempt_tick=None, missing_market=None):
    markets = {
        market: MarketFreshness(
            market, "fresh", None if market == missing_market else tick_id,
            None if market == missing_market else captured_at,
        )
        for market in ("KR", "US")
    }
    return ReceiptV1(
        1, job,
        AttemptV1(
            tick_id if attempt_tick is None else attempt_tick,
            AttemptStatus.FAILED if attempt_tick else AttemptStatus.SAVED,
            "failed_parse" if attempt_tick else "saved", NOW,
        ),
        LastSuccessV1(tick_id, f"packs/{job.value}/{tick_id}.json",
                      {JobKind.HOLDINGS: "a", JobKind.QUOTES: "b", JobKind.TRADES: "c"}[job] * 64,
                      captured_at, captured_at),
        markets,
        CompletenessV1(0, 0, False, 0, 0) if job is JobKind.TRADES else None,
    )


def fixture_inputs(*, captured=None, missing_holdings_market=None):
    captured = captured or {}
    receipts = {}
    packs = {}
    calls = {job: 0 for job in JobKind}
    for offset, job in enumerate(JobKind):
        tick_id = 100 + offset
        when = captured.get(job, NOW)
        receipts[job] = receipt(
            job, tick_id, when,
            attempt_tick=999 if job is JobKind.QUOTES else None,
            missing_market=missing_holdings_market if job is JobKind.HOLDINGS else None,
        )
        packs[job] = pack(job, tick_id, when)

    def reader_for(job):
        def read():
            calls[job] += 1
            return receipts[job]
        return read

    def pack_reader(path, expected_sha256):
        job = next(job for job in JobKind if f"/{job.value}/" in path)
        assert expected_sha256 == {
            JobKind.HOLDINGS: "a", JobKind.QUOTES: "b", JobKind.TRADES: "c"
        }[job] * 64
        return packs[job]

    return {job: reader_for(job) for job in JobKind}, pack_reader, calls


def test_receipts_once_mixed_ticks_and_newer_attempt_ignored():
    readers, pack_reader, calls = fixture_inputs()
    result = assemble_analysis_materials(
        tick=TICK, open_markets=frozenset({Market.KR, Market.US}),
        receipt_readers=readers, pack_reader=pack_reader, now=NOW,
    )
    assert calls == {job: 1 for job in JobKind}
    assert result.material_ticks == {
        JobKind.HOLDINGS: 100, JobKind.QUOTES: 101, JobKind.TRADES: 102,
    }
    assert result.proceed_markets == frozenset({Market.KR, Market.US})
    assert all(value is MarketReadiness.READY for value in result.market_readiness.values())
    assert result.materials[JobKind.QUOTES].pack_tick_id == 101


def test_no_open_markets_reads_nothing_and_all_closed():
    readers, pack_reader, calls = fixture_inputs()
    result = assemble_analysis_materials(
        tick=TICK, open_markets=frozenset(),
        receipt_readers=readers, pack_reader=pack_reader, now=NOW,
    )
    assert calls == {job: 0 for job in JobKind}
    assert result.materials == {}
    assert result.proceed_markets == frozenset()
    assert set(result.market_readiness.values()) == {MarketReadiness.SKIP_CLOSED}


def test_holdings_stale_or_missing_is_market_isolated():
    readers, pack_reader, _ = fixture_inputs(
        captured={JobKind.HOLDINGS: TICK.start_utc - timedelta(seconds=601)}
    )
    result = assemble_analysis_materials(
        tick=TICK, open_markets=frozenset({Market.KR}),
        receipt_readers=readers, pack_reader=pack_reader, now=NOW,
    )
    assert result.market_readiness[Market.KR] is MarketReadiness.SKIP_HOLDINGS_STALE
    assert result.market_readiness[Market.US] is MarketReadiness.SKIP_CLOSED

    readers, pack_reader, _ = fixture_inputs(missing_holdings_market="KR")
    result = assemble_analysis_materials(
        tick=TICK, open_markets=frozenset({Market.KR, Market.US}),
        receipt_readers=readers, pack_reader=pack_reader, now=NOW,
    )
    assert result.market_readiness[Market.KR] is MarketReadiness.SKIP_HOLDINGS_MISSING
    assert result.market_readiness[Market.US] is MarketReadiness.READY


def test_quotes_and_trades_stale_both_proceed_and_are_reported():
    readers, pack_reader, _ = fixture_inputs(captured={
        JobKind.QUOTES: TICK.start_utc - timedelta(seconds=601),
        JobKind.TRADES: TICK.start_utc - timedelta(seconds=601),
    })
    result = assemble_analysis_materials(
        tick=TICK, open_markets=frozenset({Market.KR}),
        receipt_readers=readers, pack_reader=pack_reader, now=NOW,
    )
    assert result.market_readiness[Market.KR] is MarketReadiness.READY
    assert result.stale_jobs[Market.KR] == frozenset({JobKind.QUOTES, JobKind.TRADES})
    assert result.proceed_markets == frozenset({Market.KR})


@pytest.mark.parametrize("age,stale", [
    (timedelta(seconds=600), False),
    (timedelta(seconds=600, microseconds=1), True),
])
def test_exact_600_second_ready_boundary(age, stale):
    readers, pack_reader, _ = fixture_inputs(
        captured={JobKind.HOLDINGS: TICK.start_utc - age}
    )
    result = assemble_analysis_materials(
        tick=TICK, open_markets=frozenset({Market.KR}),
        receipt_readers=readers, pack_reader=pack_reader, now=NOW,
    )
    expected = (
        MarketReadiness.SKIP_HOLDINGS_STALE if stale else MarketReadiness.READY
    )
    assert result.market_readiness[Market.KR] is expected


def test_value_error_is_bounded_unavailable_and_other_errors_propagate():
    readers, pack_reader, _ = fixture_inputs()

    def corrupt():
        raise ValueError("/private/path account_scope secret")

    readers[JobKind.QUOTES] = corrupt
    result = assemble_analysis_materials(
        tick=TICK, open_markets=frozenset({Market.KR}),
        receipt_readers=readers, pack_reader=pack_reader, now=NOW,
    )
    assert result.material_ticks[JobKind.QUOTES] is None
    assert result.market_readiness[Market.KR] is MarketReadiness.READY
    assert all("/private" not in reason and "secret" not in reason for reason in result.incidents)

    def programmer_error():
        raise RuntimeError("must propagate")

    readers[JobKind.QUOTES] = programmer_error
    with pytest.raises(RuntimeError, match="must propagate"):
        assemble_analysis_materials(
            tick=TICK, open_markets=frozenset({Market.KR}),
            receipt_readers=readers, pack_reader=pack_reader, now=NOW,
        )


def test_reader_corruption_errors_are_value_errors():
    assert issubclass(ReceiptCorruptError, ValueError)
    assert issubclass(PackHashMismatchError, ValueError)
    assert issubclass(PackCorruptError, ValueError)


@pytest.mark.parametrize("mismatch", ["job", "tick", "captured_at"])
def test_each_receipt_pack_identity_mismatch_is_unavailable(mismatch):
    readers, original_pack_reader, _ = fixture_inputs()
    if mismatch == "job":
        readers[JobKind.HOLDINGS] = lambda: receipt(JobKind.QUOTES, 100)

    def mismatched_pack_reader(path, expected_sha256):
        value = original_pack_reader(path, expected_sha256)
        if value.job is not JobKind.HOLDINGS or mismatch == "job":
            return value
        if mismatch == "tick":
            return pack(JobKind.HOLDINGS, value.tick_id + 1)
        return pack(
            JobKind.HOLDINGS, value.tick_id,
            captured_at=value.captured_at + timedelta(microseconds=1),
        )

    result = assemble_analysis_materials(
        tick=TICK, open_markets=frozenset({Market.KR, Market.US}),
        receipt_readers=readers, pack_reader=mismatched_pack_reader, now=NOW,
    )
    assert result.material_ticks[JobKind.HOLDINGS] is None
    assert result.proceed_markets == frozenset()
    assert set(result.market_readiness.values()) == {MarketReadiness.UNAVAILABLE}


def test_holdings_reader_failure_fails_all_open_markets_with_bounded_incidents():
    readers, pack_reader, _ = fixture_inputs()
    readers[JobKind.HOLDINGS] = lambda: (_ for _ in ()).throw(
        ReceiptCorruptError("private detail")
    )
    result = assemble_analysis_materials(
        tick=TICK, open_markets=frozenset({Market.KR, Market.US}),
        receipt_readers=readers, pack_reader=pack_reader, now=NOW,
    )
    assert set(result.market_readiness.values()) == {MarketReadiness.UNAVAILABLE}
    assert result.incidents == (
        "holdings_material_unavailable",
        "kr_holdings_unavailable",
        "us_holdings_unavailable",
    )


def test_market_local_dates_and_us_dst_boundaries():
    assert _local_date(datetime(2026, 7, 29, 15, tzinfo=timezone.utc), Market.KR).isoformat() == "2026-07-30"
    assert _local_date(datetime(2026, 7, 30, 3, 59, tzinfo=timezone.utc), Market.US).isoformat() == "2026-07-29"
    assert _local_date(datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc), Market.US).isoformat() == "2026-03-08"
    assert _local_date(datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc), Market.US).isoformat() == "2026-03-08"
    assert _local_date(datetime(2026, 11, 1, 5, 59, tzinfo=timezone.utc), Market.US).isoformat() == "2026-11-01"
    assert _local_date(datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc), Market.US).isoformat() == "2026-11-01"


def test_closed_market_trades_payload_is_verified_but_does_not_proceed():
    readers, pack_reader, _ = fixture_inputs()
    result = assemble_analysis_materials(
        tick=TICK, open_markets=frozenset({Market.KR}),
        receipt_readers=readers, pack_reader=pack_reader, now=NOW,
    )
    assert JobKind.TRADES in result.materials
    assert result.materials[JobKind.TRADES].payload == {
        "accounts": (), "pagination_pending": False,
    }
    assert result.market_readiness[Market.US] is MarketReadiness.SKIP_CLOSED
    assert Market.US not in result.proceed_markets


def test_sixty_ticks_are_deterministic_and_read_exactly_once_per_run():
    readers, pack_reader, calls = fixture_inputs()
    baseline = None
    for _ in range(60):
        result = assemble_analysis_materials(
            tick=TICK, open_markets=frozenset({Market.KR}),
            receipt_readers=readers, pack_reader=pack_reader, now=NOW,
        )
        comparable = (
            tuple(
                (
                    job,
                    material.pack_tick_id,
                    material.captured_at,
                    material.file_sha256,
                    material.content_hash,
                    repr(material.payload),
                    material.market_partition,
                )
                for job, material in result.materials.items()
            ),
            result.material_ticks, result.market_readiness, result.stale_jobs,
            result.incidents, result.proceed_markets,
        )
        baseline = comparable if baseline is None else baseline
        assert comparable == baseline
    assert calls == {job: 60 for job in JobKind}


def test_market_partition_mismatch_is_never_fresh_for_open_market():
    readers, pack_reader, _ = fixture_inputs()
    original = pack_reader

    def adversarial(path, expected_sha256):
        value = original(path, expected_sha256)
        if value.job is JobKind.QUOTES:
            return pack(JobKind.QUOTES, value.tick_id, market_partition=("KR",))
        return value

    result = assemble_analysis_materials(
        tick=TICK, open_markets=frozenset({Market.US}),
        receipt_readers=readers, pack_reader=adversarial, now=NOW,
    )
    assert result.market_readiness[Market.US] is MarketReadiness.READY
    assert JobKind.QUOTES in result.stale_jobs[Market.US]


def test_results_and_nested_payload_are_deeply_immutable_and_copy_owned():
    readers, pack_reader, _ = fixture_inputs()
    result = assemble_analysis_materials(
        tick=TICK, open_markets=frozenset({Market.KR}),
        receipt_readers=readers, pack_reader=pack_reader, now=NOW,
    )
    assert isinstance(result.materials, MappingProxyType)
    with pytest.raises(TypeError):
        result.materials[JobKind.QUOTES] = None
    quotes = result.materials[JobKind.QUOTES].payload
    with pytest.raises(TypeError):
        quotes["markets"] = {}
    with pytest.raises(TypeError):
        quotes["markets"]["KR"] += ("forged",)
    quote_material = result.materials[JobKind.QUOTES]
    assert quote_material.file_sha256 == "b" * 64
    assert quote_material.content_hash != quote_material.file_sha256
