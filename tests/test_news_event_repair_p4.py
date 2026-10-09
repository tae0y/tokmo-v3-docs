from datetime import UTC, datetime

import pytest

from tokmo.programs.news_alert_delivery import run_alert_delivery
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import (
    AlertChannel,
    AlertOrigin,
    AlertOutboxV1,
    AlertStatus,
    OpsKernelTransitionError,
    ProducerRole,
    ReplayResult,
)

NOW = datetime(2026, 8, 21, tzinfo=UTC)


def _news(channel=AlertChannel.SHADOW):
    return AlertOutboxV1(
        "nal-1",
        AlertOrigin.NEWS_EVENT,
        None,
        None,
        None,
        None,
        0,
        "nep-1",
        "news:nep-1:1",
        channel,
        "CORE_BREAKING",
        ("body", "cb:" + "a" * 32),
        NOW,
        status=AlertStatus.PENDING if channel is AlertChannel.TELEGRAM else None,
    )


def test_news_origin_invariants_and_exactly_once_canonical_outbox(tmp_path):
    with pytest.raises(ValueError):
        AlertOutboxV1(
            "bad",
            AlertOrigin.NEWS_EVENT,
            "decision",
            None,
            None,
            None,
            0,
            "nep-1",
            "news:nep-1:1",
            AlertChannel.SHADOW,
            "ORDINARY",
            ("body",),
            NOW,
        )
    store = OpsKernelStore(
        tmp_path / "ops" / "kernel.sqlite", stable_phase_ids=frozenset()
    )
    item = _news()
    assert (
        store.record_news_alert_outbox(ProducerRole.HERMES, item)
        is ReplayResult.CREATED
    )
    assert (
        store.record_news_alert_outbox(ProducerRole.HERMES, item)
        is ReplayResult.DUPLICATE
    )
    with pytest.raises(OpsKernelTransitionError):
        store.record_news_alert_outbox(
            ProducerRole.HERMES, _news(AlertChannel.TELEGRAM), activation_enabled=False
        )


def test_single_origin_branch_worker_unknown_never_resends(tmp_path):
    store = OpsKernelStore(
        tmp_path / "ops" / "kernel.sqlite", stable_phase_ids=frozenset()
    )
    item = _news(AlertChannel.TELEGRAM)
    store.record_news_alert_outbox(ProducerRole.HERMES, item, activation_enabled=True)
    calls = []

    def sent(text, reply_markup=None):
        calls.append((text, reply_markup))
        return "provider-1"

    original = store.mark_sent
    store.mark_sent = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("settle crash")
    )
    result = run_alert_delivery(
        ops_kernel=store,
        role=ProducerRole.HERMES,
        telegram_send=sent,
        keyboard_resolver=lambda alert_id: {"inline_keyboard": []},
        delivery_binder=lambda *args, **kwargs: None,
        now=NOW,
    )
    store.mark_sent = original
    assert result["unknown"] == 1 and len(calls) == 1
    assert (
        run_alert_delivery(
            ops_kernel=store,
            role=ProducerRole.HERMES,
            telegram_send=sent,
            keyboard_resolver=lambda alert_id: None,
            delivery_binder=lambda *args, **kwargs: None,
            now=NOW,
        )["claimed"]
        == 0
    )
