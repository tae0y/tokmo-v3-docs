import json
from datetime import UTC, datetime, timedelta

import pytest

from tokmo.programs import pulse_production_deps as production
from tokmo.programs.news_delivery_route import build_news_sender


def configure(root, **changes):
    identity = root / "hermes.env"
    identity.write_text('UNRELATED=value\nTELEGRAM_BOT_TOKEN="hermes-test-token"\n')
    identity.chmod(0o600)
    route = dict(
        destination="TOKMO_GROUP", chat_id="-10012345", credential_file=str(identity)
    )
    route.update(changes)
    (root / "news_delivery_route.json").write_text(json.dumps(route))


def test_group_route_uses_hermes_identity_and_scopes_receipt(tmp_path):
    configure(tmp_path)
    sent = []

    def transport(**kwargs):
        sent.append(kwargs)
        return 117

    sender = build_news_sender(
        tmp_path,
        lambda *a, **kw: pytest.fail("Stock bot must not receive NEWS"),
        transport,
    )
    assert sender("news") == "telegram-group:-10012345:117"
    assert sent == [
        dict(
            body_text="news",
            bot_token="hermes-test-token",
            chat_id="-10012345",
            reply_markup=None,
        )
    ]


def test_absent_route_preserves_original_sender(tmp_path):
    stock = lambda *a, **kw: 10
    assert build_news_sender(tmp_path, stock, None) is stock


@pytest.mark.parametrize("invalid", ["chat", "identity", "transport"])
def test_group_failure_never_falls_back_to_stock(tmp_path, invalid):
    configure(tmp_path, **({"chat_id": "12345"} if invalid == "chat" else {}))
    if invalid == "identity":
        (tmp_path / "hermes.env").write_text("UNRELATED=value\n")
    calls = []

    def unavailable(**kwargs):
        calls.append(kwargs)
        raise TimeoutError("transport unavailable")

    sender = build_news_sender(
        tmp_path,
        lambda *a, **kw: pytest.fail("fallback would duplicate or misroute NEWS"),
        unavailable,
    )
    with pytest.raises((ValueError, TimeoutError)):
        sender("news")
    assert len(calls) == (1 if invalid == "transport" else 0)


@pytest.mark.parametrize("use_group", [False, True])
def test_pulse_routes_news_separately_from_trade_dialogue(
    tmp_path, monkeypatch, use_group
):
    now = datetime(2026, 9, 6, 13, tzinfo=UTC)
    (tmp_path / "news_activation.json").write_text(
        json.dumps(
            dict(
                enabled=True,
                approved_by="woong",
                cutoff=now.isoformat(),
                activation_id="news-group-test",
                producer_mode="SHADOW",
                delivery_mode="TELEGRAM_CANARY",
                telegram_attempt_cap=1,
                window_end=(now + timedelta(minutes=20)).isoformat(),
            )
        )
    )
    (tmp_path / "news_activation.json").chmod(0o600)
    stock, group = [], []

    def stock_sender(text, **kwargs):
        stock.append(text)
        return 10

    def group_sender(text, **kwargs):
        group.append(text)
        return "telegram-group:-10012345:117"

    # Exercise the composition boundary used by both delivery modes without
    # contacting Telegram or creating a real trade/order.
    monkeypatch.setattr(
        production,
        "run_canary_alert_delivery",
        lambda **kw: kw["telegram_send"]("news"),
    )
    deps = production.build_pulse_production_deps(
        root=tmp_path,
        clock=lambda: now,
        watchlist_symbols={},
        dialogue_cutoff=now,
        telegram_sender=stock_sender,
        news_telegram_sender=group_sender if use_group else None,
    )
    assert deps.news_delivery_runner(now) == (
        "telegram-group:-10012345:117" if use_group else 10
    )
    assert deps.telegram_sender("trade") == 10
    assert stock == (["trade"] if use_group else ["news", "trade"])
    assert group == (["news"] if use_group else [])
