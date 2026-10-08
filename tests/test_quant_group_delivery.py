"""Narrow TDD for the Quant → tokmo-group delivery vertical (contract §8.5/§9.3):
explicit-chat gate, cycle budget (1 + 2 + optional RCA, cap 3), one-attempt
settlement reuse, UNKNOWN no-blind-replay, visible binding markers + durable
binding receipts, reply resolution to exact report/market/symbol, and the
no-secret / no-provider / no-order boundary."""

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import stat

import pytest

from tokmo.programs.quant_group_delivery import (
    MARKER_RE, NO_WRITES, ReplyBindingV1, bind_markers, budget_status,
    resolve_group_reply, run_quant_group_delivery,
)
from tokmo.programs.quant_telegram_card import MESSAGE_CHAR_CAP, MarketMessageV1
from tokmo.records.quant_group_delivery_receipts import (
    CYCLE_PURPOSES, GROUP_GATE_KEYS, LIVE_SEND_CAP, TOKMO_GROUP_CHAT_ID,
    GroupBindingStore, activation_id_for, read_group_activation,
    write_group_activation,
)
from tokmo.records.quant_telegram_card_receipts import QuantTelegramCardReceiptStore

NOW = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)
PROJECT = Path(__file__).parents[1]
TOOL = PROJECT / "tools" / "run_quant_group_delivery.py"
CARD_TOOL = PROJECT / "tools" / "run_quant_telegram_card.py"
SRC = PROJECT / "src" / "tokmo"
VID = "quant-group-20260903"
GROUP = TOKMO_GROUP_CHAT_ID
CARDS = {"KR": (("000001", "테스트전자"), ("000002", "평온산업")), "US": (("NVDA", "엔비디아"),)}


def _tool():
    spec = importlib.util.spec_from_file_location("run_quant_group_delivery", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _messages(kr_digest="d" * 64, us_digest="e" * 64):
    kr = ("📊 퀀트 KR\n2026-09-02\n카드 2종목\n\n• 테스트전자 (000001)\n\n행동 제안\n• 신규 매수: 대기\n\n• 평온산업 (000002)\n본문")
    us = "📊 퀀트 US\n2026-09-02\n카드 1종목\n\n• 엔비디아 (NVDA)\n본문"
    return (MarketMessageV1("KR", "2026-09-02", "a" * 64, kr_digest, (kr,), (), ()),
            MarketMessageV1("US", "2026-09-02", "b" * 64, us_digest, (us,), (), ()))


def _live(cycle, *, chat_id=GROUP, rca=None, same_report_ok=False, vertical_id=VID):
    return {"mode": "LIVE", "vertical_id": vertical_id, "cycle": cycle, "purpose": CYCLE_PURPOSES[cycle],
            "chat_id": chat_id, "activation_id": activation_id_for(vertical_id, cycle),
            "expires_at": (NOW + timedelta(minutes=30)).isoformat(), "rca_defect_ref": rca,
            "same_report_ok": same_report_ok, "reason": None}


def _stores(tmp_path):
    return (QuantTelegramCardReceiptStore(tmp_path / "group" / "receipts"),
            GroupBindingStore(tmp_path / "group" / "bindings"))


def _run(tmp_path, activation, send, *, chat_id=GROUP, messages=None, stores=None):
    receipts, bindings = stores or _stores(tmp_path)
    return run_quant_group_delivery(activation=activation, chat_id=chat_id, receipts=receipts,
                                    bindings=bindings, messages=messages or _messages(),
                                    cards_by_market=CARDS, telegram_send=send, now=NOW)


# --- gate ---------------------------------------------------------------------

def test_group_gate_is_off_unless_exact_unexpired_live_cycle_for_explicit_chat(tmp_path):
    gate = tmp_path / "gate.json"
    assert read_group_activation(gate, now=NOW)["reason"] == "gate_absent"
    gate.write_text("{bad")
    assert read_group_activation(gate, now=NOW)["reason"] == "gate_invalid"
    write_group_activation(gate, vertical_id=VID, cycle=1, chat_id=GROUP, mode="LIVE",
                           expires_at=NOW + timedelta(minutes=30))
    assert (gate.stat().st_mode & 0o777) == 0o600
    live = read_group_activation(gate, now=NOW)
    assert live["mode"] == "LIVE" and live["activation_id"] == f"{VID}-c1"
    assert live["chat_id"] == GROUP and live["cycle"] == 1 and live["purpose"] == CYCLE_PURPOSES[1]
    assert live["same_report_ok"] is False and live["rca_defect_ref"] is None
    assert read_group_activation(gate, now=NOW + timedelta(minutes=31))["reason"] == "gate_expired"
    write_group_activation(gate, vertical_id=VID, cycle=1, chat_id=GROUP, mode="OFF",
                           expires_at=NOW + timedelta(days=1))
    assert read_group_activation(gate, now=NOW)["reason"] == "gate_off"
    # exact key set; no recurring/schedule vocabulary can be smuggled in
    payload = json.loads(gate.read_bytes()); payload["schedule"] = "*/5 * * * *"
    gate.write_text(json.dumps(payload))
    assert read_group_activation(gate, now=NOW)["reason"] == "gate_shape"
    assert set(payload) - {"schedule"} == GROUP_GATE_KEYS
    # purpose must match the cycle; cycle is bounded by the cap; chat id must be numeric
    base = {k: v for k, v in payload.items() if k != "schedule"}
    for mutation, reason in ((dict(purpose=CYCLE_PURPOSES[2]), "gate_purpose"),
                             (dict(cycle=LIVE_SEND_CAP + 1), "gate_cycle"),
                             (dict(cycle=0), "gate_cycle"),
                             (dict(chat_id="tokmo group"), "gate_chat_id"),
                             (dict(cycle=3, purpose=CYCLE_PURPOSES[3]), "gate_rca_defect_ref"),
                             (dict(rca_defect_ref="DEF-1"), "gate_rca_defect_ref"),
                             (dict(same_report_ok="yes"), "gate_shape"),
                             (dict(mode="RECURRING"), "gate_shape")):
        gate.write_text(json.dumps({**base, "mode": "LIVE", **mutation}))
        assert read_group_activation(gate, now=NOW)["reason"] == reason, mutation
    with pytest.raises(ValueError):
        write_group_activation(gate, vertical_id=VID, cycle=3, chat_id=GROUP, mode="LIVE",
                               expires_at=NOW + timedelta(minutes=5))  # RCA needs a documented defect
    write_group_activation(gate, vertical_id=VID, cycle=3, chat_id=GROUP, mode="LIVE",
                           expires_at=NOW + timedelta(minutes=5), rca_defect_ref="QC-DEF-20260903-01")
    assert read_group_activation(gate, now=NOW)["rca_defect_ref"] == "QC-DEF-20260903-01"


def test_wrong_chat_is_refused_before_any_transport_or_receipt(tmp_path):
    calls = []
    receipts, bindings = _stores(tmp_path)
    result = _run(tmp_path, _live(1), lambda text: calls.append(text) or 1, chat_id="-100999",
                  stores=(receipts, bindings))
    assert result["off"] == "chat_mismatch" and calls == []
    assert receipts.read(f"{VID}-c1") is None and bindings.read(f"{VID}-c1") is None
    # a gate armed for another chat never delivers to the tokmo group either
    result = _run(tmp_path, _live(1, chat_id="-100999"), lambda text: calls.append(text) or 1,
                  stores=(receipts, bindings))
    assert result["off"] == "chat_mismatch" and calls == []
    off = _run(tmp_path, {"mode": "OFF", "reason": "gate_expired", "chat_id": GROUP},
               lambda text: calls.append(text) or 1, stores=(receipts, bindings))
    assert off["off"] == "gate_expired" and calls == []


# --- cycle 1: render / receive with visible binding markers -------------------

def test_cycle1_writes_binding_before_transport_and_marks_every_part(tmp_path):
    receipts, bindings = _stores(tmp_path)
    sent = []

    def send(text):
        binding = bindings.read(f"{VID}-c1")
        assert binding is not None and receipts.read(f"{VID}-c1")["status"] == "RESERVED"
        sent.append(text)
        return 900 + len(sent)
    result = _run(tmp_path, _live(1), send, stores=(receipts, bindings))
    assert result["off"] is None and result["sent"] == 2 and result["activation_id"] == f"{VID}-c1"
    assert result["cycle"] == 1 and result["chat_id"] == GROUP
    kr_marker, us_marker = (MARKER_RE.search(text).group(0) for text in sent)
    assert kr_marker == f"QC1-KR-{'d' * 12}/1" and us_marker == f"QC1-US-{'e' * 12}/1"
    assert sent[0].endswith("\n\n🔖 " + kr_marker) and sent[0].startswith("📊 퀀트 KR\n")
    assert result["markers"] == {"KR:1": kr_marker, "US:1": us_marker}
    binding = bindings.read(f"{VID}-c1")
    assert binding["chat_id"] == GROUP and binding["cycle"] == 1 and binding["vertical_id"] == VID
    assert binding["parts"]["KR:1"] == {"marker": kr_marker, "market": "KR", "report_sha256": "d" * 64,
                                        "completed_session_date": "2026-09-02",
                                        "symbols": [["000001", "테스트전자"], ["000002", "평온산업"]]}
    assert binding["parts"]["US:1"]["symbols"] == [["NVDA", "엔비디아"]]
    receipt = receipts.read(f"{VID}-c1")
    assert receipt["status"] == "SENT"
    assert [(p["key"], p["provider_message_id"]) for p in receipt["parts"]] == [("KR:1", "901"), ("US:1", "902")]
    assert (bindings.root.stat().st_mode & 0o777) == 0o700
    assert (bindings.path(f"{VID}-c1").stat().st_mode & 0o777) == 0o600


def test_marker_overflow_fails_closed_before_transport(tmp_path):
    calls = []
    huge = MarketMessageV1("KR", "2026-09-02", "a" * 64, "d" * 64, ("x" * MESSAGE_CHAR_CAP,), (), ())
    with pytest.raises(ValueError, match="marker_overflow"):
        bind_markers((huge,), CARDS, cycle=1)
    receipts, bindings = _stores(tmp_path)
    result = _run(tmp_path, _live(1), lambda text: calls.append(text) or 1, messages=(huge,),
                  stores=(receipts, bindings))
    assert result["off"] == "marker_overflow" and calls == []
    assert receipts.read(f"{VID}-c1") is None and bindings.read(f"{VID}-c1") is None


# --- budget: duplicate cycle, sequencing, cap, optional RCA -------------------

def test_duplicate_cycle_sends_nothing_and_cycle_identities_are_distinct(tmp_path):
    receipts, bindings = _stores(tmp_path)
    calls = []
    send = lambda text: calls.append(text) or 1
    first = _run(tmp_path, _live(1), send, stores=(receipts, bindings))
    assert first["sent"] == 2
    again = _run(tmp_path, _live(1), send, stores=(receipts, bindings))
    assert again["off"] == "cycle_consumed" and len(calls) == 2
    varied = _messages(kr_digest="f" * 64, us_digest="0" * 64)
    second = _run(tmp_path, _live(2), send, messages=varied, stores=(receipts, bindings))
    assert second["sent"] == 2 and second["activation_id"] == f"{VID}-c2"
    assert MARKER_RE.search(calls[2]).group(0) == f"QC2-KR-{'f' * 12}/1"
    assert {p.name for p in receipts.root.glob("*.receipt.json")} == {f"{VID}-c1.receipt.json", f"{VID}-c2.receipt.json"}
    assert sorted(bindings.root.glob("*.binding.json")) == [bindings.path(f"{VID}-c1"), bindings.path(f"{VID}-c2")]


def test_cycle_order_cap_and_optional_rca_are_enforced(tmp_path):
    receipts, bindings = _stores(tmp_path)
    calls = []
    send = lambda text: calls.append(text) or 1
    assert _run(tmp_path, _live(2), send, stores=(receipts, bindings))["off"] == "cycle1_not_attempted"
    assert _run(tmp_path, _live(3, rca="DEF-1"), send, stores=(receipts, bindings))["off"] == "planned_cycles_incomplete"
    assert calls == []
    assert _run(tmp_path, _live(1), send, stores=(receipts, bindings))["sent"] == 2
    status = budget_status(receipts, bindings, vertical_id=VID)
    assert status == {"vertical_id": VID, "cap": LIVE_SEND_CAP, "consumed": 1, "remaining": 2,
                      "cycles": {"1": "SENT", "2": None, "3": None},
                      "planned_complete": False, "rca_reserve": "available", "next_cycle": 2}
    assert _run(tmp_path, _live(2), send, messages=_messages("f" * 64, "0" * 64),
                stores=(receipts, bindings))["sent"] == 2
    status = budget_status(receipts, bindings, vertical_id=VID)
    # planned cycles done; the RCA reserve is NOT consumed merely to reach the cap
    assert status["planned_complete"] is True and status["rca_reserve"] == "unused" and status["next_cycle"] is None
    assert status["consumed"] == 2 and status["remaining"] == 1
    # RCA only with a documented defect reference
    rca_without = dict(_live(3), rca_defect_ref=None)
    assert _run(tmp_path, rca_without, send, stores=(receipts, bindings))["off"] == "rca_defect_missing"
    assert len(calls) == 4
    rca = _run(tmp_path, _live(3, rca="QC-DEF-20260903-01"), send, messages=_messages("1" * 64, "2" * 64),
               stores=(receipts, bindings))
    assert rca["sent"] == 2 and rca["rca_defect_ref"] == "QC-DEF-20260903-01"
    assert bindings.read(f"{VID}-c3")["rca_defect_ref"] == "QC-DEF-20260903-01"
    status = budget_status(receipts, bindings, vertical_id=VID)
    assert status["consumed"] == 3 and status["remaining"] == 0 and status["rca_reserve"] == "consumed"
    # cap exhausted: nothing else can be armed or sent under this vertical
    for cycle in (1, 2, 3):
        assert _run(tmp_path, _live(cycle, rca="x" if cycle == 3 else None), send,
                    stores=(receipts, bindings))["off"] in ("cycle_consumed", "cap_exhausted")
    forged = dict(_live(1), cycle=4, purpose="extra", activation_id=f"{VID}-c4")
    assert _run(tmp_path, forged, send, stores=(receipts, bindings))["off"] == "cap_exhausted"
    assert len(calls) == 6 and receipts.read(f"{VID}-c4") is None


# --- UNKNOWN: never blindly replayed --------------------------------------------

def test_unknown_part_is_never_replayed_even_when_same_report_is_allowed(tmp_path):
    receipts, bindings = _stores(tmp_path)
    starts = []

    def send(text):
        starts.append(text)
        if text.startswith("📊 퀀트 KR"):
            raise TimeoutError
        return 77
    first = _run(tmp_path, _live(1), send, stores=(receipts, bindings))
    assert first["unknown"] == 1 and first["sent"] == 1
    assert receipts.read(f"{VID}-c1")["parts"][0]["status"] == "UNKNOWN"
    # cycle 2 on the same reports: refused by report dedupe unless the gate allows same report
    second = _run(tmp_path, _live(2), send, stores=(receipts, bindings))
    assert second["off"] == "report_already_attempted" and len(starts) == 2
    assert second["deduped"] == {"KR": f"{VID}-c1:UNKNOWN", "US": f"{VID}-c1:SENT"}
    assert receipts.read(f"{VID}-c2") is None
    allowed = _run(tmp_path, _live(2, same_report_ok=True), send, stores=(receipts, bindings))
    # SENT US may be re-rendered for cycle 2; UNKNOWN KR is permanent
    assert allowed["deduped"] == {"KR": f"{VID}-c1:UNKNOWN"} and starts[2:] == [starts[1].replace("QC1", "QC2")]
    assert [p["key"] for p in receipts.read(f"{VID}-c2")["parts"]] == ["US:1"]
    assert bindings.read(f"{VID}-c2")["parts"].keys() == {"US:1"}


# --- reply resolution: exact report/market then symbol ------------------------

def _delivered(tmp_path):
    receipts, bindings = _stores(tmp_path)
    ids = iter((501, 502))
    result = _run(tmp_path, _live(1), lambda text: next(ids), stores=(receipts, bindings))
    assert result["sent"] == 2
    return receipts, bindings


def test_reply_to_bot_message_resolves_exact_report_market_then_symbol(tmp_path):
    receipts, bindings = _delivered(tmp_path)
    kwargs = dict(receipts=receipts, bindings=bindings, chat_id=GROUP)
    bound = resolve_group_reply(reply_to_message_id=501, text="테스트전자 해석 맞아", **kwargs)
    assert isinstance(bound, ReplyBindingV1) and bound.status == "bound"
    assert (bound.activation_id, bound.cycle, bound.market, bound.report_sha256, bound.part_key) == (
        f"{VID}-c1", 1, "KR", "d" * 64, "KR:1")
    assert (bound.symbol, bound.name, bound.completed_session_date) == ("000001", "테스트전자", "2026-09-02")
    assert bound.writes == NO_WRITES and bound.processed is False
    # a one-symbol part binds the symbol without any mention
    only = resolve_group_reply(reply_to_message_id=502, text="이 해석은 보수적이야", **kwargs)
    assert only.status == "bound" and (only.market, only.symbol) == ("US", "NVDA")
    # a multi-symbol part needs a discriminator; two mentions are ambiguous
    missing = resolve_group_reply(reply_to_message_id=501, text="플레이북은 좀 다르게", **kwargs)
    assert missing.status == "symbol_missing" and missing.market == "KR" and missing.symbol is None
    assert missing.candidates == (("000001", "테스트전자"), ("000002", "평온산업"))
    both = resolve_group_reply(reply_to_message_id=501, text="000001 과 평온산업 둘 다", **kwargs)
    assert both.status == "symbol_ambiguous" and both.candidates == (("000001", "테스트전자"), ("000002", "평온산업"))
    # symbol code mention binds too
    code = resolve_group_reply(reply_to_message_id=501, text="000002 는 관심 종목에서 빼줘", **kwargs)
    assert code.status == "bound" and code.symbol == "000002"


def test_copied_marker_wrong_chat_and_unknown_message_fail_closed(tmp_path):
    receipts, bindings = _delivered(tmp_path)
    kwargs = dict(receipts=receipts, bindings=bindings)
    copied = resolve_group_reply(chat_id=GROUP, reply_to_message_id=None,
                                 text=f"🔖 QC1-KR-{'d' * 12}/1\n평온산업 해석 틀렸어", **kwargs)
    assert copied.status == "bound" and (copied.market, copied.symbol, copied.part_key) == ("KR", "000002", "KR:1")
    assert resolve_group_reply(chat_id="-100999", reply_to_message_id=501, text="x", **kwargs).status == "wrong_chat"
    assert resolve_group_reply(chat_id=GROUP, reply_to_message_id=999, text="x", **kwargs).status == "unbound"
    assert resolve_group_reply(chat_id=GROUP, reply_to_message_id=None, text="테스트전자 해석 맞아", **kwargs).status == "unbound"
    stale = resolve_group_reply(chat_id=GROUP, reply_to_message_id=None, text=f"QC1-KR-{'9' * 12}/1", **kwargs)
    assert stale.status == "unbound"
    # a reply to a message whose transport ended UNKNOWN still resolves (the user saw it) but says so
    unknown_receipts = QuantTelegramCardReceiptStore(tmp_path / "u" / "receipts")
    unknown_bindings = GroupBindingStore(tmp_path / "u" / "bindings")

    def send(text):
        raise TimeoutError
    _run(tmp_path, _live(1), send, stores=(unknown_receipts, unknown_bindings))
    via_marker = resolve_group_reply(receipts=unknown_receipts, bindings=unknown_bindings, chat_id=GROUP,
                                     reply_to_message_id=None, text=f"QC1-US-{'e' * 12}/1 맞아")
    assert via_marker.status == "bound" and via_marker.part_status == "UNKNOWN" and via_marker.symbol == "NVDA"


# --- CLI: identity file, explicit chat, disarm after attempt, no secret leak ----

# A stand-in for the existing ~/.hermes/.env: many unrelated keys and comments around the one we use.
HERMES_ENV = ("# Hermes gateway env\nOPENAI_API_KEY=other-secret-1\n\nexport GLM_API_KEY=other-secret-2\n"
              "TELEGRAM_BOT_TOKEN=sekrit-token\nTELEGRAM_ALLOWED_USERS=<owner-chat-id>\nDISCORD_BOT_TOKEN=other-secret-3\n"
              "malformed line without equals\n")


def _identity(tmp_path, body=HERMES_ENV, mode=0o600):
    path = tmp_path / ".env"
    if path.exists():
        os.chmod(path, 0o600)  # a prior 0400 case must not block the rewrite
    path.write_text(body)
    os.chmod(path, mode)
    return path


def test_identity_reuses_existing_hermes_env_reads_only_the_bot_token_and_fails_closed(tmp_path):
    tool = _tool()
    assert tool.DEFAULT_IDENTITY_PATH == Path.home() / ".hermes" / ".env"
    assert tool.load_group_identity(_identity(tmp_path), uid=os.getuid()) == "sekrit-token"
    assert tool.load_group_identity(_identity(tmp_path, "export TELEGRAM_BOT_TOKEN = 'quoted:tok' \nX=1\n"),
                                    uid=os.getuid()) == "quoted:tok"
    for body in ("", "# only comments\n", "TELEGRAM_CHAT_ID=-1\nOTHER=1\n", "TELEGRAM_BOT_TOKEN=\n",
                 "TELEGRAM_BOT_TOKEN=''\n", "TELEGRAM_BOT_TOKEN=t\nX=1\nTELEGRAM_BOT_TOKEN=u\n"):
        with pytest.raises(ValueError):
            tool.load_group_identity(_identity(tmp_path, body), uid=os.getuid())
    for mode in (0o644, 0o640, 0o604):
        with pytest.raises(ValueError):
            tool.load_group_identity(_identity(tmp_path, mode=mode), uid=os.getuid())
    assert tool.load_group_identity(_identity(tmp_path, mode=0o400), uid=os.getuid()) == "sekrit-token"
    with pytest.raises(ValueError):
        tool.load_group_identity(_identity(tmp_path), uid=os.getuid() + 1)
    with pytest.raises(ValueError):
        tool.load_group_identity(tmp_path / "absent.env", uid=os.getuid())
    # reading never modifies the file
    path = _identity(tmp_path)
    before = (path.read_bytes(), path.stat().st_mode)
    tool.load_group_identity(path, uid=os.getuid())
    assert (path.read_bytes(), path.stat().st_mode) == before


def _store_report(root, market, cards, seed):
    import test_quant_telegram_card as card_tests  # sibling fixtures (pytest prepends tests/)
    from tokmo.records.quant_reports import QuantReportStore
    store = QuantReportStore(root)
    report = card_tests._report(market, cards, digest_seed=seed)
    digest = store.append_if_absent(report)
    store.finish(report.identity, "SUCCEEDED_WITH_CARDS", report.operation_counts, digest)
    return digest


def test_cli_send_requires_explicit_chat_disarms_gate_and_leaks_no_secret(tmp_path, capsys):
    import test_quant_telegram_card as card_tests
    _card, VALUES_CALM, VALUES_HOT = card_tests._card, card_tests.VALUES_CALM, card_tests.VALUES_HOT
    tool = _tool()
    root = tmp_path / "reports"
    kr_digest = _store_report(root, "KR", (_card("000001", "KR", "테스트전자", VALUES_HOT),), "a")
    us_digest = _store_report(root, "US", (_card("NVDA", "US", "엔비디아", VALUES_CALM),), "b")
    gate = tmp_path / "gate.json"
    identity = _identity(tmp_path)
    home = tmp_path / "group"
    paths = dict(holdings_path=tmp_path / "no_holdings.json", quotes_path=tmp_path / "no_quotes.sqlite3",
                 playbook_root=tmp_path / "playbooks", watchlist_root=tmp_path / "watchlists")
    sent = []

    def factory(token, chat_id):
        assert (token, chat_id) == ("sekrit-token", GROUP)
        return lambda text: sent.append(text) or 660 + len(sent)

    def run(*argv):
        code = tool.main(list(argv), clock=lambda: NOW, sender_factory=factory, gate_path=gate,
                         group_root=home, uid=os.getuid(), **paths)
        return code, json.loads(capsys.readouterr().out)

    code, out = run("arm", "--vertical-id", VID, "--cycle", "1", "--chat-id", GROUP, "--expires-in-minutes", "10")
    assert code == tool.EXIT_OK and out["armed"] is True and out["activation_id"] == f"{VID}-c1"
    assert "sekrit" not in gate.read_text()
    # explicit chat id is mandatory and must match the armed gate
    code, out = run("send", "--vertical-id", VID, "--cycle", "1", "--chat-id", "-100999",
                    "--telegram-env", str(identity), "--report-root", str(root))
    assert code == tool.EXIT_REFUSED and out["off"] == "chat_mismatch" and sent == []
    code, out = run("send", "--vertical-id", VID, "--cycle", "1", "--chat-id", GROUP,
                    "--telegram-env", str(identity), "--report-root", str(root),
                    "--expect-report-sha256", f"KR={'f' * 64}")
    assert code == tool.EXIT_REFUSED and out["off"] == "report_digest_mismatch:KR" and sent == []
    code, out = run("send", "--vertical-id", VID, "--cycle", "1", "--chat-id", GROUP,
                    "--telegram-env", str(identity), "--report-root", str(root),
                    "--expect-report-sha256", f"KR={kr_digest}", "--expect-report-sha256", f"US={us_digest}")
    assert code == tool.EXIT_OK and out["result"]["sent"] == 2 and out["gate_after"] == "OFF"
    # compact header: no group-cycle label, report hash or order-write disclaimer in the visible text
    assert len(sent) == 2 and sent[0].startswith("📊 퀀트 KR\n2026-09-02\n카드 1종목\n\n")
    assert "그룹 사이클" not in sent[0] and "※ 주문" not in sent[0] and kr_digest[:12] not in sent[0].split("🔖")[0]
    assert MARKER_RE.search(sent[0]).group(0) == f"QC1-KR-{kr_digest[:12]}/1"
    assert "NVDA" not in sent[0] and "000001" not in sent[1]
    assert out["result"]["markers"]["KR:1"] == f"QC1-KR-{kr_digest[:12]}/1"
    assert "sekrit" not in json.dumps(out) and "other-secret" not in json.dumps(out)
    for path in list(home.rglob("*.json")) + [gate]:
        assert "sekrit" not in path.read_text() and "other-secret" not in path.read_text()
        assert (path.stat().st_mode & 0o777) == 0o600
    assert not (Path(tmp_path) / "credentials").exists()
    # the gate was disarmed by the attempt: a rerun of the same command sends nothing
    code, out = run("send", "--vertical-id", VID, "--cycle", "1", "--chat-id", GROUP,
                    "--telegram-env", str(identity), "--report-root", str(root))
    assert code == tool.EXIT_REFUSED and out["off"] == "gate_off" and len(sent) == 2
    # re-arming the same cycle is refused by the budget, not by the gate
    code, out = run("arm", "--vertical-id", VID, "--cycle", "1", "--chat-id", GROUP)
    assert code == tool.EXIT_REFUSED and out["reason"] == "cycle_consumed"
    code, out = run("status", "--vertical-id", VID)
    assert out["budget"]["consumed"] == 1 and out["budget"]["next_cycle"] == 2 and out["gate"]["mode"] == "OFF"
    assert out["receipts"][f"{VID}-c1"]["status"] == "SENT"
    # reply resolution CLI: resolution only, explicitly not processed
    code, out = run("resolve-reply", "--chat-id", GROUP, "--reply-to-message-id", "661", "--text", "해석 맞아")
    assert code == tool.EXIT_OK and out["binding"]["status"] == "bound" and out["binding"]["symbol"] == "000001"
    assert out["binding"]["report_sha256"] == kr_digest and out["processed"] is False
    assert out["binding"]["writes"] == dict(NO_WRITES)
    code, out = run("resolve-reply", "--chat-id", "-100999", "--reply-to-message-id", "661", "--text", "x")
    assert code == tool.EXIT_REFUSED and out["binding"]["status"] == "wrong_chat"


def test_arm_refuses_cycle_two_before_cycle_one_and_loose_identity_keeps_gate(tmp_path, capsys):
    tool = _tool()
    gate = tmp_path / "gate.json"
    home = tmp_path / "group"
    code = tool.main(["arm", "--vertical-id", VID, "--cycle", "2", "--chat-id", GROUP], clock=lambda: NOW,
                     gate_path=gate, group_root=home, uid=os.getuid())
    out = json.loads(capsys.readouterr().out)
    assert code == tool.EXIT_REFUSED and out["reason"] == "cycle1_not_attempted" and not gate.exists()
    code = tool.main(["arm", "--vertical-id", VID, "--cycle", "3", "--chat-id", GROUP], clock=lambda: NOW,
                     gate_path=gate, group_root=home, uid=os.getuid())
    assert code == tool.EXIT_REFUSED and json.loads(capsys.readouterr().out)["reason"] == "rca_defect_missing"
    code = tool.main(["arm", "--vertical-id", VID, "--cycle", "1", "--chat-id", GROUP], clock=lambda: NOW,
                     gate_path=gate, group_root=home, uid=os.getuid())
    assert code == tool.EXIT_OK and capsys.readouterr()
    sent = []
    # a TELEGRAM_CHAT_ID in the env file is ignored: the target is bound by gate + --chat-id only
    other_chat = _identity(tmp_path, HERMES_ENV + "TELEGRAM_CHAT_ID=-100999\n")
    code = tool.main(["send", "--vertical-id", VID, "--cycle", "1", "--chat-id", "-100999", "--telegram-env",
                      str(other_chat), "--report-root", str(tmp_path / "none")], clock=lambda: NOW,
                     sender_factory=lambda token, chat_id: (lambda text: sent.append(text) or 1),
                     gate_path=gate, group_root=home, uid=os.getuid())
    out = json.loads(capsys.readouterr().out)
    assert code == tool.EXIT_REFUSED and out["off"] == "chat_mismatch" and sent == []
    loose = _identity(tmp_path, mode=0o644)
    code = tool.main(["send", "--vertical-id", VID, "--cycle", "1", "--chat-id", GROUP, "--telegram-env",
                      str(loose), "--report-root", str(tmp_path / "none")], clock=lambda: NOW,
                     sender_factory=lambda token, chat_id: (lambda text: sent.append(text) or 1),
                     gate_path=gate, group_root=home, uid=os.getuid())
    out = json.loads(capsys.readouterr().out)
    assert code == tool.EXIT_REFUSED and out["off"] == "identity_unbound" and sent == []
    assert read_group_activation(gate, now=NOW)["mode"] == "LIVE"  # refusal before attempt keeps the gate
    assert not (home / "receipts").exists() or not list((home / "receipts").glob("*.json"))


# --- boundary: only the injected sender; no provider/order/account/ledger path --

def test_delivery_modules_have_no_provider_order_ledger_or_secret_path():
    program = SRC / "programs" / "quant_group_delivery.py"
    records = SRC / "records" / "quant_group_delivery_receipts.py"
    for path in (program, records, TOOL):
        text = path.read_text(encoding="utf-8")
        for banned in ("/api/v1", "/oauth2", "toss_live", "toss_reads_live", "toss_auth_live", "conditional-orders",
                       "orders/", "trade_ledger", "TradeLedgerStore", "playbook_identity", "validate_playbooks",
                       "trade_reason_seam", "apply_approved_reason", "ops_kernel", "urllib.request",
                       "subprocess", "launchd", "crontab", "getUpdates", "callback_query", "inline_keyboard",
                       "reply_markup", "os.environ", "getenv(", "TELEGRAM_STOCK_BOT"):
            assert banned not in text, (path.name, banned)
        assert "telegram_dialogue_live" not in text or path == TOOL
        # no new credential path/file: the existing Hermes identity is reused, chat id never read from it
        for banned in ("credentials", "tokmo_group_telegram", "TELEGRAM_CHAT_ID"):
            assert banned not in text, (path.name, banned)
        if path != TOOL:
            for banned in ("TELEGRAM_BOT_TOKEN", "datetime.now(", "Path.home"):
                assert banned not in text, (path.name, banned)
    import ast
    for path in (program, records):
        names = {node.module or "" for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.ImportFrom)}
        names |= {alias.name for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.Import)
                  for alias in node.names}
        assert not any(name.startswith(("tokmo.facts", "tokmo.composition", "urllib", "http", "socket", "ssl"))
                       for name in names), (path.name, names)
    # the one default sender is the card tool's pre-entity sender (monospace table); no direct transport call here
    tool_text = TOOL.read_text(encoding="utf-8")
    assert "send_dialogue_message" not in tool_text and tool_text.count("pre_entity_sender") == 1
