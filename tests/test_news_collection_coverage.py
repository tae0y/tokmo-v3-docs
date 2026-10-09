"""Collection alarms require missing complete polls, not just runner errors."""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from test_news_group_cli import MODULE
from test_news_source_health import apply_tick, canonical_instant, row

from tokmo.programs.news_collection_coverage import recent_complete_polls
from tokmo.programs.news_poll_policy import NewsGroup, news_tick_id
from tokmo.programs.news_source_health import (
    apply_collection_health,
    coverage_notice_deferred,
    deliver_pending,
    health_adjusted_exit,
    observe_tick,
)
from tokmo.records.source_health_store import SourceHealthStore


def at(hm, day="2026-09-09"):
    return datetime.fromisoformat(f"{day}T{hm}:00+09:00")


def receipt(folder, hm, sources, *, group="C", fast=False):
    tick = news_tick_id(NewsGroup(group), at(hm), fast_primary=fast)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{tick}.json").write_text(
        json.dumps({"tick_id": tick, "status": "PARTIAL_FAILURE", "sources": sources})
    )


def incident(store, *, source="finnhub-market", first="10:10", second="10:25"):
    for hm in (first, second):
        apply_tick(store, hm, [row(source, "RUNNER_FAILURE")], at(hm))
    return store.read_health(source, now=canonical_instant(at(second)))


def test_full_and_fast_paths_are_combined_without_cross_source_masking(tmp_path):
    receipt(
        tmp_path,
        "10:00",
        [row("yna-market", "COLLECTED"), row("bbc", "RUNNER_FAILURE")],
        group="A",
    )
    receipt(
        tmp_path, "10:05", [row("yna-market", "RUNNER_FAILURE")], group="A", fast=True
    )
    receipt(
        tmp_path, "10:10", [row("yna-market", "RUNNER_FAILURE")], group="A", fast=True
    )
    coverage = recent_complete_polls(tmp_path, now=at("10:12"))
    assert coverage == {"yna-market": at("10:00")}


@pytest.mark.parametrize(
    "status,conflicts,accepted",
    [
        ("COLLECTED", 0, True),
        ("EMPTY", 0, True),
        ("RUNNER_FAILURE", 0, False),
        ("COLLECTED", 1, False),
    ],
)
def test_partial_storage_is_not_complete_but_unchanged_feeds_are(
    tmp_path, status, conflicts, accepted
):
    entry = row("finnhub-market", status)
    entry.update(inserted=0, duplicate=44, conflict=conflicts)
    receipt(tmp_path, "10:25", [entry])
    assert bool(recent_complete_polls(tmp_path, now=at("10:27"))) is accepted


def test_partial_company_targets_cannot_mask_missing_target(tmp_path):
    receipt(
        tmp_path,
        "10:20",
        [
            row("finnhub-company", "COLLECTED", target="NVDA"),
            row("finnhub-company", "RUNNER_FAILURE", target="AAPL"),
        ],
        group="B",
    )
    assert recent_complete_polls(tmp_path, now=at("10:27")) == {}


def test_diagnostics_keep_partial_unhealthy_and_variable_trace_out_of_fingerprint(
    tmp_path,
):
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    fingerprints = []
    for i, hm in enumerate(("10:10", "10:25", "10:40", "10:55")):
        entry = row("finnhub-market", "COLLECTED")
        entry["diagnostics"] = dict(
            stage="PERSIST",
            cause=None,
            received=9,
            persisted=3,
            elapsed_ms=240000 + i,
            trace=f"{i:016x}",
            http_status=200,
            stderr="token=SECRET",
        )
        receipt(tmp_path / "receipts", hm, [entry])
        obs = observe_tick(dict(tick_id=hm, sources=[entry]), now=at(hm))
        assert obs[0].outcome == "FAILURE"
        apply_collection_health(store, obs, now=at(hm))
        state = store.read_health("finnhub-market", now=canonical_instant(at(hm)))
        if i:
            fingerprints.append(state["incident_fingerprint"])
            assert "PERSIST" in state["incident_last_error"]
            assert f"trace={i:016x}" in state["incident_last_error"]
            assert "SECRET" not in state["incident_last_error"]
    assert len(set(fingerprints)) == 1
    notices = store.read_notifications(
        state["incident_id"], now=canonical_instant(at("10:55"))
    )
    assert not any(n["kind"] in {"FINGERPRINT_CHANGED", "RECOVERED"} for n in notices)
    assert recent_complete_polls(tmp_path / "receipts", now=at("10:57")) == {}


def test_future_old_and_invalid_receipts_do_not_prove_current_coverage(tmp_path):
    receipt(tmp_path, "10:40", [row("finnhub-market", "COLLECTED")])
    receipt(tmp_path, "08:55", [row("finnhub-market", "COLLECTED")])
    tick = news_tick_id(NewsGroup.C, at("10:25"))
    (tmp_path / f"{tick}.json").write_text("broken")
    assert recent_complete_polls(tmp_path, now=at("10:27")) == {}


def test_fresh_complete_collection_defers_notice_without_losing_failure(tmp_path):
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    state = incident(store, source="yna-market", first="10:05", second="10:10")
    emitted = []
    coverage = {"yna-market": at("10:00")}
    result = deliver_pending(
        store, telegram_send=emitted.append, now=at("10:12"), complete_polls=coverage
    )
    assert result.attempted == 0 and not emitted
    pending = store.read_notifications(
        state["incident_id"], now=canonical_instant(at("10:12"))
    )
    assert pending[0]["status"] == "PENDING" and pending[0]["attempt_count"] == 0
    assert (
        store.read_health("yna-market", now=canonical_instant(at("10:12")))["state"]
        == "DEGRADED"
    )
    assert (
        health_adjusted_exit(
            3,
            store,
            ("yna-market",),
            mode="DELIVER",
            now=at("10:12"),
            complete_polls=coverage,
        )
        == 0
    )


def test_new_source_gets_bounded_grace_then_alerts_without_success_record(tmp_path):
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    incident(store)
    emitted = []
    assert (
        deliver_pending(
            store, telegram_send=emitted.append, now=at("10:39"), complete_polls={}
        ).attempted
        == 0
    )
    assert (
        deliver_pending(
            store,
            telegram_send=lambda body: emitted.append(body) or "1",
            now=at("10:40"),
            complete_polls={},
        ).sent
        == 1
    )
    assert "정상 완료 기록을 확인하지 못했습니다" in emitted[0]


def test_finnhub_two_failed_polls_keep_uncertainty_visible_after_thirty_minutes(
    tmp_path,
):
    # The reported 04:55 / 05:10 / 05:25 sequence, shifted to daytime to
    # test the coverage decision independently of the night delivery gate.
    receipt(tmp_path / "receipts", "09:55", [row("finnhub-market", "COLLECTED")])
    partial = row("finnhub-market", "RUNNER_FAILURE")
    partial["duplicate"] = 44
    receipt(tmp_path / "receipts", "10:25", [partial])
    coverage = recent_complete_polls(tmp_path / "receipts", now=at("10:27"))
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    state = incident(store)
    emitted = []
    assert (
        deliver_pending(
            store,
            telegram_send=lambda body: emitted.append(body) or "1",
            now=at("10:27"),
            complete_polls=coverage,
        ).sent
        == 1
    )
    assert "09:55 (32분 전)" in emitted[0]
    assert "수집 완료 확인 지연" in emitted[0]
    assert "유실 가능" not in emitted[0]
    saved = store.read_notifications(
        state["incident_id"], now=canonical_instant(at("10:27"))
    )
    assert saved[0]["body_text"] == emitted[0] and saved[0]["status"] == "SENT"


@pytest.mark.parametrize("notified", [False, True])
def test_recovery_only_notifies_when_the_incident_was_previously_reported(
    tmp_path, notified
):
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    state = incident(store)
    emitted = []
    send = lambda body: emitted.append(body) or str(len(emitted))
    if notified:
        deliver_pending(store, telegram_send=send, now=at("10:40"), complete_polls={})
    for hm in ("10:40", "10:55"):
        apply_tick(store, hm, [row("finnhub-market", "COLLECTED")], at(hm))
    outcome = deliver_pending(
        store,
        telegram_send=send,
        now=at("10:57"),
        complete_polls={"finnhub-market": at("10:55")},
    )
    assert outcome.sent == int(notified)
    assert len(emitted) == (2 if notified else 0)
    records = store.read_notifications(
        state["incident_id"], now=canonical_instant(at("10:57"))
    )
    recovery = next(item for item in records if item["kind"] == "RECOVERED")
    assert recovery["status"] == ("SENT" if notified else "SUPERSEDED")
    assert recovery["attempt_count"] == int(notified)


def test_unresolved_overnight_failure_is_reassessed_at_eight(tmp_path):
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    incident(store, first="05:10", second="05:25")
    emitted = []
    assert (
        deliver_pending(
            store,
            telegram_send=emitted.append,
            now=at("05:27"),
            complete_polls={"finnhub-market": at("04:55")},
        ).attempted
        == 0
    )
    assert (
        deliver_pending(
            store, telegram_send=emitted.append, now=at("08:00"), complete_polls={}
        ).sent
        == 0
    )
    observations = observe_tick(
        {
            "tick_id": "fresh-08:10",
            "sources": [row("finnhub-market", "RUNNER_FAILURE")],
        },
        now=at("08:10"),
    )
    apply_collection_health(store, observations, now=at("08:12"))
    assert (
        deliver_pending(
            store,
            telegram_send=lambda body: emitted.append(body) or "1",
            now=at("08:12"),
            complete_polls={},
        ).sent
        == 1
    )
    assert (
        "05:25" not in emitted[0]
    )  # Not the stale body composed at incident confirmation.


def test_known_incident_recovered_overnight_does_not_send_morning_recovery(tmp_path):
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    state = incident(store, first="21:10", second="21:25")
    emitted = []
    deliver_pending(
        store,
        telegram_send=lambda text: emitted.append(text) or "1",
        now=at("21:40"),
        complete_polls={},
    )
    for hm in ("22:10", "22:25"):
        obs = observe_tick(
            {"tick_id": hm, "sources": [row("finnhub-market", "COLLECTED")]}, now=at(hm)
        )
        apply_collection_health(store, obs, now=at(hm))
    morning = at("08:00", day="2026-09-10")
    assert (
        deliver_pending(
            store, telegram_send=emitted.append, now=morning, complete_polls={}
        ).sent
        == 0
    )
    assert len(emitted) == 1
    records = store.read_notifications(
        state["incident_id"], now=canonical_instant(morning)
    )
    recovery = next(n for n in records if n["kind"] == "RECOVERED")
    assert recovery["status"] == "SUPERSEDED" and recovery["attempt_count"] == 0


def test_daytime_refresh_after_previous_notice_mints_only_once(tmp_path):
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    state = incident(store, first="05:10", second="05:25")
    # The previous nighttime delivery was reported: fresh daytime warning is
    # still allowed without replaying its old recovery/degradation text.
    claim = store.claim_next("finnhub-market", now=canonical_instant(at("05:27")))
    store.settle(
        claim["notification_id"],
        "SENT",
        now=canonical_instant(at("05:27")),
        provider_message_id="1",
    )
    for hm in ("08:10", "08:25", "08:40"):
        obs = observe_tick(
            {"tick_id": hm, "sources": [row("finnhub-market", "RUNNER_FAILURE")]},
            now=at(hm),
        )
        apply_collection_health(store, obs, now=at(hm))
    records = store.read_notifications(
        state["incident_id"], now=canonical_instant(at("08:40"))
    )
    fresh = [n for n in records if n["created_at"] >= canonical_instant(at("08:00"))]
    assert len(fresh) == 1 and fresh[0]["status"] == "PENDING"


def test_fresh_day_check_not_blocked_by_three_terminal_old_notices(tmp_path):
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    state = incident(store, first="05:10", second="05:25")
    for hm in ("05:25", "05:40", "05:55"):
        if hm != "05:25":
            apply_tick(store, hm, [row("finnhub-market", "RUNNER_FAILURE")], at(hm))
        claimed = store.claim_next("finnhub-market", now=canonical_instant(at(hm)))
        assert claimed is not None
        store.settle(
            claimed["notification_id"],
            "UNKNOWN",
            now=canonical_instant(at(hm)),
            failure_reason="provider_timeout",
        )
    for hm in ("08:10", "08:25"):
        obs = observe_tick(
            {"tick_id": hm, "sources": [row("finnhub-market", "RUNNER_FAILURE")]},
            now=at(hm),
        )
        apply_collection_health(store, obs, now=at(hm))
    records = store.read_notifications(
        state["incident_id"], now=canonical_instant(at("08:25"))
    )
    assert sum(n["status"] == "UNKNOWN" for n in records) == 3
    assert sum(n["status"] == "PENDING" for n in records) == 1


def test_recent_first_recovery_already_suppresses_old_degradation_notice(tmp_path):
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    incident(store)
    apply_tick(store, "10:40", [row("finnhub-market", "COLLECTED")], at("10:40"))
    assert (
        deliver_pending(
            store,
            telegram_send=lambda body: pytest.fail(body),
            now=at("10:43"),
            complete_polls={"finnhub-market": at("10:40")},
        ).attempted
        == 0
    )


def test_grace_boundary_and_future_evidence():
    state = {"incident_started_at": at("09:00").isoformat()}
    assert coverage_notice_deferred(state, at("10:00"), now=at("10:29"))
    assert not coverage_notice_deferred(state, at("10:00"), now=at("10:30"))
    assert not coverage_notice_deferred(state, at("10:40"), now=at("10:30"))


def test_scheduled_cli_applies_shared_coverage_to_delivery_and_exit(
    tmp_path, monkeypatch
):
    runtime = tmp_path / "runtime"
    receipt(runtime / "receipts", "10:00", [row("yna-market", "COLLECTED")], group="A")
    receipt(
        runtime / "receipts",
        "10:10",
        [row("yna-market", "RUNNER_FAILURE")],
        group="A",
        fast=True,
    )
    gate = runtime / "source_health_activation.json"
    gate.write_text('"DELIVER"')
    gate.chmod(0o600)
    db = tmp_path / "health.sqlite3"
    incident(SourceHealthStore(db), source="yna-market", first="09:35", second="09:40")
    args = SimpleNamespace(
        runtime=runtime,
        health_db=db,
        scheduled_for=at("10:10"),
        health_telegram_env=tmp_path / "fake.env",
    )
    monkeypatch.setattr(
        MODULE, "_load_health_sender", lambda path: lambda body: pytest.fail(body)
    )
    result = SimpleNamespace(
        status="FAILED",
        tick_id=news_tick_id(NewsGroup.A, at("10:10"), fast_primary=True),
    )
    assert MODULE._run_health_step(args, result, 3, now=at("10:12")) == 0
