from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from tokmo.facts.market_regime_toss_input import (
    candle_native_identity,
    completed_session_boundary,
    normalize_daily_candles,
)
from tokmo.values.market_regime_core import AdjustmentMode
from tokmo.values.session_gate import Market


def _row(day: int):
    stamp = datetime(2026, 1, day, 9, tzinfo=UTC)
    return {
        "timestamp": stamp,
        "open": Decimal(10),
        "high": Decimal(12),
        "low": Decimal(9),
        "close": Decimal(11),
        "volume": Decimal(100),
        "currency": "KRW",
    }


def test_boundary_identity_and_descending_normalization():
    boundary = completed_session_boundary(Market.KR, date(2026, 1, 3))
    assert boundary == datetime(2026, 1, 3, 23, 59, 59, tzinfo=ZoneInfo("Asia/Seoul"))
    assert candle_native_identity("mr", Market.KR, "005930", date(2026, 1, 3), 1) == (
        "candles:mr:KR:005930:2026-01-03:1"
    )
    rows = tuple(
        {
            **_row(3),
            "timestamp": datetime(2026, 1, 3, 9, tzinfo=UTC) - timedelta(days=i),
        }
        for i in range(200)
    )
    series = normalize_daily_candles(
        rows,
        market=Market.KR,
        symbol="005930",
        completed_session=date(2026, 1, 3),
        adjustment_mode=AdjustmentMode.ADJUSTED,
    )
    assert series.as_of == date(2026, 1, 3)
    assert [bar.session_date for bar in series.bars] == sorted(
        bar.session_date for bar in series.bars
    )
    assert all(bar.timestamp.tzinfo == ZoneInfo("Asia/Seoul") for bar in series.bars)


def test_future_cutoff_cannot_hide_a_199_bar_partial_input():
    rows = tuple(
        {
            **_row(3),
            "timestamp": datetime(2026, 1, 4, 9, tzinfo=UTC) - timedelta(days=i),
        }
        for i in range(200)
    )
    with pytest.raises(ValueError, match="must be 200"):
        normalize_daily_candles(
            rows,
            market=Market.KR,
            symbol="005930",
            completed_session=date(2026, 1, 3),
            adjustment_mode=AdjustmentMode.ADJUSTED,
        )


@pytest.mark.parametrize("days", [(3, 3, 2), (3, 1, 2)])
def test_normalizer_rejects_duplicate_or_non_descending(days):
    with pytest.raises(ValueError):
        normalize_daily_candles(
            tuple(_row(day) for day in days),
            market=Market.KR,
            symbol="005930",
            completed_session=date(2026, 1, 3),
            adjustment_mode=AdjustmentMode.ADJUSTED,
        )
