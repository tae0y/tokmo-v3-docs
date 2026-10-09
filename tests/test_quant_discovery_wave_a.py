import json
from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from tokmo.programs.quant_discovery_entry import build_reference_entry_zone
from tokmo.programs.quant_discovery_features import compute_price_features
from tokmo.programs.quant_discovery_ranking import compute_peer_standing
from tokmo.values.quant_discovery import (
    FactorOrientation,
    FactorStatus,
    FactorValueV1,
    PriceBarV1,
    PriceSeriesV1,
    QuantMarketProfileV1,
    load_peer_registry,
)

D = Decimal


def _series(closes, *, highs=True, volume=True):
    start = date(2025, 1, 1)
    bars = tuple(
        PriceBarV1(
            start + timedelta(days=i),
            D(str(c)),
            D(str(c)) + D("1") if highs else None,
            D(str(c)) - D("1") if highs else None,
            D(str(c)),
            D(str(100 + i)) if volume else None,
            D(str(1000 + 10 * i)) if volume else None,
        )
        for i, c in enumerate(closes)
    )
    return PriceSeriesV1("AAA", QuantMarketProfileV1.KR, bars)


def _factor(features, name):
    return next(f for f in features.factors if f.name == name)


def _registry_group(**changes):
    group = {
        "group_id": "g1",
        "product_kind": "PERSONAL",
        "market": "KR",
        "instrument_type": "ETF",
        "reference_symbol": "AAA",
        "peer_symbols": ["BBB"],
        "provenance_note": "reviewed public registry",
        "last_reviewed": "2026-08-24",
    }
    group.update(changes)
    return group


def _write_registry(path, group):
    data = {
        "schema": "quant-peer-registry-v1",
        "revision": "seed-1",
        "reviewed_at": "2026-08-24",
        "groups": [group],
    }
    path.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n")


def test_feature_golden_and_insufficiency():
    series = _series(range(1, 201))
    outcome = compute_price_features(
        series, completed_session_date=series.bars[-1].session_date
    )
    assert _factor(outcome.features, "ret_5").raw_value == D("0.025641")
    assert _factor(outcome.features, "price_position_200").raw_value == D("1.000000")
    assert _factor(outcome.features, "volume_change_20").raw_value == D("0.074212")
    assert (
        _factor(outcome.features, "realized_vol_63").orientation
        is FactorOrientation.LOWER_IS_BETTER
    )

    short = _series(range(1, 6))
    result = compute_price_features(
        short, completed_session_date=short.bars[-1].session_date
    )
    assert result.unavailable_code.value == "INSUFFICIENT_PRICE_HISTORY"
    assert _factor(result.features, "ret_5").status is FactorStatus.INSUFFICIENT
    assert _factor(result.features, "max_drawdown").raw_value == D("0.000000")


def test_optional_activity_history_does_not_block_price_outcome_or_entry_zone():
    setup_closes = [D("70") + D(i) / D("2") for i in range(46)]
    setup_closes += [D("93") if i % 2 == 0 else D("92.5") for i in range(14)]
    series = _series([D("50")] * 140 + setup_closes, volume=False)

    outcome = compute_price_features(
        series, completed_session_date=series.bars[-1].session_date
    )

    assert _factor(outcome.features, "ret_120").status is FactorStatus.AVAILABLE
    assert (
        _factor(outcome.features, "price_position_200").status is FactorStatus.AVAILABLE
    )
    assert (
        _factor(outcome.features, "volume_change_20").status
        is FactorStatus.INSUFFICIENT
    )
    assert (
        _factor(outcome.features, "trading_amount_change_20").status
        is FactorStatus.INSUFFICIENT
    )
    assert outcome.unavailable_code is None
    assert build_reference_entry_zone(series, outcome.features) is not None


def test_dto_invariants_and_immutability():
    series = _series([1, 2])
    with pytest.raises(FrozenInstanceError):
        series.symbol = "B"
    with pytest.raises(ValueError):
        PriceSeriesV1("A", QuantMarketProfileV1.KR, (series.bars[1], series.bars[0]))
    with pytest.raises(ValueError):
        PriceBarV1(date.today(), D("1"), D("0"), D("2"), D("1"), None, None)
    with pytest.raises(ValueError):
        FactorValueV1(
            "x",
            D("NaN"),
            FactorOrientation.HIGHER_IS_BETTER,
            FactorStatus.AVAILABLE,
            1,
            1,
        )


def test_peer_standing_nine_ten_and_average_tie():
    nine = tuple(
        ("f", f"S{i}", D(i), FactorOrientation.HIGHER_IS_BETTER, False)
        for i in range(9)
    )
    standings = compute_peer_standing(nine)
    assert standings[0].symbol == "S8"
    assert standings[0].percentile is None

    ten = tuple(
        ("f", f"S{i}", D(i), FactorOrientation.HIGHER_IS_BETTER, True)
        for i in range(10)
    )
    standings = compute_peer_standing(ten)
    assert standings[0].percentile == D("100.0")
    assert standings[-1].percentile == D("0.0")
    assert standings[0].percentile_label == "검수 peer 10개 중 백분위 100.0"

    tied = compute_peer_standing(
        (
            ("f", "A", D("2"), FactorOrientation.HIGHER_IS_BETTER, False),
            ("f", "B", D("2"), FactorOrientation.HIGHER_IS_BETTER, False),
            ("f", "C", D("1"), FactorOrientation.HIGHER_IS_BETTER, False),
        )
    )
    assert tied[0].ordinal == tied[1].ordinal == D("1.5")


def test_entry_trend_pullback_and_none():
    closes = [D("70") + D(i) / D("2") for i in range(46)]
    closes += [D("93") if i % 2 == 0 else D("92.5") for i in range(14)]
    series = _series(closes)
    features = compute_price_features(
        series, completed_session_date=series.bars[-1].session_date
    ).features
    zone = build_reference_entry_zone(series, features)
    assert zone is not None
    assert zone.setup_kind == "TREND_PULLBACK"
    assert zone.expiry_completed_sessions == 1
    assert zone.confidence == 50

    falling = _series(reversed(closes))
    falling_features = compute_price_features(
        falling, completed_session_date=falling.bars[-1].session_date
    ).features
    assert build_reference_entry_zone(falling, falling_features) is None

    missing_open = replace(
        series, bars=series.bars[:-1] + (replace(series.bars[-1], open=None),)
    )
    assert build_reference_entry_zone(missing_open, features) is None


def test_registry_canonical_and_privacy_rejection(tmp_path):
    seed = {
        "schema": "quant-peer-registry-v1",
        "revision": "seed-1",
        "reviewed_at": "2026-08-24",
        "groups": [],
    }
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(seed, sort_keys=True, separators=(",", ":")) + "\n")
    assert load_peer_registry(path).groups == ()

    noncanonical = tmp_path / "pretty.json"
    noncanonical.write_text(json.dumps(seed, indent=2))
    with pytest.raises(ValueError, match="canonical"):
        load_peer_registry(noncanonical)

    private = dict(seed, account_id="secret")
    path.write_text(json.dumps(private, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(ValueError, match="unknown"):
        load_peer_registry(path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("group_id", ""),
        ("instrument_type", None),
        ("reference_symbol", {}),
        ("provenance_note", 1),
    ],
)
def test_registry_required_scalars_raise_value_error(tmp_path, field, value):
    path = tmp_path / "registry.json"
    _write_registry(path, _registry_group(**{field: value}))

    with pytest.raises(ValueError, match="non-empty string"):
        load_peer_registry(path)


@pytest.mark.parametrize(
    "label,value",
    [
        ("industry", {"mutable": "mapping"}),
        ("theme", ["clean", ["nested"]]),
        ("duration", float("inf")),
    ],
)
def test_registry_rejects_mutable_or_nested_optional_labels(tmp_path, label, value):
    path = tmp_path / "registry.json"
    _write_registry(path, _registry_group(**{label: value}))

    with pytest.raises(ValueError, match="label"):
        load_peer_registry(path)


@pytest.mark.parametrize("leverage", [True, "NaN"])
def test_registry_leverage_validation_and_safe_label_conversion(tmp_path, leverage):
    path = tmp_path / "registry.json"
    _write_registry(path, _registry_group(leverage_factor=leverage))
    with pytest.raises(ValueError, match="leverage_factor"):
        load_peer_registry(path)

    _write_registry(
        path,
        _registry_group(leverage_factor="2.0", hedge=True, exposure=["large", 100]),
    )
    labels = dict(load_peer_registry(path).groups[0].labels)
    assert labels == {
        "exposure": ("large", 100),
        "hedge": True,
        "leverage_factor": "2.0",
    }
