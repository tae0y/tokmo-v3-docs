"""Actual outbox failure -> durable health -> explicit value reassessment."""

from datetime import timedelta
from urllib.error import HTTPError

import pytest
from run_news_decision_worker import health_notice, health_summary
from test_news_decision_engine import decision
from test_news_decision_s3 import DAY, ROLE, scene, tick

from tokmo.programs.news_alert_delivery import run_alert_delivery
from tokmo.programs.news_decision_outbox import (
    queue_proposals,
    reconcile_deliveries,
    reconsider_failed_proposals,
)


def fail_next(engine, ops, *, code=503, receipt=None):
    def sender(*args, **kwargs):
        if receipt:
            return receipt
        raise HTTPError("https://example.invalid", code, "temporary", {}, None)

    return run_alert_delivery(
        ops_kernel=ops,
        role=ROLE,
        telegram_send=sender,
        keyboard_resolver=lambda _: None,
        delivery_binder=lambda *a, **kw: None,
        now=DAY,
    )


def test_failed_news_remains_visible_then_reassesses_without_automatic_resend(tmp_path):
    store, ops, engine = scene(tmp_path)
    try:
        alert = tick(engine, "a", 0)["alerts"][0]
        queue_proposals(engine, ops, ROLE, cutoff=DAY, activation_enabled=True)
        assert fail_next(engine, ops)["failed"] == 1
        reconcile_deliveries(engine, ops)
        assert not engine.pending_alerts()
        report = {"status": "DEGRADED", "health": health_summary(store, {}, ops)}
        assert report["health"]["delivery_unresolved"][0]["statuses"] == ["FAILED"]
        state, notice = health_notice(report, None, DAY)
        assert "복구 대기 1건" in notice
        assert health_notice(report, state, DAY + timedelta(minutes=1))[1] is None
        with pytest.raises(ValueError):
            reconsider_failed_proposals(
                engine, ops, [alert["id"]], at=DAY, reason="without investigation"
            )
        at = DAY + timedelta(minutes=1)
        result = reconsider_failed_proposals(
            engine,
            ops,
            [alert["id"]],
            at=at,
            reason="provider log confirmed rejection",
            confirmed_not_sent=True,
        )
        assert (
            result["reconsidered"] == 1
            and not health_summary(store, {}, ops)["delivery_unresolved"]
        )
        event = store.events()[0]
        refs = event["delivery_refs"][alert["id"]]
        assert all(ops.read_outbox(key).status.value == "FAILED" for key in refs)
        assert (
            not engine.pending_alerts()
            and event["delivery_resolutions"][alert["id"]]["reason"]
        )
        assert (
            reconsider_failed_proposals(
                engine,
                ops,
                [alert["id"]],
                at=at,
                reason="same",
                confirmed_not_sent=True,
            )["reconsidered"]
            == 0
        )
        seen = []

        def analyze(request):
            seen.append(request)
            return decision(["a"])

        engine.analyze = analyze
        result = engine.tick([], at)
        assert not result["failures"] and result["alerts"]
        assert seen[0]["review_reason"] == "DELIVERY_RECOVERY"
        assert all(
            p["alert_id"] != alert["id"] for p in seen[0]["comparison"]["pending"]
        )
        assert result["alerts"][0]["id"] != alert["id"]
        assert all(ops.read_outbox(key).status.value == "FAILED" for key in refs)
        queue_proposals(engine, ops, ROLE, cutoff=DAY, activation_enabled=True)
        assert fail_next(engine, ops, receipt="71")["sent"] == 1
        reconcile_deliveries(engine, ops)
        assert store.events()[0]["last_sent"]["alert_id"] == result["alerts"][0]["id"]
        assert store.events()[0]["last_sent"]["outbox_refs"] != refs
        assert all(ops.read_outbox(key).status.value == "FAILED" for key in refs)
    finally:
        store.close()
        ops.close()


def test_recovery_cli_changes_only_selected_state_without_model_or_latest(
    tmp_path, capsys
):
    import json

    import run_news_decision_worker as cli
    from test_news_decision_engine import Engine, screen

    from tokmo.records.news_decision_store import NewsDecisionStore
    from tokmo.records.ops_kernel import OpsKernelStore

    (tmp_path / "news_decision.json").write_text(
        json.dumps({"enabled": True, "cutoff": DAY.isoformat()})
    )
    store = NewsDecisionStore(tmp_path / "stores/news_episodes.sqlite3")
    ops = OpsKernelStore(
        tmp_path / "ops/ops_kernel.sqlite3", stable_phase_ids=frozenset()
    )
    engine = Engine(store, screen, lambda _: decision())
    try:
        alert = tick(engine, "a", 0)["alerts"][0]
        queue_proposals(engine, ops, ROLE, cutoff=DAY, activation_enabled=True)
        fail_next(engine, ops)
        reconcile_deliveries(engine, ops)
        code = cli.main(
            [
                "--root",
                str(tmp_path),
                "--now",
                DAY.isoformat(),
                "--recover-failed-alert-id",
                alert["id"],
                "--confirmed-not-sent",
                "--recovery-reason",
                "provider rejection verified",
            ],
            build=lambda **kw: pytest.fail("model setup not needed"),
        )
        assert code == 0 and json.loads(capsys.readouterr().out)["reconsidered"] == 1
        assert not (tmp_path / "news/decision-worker/latest.json").exists()
        assert store.events()[0]["delivery_resolutions"][alert["id"]][
            "confirmed_not_sent"
        ]
    finally:
        store.close()
        ops.close()


def test_partial_delivery_cannot_be_requeued_as_wholly_unsent(tmp_path):
    store, ops, engine = scene(tmp_path)
    try:
        alert = tick(engine, "a", 0)["alerts"][0]
        event = store.events()[0]
        event["pending_alerts"][0]["body"] = "긴 메시지 " * 1200
        store.save_event(event)
        queue_proposals(engine, ops, ROLE, cutoff=DAY, activation_enabled=True)
        fail_next(engine, ops, receipt="42")
        fail_next(engine, ops)
        reconcile_deliveries(engine, ops)
        with pytest.raises(ValueError, match="wholly failed"):
            reconsider_failed_proposals(
                engine,
                ops,
                [alert["id"]],
                at=DAY,
                reason="one part rejected",
                confirmed_not_sent=True,
            )
        assert health_summary(store, {}, ops)["delivery_unresolved"]
        assert not store.events()[0].get("delivery_resolutions")
    finally:
        store.close()
        ops.close()


def test_active_transport_is_not_a_recovery_issue_but_timeout_remains_visible(tmp_path):
    store, ops, engine = scene(tmp_path)
    try:
        alert = tick(engine, "a", 0)["alerts"][0]
        queue_proposals(engine, ops, ROLE, cutoff=DAY, activation_enabled=True)
        refs = store.events()[0]["delivery_refs"][alert["id"]]

        def sending(*args, **kwargs):
            assert all(
                ops.read_outbox(key).status.value == "ATTEMPTING" for key in refs
            )
            assert not health_summary(store, {}, ops)["delivery_unresolved"]
            raise TimeoutError("receipt not confirmed")

        run_alert_delivery(
            ops_kernel=ops,
            role=ROLE,
            telegram_send=sending,
            keyboard_resolver=lambda _: None,
            delivery_binder=lambda *a, **kw: None,
            now=DAY,
        )
        reconcile_deliveries(engine, ops)
        assert health_summary(store, {}, ops)["delivery_unresolved"][0]["statuses"] == [
            "UNKNOWN"
        ]
    finally:
        store.close()
        ops.close()
