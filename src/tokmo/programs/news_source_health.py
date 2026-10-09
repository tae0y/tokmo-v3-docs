"""Source-level NEWS health: state machine, reminder slots, gated delivery.

Pure orchestration over the records store; every instant is injected and all
persisted text comes from the closed vocabularies of the group receipts.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from tokmo.values.news_collection_diagnostics import (
    collection_complete,
    diagnostic_summary,
    safe_cause,
)
from tokmo.values.news_connectors import NewsFetchStatus
from tokmo.values.news_source_health import (
    ATTEMPTING_STALE_SECONDS,
    COVERAGE_IMPACT,
    DEFAULT_COVERAGE_IMPACT,
    DEGRADE_CONFIRM_TICKS,
    HEALTH_TZ_KEY,
    MAX_ATTEMPTS_PER_NOTIFICATION,
    MAX_DEGRADED_ROWS_PER_INCIDENT,
    RECOVER_CONFIRM_TICKS,
    REMINDER_MIN_GAP_SECONDS,
    REMINDER_SLOT_HOURS_KST,
    DeliveryOutcome,
    HealthDecision,
    SourceTickObservation,
)

_SUCCESS_STATUSES = frozenset({"COLLECTED", "EMPTY"})
COLLECTION_NOTICE_GRACE = timedelta(minutes=30)


def coverage_notice_deferred(row, last_complete, *, now):
    """One runner failure is not evidence of a source-wide collection gap."""
    if last_complete is not None:
        return timedelta(0) <= now - last_complete < COLLECTION_NOTICE_GRACE
    started = row.get("incident_started_at") or row.get("first_failure_at")
    return (
        started is not None
        and now - datetime.fromisoformat(started) < COLLECTION_NOTICE_GRACE
    )


def _coverage_notice(candidate, row, last_complete, *, now):
    source = candidate["source"]
    if candidate["kind"] == "RECOVERED":
        return f"[NEWS 수집 상태] {source} 복구 확인\n정상 수집을 다시 확인했습니다."
    if row is not None and coverage_notice_deferred(row, last_complete, now=now):
        return None
    zone = ZoneInfo(HEALTH_TZ_KEY)
    if last_complete is None:
        recent = "최근 1시간 안에 정상 완료 기록을 확인하지 못했습니다."
    else:
        age = int((now - last_complete).total_seconds() // 60)
        recent = (
            f"최근 정상 수집 회차: {last_complete.astimezone(zone):%m-%d %H:%M}"
            f" ({age}분 전)"
        )
    error = (row or {}).get("incident_last_error") or "수집 실행 실패"
    return (
        f"[NEWS 수집 상태] {source} 수집 완료 확인 지연\n"
        f"{recent}\n확인된 오류: {error}\n"
        "새 뉴스의 수집 여부를 확인하지 못하고 있습니다. "
        "실제 기사 누락이 확인된 것은 아닙니다."
    )[:1200]


def canonical_instant(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("aware instant required")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _pair_sort_key(pair):
    status, failure_class = pair
    return (status, failure_class is not None, failure_class or "")


def encode_failure_pairs(pairs) -> str:
    ordered = sorted(set(pairs), key=_pair_sort_key)
    encoded = json.dumps(
        [list(pair) for pair in ordered], ensure_ascii=True, separators=(",", ":")
    )
    if len(encoded.encode("ascii")) > 1024:
        raise ValueError("failure pairs exceed bounded encoding")
    return encoded


def decode_failure_pairs(encoded: str):
    return tuple(
        (status, failure_class) for status, failure_class in json.loads(encoded)
    )


def pairs_fingerprint(encoded: str) -> str:
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def mint_incident_id(source: str, first_failure_at: str, fingerprint: str) -> str:
    parts = tuple(
        value.encode("utf-8") for value in (source, first_failure_at, fingerprint)
    )
    blob = b"".join(len(part).to_bytes(4, "big") + part for part in parts)
    return hashlib.sha256(blob).hexdigest()


def observe_tick(receipt_payload, *, now):
    observed_at = canonical_instant(now)
    tick_id = receipt_payload["tick_id"]
    order, grouped = [], {}
    for entry in receipt_payload["sources"]:
        if entry["source"] not in grouped:
            order.append(entry["source"])
            grouped[entry["source"]] = []
        grouped[entry["source"]].append(entry)
    observations = []
    for source in order:
        entries = grouped[source]
        failing = [entry for entry in entries if not collection_complete(entry)]
        statuses = {status.value for status in NewsFetchStatus} | {
            "RUNNER_FAILURE",
            "STORAGE_FAILURE",
        }
        pairs = tuple(
            sorted(
                {
                    (
                        "RUNNER_FAILURE"
                        if entry["status"] in _SUCCESS_STATUSES
                        else entry["status"]
                        if entry["status"] in statuses
                        else "RUNNER_FAILURE",
                        "INCOMPLETE_STORAGE"
                        if entry["status"] in _SUCCESS_STATUSES
                        else safe_cause(entry["failure_class"])
                        if entry.get("failure_class") is not None
                        else None,
                    )
                    for entry in failing
                },
                key=_pair_sort_key,
            )
        )
        observations.append(
            SourceTickObservation(
                tick_id,
                source,
                observed_at,
                "FAILURE" if failing else "SUCCESS",
                pairs,
                len(entries),
                len(failing),
                diagnostic_summary(failing[0].get("diagnostics")) if failing else "",
            )
        )
    return tuple(observations)


def _fresh_row(source):
    return {
        "source": source,
        "state": "HEALTHY",
        "consecutive_failures": 0,
        "consecutive_successes": 0,
        "last_tick_id": None,
        "last_outcome": None,
        "pending_failure_pairs": None,
        "pending_fingerprint": None,
        "pending_fingerprint_ticks": 0,
        "first_failure_at": None,
        "incident_id": None,
        "incident_started_at": None,
        "incident_confirmed_at": None,
        "incident_fingerprint": None,
        "incident_last_error": None,
        "incident_failing_ticks": 0,
        "incident_alert_delivered_at": None,
        "last_processed_slot_utc": None,
        "updated_at": None,
    }


def _render_error(pairs, detail=""):
    line = ", ".join(
        status if failure_class is None else f"{status} {failure_class}"
        for status, failure_class in pairs
    )
    return (
        (line[:60] + " " + detail)[:120] if detail else line[:120] or "NO_HTTP_STATUS"
    )


def enumerate_slots(start, end, *, tz):
    """All slot instants in (start, end], newest-64-bounded, as UTC datetimes."""
    if end <= start:
        return (), False
    floor = end - timedelta(days=33)
    overflow = start < floor
    base = max(start, floor)
    slots = []
    day = base.astimezone(tz).date()
    last = end.astimezone(tz).date()
    while day <= last:
        for hour in REMINDER_SLOT_HOURS_KST:
            slot = datetime(day.year, day.month, day.day, hour, tzinfo=tz).astimezone(
                UTC
            )
            if base < slot <= end:
                slots.append(slot)
        day += timedelta(days=1)
    if len(slots) > 64:
        overflow = True
        slots = slots[-64:]
    return tuple(slots), overflow


def _next_slot(reference: str, tz) -> str:
    value = datetime.fromisoformat(reference).astimezone(tz)
    for day_offset in range(3):
        day = (value + timedelta(days=day_offset)).date()
        for hour in REMINDER_SLOT_HOURS_KST:
            slot = datetime(day.year, day.month, day.day, hour, tzinfo=tz)
            if slot >= value:
                return canonical_instant(slot)
    raise ValueError("slot horizon exhausted")


def _kst(instant: str, tz) -> str:
    value = datetime.fromisoformat(instant).astimezone(tz)
    return value.strftime("%Y-%m-%d %H:%M KST")


def _mint(incident_id, source, kind, dedupe_key, body_text, created_at):
    return {
        "notification_id": hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest(),
        "incident_id": incident_id,
        "source": source,
        "kind": kind,
        "dedupe_key": dedupe_key,
        "body_text": body_text,
        "created_at": created_at,
    }


def _clear_pending(row):
    row["pending_failure_pairs"] = None
    row["pending_fingerprint"] = None
    row["pending_fingerprint_ticks"] = 0


def _apply_healthy(row, obs, mints, tz):
    if obs.outcome == "SUCCESS":
        row["consecutive_failures"] = 0
        row["first_failure_at"] = None
        _clear_pending(row)
        return
    row["consecutive_successes"] = 0
    row["consecutive_failures"] += 1
    tick_encoded = encode_failure_pairs(obs.failure_pairs)
    if row["first_failure_at"] is None:
        row["first_failure_at"] = obs.observed_at
        row["pending_failure_pairs"] = tick_encoded
        row["pending_fingerprint_ticks"] = 1
    else:
        union = decode_failure_pairs(row["pending_failure_pairs"])
        row["pending_failure_pairs"] = encode_failure_pairs(union + obs.failure_pairs)
        row["pending_fingerprint_ticks"] += 1
    row["pending_fingerprint"] = pairs_fingerprint(row["pending_failure_pairs"])
    if row["consecutive_failures"] < DEGRADE_CONFIRM_TICKS:
        return
    union_pairs = decode_failure_pairs(row["pending_failure_pairs"])
    incident_id = mint_incident_id(
        row["source"], row["first_failure_at"], row["pending_fingerprint"]
    )
    row.update(
        state="DEGRADED",
        incident_id=incident_id,
        incident_started_at=row["first_failure_at"],
        incident_confirmed_at=obs.observed_at,
        incident_fingerprint=row["pending_fingerprint"],
        incident_last_error=_render_error(union_pairs, obs.diagnostic_summary),
        incident_failing_ticks=DEGRADE_CONFIRM_TICKS,
        first_failure_at=None,
    )
    _clear_pending(row)
    mints.append(
        _mint(
            incident_id,
            row["source"],
            "DEGRADED",
            f"{incident_id}:DEGRADED:1",
            build_notification_body(
                "DEGRADED",
                source=row["source"],
                started_at=row["incident_started_at"],
                now=obs.observed_at,
                last_error=row["incident_last_error"],
                targets_total=obs.targets_total,
                targets_failed=obs.targets_failed,
                failing_ticks=row["incident_failing_ticks"],
                tz=tz,
                next_check=_next_slot(obs.observed_at, tz),
            ),
            obs.observed_at,
        )
    )


def _apply_degraded(row, obs, mints, supersede, tz, existing):
    if obs.outcome == "FAILURE":
        row["consecutive_successes"] = 0
        row["consecutive_failures"] += 1
        row["incident_failing_ticks"] += 1
        row["incident_last_error"] = _render_error(
            obs.failure_pairs, obs.diagnostic_summary
        )
        tick_encoded = encode_failure_pairs(obs.failure_pairs)
        tick_fingerprint = pairs_fingerprint(tick_encoded)
        if tick_fingerprint == row["incident_fingerprint"]:
            _clear_pending(row)
        elif tick_fingerprint == row["pending_fingerprint"]:
            row["pending_fingerprint_ticks"] += 1
            if row["pending_fingerprint_ticks"] >= DEGRADE_CONFIRM_TICKS:
                row["incident_fingerprint"] = tick_fingerprint
                _clear_pending(row)
                mints.append(
                    _mint(
                        row["incident_id"],
                        row["source"],
                        "FINGERPRINT_CHANGED",
                        f"{row['incident_id']}:FP:{obs.tick_id}",
                        build_notification_body(
                            "FINGERPRINT_CHANGED",
                            source=row["source"],
                            started_at=row["incident_started_at"],
                            now=obs.observed_at,
                            last_error=row["incident_last_error"],
                            targets_total=obs.targets_total,
                            targets_failed=obs.targets_failed,
                            failing_ticks=row["incident_failing_ticks"],
                            tz=tz,
                            next_check=_next_slot(obs.observed_at, tz),
                        ),
                        obs.observed_at,
                    )
                )
        else:
            row["pending_failure_pairs"] = tick_encoded
            row["pending_fingerprint"] = tick_fingerprint
            row["pending_fingerprint_ticks"] = 1
        if row["incident_alert_delivered_at"] is None:
            _remint_degraded(row, obs, mints, tz, existing)
        return
    row["consecutive_failures"] = 0
    row["consecutive_successes"] += 1
    _clear_pending(row)
    if row["consecutive_successes"] < RECOVER_CONFIRM_TICKS:
        return
    mints.append(
        _mint(
            row["incident_id"],
            row["source"],
            "RECOVERED",
            f"{row['incident_id']}:RECOVERED",
            build_notification_body(
                "RECOVERED",
                source=row["source"],
                started_at=row["incident_started_at"],
                now=obs.observed_at,
                last_error=row["incident_last_error"],
                tz=tz,
            ),
            obs.observed_at,
        )
    )
    supersede.extend(
        record["notification_id"]
        for record in existing
        if record["kind"] in ("DEGRADED", "REMINDER", "FINGERPRINT_CHANGED")
        and (
            record["status"] in ("PENDING", "ATTEMPTING")
            or (
                record["status"] == "FAILED"
                and record["attempt_count"] < MAX_ATTEMPTS_PER_NOTIFICATION
            )
        )
    )
    row.update(
        state="HEALTHY",
        incident_id=None,
        incident_started_at=None,
        incident_confirmed_at=None,
        incident_fingerprint=None,
        incident_last_error=None,
        incident_failing_ticks=0,
        incident_alert_delivered_at=None,
        last_processed_slot_utc=None,
        consecutive_successes=0,
    )


def _remint_degraded(row, obs, mints, tz, existing):
    """An undelivered incident may mint at most 3 DEGRADED rows (D4)."""
    degraded_rows = [record for record in existing if record["kind"] == "DEGRADED"]
    still_open = any(
        record["status"] in ("PENDING", "ATTEMPTING")
        or (
            record["status"] == "FAILED"
            and record["attempt_count"] < MAX_ATTEMPTS_PER_NOTIFICATION
        )
        for record in degraded_rows
    )
    if (
        not degraded_rows
        or still_open
        or len(degraded_rows) >= MAX_DEGRADED_ROWS_PER_INCIDENT
    ):
        return
    sequence = len(degraded_rows) + 1
    mints.append(
        _mint(
            row["incident_id"],
            row["source"],
            "DEGRADED",
            f"{row['incident_id']}:DEGRADED:{sequence}",
            build_notification_body(
                "DEGRADED",
                source=row["source"],
                started_at=row["incident_started_at"],
                now=obs.observed_at,
                last_error=row["incident_last_error"],
                targets_total=obs.targets_total,
                targets_failed=obs.targets_failed,
                failing_ticks=row["incident_failing_ticks"],
                tz=tz,
                next_check=_next_slot(obs.observed_at, tz),
            ),
            obs.observed_at,
        )
    )


def _process_slots(row, obs, mints, supersede, tz, existing):
    start = datetime.fromisoformat(
        row["last_processed_slot_utc"] or row["incident_confirmed_at"]
    )
    end = datetime.fromisoformat(obs.observed_at)
    slots, overflow = enumerate_slots(start, end, tz=tz)
    if not slots:
        return
    row["last_processed_slot_utc"] = canonical_instant(slots[-1])
    anchor = datetime.fromisoformat(row["incident_alert_delivered_at"])
    eligible = [
        slot
        for slot in slots
        if (slot - anchor).total_seconds() >= REMINDER_MIN_GAP_SECONDS
    ]
    if not eligible:
        return
    slot_text = canonical_instant(eligible[-1])
    supersede.extend(
        record["notification_id"]
        for record in existing
        if record["kind"] == "REMINDER"
        and record["status"] in ("PENDING", "ATTEMPTING", "FAILED")
    )
    mints.append(
        _mint(
            row["incident_id"],
            row["source"],
            "REMINDER",
            f"{row['incident_id']}:REM:{slot_text}",
            build_notification_body(
                "REMINDER",
                source=row["source"],
                started_at=row["incident_started_at"],
                now=obs.observed_at,
                last_error=row["incident_last_error"],
                failing_ticks=row["incident_failing_ticks"],
                collapsed=len(eligible),
                overflow=overflow,
                tz=tz,
                next_check=_next_slot(obs.observed_at, tz),
            ),
            obs.observed_at,
        )
    )


def apply_source_health(store, observations, *, now, tz=None):
    zone = ZoneInfo(HEALTH_TZ_KEY) if tz is None else tz
    stamp = canonical_instant(now)
    decisions = []
    for obs in observations:
        row = store.read_health(obs.source, now=stamp)
        if row is not None and row["last_tick_id"] == obs.tick_id:
            decisions.append(HealthDecision(obs.source, obs.outcome, row["state"], ()))
            continue
        row = row or _fresh_row(obs.source)
        mints, supersede = [], []
        if row["state"] == "HEALTHY":
            _apply_healthy(row, obs, mints, zone)
        else:
            existing = store.read_notifications(row["incident_id"], now=stamp)
            _apply_degraded(row, obs, mints, supersede, zone, existing)
            if (
                row["state"] == "DEGRADED"
                and row["incident_alert_delivered_at"] is not None
            ):
                _process_slots(row, obs, mints, supersede, zone, existing)
        row["last_tick_id"] = obs.tick_id
        row["last_outcome"] = obs.outcome
        row["updated_at"] = stamp
        store.commit_tick(
            row, mints=tuple(mints), supersede_ids=tuple(supersede), now=stamp
        )
        decisions.append(
            HealthDecision(
                obs.source,
                obs.outcome,
                row["state"],
                tuple(record["kind"] for record in mints),
            )
        )
    return tuple(decisions)


def _daytime_start(now):
    local = now.astimezone(ZoneInfo(HEALTH_TZ_KEY))
    if local.hour not in range(8, 22):
        return None
    return canonical_instant(local.replace(hour=8, minute=0, second=0, microsecond=0))


def apply_collection_health(store, observations, *, now):
    """Discard overnight notices, then report only a fresh daytime failure.

    The incident/last successful delivery remains intact. A sustained incident
    gets at most one fresh daytime notice when no current notice already exists.
    """
    start, stamp = _daytime_start(now), canonical_instant(now)
    if start is not None:
        store.supersede_pending_before(start, now=stamp)
    decisions = apply_source_health(store, observations, now=now)
    if start is None:
        return decisions
    for obs in observations:
        if obs.outcome != "FAILURE":
            continue
        row = store.read_health(obs.source, now=stamp)
        if row["state"] != "DEGRADED" or row["incident_started_at"] >= start:
            continue
        existing = store.read_notifications(row["incident_id"], now=stamp)
        if any(n["kind"] != "RECOVERED" and n["created_at"] >= start for n in existing):
            continue
        kind = "DEGRADED" if row["incident_alert_delivered_at"] is None else "REMINDER"
        notice = _mint(
            row["incident_id"],
            obs.source,
            kind,
            f"{row['incident_id']}:DAY_CHECK:{start}",
            build_notification_body(
                kind,
                source=obs.source,
                started_at=row["incident_started_at"],
                now=stamp,
                last_error=row["incident_last_error"],
                failing_ticks=row["incident_failing_ticks"],
                tz=ZoneInfo(HEALTH_TZ_KEY),
            ),
            stamp,
        )
        store.commit_tick(row, mints=(notice,), supersede_ids=(), now=stamp)
    return decisions


def deliver_pending(store, *, telegram_send, now, complete_polls=None):
    """One bounded delivery pass: reconcile, then one claim per source."""
    stamp = canonical_instant(now)
    stale_before = canonical_instant(now - timedelta(seconds=ATTEMPTING_STALE_SECONDS))
    store.reconcile_delivery(stale_before=stale_before, now=stamp)
    # Nighttime messages are retained as history, not a morning delivery queue.
    start = _daytime_start(now)
    if start is None:
        return DeliveryOutcome(0, 0, 0, 0)
    store.supersede_pending_before(start, now=stamp)
    attempted = sent = failed = unknown = 0
    for source in store.pending_sources(now=stamp):
        prepare = None
        if complete_polls is not None:
            row = store.read_health(source, now=stamp)
            last_complete = complete_polls.get(source)
            prepare = lambda candidate: _coverage_notice(
                candidate, row, last_complete, now=now
            )
        claimed = store.claim_next(
            source,
            now=stamp,
            prepare=prepare,
            suppress_unseen_recovery=complete_polls is not None,
        )
        if claimed is None:
            continue
        attempted += 1
        try:
            message_id = telegram_send(claimed["body_text"])
        except TimeoutError:
            store.settle(
                claimed["notification_id"],
                "UNKNOWN",
                now=stamp,
                failure_reason="provider_timeout",
            )
            unknown += 1
        except Exception:
            store.settle(
                claimed["notification_id"],
                "FAILED",
                now=stamp,
                failure_reason="provider_failure",
            )
            failed += 1
        else:
            store.settle(
                claimed["notification_id"],
                "SENT",
                now=stamp,
                provider_message_id=str(message_id),
            )
            sent += 1
    return DeliveryOutcome(attempted, sent, failed, unknown)


def health_adjusted_exit(
    raw_exit, store, failing_sources, *, mode, now, complete_polls=None
):
    """A recorded source failure must not bypass quiet hours via a cron failure.
    During daytime keep the existing confirmed-and-notified suppression rule."""
    if mode != "DELIVER" or raw_exit != 3 or not failing_sources:
        return raw_exit
    stamp = canonical_instant(now)
    for source in failing_sources:
        row = store.read_health(source, now=stamp)
        if row is not None and now.astimezone(
            ZoneInfo(HEALTH_TZ_KEY)
        ).hour not in range(8, 22):
            continue
        if (
            row is not None
            and complete_polls is not None
            and coverage_notice_deferred(row, complete_polls.get(source), now=now)
        ):
            continue
        if (
            row is None
            or row["state"] != "DEGRADED"
            or row["incident_alert_delivered_at"] is None
        ):
            return raw_exit
    return 0


def build_notification_body(
    kind,
    *,
    source,
    started_at,
    now,
    last_error,
    tz,
    targets_total=0,
    targets_failed=0,
    failing_ticks=0,
    collapsed=0,
    overflow=False,
    next_check=None,
):
    minutes = int(
        (
            datetime.fromisoformat(now) - datetime.fromisoformat(started_at)
        ).total_seconds()
        // 60
    )
    impact = COVERAGE_IMPACT.get(source, DEFAULT_COVERAGE_IMPACT)
    error_line = last_error
    if targets_total > 1:
        error_line += f" (대상 {targets_total}개 중 {targets_failed}개 실패)"
    if kind == "RECOVERED":
        lines = [
            f"[NEWS 수집 상태] {source} 복구 확인",
            f"기간: {_kst(started_at, tz)} ~ {_kst(now, tz)} (지속 {minutes}분)",
            f"복구: {impact}",
        ]
        return "\n".join(lines)[:1200]
    titles = {
        "DEGRADED": "저하 확인",
        "FINGERPRINT_CHANGED": "오류 양상 변경",
        "REMINDER": "저하 지속",
    }
    lines = [
        f"[NEWS 수집 상태] {source} {titles[kind]}",
        f"시작: {_kst(started_at, tz)} (지속 {minutes}분, 실패 tick {failing_ticks}회)",
        f"오류: {error_line}",
        f"영향: {impact}",
    ]
    if kind == "REMINDER":
        lines.insert(2, f"경과: {minutes // 60}시간")
        if overflow:
            lines.append("누락 슬롯 다수")
        elif collapsed > 1:
            lines.append(f"누락 슬롯 {collapsed}개 통합")
    if next_check is not None:
        lines.append(f"다음 점검: {_kst(next_check, tz)}")
    return "\n".join(lines)[:1200]
