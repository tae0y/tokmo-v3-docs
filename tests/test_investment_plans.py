"""Plan lifecycle with local temporary sources and fake model/transport only."""

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from test_investment_followup import BASE, NOW, response
from test_investment_price_checkpoint import market, quote

from tokmo.programs.investment_followup import deliver_pending, review_choice
from tokmo.programs.investment_followup_worker import run_tick
from tokmo.programs.investment_plans import cancel_plan, register_plan, run_plan_reviews
from tokmo.programs.quant_decision_inputs import read_user_choices
from tokmo.records.investment_followup import InvestmentFollowupStore, read_news_context
from tokmo.values.quant_decisions import parse_snapshot


@pytest.fixture
def store(tmp_path):
    s = InvestmentFollowupStore(tmp_path / "private/investment-followup.sqlite3")
    yield s
    s.close()


def supplied(**changes):
    value = dict(
        raw_text="장기 전망은 믿지만 시장 충격을 보고 다시 분석해줘.",
        interpretation=dict(
            action="WATCH",
            phase="INTENT",
            account="personal-a",
            scope="personal",
            keys=["KR:000001"],
            summary="시장 충격과 장기 전망 점검",
            reason_quotes=["장기 전망은 믿지만 시장 충격을 보고"],
            condition_quotes=[],
            suggested_checks=["기존 가설과 새 위험을 함께 확인한다"],
            supersedes=None,
        ),
        plan=dict(
            kind="ANALYSIS",
            question="현재 가격과 위험을 다시 분석해줘.",
            model="codex:gpt-6-astra",
            reasoning_effort="medium",
            monitor=False,
            review_at=(NOW + timedelta(minutes=5)).isoformat(),
            price=None,
        ),
    )
    value["plan"].update(changes)
    return value


def register(store, root, data=None, source="chat:123"):
    return register_plan(
        store, data or supplied(), source_id=source, at=NOW, project_root=root
    )


def unchanged(prompt):
    return response(
        effect="MAINTAIN",
        notify=False,
        material_change=False,
        pending_action="KEEP",
        comparison="현재 자료로는 기존 판단을 바꿀 변화가 확인되지 않았다.",
        new_facts=[],
    )


def run(store, root, at=None, model=unchanged, **kw):
    return run_plan_reviews(
        store,
        store.choices(at=at or NOW),
        data_root=root,
        snapshot=kw.pop("snapshot", None),
        at=at or NOW,
        model_for_plan=lambda p: model,
        max_reviews=kw.pop("max_reviews", 4),
        attempt=kw.pop("attempt", "1"),
        **kw,
    )


def test_registration_is_idempotent_preserves_playbook_and_reaches_both_inputs(
    store, tmp_path
):
    path = tmp_path / "playbooks/personal/KR/000001.md"
    path.parent.mkdir(parents=True)
    original = "# Existing\n<!-- USER_THESIS_START -->\n기존 사용자 이유\n<!-- USER_THESIS_END -->\n"
    path.write_text(original)
    first = register(store, tmp_path)
    second = register(store, tmp_path)
    assert first["choice"] == second["choice"]
    assert path.read_text().startswith(original)
    assert path.read_text().count("### choice-") == 1
    assert first["choice"]["execution_status"] == "NOT_VERIFIED"
    choices = read_user_choices(tmp_path, at=NOW)
    assert choices[0]["plan"]["model"] == "codex:gpt-6-astra"
    ctx = read_news_context(tmp_path / "private/investment-followup.sqlite3", at=NOW)
    assert ctx["choices"][0]["plan"]["kind"] == "ANALYSIS"
    assert "personal-a" not in json.dumps(ctx)
    changed = supplied()
    changed["plan"]["question"] = "다른 질문"
    with pytest.raises(ValueError):
        register(store, tmp_path, changed)


def test_time_request_waits_without_model_then_delivers_unchanged_once(store, tmp_path):
    c = register(store, tmp_path)["choice"]
    calls = []

    def model(prompt):
        calls.append(json.loads(prompt.split("\nINPUT\n")[1]))
        return unchanged(prompt)

    assert run(store, tmp_path, model=model)["reviewed"] == [] and not calls
    at = NOW + timedelta(minutes=5)
    result = run(store, tmp_path, at=at, model=model)
    assert len(calls) == 1 and len(result["reviewed"]) == 1
    assert calls[0]["trigger"]["scheduled_at"] == at.isoformat()
    assert calls[0]["choice"]["raw_text"] == supplied()["raw_text"]
    assert store.pending(at=at)[0]["assessment"]["notify"] is True
    bodies = []
    assert (
        deliver_pending(store, at=at, sender=lambda b: bodies.append(b) or "fake:123")[
            0
        ]["status"]
        == "SENT"
    )
    assert "요청한 계획 점검\n" in bodies[0]
    assert (
        run(store, tmp_path, at=at + timedelta(minutes=5), model=model)["reviewed"]
        == []
    )
    assert len(calls) == 1 and not deliver_pending(
        store, at=at, sender=lambda _: pytest.fail("resend")
    )
    assert c["id"] in {c["id"] for c in store.choices(at=at)}


def test_failure_is_not_retried_each_tick_but_explicit_attempt_can_retry(
    store, tmp_path
):
    register(store, tmp_path)
    calls = []
    at = NOW + timedelta(minutes=5)

    def failed(prompt):
        calls.append(prompt)
        raise ValueError("invalid analysis")

    assert run(store, tmp_path, at=at, model=failed)["errors"]
    assert run(store, tmp_path, at=at + timedelta(minutes=5), model=failed)["errors"]
    assert len(calls) == 1
    assert run(store, tmp_path, at=at + timedelta(minutes=6), attempt="investigated-2")[
        "reviewed"
    ]


def test_cancel_closes_pending_monitor_and_context_without_reviving_previous(
    store, tmp_path
):
    old = register(store, tmp_path)["choice"]
    revised = supplied()
    revised["interpretation"]["supersedes"] = old["id"]
    revised["plan"]["question"] = "조건을 바꿔서 다시 봐줘"
    new = register(store, tmp_path, revised, "chat:124")["choice"]
    at = NOW + timedelta(minutes=5)
    run(store, tmp_path, at=at)
    assert store.pending(at=at)
    assert (
        cancel_plan(
            store, new["id"], at=at, reason="사용자가 취소", project_root=tmp_path
        )["status"]
        == "CANCELLED"
    )
    assert store.choices(at=at) == [] and store.pending(at=at) == []
    assert read_user_choices(tmp_path, at=at) == []
    assert (
        read_news_context(tmp_path / "private/investment-followup.sqlite3", at=at)[
            "choices"
        ]
        == []
    )
    assert len(store.choices(at=at, active=False)) == 2
    register(store, tmp_path, revised, "chat:124")
    assert store.choices(at=at) == []  # Replaying a source must not rearm it.


def test_long_term_and_tactical_plans_coexist_and_schedule_does_not_stop_monitor(
    store, tmp_path
):
    long = supplied(kind="DECISION", monitor=True)
    long["interpretation"].update(action="HOLD", phase="DECIDED")
    a = register(store, tmp_path, long)["choice"]
    b = register(store, tmp_path, supplied(), "chat:tactical")["choice"]
    assert len(store.choices(at=NOW)) == 2
    at = NOW + timedelta(minutes=5)
    run(store, tmp_path, at=at)
    seen = []

    def direct(plan):
        assert plan["model"] == "codex:gpt-6-astra"
        return lambda p: (
            seen.append(json.loads(p.split("\nINPUT\n")[1])) or unchanged(p)
        )

    result = run_tick(
        store,
        news_path=tmp_path / "stores/news_episodes.sqlite3",
        data_root=tmp_path,
        snapshot=None,
        at=at + timedelta(hours=7),
        model=lambda _: pytest.fail("wrong model"),
        model_for_plan=direct,
    )
    assert len(seen) == 1 and seen[0]["choice"]["id"] == a["id"]
    assert b["id"] in {c["id"] for c in store.choices(at=at)}


def test_current_quant_calculation_is_in_scheduled_packet(store, tmp_path):
    data = supplied(
        analysis_requests=[
            dict(
                key="KR:000001",
                account="personal-a",
                scope="personal",
                quantity="100",
                entry_price="1000",
                exit_price="1100",
                side="NEW_BUY",
                currency="KRW",
            )
        ]
    )
    register(store, tmp_path, data)
    snapshot = parse_snapshot(json.loads(BASE.read_text()))

    def model(prompt):
        packet = json.loads(prompt.split("\nINPUT\n")[1])
        q = packet["quant"]["KR:000001"]
        assert q["holdings"] and q["options"] and q["current_user_choices"] is not None
        assert (
            q["hypothesis"]["quantity"] == "100"
            and q["hypothesis"]["gross_profit"] == "10000"
        )
        assert q["hypothesis"]["execution_eligible"] is False
        return unchanged(prompt)

    result = run(
        store, tmp_path, at=NOW + timedelta(minutes=5), model=model, snapshot=snapshot
    )
    assert result["reviewed"] and not result["errors"]


def test_price_and_time_in_same_tick_one_review_and_both_complete(store, tmp_path):
    cp = dict(
        key="KR:000001",
        name="합성 종목",
        direction=">=",
        currency="KRW",
        threshold="100",
        starts_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(days=1)).isoformat(),
        question="조건 도달 때 점검",
        recipient="TOKMO_GROUP",
    )
    data = supplied(price=cp, review_at=NOW.isoformat())
    register(store, tmp_path, data)
    market(tmp_path, at=NOW)
    quote(tmp_path, "101", at=NOW)
    seen = []
    result = run(store, tmp_path, model=lambda p: seen.append(p) or unchanged(p))
    assert not result["errors"] and len(seen) == 1
    assert set(result["plans"][0]["done"]) == {"PRICE", "TIME"}
    assert (
        run(store, tmp_path, model=lambda p: pytest.fail("duplicate"))["reviewed"] == []
    )


def test_stale_price_never_triggers_but_requested_time_still_reports_gap(
    store, tmp_path
):
    cp = dict(
        key="KR:000001",
        name="합성 종목",
        direction="<=",
        currency="KRW",
        threshold="100",
        starts_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(days=1)).isoformat(),
        question="점검",
        recipient="TOKMO_GROUP",
    )
    register(store, tmp_path, supplied(price=cp))
    market(tmp_path, at=NOW)
    quote(tmp_path, "90", at=NOW - timedelta(hours=1))
    result = run(store, tmp_path, at=NOW + timedelta(minutes=5))
    assert result["plans"][0]["done"] == ["TIME"]
    packet = store.latest_review(
        result["plans"][0]["choice_id"], at=NOW + timedelta(minutes=5)
    )["input"]
    assert packet["trigger"]["observation"] is None and packet["quant"] == {}


def test_partial_playbook_failure_is_visible_and_repeat_repairs_it(
    store, tmp_path, monkeypatch
):
    import tokmo.programs.investment_plans as plans

    actual = plans.sync_playbooks
    monkeypatch.setattr(
        plans, "sync_playbooks", lambda *a, **kw: (_ for _ in ()).throw(OSError())
    )
    result = register(store, tmp_path)
    assert result["status"] == "PLAYBOOK_PENDING" and len(store.choices(at=NOW)) == 1
    monkeypatch.setattr(plans, "sync_playbooks", actual)
    assert register(store, tmp_path)["status"] == "REGISTERED"
    assert len(store.choices(at=NOW)) == 1


def test_analysis_request_cannot_be_registered_as_buy(store, tmp_path):
    data = supplied()
    data["interpretation"].update(action="BUY", phase="DECIDED")
    with pytest.raises(ValueError):
        register(store, tmp_path, data)
    assert store.choices(at=NOW) == []


def test_fluctuating_price_cannot_rearm_a_failed_time_request(store, tmp_path):
    cp = dict(
        key="KR:000001",
        name="합성 종목",
        direction=">=",
        currency="KRW",
        threshold="100",
        starts_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(days=1)).isoformat(),
        question="점검",
        recipient="TOKMO_GROUP",
    )
    register(store, tmp_path, supplied(price=cp, review_at=NOW.isoformat()))
    calls = []

    def failed(p):
        calls.append(p)
        raise ValueError("invalid analysis")

    market(tmp_path, at=NOW)
    for n, price in enumerate(["90", "110", "90", "110"]):
        at = NOW + timedelta(minutes=n)
        quote(tmp_path, price, at=at)
        assert run(store, tmp_path, at=at, model=failed)["errors"]
    assert len(calls) == 1


def test_automatic_no_change_cannot_cancel_explicit_requested_result(store, tmp_path):
    c = register(store, tmp_path)["choice"]
    at = NOW + timedelta(minutes=5)
    run(store, tmp_path, at=at)
    review_choice(
        store,
        c,
        [],
        snapshot=None,
        at=at + timedelta(minutes=1),
        trigger={"reason": "PERIODIC"},
        model=lambda p: dict(unchanged(p), pending_action="CANCEL"),
    )
    assert len(store.pending(at=at + timedelta(minutes=1))) == 1


@pytest.mark.parametrize(
    "entry_name", ["run_investment_followup.py", "run_investment_followup_cron.py"]
)
def test_followup_entry_uses_installed_model_contract_not_a_bypass(
    monkeypatch, tmp_path, entry_name
):
    import quant_decision_models as models
    from test_quant_repeat_latency import installed_adapter

    _, _, approval, manifest, _, _ = installed_adapter(monkeypatch, tmp_path)
    entry = str(Path(manifest["runtime_root"]) / "tools" / entry_name)
    monkeypatch.setattr(sys, "argv", [entry])
    monkeypatch.setattr(sys.modules["__main__"], "__file__", entry)
    assert models.require_product_scope()["entrypoint"] == entry
    changed = json.loads(approval.read_text())
    changed["scope"].pop("scheduled_plans")
    approval.write_text(json.dumps(changed))
    with pytest.raises(PermissionError):
        models.require_product_scope()


def test_scheduled_review_reads_broad_shock_then_avoids_duplicate_match_review(
    store, tmp_path
):
    from test_followup_bundling import news, router

    data = supplied(monitor=True, review_at=NOW.isoformat())
    c = register(store, tmp_path, data)["choice"]
    path = tmp_path / "stores/news_episodes.sqlite3"
    path.parent.mkdir()
    news(path, "global-risk", NOW, "금융시장 전반의 자금 조달 위축이 확인됐다")
    seen = []

    def model(prompt):
        packet = json.loads(prompt.split("\nINPUT\n")[1])
        seen.append(packet)
        assert any(e["id"] == "global-risk" for e in packet["evidence"])
        return unchanged(prompt)

    result = run_tick(
        store,
        news_path=path,
        data_root=tmp_path,
        snapshot=None,
        at=NOW,
        model=lambda p: pytest.fail("wrong model"),
        model_for_plan=lambda p: model,
        screen_model=router("REVIEW"),
    )
    assert len(seen) == 1 and len(result["reviewed"]) == 1 and not result["errors"]
    assert result["remaining"] == 0
    assert store.watermark("news_versions")["global-risk"]


def test_schedule_news_packet_is_bounded_and_does_not_claim_full_source_read(
    store, tmp_path
):
    from test_followup_bundling import news

    register(store, tmp_path, supplied(review_at=NOW.isoformat()))
    path = tmp_path / "stores/news_episodes.sqlite3"
    path.parent.mkdir()
    for n in range(20):
        news(path, str(n), NOW, "시장 변화 " * 1000)

    def model(prompt):
        packet = json.loads(prompt.split("\nINPUT\n")[1])
        coverage = packet["trigger"]["news_coverage"]
        assert coverage["events_available"] == 20 and coverage["complete"] is False
        assert sum(len(e["text"]) for e in packet["evidence"]) <= 24000
        return unchanged(prompt)

    assert run(store, tmp_path, model=model)["reviewed"]


@pytest.mark.parametrize("selection", ["codex:gpt-6-astra", "astra"])
def test_cli_registration_no_model_then_common_tick_routes_selected_model(
    tmp_path, monkeypatch, capsys, selection
):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import run_investment_followup as cli

    config = tmp_path / "plan.json"
    config.write_text(json.dumps(supplied(model=selection)))
    args = [
        "--state",
        str(tmp_path / "private/investment-followup.sqlite3"),
        "--project-root",
        str(tmp_path),
        "--data-root",
        str(tmp_path),
        "--at",
        NOW.isoformat(),
    ]
    monkeypatch.setattr(cli, "QuantModel", lambda **kw: pytest.fail("model at intake"))
    assert (
        cli.main(
            [
                "register-plan",
                *args,
                "--plan-file",
                str(config),
                "--source-id",
                "chat:1",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "REGISTERED"
    import quant_decision_models

    routing = quant_decision_models.ROUTING_PATH
    routing.write_text(routing.read_text().replace("gpt-6-astra", "synthetic-astra-99"))
    calls = []

    def factory(**kw):
        def model(prompt):
            calls.append(kw)
            return unchanged(prompt)

        return model

    monkeypatch.setattr(cli, "QuantModel", factory)
    monkeypatch.setattr(
        cli, "load_local_snapshot", lambda *a, **kw: json.loads(BASE.read_text())
    )
    args[-1] = (NOW + timedelta(minutes=5)).isoformat()
    assert cli.main(["tick", *args]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["reviewed"] and len(calls) == 1
    assert calls[0]["model"] == "codex:gpt-6-astra" and calls[0]["on_demand"] is True
    with pytest.raises(SystemExit):
        cli.main(["tick", *args, "--send"])
