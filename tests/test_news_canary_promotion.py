"""NEWS canary auto-promotion controller RED suite (2026-08-22 watchdog).

The controller is deterministic and zero-model/zero-network: every
judgment is a durable-state read through the production gate parser,
canary readback, and outbox; the only writes are the controller's own
atomic gate rewrite (0600 + fsync + byte backup) and the durable slot
proof recorded via the production ``record_canary_duplicate_proof`` seam.
Cron ``no_agent`` semantics: empty stdout + exit 0 on every wait, exactly
one concise Korean line on a terminal transition (promotion or OFF), and
a non-zero exit only for a broken controller. Telegram is never sent.
"""
import fcntl
import json
import os
import stat as stat_module
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tokmo.records.news_episode_store import NewsEpisodeStore
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import (
    AlertChannel,
    AlertOrigin,
    AlertOutboxV1,
    AlertStatus,
    ProducerRole,
)

UTC = timezone.utc
CANARY_ID = "news-taxonomy-canary-20260822T022825Z-r3"
CUTOFF = datetime(2026, 8, 22, 2, 28, 25, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 24, 3, 30, tzinfo=UTC)  # Mon 12:30 KST
NOW = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)          # inside the window
EXPIRED_NOW = datetime(2026, 8, 24, 4, 0, tzinfo=UTC)  # past window_end
RESERVED_AT = CUTOFF + timedelta(minutes=5)
SETTLED_AT = CUTOFF + timedelta(minutes=10)

GOOD_BODY = ("📰 삼성전자 뉴스 동향\n"
             "사건: 삼성전자 3분기 영업이익 15조 발표\n"
             "근거: 기사 3건 · 출처 2곳 · 포트폴리오 직접 관련")


def _canary_gate(**extra):
    body = {
        "enabled": True, "producer_mode": "SHADOW",
        "delivery_mode": "TELEGRAM_CANARY",
        "activation_id": CANARY_ID,
        "cutoff": CUTOFF.isoformat(),
        "window_end": WINDOW_END.isoformat(),
        "telegram_attempt_cap": 1, "approved_by": "woong",
    }
    body.update(extra)
    return {key: value for key, value in body.items() if value is not ...}


def _write_gate(root, payload, mode=0o600):
    path = Path(root) / "news_activation.json"
    path.write_text(payload if isinstance(payload, str)
                    else json.dumps(payload))
    os.chmod(path, mode)
    return path


def _stores(tmp_path):
    news = NewsEpisodeStore(tmp_path / "stores" / "news_episodes.sqlite3")
    ops = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3",
                         stable_phase_ids=frozenset())
    return news, ops


def _outbox_row(alert_id, *, created_at, body=GOOD_BODY):
    return AlertOutboxV1(
        alert_id, AlertOrigin.NEWS_EVENT, None, None, None, None, 0,
        f"nep-{alert_id}", f"news:{alert_id}:1", AlertChannel.TELEGRAM,
        "ORDINARY", (body, "cb:" + alert_id), created_at,
        status=AlertStatus.PENDING)


def _sent_row(ops, alert_id, *, created_at, sent_at, body=GOOD_BODY):
    """One canonical-outbox NEWS Telegram transport start settled SENT."""
    ops.record_news_alert_outbox(ProducerRole.HERMES,
                                 _outbox_row(alert_id, created_at=created_at,
                                             body=body),
                                 activation_enabled=True)
    claimed = ops.claim_pending_telegram(
        ProducerRole.HERMES, sent_at, origins=(AlertOrigin.NEWS_EVENT.value,),
        created_not_before=created_at)
    assert claimed is not None and claimed.alert_id == alert_id
    ops.mark_sent(ProducerRole.HERMES, alert_id, sent_at, "777")


def _seed_sent_canary(news, ops, *, alert_id="nal-canary", body=GOOD_BODY,
                      ledger_alert_id=None):
    _sent_row(ops, alert_id, created_at=RESERVED_AT, sent_at=SETTLED_AT,
              body=body)
    news.reserve_canary_attempt(CANARY_ID, ledger_alert_id or alert_id,
                                now=RESERVED_AT)
    news.settle_canary_attempt(CANARY_ID, "SENT", now=SETTLED_AT)


def _historical_noise(ops, count=26):
    """26 pre-cutoff SENT transport starts + one pre-cutoff PENDING bond."""
    for index in range(count):
        created = CUTOFF - timedelta(days=2, minutes=index + 1)
        _sent_row(ops, f"nal-hist-{index}", created_at=created,
                  sent_at=created + timedelta(minutes=1))
    ops.record_news_alert_outbox(
        ProducerRole.HERMES,
        _outbox_row("nal-precutoff-bond",
                    created_at=CUTOFF - timedelta(hours=1)),
        activation_enabled=True)


def _complete_slots(tmp_path, slots=(SETTLED_AT + timedelta(minutes=50),
                                     SETTLED_AT + timedelta(minutes=110))):
    from tokmo.records.pulse_tick_receipts import PulseTickReceiptStore
    for index, slot in enumerate(slots):
        receipts = PulseTickReceiptStore(
            tmp_path / "ops" / "pulse_tick_receipts.sqlite3",
            clock=lambda slot=slot: slot)
        receipts.begin_tick("pulse-60s-1", slot, index + 1,
                            observed_start=slot, lag_seconds=0.0,
                            max_lag_seconds=30)
        receipts.finish_tick("pulse-60s-1", slot, status="SAVED",
                             reason=None, stages=(), wire_counts={},
                             finished_at=slot)


def _run(tmp_path, *, now=NOW):
    from tokmo.programs.news_canary_promotion import run_canary_promotion
    return run_canary_promotion(tmp_path, now=now)


def _main(tmp_path, capsys, *, now=NOW):
    from tokmo.programs.news_canary_promotion import main
    code = main(["--root", str(tmp_path), "--now", now.isoformat()])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _gate_payload(tmp_path):
    return json.loads((Path(tmp_path) / "news_activation.json").read_text())


def _gate_mode(tmp_path):
    return stat_module.S_IMODE(
        os.stat(Path(tmp_path) / "news_activation.json").st_mode)


def _backups(tmp_path):
    return sorted(Path(tmp_path).glob("news_activation.json.bak-*"))


# ---------------------------------------------------------------- no-op reads


def test_noop_when_gate_absent(tmp_path, capsys):
    code, out, err = _main(tmp_path, capsys)
    assert (code, out, err) == (0, "", "")


@pytest.mark.parametrize("payload", [
    "{not json",                                   # malformed bytes
    {"enabled": False},                            # disabled
    _canary_gate(enabled=...),                     # invalid activation
    _canary_gate(delivery_mode=...),               # producer-only gate
])
def test_noop_when_gate_not_canary_and_file_untouched(tmp_path, capsys,
                                                      payload):
    path = _write_gate(tmp_path, payload)
    before = path.read_bytes()
    code, out, err = _main(tmp_path, capsys)
    assert (code, out, err) == (0, "", "")
    assert path.read_bytes() == before
    assert not _backups(tmp_path)


def test_noop_when_gate_already_live(tmp_path, capsys):
    _write_gate(tmp_path, {
        "enabled": True, "producer_mode": "SHADOW",
        "delivery_mode": "TELEGRAM_LIVE", "activation_id": "news-live-x",
        "predecessor_activation_id": CANARY_ID,
        "cutoff": CUTOFF.isoformat(),
        "window_end": (CUTOFF + timedelta(days=1)).isoformat(),
        "approved_by": "woong"})
    code, out, err = _main(tmp_path, capsys)
    assert (code, out, err) == (0, "", "")


# ------------------------------------------------------------------- waiting


def test_wait_silent_when_no_attempt_inside_window(tmp_path, capsys):
    _write_gate(tmp_path, _canary_gate())
    _stores(tmp_path)
    code, out, err = _main(tmp_path, capsys)
    assert (code, out, err) == (0, "", "")
    assert _gate_payload(tmp_path)["enabled"] is True


def test_wait_silent_when_reserved_is_recent(tmp_path, capsys):
    _write_gate(tmp_path, _canary_gate())
    news, _ = _stores(tmp_path)
    news.reserve_canary_attempt(CANARY_ID, "nal-canary",
                                now=NOW - timedelta(minutes=5))
    code, out, err = _main(tmp_path, capsys)
    assert (code, out, err) == (0, "", "")
    assert _gate_payload(tmp_path)["enabled"] is True


def test_wait_silent_when_sent_but_fewer_than_two_slots(tmp_path, capsys):
    _write_gate(tmp_path, _canary_gate())
    news, ops = _stores(tmp_path)
    _seed_sent_canary(news, ops)
    _complete_slots(tmp_path, slots=(SETTLED_AT + timedelta(minutes=50),))
    code, out, err = _main(tmp_path, capsys)
    assert (code, out, err) == (0, "", "")
    assert news.canary_slot_proof(CANARY_ID) is None
    assert _gate_payload(tmp_path)["delivery_mode"] == "TELEGRAM_CANARY"


# ------------------------------------------------------------- expiry -> OFF


def test_expiry_without_attempt_writes_off_once(tmp_path, capsys):
    _write_gate(tmp_path, _canary_gate())
    _stores(tmp_path)
    original = (Path(tmp_path) / "news_activation.json").read_bytes()
    code, out, err = _main(tmp_path, capsys, now=EXPIRED_NOW)
    assert code == 0 and err == ""
    assert out.count("\n") == 1 and "canary_expired_no_eligible_event" in out
    payload = _gate_payload(tmp_path)
    assert payload["enabled"] is False
    assert payload["delivery_mode"] == "OFF"
    assert payload["off_reason"] == "canary_expired_no_eligible_event"
    assert _gate_mode(tmp_path) == 0o600
    backups = _backups(tmp_path)
    assert len(backups) == 1 and backups[0].read_bytes() == original
    # production parser resolves the rewritten gate fully OFF
    from tokmo.programs.pulse_production_deps import read_news_gate
    snapshot = read_news_gate(tmp_path)
    assert snapshot[1] == "OFF" and snapshot.delivery.mode == "OFF"
    # idempotent replay: silent, no second line, bytes unchanged
    after = (Path(tmp_path) / "news_activation.json").read_bytes()
    code, out, err = _main(tmp_path, capsys, now=EXPIRED_NOW)
    assert (code, out, err) == (0, "", "")
    assert (Path(tmp_path) / "news_activation.json").read_bytes() == after


# --------------------------------------------------- failed attempts -> OFF


@pytest.mark.parametrize("outcome,reason", [
    ("FAILED", "canary_attempt_failed"),
    ("UNKNOWN", "canary_attempt_unknown"),
])
def test_settled_failure_outcomes_fail_closed_off(tmp_path, capsys, outcome,
                                                  reason):
    _write_gate(tmp_path, _canary_gate())
    news, _ = _stores(tmp_path)
    news.reserve_canary_attempt(CANARY_ID, "nal-canary", now=RESERVED_AT)
    news.settle_canary_attempt(CANARY_ID, outcome, now=SETTLED_AT)
    code, out, err = _main(tmp_path, capsys)
    assert code == 0 and err == ""
    assert out.count("\n") == 1 and reason in out
    assert _gate_payload(tmp_path)["off_reason"] == reason
    assert _gate_payload(tmp_path)["enabled"] is False


def test_stale_reserved_fails_closed_off(tmp_path, capsys):
    _write_gate(tmp_path, _canary_gate())
    news, _ = _stores(tmp_path)
    news.reserve_canary_attempt(CANARY_ID, "nal-canary",
                                now=NOW - timedelta(minutes=31))
    code, out, err = _main(tmp_path, capsys)
    assert code == 0 and err == ""
    assert "canary_attempt_reserved_stale" in out
    assert _gate_payload(tmp_path)["enabled"] is False


# -------------------------------------------------- outbox receipt judgment


def test_missing_outbox_row_fails_closed_off(tmp_path, capsys):
    _write_gate(tmp_path, _canary_gate())
    news, ops = _stores(tmp_path)
    _seed_sent_canary(news, ops, ledger_alert_id="nal-vanished")
    _complete_slots(tmp_path)
    code, out, err = _main(tmp_path, capsys)
    assert code == 0 and "canary_outbox_row_missing" in out
    assert _gate_payload(tmp_path)["enabled"] is False


def test_incomplete_provider_receipt_fails_closed_off(tmp_path, capsys):
    _write_gate(tmp_path, _canary_gate())
    news, ops = _stores(tmp_path)
    _seed_sent_canary(news, ops)
    _complete_slots(tmp_path)
    ops._connect().execute(
        "UPDATE alert_outbox SET provider_message_id=NULL"
        " WHERE alert_id='nal-canary'")
    code, out, err = _main(tmp_path, capsys)
    assert code == 0 and "canary_outbox_receipt_incomplete" in out
    assert _gate_payload(tmp_path)["enabled"] is False
    assert news.canary_slot_proof(CANARY_ID) is None  # never reached proof


def test_outbox_status_not_sent_fails_closed_off(tmp_path, capsys):
    _write_gate(tmp_path, _canary_gate())
    news, ops = _stores(tmp_path)
    # ledger says SENT but the canonical row never left PENDING
    ops.record_news_alert_outbox(
        ProducerRole.HERMES, _outbox_row("nal-canary",
                                         created_at=RESERVED_AT),
        activation_enabled=True)
    news.reserve_canary_attempt(CANARY_ID, "nal-canary", now=RESERVED_AT)
    news.settle_canary_attempt(CANARY_ID, "SENT", now=SETTLED_AT)
    _complete_slots(tmp_path)
    code, out, err = _main(tmp_path, capsys)
    assert code == 0 and "canary_outbox_status_not_sent" in out
    assert _gate_payload(tmp_path)["enabled"] is False


def test_pre_cutoff_outbox_row_fails_closed_off(tmp_path, capsys):
    _write_gate(tmp_path, _canary_gate())
    news, ops = _stores(tmp_path)
    created = CUTOFF - timedelta(hours=1)
    _sent_row(ops, "nal-canary", created_at=created,
              sent_at=created + timedelta(minutes=1))
    news.reserve_canary_attempt(CANARY_ID, "nal-canary", now=RESERVED_AT)
    news.settle_canary_attempt(CANARY_ID, "SENT", now=SETTLED_AT)
    _complete_slots(tmp_path)
    code, out, err = _main(tmp_path, capsys)
    assert code == 0 and "canary_outbox_pre_cutoff" in out
    assert _gate_payload(tmp_path)["enabled"] is False


@pytest.mark.parametrize("body,reason", [
    ('{"episode_id": "nep-abc", "revision_no": 1, "body_text": "본문"}',
     "body_internal_json"),
    ("NEWS_EVENT PENDING 라우팅 대기 항목이 있습니다. 상세 내역을 확인하세요.",
     "body_routing_label"),
    ("밤사이 최신 material 상태 통합: 📰 삼성전자 뉴스 동향 판정: 긴급 알림 아님",
     "body_boilerplate"),
    ("속보 알림 · 근거: 기사 26건 · 출처 12곳 · 업데이트 3회",
     "body_count_only"),
    ("📰 could 뉴스 동향\n사건: could rise more than expected this week\n"
     "근거: 기사 2건 · 출처 2곳", "body_generic_subject"),
    ("본문", "body_too_short"),
])
def test_meaningless_body_fails_closed_off(tmp_path, capsys, body, reason):
    _write_gate(tmp_path, _canary_gate())
    news, ops = _stores(tmp_path)
    _seed_sent_canary(news, ops, body=body)
    _complete_slots(tmp_path)
    code, out, err = _main(tmp_path, capsys)
    assert code == 0 and err == ""
    assert out.count("\n") == 1 and reason in out
    assert _gate_payload(tmp_path)["off_reason"].endswith(reason)
    assert news.canary_slot_proof(CANARY_ID) is None  # rejected before proof


# ------------------------------------------------------- duplicate transport


def test_duplicate_post_cutoff_transport_fails_closed_off(tmp_path, capsys):
    _write_gate(tmp_path, _canary_gate())
    news, ops = _stores(tmp_path)
    _seed_sent_canary(news, ops)
    _sent_row(ops, "nal-dup", created_at=SETTLED_AT + timedelta(minutes=5),
              sent_at=SETTLED_AT + timedelta(minutes=6))
    _complete_slots(tmp_path)
    code, out, err = _main(tmp_path, capsys)
    assert code == 0 and "canary_proof_refused" in out
    assert _gate_payload(tmp_path)["enabled"] is False
    assert news.canary_slot_proof(CANARY_ID) is None


# ----------------------------------------------------------------- promotion


def test_valid_canary_promotes_once_and_replay_is_silent(tmp_path, capsys):
    _write_gate(tmp_path, _canary_gate())
    news, ops = _stores(tmp_path)
    _historical_noise(ops)  # 26 historical starts + pre-cutoff PENDING bond
    _seed_sent_canary(news, ops)
    _complete_slots(tmp_path)
    original = (Path(tmp_path) / "news_activation.json").read_bytes()
    code, out, err = _main(tmp_path, capsys)
    assert code == 0 and err == ""
    assert out.count("\n") == 1
    assert CANARY_ID in out and WINDOW_END.isoformat() not in out
    payload = _gate_payload(tmp_path)
    assert payload["delivery_mode"] == "TELEGRAM_LIVE"
    assert payload["enabled"] is True
    assert payload["producer_mode"] == "SHADOW"
    assert payload["predecessor_activation_id"] == CANARY_ID
    assert payload["activation_id"] != CANARY_ID
    assert payload["approved_by"] == "woong"
    assert "telegram_attempt_cap" not in payload
    assert payload["window_end"] in out  # live end in the receipt line
    assert GOOD_BODY.splitlines()[1] not in out  # no bodies in the line
    cutoff = datetime.fromisoformat(payload["cutoff"])
    window_end = datetime.fromisoformat(payload["window_end"])
    assert cutoff == NOW  # live cutoff is the promotion instant
    assert window_end - cutoff == timedelta(days=7)  # exactly 7d window
    assert _gate_mode(tmp_path) == 0o600
    backups = _backups(tmp_path)
    assert len(backups) == 1 and backups[0].read_bytes() == original
    proof = news.canary_slot_proof(CANARY_ID)
    assert proof is not None and proof["transport_starts"] == 1
    # production readback authorizes the live lane right now
    from tokmo.programs.news_alert_delivery import live_delivery_authorized
    from tokmo.programs.pulse_production_deps import read_news_gate
    delivery = read_news_gate(tmp_path).delivery
    authorized, reason = live_delivery_authorized(news, delivery, NOW)
    assert (authorized, reason) == (True, None)
    # replay: gate is LIVE now, controller is a silent no-op
    code, out, err = _main(tmp_path, capsys)
    assert (code, out, err) == (0, "", "")
    assert _gate_payload(tmp_path) == payload


def test_promotion_with_existing_proof_row_replays_promotion(tmp_path,
                                                             capsys):
    from tokmo.programs.news_live_control import record_canary_duplicate_proof
    _write_gate(tmp_path, _canary_gate())
    news, ops = _stores(tmp_path)
    _seed_sent_canary(news, ops)
    _complete_slots(tmp_path)
    record_canary_duplicate_proof(root=tmp_path, activation_id=CANARY_ID,
                                  now=NOW - timedelta(minutes=10), store=news)
    code, out, err = _main(tmp_path, capsys)
    assert code == 0 and out.count("\n") == 1
    assert _gate_payload(tmp_path)["delivery_mode"] == "TELEGRAM_LIVE"


def test_gate_write_failure_never_claims_promotion(tmp_path, capsys,
                                                   monkeypatch):
    import tokmo.programs.news_canary_promotion as controller
    _write_gate(tmp_path, _canary_gate())
    news, ops = _stores(tmp_path)
    _seed_sent_canary(news, ops)
    _complete_slots(tmp_path)
    original = (Path(tmp_path) / "news_activation.json").read_bytes()

    def _broken(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(controller, "_atomic_write_bytes", _broken)
    code, out, err = _main(tmp_path, capsys)
    assert code != 0 and out == "" and err != ""
    assert (Path(tmp_path) / "news_activation.json").read_bytes() == original
    monkeypatch.undo()
    # controller recovers on the next tick: proof replays, gate promotes
    code, out, err = _main(tmp_path, capsys)
    assert code == 0 and out.count("\n") == 1
    assert _gate_payload(tmp_path)["delivery_mode"] == "TELEGRAM_LIVE"


def test_readback_mismatch_restores_gate_and_exits_nonzero(tmp_path, capsys,
                                                           monkeypatch):
    import tokmo.programs.news_canary_promotion as controller
    _write_gate(tmp_path, _canary_gate())
    news, ops = _stores(tmp_path)
    _seed_sent_canary(news, ops)
    _complete_slots(tmp_path)
    original = (Path(tmp_path) / "news_activation.json").read_bytes()
    # corrupt the written live payload so the production parser rejects it
    real_payload = controller._live_gate_payload

    def _corrupt(canary_id, now):
        live_id, payload = real_payload(canary_id, now)
        payload["window_end"] = "not-a-timestamp"
        return live_id, payload
    monkeypatch.setattr(controller, "_live_gate_payload", _corrupt)
    code, out, err = _main(tmp_path, capsys)
    assert code != 0
    assert out.count("\n") == 1  # one line reporting the failed verification
    # the canary gate bytes were restored — no half-promoted state remains
    assert (Path(tmp_path) / "news_activation.json").read_bytes() == original


def test_readback_parser_exception_restores_gate_bytes(tmp_path, capsys,
                                                       monkeypatch):
    import tokmo.programs.news_canary_promotion as controller
    _write_gate(tmp_path, _canary_gate())
    news, ops = _stores(tmp_path)
    _seed_sent_canary(news, ops)
    _complete_slots(tmp_path)
    original = (Path(tmp_path) / "news_activation.json").read_bytes()

    def _raise(_root):
        raise ValueError("synthetic parser failure")

    monkeypatch.setattr(controller, "read_news_gate", _raise)
    code, out, err = _main(tmp_path, capsys)
    assert code != 0 and out == "" and err != ""
    assert (Path(tmp_path) / "news_activation.json").read_bytes() == original


# --------------------------------------------------------------- cron seams


def test_flock_second_instance_exits_silently(tmp_path, capsys):
    from tokmo.programs.news_canary_promotion import LOCK_NAME
    _write_gate(tmp_path, _canary_gate())
    _stores(tmp_path)
    lock_path = Path(tmp_path) / LOCK_NAME
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        # even an expired gate stays untouched while another holder runs
        code, out, err = _main(tmp_path, capsys, now=EXPIRED_NOW)
        assert (code, out, err) == (0, "", "")
        assert _gate_payload(tmp_path)["enabled"] is True
    finally:
        os.close(fd)


def test_cli_rejects_naive_now(tmp_path, capsys):
    from tokmo.programs.news_canary_promotion import main
    with pytest.raises(SystemExit):
        main(["--root", str(tmp_path), "--now", "2026-08-22T09:00:00"])


def test_cli_requires_now_before_any_write(tmp_path):
    from tokmo.programs.news_canary_promotion import main
    with pytest.raises(SystemExit, match="2"):
        main(["--root", str(tmp_path)])
    assert list(tmp_path.iterdir()) == []


def test_cli_rejects_naive_provider_before_any_write(tmp_path):
    from tokmo.programs.news_canary_promotion import main
    with pytest.raises(SystemExit, match="2"):
        main(
            ["--root", str(tmp_path)],
            now_provider=lambda: datetime(2026, 8, 22, 9, 0),
        )
    assert list(tmp_path.iterdir()) == []


def test_fixed_provider_matches_explicit_now(tmp_path_factory, capsys):
    from tokmo.programs.news_canary_promotion import main
    explicit_root = tmp_path_factory.mktemp("explicit-now")
    provider_root = tmp_path_factory.mktemp("provider-now")
    explicit_code = main([
        "--root", str(explicit_root), "--now", NOW.isoformat(),
    ])
    explicit = capsys.readouterr()
    calls = []

    def provider():
        calls.append(True)
        return NOW

    provider_code = main(
        ["--root", str(provider_root)], now_provider=provider,
    )
    injected = capsys.readouterr()
    assert calls == [True]
    assert (provider_code, injected.out, injected.err) == (
        explicit_code, explicit.out, explicit.err,
    )


def test_controller_has_no_ambient_clock_source():
    source = (Path(__file__).parents[1] / "src" / "tokmo" / "programs"
              / "news_canary_promotion.py").read_text()
    for forbidden in (
        "datetime.now(", "time.time(", "fromtimestamp(",
        "tokmo.runtime.wallclock", "utc_now(",
    ):
        assert forbidden not in source


def test_controller_never_binds_transport(tmp_path):
    source = (Path(__file__).parents[1] / "src" / "tokmo" / "programs"
              / "news_canary_promotion.py").read_text()
    for forbidden in ("requests", "urllib", "http", "socket",
                      "telegram_send", "api.telegram.org"):
        assert forbidden not in source
