"""The single ambient wall-clock leaf in the tree (live contract §7).

Only the live CLI in the composition root may call ``utc_now`` to build the
clock it injects, or ``sleep_seconds`` to build the pacer it injects; every
other module keeps receiving them as callables. The architecture scan
allowlists exactly this file for the direct clock call.
"""

import time
from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def sleep_seconds(seconds: float) -> None:
    """Real elapsed-time pacing between bounded campaign request-starts."""
    time.sleep(seconds)
