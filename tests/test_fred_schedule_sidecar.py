"""SDS AR-1/AR-2/AR-23: the moved schedule sidecar module and its parity.

This test file exercises the shared symbols moved from
``tools/fred_ondemand_canary.py`` into ``src/tokmo/records/fred_schedule_sidecar.py``
so that both the tool and the Macro caller share one definition of the sidecar
format. Every assertion here is a parity proof against the tool's own tests.
"""

import ast
from datetime import UTC, date, datetime

import pytest

from tokmo.values.fred_query import (
    SERIES_PAYEMS,
    SERIES_REGISTRY,
    release_slot_for,
)

ROOT = __import__("pathlib").Path(__file__).parents[1]
UTC = UTC
NOW = datetime(2026, 8, 7, 12, 45, tzinfo=UTC)
RELEASE_ID = 50
RELEASE_DATE = date(2026, 8, 7)
SLOT = "PAYEMS:2026-08-07"
OFFICIAL_AT = datetime(2026, 8, 7, 12, 30, tzinfo=UTC)


# --- AR-2: release_slot_for ------------------------------------------------


def test_release_slot_for_matches_the_inline_fstring_for_every_registered_series():
    """``release_slot_for(series_id, date)`` must equal ``f"{series_id}:{isoformat()}"``."""
    sample_dates = [date(2026, 8, 7), date(2026, 1, 9), date(2025, 12, 31)]
    for series_id in SERIES_REGISTRY:
        for release_date in sample_dates:
            assert release_slot_for(series_id, release_date) == (
                f"{series_id}:{release_date.isoformat()}"
            )


def test_release_slot_for_rejects_unregistered_series():
    with pytest.raises(ValueError, match="unregistered_series"):
        release_slot_for("UNRATE", date(2026, 8, 7))


def test_release_slot_for_rejects_non_date():
    with pytest.raises(ValueError, match="invalid_release_date"):
        release_slot_for(SERIES_PAYEMS, "2026-08-07")


# --- AR-23: the sidecar module opens no host, home or clock -----------------


def _imports(relative):
    names = []
    for node in ast.walk(ast.parse((ROOT / relative).read_text())):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_the_sidecar_records_module_opens_no_host_home_or_clock():
    text = (ROOT / "src/tokmo/records/fred_schedule_sidecar.py").read_text()
    for banned in (
        "bls.gov",
        "BLS_SOURCE_URL",
        "news_release",
        "https://",
        "Path.home()",
        "datetime.now(",
        "utcnow(",
        "os.environ",
        "getenv(",
        "urlopen(",
    ):
        assert banned not in text, banned
    names = _imports("src/tokmo/records/fred_schedule_sidecar.py")
    assert not any(
        name.startswith(("tokmo.facts", "tokmo.programs", "tokmo.runtime"))
        for name in names
    )
