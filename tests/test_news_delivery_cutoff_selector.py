"""NEWS delivery-selector cutoff RED suite (production incident 2026-08-22).

Live incident: gate canary r2 carried cutoff 2026-08-22T02:06:19Z with
attempt cap 1, but the ops outbox already held two PENDING NEWS rows
created 2026-08-21T23:33:00.283067Z and 23:34:00.339226Z — before the
cutoff. The 02:07 pulse ran run_canary_alert_delivery, which claimed from
existing PENDING without enforcing the delivery cutoff, transported the
old low-value row, and consumed the single canary attempt. Contract: the
TELEGRAM_CANARY and TELEGRAM_LIVE selectors must never transport-select
an outbox row created before delivery.cutoff (semantic aware-UTC
comparison, fail-closed on malformed/naive timestamps), and the attempt
reservation/cap must stay untouched when no post-cutoff row exists.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tokmo.programs.news_event_admission import run_news_event_admission
from tokmo.records.news_episode_store import NewsEpisodeStore
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import ProducerRole

UTC = UTC
CUTOFF = datetime(2026, 8, 22, 2, 6, 19, tzinfo=UTC)  # gate r2 cutoff
WINDOW_END = CUTOFF + timedelta(minutes=60)
NOW = datetime(2026, 8, 22, 2, 7, 0, tzinfo=UTC)  # natural pulse tick
OLD_ONE = datetime(2026, 8, 21, 23, 33, 0, 283067, tzinfo=UTC)
OLD_TWO = datetime(2026, 8, 21, 23, 34, 0, 339226, tzinfo=UTC)
FRESH = CUTOFF + timedelta(seconds=30)
ACTIVATION_ID = "news-g3-canary-20260822-r2"
LIVE_ID = "news-g3-live-20260822"
PRED_ID = "news-g3-canary-20260821"


def _stores(tmp_path):
    news = NewsEpisodeStore(tmp_path / "stores" / "news_episodes.sqlite3")
    ops = OpsKernelStore(
        tmp_path / "ops" / "ops_kernel.sqlite3", stable_phase_ids=frozenset()
    )
    return news, ops


def _draft(episode_id, revision_no, *, observed_at, body="본문"):
    return {
        "episode_id": episode_id,
        "revision_no": revision_no,
        "revision_kind": "INITIAL" if revision_no == 1 else "MATERIAL_UPDATE",
        "body_text": body,
        "portfolio_context_digest": "0" * 64,
        "confidence": 50,
        "material_change_kind": "FIRST_ADMISSION"
        if revision_no == 1
        else "SCALE_CHANGE",
        "material_change_observed_at": observed_at,
        "links": (),
    }


def _admit(news, ops, term, *, now, body="본문"):
    """Admit one Telegram-PENDING outbox row whose created_at is ``now`` —
    the incident's old rows were admitted under the earlier activation, so
    admission-time eligibility passes (no admission cutoff)."""
    episode = news.upsert(term, (), now)
    run_news_event_admission(
        store=news,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        draft=_draft(
            episode.episode_id, 1, observed_at=now - timedelta(minutes=5), body=body
        ),
        activation_enabled=True,
        telegram_cutoff=None,
        now=now,
    )
    return episode.episode_id


def _sender_recorder(calls, provider_message_id="608"):
    def sender(text, reply_markup=None):
        calls.append((text, reply_markup))
        return provider_message_id

    return sender


def _canary_delivery():
    from tokmo.programs.pulse_production_deps import NewsDeliveryV1

    return NewsDeliveryV1("TELEGRAM_CANARY", ACTIVATION_ID, CUTOFF, WINDOW_END, 1)


def _live_delivery():
    from tokmo.programs.pulse_production_deps import NewsDeliveryV1

    return NewsDeliveryV1(
        "TELEGRAM_LIVE", LIVE_ID, CUTOFF, CUTOFF + timedelta(hours=24), None, PRED_ID
    )


def _run_canary(news, ops, sender, *, now=NOW):
    from tokmo.programs.news_alert_delivery import run_canary_alert_delivery

    return run_canary_alert_delivery(
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        ledger=news,
        delivery=_canary_delivery(),
        telegram_send=sender,
        keyboard_resolver=lambda alert_id: None,
        delivery_binder=news.bind_delivery_by_ops_alert,
        now=now,
    )


def _seed_predecessor(news):
    earlier = CUTOFF - timedelta(hours=2)
    news.reserve_canary_attempt(PRED_ID, "nal-pred", now=earlier)
    news.settle_canary_attempt(PRED_ID, "SENT", now=earlier)
    news.record_canary_slot_proof(
        PRED_ID,
        ("2026-08-22T00:30:00+00:00", "2026-08-22T01:00:00+00:00"),
        1,
        now=CUTOFF - timedelta(hours=1),
    )


def _run_live(news, ops, sender, *, now=NOW):
    from tokmo.programs.news_alert_delivery import run_live_alert_delivery

    return run_live_alert_delivery(
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        ledger=news,
        delivery=_live_delivery(),
        telegram_send=sender,
        keyboard_resolver=lambda alert_id: None,
        delivery_binder=news.bind_delivery_by_ops_alert,
        now=now,
        body_guard=lambda outbox: True,
    )


def _telegram_rows(ops):
    return [
        tuple(row)
        for row in ops._connect()
        .execute(
            "SELECT alert_id,status,attempted_at,created_at FROM alert_outbox"
            " WHERE channel='TELEGRAM' ORDER BY created_at,alert_id"
        )
        .fetchall()
    ]


def _rewrite_created_at(ops, alert_id, raw):
    connection = ops._connect()
    connection.execute(
        "UPDATE alert_outbox SET created_at=? WHERE alert_id=?", (raw, alert_id)
    )
    connection.commit()


# ------------------------------------------- incident reproduction: canary


def test_incident_old_pending_backlog_not_selected_by_canary(tmp_path):
    """Exact incident shape: two pre-cutoff PENDING rows, canary window
    open at 02:07 — the selector must claim nothing, start no transport,
    and leave the one-attempt cap unconsumed."""
    news, ops = _stores(tmp_path)
    _admit(news, ops, "엠알엔에이", now=OLD_ONE, body="mRNA 저가치 배경")
    _admit(news, ops, "채권금리", now=OLD_TWO, body="bond 배경")
    calls = []
    result = _run_canary(news, ops, _sender_recorder(calls))
    assert result["claimed"] == 0 and result["sent"] == 0
    assert result["reserved"] == 0
    assert calls == []
    assert news.canary_attempt(ACTIVATION_ID) is None
    assert [(row[1], row[2]) for row in _telegram_rows(ops)] == [
        ("PENDING", None),
        ("PENDING", None),
    ]


def test_canary_replay_zero_then_fresh_row_still_has_cap(tmp_path):
    """Replaying the tick over pre-cutoff backlog stays zero every time;
    the unburned cap then serves the first genuinely fresh row."""
    news, ops = _stores(tmp_path)
    _admit(news, ops, "엠알엔에이", now=OLD_ONE)
    calls = []
    for tick in range(3):
        result = _run_canary(
            news, ops, _sender_recorder(calls), now=NOW + timedelta(seconds=60 * tick)
        )
        assert result["claimed"] == 0 and calls == []
        assert news.canary_attempt(ACTIVATION_ID) is None
    fresh_episode = _admit(news, ops, "신규사건", now=FRESH, body="신규본문")
    result = _run_canary(news, ops, _sender_recorder(calls))
    assert result["sent"] == 1 and len(calls) == 1
    assert "신규본문" in calls[0][0]
    assert news.canary_attempt(ACTIVATION_ID)["outcome"] == "SENT"
    assert fresh_episode is not None


def test_canary_mixed_backlog_selects_only_post_cutoff_row(tmp_path):
    news, ops = _stores(tmp_path)
    _admit(news, ops, "엠알엔에이", now=OLD_ONE, body="옛본문")
    _admit(news, ops, "신규사건", now=FRESH, body="신규본문")
    calls = []
    result = _run_canary(news, ops, _sender_recorder(calls))
    assert result["sent"] == 1 and len(calls) == 1
    assert "신규본문" in calls[0][0] and "옛본문" not in calls[0][0]
    rows = _telegram_rows(ops)
    assert [(row[1], row[2]) for row in rows][0] == ("PENDING", None)
    assert rows[1][1] == "SENT"


# ------------------------------------------------ live-selector equivalent


def test_live_selector_excludes_pre_cutoff_backlog(tmp_path):
    news, ops = _stores(tmp_path)
    _seed_predecessor(news)
    _admit(news, ops, "엠알엔에이", now=OLD_ONE)
    calls = []
    result = _run_live(news, ops, _sender_recorder(calls))
    assert result["claimed"] == 0 and result["sent"] == 0
    assert result["tripped"] is None and calls == []
    assert news.live_trip(LIVE_ID) is None
    assert [(row[1], row[2]) for row in _telegram_rows(ops)] == [("PENDING", None)]


def test_live_mixed_backlog_selects_only_post_cutoff_row(tmp_path):
    news, ops = _stores(tmp_path)
    _seed_predecessor(news)
    _admit(news, ops, "엠알엔에이", now=OLD_ONE, body="옛본문")
    _admit(news, ops, "신규사건", now=FRESH, body="신규본문")
    calls = []
    result = _run_live(news, ops, _sender_recorder(calls))
    assert result["sent"] == 1 and len(calls) == 1
    assert "신규본문" in calls[0][0]
    rows = _telegram_rows(ops)
    assert rows[0][1] == "PENDING" and rows[0][2] is None
    assert rows[1][1] == "SENT"


# ------------------------------------- timestamp semantics and fail-closed


def test_malformed_or_naive_created_at_fails_closed(tmp_path):
    """A row whose created_at is unparseable or naive is never
    transport-selected and never mutated; the cap stays unconsumed."""
    news, ops = _stores(tmp_path)
    first = _telegram_rows(ops)
    assert first == []
    _admit(news, ops, "손상행일", now=FRESH)
    _admit(news, ops, "손상행이", now=FRESH + timedelta(seconds=1))
    rows = _telegram_rows(ops)
    _rewrite_created_at(ops, rows[0][0], "not-a-timestamp")
    _rewrite_created_at(ops, rows[1][0], "2026-08-22T03:00:00")  # naive
    calls = []
    result = _run_canary(news, ops, _sender_recorder(calls))
    assert result["claimed"] == 0 and calls == []
    assert news.canary_attempt(ACTIVATION_ID) is None
    after = {row[0]: (row[1], row[2], row[3]) for row in _telegram_rows(ops)}
    assert after[rows[0][0]] == ("PENDING", None, "not-a-timestamp")
    assert after[rows[1][0]] == ("PENDING", None, "2026-08-22T03:00:00")


def test_utc_offset_forms_compare_semantically_not_textually(tmp_path):
    """+09:00 instant 2026-08-22T11:06:00 equals 02:06:00Z — semantically
    pre-cutoff even though its text sorts after the cutoff's Z/+00 form —
    so it must be excluded; the boundary instant itself in +00:00 form
    (== cutoff exactly, text-sorting before the Z form) is eligible."""
    news, ops = _stores(tmp_path)
    _admit(news, ops, "구형시각", now=OLD_ONE)
    (old_row,) = _telegram_rows(ops)
    _rewrite_created_at(ops, old_row[0], "2026-08-22T11:06:00+09:00")
    calls = []
    result = _run_canary(news, ops, _sender_recorder(calls))
    assert result["claimed"] == 0 and calls == []
    assert news.canary_attempt(ACTIVATION_ID) is None
    _admit(news, ops, "경계시각", now=FRESH, body="경계본문")
    boundary_id = next(row[0] for row in _telegram_rows(ops) if row[0] != old_row[0])
    _rewrite_created_at(ops, boundary_id, "2026-08-22T02:06:19+00:00")
    result = _run_canary(news, ops, _sender_recorder(calls))
    assert result["sent"] == 1 and len(calls) == 1
    assert "경계본문" in calls[0][0]
    after = {row[0]: row[1] for row in _telegram_rows(ops)}
    assert after[old_row[0]] == "PENDING"
    assert after[boundary_id] == "SENT"


# ----------------------- M1 reclosure: bounded first-eligible selection


def test_cutoff_scan_skips_deep_ineligible_backlog_to_first_eligible(tmp_path):
    """M1 reclosure: a deep pre-cutoff/malformed backlog is scanned past —
    each row left byte-identical PENDING — and the first eligible row in
    (created_at, alert_id) order is the single row claimed; the later
    eligible row is untouched (selection stops at the first eligible)."""
    news, ops = _stores(tmp_path)
    for index in range(8):
        _admit(
            news,
            ops,
            f"구형{index}",
            now=OLD_ONE + timedelta(seconds=index),
            body=f"옛본문{index}",
        )
    backlog = _telegram_rows(ops)
    _rewrite_created_at(ops, backlog[0][0], "0000-not-a-timestamp")
    _rewrite_created_at(ops, backlog[1][0], "2026-08-21T23:00:00")  # naive
    ineligible = {row[0]: (row[1], row[2], row[3]) for row in _telegram_rows(ops)}
    _admit(news, ops, "첫적격", now=FRESH, body="첫적격본문")
    first_id = next(row[0] for row in _telegram_rows(ops) if row[0] not in ineligible)
    _admit(news, ops, "차적격", now=FRESH + timedelta(seconds=1), body="차적격본문")
    second_id = next(
        row[0]
        for row in _telegram_rows(ops)
        if row[0] not in ineligible and row[0] != first_id
    )
    calls = []
    result = _run_canary(news, ops, _sender_recorder(calls))
    assert result["sent"] == 1 and len(calls) == 1
    assert "첫적격본문" in calls[0][0]
    after = {row[0]: (row[1], row[2], row[3]) for row in _telegram_rows(ops)}
    assert after[first_id][0] == "SENT"
    assert after[second_id][0] == "PENDING" and after[second_id][1] is None
    for alert_id, before in ineligible.items():
        assert after[alert_id] == before


def test_no_cutoff_claim_keeps_historical_unfiltered_first_row(tmp_path):
    """M1 reclosure guard: without a delivery cutoff (pension/general
    lanes) the selector keeps its historical contract — the first PENDING
    row in (created_at, alert_id) order is claimed regardless of its age;
    cutoff eligibility applies only when a cutoff is supplied."""
    news, ops = _stores(tmp_path)
    _admit(news, ops, "구형행", now=OLD_ONE, body="옛본문")
    _admit(news, ops, "신규행", now=FRESH, body="신규본문")
    rows = _telegram_rows(ops)
    claimed = ops.claim_pending_telegram(ProducerRole.HERMES, NOW)
    assert claimed is not None and claimed.alert_id == rows[0][0]
    after = {row[0]: (row[1], row[2]) for row in _telegram_rows(ops)}
    assert after[rows[0][0]][0] == "ATTEMPTING"
    assert after[rows[1][0]] == ("PENDING", None)


# ------------------------------------ read-only operator surface exposure


def _write_gate(root, cutoff=CUTOFF, window_end=WINDOW_END):
    path = Path(root) / "news_activation.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "producer_mode": "SHADOW",
                "delivery_mode": "TELEGRAM_CANARY",
                "activation_id": ACTIVATION_ID,
                "cutoff": cutoff.isoformat(),
                "window_end": window_end.isoformat(),
                "telegram_attempt_cap": 1,
                "approved_by": "woong",
            }
        )
    )
    os.chmod(path, 0o600)


def test_readback_and_preflight_expose_post_cutoff_pending_count(tmp_path):
    from tokmo.programs.news_canary_readback import read_news_canary_state
    from tokmo.programs.news_g3_preflight import read_news_preflight

    _write_gate(tmp_path)
    news, ops = _stores(tmp_path)
    _admit(news, ops, "엠알엔에이", now=OLD_ONE)
    _admit(news, ops, "신규사건", now=FRESH)
    state = read_news_canary_state(tmp_path, now=NOW)
    assert state["telegram_outbox"] == {"PENDING": 2}
    assert state["telegram_pending_post_cutoff"] == 1
    receipt = read_news_preflight(tmp_path, now=NOW)
    assert receipt["telegram_outbox"] == {"PENDING": 2}
    assert receipt["telegram_pending_post_cutoff"] == 1
