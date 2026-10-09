from datetime import UTC, datetime, timedelta

from tokmo.values.ops_tick import (
    TICK_SECONDS,
    tick_id_from_epoch_seconds,
    tick_id_from_utc,
)


def test_tick_identity_is_five_minute_floor_for_all_collector_offsets():
    boundary = datetime(2026, 7, 30, 0, 0, tzinfo=UTC)
    expected = int(boundary.timestamp()) // 300

    assert TICK_SECONDS == 300
    assert tick_id_from_epoch_seconds(int(boundary.timestamp())) == expected
    for offset in (0, 20, 40, 120):
        tick = tick_id_from_utc(boundary + timedelta(seconds=offset))
        assert tick.tick_id == expected
        assert tick.epoch == int((boundary + timedelta(seconds=offset)).timestamp())
        assert tick.start_utc == boundary
