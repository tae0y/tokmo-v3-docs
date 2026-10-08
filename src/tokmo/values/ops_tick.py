"""Five-minute operations tick identity."""

from dataclasses import dataclass
from datetime import datetime, timezone

TICK_SECONDS = 300  # One-minute operation requires a separate scheduler gate.


@dataclass(frozen=True)
class TickId:
    epoch: int
    tick_id: int
    start_utc: datetime


def tick_id_from_epoch_seconds(seconds: int) -> int:
    if type(seconds) is not int:
        raise TypeError("epoch seconds must be an int")
    return seconds // TICK_SECONDS


def tick_id_from_utc(now: datetime) -> TickId:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    epoch = int(now.timestamp())
    tick_id = tick_id_from_epoch_seconds(epoch)
    return TickId(
        epoch=epoch,
        tick_id=tick_id,
        start_utc=datetime.fromtimestamp(tick_id * TICK_SECONDS, tz=timezone.utc),
    )
