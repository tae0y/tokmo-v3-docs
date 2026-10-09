"""Source-health state machine, reminder slots, delivery, and storm replay."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from tokmo.programs.news_source_health import (
    apply_source_health,
    canonical_instant,
    deliver_pending,
    enumerate_slots,
    health_adjusted_exit,
    mint_incident_id,
    observe_tick,
)
from tokmo.records.source_health_store import SourceHealthStore

T1 = datetime(2026, 8, 31, 8, 20, tzinfo=UTC)
T2 = T1 + timedelta(minutes=15)
T3 = T1 + timedelta(minutes=30)
T4 = T1 + timedelta(minutes=45)


def tick_at(n):
    return T1 + timedelta(minutes=15 * n)


def degrade(
    store, source="finnhub-market", failure_class="HTTP_503", status="TRANSPORT_FAILURE"
):
    """Two failing ticks 0/1 → confirmed DEGRADED incident."""
    for n in range(2):
        apply_tick(
            store, f"t{n}", [row(source, status, failure_class, 503)], tick_at(n)
        )
    return store.read_health(source, now=canonical_instant(tick_at(1)))


def row(source, status, failure_class=None, http_status=None, target=None):
    return {
        "source": source,
        "status": status,
        "inserted": 0,
        "duplicate": 0,
        "conflict": 0,
        "revision": 0,
        "http_status": http_status,
        "failure_class": failure_class,
        "target": target,
    }


def receipt(tick_id, rows):
    return {"tick_id": tick_id, "status": "PARTIAL_FAILURE", "sources": rows}


def apply_tick(store, tick_id, rows, when):
    observations = observe_tick(receipt(tick_id, rows), now=when)
    return apply_source_health(store, observations, now=when)


def pairs_json(pairs):
    return json.dumps(
        [list(pair) for pair in pairs], ensure_ascii=True, separators=(",", ":")
    )


def test_single_failing_tick_stays_healthy_with_stored_pair_bytes(tmp_path):
    """R-HLT-1 first half: one failure is provenance, not an incident."""
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    decisions = apply_tick(
        store, "t1", [row("finnhub-market", "TRANSPORT_FAILURE", "HTTP_503", 503)], T1
    )
    assert [(d.state, d.minted) for d in decisions] == [("HEALTHY", ())]
    stored = store.read_health("finnhub-market", now=canonical_instant(T1))
    assert stored["state"] == "HEALTHY"
    assert stored["consecutive_failures"] == 1
    assert stored["first_failure_at"] == canonical_instant(T1)
    expected_pairs = pairs_json([("TRANSPORT_FAILURE", "HTTP_503")])
    assert stored["pending_failure_pairs"] == expected_pairs
    assert (
        stored["pending_fingerprint"]
        == hashlib.sha256(expected_pairs.encode("ascii")).hexdigest()
    )
    assert stored["incident_id"] is None
    assert store.pending_sources(now=canonical_instant(T1)) == ()


def test_second_failing_tick_confirms_with_union_from_stored_bytes(tmp_path):
    """R-HLT-1 second half + v0.4 pair-union representability."""
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    apply_tick(
        store, "t1", [row("finnhub-market", "TRANSPORT_FAILURE", "HTTP_503", 503)], T1
    )
    # Union is recomputed from the stored ASCII pair bytes, not from the digest.
    stored = store.read_health("finnhub-market", now=canonical_instant(T1))
    union = sorted(
        json.loads(stored["pending_failure_pairs"]) + [["RATE_LIMITED", "HTTP_429"]]
    )
    expected_pairs = pairs_json([tuple(pair) for pair in union])
    decisions = apply_tick(
        store, "t2", [row("finnhub-market", "RATE_LIMITED", "HTTP_429", 429)], T2
    )
    assert [(d.state, d.minted) for d in decisions] == [("DEGRADED", ("DEGRADED",))]
    confirmed = store.read_health("finnhub-market", now=canonical_instant(T2))
    assert confirmed["incident_started_at"] == canonical_instant(T1)
    assert confirmed["incident_confirmed_at"] == canonical_instant(T2)
    assert (
        confirmed["incident_fingerprint"]
        == hashlib.sha256(expected_pairs.encode("ascii")).hexdigest()
    )
    assert confirmed["first_failure_at"] is None
    assert confirmed["pending_failure_pairs"] is None
    assert confirmed["pending_fingerprint_ticks"] == 0
    notifications = store.read_notifications(
        confirmed["incident_id"], now=canonical_instant(T2)
    )
    assert [(n["kind"], n["status"], n["dedupe_key"]) for n in notifications] == [
        ("DEGRADED", "PENDING", confirmed["incident_id"] + ":DEGRADED:1")
    ]


def test_interleaved_success_clears_streak_provenance(tmp_path):
    """R-HLT-1: a broken streak restarts from scratch, carrying nothing."""
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    apply_tick(
        store, "t1", [row("finnhub-market", "TRANSPORT_FAILURE", "HTTP_503", 503)], T1
    )
    apply_tick(store, "t2", [row("finnhub-market", "COLLECTED")], T2)
    cleared = store.read_health("finnhub-market", now=canonical_instant(T2))
    assert cleared["first_failure_at"] is None
    assert cleared["pending_failure_pairs"] is None
    assert cleared["pending_fingerprint"] is None
    assert cleared["pending_fingerprint_ticks"] == 0
    assert cleared["consecutive_failures"] == 0
    apply_tick(
        store, "t3", [row("finnhub-market", "RATE_LIMITED", "HTTP_429", 429)], T3
    )
    restarted = store.read_health("finnhub-market", now=canonical_instant(T3))
    assert restarted["state"] == "HEALTHY"
    assert restarted["first_failure_at"] == canonical_instant(T3)
    assert restarted["pending_failure_pairs"] == pairs_json(
        [("RATE_LIMITED", "HTTP_429")]
    )


def test_multi_target_source_folds_to_one_observation_without_per_symbol_rows(tmp_path):
    """R-HLT-2: 3-of-4 failing targets = one source-level observation."""
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    rows = [
        row("finnhub-company", "TRANSPORT_FAILURE", "HTTP_503", 503, "GOOGL"),
        row("finnhub-company", "TRANSPORT_FAILURE", "HTTP_503", 503, "NVDA"),
        row("finnhub-company", "COLLECTED", target="MSFT"),
        row("finnhub-company", "TRANSPORT_FAILURE", "HTTP_503", 503, "TSLA"),
    ]
    observations = observe_tick(receipt("t1", rows), now=T1)
    assert len(observations) == 1
    assert observations[0].targets_total == 4
    assert observations[0].targets_failed == 3
    assert observations[0].failure_pairs == (("TRANSPORT_FAILURE", "HTTP_503"),)
    apply_tick(store, "t1", rows, T1)
    apply_tick(store, "t2", rows, T2)
    confirmed = store.read_health("finnhub-company", now=canonical_instant(T2))
    assert confirmed["state"] == "DEGRADED"
    # Zero per-symbol notifications: exactly one source-level DEGRADED mint.
    notifications = store.read_notifications(
        confirmed["incident_id"], now=canonical_instant(T2)
    )
    assert [n["kind"] for n in notifications] == ["DEGRADED"]
    assert "GOOGL" not in notifications[0]["body_text"]
    assert "대상 4개 중 3개 실패" in notifications[0]["body_text"]
    # Historical receipts without the target key fold identically.
    legacy = [
        {key: value for key, value in entry.items() if key != "target"}
        for entry in rows
    ]
    assert observe_tick(receipt("t3", legacy), now=T3) == tuple(
        obs.__class__(
            **{**obs.__dict__, "tick_id": "t3", "observed_at": canonical_instant(T3)}
        )
        for obs in observations
    )


def test_fingerprint_change_needs_two_ticks_and_resets_success_streak(tmp_path):
    """R-HLT-3: changed fingerprint persists 2 failing ticks; DEGRADED+FAILURE
    always commits consecutive_successes=0 (v0.4)."""
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    incident = degrade(store)
    # Seed consecutive_successes=1 before the changed-fingerprint failure path.
    apply_tick(store, "t2", [row("finnhub-market", "COLLECTED")], tick_at(2))
    decisions = apply_tick(
        store,
        "t3",
        [row("finnhub-market", "CONFIG_UNAVAILABLE", "HTTP_401", 401)],
        tick_at(3),
    )
    changed_once = store.read_health(
        "finnhub-market", now=canonical_instant(tick_at(3))
    )
    assert decisions[0].minted == ()
    assert changed_once["consecutive_successes"] == 0
    assert changed_once["incident_fingerprint"] == incident["incident_fingerprint"]
    assert changed_once["pending_fingerprint_ticks"] == 1
    decisions = apply_tick(
        store,
        "t4",
        [row("finnhub-market", "CONFIG_UNAVAILABLE", "HTTP_401", 401)],
        tick_at(4),
    )
    assert decisions[0].minted == ("FINGERPRINT_CHANGED",)
    confirmed = store.read_health("finnhub-market", now=canonical_instant(tick_at(4)))
    assert confirmed["incident_fingerprint"] != incident["incident_fingerprint"]
    assert confirmed["pending_fingerprint"] is None
    notifications = store.read_notifications(
        incident["incident_id"], now=canonical_instant(tick_at(4))
    )
    keys = [
        n["dedupe_key"] for n in notifications if n["kind"] == "FINGERPRINT_CHANGED"
    ]
    assert keys == [incident["incident_id"] + ":FP:t4"]
    # Seed consecutive_successes=1 before a same-fingerprint failure path too.
    apply_tick(store, "t5", [row("finnhub-market", "COLLECTED")], tick_at(5))
    apply_tick(
        store,
        "t6",
        [row("finnhub-market", "CONFIG_UNAVAILABLE", "HTTP_401", 401)],
        tick_at(6),
    )
    same_path = store.read_health("finnhub-market", now=canonical_instant(tick_at(6)))
    assert same_path["consecutive_successes"] == 0
    assert same_path["state"] == "DEGRADED"


def test_oscillating_fingerprints_never_notify(tmp_path):
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    incident = degrade(store)
    flapping = [("CONFIG_UNAVAILABLE", "HTTP_401"), ("RATE_LIMITED", "HTTP_429")]
    for n, (status, failure_class) in enumerate(flapping * 2, start=2):
        apply_tick(
            store,
            f"t{n}",
            [row("finnhub-market", status, failure_class, 500)],
            tick_at(n),
        )
    kinds = [
        n["kind"]
        for n in store.read_notifications(
            incident["incident_id"], now=canonical_instant(tick_at(6))
        )
    ]
    assert kinds == ["DEGRADED"]


def test_recovery_needs_two_successes_and_clears_incident_atomically(tmp_path):
    """R-HLT-4 + R-DLV-6: RECOVERED mint, open rows SUPERSEDED, columns NULL."""
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    incident = degrade(store)
    # Open a PENDING FINGERPRINT_CHANGED row to be superseded by recovery.
    for n in (2, 3):
        apply_tick(
            store,
            f"t{n}",
            [row("finnhub-market", "CONFIG_UNAVAILABLE", "HTTP_401", 401)],
            tick_at(n),
        )
    apply_tick(store, "t4", [row("finnhub-market", "COLLECTED")], tick_at(4))
    still = store.read_health("finnhub-market", now=canonical_instant(tick_at(4)))
    assert still["state"] == "DEGRADED"
    assert still["consecutive_successes"] == 1
    decisions = apply_tick(
        store, "t5", [row("finnhub-market", "COLLECTED")], tick_at(5)
    )
    assert decisions[0].minted == ("RECOVERED",)
    recovered = store.read_health("finnhub-market", now=canonical_instant(tick_at(5)))
    assert recovered["state"] == "HEALTHY"
    for column in (
        "incident_id",
        "incident_started_at",
        "incident_confirmed_at",
        "incident_fingerprint",
        "incident_last_error",
        "incident_alert_delivered_at",
        "last_processed_slot_utc",
    ):
        assert recovered[column] is None, column
    assert recovered["incident_failing_ticks"] == 0
    rows = {
        n["kind"]: n
        for n in store.read_notifications(
            incident["incident_id"], now=canonical_instant(tick_at(5))
        )
    }
    superseded = rows["FINGERPRINT_CHANGED"]
    assert superseded["status"] == "SUPERSEDED"
    assert superseded["attempted_at"] is None
    assert superseded["settled_at"] == canonical_instant(tick_at(5))
    assert superseded["attempt_count"] == 0
    span = rows["RECOVERED"]["body_text"]
    assert "17:20 KST" in span and "18:35 KST" in span  # outage span, KST


def test_recovery_supersedes_failed_degraded_row_before_delivery(tmp_path):
    """F2: a non-exhausted FAILED DEGRADED row must not outlive recovery.

    Without the supersede, reconcile_delivery re-arms the FAILED row and
    claim_next's DEGRADED-first priority sends a stale 저하 확인 after the
    incident already closed.
    """
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    incident = degrade(store)

    def broken(body):
        raise RuntimeError("provider down")

    outcome = deliver_pending(store, telegram_send=broken, now=tick_at(2))
    assert (outcome.attempted, outcome.failed) == (1, 1)
    for n in (3, 4):
        apply_tick(store, f"t{n}", [row("finnhub-market", "COLLECTED")], tick_at(n))
    rows = {
        n["kind"]: n
        for n in store.read_notifications(
            incident["incident_id"], now=canonical_instant(tick_at(4))
        )
    }
    assert rows["RECOVERED"]["status"] == "PENDING"
    assert rows["DEGRADED"]["status"] == "SUPERSEDED"
    assert rows["DEGRADED"]["attempt_count"] == 1  # non-exhausted, still settled
    sent_bodies = []

    def working(body):
        sent_bodies.append(body)
        return "88"

    outcome = deliver_pending(store, telegram_send=working, now=tick_at(5))
    assert (outcome.attempted, outcome.sent) == (1, 1)
    assert [body.splitlines()[0] for body in sent_bodies] == [
        "[NEWS 수집 상태] finnhub-market 복구 확인"
    ]
    recovered = store.read_health("finnhub-market", now=canonical_instant(tick_at(5)))
    assert recovered["incident_alert_delivered_at"] is None


def deliver_incident_alert(store, when, source="finnhub-market"):
    """Settle the incident's DEGRADED row SENT so reminders have an anchor."""
    stamp = canonical_instant(when)
    claimed = store.claim_next(source, now=stamp)
    store.settle(
        claimed["notification_id"], "SENT", now=stamp, provider_message_id="99"
    )
    return store.read_health(source, now=stamp)


def fail_tick(store, tick_id, when, source="finnhub-market"):
    return apply_tick(
        store, tick_id, [row(source, "TRANSPORT_FAILURE", "HTTP_503", 503)], when
    )


def test_undelivered_incident_alert_means_zero_reminder_processing(tmp_path):
    """R-SLT-4: reminders exist only relative to a delivered incident alert."""
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    incident = degrade(store)
    crossing = datetime(2026, 8, 31, 11, 5, tzinfo=UTC)  # past 20:00 KST
    fail_tick(store, "t2", crossing)
    after = store.read_health("finnhub-market", now=canonical_instant(crossing))
    assert after["last_processed_slot_utc"] is None
    kinds = [
        n["kind"]
        for n in store.read_notifications(
            incident["incident_id"], now=canonical_instant(crossing)
        )
    ]
    assert "REMINDER" not in kinds


def test_slot_two_hours_after_delivery_is_eligible_and_earlier_skips_forever(tmp_path):
    """R-SLT-1: >=7200 s eligible; a 1h59m59s slot is watermarked, never minted."""
    slot_utc = datetime(2026, 8, 31, 11, 0, tzinfo=UTC)  # 20:00 KST
    # Skip arm: delivered 1h59m59s before the slot.
    store = SourceHealthStore(tmp_path / "skip.sqlite3")
    incident = degrade(store)
    deliver_incident_alert(store, slot_utc - timedelta(hours=1, minutes=59, seconds=59))
    fail_tick(store, "t2", slot_utc + timedelta(minutes=5))
    stamp = canonical_instant(slot_utc + timedelta(minutes=5))
    after = store.read_health("finnhub-market", now=stamp)
    assert after["last_processed_slot_utc"] == canonical_instant(slot_utc)
    assert "REMINDER" not in [
        n["kind"] for n in store.read_notifications(incident["incident_id"], now=stamp)
    ]
    # Even a later tick never revisits the skipped slot.
    fail_tick(store, "t3", slot_utc + timedelta(hours=1))
    assert "REMINDER" not in [
        n["kind"]
        for n in store.read_notifications(
            incident["incident_id"],
            now=canonical_instant(slot_utc + timedelta(hours=1)),
        )
    ]
    # Eligible arm: delivered exactly 2 h before the slot.
    store = SourceHealthStore(tmp_path / "eligible.sqlite3")
    incident = degrade(store)
    deliver_incident_alert(store, slot_utc - timedelta(hours=2))
    fail_tick(store, "t2", slot_utc + timedelta(minutes=5))
    reminders = [
        n
        for n in store.read_notifications(incident["incident_id"], now=stamp)
        if n["kind"] == "REMINDER"
    ]
    assert [n["dedupe_key"] for n in reminders] == [
        incident["incident_id"] + ":REM:" + canonical_instant(slot_utc)
    ]


def test_missed_slots_collapse_to_one_reminder_and_replay_adds_nothing(tmp_path):
    """R-SLT-2: three missed slots -> one collapsed reminder + watermark."""
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    incident = degrade(store)
    deliver_incident_alert(store, datetime(2026, 8, 31, 9, 0, tzinfo=UTC))
    wake = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    fail_tick(store, "t2", wake)  # slots 11:00, 23:00 (8/31), 11:00 (9/1) UTC
    stamp = canonical_instant(wake)
    newest = canonical_instant(datetime(2026, 9, 1, 11, 0, tzinfo=UTC))
    reminders = [
        n
        for n in store.read_notifications(incident["incident_id"], now=stamp)
        if n["kind"] == "REMINDER"
    ]
    assert [n["dedupe_key"] for n in reminders] == [
        incident["incident_id"] + ":REM:" + newest
    ]
    assert "누락 슬롯 3개 통합" in reminders[0]["body_text"]
    assert (
        store.read_health("finnhub-market", now=stamp)["last_processed_slot_utc"]
        == newest
    )
    # Replaying the same tick payload adds nothing.
    fail_tick(store, "t2", wake)
    assert (
        len(
            [
                n
                for n in store.read_notifications(incident["incident_id"], now=stamp)
                if n["kind"] == "REMINDER"
            ]
        )
        == 1
    )
    # The next reminder supersedes the previous open one.
    later = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    fail_tick(store, "t3", later)
    reminders = [
        n
        for n in store.read_notifications(
            incident["incident_id"], now=canonical_instant(later)
        )
        if n["kind"] == "REMINDER"
    ]
    statuses = {n["dedupe_key"]: n["status"] for n in reminders}
    assert statuses[incident["incident_id"] + ":REM:" + newest] == "SUPERSEDED"
    assert len(reminders) == 2


def test_slot_enumerator_crosses_date_boundaries_and_dst(tmp_path):
    """R-SLT-3: pure wall-clock localization, PEP-495 fold=0."""
    kst = ZoneInfo("Asia/Seoul")
    slots, overflow = enumerate_slots(
        datetime(2026, 8, 31, 14, 0, tzinfo=UTC),
        datetime(2026, 9, 1, 3, 0, tzinfo=UTC),
        tz=kst,
    )
    assert overflow is False
    assert slots == (datetime(2026, 8, 31, 23, 0, tzinfo=UTC),)
    new_york = ZoneInfo("America/New_York")
    slots, overflow = enumerate_slots(
        datetime(2026, 3, 7, 5, 0, tzinfo=UTC),
        datetime(2026, 3, 9, 5, 0, tzinfo=UTC),
        tz=new_york,
    )
    assert [slot.isoformat() for slot in slots] == [
        "2026-03-07T13:00:00+00:00",  # 08:00 EST
        "2026-03-08T01:00:00+00:00",  # 20:00 EST
        "2026-03-08T12:00:00+00:00",  # 08:00 EDT after spring-forward
        "2026-03-09T00:00:00+00:00",  # 20:00 EDT
    ]
    # Bounded: a month-long gap caps at 64 newest slots and flags overflow.
    slots, overflow = enumerate_slots(
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
        tz=kst,
    )
    assert overflow is True
    assert len(slots) <= 64


def test_claim_is_durable_before_transport_and_sent_binds_delivery(tmp_path):
    """R-DLV-1: ATTEMPTING committed before the sender runs; SENT anchors."""
    path = tmp_path / "health.sqlite3"
    store = SourceHealthStore(path)
    incident = degrade(store)
    seen = []

    def sender(body):
        observer = SourceHealthStore(path)
        rows = observer.read_notifications(
            incident["incident_id"], now=canonical_instant(T3)
        )
        seen.append((rows[0]["status"], rows[0]["attempted_at"]))
        return "77"

    outcome = deliver_pending(store, telegram_send=sender, now=T3)
    assert (outcome.attempted, outcome.sent) == (1, 1)
    assert seen == [("ATTEMPTING", canonical_instant(T3))]
    row_after = store.read_notifications(
        incident["incident_id"], now=canonical_instant(T3)
    )[0]
    assert (row_after["status"], row_after["provider_message_id"]) == ("SENT", "77")
    delivered = store.read_health("finnhub-market", now=canonical_instant(T3))
    assert delivered["incident_alert_delivered_at"] == canonical_instant(T3)


def test_timeout_is_terminal_unknown_and_remint_caps_at_three_rows(tmp_path):
    """R-DLV-2: UNKNOWN never resent; fresh DEGRADED rows #2/#3 only."""
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    incident = degrade(store)

    def timeout(body):
        raise TimeoutError

    for n in range(2, 6):
        outcome = deliver_pending(store, telegram_send=timeout, now=tick_at(n))
        fail_tick(store, f"t{n}", tick_at(n))
    keys = [
        (n["dedupe_key"].rsplit(":", 1)[-1], n["status"])
        for n in store.read_notifications(
            incident["incident_id"], now=canonical_instant(tick_at(6))
        )
    ]
    assert keys == [("1", "UNKNOWN"), ("2", "UNKNOWN"), ("3", "UNKNOWN")]
    undelivered = store.read_health("finnhub-market", now=canonical_instant(tick_at(6)))
    assert undelivered["incident_alert_delivered_at"] is None
    # Past the cap the raw exit stays unsuppressed: the wrapper keeps paging.
    assert (
        health_adjusted_exit(
            3, store, ("finnhub-market",), mode="DELIVER", now=tick_at(6)
        )
        == 3
    )


def test_stale_attempting_reconciles_inside_delivery(tmp_path):
    """R-DLV-3: >600 s ATTEMPTING settles UNKNOWN on the next delivery pass."""
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    incident = degrade(store)
    store.claim_next("finnhub-market", now=canonical_instant(T3))
    later = T3 + timedelta(seconds=601)
    outcome = deliver_pending(store, telegram_send=lambda body: "1", now=later)
    assert outcome.attempted == 0
    row_after = store.read_notifications(
        incident["incident_id"], now=canonical_instant(later)
    )[0]
    assert (row_after["status"], row_after["failure_reason"]) == (
        "UNKNOWN",
        "attempt_stale_crash",
    )


def test_bodies_carry_only_closed_vocabulary(tmp_path):
    """R-DLV-4: no token/api_key/URL/query text or symbol lists in any body."""
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    company = [
        row("finnhub-company", "TRANSPORT_FAILURE", "HTTP_503", 503, "GOOGL"),
        row("finnhub-company", "TRANSPORT_FAILURE", "HTTP_503", 503, "NVDA"),
    ]
    apply_tick(store, "t0", company, tick_at(0))
    apply_tick(store, "t1", company, tick_at(1))
    incident = store.read_health("finnhub-company", now=canonical_instant(tick_at(1)))
    deliver_incident_alert(store, tick_at(1), source="finnhub-company")
    changed = [
        row("finnhub-company", "CONFIG_UNAVAILABLE", "HTTP_401", 401, "GOOGL"),
        row("finnhub-company", "CONFIG_UNAVAILABLE", "HTTP_401", 401, "NVDA"),
    ]
    apply_tick(store, "t2", changed, tick_at(2))
    apply_tick(store, "t3", changed, tick_at(3))
    apply_tick(
        store, "t4", changed, datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    )  # reminder
    apply_tick(
        store, "t5", [row("finnhub-company", "COLLECTED", target="GOOGL")], tick_at(5)
    )
    apply_tick(
        store, "t6", [row("finnhub-company", "COLLECTED", target="GOOGL")], tick_at(6)
    )
    rows = store.read_notifications(
        incident["incident_id"], now=canonical_instant(tick_at(6))
    )
    assert {n["kind"] for n in rows} == {
        "DEGRADED",
        "FINGERPRINT_CHANGED",
        "REMINDER",
        "RECOVERED",
    }
    for record in rows:
        assert len(record["body_text"]) <= 1200
        for banned in ("token", "api_key", "https://", "query", "GOOGL", "NVDA"):
            assert banned not in record["body_text"], record["kind"]


def test_storm_replay_collapses_to_four_deliveries(tmp_path):
    """R-RPL-1: the 2026-08-31 storm shape -> 2 DEGRADED + 2 RECOVERED sends."""
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    sends, exits = [], []
    start = datetime(2026, 8, 31, 3, 5, tzinfo=UTC)  # 12:05 KST

    def run_tick(n, tick_id, rows):
        when = start + timedelta(minutes=15 * n)
        apply_tick(store, tick_id, rows, when)
        deliver_pending(
            store,
            telegram_send=lambda body: sends.append(body) or str(len(sends)),
            now=when,
        )
        failing = tuple(
            {
                entry["source"]
                for entry in rows
                if entry["status"] not in ("COLLECTED", "EMPTY")
            }
        )
        raw = 3 if failing else 0
        exits.append(
            health_adjusted_exit(raw, store, failing, mode="DELIVER", now=when)
        )

    company_fail = [
        row("finnhub-company", "TRANSPORT_FAILURE", "HTTP_503", 503, symbol)
        for symbol in ("GOOGL", "NVDA", "MSFT")
    ] + [row("jtbc-breaking", "COLLECTED")]
    market_fail = [
        row("finnhub-market", "TRANSPORT_FAILURE", "HTTP_503", 503),
        row("yna-industry", "COLLECTED"),
    ]
    for n in range(23):  # 23 failing ticks over ~3.25 h, B/C interleaved
        run_tick(n, f"storm-{n}", company_fail if n % 2 == 0 else market_fail)
    for n in range(23, 27):  # recovery: two clean ticks per source
        run_tick(
            n,
            f"recover-{n}",
            [
                row("finnhub-company", "COLLECTED", target="GOOGL"),
                row("finnhub-market", "COLLECTED"),
            ],
        )
    assert len(sends) == 4  # 2 DEGRADED + 2 RECOVERED, zero reminders
    assert exits.count(3) == 2  # one page per source before its delivery
    assert exits[:27].count(0) + exits.count(3) == 27
    # Legacy mapping would have paged on all 23 failing ticks.
    assert sum(1 for code in exits[:23] if code == 0) == 21


def test_incident_id_is_replay_safe_and_streak_distinct(tmp_path):
    """R-HLT-1B: canonical mint at confirmation; replay adds nothing."""
    store = SourceHealthStore(tmp_path / "health.sqlite3")
    failing = [row("finnhub-market", "TRANSPORT_FAILURE", "HTTP_503", 503)]
    apply_tick(store, "t1", failing, T1)
    apply_tick(store, "t2", failing, T2)
    confirmed = store.read_health("finnhub-market", now=canonical_instant(T2))
    assert confirmed["incident_id"] == mint_incident_id(
        "finnhub-market", canonical_instant(T1), confirmed["incident_fingerprint"]
    )
    # Replaying the confirming receipt yields the same id and no second mint.
    replayed = apply_tick(store, "t2", failing, T2)
    assert [d.minted for d in replayed] == [()]
    after = store.read_health("finnhub-market", now=canonical_instant(T2))
    assert after["incident_id"] == confirmed["incident_id"]
    assert (
        len(
            store.read_notifications(
                confirmed["incident_id"], now=canonical_instant(T2)
            )
        )
        == 1
    )
    # A distinct later streak cannot collide without a SHA-256 collision.
    assert (
        mint_incident_id(
            "finnhub-market", canonical_instant(T3), confirmed["incident_fingerprint"]
        )
        != confirmed["incident_id"]
    )
