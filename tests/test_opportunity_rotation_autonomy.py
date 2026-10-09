"""Synthetic executor through real slot/adapter; no external model invocation."""

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from test_opportunity_rotation import at, discovery, market, paper, proposal

from tokmo.values.opportunity_rotation import instant

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))


def ready(time="10:05:00"):
    d = discovery()
    d["as_of"] = at(time)
    d["candidates"][0]["quote"]["at"] = at(time)
    d["completed_session"] = "2026-09-23"
    return d


def test_autonomous_slot_calls_gate_and_receipts_once_with_actual_time(tmp_path):
    import opportunity_rotation_model as adapter

    from tokmo.programs.opportunity_rotation_schedule import run_slot

    p = paper(tmp_path)
    calls = []
    ticks = iter(
        [
            instant(at("10:05:10")),
            instant(at("10:05:30")),
            instant(at("10:05:30")),
            instant(at("10:05:30")),
        ]
    )

    class Gate:
        def admit(self, path, model, *, provider):
            calls.append(("gate", str(path), model, provider))
            return {
                "scope": {"identity_synthetic": False},
                "route": {"model": model, "provider": provider},
            }

        def recheck(self, path, facts):
            assert facts["route"] == {
                "model": adapter.MODEL,
                "provider": adapter.PROVIDER,
            }
            assert (
                json.loads(
                    next((tmp_path / "receipts").glob("*CALL.json")).read_text()
                )["admission"]
                == facts
            )
            calls.append(("recheck",))

    def execute(prompt, **kwargs):
        calls.append(("model", kwargs))
        assert list((tmp_path / "receipts").glob("*CALL.json"))
        packet = json.loads(prompt.split("\n", 1)[1])
        assert packet["portfolio"]["cash"] == "2000000"
        contract = packet["decision_contract"]
        assert contract["action_fields"]["ENTER"] == [
            "symbol",
            "action",
            "reason",
            "proposal",
        ]
        assert contract["action_fields"]["WATCH"] == ["symbol", "action", "reason"]
        assert contract["action_fields"]["HOLD"] == ["symbol", "action", "reason"]
        assert contract["proposal_enums"] == dict(
            side=["BUY", "SELL"],
            trigger=["BELOW", "ABOVE"],
            target=["NORMAL", "WEAK", "NONE"],
        )
        assert (
            "assumed_exit_price < price"
            in contract["proposal_meanings"]["assumed_exit_price"]
        )
        assert (
            "effective_at" in contract["timestamps"]
            and "review_at > proposed_at" in contract["timestamps"]
        )
        assert "proposed_at" not in contract["proposal_fields"]
        pr = {
            k: v
            for k, v in proposal(
                time="10:05:00",
                protection={"target_price": "10300", "stop_price": "9500"},
            ).items()
            if k != "proposed_at"
        }
        assert set(pr) == set(contract["proposal_fields"] + contract["buy_required"])
        return json.dumps(
            {
                "cash_reason": "합성 대기",
                "assessments": [
                    {
                        "symbol": "111111",
                        "action": "ENTER",
                        "reason": "합성 판단",
                        "proposal": pr,
                    }
                ],
            }
        ), {"_model": "gpt-6-astra", "_provider": "codex"}

    model = adapter.ProductModel(
        manifest=tmp_path / "missing-synthetic-manifest",
        receipt_root=tmp_path / "receipts",
        gate=Gate(),
        executor=execute,
        synthetic=True,
    )
    result = run_slot(p, ready(), clock=lambda: next(ticks), model=model)
    assert result["status"] == "WAITING_DATA"
    assert p.state()["proposals"] == {}
    p.advance(market(time="10:06:00"))
    assert p.state()["proposals"]["b1"]["proposed_at"] == at("10:06:00")
    assert (
        run_slot(p, ready(), clock=lambda: instant(at("10:06:00")), model=model)[
            "status"
        ]
        == "DUPLICATE"
    )
    assert [c[0] for c in calls] == ["gate", "recheck", "model"]
    assert (
        json.loads(next((tmp_path / "receipts").glob("*RESULT.json")).read_text())[
            "synthetic"
        ]
        is True
    )


def test_uninstalled_gate_and_missing_manifest_fail_before_executor(tmp_path):
    import opportunity_rotation_model as adapter

    calls = []
    m = adapter.ProductModel(
        manifest=tmp_path / "absent",
        receipt_root=tmp_path / "receipts",
        executor=lambda *a, **kw: calls.append("forbidden"),
        synthetic=True,
    )
    with pytest.raises(ValueError, match="GATE_NOT_INSTALLED|MANIFEST"):
        m.decide({"id": "synthetic"}, call_id="test", at=at("10:05:00"))
    assert calls == []


def test_buy_plan_requires_protection_and_pending_review(tmp_path):
    from test_opportunity_rotation import decision, session

    from tokmo.programs.opportunity_rotation import build_packet

    p = paper(tmp_path)
    pkt = build_packet(discovery(), p.state())
    unsafe = decision(pkt)
    unsafe["assessments"][0]["proposal"].pop("protection")
    with pytest.raises(ValueError, match="protection"):
        p.decide(pkt, unsafe, at("09:00:00"))
    p.propose([proposal()], session())
    pkt = build_packet(discovery(), p.state())
    with pytest.raises(ValueError, match="pending"):
        p.decide(pkt, decision(pkt, assessments=[]), at("09:01:30"))


def test_model_packet_drops_unrelated_credentials(tmp_path):
    from tokmo.programs.opportunity_rotation import build_packet

    d = ready()
    d["credential"] = "sensitive-secret"
    d["candidates"][0]["account"] = "private-account"
    packet = build_packet(d, paper(tmp_path).state())
    assert "sensitive-secret" not in json.dumps(packet)
    assert "private-account" not in json.dumps(packet)


@pytest.mark.parametrize(
    "time,status",
    [
        ("10:04:59", "OUTSIDE_SLOT"),
        ("11:00:00", "OUTSIDE_SLOT"),
        ("15:31:00", "CLOSED"),
    ],
)
def test_no_backdated_or_closed_call(tmp_path, time, status):
    from tokmo.programs.opportunity_rotation_schedule import run_slot

    class NoCall:
        def decide(self, *a, **kw):
            raise AssertionError("no call allowed")

    assert (
        run_slot(
            paper(tmp_path),
            ready(time),
            clock=lambda: instant(at(time)),
            model=NoCall(),
        )["status"]
        == status
    )


def test_official_dart_exception_uses_existing_context_shape_once(tmp_path):
    from tokmo.programs.opportunity_rotation_schedule import run_slot

    p = paper(tmp_path)
    d = ready("11:00:00")
    d["candidates"][0]["evidence"] = [
        dict(
            id="local-dart-synthetic",
            kind="disclosure",
            source="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=synthetic",
            available_at=at("10:59:00"),
            metadata=dict(content_extent="body_excerpt", published_date="2026-09-28"),
        )
    ]

    class Model:
        def decide(self, packet, call_id, at):
            return dict(
                id=call_id,
                packet_id=packet["id"],
                author="synthetic",
                verified_by="synthetic",
                verified_at=at,
                cash_reason="중요 공시 재검토",
                assessments=[dict(symbol="111111", action="WATCH", reason="대기")],
            )

    event = dict(
        evidence_id="local-dart-synthetic",
        material=True,
        reason="공식 본문에 기존 가정 변경",
    )
    assert (
        run_slot(
            p,
            d,
            clock=lambda: instant(at("11:00:00")),
            model=Model(),
            exceptional=event,
        )["status"]
        == "WAITING_DATA"
    )
    p.advance(market(time="11:01:00"))
    assert (
        run_slot(
            p,
            d,
            clock=lambda: instant(at("11:01:00")),
            model=Model(),
            exceptional=event,
        )["status"]
        == "DUPLICATE"
    )


def test_cli_step_consumes_captured_facts_and_fails_closed_at_model_slot(tmp_path):
    import os
    import subprocess

    p = paper(tmp_path / "paper")
    bundle = tmp_path / "captured.json"
    bundle.write_text(
        json.dumps({"discovery": ready(), "market": market(time="10:05:00")})
    )
    command = [
        sys.executable,
        str(ROOT / "tools/run_opportunity_rotation.py"),
        "--state-root",
        str(tmp_path / "paper"),
        "step",
        "--captured",
        str(bundle),
        "--at",
        at("10:05:00"),
        "--context-root",
        str(tmp_path / "empty-context"),
        "--manifest",
        str(tmp_path / "absent-manifest"),
    ]
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src")
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2
    output = json.loads(result.stdout)
    assert output["decision"]["status"] == "FAILED"
    assert (
        "GATE_NOT_INSTALLED" in output["decision"]["reason"]
        or "MANIFEST" in output["decision"]["reason"]
    )
    assert p.state()["proposals"] == {}
    again = subprocess.run(
        command,
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src")
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        },
        text=True,
        capture_output=True,
    )
    assert json.loads(again.stdout)["decision"]["status"] == "DUPLICATE"
    assert not list((tmp_path / "paper/receipts").glob("*CALL.json"))


@pytest.mark.parametrize(
    "kind",
    [
        "quota",
        "synthetic_identity",
        "bad_json",
        "bad_provenance",
        "call_receipt_failure",
        "approval_changed",
    ],
)
def test_adapter_refuses_denials_and_never_retries(tmp_path, monkeypatch, kind):
    import opportunity_rotation_model as adapter

    calls = []

    class Gate:
        def admit(self, *a, **kw):
            if kind == "quota":
                raise PermissionError("synthetic exhausted quota")
            return {
                "scope": {"identity_synthetic": kind == "synthetic_identity"},
                "route": {},
            }

        def recheck(self, path, facts):
            assert next((tmp_path / "receipts").glob("*CALL.json")).is_file()
            if kind == "approval_changed":
                raise PermissionError("synthetic approval changed after CALL")

    def executor(*a, **kw):
        calls.append(1)
        return (
            '{"cash_reason":"x","assessments":[],"extra":true}'
            if kind == "bad_json"
            else '{"cash_reason":"x","assessments":[]}'
        ), {
            "_model": "wrong" if kind == "bad_provenance" else adapter.MODEL,
            "_provider": "codex",
        }

    if kind == "call_receipt_failure":
        monkeypatch.setattr(
            adapter,
            "atomic_private",
            lambda *a: (_ for _ in ()).throw(OSError("synthetic disk refusal")),
        )
    m = adapter.ProductModel(
        manifest=tmp_path / "synthetic",
        receipt_root=tmp_path / "receipts",
        gate=Gate(),
        executor=executor,
        synthetic=True,
    )
    with pytest.raises((ValueError, OSError)):
        m.decide({"id": "test"}, call_id="test", at=at("10:05:00"))
    assert len(calls) == (1 if kind in ("bad_json", "bad_provenance") else 0)
    if kind == "approval_changed":
        result = json.loads(
            next((tmp_path / "receipts").glob("*RESULT.json")).read_text()
        )
        assert result["status"] == "FAILED" and result["error"] == "PermissionError"
    if calls:
        with pytest.raises(ValueError, match="ALREADY"):
            m.decide({"id": "test"}, call_id="test", at=at("10:05:01"))
        assert len(calls) == 1


def test_two_regular_slots_have_separate_calls_and_failed_slot_is_not_retried(tmp_path):
    from tokmo.programs.opportunity_rotation_schedule import run_slot

    p = paper(tmp_path)
    calls = []

    class Model:
        def decide(self, packet, call_id, at):
            calls.append(call_id)
            return dict(
                id=call_id,
                packet_id=packet["id"],
                author="synthetic",
                verified_by="synthetic",
                verified_at=at,
                cash_reason="대기",
                assessments=[dict(symbol="111111", action="WATCH", reason="검토")],
            )

    for time in ("10:05:00", "13:30:00"):
        assert (
            run_slot(p, ready(time), clock=lambda: instant(at(time)), model=Model())[
                "status"
            ]
            == "WAITING_DATA"
        )
        p.advance(
            market(time=(instant(at(time)) + timedelta(minutes=1)).strftime("%H:%M:%S"))
        )
    assert calls == ["2026-09-28:10:05", "2026-09-28:13:30"]
    assert (
        run_slot(
            p, ready("13:31:00"), clock=lambda: instant(at("13:31:00")), model=Model()
        )["status"]
        == "DUPLICATE"
    )


def test_afternoon_can_reassess_fresh_reconciled_pending_without_releasing_cash(
    tmp_path,
):
    from test_opportunity_rotation import session

    from tokmo.programs.opportunity_rotation_schedule import run_slot

    p = paper(tmp_path)
    p.propose([proposal(time="13:28:00")], session())
    p.advance(market(time="13:29:00"))
    calls = []

    class Model:
        def decide(self, packet, call_id, at):
            calls.append(packet)
            return dict(
                id=call_id,
                packet_id=packet["id"],
                author="synthetic",
                verified_by="synthetic",
                verified_at=at,
                cash_reason="예약 유지",
                assessments=[
                    dict(symbol="111111", action="WATCH", reason="미체결 유지")
                ],
            )

    result = run_slot(
        p, ready("13:30:00"), clock=lambda: instant(at("13:30:00")), model=Model()
    )
    assert result["status"] == "WAITING_DATA"
    p.advance(market(time="13:31:00"))
    assert p.state()["slots"][result["slot"]]["status"] == "DECIDED"
    assert len(calls) == 1 and p.report()["reserved_cash"] == "100000"
