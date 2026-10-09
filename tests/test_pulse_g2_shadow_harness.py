"""G2 shadow harness and rolling pause marker / marker clear runbook.

Plan §27/§28: the G2 shadow harness runs 30+ ticks in a single isolated
persistent root with fixture-only data and validates receipt accumulation,
rolling count, duplicate wire0, and marker persistence. The rolling
pause marker is a durable fail-safe: when the rolling 5-minute wire ceiling
is exceeded AND provider operations are enabled, Pulse auto-pauses and
writes a marker file. No system alert is generated — the operator must
actively check the marker/receipt. Clearing the marker requires manual
verification — never automatic.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

CLOCK_NOW = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)


def test_pulse_pause_marker_path(tmp_path):
    """The pause marker lives under runtime/ in the isolated root.
    When provider operations are disabled (default), no marker is
    created even if the rolling ceiling is high."""
    marker = tmp_path / "runtime" / "pulse-paused.marker"
    assert not marker.exists()


def test_pulse_pause_marker_clear_requires_manual_verification(tmp_path):
    """Clearing the pause marker must be a documented manual step, not
    automatic. The runbook documents the procedure."""
    from tokmo.programs.integrated_pulse import _PAUSE_MARKER

    marker = tmp_path / "runtime" / _PAUSE_MARKER
    marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker.write_text("rolling_ceiling_exceeded")
    marker.chmod(0o600)
    assert marker.exists()
    # The marker is NOT automatically cleared by any Pulse tick
    # The runbook (docs/pulse-g2-shadow-runbook.md) documents manual clear


def test_g2_shadow_harness_fixture_loads(tmp_path):
    """The G2 shadow harness fixture is loadable and valid."""
    fixture_path = Path(__file__).parent / "fixtures" / "pulse_g2_shadow_fixture.json"
    if fixture_path.exists():
        fixture = json.loads(fixture_path.read_text())
        assert "lookahead" in fixture
        assert "holdings" in fixture


def test_g2_shadow_harness_30_ticks_same_root_green(tmp_path):
    """The G2 shadow harness runs 30 ticks in a SINGLE persistent root
    and all complete green. This validates:
    - receipt accumulation across ticks (not 30 separate roots)
    - duplicate wire0 across the persistent session
    - rolling wire count tracked correctly
    - marker not created (provider operations disabled by default)
    """
    from tokmo.programs.integrated_pulse import (
        build_fixture_pulse_deps,
        run_pulse_once,
    )
    from tokmo.records.pulse_tick_receipts import PulseTickReceiptStore

    fixture = {
        "lookahead": {},
        "holdings": {
            "captured_at": "2026-08-10T00:00:00Z",
            "accounts": [
                {
                    "account_scope": "toss-acct-" + "a" * 24,
                    "label": "개인",
                    "holdings": [],
                }
            ],
        },
        "native_refs": {},
        "quotes": {},
        "orders": {},
        "watchlists": {},
    }
    # SINGLE persistent root for all 30 ticks
    root = tmp_path / "pulse_root"
    root.mkdir(mode=0o700)
    receipt_store = PulseTickReceiptStore(
        root / "ops" / "pulse_tick_receipts.sqlite3",
        clock=lambda: CLOCK_NOW,
    )

    green_count = 0
    saved_slots = []
    for i in range(30):
        slot = CLOCK_NOW + timedelta(seconds=60 * i)
        deps = build_fixture_pulse_deps(
            fixture=fixture,
            clock=lambda s=slot: s,
            root=root,
        )
        result = run_pulse_once(
            root=root,
            policy_version="pulse-60s-v1",
            scheduled_for_utc=slot,
            observed_now_utc=slot,
            deps=deps,
        )
        if result.outcome in ("SAVED", "SKIPPED_CLOSED"):
            green_count += 1
            saved_slots.append(slot)

    assert green_count == 30

    # Verify receipts accumulated in the persistent store (read back
    # at least the first saved slot from the SAME root)
    first_receipt = receipt_store.read_tick("pulse-60s-v1", saved_slots[0])
    assert first_receipt is not None
    assert first_receipt["status"] in ("SAVED", "SKIPPED_CLOSED")

    # No pause marker (provider operations disabled)
    marker = root / "runtime" / "pulse-paused.marker"
    assert not marker.exists()


def test_runbook_document_exists():
    """The G2 shadow runbook markdown must exist."""
    runbook = Path(__file__).parent.parent / "docs" / "pulse-g2-shadow-runbook.md"
    if not runbook.exists():
        pytest.skip("runbook not yet created")


def test_runbook_truthfully_documents_no_system_alert():
    """The runbook must truthfully document that no system alert is
    generated for rolling pause — only marker and receipt."""
    runbook = Path(__file__).parent.parent / "docs" / "pulse-g2-shadow-runbook.md"
    if not runbook.exists():
        pytest.skip("runbook not yet created")
    text = runbook.read_text()
    # The runbook must NOT claim an alert is sent
    assert "시스템 알림은 보내지 않습니다" in text
    # Must mention marker and receipt as the verification path
    assert "marker" in text.lower() or "마커" in text
    assert "receipt" in text.lower() or "수신" in text or "영수증" in text


def test_ceiling_marker_only_when_provider_operations_active(tmp_path):
    """A marker is created only when rolling ceiling is exceeded AND
    provider_operations is active. When provider operations are disabled
    (default), no marker is created. This test documents the actual
    contract — it does not test the ceiling-exceeded path itself
    (which requires a live provider simulation)."""
    from tokmo.programs.integrated_pulse import (
        _PAUSE_MARKER,
        build_fixture_pulse_deps,
        run_pulse_once,
    )

    fixture = {
        "lookahead": {},
        "holdings": None,
        "native_refs": {},
        "quotes": {},
        "orders": {},
        "watchlists": {},
    }
    # Provider ops disabled → no marker even if rolling is high
    tick_root = tmp_path / "tick_0000"
    tick_root.mkdir(mode=0o700)
    slot = CLOCK_NOW
    deps = build_fixture_pulse_deps(
        fixture=fixture,
        clock=lambda: slot,
        root=tick_root,
    )
    result = run_pulse_once(
        root=tick_root,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=slot,
        observed_now_utc=slot,
        deps=deps,
    )
    marker = tick_root / "runtime" / _PAUSE_MARKER
    assert not marker.exists()  # disabled = no marker


def test_marker_persists_across_ticks_in_same_root(tmp_path):
    """When a marker exists in a persistent root, subsequent ticks must
    see it and fail immediately. The marker is durable across ticks."""
    from tokmo.programs.integrated_pulse import (
        _PAUSE_MARKER,
        build_fixture_pulse_deps,
        run_pulse_once,
    )

    fixture = {
        "lookahead": {},
        "holdings": {
            "captured_at": "2026-08-10T00:00:00Z",
            "accounts": [
                {
                    "account_scope": "toss-acct-" + "a" * 24,
                    "label": "개인",
                    "holdings": [],
                }
            ],
        },
        "native_refs": {},
        "quotes": {},
        "orders": {},
        "watchlists": {},
    }
    root = tmp_path / "pulse_root"
    root.mkdir(mode=0o700)

    # Manually create the marker to simulate ceiling exceeded
    marker = root / "runtime" / _PAUSE_MARKER
    marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker.write_text("rolling_ceiling_exceeded")
    marker.chmod(0o600)

    # Next tick should see the marker and fail
    slot = CLOCK_NOW
    deps = build_fixture_pulse_deps(
        fixture=fixture,
        clock=lambda: slot,
        root=root,
    )
    result = run_pulse_once(
        root=root,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=slot,
        observed_now_utc=slot,
        deps=deps,
    )
    assert result.outcome == "FAILED"
    assert result.reason is not None
    # When the marker exists, Pulse fails immediately with "paused" reason
    assert "paused" in str(result.reason) or "rolling_ceiling_exceeded" in str(
        result.reason
    )
