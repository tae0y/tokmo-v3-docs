"""NEWS G3 Telegram exactly-one canary RED suite (approval packet 2026-08-21).

Covers §2.1 independent fail-closed delivery gate parsing, §2.2 durable
non-rearmable one-attempt ledger with reservation-before-transport, §2.3
cutoff/backlog exclusion, §2.5 canonical outbox reuse, §3 automatic
fail-closed OFF, split producer/delivery rollback, dialogue-gate
independence, SHADOW compatibility, and a production-shaped fake-transport
run through the real composition/Pulse seams with network zero.
"""

import dataclasses
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tokmo.programs.news_event_admission import run_news_event_admission
from tokmo.records.news_episode_store import (
    NewsEpisodeStore,
    NewsEpisodeStoreError,
)
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import ProducerRole
from tokmo.values.session_gate import Market

UTC = UTC
NOW = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)  # 18:00 KST — outside night hold
CANARY_CUTOFF = NOW - timedelta(minutes=10)
WINDOW_END = CANARY_CUTOFF + timedelta(minutes=60)
ACTIVATION_ID = "news-g3-canary-20260821"
# Weekend/holiday extension (user direction 2026-08-22): SAME canary
# identity r3, window_end moved to Monday 12:30 KST — never a new attempt.
EXTENDED_ACTIVATION_ID = "news-taxonomy-canary-20260822T022825Z-r3"
EXTENDED_CUTOFF = datetime(2026, 8, 22, 2, 28, 25, tzinfo=UTC)
EXTENDED_WINDOW_END = datetime(2026, 8, 24, 3, 30, tzinfo=UTC)  # Mon 12:30 KST


def _canary_body(**extra):
    body = {
        "enabled": True,
        "producer_mode": "SHADOW",
        "delivery_mode": "TELEGRAM_CANARY",
        "activation_id": ACTIVATION_ID,
        "cutoff": CANARY_CUTOFF.isoformat(),
        "window_end": WINDOW_END.isoformat(),
        "telegram_attempt_cap": 1,
        "approved_by": "woong",
    }
    body.update(extra)
    return {key: value for key, value in body.items() if value is not ...}


def _write_gate(root, payload, mode=0o600):
    path = Path(root) / "news_activation.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    os.chmod(path, mode)
    return path


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


def _delivery(cutoff=CANARY_CUTOFF, window_end=WINDOW_END, activation_id=ACTIVATION_ID):
    from tokmo.programs.pulse_production_deps import NewsDeliveryV1

    return NewsDeliveryV1("TELEGRAM_CANARY", activation_id, cutoff, window_end, 1)


def _run_canary(news, ops, sender, *, now=NOW, delivery=None):
    from tokmo.programs.news_alert_delivery import run_canary_alert_delivery

    return run_canary_alert_delivery(
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        ledger=news,
        delivery=delivery if delivery is not None else _delivery(),
        telegram_send=sender,
        keyboard_resolver=lambda alert_id: None,
        delivery_binder=news.bind_delivery_by_ops_alert,
        now=now,
    )


def _admit_pending_telegram(
    news, ops, *, term="캐너리사건", revision_no=1, observed_at=None, now=NOW
):
    observed = observed_at or (NOW - timedelta(minutes=5))
    episode = news.upsert(term, (), now)
    return episode.episode_id, run_news_event_admission(
        store=news,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        draft=_draft(episode.episode_id, revision_no, observed_at=observed),
        activation_enabled=True,
        telegram_cutoff=CANARY_CUTOFF,
        now=now,
    )


def _sender_recorder(calls, provider_message_id="777"):
    def sender(text, reply_markup=None):
        calls.append((text, reply_markup))
        return provider_message_id

    return sender


def _build(root, *, telegram_sender=None, dialogue_activation_enabled=False):
    from tokmo.programs.pulse_production_deps import build_pulse_production_deps

    return build_pulse_production_deps(
        root=root,
        clock=lambda: NOW,
        watchlist_symbols={Market.KR: frozenset(), Market.US: frozenset()},
        dialogue_cutoff=NOW,
        telegram_sender=telegram_sender,
        dialogue_activation_enabled=dialogue_activation_enabled,
    )


def _telegram_rows(ops):
    return [
        tuple(row)
        for row in ops._connect()
        .execute(
            "SELECT alert_id,origin,status,provider_message_id FROM alert_outbox "
            "WHERE channel='TELEGRAM' ORDER BY created_at,alert_id"
        )
        .fetchall()
    ]


# ------------------------------------------------------- §2.1 gate parsing


def test_gate_valid_canary_payload_parses_with_shadow_producer(tmp_path):
    from tokmo.programs.pulse_production_deps import read_news_gate

    _write_gate(tmp_path, _canary_body())
    snapshot = read_news_gate(tmp_path)
    activation, producer_mode = snapshot
    assert activation.enabled is True and producer_mode == "SHADOW"
    delivery = snapshot.delivery
    assert delivery.mode == "TELEGRAM_CANARY"
    assert delivery.activation_id == ACTIVATION_ID
    assert delivery.cutoff == CANARY_CUTOFF
    assert delivery.window_end == WINDOW_END
    assert delivery.attempt_cap == 1


def test_canary_window_max_constant_is_72_hours():
    from tokmo.programs.pulse_production_deps import (
        NEWS_CANARY_WINDOW_MAX_SECONDS,
    )

    assert NEWS_CANARY_WINDOW_MAX_SECONDS == 72 * 3600


@pytest.mark.parametrize(
    "span",
    (
        timedelta(hours=1),  # historical one-hour gate
        EXTENDED_WINDOW_END - EXTENDED_CUTOFF,  # ~49h weekend extension
        timedelta(hours=72),  # exact upper boundary
    ),
)
def test_gate_bounded_canary_window_spans_are_valid(tmp_path, span):
    from tokmo.programs.pulse_production_deps import read_news_gate

    window_end = CANARY_CUTOFF + span
    _write_gate(tmp_path, _canary_body(window_end=window_end.isoformat()))
    delivery = read_news_gate(tmp_path).delivery
    assert delivery.mode == "TELEGRAM_CANARY", span
    assert delivery.window_end == window_end
    assert delivery.attempt_cap == 1


def test_gate_extended_r3_identity_parses_to_monday_1230_kst(tmp_path):
    from tokmo.programs.pulse_production_deps import read_news_gate

    _write_gate(
        tmp_path,
        _canary_body(
            activation_id=EXTENDED_ACTIVATION_ID,
            cutoff="2026-08-22T02:28:25Z",
            window_end="2026-08-24T03:30:00Z",
        ),
    )
    delivery = read_news_gate(tmp_path).delivery
    assert delivery.mode == "TELEGRAM_CANARY"
    assert delivery.activation_id == EXTENDED_ACTIVATION_ID
    assert delivery.cutoff == EXTENDED_CUTOFF
    assert delivery.window_end == EXTENDED_WINDOW_END
    assert delivery.attempt_cap == 1
    # active over the weekend daytime, closed at the Monday boundary
    assert delivery.canary_active(datetime(2026, 8, 23, 12, 0, tzinfo=UTC))
    assert not delivery.canary_active(EXTENDED_WINDOW_END)


def test_gate_shadow_payload_without_delivery_fields_is_off(tmp_path):
    from tokmo.programs.pulse_production_deps import read_news_gate

    _write_gate(
        tmp_path,
        {
            "enabled": True,
            "cutoff": CANARY_CUTOFF.isoformat(),
            "approved_by": "woong",
            "producer_mode": "SHADOW",
        },
    )
    snapshot = read_news_gate(tmp_path)
    activation, producer_mode = snapshot
    assert activation.enabled is True and producer_mode == "SHADOW"
    assert snapshot.delivery.mode == "OFF"
    assert snapshot.delivery.activation_id is None


@pytest.mark.parametrize(
    "extra",
    (
        {"delivery_mode": "TELEGRAM"},
        {"delivery_mode": "telegram_canary"},
        {"delivery_mode": "LIVE"},
        {"delivery_mode": 1},
        {"delivery_mode": None},
        {"activation_id": ...},
        {"activation_id": ""},
        {"activation_id": 7},
        {"activation_id": "x" * 65},
        {"activation_id": "has space"},
        {"activation_id": ".leading-dot"},
        {"telegram_attempt_cap": ...},
        {"telegram_attempt_cap": 0},
        {"telegram_attempt_cap": 2},
        {"telegram_attempt_cap": True},
        {"telegram_attempt_cap": "1"},
        {"telegram_attempt_cap": 1.0},
        {"window_end": ...},
        {"window_end": "2026-08-21T09:40:00"},
        {"window_end": CANARY_CUTOFF.isoformat()},
        {"window_end": (CANARY_CUTOFF + timedelta(hours=72, seconds=1)).isoformat()},
        {"window_end": (CANARY_CUTOFF - timedelta(minutes=60)).isoformat()},
        {"window_end": 12},
        {"producer_mode": "LIVE"},
        {"producer_mode": ...},
        {"enabled": False},
        {"cutoff": ...},
        {"cutoff": "2026-08-21T08:50:00"},
        {"approved_by": ""},
    ),
)
def test_gate_wrong_mode_cap_window_fails_closed(tmp_path, extra):
    from tokmo.programs.pulse_production_deps import read_news_gate

    _write_gate(tmp_path, _canary_body(**extra))
    snapshot = read_news_gate(tmp_path)
    assert snapshot.delivery.mode == "OFF", extra
    assert snapshot.delivery.activation_id is None


@pytest.mark.parametrize(
    "payload",
    (
        "",
        "{not json",
        '"scalar"',
        "[1,2]",
    ),
)
def test_gate_malformed_bytes_fail_closed(tmp_path, payload):
    from tokmo.programs.pulse_production_deps import read_news_gate

    _write_gate(tmp_path, payload)
    snapshot = read_news_gate(tmp_path)
    activation, producer_mode = snapshot
    assert activation.enabled is False and producer_mode == "OFF"
    assert snapshot.delivery.mode == "OFF"


def test_gate_world_readable_canary_fails_closed(tmp_path):
    from tokmo.programs.pulse_production_deps import read_news_gate

    _write_gate(tmp_path, _canary_body(), mode=0o644)
    snapshot = read_news_gate(tmp_path)
    assert snapshot.delivery.mode == "OFF"
    assert snapshot[0].enabled is False


def test_gate_bad_delivery_fields_keep_shadow_producer_alive(tmp_path):
    """Split reversibility: a broken delivery block never kills SHADOW."""
    from tokmo.programs.news_production import NewsProducerDepsV1
    from tokmo.programs.pulse_production_deps import read_news_gate

    _write_gate(tmp_path, _canary_body(telegram_attempt_cap=2))
    snapshot = read_news_gate(tmp_path)
    assert snapshot[1] == "SHADOW"
    assert snapshot.delivery.mode == "OFF"
    deps = _build(tmp_path, telegram_sender=_sender_recorder([]))
    assert isinstance(deps.news_producer_deps, NewsProducerDepsV1)


def test_dialogue_gate_never_activates_news_delivery(tmp_path):
    from tokmo.programs.pulse_production_deps import read_news_gate
    from tokmo.records.dialogue_activation import read_dialogue_activation

    dialogue = tmp_path / "dialogue_activation.json"
    dialogue.write_text(
        json.dumps(
            {
                "enabled": True,
                "cutoff": CANARY_CUTOFF.isoformat(),
                "approved_by": "woong",
            }
        )
    )
    os.chmod(dialogue, 0o600)
    assert read_dialogue_activation(dialogue).enabled is True
    # no news gate at all -> whole NEWS lane absent
    deps = _build(
        tmp_path, telegram_sender=_sender_recorder([]), dialogue_activation_enabled=True
    )
    assert deps.news_delivery_runner is None
    # news gate SHADOW without delivery fields -> delivery still OFF
    _write_gate(
        tmp_path,
        {
            "enabled": True,
            "cutoff": CANARY_CUTOFF.isoformat(),
            "approved_by": "woong",
            "producer_mode": "SHADOW",
        },
    )
    snapshot = read_news_gate(tmp_path)
    assert snapshot.delivery.mode == "OFF"
    calls = []
    deps = _build(
        tmp_path,
        telegram_sender=_sender_recorder(calls),
        dialogue_activation_enabled=True,
    )
    news = deps.news_producer_deps.episode_store
    ops = deps.news_producer_deps.ops_kernel
    episode = news.upsert("대화게이트독립", (), NOW)
    news.admit_alert_revision(
        _draft(episode.episode_id, 1, observed_at=NOW - timedelta(minutes=5)), now=NOW
    )
    deps.news_outbox_reconcile(NOW)
    deps.news_delivery_runner(NOW)
    assert calls == []
    assert _telegram_rows(ops) == []


# ------------------------------------------------- §2.2 durable attempt ledger


def test_ledger_reserve_is_exactly_once_and_survives_reopen(tmp_path):
    news, _ = _stores(tmp_path)
    assert news.canary_attempt(ACTIVATION_ID) is None
    assert news.reserve_canary_attempt(ACTIVATION_ID, "nal-x", now=NOW) is True
    assert news.reserve_canary_attempt(ACTIVATION_ID, "nal-y", now=NOW) is False
    row = news.canary_attempt(ACTIVATION_ID)
    assert row["outcome"] == "RESERVED" and row["ops_alert_id"] == "nal-x"
    news.close()
    reopened = NewsEpisodeStore(tmp_path / "stores" / "news_episodes.sqlite3")
    assert reopened.canary_attempt(ACTIVATION_ID)["outcome"] == "RESERVED"
    assert reopened.reserve_canary_attempt(ACTIVATION_ID, "nal-z", now=NOW) is False


def test_ledger_settle_transitions_and_rejects_rearm(tmp_path):
    news, _ = _stores(tmp_path)
    news.reserve_canary_attempt(ACTIVATION_ID, "nal-x", now=NOW)
    news.settle_canary_attempt(ACTIVATION_ID, "SENT", now=NOW)
    row = news.canary_attempt(ACTIVATION_ID)
    assert row["outcome"] == "SENT" and row["settled_at"] is not None
    with pytest.raises(NewsEpisodeStoreError):
        news.settle_canary_attempt(ACTIVATION_ID, "FAILED", now=NOW)
    with pytest.raises(NewsEpisodeStoreError):
        news.settle_canary_attempt("absent-id", "SENT", now=NOW)
    assert not hasattr(news, "delete_canary_attempt")


def test_ledger_migration_adds_table_to_existing_store(tmp_path):
    from tokmo.records.news_episode_store import _actual_ddl_digest

    path = tmp_path / "stores" / "news_episodes.sqlite3"
    news = NewsEpisodeStore(path)
    news.upsert("보존사건", (), NOW)
    news.close()
    # Reconstruct the pre-G3 shape: drop the ledger and re-seal the digest.
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE canary_attempts")
    connection.execute(
        "UPDATE schema_meta SET ddl_sha256=?", (_actual_ddl_digest(connection),)
    )
    connection.commit()
    connection.close()
    upgraded = NewsEpisodeStore(path)
    assert upgraded.canary_attempt(ACTIVATION_ID) is None
    assert upgraded.reserve_canary_attempt(ACTIVATION_ID, "nal-x", now=NOW) is True
    assert (
        upgraded.episode_state(
            "nep-"
            + __import__("hashlib")
            .sha256(
                __import__("hashlib").sha256("보존사건".encode()).hexdigest().encode()
            )
            .hexdigest()[:24]
        )
        is not None
    )
    upgraded.close()
    reopened = NewsEpisodeStore(path)
    assert reopened.canary_attempt(ACTIVATION_ID)["outcome"] == "RESERVED"


# ----------------------------------------- §2.2/§2.5 canary delivery runner


def test_reservation_is_durable_before_transport_start(tmp_path):
    news, ops = _stores(tmp_path)
    _admit_pending_telegram(news, ops)
    seen = []

    def sender(text, reply_markup=None):
        row = news.canary_attempt(ACTIVATION_ID)
        seen.append(None if row is None else row["outcome"])
        return "42"

    result = _run_canary(news, ops, sender)
    assert seen == ["RESERVED"]
    assert result["claimed"] == 1 and result["sent"] == 1
    assert news.canary_attempt(ACTIVATION_ID)["outcome"] == "SENT"


def test_sent_consumes_cap_and_second_send_is_zero(tmp_path):
    news, ops = _stores(tmp_path)
    _admit_pending_telegram(news, ops, term="사건일")
    _admit_pending_telegram(news, ops, term="사건이")
    calls = []
    first = _run_canary(news, ops, _sender_recorder(calls))
    assert first["sent"] == 1 and len(calls) == 1
    second = _run_canary(news, ops, _sender_recorder(calls))
    assert second == second | {"claimed": 0, "sent": 0, "failed": 0, "unknown": 0}
    assert second["off"] == "cap_consumed"
    assert len(calls) == 1
    rows = _telegram_rows(ops)
    assert [row[2] for row in rows] == ["SENT", "PENDING"]


def test_extended_window_same_identity_keeps_consumed_cap(tmp_path):
    """Extending window_end for the SAME activation_id never re-arms the
    durable one-attempt ledger: a cap consumed under the original 1h
    window stays OFF across the whole extended (~49h) window."""
    news, ops = _stores(tmp_path)
    _admit_pending_telegram(news, ops, term="주말연장일")
    calls = []
    first = _run_canary(news, ops, _sender_recorder(calls))
    assert first["sent"] == 1 and len(calls) == 1
    _admit_pending_telegram(news, ops, term="주말연장이")
    extended = _delivery(window_end=CANARY_CUTOFF + timedelta(hours=49))
    for later in (NOW + timedelta(hours=5), NOW + timedelta(hours=40)):
        result = _run_canary(
            news, ops, _sender_recorder(calls), now=later, delivery=extended
        )
        assert result["off"] == "cap_consumed"
        assert result["claimed"] == 0 and result["sent"] == 0
    assert len(calls) == 1
    rows = _telegram_rows(ops)
    assert [row[2] for row in rows] == ["SENT", "PENDING"]


def test_failed_transport_consumes_cap(tmp_path):
    news, ops = _stores(tmp_path)
    _admit_pending_telegram(news, ops)

    def sender(text, reply_markup=None):
        raise RuntimeError("provider down")

    result = _run_canary(news, ops, sender)
    assert result["failed"] == 1 and result["sent"] == 0
    assert news.canary_attempt(ACTIVATION_ID)["outcome"] == "FAILED"
    calls = []
    again = _run_canary(news, ops, _sender_recorder(calls))
    assert again["off"] == "cap_consumed" and calls == []


def test_unknown_transport_consumes_cap_and_is_never_requeued(tmp_path):
    news, ops = _stores(tmp_path)
    episode_id, _ = _admit_pending_telegram(news, ops)

    def sender(text, reply_markup=None):
        raise TimeoutError("no settle")

    result = _run_canary(news, ops, sender)
    assert result["unknown"] == 1
    assert news.canary_attempt(ACTIVATION_ID)["outcome"] == "UNKNOWN"
    (row,) = _telegram_rows(ops)
    assert row[2] == "UNKNOWN"
    calls = []
    again = _run_canary(news, ops, _sender_recorder(calls))
    assert again["off"] == "cap_consumed"
    assert calls == [] and _telegram_rows(ops)[0][2] == "UNKNOWN"


def test_window_expiry_means_zero_claims(tmp_path):
    news, ops = _stores(tmp_path)
    _admit_pending_telegram(news, ops)
    calls = []
    result = _run_canary(news, ops, _sender_recorder(calls), now=WINDOW_END)
    assert result["off"] == "window_expired"
    assert result["claimed"] == 0 and calls == []
    assert news.canary_attempt(ACTIVATION_ID) is None
    before = _run_canary(
        news, ops, _sender_recorder(calls), now=CANARY_CUTOFF - timedelta(seconds=1)
    )
    assert before["off"] == "before_cutoff" and calls == []


def test_lost_reservation_race_marks_failed_without_transport(tmp_path):
    news, ops = _stores(tmp_path)
    _admit_pending_telegram(news, ops)
    real_reserve = news.reserve_canary_attempt

    def stealing_reserve(activation_id, ops_alert_id, *, now):
        real_reserve(activation_id, "nal-other", now=now)
        return real_reserve(activation_id, ops_alert_id, now=now)

    news.reserve_canary_attempt = stealing_reserve
    calls = []
    result = _run_canary(news, ops, _sender_recorder(calls))
    assert calls == []
    assert result["claimed"] == 1 and result["failed"] == 1
    (row,) = _telegram_rows(ops)
    assert row[2] == "FAILED"


# ------------------------------------------------ §2.3 cutoff and backlog


def test_pre_cutoff_backlog_never_becomes_telegram(tmp_path):
    from tokmo.programs.news_event_admission import reconcile_news_outbox

    news, ops = _stores(tmp_path)
    episode = news.upsert("백로그사건", (), NOW)
    news.admit_alert_revision(
        _draft(episode.episode_id, 1, observed_at=CANARY_CUTOFF - timedelta(hours=2)),
        now=NOW,
    )
    changed = reconcile_news_outbox(
        store=news,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        activation_enabled=True,
        telegram_cutoff=CANARY_CUTOFF,
        now=NOW,
    )
    assert changed == 1
    assert _telegram_rows(ops) == []
    shadow = (
        ops._connect()
        .execute("SELECT count(*) FROM alert_outbox WHERE channel='SHADOW'")
        .fetchone()
    )
    assert shadow[0] == 1
    calls = []
    result = _run_canary(news, ops, _sender_recorder(calls))
    assert result["claimed"] == 0 and calls == []
    assert news.canary_attempt(ACTIVATION_ID) is None


def test_morning_coalesced_backlog_stays_shadow_fresh_material_delivers(tmp_path):
    """A coalesced overnight hold inherits the held revision's material
    observed time: pre-cutoff backlog can never re-date itself into
    Telegram at release, while genuinely fresh material stays eligible."""
    from tokmo.programs.news_event_admission import reconcile_news_outbox

    news, ops = _stores(tmp_path)
    backlog = news.upsert("야간백로그", (), NOW)
    news.hold_morning_revision(
        backlog.episode_id,
        1,
        "사건: 백로그본문",
        now=CANARY_CUTOFF - timedelta(hours=5),
        release_not_before=NOW - timedelta(minutes=1),
    )
    fresh = news.upsert("야간신규", (), NOW)
    news.hold_morning_revision(
        fresh.episode_id,
        1,
        "사건: 신규본문",
        now=CANARY_CUTOFF + timedelta(minutes=5),
        release_not_before=NOW - timedelta(minutes=1),
    )
    assert (
        len(news.release_morning_holds(now_kst=NOW, telegram_cutoff=CANARY_CUTOFF)) == 2
    )
    reconcile_news_outbox(
        store=news,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        activation_enabled=True,
        telegram_cutoff=CANARY_CUTOFF,
        now=NOW,
    )
    rows = _telegram_rows(ops)
    assert len(rows) == 1 and rows[0][1] == "NEWS_EVENT"
    fresh_alert = ops.read_outbox(rows[0][0])
    assert "신규본문" in fresh_alert.body_evidence[0]


def test_mixed_cutoff_digest_partition_keeps_backlog_out_of_telegram(tmp_path):
    """M4: a combined morning digest never mixes pre-cutoff backlog into a
    Telegram-eligible body — the release partitions on the cutoff, backlog
    coalesces SHADOW-only, and at most ONE Telegram-visible digest exists."""
    from tokmo.programs.news_event_admission import reconcile_news_outbox

    news, ops = _stores(tmp_path)
    release = NOW - timedelta(minutes=1)
    for key, observed in (
        ("백로그일", CANARY_CUTOFF - timedelta(hours=5)),
        ("백로그이", CANARY_CUTOFF - timedelta(hours=4)),
        ("신규일", CANARY_CUTOFF + timedelta(minutes=5)),
        ("신규이", CANARY_CUTOFF + timedelta(minutes=6)),
    ):
        episode = news.upsert(key, (), NOW)
        news.hold_morning_revision(
            episode.episode_id,
            1,
            f"📰 {key} 뉴스 동향\n사건: {key} 수주 확대 공시\n근거: 기사 3건 · 출처 2곳",
            now=observed,
            release_not_before=release,
        )
    released = news.release_morning_holds(now_kst=NOW, telegram_cutoff=CANARY_CUTOFF)
    assert len(released) == 2  # one backlog digest, one fresh digest
    reconcile_news_outbox(
        store=news,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        activation_enabled=True,
        telegram_cutoff=CANARY_CUTOFF,
        now=NOW,
    )
    rows = _telegram_rows(ops)
    assert len(rows) == 1
    telegram_body = ops.read_outbox(rows[0][0]).body_evidence[0]
    assert "신규일" in telegram_body and "신규이" in telegram_body
    assert "백로그일" not in telegram_body and "백로그이" not in telegram_body


def test_post_cutoff_event_becomes_pending_telegram(tmp_path):
    from tokmo.programs.news_event_admission import reconcile_news_outbox

    news, ops = _stores(tmp_path)
    episode = news.upsert("컷오프이후", (), NOW)
    news.admit_alert_revision(
        _draft(episode.episode_id, 1, observed_at=CANARY_CUTOFF + timedelta(minutes=1)),
        now=NOW,
    )
    reconcile_news_outbox(
        store=news,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        activation_enabled=True,
        telegram_cutoff=CANARY_CUTOFF,
        now=NOW,
    )
    (row,) = _telegram_rows(ops)
    assert row[2] == "PENDING"


# ------------------------------------- wiring: build_pulse_production_deps


def _wired(tmp_path, calls, body=None):
    _write_gate(tmp_path, body if body is not None else _canary_body())
    deps = _build(tmp_path, telegram_sender=_sender_recorder(calls))
    news = deps.news_producer_deps.episode_store
    ops = deps.news_producer_deps.ops_kernel
    return deps, news, ops


def test_wired_canary_delivers_exactly_once_then_off(tmp_path):
    calls = []
    deps, news, ops = _wired(tmp_path, calls)
    episode = news.upsert("배선사건", (), NOW)
    news.admit_alert_revision(
        _draft(episode.episode_id, 1, observed_at=NOW - timedelta(minutes=5)), now=NOW
    )
    deps.news_outbox_reconcile(NOW)
    result = deps.news_delivery_runner(NOW)
    assert result["sent"] == 1 and len(calls) == 1
    assert news.canary_attempt(ACTIVATION_ID)["outcome"] == "SENT"
    # split rollback: delivery OFF, SHADOW producer path still writes rows
    episode2 = news.upsert("후속사건", (), NOW)
    news.admit_alert_revision(
        _draft(episode2.episode_id, 1, observed_at=NOW - timedelta(minutes=4)), now=NOW
    )
    assert deps.news_outbox_reconcile(NOW + timedelta(seconds=60)) == 1
    after = deps.news_delivery_runner(NOW + timedelta(seconds=60))
    assert after["off"] == "cap_consumed" and len(calls) == 1
    statuses = [row[2] for row in _telegram_rows(ops)]
    assert statuses == ["SENT"]


def test_wired_restart_and_gate_rewrite_cannot_rearm(tmp_path):
    calls = []
    deps, news, ops = _wired(tmp_path, calls)
    episode = news.upsert("재기동사건", (), NOW)
    news.admit_alert_revision(
        _draft(episode.episode_id, 1, observed_at=NOW - timedelta(minutes=5)), now=NOW
    )
    deps.news_outbox_reconcile(NOW)
    assert deps.news_delivery_runner(NOW)["sent"] == 1
    # simulate restart plus operator rewriting the same-id gate file
    _write_gate(tmp_path, _canary_body())
    rebuilt = _build(tmp_path, telegram_sender=_sender_recorder(calls))
    news2 = rebuilt.news_producer_deps.episode_store
    episode2 = news2.upsert("재기동후속", (), NOW)
    news2.admit_alert_revision(
        _draft(episode2.episode_id, 1, observed_at=NOW - timedelta(minutes=3)), now=NOW
    )
    rebuilt.news_outbox_reconcile(NOW + timedelta(seconds=120))
    result = rebuilt.news_delivery_runner(NOW + timedelta(seconds=120))
    assert result["off"] == "cap_consumed"
    assert len(calls) == 1
    assert news2.canary_attempt(ACTIVATION_ID)["outcome"] == "SENT"


def test_wired_without_sender_stays_shadow_and_burns_nothing(tmp_path):
    _write_gate(tmp_path, _canary_body())
    deps = _build(tmp_path, telegram_sender=None)
    news = deps.news_producer_deps.episode_store
    ops = deps.news_producer_deps.ops_kernel
    episode = news.upsert("전송자부재", (), NOW)
    news.admit_alert_revision(
        _draft(episode.episode_id, 1, observed_at=NOW - timedelta(minutes=5)), now=NOW
    )
    deps.news_outbox_reconcile(NOW)
    deps.news_delivery_runner(NOW)
    assert _telegram_rows(ops) == []
    assert news.canary_attempt(ACTIVATION_ID) is None


def test_wired_expired_window_is_shadow_only(tmp_path):
    calls = []
    cutoff = NOW - timedelta(hours=2)
    body = _canary_body(
        cutoff=cutoff.isoformat(),
        window_end=(cutoff + timedelta(minutes=60)).isoformat(),
    )
    deps, news, ops = _wired(tmp_path, calls, body=body)
    episode = news.upsert("만료사건", (), NOW)
    news.admit_alert_revision(
        _draft(episode.episode_id, 1, observed_at=NOW - timedelta(minutes=5)), now=NOW
    )
    deps.news_outbox_reconcile(NOW)
    result = deps.news_delivery_runner(NOW)
    assert calls == [] and _telegram_rows(ops) == []
    assert news.canary_attempt(ACTIVATION_ID) is None
    assert result["off"] == "window_expired"


def test_producer_admission_holds_ordinary_night_event_under_activation(tmp_path):
    """The producer stage must hold, not immediately outbox, an ORDINARY
    night event even while canary delivery activation is on."""
    from tokmo.programs.news_production import NewsProducerDepsV1, run_news_production
    from tokmo.values.news_classifier import DEFAULT_NEWS_CLASSIFIER_MODEL_ID

    night = datetime(2026, 8, 21, 15, 0, tzinfo=UTC)  # 24:00 KST
    news, ops = _stores(tmp_path)
    deps = NewsProducerDepsV1(
        episode_store=news,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        documents_reader=lambda window_from: None,
        holding_terms=lambda: frozenset(),
        classifier_call=_ok_classifier(),
        classifier_config_path=tmp_path / "news_classifier_model.json",
        admitted_catalog={DEFAULT_NEWS_CLASSIFIER_MODEL_ID: "openai-codex"},
        gate_cutoff=night - timedelta(minutes=30),
        slot_seconds=1800,
        candidates_per_tick=2,
        lease_seconds=90,
        backoff_cap_seconds=3600,
        ttl_seconds=86400,
        attempt_max=6,
        delivery_activation=lambda now: True,
    )
    _seed_candidate(
        news,
        "야간사건종목",
        now=night - timedelta(seconds=60),
        observed_at=night - timedelta(minutes=5),
    )
    _mark_slot_done(news, night)
    run_news_production(producer_deps=deps, now=night)
    held = news.connection.execute(
        "SELECT delivery_state FROM alert_revisions"
    ).fetchall()
    assert held == [("HELD_MORNING",)]
    assert _telegram_rows(ops) == []


# ----------------------------------------------- readback/operator surface


def test_readback_is_side_effect_free_and_proves_off(tmp_path):
    from tokmo.programs.news_canary_readback import read_news_canary_state

    empty = tmp_path / "empty"
    empty.mkdir()
    state = read_news_canary_state(empty, now=NOW)
    assert state["delivery_mode"] == "OFF"
    assert state["delivery_enabled_now"] is False
    assert state["attempt"] is None
    assert list(empty.iterdir()) == []  # no store creation, ever


def test_readback_reports_attempt_and_off_after_send(tmp_path):
    from tokmo.programs.news_canary_readback import read_news_canary_state

    calls = []
    deps, news, ops = _wired(tmp_path, calls)
    episode = news.upsert("판독사건", (), NOW)
    news.admit_alert_revision(
        _draft(episode.episode_id, 1, observed_at=NOW - timedelta(minutes=5)), now=NOW
    )
    deps.news_outbox_reconcile(NOW)
    deps.news_delivery_runner(NOW)
    state = read_news_canary_state(tmp_path, now=NOW + timedelta(seconds=60))
    assert state["delivery_mode"] == "TELEGRAM_CANARY"
    assert state["activation_id"] == ACTIVATION_ID
    assert state["delivery_enabled_now"] is False
    assert "cap_consumed" in state["off_reasons"]
    assert state["attempt"]["outcome"] == "SENT"
    assert state["telegram_outbox"] == {"SENT": 1}
    assert state["telegram_transport_starts"] == 1
    body_bytes = json.dumps(state, ensure_ascii=False)
    assert "판독사건" not in body_bytes  # no alert bodies in the surface


def test_readback_cli_prints_state_and_requires_aware_now(tmp_path, capsys):
    from tokmo.programs.news_canary_readback import main

    _write_gate(tmp_path, _canary_body())
    assert main(["--root", str(tmp_path), "--now", NOW.isoformat()]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["delivery_mode"] == "TELEGRAM_CANARY"
    assert printed["delivery_enabled_now"] is True
    assert printed["attempt"] is None
    with pytest.raises(SystemExit):
        main(["--root", str(tmp_path), "--now", "2026-08-21T09:00:00"])
    with pytest.raises(SystemExit):
        main(["--root", str(tmp_path)])


def test_live_composition_carries_canary_judgment_without_transport(tmp_path):
    """The one-read gate snapshot reaches the builder with its delivery
    judgment intact through the real composition path; without Telegram
    env the sender is None and delivery fail-closes to OFF."""
    from tokmo import composition

    project = tmp_path / "project"
    (project / "watchlists").mkdir(parents=True)
    for market in ("KR", "US"):
        (project / "watchlists" / f"{market}.json").write_text(
            json.dumps({"schema_version": 1, "market": market, "entries": []})
        )
    validator = project / "scripts" / "playbooks" / "validate_playbooks.py"
    validator.parent.mkdir(parents=True)
    validator.write_text("def validate_tree(root): return []\n")
    (project / ".env.toss").write_text("TOSS_CLIENT_ID=x\nTOSS_CLIENT_SECRET=y\n")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    _write_gate(runtime, _canary_body())
    deps = composition.build_pulse_production_live_deps(
        project_root=project,
        clock=lambda: NOW,
        runtime_root=runtime,
        dialogue_cutoff=NOW,
        environ={},
    )
    assert deps.news_producer_deps is not None
    assert callable(deps.news_producer_deps.delivery_activation)
    news = deps.news_producer_deps.episode_store
    ops = deps.news_producer_deps.ops_kernel
    episode = news.upsert("조립경로사건", (), NOW)
    news.admit_alert_revision(
        _draft(episode.episode_id, 1, observed_at=NOW - timedelta(minutes=5)), now=NOW
    )
    deps.news_outbox_reconcile(NOW)
    deps.news_delivery_runner(NOW)
    assert _telegram_rows(ops) == []  # no sender -> fail-closed OFF
    assert news.canary_attempt(ACTIVATION_ID) is None


# -------------------------- production-shaped Pulse run, fake transport


def test_pulse_seams_deliver_exactly_one_canary_with_network_zero(tmp_path):
    from tokmo.programs.integrated_pulse import run_pulse_once

    _write_gate(tmp_path, _canary_body())
    _seed_empty_corpus(tmp_path)
    calls = []
    deps = _build(tmp_path, telegram_sender=_sender_recorder(calls))
    producer = deps.news_producer_deps
    ok_classifier = _ok_classifier()
    # G3 live-lane follow-up: production wires the async classifier queue;
    # this test keeps exercising the synchronous seam by unbinding it.
    deps = dataclasses.replace(
        deps,
        news_producer_deps=dataclasses.replace(
            producer, classifier_call=ok_classifier, classifier_queue=None
        ),
    )
    store = deps.news_producer_deps.episode_store
    ops = deps.news_producer_deps.ops_kernel
    _seed_candidate(store, "펄스캐너리종목", now=NOW - timedelta(seconds=60))
    first = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=NOW,
        observed_now_utc=NOW,
        deps=deps,
    )
    stages = {item["stage"]: item for item in first.stages}
    assert stages["news_producer"]["status"] in ("complete", "partial")
    assert stages["news_delivery"]["status"] == "complete"
    assert len(calls) == 1
    assert store.canary_attempt(ACTIVATION_ID)["outcome"] == "SENT"
    (row,) = _telegram_rows(ops)
    assert row[2] == "SENT" and row[3] == "777"
    bound = store.connection.execute(
        "SELECT delivery_state,provider_message_id FROM alert_revisions "
        "WHERE ops_alert_id=?",
        (row[0],),
    ).fetchone()
    assert bound == ("DELIVERY_BOUND", "777")
    # second tick with a fresh eligible event: transport start stays one
    _seed_candidate(store, "두번째종목", now=NOW)
    second = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=NOW + timedelta(seconds=60),
        observed_now_utc=NOW + timedelta(seconds=60),
        deps=deps,
    )
    assert len(calls) == 1
    stages2 = {item["stage"]: item for item in second.stages}
    assert stages2["news_delivery"]["status"] == "complete"
    assert [r[2] for r in _telegram_rows(ops)] == ["SENT"]


def test_pulse_shadow_gate_stays_fully_shadow(tmp_path):
    from tokmo.programs.integrated_pulse import run_pulse_once

    _write_gate(
        tmp_path,
        {
            "enabled": True,
            "cutoff": CANARY_CUTOFF.isoformat(),
            "approved_by": "woong",
            "producer_mode": "SHADOW",
        },
    )
    _seed_empty_corpus(tmp_path)
    calls = []
    deps = _build(tmp_path, telegram_sender=_sender_recorder(calls))
    deps = dataclasses.replace(
        deps,
        news_producer_deps=dataclasses.replace(
            deps.news_producer_deps,
            classifier_call=_ok_classifier(),
            classifier_queue=None,
        ),
    )
    store = deps.news_producer_deps.episode_store
    ops = deps.news_producer_deps.ops_kernel
    _seed_candidate(store, "쉐도우호환종목", now=NOW - timedelta(seconds=60))
    run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=NOW,
        observed_now_utc=NOW,
        deps=deps,
    )
    assert calls == []
    assert _telegram_rows(ops) == []
    shadow = (
        ops._connect()
        .execute("SELECT count(*) FROM alert_outbox WHERE channel='SHADOW'")
        .fetchone()
    )
    assert shadow[0] >= 1
    assert store.canary_attempt(ACTIVATION_ID) is None


# ------------------------------------------------------------ test helpers


def _ok_classifier():
    def call(request):
        return json.dumps(
            {
                "provider": request.provider,
                "model_id": request.model_id,
                "citations": [],
                "event_class": "ORDINARY",
            }
        )

    return call


def _seed_empty_corpus(tmp_path):
    path = tmp_path / "news" / "external_news_v4.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE news_documents(news_document_id TEXT, title TEXT,"
        " title_fingerprint TEXT, content_hash TEXT, source_id TEXT,"
        " first_seen_at TEXT, revision_no INTEGER, summary_text TEXT)"
    )
    connection.commit()
    connection.close()


def _mark_slot_done(store, now, *, slot_seconds=1800):
    slot = (int(now.timestamp()) // slot_seconds) * slot_seconds
    window_to = datetime.fromtimestamp(slot, UTC)
    store.connection.execute(
        "INSERT INTO discovery_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"ndr-slotmark-{slot}",
            window_to.isoformat(),
            window_to.isoformat(),
            "0" * 64,
            "0" * 64,
            0,
            0,
            0,
            "OK",
            now.isoformat(),
            now.isoformat(),
        ),
    )
    store.connection.commit()


def _seed_candidate(store, term, *, now, observed_at=None):
    import hashlib

    run_id = (
        "ndr-"
        + hashlib.sha256(f"seed\0{now.isoformat()}\0{term}".encode()).hexdigest()[:24]
    )
    store.connection.execute(
        "INSERT INTO discovery_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            now.isoformat(),
            now.isoformat(),
            "1" * 64,
            "2" * 64,
            3,
            3,
            0,
            "OK",
            now.isoformat(),
            now.isoformat(),
        ),
    )
    cid = "ndc-" + hashlib.sha256(f"{run_id}\0{term}".encode()).hexdigest()[:24]
    evidence = json.dumps(
        {
            "v": 1,
            "document_ids": [f"{term}-0", f"{term}-1", f"{term}-2"],
            "latest_observed_at": (
                observed_at if observed_at is not None else NOW - timedelta(minutes=5)
            ).isoformat(),
            "headline": f"{term} 수주 확대 공시",
        },
        separators=(",", ":"),
    )
    store.connection.execute(
        "INSERT INTO discovery_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            cid,
            run_id,
            "WHOLE_CORPUS_FREQUENCY",
            None,
            term,
            3,
            3,
            evidence,
            "SURFACED",
            None,
            now.isoformat(),
            now.isoformat(),
        ),
    )
    store.connection.commit()
    return cid


# ------------------- M2/M3 wiring: identity pairs and typed interest terms


def test_wired_identity_pairs_resolve_registry_and_market(tmp_path):
    """Holdings never mint a bare-symbol identity: registry canonical when an
    official alias matches, stable market-qualified identity otherwise."""
    from types import SimpleNamespace

    _write_gate(
        tmp_path,
        {
            "enabled": True,
            "producer_mode": "SHADOW",
            "cutoff": CANARY_CUTOFF.isoformat(),
            "approved_by": "woong",
        },
    )
    scope = "toss-acct-" + "a" * 24
    playbooks = tmp_path / "playbooks"
    for symbol, name in (("000660", "SK하이닉스"), ("005930", "삼성전자")):
        path = playbooks / "personal" / "KR" / f"{symbol}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'---\ninstrument_code: "{symbol}"\ninstrument_name: "{name}"\n---\n',
            encoding="utf-8",
        )
    holding = lambda symbol: SimpleNamespace(
        symbol=symbol, market_raw="KR", market_field=None
    )
    holdings_store = SimpleNamespace(
        read=lambda: SimpleNamespace(
            accounts=(
                SimpleNamespace(
                    account_scope=scope, holdings=(holding("000660"), holding("005930"))
                ),
            )
        )
    )
    from tokmo.programs.pulse_production_deps import (
        build_pulse_production_deps,
    )

    deps = build_pulse_production_deps(
        root=tmp_path,
        clock=lambda: NOW,
        watchlist_symbols={Market.KR: frozenset(), Market.US: frozenset()},
        dialogue_cutoff=NOW,
        holdings_store=holdings_store,
        playbook_root=playbooks,
    )
    pairs = deps.news_producer_deps.identity_pairs()
    assert "ent:sk-hynix" in pairs
    assert "000660" in pairs["ent:sk-hynix"]
    assert "SK하이닉스" in pairs["ent:sk-hynix"]
    assert "ent:000660" not in pairs and "ent:005930" not in pairs
    assert "ent:kr:005930" in pairs
    assert "삼성전자" in pairs["ent:kr:005930"]


def test_wired_interest_terms_bound_and_fail_closed(tmp_path):
    """Production binds the typed interest-term enumerator; with no
    watchlist files and an empty ops kernel it degrades to nothing."""
    _write_gate(
        tmp_path,
        {
            "enabled": True,
            "producer_mode": "SHADOW",
            "cutoff": CANARY_CUTOFF.isoformat(),
            "approved_by": "woong",
        },
    )
    deps = _build(tmp_path)
    seam = deps.news_producer_deps.interest_terms
    assert seam is not None
    assert dict(seam()) == {}
