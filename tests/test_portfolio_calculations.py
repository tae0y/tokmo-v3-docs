"""Public receipt -> immutable MaterialPack -> barrier -> portfolio snapshot TDD."""

import hashlib
from dataclasses import fields, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from tokmo.programs.analysis_barrier import assemble_analysis_materials
from tokmo.programs.portfolio_calculations import (
    _semantic,
    compute_portfolio_snapshot,
    fx_content_hash_v1,
    project_portfolio_presentation,
    reference_material_digest_v1,
)
from tokmo.records.material_pack_store import MaterialPackWriter
from tokmo.values.analysis_barrier import MarketReadiness
from tokmo.values.cadence_policy import CADENCE_POLICY_V1, cadence_tick_v1
from tokmo.values.material_pack import (
    JobKind,
    build_material_pack_v1,
    canonical_encode,
    canonical_pack_file_bytes,
)
from tokmo.values.open_market_mask import (
    MaskMarketEntryV1,
    OpenMarketMaskV1,
    mask_digest_v1,
)
from tokmo.values.portfolio_calculations import (
    ConcentrationBasis,
    FxEvidenceEnvelopeV1,
    FxStatus,
    PortfolioScope,
    PositionPresentationV1,
    PricingState,
    RatioState,
    ScopeUnresolved,
    SnapshotStatus,
    TradeGlobalQualityState,
    TradeMarketState,
)
from tokmo.values.receipt_v1 import (
    AttemptStatus,
    AttemptV1,
    CompletenessV1,
    LastSuccessV1,
    MarketFreshness,
    ReceiptV1,
)
from tokmo.values.session_gate import Market

NOW = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
TICK = cadence_tick_v1(CADENCE_POLICY_V1.policy_version, NOW)
PRIVATE_PERSONAL = "toss-acct-111111111111111111111111"
PRIVATE_PENSION = "toss-acct-222222222222222222222222"


def holding(symbol, quantity, avg_price, currency, market):
    return {
        "symbol": symbol,
        "quantity": quantity,
        "avg_price": avg_price,
        "currency": currency,
        "market_raw": market,
        "market_field": market,
    }


def quote(symbol, price, currency, when=NOW):
    stamp = when.isoformat().replace("+00:00", "Z")
    return {
        "symbol": symbol,
        "last_price": price,
        "currency_raw": currency,
        "provider_timestamp": stamp,
        "fetched_at": stamp,
        "stored_at": stamp,
    }


def trade(key, market, *, quantity="1", price="10", amount="10"):
    return {
        "source_record_key": key,
        "trade_date": "2026-08-04",
        "transaction_type_raw": "provider-buy",
        "instrument_raw": "SYN",
        "quantity": quantity,
        "unit_price": price,
        "amount": amount,
        "medium_raw": "provider-medium",
        "currency_raw": "KRW" if market == "KR" else "USD",
        "classified_market": market,
        "filled_at": "2026-08-04T00:00:00Z",
    }


def account(
    scope, holdings=(), inserted=(), duplicates=(), quarantine=(), incomplete=()
):
    return {
        "account_scope": scope,
        "account_type_label": "private-label",
        "ordinal": 1,
        "holdings": list(holdings),
    }, {
        "account_scope": scope,
        "inserted": list(inserted),
        "duplicates": list(duplicates),
        "quarantine": list(quarantine),
        "incomplete": list(incomplete),
    }


def _mask(open_markets):
    entries = {}
    local_dates = {Market.KR: date(2026, 8, 4), Market.US: date(2026, 8, 3)}
    for market in Market:
        opened = market in open_markets
        entries[market] = MaskMarketEntryV1(
            market,
            local_dates[market],
            "open" if opened else "after_close",
            NOW - timedelta(hours=1) if opened else None,
            NOW + timedelta(hours=1) if opened else None,
        )
    digest = mask_digest_v1(TICK.policy_version, NOW, TICK.tick_id, entries)
    return OpenMarketMaskV1(
        TICK.policy_version, NOW, TICK.tick_id, frozenset(open_markets), entries, digest
    )


def public_barrier(
    tmp_path,
    *,
    holding_accounts=(),
    trade_accounts=(),
    quotes=None,
    open_markets=frozenset({Market.KR, Market.US}),
    partitions=None,
    captured=None,
    freshness_at=None,
    pagination=False,
    unavailable_jobs=frozenset(),
):
    mask = _mask(open_markets)
    partitions = partitions or {
        job: tuple(
            market.value for market in (Market.KR, Market.US) if market in open_markets
        )
        for job in JobKind
    }
    captured = captured or {}
    freshness_at = freshness_at or {}
    payloads = {
        JobKind.HOLDINGS: {
            "accounts": list(holding_accounts),
            "global_inventory": True,
        },
        JobKind.QUOTES: {"markets": quotes or {"KR": [], "US": []}},
        JobKind.TRADES: {
            "accounts": list(trade_accounts),
            "pagination_pending": pagination,
        },
    }
    writer = MaterialPackWriter(tmp_path, clock=lambda: NOW)
    receipts = {}
    for job in JobKind:
        when = captured.get(job, NOW)
        pack = build_material_pack_v1(
            schema_version=1,
            job=job,
            tick_id=TICK.tick_id,
            market_partition=tuple(partitions[job]),
            payload=payloads[job],
            captured_at=when,
            source_timestamps={"provider": None, "fetched": None, "stored": None},
            parser_version="portfolio-test-v1",
            mask_digest=mask.digest,
        )
        path = writer.write(pack)
        sha = hashlib.sha256(canonical_pack_file_bytes(pack)).hexdigest()
        freshness = {
            market.value: MarketFreshness(
                market.value,
                "fresh",
                TICK.tick_id,
                freshness_at.get((job, market), when),
            )
            for market in Market
        }
        receipts[job] = ReceiptV1(
            1,
            job,
            AttemptV1(TICK.tick_id, AttemptStatus.SAVED, "saved", NOW),
            LastSuccessV1(TICK.tick_id, str(path), sha, when, NOW),
            freshness,
            CompletenessV1(len(trade_accounts), 0, pagination, 0, 0)
            if job is JobKind.TRADES
            else None,
            mask_digest=mask.digest,
        )
    calls = {job: 0 for job in JobKind}

    def reader(job):
        def read():
            calls[job] += 1
            if job in unavailable_jobs:
                raise ValueError("fixture material unavailable")
            return receipts[job]

        return read

    barrier = assemble_analysis_materials(
        tick=TICK,
        mask=mask,
        receipt_readers={job: reader(job) for job in JobKind},
        pack_reader=lambda path, sha: writer.read_and_verify(Path(path), sha),
        now=NOW,
    )
    expected_reads = 0 if not open_markets else 1
    assert calls == {job: expected_reads for job in JobKind}
    return barrier


def resolver(raw):
    return {
        PRIVATE_PERSONAL: PortfolioScope.PERSONAL,
        PRIVATE_PENSION: PortfolioScope.PENSION,
    }.get(raw, ScopeUnresolved.UNRESOLVED)


def snapshot(tmp_path, **kwargs):
    barrier = public_barrier(tmp_path, **kwargs)
    return barrier, compute_portfolio_snapshot(
        barrier,
        resolve_scope=resolver,
        fx_evidence=None,
    )


def test_pc1_public_aggregation_fractional_weighted_average_and_scope_boundaries(
    tmp_path,
):
    p1, _ = account(PRIVATE_PERSONAL, [holding("SYN", "0.25", "100", "USD", "US")])
    p2, _ = account(PRIVATE_PERSONAL, [holding("SYN", "0.75", "200", "USD", "US")])
    pension, _ = account(PRIVATE_PENSION, [holding("SYN", "2", "50", "USD", "US")])
    _, result = snapshot(
        tmp_path,
        holding_accounts=[p1, p2, pension],
        quotes={"KR": [], "US": [quote("SYN", "250", "USD")]},
    )
    assert result.status is SnapshotStatus.COMPLETE
    assert result.semantic_digest
    assert [
        (p.portfolio_scope, p.quantity, p.average_cost) for p in result.body.positions
    ] == [
        (PortfolioScope.PERSONAL, Decimal("1.00"), Decimal(175)),
        (PortfolioScope.PENSION, Decimal(2), Decimal(50)),
    ]
    assert [p.total_cost_basis_native for p in result.body.positions] == [
        Decimal("175.00"),
        Decimal(100),
    ]
    assert len(result.body.partition_totals) == 2
    assert PRIVATE_PERSONAL not in repr(result) and PRIVATE_PENSION not in repr(result)


def test_pc1_negative_average_cost_fails_after_pack_accepts_it(tmp_path):
    bad, _ = account(PRIVATE_PERSONAL, [holding("BAD", "1", "-1", "USD", "US")])
    barrier = public_barrier(tmp_path, holding_accounts=[bad])
    assert (
        barrier.materials[JobKind.HOLDINGS].payload["accounts"][0]["holdings"][0][
            "avg_price"
        ]
        == "-1"
    )
    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=None
    )
    assert result.status is SnapshotStatus.MATERIAL_INVALID
    assert result.invalid_reason == "INVALID_HOLDINGS_COST"
    assert result.body is None and result.semantic_digest is None


@pytest.mark.parametrize(
    ("quantity", "average", "reason"),
    [("-1", "10", "INVALID_HOLDINGS_COST"), ("1", "-10", "INVALID_HOLDINGS_COST")],
)
def test_pc1_negative_holding_inputs_fail_closed_without_any_dto(
    tmp_path, quantity, average, reason
):
    bad, _ = account(PRIVATE_PERSONAL, [holding("BAD", quantity, average, "USD", "US")])
    result = compute_portfolio_snapshot(
        public_barrier(tmp_path, holding_accounts=[bad]),
        resolve_scope=resolver,
        fx_evidence=None,
    )
    assert result.status is SnapshotStatus.MATERIAL_INVALID
    assert result.invalid_reason == reason
    assert result.body is None and result.semantic_digest is None


def test_pc1_signed_negative_zero_is_rejected_by_pack_and_canonical_zero_stays_positive(
    tmp_path,
):
    negative_zero, _ = account(
        PRIVATE_PERSONAL, [holding("ZERO", "-0", "-0", "KRW", "KR")]
    )
    with pytest.raises(ValueError, match="canonical decimal"):
        public_barrier(tmp_path / "negative", holding_accounts=[negative_zero])
    canonical_zero, _ = account(
        PRIVATE_PERSONAL, [holding("ZERO", "0", "0", "KRW", "KR")]
    )
    canonical = compute_portfolio_snapshot(
        public_barrier(tmp_path / "canonical", holding_accounts=[canonical_zero]),
        resolve_scope=resolver,
        fx_evidence=None,
    )
    assert canonical.status is SnapshotStatus.COMPLETE
    assert canonical.body.positions[0].quantity == Decimal(0)
    assert canonical.body.positions[0].quantity.as_tuple().sign == 0
    assert canonical.body.positions[0].average_cost.as_tuple().sign == 0


def test_pc1_zero_cost_retained_with_typed_undefined(tmp_path):
    zero, _ = account(PRIVATE_PERSONAL, [holding("ZERO", "0", "0", "KRW", "KR")])
    _, result = snapshot(
        tmp_path,
        holding_accounts=[zero],
        quotes={"KR": [quote("ZERO", "10", "KRW")], "US": []},
    )
    position = result.body.positions[0]
    partition = result.body.partition_totals[0]
    assert position.quantity == position.average_cost == Decimal(0)
    assert position.unrealized_return.state is RatioState.ZERO_COST_UNDEFINED
    assert (
        partition.priced_cost_basis_coverage_ratio.state
        is RatioState.ZERO_DENOMINATOR_UNDEFINED
    )


def test_pc6_unresolved_scope_fails_without_identity(tmp_path):
    unknown = "toss-acct-333333333333333333333333"
    row, _ = account(unknown, [holding("SYN", "1", "1", "KRW", "KR")])
    barrier = public_barrier(tmp_path, holding_accounts=[row])
    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=None
    )
    assert result.status is SnapshotStatus.SCOPE_RESOLUTION_INCOMPLETE
    assert result.unresolved_account_count == 1
    assert result.body is None and result.semantic_digest is None
    assert unknown not in repr(result)


def valid_fx(barrier, **changes):
    reference_at = max(material.captured_at for material in barrier.materials.values())
    base = FxEvidenceEnvelopeV1(
        1,
        "toss-exchange-rate-v1",
        "USD",
        "KRW",
        "1500",
        reference_at - timedelta(seconds=30),
        reference_at - timedelta(seconds=20),
        reference_at - timedelta(minutes=5),
        reference_at + timedelta(minutes=5),
        600,
        reference_at,
        reference_material_digest_v1(barrier),
        "0" * 64,
    )
    base = replace(base, **changes)
    return replace(base, content_hash=fx_content_hash_v1(base))


def rehash_fx(envelope):
    seed = replace(envelope, content_hash="0" * 64)
    return replace(seed, content_hash=fx_content_hash_v1(seed))


def test_pc2_quote_duplicate_wrong_currency_missing_and_stale(tmp_path):
    row, _ = account(PRIVATE_PERSONAL, [holding("Q", "2", "10", "USD", "US")])
    duplicate = {"KR": [], "US": [quote("Q", "11", "USD"), quote("Q", "12", "USD")]}
    _, result = snapshot(tmp_path / "dup", holding_accounts=[row], quotes=duplicate)
    assert result.status is SnapshotStatus.MATERIAL_INVALID
    assert result.invalid_reason == "INVALID_QUOTE"

    wrong = {"KR": [], "US": [quote("Q", "11", "KRW")]}
    _, result = snapshot(tmp_path / "wrong", holding_accounts=[row], quotes=wrong)
    assert result.status is SnapshotStatus.MATERIAL_INVALID

    _, missing = snapshot(tmp_path / "missing", holding_accounts=[row])
    assert missing.body.positions[0].pricing_state is PricingState.UNPRICED

    barrier = public_barrier(
        tmp_path / "stale",
        holding_accounts=[row],
        quotes={"KR": [], "US": [quote("Q", "11", "USD")]},
        captured={JobKind.QUOTES: NOW - timedelta(seconds=601)},
    )
    stale = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=None
    )
    assert stale.body.positions[0].pricing_state is PricingState.STALE_QUOTE
    assert stale.body.positions[0].current_price is None


def test_pc3_pc4_pnl_partition_reconciliation_and_unpriced_coverage(tmp_path):
    row, _ = account(
        PRIVATE_PERSONAL,
        [
            holding("GAIN", "2", "10", "KRW", "KR"),
            holding("LOSS", "1", "20", "KRW", "KR"),
            holding("MISS", "1", "30", "KRW", "KR"),
        ],
    )
    _, result = snapshot(
        tmp_path,
        holding_accounts=[row],
        quotes={
            "KR": [quote("GAIN", "15", "KRW"), quote("LOSS", "15", "KRW")],
            "US": [],
        },
    )
    partition = result.body.partition_totals[0]
    assert partition.total_cost_basis_native == Decimal(70)
    assert partition.priced_cost_basis_native == Decimal(40)
    assert partition.priced_market_value_native == Decimal(45)
    assert partition.unrealized_pnl_native == Decimal(5)
    assert partition.gross_gains_native == Decimal(10)
    assert partition.gross_losses_native == Decimal(-5)
    assert (
        partition.unrealized_pnl_native
        == partition.gross_gains_native + partition.gross_losses_native
    )
    with localcontext() as context:
        context.prec = 50
        assert partition.priced_cost_basis_coverage_ratio.value == Decimal(
            40
        ) / Decimal(70)
    assert partition.unpriced_position_count == 1


def test_pc5_repeating_fraction_weights_are_exact_and_unquantized(tmp_path):
    row, _ = account(
        PRIVATE_PERSONAL,
        [
            holding("A", "1", "1", "KRW", "KR"),
            holding("B", "1", "1", "KRW", "KR"),
            holding("C", "1", "1", "KRW", "KR"),
        ],
    )
    _, result = snapshot(
        tmp_path,
        holding_accounts=[row],
        quotes={"KR": [quote(symbol, "1", "KRW") for symbol in "ABC"], "US": []},
    )
    weights = [p.currency_partition_market_value_weight for p in result.body.positions]
    with localcontext() as context:
        context.prec = 50
        assert weights == [Decimal(1) / Decimal(3)] * 3
        assert sum(weights) == Decimal(
            "0.99999999999999999999999999999999999999999999999999"
        )
    assert all(weight != Decimal("0.333333") for weight in weights)


def test_pc7_pc9_valid_fx_preserves_usd_adds_krw_and_concentration(tmp_path):
    personal, _ = account(
        PRIVATE_PERSONAL,
        [
            holding("US1", "0.5", "100", "USD", "US"),
            holding("KR1", "1", "10000", "KRW", "KR"),
        ],
    )
    barrier = public_barrier(
        tmp_path,
        holding_accounts=[personal],
        quotes={
            "KR": [quote("KR1", "12000", "KRW")],
            "US": [quote("US1", "120", "USD")],
        },
    )
    fx = valid_fx(barrier)
    result = compute_portfolio_snapshot(barrier, resolve_scope=resolver, fx_evidence=fx)
    us = next(p for p in result.body.positions if p.market is Market.US)
    assert us.currency == "USD" and us.average_cost == Decimal(100)
    assert us.internal_total_cost_basis_krw == Decimal("75000.0")
    assert us.priced_market_value_native == Decimal("60.0")
    assert us.priced_market_value_krw == Decimal("90000.0")
    assert us.fx_binding_digest == result.body.fx_binding_digest
    scope = result.body.scope_krw_totals[0]
    assert scope.fx_status is FxStatus.AVAILABLE
    assert scope.internal_total_cost_basis_krw == Decimal("85000.0")
    assert scope.priced_market_value_krw == Decimal("102000.0")
    assert scope.unrealized_pnl_krw == Decimal("17000.0")
    assert (
        scope.top_by_priced_market_value[0].basis
        is ConcentrationBasis.PRICED_MARKET_VALUE_KRW
    )
    assert sum(
        p.scope_krw_total_cost_basis_weight for p in result.body.positions
    ) == Decimal(1)


def test_pc7_pc8_stale_or_missing_fx_keeps_native_usd_and_disables_scope(tmp_path):
    personal, _ = account(PRIVATE_PERSONAL, [holding("US1", "1", "100", "USD", "US")])
    barrier = public_barrier(
        tmp_path,
        holding_accounts=[personal],
        quotes={"KR": [], "US": [quote("US1", "120", "USD")]},
    )
    missing = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=None
    )
    us = missing.body.positions[0]
    assert us.priced_market_value_native == Decimal(120)
    assert us.priced_market_value_krw is None
    assert missing.body.scope_krw_totals[0].fx_status is FxStatus.UNAVAILABLE
    assert missing.body.scope_krw_totals[0].priced_market_value_krw is None

    stale_fx = valid_fx(barrier, observed_at=NOW - timedelta(seconds=601))
    invalid = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=stale_fx
    )
    assert invalid.body.positions[0].priced_market_value_native == Decimal(120)
    assert invalid.body.positions[0].priced_market_value_krw is None
    assert invalid.body.scope_krw_totals[0].fx_status is FxStatus.INVALID


def _us_fx_barrier(tmp_path):
    personal, _ = account(PRIVATE_PERSONAL, [holding("US1", "2", "80", "USD", "US")])
    return public_barrier(
        tmp_path,
        holding_accounts=[personal],
        quotes={"KR": [], "US": [quote("US1", "100", "USD")]},
    )


def _assert_invalid_fx_snapshot(result):
    assert result.status is SnapshotStatus.COMPLETE
    assert result.semantic_digest
    assert result.body.scope_krw_totals[0].fx_status is FxStatus.INVALID
    position = result.body.positions[0]
    assert position.priced_market_value_native == Decimal(200)
    assert position.unrealized_pnl_native == Decimal(40)
    assert position.priced_market_value_krw is None
    assert position.unrealized_pnl_krw is None
    assert position.internal_total_cost_basis_krw is None
    assert result.body.fx_binding_digest is None


@pytest.mark.parametrize(
    "time_field",
    ["observed_at", "acquired_at", "valid_from", "valid_until", "reference_at"],
)
def test_fx_boundary_naive_datetime_never_raises_and_is_sanitized(tmp_path, time_field):
    barrier = _us_fx_barrier(tmp_path)
    valid = valid_fx(barrier)
    aware = getattr(valid, time_field)
    malformed = replace(valid, **{time_field: aware.replace(tzinfo=None)})
    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=malformed
    )
    _assert_invalid_fx_snapshot(result)
    assert result.body.fx_evidence is None


_WRONG_FX_VALUES = (1, "wrong", {"bad": 1}, 1.5, Decimal(1), b"bad")
_FX_FIELDS = tuple(field.name for field in fields(FxEvidenceEnvelopeV1))
_FX_EXPECTED_TYPES = {
    "schema_version": int,
    "source": str,
    "base_currency": str,
    "quote_currency": str,
    "rate": str,
    "observed_at": datetime,
    "acquired_at": datetime,
    "valid_from": datetime,
    "valid_until": datetime,
    "max_age_seconds": int,
    "reference_at": datetime,
    "reference_material_digest": str,
    "content_hash": str,
}
_WRONG_FX_FIELD_CASES = tuple(
    (field_name, wrong_value)
    for field_name in _FX_FIELDS
    for wrong_value in _WRONG_FX_VALUES
    if type(wrong_value) is not _FX_EXPECTED_TYPES[field_name]
)


@pytest.mark.parametrize(
    ("field_name", "wrong_value"),
    _WRONG_FX_FIELD_CASES,
    ids=lambda value: type(value).__name__ if not isinstance(value, str) else value,
)
def test_fx_boundary_every_wrong_field_type_is_invalid_not_exception(
    tmp_path, field_name, wrong_value
):
    barrier = _us_fx_barrier(tmp_path)
    malformed = replace(valid_fx(barrier), **{field_name: wrong_value})
    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=malformed
    )
    _assert_invalid_fx_snapshot(result)
    assert result.body.fx_evidence is None


class _FxEnvelopeSubclass(FxEvidenceEnvelopeV1):
    pass


@pytest.mark.parametrize(
    "garbage",
    [1, "garbage", {"rate": "1500"}, 1.5, Decimal(1500), b"garbage"],
    ids=lambda value: type(value).__name__,
)
def test_fx_boundary_non_envelope_garbage_never_enters_body(tmp_path, garbage):
    barrier = _us_fx_barrier(tmp_path)
    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=garbage
    )
    _assert_invalid_fx_snapshot(result)
    assert result.body.fx_evidence is None


def test_fx_boundary_subclass_is_rejected_as_non_envelope(tmp_path):
    barrier = _us_fx_barrier(tmp_path)
    valid = valid_fx(barrier)
    subclass = _FxEnvelopeSubclass(
        *(getattr(valid, field.name) for field in fields(FxEvidenceEnvelopeV1))
    )
    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=subclass
    )
    _assert_invalid_fx_snapshot(result)
    assert result.body.fx_evidence is None


def _invalid_factual_fx_cases(valid):
    return {
        "source": replace(valid, source="wrong-source"),
        "base": replace(valid, base_currency="EUR"),
        "quote": replace(valid, quote_currency="USD"),
        "schema": replace(valid, schema_version=2),
        "zero_rate": replace(valid, rate="0"),
        "negative_rate": replace(valid, rate="-1"),
        "noncanonical_rate": replace(valid, rate="1500.0"),
        "max_age": replace(valid, max_age_seconds=601),
        "max_age_overflow": replace(valid, max_age_seconds=10**100),
        "acquired_after_reference": replace(
            valid, acquired_at=valid.reference_at + timedelta(microseconds=1)
        ),
        "acquired_before_observed": replace(
            valid, acquired_at=valid.observed_at - timedelta(microseconds=1)
        ),
        "tampered_hash": replace(valid, content_hash="f" * 64),
        "malformed_hash": replace(valid, content_hash="bad"),
        "reference_at": replace(
            valid, reference_at=valid.reference_at + timedelta(microseconds=1)
        ),
        "reference_digest": replace(valid, reference_material_digest="e" * 64),
        "valid_from_after": replace(
            valid, valid_from=valid.reference_at + timedelta(microseconds=1)
        ),
        "valid_until_equal": replace(valid, valid_until=valid.reference_at),
        "age_601": rehash_fx(
            replace(
                valid,
                observed_at=valid.reference_at - timedelta(seconds=601),
                acquired_at=valid.reference_at - timedelta(seconds=1),
            )
        ),
    }


@pytest.mark.parametrize(
    "case_name",
    [
        "source",
        "base",
        "quote",
        "schema",
        "zero_rate",
        "negative_rate",
        "noncanonical_rate",
        "max_age",
        "max_age_overflow",
        "acquired_after_reference",
        "acquired_before_observed",
        "tampered_hash",
        "malformed_hash",
        "reference_at",
        "reference_digest",
        "valid_from_after",
        "valid_until_equal",
        "age_601",
    ],
)
def test_fx_factual_invalidity_matrix_is_typed_and_digest_bound(tmp_path, case_name):
    barrier = _us_fx_barrier(tmp_path)
    invalid = _invalid_factual_fx_cases(valid_fx(barrier))[case_name]
    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=invalid
    )
    _assert_invalid_fx_snapshot(result)
    assert result.body.fx_evidence == invalid


def test_fx_valid_boundaries_are_inclusive_exclusive_and_age_600_allowed(tmp_path):
    barrier = _us_fx_barrier(tmp_path)
    valid = valid_fx(barrier)
    cases = (
        rehash_fx(replace(valid, valid_from=valid.reference_at)),
        rehash_fx(
            replace(
                valid,
                observed_at=valid.reference_at - timedelta(seconds=600),
                acquired_at=valid.reference_at,
            )
        ),
    )
    for envelope in cases:
        result = compute_portfolio_snapshot(
            barrier, resolve_scope=resolver, fx_evidence=envelope
        )
        assert result.status is SnapshotStatus.COMPLETE
        assert result.body.scope_krw_totals[0].fx_status is FxStatus.AVAILABLE
        assert result.body.positions[0].priced_market_value_krw == Decimal(300000)


def test_two_serializable_invalid_factual_envelopes_change_semantic_digest(tmp_path):
    barrier = _us_fx_barrier(tmp_path)
    valid = valid_fx(barrier)
    first = compute_portfolio_snapshot(
        barrier,
        resolve_scope=resolver,
        fx_evidence=replace(valid, source="invalid-a"),
    )
    second = compute_portfolio_snapshot(
        barrier,
        resolve_scope=resolver,
        fx_evidence=replace(valid, source="invalid-b"),
    )
    _assert_invalid_fx_snapshot(first)
    _assert_invalid_fx_snapshot(second)
    assert first.body.fx_evidence == replace(valid, source="invalid-a")
    assert second.body.fx_evidence == replace(valid, source="invalid-b")
    assert first.semantic_digest != second.semantic_digest


def _body_digest(body):
    return hashlib.sha256(
        canonical_encode(
            {
                "v": 1,
                "domain": "tokmo-v3:portfolio-snapshot-body:v1",
                "body": _semantic(body),
            }
        )
    ).hexdigest()


@pytest.mark.parametrize("zero_positions", [False, True])
def test_m6_semantic_digest_is_exactly_recomputable_from_sanitized_body(
    tmp_path, zero_positions
):
    if zero_positions:
        barrier = public_barrier(tmp_path, holding_accounts=[])
    else:
        krw, _ = account(PRIVATE_PERSONAL, [holding("KR1", "1", "1000", "KRW", "KR")])
        barrier = public_barrier(
            tmp_path,
            holding_accounts=[krw],
            quotes={"KR": [quote("KR1", "1100", "KRW")], "US": []},
        )
    valid = valid_fx(barrier)
    inputs = (
        None,
        replace(valid, source="invalid-a"),
        replace(valid, source="invalid-b", rate="1499"),
        replace(valid, observed_at=valid.observed_at.replace(tzinfo=None)),
        {"rate": "caller-garbage"},
    )
    results = tuple(
        compute_portfolio_snapshot(barrier, resolve_scope=resolver, fx_evidence=value)
        for value in inputs
    )
    for result in results:
        assert result.semantic_digest == _body_digest(result.body)
    for left in results:
        for right in results:
            body_equal = _semantic(left.body) == _semantic(right.body)
            assert (left.semantic_digest == right.semantic_digest) is body_equal
    assert results[1].body.fx_evidence == inputs[1]
    assert results[2].body.fx_evidence == inputs[2]
    assert results[3].body.fx_evidence is None
    assert results[4].body.fx_evidence is None


def _reverse_mapping(value):
    return {key: value[key] for key in reversed(tuple(value))}


def test_am1_trade_account_reorder_preserves_quality_multiset_but_changes_hashes_and_digest(
    tmp_path,
):
    holding_row, _ = account(PRIVATE_PERSONAL, [holding("SYN", "1", "10", "USD", "US")])
    _, trade_a = account(
        PRIVATE_PERSONAL,
        inserted=[trade("private-order-a", "US")],
        duplicates=["opaque-a"],
        quarantine=["opaque-q1", "opaque-q2"],
    )
    _, trade_b = account(
        PRIVATE_PENSION,
        inserted=[trade("private-order-b", "KR")],
        incomplete=["opaque-i1", "opaque-i2", "opaque-i3"],
    )
    pension_holding, _ = account(
        PRIVATE_PENSION, [holding("KR", "1", "10", "KRW", "KR")]
    )
    barrier_a = public_barrier(
        tmp_path / "a",
        holding_accounts=[holding_row, pension_holding],
        trade_accounts=[trade_a, trade_b],
    )
    barrier_b = public_barrier(
        tmp_path / "b",
        holding_accounts=[holding_row, pension_holding],
        trade_accounts=[trade_b, trade_a],
    )
    result_a = compute_portfolio_snapshot(
        barrier_a, resolve_scope=resolver, fx_evidence=None
    )
    result_b = compute_portfolio_snapshot(
        barrier_b, resolve_scope=resolver, fx_evidence=None
    )
    quality_a = result_a.body.trade_facts.global_quality
    quality_b = result_b.body.trade_facts.global_quality
    assert quality_a.account_qualities == quality_b.account_qualities
    assert (
        quality_a.duplicate_count,
        quality_a.quarantine_count,
        quality_a.incomplete_count,
    ) == (1, 2, 3)
    assert quality_a == quality_b
    trades_a = barrier_a.materials[JobKind.TRADES]
    trades_b = barrier_b.materials[JobKind.TRADES]
    assert trades_a.content_hash != trades_b.content_hash
    assert trades_a.file_sha256 != trades_b.file_sha256
    assert result_a.semantic_digest != result_b.semantic_digest


def test_am1_mapping_insertion_order_with_same_canonical_bytes_preserves_digest(
    tmp_path,
):
    holdings, trades = account(
        PRIVATE_PERSONAL,
        [holding("SYN", "1", "10", "USD", "US")],
        inserted=[trade("private-order", "US")],
        duplicates=["opaque"],
    )
    reordered_holdings = _reverse_mapping(holdings)
    reordered_holdings["holdings"] = [_reverse_mapping(holdings["holdings"][0])]
    reordered_trades = _reverse_mapping(trades)
    reordered_trades["inserted"] = [_reverse_mapping(trades["inserted"][0])]
    barrier_a = public_barrier(
        tmp_path / "a", holding_accounts=[holdings], trade_accounts=[trades]
    )
    barrier_b = public_barrier(
        tmp_path / "b",
        holding_accounts=[reordered_holdings],
        trade_accounts=[reordered_trades],
    )
    assert all(
        barrier_a.materials[job].content_hash == barrier_b.materials[job].content_hash
        and barrier_a.materials[job].file_sha256 == barrier_b.materials[job].file_sha256
        for job in JobKind
    )
    result_a = compute_portfolio_snapshot(
        barrier_a, resolve_scope=resolver, fx_evidence=None
    )
    result_b = compute_portfolio_snapshot(
        barrier_b, resolve_scope=resolver, fx_evidence=None
    )
    assert result_a.semantic_digest == result_b.semantic_digest


def test_am2_projection_allows_only_evaluated_value_and_pnl_krw_companions(tmp_path):
    personal, _ = account(PRIVATE_PERSONAL, [holding("US1", "2", "80", "USD", "US")])
    barrier = public_barrier(
        tmp_path,
        holding_accounts=[personal],
        quotes={"KR": [], "US": [quote("US1", "100", "USD")]},
    )
    fx = valid_fx(barrier)
    result = compute_portfolio_snapshot(barrier, resolve_scope=resolver, fx_evidence=fx)
    presentation = project_portfolio_presentation(result.body)
    position = presentation.positions[0]
    assert position.average_cost_native == Decimal(80)
    assert position.current_price_native == Decimal(100)
    assert position.invested_cost_basis_native == Decimal(160)
    assert position.evaluated_market_value_native == Decimal(200)
    assert position.evaluated_market_value_krw == Decimal(300000)
    assert position.unrealized_pnl_native == Decimal(40)
    assert position.unrealized_pnl_krw == Decimal(60000)
    assert position.fx_binding_digest
    partition = presentation.scopes[0].partitions[0]
    assert partition.evaluated_market_value_native == Decimal(200)
    assert partition.evaluated_market_value_krw == Decimal(300000)
    assert partition.unrealized_pnl_native == Decimal(40)
    assert partition.unrealized_pnl_krw == Decimal(60000)
    forbidden = {
        "average_cost_krw",
        "current_price_krw",
        "total_cost_basis_krw",
        "priced_cost_basis_krw",
        "converted_krw",
        "internal_total_cost_basis_krw",
        "internal_priced_cost_basis_krw",
    }
    public_field_names = {
        field.name
        for cls in (
            type(presentation),
            type(position),
            type(partition),
            type(presentation.scopes[0]),
        )
        for field in fields(cls)
    }
    assert not forbidden & public_field_names
    assert result.body.positions[0].internal_total_cost_basis_krw == Decimal(240000)


def test_am2_missing_fx_keeps_usd_and_marks_only_allowed_krw_unavailable(tmp_path):
    personal, _ = account(PRIVATE_PERSONAL, [holding("US1", "2", "80", "USD", "US")])
    barrier = public_barrier(
        tmp_path,
        holding_accounts=[personal],
        quotes={"KR": [], "US": [quote("US1", "100", "USD")]},
    )
    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=None
    )
    presentation = project_portfolio_presentation(result.body)
    position = presentation.positions[0]
    assert (
        position.average_cost_native,
        position.current_price_native,
        position.invested_cost_basis_native,
    ) == (Decimal(80), Decimal(100), Decimal(160))
    assert position.evaluated_market_value_native == Decimal(200)
    assert position.unrealized_pnl_native == Decimal(40)
    assert position.evaluated_market_value_krw is None
    assert position.unrealized_pnl_krw is None
    assert position.fx_binding_digest is None
    assert presentation.scopes[0].fx_status is FxStatus.UNAVAILABLE


def test_pc10_actual_inserted_execution_projection_is_lossless_sanitized_and_separate(
    tmp_path,
):
    holdings, trades = account(
        PRIVATE_PERSONAL,
        [holding("SYN", "1", "10", "USD", "US")],
        inserted=[
            trade(
                "private-order-secret-77",
                "US",
                quantity="0.125",
                price="12.34",
                amount="1.5425",
            )
        ],
    )
    barrier = public_barrier(
        tmp_path, holding_accounts=[holdings], trade_accounts=[trades]
    )
    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=None
    )
    execution = result.body.trade_facts.completed_executions[0]
    assert execution.portfolio_scope is PortfolioScope.PERSONAL
    assert execution.transaction_type_raw == "provider-buy"
    assert execution.instrument_raw == "SYN"
    assert (execution.quantity, execution.unit_price, execution.amount) == (
        Decimal("0.125"),
        Decimal("12.34"),
        Decimal("1.5425"),
    )
    assert execution.medium_raw == "provider-medium"
    assert execution.currency_raw == "USD"
    assert execution.classified_market is Market.US
    assert len(execution.source_record_key_digest) == 64
    assert "private-order-secret-77" not in repr(result)
    assert result.body.positions[0].unrealized_pnl_native is None


@pytest.mark.parametrize(
    ("pagination", "duplicates", "expected"),
    [
        (False, (), TradeGlobalQualityState.COMPLETE),
        (False, ("opaque",), TradeGlobalQualityState.UNATTRIBUTED_PARTIAL),
        (True, (), TradeGlobalQualityState.PAGINATION_PENDING),
        (
            True,
            ("opaque",),
            TradeGlobalQualityState.PAGINATION_PENDING_AND_UNATTRIBUTED_PARTIAL,
        ),
    ],
)
def test_pc11_global_trade_quality_overlay_four_states(
    tmp_path, pagination, duplicates, expected
):
    holdings, trades = account(
        PRIVATE_PERSONAL,
        [holding("SYN", "1", "1", "USD", "US")],
        duplicates=duplicates,
    )
    result = compute_portfolio_snapshot(
        public_barrier(
            tmp_path,
            holding_accounts=[holdings],
            trade_accounts=[trades],
            pagination=pagination,
        ),
        resolve_scope=resolver,
        fx_evidence=None,
    )
    quality = result.body.trade_facts.global_quality
    assert quality.state is expected
    assert quality.pagination_pending is pagination
    assert quality.duplicate_count == len(duplicates)
    assert quality.quarantine_count == quality.incomplete_count == 0


def test_pc11_mixed_kr_stale_us_fresh_market_states_and_unattributed_overlay(tmp_path):
    kr_holdings, kr_trades = account(
        PRIVATE_PERSONAL,
        [holding("KR1", "1", "10", "KRW", "KR")],
        inserted=[trade("kr-order", "KR")],
        duplicates=["opaque-kr-account"],
    )
    us_holdings, us_trades = account(
        PRIVATE_PENSION,
        [holding("US1", "1", "10", "USD", "US")],
        inserted=[trade("us-order", "US")],
        incomplete=["opaque-us-account"],
    )
    barrier = public_barrier(
        tmp_path,
        holding_accounts=[kr_holdings, us_holdings],
        trade_accounts=[kr_trades, us_trades],
        freshness_at={(JobKind.TRADES, Market.KR): NOW - timedelta(hours=1)},
    )
    assert barrier.proceed_markets == frozenset({Market.KR, Market.US})
    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=None
    )
    market_rows = {row.market: row for row in result.body.trade_facts.markets}
    assert market_rows[Market.KR].state is TradeMarketState.STALE
    assert market_rows[Market.KR].inserted_count == 1
    assert market_rows[Market.KR].projected_execution_count == 0
    assert market_rows[Market.US].state is TradeMarketState.COMPLETE_WITH_EXECUTIONS
    assert market_rows[Market.US].projected_execution_count == 1
    assert [
        row.classified_market for row in result.body.trade_facts.completed_executions
    ] == [Market.US]
    quality = result.body.trade_facts.global_quality
    assert (
        quality.duplicate_count,
        quality.quarantine_count,
        quality.incomplete_count,
    ) == (1, 0, 1)


def test_pc11_absent_and_complete_empty_market_states(tmp_path):
    holdings, _ = account(PRIVATE_PERSONAL, [holding("US1", "1", "10", "USD", "US")])
    barrier = public_barrier(
        tmp_path,
        holding_accounts=[holdings],
        partitions={
            JobKind.HOLDINGS: ("KR", "US"),
            JobKind.QUOTES: ("KR", "US"),
            JobKind.TRADES: ("US",),
        },
    )
    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=None
    )
    states = {row.market: row for row in result.body.trade_facts.markets}
    assert states[Market.KR].state is TradeMarketState.ABSENT
    assert not states[Market.KR].eligible_for_current_snapshot
    assert states[Market.US].state is TradeMarketState.COMPLETE_EMPTY
    assert states[Market.US].eligible_for_current_snapshot
    assert (
        states[Market.US].inserted_count
        == states[Market.US].projected_execution_count
        == 0
    )


def test_pc11_unresolved_opaque_quality_does_not_resolve_but_eligible_execution_does(
    tmp_path,
):
    unknown = "toss-acct-333333333333333333333333"
    _, opaque_only = account(unknown, duplicates=["opaque"])
    complete = compute_portfolio_snapshot(
        public_barrier(tmp_path / "quality", trade_accounts=[opaque_only]),
        resolve_scope=resolver,
        fx_evidence=None,
    )
    assert complete.status is SnapshotStatus.COMPLETE
    assert complete.body.trade_facts.global_quality.duplicate_count == 1
    _, with_execution = account(unknown, inserted=[trade("private", "US")])
    incomplete = compute_portfolio_snapshot(
        public_barrier(tmp_path / "execution", trade_accounts=[with_execution]),
        resolve_scope=resolver,
        fx_evidence=None,
    )
    assert incomplete.status is SnapshotStatus.SCOPE_RESOLUTION_INCOMPLETE
    assert incomplete.body is None and incomplete.semantic_digest is None


def test_pc6_two_unresolved_trade_only_accounts_return_sanitized_count(tmp_path):
    unknown_a = "toss-acct-333333333333333333333333"
    unknown_b = "toss-acct-444444444444444444444444"
    _, trade_a = account(unknown_a, inserted=[trade("private-a", "US")])
    _, trade_b = account(unknown_b, inserted=[trade("private-b", "KR")])
    result = compute_portfolio_snapshot(
        public_barrier(tmp_path, trade_accounts=[trade_a, trade_b]),
        resolve_scope=resolver,
        fx_evidence=None,
    )
    assert result.status is SnapshotStatus.SCOPE_RESOLUTION_INCOMPLETE
    assert result.unresolved_account_count == 2
    assert result.body is None and result.semantic_digest is None
    assert unknown_a not in repr(result) and unknown_b not in repr(result)
    assert "private-a" not in repr(result) and "private-b" not in repr(result)


def test_pc12_digest_replay_execution_mutation_and_envelope_time_semantics(tmp_path):
    holdings, trades = account(
        PRIVATE_PERSONAL,
        [holding("SYN", "1", "10", "USD", "US")],
        inserted=[trade("private-order", "US", amount="10")],
    )
    barrier_a = public_barrier(
        tmp_path / "a", holding_accounts=[holdings], trade_accounts=[trades]
    )
    barrier_b = public_barrier(
        tmp_path / "b", holding_accounts=[holdings], trade_accounts=[trades]
    )
    result_a = compute_portfolio_snapshot(
        barrier_a, resolve_scope=resolver, fx_evidence=None
    )
    result_b = compute_portfolio_snapshot(
        barrier_b, resolve_scope=resolver, fx_evidence=None
    )
    assert result_a.semantic_digest == result_b.semantic_digest
    assert (
        replace(result_a, computed_at=NOW + timedelta(seconds=1)).semantic_digest
        == result_a.semantic_digest
    )
    _, changed_trades = account(
        PRIVATE_PERSONAL, inserted=[trade("private-order", "US", amount="11")]
    )
    changed = compute_portfolio_snapshot(
        public_barrier(
            tmp_path / "changed",
            holding_accounts=[holdings],
            trade_accounts=[changed_trades],
        ),
        resolve_scope=resolver,
        fx_evidence=None,
    )
    assert changed.semantic_digest != result_a.semantic_digest


def test_pc14_representative_personal_pension_cross_currency_reconciles_and_projects(
    tmp_path,
):
    personal_a, personal_trade = account(
        PRIVATE_PERSONAL,
        [
            holding("DUP", "0.25", "100", "USD", "US"),
            holding("UNPRICED", "2", "25", "USD", "US"),
        ],
        inserted=[trade("personal-private", "US")],
        duplicates=["opaque-duplicate"],
    )
    personal_b, _ = account(
        PRIVATE_PERSONAL, [holding("DUP", "0.75", "200", "USD", "US")]
    )
    pension, pension_trade = account(
        PRIVATE_PENSION,
        [
            holding("DUP", "3", "10000", "KRW", "KR"),
            holding("USD2", "0.5", "40", "USD", "US"),
        ],
        inserted=[trade("pension-private", "KR")],
        quarantine=["opaque-quarantine"],
        incomplete=["opaque-incomplete"],
    )
    barrier = public_barrier(
        tmp_path,
        holding_accounts=[personal_a, personal_b, pension],
        trade_accounts=[personal_trade, pension_trade],
        pagination=True,
        quotes={
            "KR": [quote("DUP", "12000", "KRW")],
            "US": [quote("DUP", "250", "USD"), quote("USD2", "60", "USD")],
        },
    )
    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=valid_fx(barrier)
    )
    assert result.status is SnapshotStatus.COMPLETE
    body = result.body
    keys = [
        (row.portfolio_scope, row.market, row.symbol, row.currency)
        for row in body.positions
    ]
    assert keys == [
        (PortfolioScope.PERSONAL, Market.US, "DUP", "USD"),
        (PortfolioScope.PERSONAL, Market.US, "UNPRICED", "USD"),
        (PortfolioScope.PENSION, Market.KR, "DUP", "KRW"),
        (PortfolioScope.PENSION, Market.US, "USD2", "USD"),
    ]
    personal_dup = body.positions[0]
    assert (personal_dup.quantity, personal_dup.average_cost) == (
        Decimal("1.00"),
        Decimal(175),
    )
    personal_scope = next(
        row
        for row in body.scope_krw_totals
        if row.portfolio_scope is PortfolioScope.PERSONAL
    )
    pension_scope = next(
        row
        for row in body.scope_krw_totals
        if row.portfolio_scope is PortfolioScope.PENSION
    )
    assert personal_scope.internal_total_cost_basis_krw == Decimal("337500.00")
    assert personal_scope.internal_priced_cost_basis_krw == Decimal("262500.00")
    assert personal_scope.priced_market_value_krw == Decimal("375000.00")
    assert personal_scope.unrealized_pnl_krw == Decimal("112500.00")
    assert pension_scope.internal_total_cost_basis_krw == Decimal("60000.0")
    assert pension_scope.priced_market_value_krw == Decimal("81000.0")
    assert pension_scope.unrealized_pnl_krw == Decimal("21000.0")
    assert sum(
        row.scope_krw_total_cost_basis_weight
        for row in body.positions
        if row.portfolio_scope is PortfolioScope.PERSONAL
    ) == Decimal(1)
    assert sum(
        row.scope_krw_total_cost_basis_weight
        for row in body.positions
        if row.portfolio_scope is PortfolioScope.PENSION
    ) == Decimal(1)
    quality = body.trade_facts.global_quality
    assert (
        quality.state
        is TradeGlobalQualityState.PAGINATION_PENDING_AND_UNATTRIBUTED_PARTIAL
    )
    assert (
        quality.duplicate_count,
        quality.quarantine_count,
        quality.incomplete_count,
    ) == (1, 1, 1)
    presentation = project_portfolio_presentation(body)
    assert len(presentation.scopes) == 2
    us_rows = [row for row in presentation.positions if row.currency == "USD"]
    assert all(
        row.evaluated_market_value_krw is not None
        for row in us_rows
        if row.pricing_state is PricingState.PRICED
    )
    assert all(
        not hasattr(row, "average_cost_krw") and not hasattr(row, "current_price_krw")
        for row in us_rows
    )
    assert PRIVATE_PERSONAL not in repr(result) and PRIVATE_PENSION not in repr(result)
    assert "personal-private" not in repr(result) and "pension-private" not in repr(
        result
    )


def test_pc14_non_proceed_retained_material_excluded_and_digest_bound(tmp_path):
    kr_holdings, kr_trades = account(
        PRIVATE_PERSONAL,
        [holding("KR1", "1", "10", "KRW", "KR")],
        inserted=[trade("kr-private", "KR")],
    )
    us_holdings, us_trades = account(
        PRIVATE_PENSION,
        [holding("US1", "1", "10", "USD", "US")],
        inserted=[trade("us-private", "US")],
    )
    barrier = public_barrier(
        tmp_path,
        holding_accounts=[kr_holdings, us_holdings],
        trade_accounts=[kr_trades, us_trades],
        freshness_at={(JobKind.HOLDINGS, Market.KR): NOW - timedelta(hours=1)},
    )
    assert Market.KR not in barrier.proceed_markets
    assert JobKind.HOLDINGS in barrier.materials
    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=None
    )
    assert all(row.market is not Market.KR for row in result.body.positions)
    population = {row.market: row for row in result.body.market_population}
    assert not population[Market.KR].proceed
    assert population[Market.KR].holdings_material_present
    trade_market = {row.market: row for row in result.body.trade_facts.markets}
    assert trade_market[Market.KR].state is TradeMarketState.EXCLUDED_NON_PROCEED
    assert trade_market[Market.KR].inserted_count == 1
    assert trade_market[Market.KR].projected_execution_count == 0
    assert result.semantic_digest


@pytest.mark.parametrize("market", [Market.KR, Market.US])
@pytest.mark.parametrize(
    "readiness",
    [
        MarketReadiness.READY,
        MarketReadiness.SKIP_CLOSED,
        MarketReadiness.SKIP_HOLDINGS_STALE,
        MarketReadiness.SKIP_HOLDINGS_MISSING,
        MarketReadiness.UNAVAILABLE,
    ],
)
def test_pc6a_public_barrier_population_matrix_excludes_non_ready_market(
    tmp_path, market, readiness
):
    other = Market.US if market is Market.KR else Market.KR
    holdings, trades = account(
        PRIVATE_PERSONAL,
        [
            holding("KR1", "1", "1000", "KRW", "KR"),
            holding("US1", "1", "10", "USD", "US"),
        ],
        [
            trade("kr-population", "KR", price="1000", amount="1000"),
            trade("us-population", "US", price="10", amount="10"),
        ],
    )
    kwargs = {
        "holding_accounts": [holdings],
        "trade_accounts": [trades],
        "quotes": {
            "KR": [quote("KR1", "1200", "KRW")],
            "US": [quote("US1", "12", "USD")],
        },
    }
    if readiness is MarketReadiness.SKIP_CLOSED:
        kwargs["open_markets"] = frozenset({other})
    elif readiness is MarketReadiness.SKIP_HOLDINGS_STALE:
        kwargs["freshness_at"] = {
            (JobKind.HOLDINGS, market): NOW - timedelta(seconds=601)
        }
    elif readiness is MarketReadiness.SKIP_HOLDINGS_MISSING:
        kwargs["partitions"] = {
            JobKind.HOLDINGS: (other.value,),
            JobKind.QUOTES: ("KR", "US"),
            JobKind.TRADES: ("KR", "US"),
        }
    elif readiness is MarketReadiness.UNAVAILABLE:
        kwargs["open_markets"] = frozenset({market})
        kwargs["unavailable_jobs"] = frozenset({JobKind.HOLDINGS})
    barrier = public_barrier(tmp_path, **kwargs)
    assert barrier.market_readiness[market] is readiness

    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=None
    )
    assert result.status is SnapshotStatus.COMPLETE
    population = next(
        row for row in result.body.market_population if row.market is market
    )
    assert population.readiness is readiness
    assert population.proceed is (readiness is MarketReadiness.READY)
    target_positions = tuple(
        row for row in result.body.positions if row.market is market
    )
    target_partitions = tuple(
        row for row in result.body.partition_totals if row.market is market
    )
    trade_state = next(
        row for row in result.body.trade_facts.markets if row.market is market
    )
    if readiness is MarketReadiness.READY:
        assert len(target_positions) == 1 and len(target_partitions) == 1
        assert population.included_position_count == 1
        assert population.included_execution_count == 1
        assert trade_state.state is TradeMarketState.COMPLETE_WITH_EXECUTIONS
    else:
        assert target_positions == () and target_partitions == ()
        assert population.included_position_count == 0
        assert population.included_execution_count == 0
        expected = (
            TradeMarketState.ABSENT
            if readiness is MarketReadiness.SKIP_CLOSED
            else TradeMarketState.EXCLUDED_NON_PROCEED
        )
        assert trade_state.state is expected
        assert trade_state.projected_execution_count == 0


def test_pc6a_each_barrier_dimension_changes_digest_in_isolation(tmp_path):
    holdings, trades = account(
        PRIVATE_PERSONAL,
        [holding("US1", "1", "10", "USD", "US")],
        [trade("us-digest", "US", price="10", amount="10")],
    )
    barrier = public_barrier(
        tmp_path,
        holding_accounts=[holdings],
        trade_accounts=[trades],
        quotes={"KR": [], "US": [quote("US1", "12", "USD")]},
    )
    original = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=None
    )
    mutations = (
        replace(barrier, proceed_markets=frozenset({Market.KR})),
        replace(
            barrier,
            market_readiness={
                **barrier.market_readiness,
                Market.US: MarketReadiness.SKIP_CLOSED,
            },
        ),
        replace(
            barrier,
            stale_jobs={**barrier.stale_jobs, Market.US: frozenset({JobKind.QUOTES})},
        ),
    )
    for mutation in mutations:
        changed = compute_portfolio_snapshot(
            mutation, resolve_scope=resolver, fx_evidence=None
        )
        assert changed.semantic_digest != original.semantic_digest


def test_missing_quote_key_under_stale_quote_pack_is_fail_closed_stale(tmp_path):
    holdings, _ = account(PRIVATE_PERSONAL, [holding("US1", "1", "10", "USD", "US")])
    barrier = public_barrier(
        tmp_path,
        holding_accounts=[holdings],
        quotes={"KR": [], "US": []},
        freshness_at={(JobKind.QUOTES, Market.US): NOW - timedelta(seconds=601)},
    )
    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=None
    )
    position = next(row for row in result.body.positions if row.market is Market.US)
    # A stale pack cannot prove that a missing key is truly absent, so fail closed.
    assert position.pricing_state is PricingState.STALE_QUOTE
    assert position.current_price is None
    assert position.priced_market_value_native is None


def test_all_markets_closed_is_explicit_pre_wiring_unsupported_disposition(tmp_path):
    barrier = public_barrier(tmp_path, open_markets=frozenset())
    assert barrier.proceed_markets == frozenset() and barrier.materials == {}
    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=None
    )
    # SnapshotStatus has no normative NO_OPEN_MARKETS member and §7.1 has no
    # reference_at without material. Future wiring must skip this calculation.
    assert result.status is SnapshotStatus.MATERIAL_INVALID
    assert result.invalid_reason == "HOLDINGS_MATERIAL_ABSENT"
    assert result.body is None and result.semantic_digest is None


def test_evidence_only_empty_snapshot_uses_latest_verified_capture_for_reference(
    tmp_path,
):
    quote_capture = NOW - timedelta(minutes=2)
    trade_capture = NOW - timedelta(minutes=1)
    barrier = public_barrier(
        tmp_path,
        captured={JobKind.QUOTES: quote_capture, JobKind.TRADES: trade_capture},
        freshness_at={(JobKind.QUOTES, market): quote_capture for market in Market}
        | {(JobKind.TRADES, market): trade_capture for market in Market},
        unavailable_jobs=frozenset({JobKind.HOLDINGS}),
    )
    assert barrier.proceed_markets == frozenset()
    assert JobKind.HOLDINGS not in barrier.materials
    assert set(barrier.materials) == {JobKind.QUOTES, JobKind.TRADES}
    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=None
    )
    assert result.status is SnapshotStatus.COMPLETE
    assert result.body.reference_at == trade_capture
    assert result.body.positions == ()
    assert result.body.partition_totals == ()
    assert result.semantic_digest == _body_digest(result.body)


def test_pc15_pension_live_reconciliation_remains_unverified_ud1():
    pytest.skip(
        "UNVERIFIED_UD-1: no approved canonical account-scope mapping is configured; "
        "the live unresolved fail-closed path was verified read-only, while a "
        "PENSION-mapped successful reconciliation cannot be exercised without guessing"
    )


def test_pc9_concentration_top_five_ties_unpriced_and_exact_weights(tmp_path):
    costs = {
        "A": "100",
        "B": "100",
        "C": "90",
        "D": "80",
        "E": "70",
        "F": "60",
        "G": "50",
        "H": "1000",
    }
    holdings, _ = account(
        PRIVATE_PERSONAL,
        [holding(symbol, "1", cost, "USD", "US") for symbol, cost in costs.items()],
    )
    quoted = {
        "A": "100",
        "B": "100",
        "C": "90",
        "D": "80",
        "E": "70",
        "F": "60",
        "G": "50",
    }
    barrier = public_barrier(
        tmp_path,
        holding_accounts=[holdings],
        quotes={
            "KR": [],
            "US": [quote(symbol, value, "USD") for symbol, value in quoted.items()],
        },
    )
    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=valid_fx(barrier)
    )
    scope = result.body.scope_krw_totals[0]
    assert scope.concentration_top_n == 5
    assert [row.symbol for row in scope.top_by_priced_market_value] == [
        "A",
        "B",
        "C",
        "D",
        "E",
    ]
    assert [row.symbol for row in scope.top_by_total_cost_basis] == [
        "H",
        "A",
        "B",
        "C",
        "D",
    ]
    assert "H" not in {row.symbol for row in scope.top_by_priced_market_value}
    with localcontext() as context:
        context.prec = 50
        expected_market_weight = Decimal(150000) / Decimal(825000)
        expected_cost_weight = Decimal(1500000) / Decimal(2325000)
    assert (
        scope.top_by_priced_market_value[0].scope_krw_weight == expected_market_weight
    )
    assert scope.top_by_total_cost_basis[0].scope_krw_weight == expected_cost_weight
    priced_weights = [
        row.scope_krw_priced_market_value_weight
        for row in result.body.positions
        if row.priced_market_value_krw is not None
    ]
    cost_weights = [
        row.scope_krw_total_cost_basis_weight for row in result.body.positions
    ]
    assert sum(priced_weights, Decimal(0)) == Decimal(1)
    assert sum(cost_weights, Decimal(0)) == Decimal(1)

    unavailable = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=None
    )
    unavailable_scope = unavailable.body.scope_krw_totals[0]
    assert unavailable_scope.fx_status is FxStatus.UNAVAILABLE
    assert unavailable_scope.top_by_priced_market_value == ()
    assert unavailable_scope.top_by_total_cost_basis == ()
    assert all(
        row.scope_krw_priced_market_value_weight is None
        for row in unavailable.body.positions
    )
    assert all(
        row.scope_krw_total_cost_basis_weight is None
        for row in unavailable.body.positions
    )


def test_unpriced_only_partition_return_is_unavailable_not_zero_cost(tmp_path):
    holdings, _ = account(
        PRIVATE_PERSONAL, [holding("NOQUOTE", "1", "10", "USD", "US")]
    )
    barrier = public_barrier(
        tmp_path, holding_accounts=[holdings], quotes={"KR": [], "US": []}
    )
    result = compute_portfolio_snapshot(
        barrier, resolve_scope=resolver, fx_evidence=valid_fx(barrier)
    )
    partition = result.body.partition_totals[0]
    assert partition.total_cost_basis_native == Decimal(10)
    assert partition.priced_position_count == 0
    assert partition.priced_cost_basis_native == Decimal(0)
    assert partition.unrealized_return.state is RatioState.UNAVAILABLE_UNPRICED
    assert partition.unrealized_return.value is None


def test_pc16_future_candidate_contract_uses_only_bounded_projection_types():
    position_fields = {field.name for field in fields(PositionPresentationV1)}
    assert {
        "portfolio_scope",
        "quantity",
        "average_cost_native",
        "current_price_native",
        "invested_cost_basis_native",
        "evaluated_market_value_native",
        "evaluated_market_value_krw",
        "unrealized_pnl_native",
        "unrealized_pnl_krw",
    } <= position_fields
    assert (
        not {
            "average_cost_krw",
            "current_price_krw",
            "total_cost_basis_krw",
            "priced_cost_basis_krw",
            "account_scope",
        }
        & position_fields
    )
