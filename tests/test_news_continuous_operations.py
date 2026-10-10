"""Explicit ongoing delivery preserves expiry defaults and fresh-value review."""

import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import pytest
from test_news_g3_live_lane import LIVE_WINDOW_END, NOW, _live_body, _write_gate
from test_news_observation import NOW as REPORT_NOW
from test_news_observation import (
    build_report,
    environment,  # pytest fixture
    put,
)

from tokmo.programs.news_alert_delivery import live_delivery_authorized
from tokmo.programs.news_decision_engine import morning
from tokmo.programs.pulse_production_deps import read_news_gate
from tokmo.records.news_decision_store import kst


class Ledger:
    proven = True
    tripped = False

    def canary_attempt(self, _):
        return {"outcome": "SENT"} if self.proven else None

    def canary_slot_proof(self, _):
        return {"proof": True}

    def live_trip(self, _):
        return {"reason": "unknown delivery"} if self.tripped else None


def test_continuous_live_has_no_expiry_but_keeps_cutoff_and_delivery_proof(tmp_path):
    _write_gate(tmp_path, _live_body(continuous=True, window_end=None))
    d = read_news_gate(tmp_path).delivery
    ledger = Ledger()
    later = NOW + timedelta(days=365)
    assert d.continuous and d.window_end is None and d.live_active(later)
    assert live_delivery_authorized(ledger, d, later) == (True, None)
    assert live_delivery_authorized(ledger, d, d.cutoff - timedelta(seconds=1)) == (
        False,
        "before_cutoff",
    )
    ledger.proven = False
    assert live_delivery_authorized(ledger, d, later) == (False, "predecessor_unproven")
    ledger.proven = True
    ledger.tripped = True
    assert live_delivery_authorized(ledger, d, later) == (False, "live_tripped")
    _write_gate(tmp_path, _live_body())
    assert live_delivery_authorized(
        Ledger(), read_news_gate(tmp_path).delivery, LIVE_WINDOW_END
    ) == (False, "window_expired")


@pytest.mark.parametrize(
    "overrides",
    [
        {"window_end": None},
        {"continuous": 1, "window_end": None},
        {"continuous": "true", "window_end": None},
        {"continuous": True},
        {"continuous": True, "window_end": None, "delivery_mode": "TELEGRAM_CANARY"},
    ],
)
def test_continuous_requires_explicit_valid_live_setting(tmp_path, overrides):
    _write_gate(tmp_path, _live_body(**overrides))
    assert read_news_gate(tmp_path).delivery.mode == "OFF"


def test_observation_report_accepts_continuous_operation(environment):
    root, hermes = environment
    path = root / "news_activation.json"
    gate = json.loads(path.read_text())
    gate.update(continuous=True, window_end=None, delivery_mode="TELEGRAM_LIVE")
    put(path, gate)
    result = build_report(root, hermes, now=REPORT_NOW)
    assert not result["health"]["issues"]
    assert result["health"]["details"]["운영 설정"]["continuous"] is True


def test_resume_does_not_release_old_night_alert_but_new_urgent_evidence_still_can(
    tmp_path,
):
    from test_news_decision_engine import followup, observation
    from test_news_decision_s3 import DAY, ROLE, scene, tick

    from tokmo.programs.news_decision_outbox import queue_proposals

    store, ops, engine = scene(tmp_path)
    try:
        old = tick(engine, "a", 0)["alerts"][0]
        resume = DAY + timedelta(hours=13)
        next_open = morning(resume)
        engine.retire_unqueued_proposals(
            [old["id"]], at=resume, reason="Resume with fresh reassessment"
        )
        event = store.events()[0]
        event.update(recovery_review_at=kst(next_open), next_check_at=kst(next_open))
        store.save_event(event)
        check = engine.tick([], resume)
        assert check["calls"] == 0 and not check["alerts"]
        assert (
            queue_proposals(engine, ops, ROLE, cutoff=resume, activation_enabled=True)[
                "queued"
            ]
            == 0
        )
        engine.analyze = lambda _: followup(["a", "b"])
        fresh = engine.tick(
            [observation("b", resume + timedelta(minutes=1))],
            resume + timedelta(minutes=1),
        )
        assert not fresh["failures"] and fresh["alerts"]
        assert fresh["alerts"][0]["id"] != old["id"]
    finally:
        store.close()
        ops.close()
