"""Finite live analyzer + Telegram execution wiring (2026-09-04, one approved
canary): default paths keep zero live effects; ``canary-preview`` makes
exactly one bounded model call per located card (≤4) and zero sends with
the reservation persisted before every call; ``canary-send`` re-renders
from the sanitized receipt with zero new model calls and sends exactly the
two market root messages; replays make zero calls and zero sends; cap,
card failure, digest mismatch/drift and coverage gaps fail closed before
Telegram. No network, no credential, no real store is touched here."""

from datetime import timedelta
import hashlib
import json
import threading
import time

import pytest

import test_quant_telegram_card as card_tests
import test_quant_card_cycle2 as cycle2
from test_quant_telegram_card import NOW, VALUES_CALM, VALUES_HOT, ZONE, _card, _store_report
from tokmo.programs import quant_finite_canary as qfc
from tokmo.programs.quant_contextual_analysis import build_analyzer_request, run_contextual_analysis
from tokmo.programs.quant_telegram_card import ANALYSIS_LABEL, MarketMessageV1
from tokmo.records.quant_telegram_card_receipts import QuantTelegramCardReceiptStore, write_card_activation
from tokmo.values.news_classifier import NewsClassifierFailure

ACTIVATION = "quant-finite-canary-20260904-01"
ENV = {"TELEGRAM_STOCK_BOT_TOKEN": "tok", "TELEGRAM_STOCK_BOT_CHAT_ID": "42"}
KR_CARDS = (_card("000001", "KR", "테스트전자", VALUES_HOT, ZONE), _card("000002", "KR", "평온산업", VALUES_CALM))
US_CARDS = (_card("AAPL", "US", "애플", VALUES_CALM), _card("NVDA", "US", "엔비디아", VALUES_CALM))
ALL_KEYS = ["KR:000001", "KR:000002", "US:AAPL", "US:NVDA"]


def _authority(_request=None):
    # the executor's authority reader returns file stamps WITH private paths;
    # the canary receipt must keep digests/mtime/observed_at only
    return {"routing_state": {"path": "/Users/x/.hermes/MODEL_ROUTING_STATE.md", "sha256": "r" * 64, "mtime_ns": 1},
            "quota_latest": {"path": "/Users/x/.hermes/cron/state/model-quota-latest.json", "sha256": "q" * 64,
                             "mtime_ns": 2, "observed_at": NOW.isoformat()}}


def _refusing_authority(_request=None):
    raise NewsClassifierFailure("MODEL_UNAVAILABLE")


class _Harness:
    def __init__(self, tmp_path, capsys):
        self.tmp, self.capsys = tmp_path, capsys
        self.tool = card_tests._tool()
        setattr(self.tool, "build_live_conditional_fetch",
                lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("credential unavailable fixture")))
        self.root, self.gate, self.receipt_root = tmp_path / "reports", tmp_path / "gate.json", tmp_path / "receipts"
        self.receipts = QuantTelegramCardReceiptStore(self.receipt_root)
        self.calls, self.sent, self.factory = [], [], {"built": 0, "receipt_present_at_build": None}
        self.sender_built = 0
        self.paths = dict(holdings_path=tmp_path / "no_holdings.json", quotes_path=tmp_path / "no_quotes.sqlite3",
                          playbook_root=tmp_path / "playbooks", watchlist_root=tmp_path / "watchlists",
                          native_refs_path=tmp_path / "no_refs.sqlite3", news_path=tmp_path / "no_news.sqlite3",
                          dart_path=tmp_path / "no_dart.sqlite3")

    def reports(self, kr=KR_CARDS, us=US_CARDS, seeds=("a", "b")):
        return {"KR": _store_report(self.root, "KR", kr, seeds[0]), "US": _store_report(self.root, "US", us, seeds[1])}

    def arm(self, activation_id=ACTIVATION):
        write_card_activation(self.gate, activation_id=activation_id, mode="CANARY", expires_at=NOW + timedelta(minutes=30))

    def transport(self, failing=()):
        def transport(request):
            pack = request["evidence_pack"]
            key = f"{pack['market']}:{pack['symbol']}"
            receipt = self.receipts.read_analysis(ACTIVATION)
            self.calls.append({"key": key, "status": receipt["status"], "model_calls": receipt["model_calls"],
                               "reserved": [call["key"] for call in receipt["calls"]]})
            if pack["symbol"] in failing:
                raise TimeoutError("model timeout fixture")
            # raw provider form (indented) differs from any canonical re-serialisation
            return json.dumps(cycle2._wire_good_output(request), indent=2, ensure_ascii=False)
        return transport

    def factory_for(self, transport):
        def factory(**kwargs):
            self.factory["built"] += 1
            self.factory["receipt_present_at_build"] = self.receipts.read_analysis(ACTIVATION) is not None
            self.factory["kwargs"] = kwargs
            return transport
        return factory

    def sender_factory(self, token, chat):
        self.sender_built += 1
        return lambda text: self.sent.append(text) or 700 + len(self.sent)

    def run(self, *argv, transport=None, authority=_authority):
        factory = self.factory_for(transport if transport is not None else self.transport())
        code = self.tool.main(list(argv), environ=ENV, clock=lambda: NOW, gate_path=self.gate,
                              receipt_root=self.receipt_root, transport_factory=factory, authority_reader=authority,
                              sender_factory=self.sender_factory, **self.paths)
        out = self.capsys.readouterr().out
        return code, json.loads(out.strip().split("\n")[-1]), out

    def preview(self, *extra, **kwargs):
        return self.run("canary-preview", "--report-root", str(self.root), "--activation-id", ACTIVATION, *extra, **kwargs)

    def send(self, digests, *extra, **kwargs):
        expect = [item for market, digest in sorted(digests.items()) for item in ("--expect-report-sha256", f"{market}={digest}")]
        return self.run("canary-send", "--report-root", str(self.root), "--activation-id", ACTIVATION, *expect, *extra, **kwargs)

    def analysis_receipt(self):
        return self.receipts.read_analysis(ACTIVATION)


# --- zero live effects by default ---------------------------------------------------------

def test_default_paths_unchanged_and_canary_refuses_before_any_reservation_or_credential(tmp_path, capsys):
    h = _Harness(tmp_path, capsys)
    digests = h.reports()
    tool = h.tool
    # existing preview/send contracts untouched: the flag alone stays unbound, send never analyses
    code, out, _ = h.run("preview", "--report-root", str(h.root), "--analysis-preview")
    assert code == tool.EXIT_OK and out["sent"] == 0
    assert out["reports"]["evidence"]["analysis"] == {"enabled": False, "model_calls": 0, "reason": "model_seam_unbound"}
    assert h.factory["built"] == 0 and h.calls == []
    # canary-preview without an armed gate: refused before authority, reservation, credential, transport
    code, out, _ = h.preview(authority=_refusing_authority)
    assert code == tool.EXIT_REFUSED and out["off"] == "gate_absent" and out["model_calls"] == 0 and out["sent"] == 0
    assert h.factory["built"] == 0 and h.analysis_receipt() is None
    # armed but the route authority refuses: still before reservation and before the transport is even built
    h.arm()
    code, out, _ = h.preview(authority=_refusing_authority)
    assert code == tool.EXIT_REFUSED and out["off"] == "route_not_admitted:MODEL_UNAVAILABLE"
    assert h.factory["built"] == 0 and h.analysis_receipt() is None and h.calls == []
    # canary-send without a validated analysis receipt: zero sends, no transport
    code, out, _ = h.send(digests)
    assert code == tool.EXIT_REFUSED and out["off"] == "analysis_receipt_missing" and out["sent"] == 0
    assert h.sent == [] and h.factory["built"] == 0
    assert not list(h.receipt_root.glob("*")) and h.receipts.read(ACTIVATION) is None
    # mismatched activation id on an armed gate is a refusal, not a reservation
    code, out, _ = h.run("canary-preview", "--report-root", str(h.root), "--activation-id", "quant-other-id-01")
    assert out["off"] == "activation_mismatch" and h.analysis_receipt() is None and h.factory["built"] == 0


@pytest.mark.parametrize("conditional_live", [True, False], ids=["default-live", "local-only"])
def test_canary_preview_conditional_live_seam(tmp_path, capsys, monkeypatch, conditional_live):
    h = _Harness(tmp_path, capsys)
    h.reports()
    h.arm()
    cycle2.CurrentHoldingsStore(h.paths["holdings_path"], clock=lambda: NOW).replace(
        cycle2._holdings(), identity_fingerprints={cycle2.TOSS_SCOPE: "f" * 64})
    builds, scans, recorded = [], [], []

    def unavailable_builder(**kwargs):
        builds.append(kwargs)
        raise RuntimeError("credential unavailable fixture")

    def forbidden_scan(**kwargs):
        scans.append(kwargs)
        raise AssertionError("conditional provider must remain unqueried")

    build_contexts = h.tool.build_contexts

    def record_contexts(*args, **kwargs):
        result = build_contexts(*args, **kwargs)
        recorded.append(result)
        return result

    monkeypatch.setattr(h.tool, "build_live_conditional_fetch", unavailable_builder)
    monkeypatch.setattr(h.tool, "collect_open_conditional_orders", forbidden_scan)
    monkeypatch.setattr(h.tool, "build_contexts", record_contexts)
    if not conditional_live:
        h.paths["canary_conditional_live"] = False
    code, out, _ = h.preview()
    assert len(builds) == int(conditional_live)
    assert scans == []
    assert code == h.tool.EXIT_OK and out["status"] == "ANALYZED"
    assert out["provider_calls"] == ({"seam": "unavailable", "error": "RuntimeError"}
                                     if conditional_live else None)
    assert out["reports"]["evidence"]["conditional"] == {
        "performed": False, "provider_calls": 0, "scopes": {}}
    assert len(recorded) == 1
    contexts, evidence = recorded[0]
    assert evidence["holdings"]["available"] is True
    assert all(row.conditional.status == "UNAVAILABLE" and row.conditional.reason == "조회 미실행"
               for rows in contexts.values() for row in rows)
    assert len(h.calls) == h.analysis_receipt()["model_calls"] == 4  # synthetic transport only
    assert h.sent == [] and h.sender_built == 0


# --- preview: 4 model calls, 0 sends, reservation before every call, sanitized receipt ----

def test_canary_preview_makes_four_calls_zero_sends_with_reservation_before_each_call(tmp_path, capsys):
    h = _Harness(tmp_path, capsys)
    digests = h.reports()
    h.arm()
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    code, out, text = h.preview("--text")
    assert code == h.tool.EXIT_OK and out["mode"] == "canary-preview" and out["sent"] == 0 and h.sent == []
    assert out["status"] == "ANALYZED" and out["off"] is None and out["model_calls"] == 4 == len(h.calls)
    assert out["validated"] == ALL_KEYS and out["rejected"] == {}
    # the transport was built only after the durable reservation existed; each call saw its own
    # reservation persisted (status RESERVED, counter already incremented) before the transport ran
    assert h.factory["built"] == 1 and h.factory["receipt_present_at_build"] is True
    assert set(h.factory["kwargs"]) == {"model_id", "env_file", "timeout_seconds", "max_tokens"}
    assert h.factory["kwargs"]["model_id"] == qfc.FINITE_CANARY_MODEL_ID
    assert h.factory["kwargs"]["max_tokens"] == qfc.FINITE_CANARY_MAX_TOKENS
    assert h.factory["kwargs"]["timeout_seconds"] == qfc.FINITE_CANARY_CALL_TIMEOUT_SECONDS
    for index, call in enumerate(h.calls, 1):
        assert call["status"] == "RESERVED" and call["model_calls"] == index and call["reserved"][-1] == call["key"]
    assert [call["key"] for call in h.calls] == ALL_KEYS
    receipt = h.analysis_receipt()
    assert receipt["schema"] == qfc.ANALYSIS_RECEIPT_SCHEMA and receipt["status"] == "ANALYZED"
    assert receipt["model"] == {"provider": "zai", "model_id": qfc.FINITE_CANARY_MODEL_ID, "call_cap": 4, "retry": 0,
                                "timeout_seconds": qfc.FINITE_CANARY_CALL_TIMEOUT_SECONDS,
                                "completion_cap": qfc.FINITE_CANARY_MAX_TOKENS, "purpose": qfc.FINITE_CANARY_MODEL_PURPOSE}
    assert receipt["telegram_root_cap"] == 2 and receipt["model_calls"] == 4 and receipt["cards"] == ALL_KEYS
    assert [(call["key"], call["status"]) for call in receipt["calls"]] == [(key, "RETURNED") for key in ALL_KEYS]
    assert receipt["report_digests"] == digests and receipt["validated"] == ALL_KEYS
    assert sorted(receipt["analyses"]["KR"]) == ["000001", "000002"] and sorted(receipt["analyses"]["US"]) == ["AAPL", "NVDA"]
    assert set(receipt["message_digests"]) == {"KR", "US"} and all(len(d) == 64 for d in receipt["message_digests"].values())
    assert receipt["external"] == {"queries": 0, "fetches": 0}
    # route authority is carried as digests only — never the private file paths the reader returns
    assert receipt["route_authority"] == {"routing_state": {"sha256": "r" * 64, "mtime_ns": 1},
                                          "quota_latest": {"sha256": "q" * 64, "mtime_ns": 2, "observed_at": NOW.isoformat()}}
    # sanitized: no prompt/system contract, no raw provider output, no evidence pack, no paths/secrets
    raw_text = h.receipts.analysis_path(ACTIVATION).read_text(encoding="utf-8")
    request = build_analyzer_request(cycle2._pack())
    for leak in (request["system"], "evidence_pack", "output_contract", "/Users/", "token", "secret", "api_key",
                 "Bearer", "GLM_API_KEY", json.dumps(cycle2._good_output(cycle2._pack()), indent=2, ensure_ascii=False)):
        assert leak not in raw_text
    assert "account" not in raw_text.lower()
    # rendered preview carries the validated analysis for every card; only the receipt was written
    assert text.count("왜 이렇게 보나") == 4
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert sorted(set(after) - set(before)) == ["receipts/" + h.receipts.analysis_path(ACTIVATION).name,
                                                "receipts/" + h.receipts.analysis_lock_path(ACTIVATION).name]
    assert h.receipts.read(ACTIVATION) is None  # the send receipt is untouched by preview


def test_canary_preview_replay_makes_zero_calls(tmp_path, capsys):
    h = _Harness(tmp_path, capsys)
    h.reports()
    h.arm()
    assert h.preview()[1]["status"] == "ANALYZED" and len(h.calls) == 4
    code, out, _ = h.preview()
    assert code == h.tool.EXIT_REFUSED and out["off"] == "replay_no_call" and out["model_calls"] == 0
    assert out["receipt_status"] == "ANALYZED" and out["receipt_model_calls"] == 4
    assert len(h.calls) == 4 and h.factory["built"] == 1 and h.sent == []


# --- delivery: 0 new model calls, exactly 2 sends, replay 0 ------------------------------

def test_canary_send_delivers_exactly_two_root_messages_with_zero_new_model_calls(tmp_path, capsys):
    h = _Harness(tmp_path, capsys)
    digests = h.reports()
    h.arm()
    assert h.preview()[1]["status"] == "ANALYZED"
    preview_receipt = h.analysis_receipt()

    def forbidden(_request):
        raise AssertionError("delivery must never call the model")
    code, out, _ = h.send(digests, transport=forbidden)
    assert code == h.tool.EXIT_OK and out["mode"] == "canary-send" and out["model_calls"] == 0
    assert out["result"]["sent"] == 2 and out["result"]["off"] is None and len(h.sent) == 2
    assert out["reports"]["evidence"]["analysis"] == {"enabled": True, "model_calls": 0, "source": "analysis_receipt",
                                                      "validated": ALL_KEYS}
    assert len(h.calls) == 4 and h.factory["built"] == 1  # transport never rebuilt for delivery
    assert all("왜 이렇게 보나" in text for text in h.sent) and h.sent[0].startswith("📊 퀀트 KR") and h.sent[1].startswith("📊 퀀트 US")
    assert out["message_digests_verified"] == ["KR", "US"] and "message_drift_since_preview" not in out
    assert {market: hashlib.sha256(text.encode("utf-8")).hexdigest() for market, text in zip(("KR", "US"), h.sent)} \
        == preview_receipt["message_digests"]
    receipt = h.receipts.read(ACTIVATION)
    assert receipt["status"] == "SENT" and receipt["report_digests"] == digests and receipt["attempt_cap"] == 1
    assert [(part["key"], part["status"]) for part in receipt["parts"]] == [("KR:1", "SENT"), ("US:1", "SENT")]
    assert h.analysis_receipt() == preview_receipt  # delivery never rewrites the analysis receipt
    # replay of delivery: zero sends, zero model calls
    code, out, _ = h.send(digests, transport=forbidden)
    assert code == h.tool.EXIT_REFUSED and out["result"]["off"] == "cap_consumed" and len(h.sent) == 2


# --- fail closed: card failure, cap, mismatch, drift, coverage ----------------------------

def test_any_card_failure_stops_before_telegram_and_never_falls_back(tmp_path, capsys):
    h = _Harness(tmp_path, capsys)
    digests = h.reports()
    h.arm()
    code, out, text = h.preview("--text", transport=h.transport(failing=("NVDA",)))
    assert code == h.tool.EXIT_REFUSED and out["status"] == "FAILED" and out["sent"] == 0
    assert out["off"] == "analysis_rejected:US:NVDA" and out["rejected"] == {"US:NVDA": "model_failure:TimeoutError"}
    assert out["model_calls"] == 4 == len(h.calls)  # the remaining cards still got their single attempt, retry 0
    receipt = h.analysis_receipt()
    assert receipt["status"] == "FAILED" and "message_digests" not in receipt
    assert receipt["validated"] == ALL_KEYS[:-1] and set(receipt["analyses"]["US"]) == {"AAPL"}
    assert "observation" not in receipt and "partial_cards" not in receipt
    assert [(call["key"], call["status"]) for call in receipt["calls"]] == [
        ("KR:000001", "RETURNED"), ("KR:000002", "RETURNED"), ("US:AAPL", "RETURNED"), ("US:NVDA", "FAILED:TimeoutError")]
    # delivery refuses: no deterministic-card fallback, zero sends
    code, out, _ = h.send(digests)
    assert code == h.tool.EXIT_REFUSED and out["off"] == "analysis_not_validated:FAILED" and h.sent == []
    assert h.receipts.read(ACTIVATION) is None
    # a replay of the failed preview also makes no call
    assert h.preview()[1]["off"] == "replay_no_call" and len(h.calls) == 4


def test_model_cap_refuses_before_reservation_when_cards_exceed_four(tmp_path, capsys):
    h = _Harness(tmp_path, capsys)
    h.reports(kr=KR_CARDS + (_card("000003", "KR", "세번째", VALUES_CALM),))
    h.arm()
    code, out, _ = h.preview()
    assert code == h.tool.EXIT_REFUSED and out["off"] == "model_cap_exceeded:5" and out["model_calls"] == 0
    assert h.factory["built"] == 0 and h.calls == [] and h.analysis_receipt() is None


def test_send_refuses_on_expected_digest_mismatch_report_drift_and_missing_expectation(tmp_path, capsys):
    h = _Harness(tmp_path, capsys)
    digests = h.reports()
    h.arm()
    assert h.preview()[1]["status"] == "ANALYZED"
    code, out, _ = h.send({"KR": digests["KR"], "US": "f" * 64})
    assert code == h.tool.EXIT_REFUSED and out["off"] == "report_digest_mismatch:US" and h.sent == []
    code, out, _ = h.send({"KR": digests["KR"]})
    assert out["off"] == "expected_digest_missing:US" and h.sent == []
    code, out, _ = h.run("canary-send", "--report-root", str(h.root), "--activation-id", ACTIVATION,
                         "--expect-report-sha256", "KR=short")
    assert out["off"] == "invalid_expected_digest" and h.sent == []
    # the located KR report is no longer the analysed one (receipt digest differs) → drift, no send
    tampered = dict(h.analysis_receipt())
    tampered["report_digests"] = dict(tampered["report_digests"], KR="0" * 64)
    h.receipts.write_analysis(ACTIVATION, tampered)
    code, out, _ = h.send(digests)
    assert out["off"] == "report_digest_drift:KR" and h.sent == [] and h.receipts.read(ACTIVATION) is None
    # a receipt whose stored analyses do not cover every located card is refused too (no plain-card fallback)
    partial = dict(h.analysis_receipt(), report_digests=digests)
    partial["analyses"] = {"KR": dict(partial["analyses"]["KR"]), "US": {}}
    h.receipts.write_analysis(ACTIVATION, partial)
    code, out, _ = h.send(digests)
    assert out["off"] == "analysis_coverage_mismatch" and h.sent == []


def test_canary_send_refuses_before_telegram_when_the_rendered_message_drifted_from_the_preview(tmp_path, capsys):
    """M1: the message about to be sent must be byte-identical to the previewed one."""
    h = _Harness(tmp_path, capsys)
    digests = h.reports()
    h.arm()
    assert h.preview()[1]["status"] == "ANALYZED"
    preview_receipt = h.analysis_receipt()
    # A quote appears after preview; no model is run and the surviving facts block changes.
    from dataclasses import replace
    from decimal import Decimal
    from tokmo.programs.quant_telegram_card import QuoteContextV1
    original_contexts = h.tool.build_contexts
    def changed_contexts(*args, **kwargs):
        contexts, evidence = original_contexts(*args, **kwargs)
        contexts['KR'] = tuple(replace(ctx, quote=QuoteContextV1(Decimal('123'), 'KRW', NOW, 'fixture')) for ctx in contexts['KR'])
        return contexts, evidence
    h.tool.build_contexts = changed_contexts
    code, out, _ = h.send(digests)
    assert code == h.tool.EXIT_REFUSED and out["off"] == "message_digest_drift:KR" and out["sent"] == 0
    assert h.sent == [] and h.sender_built == 0 and h.receipts.read(ACTIVATION) is None  # no reservation, no transport
    assert h.analysis_receipt() == preview_receipt and len(h.calls) == 4
    h.tool.build_contexts = original_contexts
    # a receipt whose stored digest no longer equals the render is refused the same way (US)
    tampered = dict(preview_receipt)
    tampered["message_digests"] = dict(preview_receipt["message_digests"], US="0" * 64)
    h.receipts.write_analysis(ACTIVATION, tampered)
    code, out, _ = h.send(digests)
    assert out["off"] == "message_digest_drift:US" and h.sent == [] and h.sender_built == 0
    assert h.receipts.read(ACTIVATION) is None
    # restored receipt: the identical render delivers exactly two
    h.receipts.write_analysis(ACTIVATION, preview_receipt)
    code, out, _ = h.send(digests)
    assert code == h.tool.EXIT_OK and out["result"]["sent"] == 2 and h.sender_built == 1
    assert qfc.check_message_digests((), {"message_digests": {}}) is None


def test_telegram_cap_refuses_a_market_that_needs_more_than_one_root_message():
    one = MarketMessageV1("KR", "2026-09-02", "a" * 64, "d" * 64, ("KR body",), (), ())
    two = MarketMessageV1("US", "2026-09-02", "b" * 64, "e" * 64, ("US part 1", "US part 2"), (), ())
    assert qfc.check_messages((one, MarketMessageV1("US", "2026-09-02", "b" * 64, "e" * 64, ("US body",), (), ()))) is None
    assert qfc.check_messages((one, two)) == "telegram_cap_exceeded:US:2"
    assert qfc.check_messages((one,)) == "markets_incomplete"
    assert qfc.check_messages((one, one)) == "markets_incomplete"
    assert qfc.TELEGRAM_ROOT_CAP == 2 and qfc.MODEL_CALL_CAP == 4 and qfc.RETRIES == 0


# --- B1: exact-route admission (provider=zai, model=glm-5.3), never the NEWS flash marker ----

EXACT_ROUTE_LINE = "- direct Z.AI GLM route: `provider=zai`, `model=glm-5.3`, fallback 0 preflight PASS\n"
FLASH_ONLY_DOC = ("# Model Routing State\n- quota authority: `~/.hermes/cron/state/model-quota-latest.json`\n"
                  "- invocation freshness: launcher reads both files\n"
                  "- NEWS classifier exact route: `provider=zai`, `model=glm-5.3-flash` bounded probe worker only\n")


def _quota(observed=NOW, *, ok=True, five=40, weekly=30, schema="model-quota-latest-v1", drop=()):
    windows = [{"label": "5시간", "remaining": five}, {"label": "주간", "remaining": weekly},
               {"label": "월간 MCP", "remaining": 100}]
    return {"schema": schema, "observed_at": observed.isoformat(),
            "providers": {"zai": {"ok": ok, "windows": [w for w in windows if w["label"] not in drop]}}}


def test_route_admission_requires_the_exact_zai_glm_5_3_markers_and_a_fresh_zai_quota():
    exact = FLASH_ONLY_DOC + EXACT_ROUTE_LINE

    def refused(text, quota, now=NOW):
        with pytest.raises(qfc.RouteNotAdmitted) as stop:
            qfc.admit_route(routing_text=text, quota_payload=quota, now=now)
        return stop.value.exit_code
    assert qfc.admit_route(routing_text=exact, quota_payload=_quota(), now=NOW) == NOW.isoformat()
    # the NEWS flash marker alone never admits the quant route; `model=glm-5.3-flash` is not `model=glm-5.3`
    assert refused(FLASH_ONLY_DOC, _quota()) == "ROUTE_NOT_ADMITTED"
    assert refused(FLASH_ONLY_DOC.replace("glm-5.3-flash", "glm-5.3-flash`, `provider=zai"), _quota()) == "ROUTE_NOT_ADMITTED"
    # the exact line alone (no NEWS markers anywhere) is sufficient on the routing side
    assert qfc.admit_route(routing_text=EXACT_ROUTE_LINE, quota_payload=_quota(), now=NOW) == NOW.isoformat()
    assert refused(exact.replace("`model=glm-5.3`", "`model=glm-5.3-flash`"), _quota()) == "ROUTE_NOT_ADMITTED"
    assert refused(exact.replace("`provider=zai`", "`provider=codex`"), _quota()) == "ROUTE_NOT_ADMITTED"
    # quota side reuses the canonical bounds: schema, freshness (45 min), skew (5 min), provider ok, both windows
    assert refused(exact, _quota(schema="other")) == "MODEL_UNAVAILABLE"
    assert refused(exact, _quota(NOW - timedelta(minutes=46))) == "MODEL_UNAVAILABLE"
    assert qfc.admit_route(routing_text=exact, quota_payload=_quota(NOW - timedelta(minutes=44)), now=NOW)
    assert refused(exact, _quota(NOW + timedelta(minutes=6))) == "MODEL_UNAVAILABLE"
    assert refused(exact, _quota(ok=False)) == "MODEL_UNAVAILABLE"
    assert refused(exact, _quota(drop=("주간",))) == "MODEL_UNAVAILABLE"
    assert refused(exact, _quota(five=0)) == "QUOTA_EXCEEDED"
    assert refused(exact, _quota(weekly=True)) == "MODEL_UNAVAILABLE"
    assert refused(exact, "not a dict") == "MODEL_UNAVAILABLE"
    assert refused(exact, dict(_quota(), observed_at="2026-09-03T01:00:00")) == "MODEL_UNAVAILABLE"  # naive
    assert qfc.ROUTE_MARKERS == ("direct Z.AI GLM route", "`provider=zai`", "`model=glm-5.3`")


def test_canary_preview_admits_only_through_the_exact_route_in_the_real_authority_files(tmp_path, capsys):
    h = _Harness(tmp_path, capsys)
    h.reports()
    h.arm()
    routing, quota = tmp_path / "MODEL_ROUTING_STATE.md", tmp_path / "model-quota-latest.json"
    routing.write_text(FLASH_ONLY_DOC, encoding="utf-8")
    quota.write_text(json.dumps(_quota()), encoding="utf-8")
    files = ("--routing-state", str(routing), "--quota-latest", str(quota))
    code, out, _ = h.preview(*files, authority=None)  # no injected reader: the tool's own file-bound admission
    assert code == h.tool.EXIT_REFUSED and out["off"] == "route_not_admitted:ROUTE_NOT_ADMITTED"
    assert h.factory["built"] == 0 and h.analysis_receipt() is None and h.calls == []
    quota.unlink()
    routing.write_text(FLASH_ONLY_DOC + EXACT_ROUTE_LINE, encoding="utf-8")
    code, out, _ = h.preview(*files, authority=None)
    assert out["off"] == "route_not_admitted:MODEL_UNAVAILABLE" and h.analysis_receipt() is None
    quota.write_text(json.dumps(_quota()), encoding="utf-8")
    code, out, _ = h.preview(*files, authority=None)
    assert code == h.tool.EXIT_OK and out["status"] == "ANALYZED" and len(h.calls) == 4
    assert h.analysis_receipt()["route_authority"] == {
        "routing_state": {"sha256": hashlib.sha256(routing.read_bytes()).hexdigest(), "mtime_ns": routing.stat().st_mtime_ns},
        "quota_latest": {"sha256": hashlib.sha256(quota.read_bytes()).hexdigest(), "mtime_ns": quota.stat().st_mtime_ns,
                         "observed_at": NOW.isoformat()}}


# --- B2: atomic reservation and durable cumulative cap across processes/crash/replay ------

def _ledger_store(tmp_path):
    receipts = QuantTelegramCardReceiptStore(tmp_path / "receipts")
    assert receipts.reserve_analysis(ACTIVATION, {"activation_id": ACTIVATION, "model_calls": 0, "calls": []}) is True
    return receipts


def _ledger_over(receipts, activation_id=ACTIVATION, cap=qfc.MODEL_CALL_CAP):
    return qfc.bind_call_ledger(update=lambda mutate: receipts.update_analysis(activation_id, mutate), cap=cap)


def test_reservation_is_an_exclusive_create_and_updates_are_locked_read_modify_write(tmp_path):
    receipts = _ledger_store(tmp_path)
    first = receipts.read_analysis(ACTIVATION)
    # a second reservation never replaces the first (O_EXCL), whatever it carries
    assert receipts.reserve_analysis(ACTIVATION, {"activation_id": ACTIVATION, "model_calls": 99, "calls": []}) is False
    assert receipts.read_analysis(ACTIVATION) == first
    with pytest.raises(ValueError):
        receipts.reserve_analysis(ACTIVATION, {"activation_id": "quant-other-id-01"})
    updated = receipts.update_analysis(ACTIVATION, lambda receipt: dict(receipt, model_calls=receipt["model_calls"] + 1))
    assert updated["model_calls"] == 1 == receipts.read_analysis(ACTIVATION)["model_calls"]
    with pytest.raises(ValueError):
        receipts.update_analysis("quant-never-reserved-01", lambda receipt: receipt)
    assert not (tmp_path / "receipts" / "quant-never-reserved-01.analysis.json").exists()


def test_call_ledger_gates_every_start_on_the_durable_count_including_after_a_crash(tmp_path):
    receipts = _ledger_store(tmp_path)
    reserve, settle = _ledger_over(receipts)
    for index in range(1, 4):
        reserve(f"K:{index}")
        settle(f"K:{index}", "RETURNED")
    # process crashed after reserving the 4th call and before settling it: the row stays RESERVED
    reserve("K:4")
    receipt = receipts.read_analysis(ACTIVATION)
    assert receipt["model_calls"] == 4 and receipt["calls"][-1] == {"key": "K:4", "status": "RESERVED"}
    # a fresh ledger (new process, new in-memory state) cannot start a 5th call: the durable count gates it
    reserve_again, _settle = _ledger_over(receipts)
    with pytest.raises(qfc.ModelCapExhausted):
        reserve_again("K:5")
    assert receipts.read_analysis(ACTIVATION)["model_calls"] == 4 and len(receipts.read_analysis(ACTIVATION)["calls"]) == 4
    # the capped seam refuses without touching the transport
    calls = []
    analysis = qfc.capped_analysis(lambda request: calls.append(request) or "{}", reserve=reserve_again, settle=_settle,
                                   captured={})
    assert run_contextual_analysis(cycle2._ADAPTER_PACK, analysis=analysis) == (None, "model_failure:ModelCapExhausted")
    assert calls == []


def test_concurrent_ledgers_on_one_activation_never_exceed_four_starts(tmp_path):
    receipts = _ledger_store(tmp_path)
    contenders, started, refused, errors = 16, [], [], []
    barrier = threading.Barrier(contenders)

    def worker(index):
        reserve, settle = _ledger_over(receipts)  # each contender: own ledger, own descriptors (like a process)
        key = f"K:{index}"
        try:
            barrier.wait()
            reserve(key)
        except qfc.ModelCapExhausted:
            refused.append(key)
            return
        except Exception as error:  # noqa: BLE001 - any other failure is a test failure
            errors.append(repr(error))
            return
        started.append(key)
        time.sleep(0.01)
        settle(key, "RETURNED")
    threads = [threading.Thread(target=worker, args=(index,)) for index in range(contenders)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and len(started) == 4 and len(refused) == 12
    receipt = receipts.read_analysis(ACTIVATION)
    assert receipt["model_calls"] == 4 and sorted(call["key"] for call in receipt["calls"]) == sorted(started)
    assert all(call["status"] == "RETURNED" for call in receipt["calls"])


def test_concurrent_canary_previews_on_one_activation_make_at_most_four_starts_total(tmp_path, capsys):
    h = _Harness(tmp_path, capsys)
    h.reports()
    h.arm()
    runners, starts, codes, errors = 6, [], [], []
    barrier = threading.Barrier(runners)
    lock = threading.Lock()

    def transport(request):
        with lock:
            starts.append(f"{request['evidence_pack']['market']}:{request['evidence_pack']['symbol']}")
        time.sleep(0.02)  # widen the window in which a duplicate process would also be calling
        return json.dumps(cycle2._wire_good_output(request), ensure_ascii=False)

    def runner():
        try:
            barrier.wait()
            code = h.tool.main(["canary-preview", "--report-root", str(h.root), "--activation-id", ACTIVATION],
                               environ=ENV, clock=lambda: NOW, gate_path=h.gate, receipt_root=h.receipt_root,
                               transport_factory=lambda **_kwargs: transport, authority_reader=_authority,
                               sender_factory=h.sender_factory, **h.paths)
            with lock:
                codes.append(code)
        except Exception as error:  # noqa: BLE001
            with lock:
                errors.append(repr(error))
    threads = [threading.Thread(target=runner) for _ in range(runners)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    capsys.readouterr()
    assert errors == [] and sorted(codes) == [h.tool.EXIT_OK] + [h.tool.EXIT_REFUSED] * (runners - 1)
    assert len(starts) == 4 and sorted(starts) == ALL_KEYS  # total starts across all runners ≤ cap, one per card
    receipt = h.analysis_receipt()
    assert receipt["status"] == "ANALYZED" and receipt["model_calls"] == 4 and h.sent == []
    assert sorted(path.name for path in (tmp_path / "receipts").iterdir() if not path.name.startswith(".")) \
        == sorted({h.receipts.analysis_path(ACTIVATION).name, h.receipts.analysis_lock_path(ACTIVATION).name})


# --- pure seams: cap wrapper, record round trip, prompt, authority sanitising -------------

def _memory_ledger(ledger, cap=qfc.MODEL_CALL_CAP):
    def update(mutate):
        ledger.update(mutate(dict(ledger)))
        return dict(ledger)
    return qfc.bind_call_ledger(update=update, cap=cap)


def test_capped_analysis_reserves_before_transport_and_refuses_beyond_cap():
    ledger, captured, seen = {"model_calls": 0, "calls": []}, {}, []
    reserve, settle = _memory_ledger(ledger, cap=1)

    def transport(request):
        seen.append(list(ledger["calls"]))  # reservation already durable when the transport runs
        return json.dumps(cycle2._wire_good_output(request), ensure_ascii=False)
    analysis = qfc.capped_analysis(transport, reserve=reserve, settle=settle, captured=captured)
    result, reason = run_contextual_analysis(cycle2._ADAPTER_PACK, analysis=analysis)
    assert reason == "validated" and result is not None and captured == {"KR:000001": result}
    assert seen == [[{"key": "KR:000001", "status": "RESERVED"}]]
    assert ledger == {"model_calls": 1, "calls": [{"key": "KR:000001", "status": "RETURNED"}]}
    result, reason = run_contextual_analysis(cycle2._CALM_PACK, analysis=analysis)
    assert result is None and reason == "model_failure:ModelCapExhausted" and len(seen) == 1
    assert ledger["model_calls"] == 1 and "KR:000002" not in captured  # no reservation, no call beyond the cap


@pytest.mark.parametrize("invalid,reason", [
    ({"unrelated": True}, "required_sections"),
    (cycle2._good_output(cycle2._ADAPTER_PACK), "unknown_evidence_id"),
])
def test_capped_analysis_settles_transport_failure_and_keeps_invalid_output_uncaptured(invalid, reason):
    ledger, captured = {"model_calls": 0, "calls": []}, {}
    reserve, settle = _memory_ledger(ledger)

    def failing(_request):
        raise NewsClassifierFailure("QUOTA_EXCEEDED")
    analysis = qfc.capped_analysis(failing, reserve=reserve, settle=settle, captured=captured)
    assert run_contextual_analysis(cycle2._ADAPTER_PACK, analysis=analysis) == (None, "model_failure:NewsClassifierFailure")
    assert ledger["calls"] == [{"key": "KR:000001", "status": "FAILED:NewsClassifierFailure"}] and captured == {}
    analysis = qfc.capped_analysis(lambda _r: json.dumps(invalid), reserve=reserve, settle=settle,
                                   captured=captured)
    assert run_contextual_analysis(cycle2._ADAPTER_PACK, analysis=analysis) == (None, "invalid_output:" + reason)
    assert ledger["calls"][-1] == {"key": "KR:000001", "status": "RETURNED"} and captured == {}


def test_analysis_record_round_trips_exactly_and_rejects_foreign_shapes():
    result, reason = run_contextual_analysis(cycle2._ADAPTER_PACK, analysis=cycle2.analyzer_adapter(cycle2._good_transport()))
    assert reason == "validated"
    record = qfc.analysis_record(result)
    assert json.loads(json.dumps(record, ensure_ascii=False)) == record  # JSON-safe
    assert qfc.analysis_from_record(record) == result
    for broken in ({}, dict(record, extra=1), {k: v for k, v in record.items() if k != "labels"}, "text"):
        with pytest.raises(ValueError):
            qfc.analysis_from_record(broken)
    receipt = {"schema": qfc.ANALYSIS_RECEIPT_SCHEMA, "status": "ANALYZED", "analyses": {"KR": {"000001": record}}}
    assert qfc.analyses_from_receipt(receipt) == {"KR": {"000001": result}}
    with pytest.raises(ValueError):
        qfc.analyses_from_receipt({"schema": qfc.ANALYSIS_RECEIPT_SCHEMA, "analyses": {"KR": {"000009": record}}})


def test_prompt_text_is_deterministic_json_only_and_carries_nothing_but_the_request():
    request = build_analyzer_request(cycle2._ADAPTER_PACK)
    prompt = qfc.analyzer_prompt_text(request)
    assert prompt == qfc.analyzer_prompt_text(build_analyzer_request(cycle2._ADAPTER_PACK))
    assert request["system"] in prompt and request["task"]["instruction"] in prompt
    assert json.dumps(request["task"]["output_contract"], ensure_ascii=False, sort_keys=True, separators=(",", ":")) in prompt
    assert json.dumps(request["evidence_pack"], ensure_ascii=False, sort_keys=True, separators=(",", ":")) in prompt
    assert "JSON 객체 하나뿐" in prompt
    for leak in ("/Users/", "token", "secret", "api_key"):
        assert leak not in prompt


# --- RCA 2026-09-04 (first live activation: 4 calls, 0 sends, 4 validator rejections) ------
# statement_shape / free_text_number / free_text_number / statement_text — the prompt must make
# every validator constraint machine-checkably explicit; the validator itself is untouched.

from tokmo.programs.quant_contextual_analysis import validate_analysis  # noqa: E402
from tokmo.programs import quant_contextual_analysis as qca  # noqa: E402

_ORDER_PACK = cycle2.build_evidence_pack(
    _card("000001", "KR", "테스트전자", VALUES_HOT, ZONE),
    cycle2.CardContextV1("000001", cycle2._QUOTE("3000000"), None, (), True, False, None, None,
                         cycle2.ConditionalContextV1("ACTIVE", None, (cycle2._order_ctx("2300000"),))),
    completed_session_date="2026-09-02")


def _fill(skeleton, sentence="근거가 서로 지지해 판단이 유지됨"):
    """The skeleton with its placeholders replaced by ordinary sentences (ids untouched)."""
    def fill(value):
        if isinstance(value, dict):
            return {key: (sentence if key == "statement" and value[key] == qfc.STATEMENT_PLACEHOLDER
                          else fill(value[key])) for key in value}
        if isinstance(value, list):
            return [fill(item) for item in value]
        return sentence if value == qfc.STATEMENT_PLACEHOLDER else value
    return fill(skeleton)


def _rejects(output, pack):
    with pytest.raises(qca.AnalysisValidationError) as stop:
        validate_analysis(output, pack)
    return stop.value.args[0]


def test_prompt_skeleton_is_exactly_the_validator_shape_and_cannot_be_echoed():
    for pack in (cycle2._ADAPTER_PACK, cycle2._CALM_PACK, _ORDER_PACK):
        request = build_analyzer_request(pack)
        skeleton = qfc.analyzer_prompt_skeleton(request)
        assert set(skeleton) == qca.REQUIRED_KEYS and skeleton["symbol"] == pack["symbol"]
        for name in qca.LIST_SECTIONS:
            items = skeleton[name]
            assert isinstance(items, list) and (items or name == "assumptions")
            assert all(set(item) == {"statement", "evidence_ids"} for item in items)
        assert skeleton["conditional_order_assessment"] is None
        assert set(skeleton["confidence"]) == {"level", "statement", "evidence_ids"}
        assert len(skeleton["evidence_relations"][0]["evidence_ids"]) >= 2
        # every id in the skeleton is a real pack id; a filled skeleton validates for with/without orders
        ids = set(qca._citation_map(pack).values())
        cited = [item for name in qca.LIST_SECTIONS for entry in skeleton[name] for item in entry["evidence_ids"]]
        cited += skeleton["confidence"]["evidence_ids"]
        assert cited and set(cited) <= ids
        filled = cycle2._map_reply(_fill(skeleton), {v: k for k, v in qca._citation_map(pack).items()})
        assert validate_analysis(filled, pack).symbol == pack["symbol"]
        # the literal skeleton is NOT acceptable output: its placeholder carries digits on purpose
        assert _rejects(skeleton, pack) == "free_text_number"
        # the prompt embeds the skeleton verbatim as one parseable JSON line
        prompt = qfc.analyzer_prompt_text(request)
        line = next(line for line in prompt.splitlines() if line.startswith("skeleton: "))
        assert json.loads(line[len("skeleton: "):]) == skeleton
    assert qfc.analyzer_prompt_skeleton(build_analyzer_request(_ORDER_PACK))["conditional_order_assessment"] is None
    order_prompt = qfc.analyzer_prompt_text(build_analyzer_request(_ORDER_PACK))
    assert "기본값은 null" in order_prompt and "방향만으로 익절·손절 목적을 추론하지 않는다" in order_prompt


def test_prompt_rules_name_each_observed_failure_class_explicitly():
    pack = cycle2._ADAPTER_PACK
    good = cycle2._good_output(pack)
    # KR:000660 statement_shape — a list item carrying an extra key (the model added a verdict)
    shape = dict(good, alternatives=[dict(good["alternatives"][0], verdict="조정 검토")])
    # KR:005930 free_text_number — a digit-bearing token that is not an approved metric label
    sma = dict(good, summary="SMA20 위에서 3개월째 상승 추세 유지")
    # US:NVDA free_text_number — a restated numeric value instead of an evidence-id citation
    pct = dict(good, counterevidence=[cycle2._statement("최대낙폭이 21.4% 로 되돌림 위험 큼", "f:max_drawdown")])
    # US:SPY statement_text — an empty statement (and an over-long one)
    empty = dict(good, summary="")
    long = dict(good, summary="가" * (qca.MAX_STATEMENT_CHARS + 1))
    assert _rejects(shape, pack) == "statement_shape"
    assert _rejects(sma, pack) == "free_text_number" and _rejects(pct, pack) == "free_text_number"
    assert _rejects(empty, pack) == "statement_text" and _rejects(long, pack) == "statement_text"
    assert validate_analysis(good, pack) is not None  # the validator itself is unchanged
    rules = qfc.analyzer_prompt_rules(build_analyzer_request(pack))
    text = "\n".join(rules)
    # shape: exact keys of list items / assessment / confidence, and the exact top-level key set
    assert "statement, evidence_ids" in text and "verdict, statement, evidence_ids" in text
    assert "level, statement, evidence_ids" in text and ", ".join(sorted(qca.REQUIRED_KEYS)) in text
    # numbers: no digits except the approved labels, cite the id instead of restating the value
    assert ", ".join(qca._ALLOWED_ANALYSIS_NUMBER_LABELS) in text and "아라비아 숫자" in text
    assert "evidence_ids" in text and "인용" in text and "SMA20" not in text
    # text: non-empty and the exact length bound
    assert f"{qca.MAX_STATEMENT_CHARS}자" in text and "비어 있지 않" in text
    # citations, verdict/level enums, allowed ids, single-object output
    citations = build_analyzer_request(pack)["task"]["output_contract"]["citations"]
    assert f"{citations['min_per_relation']}개 이상" in text
    assert f"{citations['min_per_statement']}개 이상" in text and "중복" in text
    assert "한국어 문장" not in text and "문장 하나에 판단 하나" not in text
    assert ", ".join(qca.CONDITIONAL_VERDICTS) in text and "/".join(qca.CONFIDENCE_LEVELS) in text
    assert all(item in text for item in qca._citation_map(pack).values())
    assert "JSON 객체 하나뿐" in text and "코드펜스" in text and "마크다운" in text
    # the rules are deterministic and every rule is numbered
    assert rules == qfc.analyzer_prompt_rules(build_analyzer_request(pack))
    assert all(rule.split(".")[0].isdigit() for rule in rules)


def test_prompt_final_self_check_repeats_contract_citation_minima():
    for pack in (cycle2._ADAPTER_PACK, cycle2._CALM_PACK, _ORDER_PACK):
        request = build_analyzer_request(pack)
        citations = request["task"]["output_contract"]["citations"]
        prompt = qfc.analyzer_prompt_text(request)
        skeleton = qfc.analyzer_prompt_skeleton(request)
        assert f"skeleton의 {qfc.STATEMENT_PLACEHOLDER} 자리만 실제 서술로 바꾼다." not in prompt
        assert (
            "skeleton은 객체마다 키 집합을 그대로 보여준다. "
            "각 객체의 키 집합은 skeleton과 똑같이 유지하고 <placeholder>를 실제 서술로 바꾼다. "
            "항목 수는 규칙 3, level은 규칙 5 안에서 고른다. "
            "skeleton의 evidence_ids가 완성 문장을 직접 지지하면 유지하고, "
            "아니면 관련 허용 id로 교체한다. 그 JSON 객체 하나만 출력하라."
        ) in prompt
        assert "conditional_order_assessment는 null 또는 정확히 3개 키" in prompt
        assert "기본값은 null이다." in prompt
        assert "출력 직전 자체검사" in prompt
        assert f"서로 다른 관련 허용 id를 최소 {citations['min_per_relation']}개" in prompt
        assert f"관련 허용 id를 최소 {citations['min_per_statement']}개" in prompt
        assert "문장을 직접 지지하면 유지하고, 아니면 관련 허용 id로 교체" in prompt
        assert "중복 제거" in prompt and "허용 id 전체와 대조" in prompt and "최소 개수 확인" in prompt
        assert "중복·빈 배열·허용 목록 밖 id·최소 개수 미달" in prompt
        assert "summary와 모든 statement에서 승인 지표명을 잠시 제거한 뒤 아라비아 숫자를 검사" in prompt
        assert "숫자 주장을 정성 문장으로 바꾸고 해당 값을 가진 evidence id를 인용" in prompt
        assert "evidence_ids를 허용 id로 채운 뒤" not in prompt
        for name in qca.LIST_SECTIONS:
            minimum = citations["min_per_relation"] if name == "evidence_relations" else citations["min_per_statement"]
            assert all(len(item["evidence_ids"]) >= minimum for item in skeleton[name])
        assert skeleton["conditional_order_assessment"] is None
        assert len(skeleton["confidence"]["evidence_ids"]) >= citations["min_per_statement"]
    good = cycle2._good_output(cycle2._ADAPTER_PACK)
    duplicate = dict(good, alternatives=[dict(good["alternatives"][0], evidence_ids=["f:rsi14", "f:rsi14"])])
    empty = dict(good, counterevidence=[dict(good["counterevidence"][0], evidence_ids=[])])
    assert _rejects(duplicate, cycle2._ADAPTER_PACK) == "unsupported_claim"
    assert _rejects(empty, cycle2._ADAPTER_PACK) == "unsupported_claim"


def test_prompt_is_bounded_and_privacy_checked_before_any_reservation():
    ledger, captured, calls = {"model_calls": 0, "calls": []}, {}, []
    reserve, settle = _memory_ledger(ledger)
    analysis = qfc.capped_analysis(lambda request: calls.append(request) or "{}", reserve=reserve, settle=settle,
                                   captured=captured)
    for pack in (cycle2._ADAPTER_PACK, cycle2._CALM_PACK, _ORDER_PACK):
        assert len(qfc.analyzer_prompt_text(build_analyzer_request(pack))) <= qfc.PROMPT_MAX_CHARS
    huge = dict(cycle2._ADAPTER_PACK, name="가" * qfc.PROMPT_MAX_CHARS)
    assert run_contextual_analysis(huge, analysis=analysis) == (None, "model_failure:PromptRejected")
    leaky = dict(cycle2._ADAPTER_PACK, name="/Users/someone/private")
    assert run_contextual_analysis(leaky, analysis=analysis) == (None, "model_failure:PromptRejected")
    assert ledger == {"model_calls": 0, "calls": []} and calls == []  # no reservation, no transport


def test_sanitized_authority_keeps_digests_only():
    assert qfc.sanitize_authority(_authority()) == {
        "routing_state": {"sha256": "r" * 64, "mtime_ns": 1},
        "quota_latest": {"sha256": "q" * 64, "mtime_ns": 2, "observed_at": NOW.isoformat()}}
    assert qfc.sanitize_authority(None) == {} and qfc.sanitize_authority({"routing_state": "x"}) == {"routing_state": {}}


# --- sanctioned direct route composition (no network: injected opener) --------------------

class _FakeResponse:
    def __init__(self, body):
        self._body, self.status = body, 200

    def read(self, amount=-1):
        return self._body if amount is None or amount < 0 else self._body[:amount]

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("json_mode", [False, True])
def test_live_transport_composes_direct_route_with_per_call_credential_and_json_extraction(tmp_path, json_mode):
    from tokmo.facts.news_http import MAX_TOKENS
    tool = card_tests._tool()
    env_file = tmp_path / "hermes.env"
    env_file.write_text("GLM_API_KEY=live-secret-key\n", encoding="utf-8")
    env_file.chmod(0o600)
    captures = []
    good = cycle2._good_output(cycle2._ADAPTER_PACK)

    class _Opener:
        def open(self, request, timeout=None):
            captures.append({"url": request.full_url, "authorization": request.get_header("Authorization"),
                             "body": json.loads(request.data.decode("utf-8")), "timeout": timeout})
            content = "설명 앞말\n```json\n" + json.dumps(good, ensure_ascii=False) + "\n```"
            return _FakeResponse(json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8"))
    transport = tool.build_live_analyzer_transport(model_id=qfc.FINITE_CANARY_MODEL_ID, env_file=env_file,
                                                   timeout_seconds=7, max_tokens=qfc.FINITE_CANARY_MAX_TOKENS,
                                                   opener=_Opener(), json_mode=json_mode)
    request = build_analyzer_request(cycle2._ADAPTER_PACK)
    assert json.loads(transport(request)) == good  # fenced reply → exactly the last JSON object, as a string
    assert len(captures) == 1 and captures[0]["timeout"] == 7
    assert captures[0]["url"] == "https://api.z.ai/api/coding/paas/v4/chat/completions"
    assert captures[0]["authorization"] == "Bearer live-secret-key"
    body = captures[0]["body"]
    assert body["model"] == "glm-5.3" and body["max_tokens"] == qfc.FINITE_CANARY_MAX_TOKENS != MAX_TOKENS
    assert body["messages"] == [{"role": "user", "content": qfc.analyzer_prompt_text(request)}]
    if json_mode:
        assert body.pop("response_format") == {"type": "json_object"}
    assert set(body) == {"model", "messages", "stream", "thinking", "reasoning_effort", "max_tokens"}
    # the credential is resolved per call, never at build time: an insecure file fails closed on the call itself
    env_file.chmod(0o644)
    with pytest.raises(NewsClassifierFailure):
        transport(request)
    assert len(captures) == 1  # no request left the process without a valid credential
    # the NEWS classifier default body is untouched
    from tokmo.facts.news_http import zai_chat_once
    zai_chat_once("p", timeout_seconds=1, api_key="k", opener=_Opener())
    assert captures[-1]["body"]["max_tokens"] == MAX_TOKENS and captures[-1]["body"]["model"] == "glm-5.3-flash"


def test_cli_help_documents_the_finite_canary_subcommands(capsys):
    tool = card_tests._tool()
    with pytest.raises(SystemExit) as stop:
        tool.main(["--help"])
    assert stop.value.code == 0
    text = capsys.readouterr().out
    assert "canary-preview" in text and "canary-send" in text
    for command in ("canary-preview", "canary-send"):
        with pytest.raises(SystemExit) as stop:
            tool.main([command, "--help"])
        assert stop.value.code == 0
        text = capsys.readouterr().out
        assert "--activation-id" in text and "--report-root" in text
    assert "--expect-report-sha256" in text


# User-authorized one-off observation; every fixture stays under tmp_path.
OBSERVATION = {"set_id": "quant-set1-20260905", "round_id": "diagnosis-1",
               "repair_of": None, "retest_of": None}


def test_observation_partial_persisted_before_next_call_and_pure_replay(tmp_path, capsys, monkeypatch):
    h = _Harness(tmp_path, capsys)
    digests = h.reports()
    h.arm()
    original = h.transport()
    def transport(request):
        if h.calls:
            receipt = h.analysis_receipt()
            assert ALL_KEYS[0] in receipt['validated']
            assert ALL_KEYS[0] in receipt['partial_cards']
        output = json.loads(original(request))
        if request['evidence_pack']['symbol'] == 'NVDA':
            output['summary'] = 'E01 근거를 검토한다'
        return json.dumps(output)
    code = h.tool.main(['canary-preview', '--report-root', str(h.root), '--activation-id', ACTIVATION],
        environ={}, clock=lambda: NOW, gate_path=h.gate, receipt_root=h.receipt_root,
        transport_factory=h.factory_for(transport), authority_reader=_authority,
        observation=OBSERVATION, **h.paths)
    assert code == h.tool.EXIT_REFUSED
    receipt = h.analysis_receipt()
    assert receipt['status'] == 'FAILED' and 'NVDA' not in receipt['analyses'].get('US', {})
    assert receipt['validated'] == ALL_KEYS[:-1] and 'message_digests' not in receipt
    saved = h.receipts.analysis_path(ACTIVATION).read_bytes()
    def denied(*args, **kwargs):
        raise AssertionError('replay must be pure')
    monkeypatch.setattr(h.tool, 'build_contexts', denied)
    monkeypatch.setattr(h.tool, 'build_live_analyzer_transport', denied)
    texts = qfc.partial_cards_from_receipt(receipt, requested=True)
    assert len(texts) == 3 and all('TEST / PARTIAL / as-of' in text for text in texts)
    assert all("왜 이렇게 보나" in text and 'NVDA' not in text and '/Users/' not in text for text in texts)
    assert texts == qfc.partial_cards_from_receipt(receipt, requested=True)
    assert qfc.partial_cards_from_receipt(receipt) == ()
    assert len(h.calls) == 4 and h.sent == []
    assert h.send(digests)[1]['off'] == 'analysis_not_validated:FAILED'
    assert h.receipts.analysis_path(ACTIVATION).read_bytes() == saved
    from copy import deepcopy
    for mutate in (lambda r: r['validated'].append('US:NVDA'),
                   lambda r: r['analyses']['KR']['000001'].update(summary='tampered'),
                   lambda r: r['partial_cards'][ALL_KEYS[0]].update(text='fallback')):
        bad = deepcopy(receipt)
        mutate(bad)
        with pytest.raises(ValueError):
            qfc.partial_cards_from_receipt(bad, requested=True)
    capsys.readouterr()


@pytest.mark.parametrize('value,field,reason,retained', [
    ('E01 근거', 'summary', 'free_text_number', 'E01 근거'),
    ('E99', 'citation', 'unknown_evidence_id', 'E99'),
    ('  eVidence NVDA E 01\t', 'citation', 'unknown_evidence_id', '  eVidence NVDA E 01\t'),
    ('NVDA earnings rose 4 while SPY held steady.', 'summary', 'free_text_number',
     'NVDA earnings rose 4 while SPY held steady.'),
    ('E01 /Users/synthetic/private/file outlook', 'summary', 'privacy', 'E01 [REDACTED] outlook'),
    ('x' * (qfc.MAX_STATEMENT_CHARS + 1), 'summary', 'statement_text', None),
    ('api_key=synthetic-secret', 'summary', 'privacy', '[REDACTED]'),
    ('account_ref=123456789', 'citation', 'unknown_evidence_id', '[REDACTED]'),
    ([], 'summary', 'statement_text', None),
    (None, 'section', 'section_evidence_relations', None),
    ('not-json synthetic-secret', 'raw', 'JSONDecodeError', None),
])
def test_observation_minimal_private_failure(tmp_path, capsys, value, field, reason, retained):
    h = _Harness(tmp_path, capsys)
    h.reports()
    h.arm()
    def transport(request):
        output = cycle2._wire_good_output(request)
        if field == 'raw':
            return value
        if field == 'citation':
            output['evidence_relations'][0]['evidence_ids'][0] = value
        elif field == 'section':
            output['evidence_relations'] = value
        else:
            output['summary'] = value
        return json.dumps(output)
    h.tool.main(['canary-preview', '--report-root', str(h.root), '--activation-id', ACTIVATION],
        environ={}, clock=lambda: NOW, gate_path=h.gate, receipt_root=h.receipt_root,
        transport_factory=h.factory_for(transport), authority_reader=_authority,
        observation=OBSERVATION, **h.paths)
    receipt = h.analysis_receipt()
    assert receipt['status'] == 'FAILED' and receipt['validated'] == []
    path = h.receipts.observation_path(ACTIVATION)
    assert path.stat().st_mode & 0o777 == 0o600
    private = json.loads(path.read_text())
    assert private['activation_id'] == ACTIVATION and len(private['failures']) == 4
    finding = private['failures'][ALL_KEYS[0]]
    assert finding['class'] == reason and finding['original'] == retained
    omitted = ('no_extracted_field' if field in ('section', 'raw') else
               'nontext' if type(value) is not str else 'oversize') if retained is None else None
    assert finding['original_omitted'] == omitted
    assert finding['original_state'] == ('shape_only' if retained is None else
                                       'exact' if retained == value else 'redacted')
    assert finding['path'] == {'citation': 'evidence_relations[0].evidence_ids[0]',
        'summary': 'summary', 'section': 'evidence_relations', 'raw': '$'}[field]
    assert finding['linkage'] == OBSERVATION
    metadata = receipt['observation']['failures'][ALL_KEYS[0]]
    assert set(metadata) == {'class', 'path', 'linkage', 'code_sha256', 'prompt_sha256', 'validator_sha256'}
    assert all(metadata[key] == finding[key] for key in metadata)
    assert all(len(finding[name]) == 64 for name in ('code_sha256', 'prompt_sha256', 'validator_sha256'))
    assert finding['allowed_ids'] and all(not k.startswith('E') for k in finding['allowed_ids'])
    assert all(item['id'] in finding['allowed_ids'] for item in finding['related_evidence'])
    safe = h.receipts.analysis_path(ACTIVATION).read_text() + capsys.readouterr().out
    assert 'original' not in safe and 'related_evidence' not in safe
    assert receipt['observation']['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    for leak in ('synthetic-secret', 'account_ref', '123456789', 'evidence_pack', 'output_contract'):
        assert leak not in path.read_text() and leak not in safe
    if retained:
        assert retained not in safe


def test_observation_second_oversize_snapshot_keeps_four_validated_analyses(tmp_path, capsys, monkeypatch):
    from copy import deepcopy
    h = _Harness(tmp_path, capsys)
    h.reports()
    h.arm()
    original = h.transport()
    expected = {}
    snapshot = h.tool.partial_card_snapshot
    def capture_snapshot(**kwargs):
        expected[kwargs['key']] = qfc.analysis_record(kwargs['analysis'])
        return snapshot(**kwargs)
    monkeypatch.setattr(h.tool, 'partial_card_snapshot', capture_snapshot)
    def transport(request):
        output = json.loads(original(request))
        if len(h.calls) == 2:
            for section in ('evidence_relations', 'alternatives', 'counterevidence',
                            'assumptions', 'falsifiers', 'change_conditions'):
                output[section] = [dict(output['change_conditions'][0] if section != 'evidence_relations' else output['evidence_relations'][0], statement='근거 확인 ' * 40)
                                   for _ in range(4)]
        return json.dumps(output)
    h.tool.main(['canary-preview', '--report-root', str(h.root), '--activation-id', ACTIVATION],
        environ={}, clock=lambda: NOW, gate_path=h.gate, receipt_root=h.receipt_root,
        transport_factory=h.factory_for(transport), authority_reader=_authority,
        observation=OBSERVATION, **h.paths)
    receipt = h.analysis_receipt()
    assert len(h.calls) == receipt['model_calls'] == 4
    assert receipt['validated'] == ALL_KEYS and receipt['rejected'] == {}
    for key in ALL_KEYS:
        market, symbol = key.split(':')
        assert receipt['analyses'][market][symbol] == expected[key]
    assert receipt['partial_cards'][ALL_KEYS[1]] == {'omitted': 'partial_card_unrenderable'}
    texts = qfc.partial_cards_from_receipt(receipt, requested=True)
    assert texts == tuple(receipt['partial_cards'][key]['text'] for key in ALL_KEYS if key != ALL_KEYS[1])
    assert len(texts) == 3 and h.sent == []
    for mutate in (lambda r: r['partial_cards'].pop(ALL_KEYS[1]),
                   lambda r: r['partial_cards'][ALL_KEYS[1]].update(text='fallback'),
                   lambda r: r['partial_cards'][ALL_KEYS[1]].update(omitted='unknown'),
                   lambda r: r['rejected'].update({ALL_KEYS[1]: 'privacy'})):
        bad = deepcopy(receipt)
        mutate(bad)
        with pytest.raises(ValueError):
            qfc.partial_cards_from_receipt(bad, requested=True)
    capsys.readouterr()


def test_observation_render_failure_keeps_analysis_and_historical_receipt_immutable(tmp_path, capsys, monkeypatch):
    h = _Harness(tmp_path, capsys)
    h.reports()
    h.arm()
    def broken(**kwargs):
        raise ValueError('synthetic_renderer_failure')
    monkeypatch.setattr(h.tool, 'partial_card_snapshot', broken)
    h.tool.main(['canary-preview', '--report-root', str(h.root), '--activation-id', ACTIVATION],
        environ={}, clock=lambda: NOW, gate_path=h.gate, receipt_root=h.receipt_root,
        transport_factory=h.factory_for(h.transport()), authority_reader=_authority,
        observation=OBSERVATION, **h.paths)
    receipt = h.analysis_receipt()
    assert receipt['status'] == 'FAILED' and receipt['validated'] == ALL_KEYS[:1]
    assert receipt['analyses']['KR']['000001']['symbol'] == '000001'
    before = h.receipts.analysis_path(ACTIVATION).read_bytes()
    with pytest.raises(ValueError, match='observation_not_reserved'):
        h.receipts.observe_failure(ACTIVATION, ALL_KEYS[0], {})
    assert h.receipts.analysis_path(ACTIVATION).read_bytes() == before
    assert not h.receipts.observation_path(ACTIVATION).exists()
    with pytest.raises(ValueError):
        qfc.partial_cards_from_receipt(receipt, requested=True)
    capsys.readouterr()


@pytest.mark.parametrize('mutate,reason,path', [
    (lambda o: o.update(extra='opaque-private-body'), 'required_sections', '$'),
    (lambda o: o.update(confidence=[]), 'confidence_shape', 'confidence'),
    (lambda o: o['evidence_relations'][0].update(extra='opaque-private-body'), 'statement_shape', 'evidence_relations[0]'),
    (lambda o: o['evidence_relations'][0].update(evidence_ids=[{}, 'E02']), 'unsupported_claim', 'evidence_relations[0].evidence_ids'),
])
def test_observation_shapes_do_not_capture_body(mutate, reason, path):
    from tokmo.programs import quant_contextual_analysis as q
    pack = cycle2._pack()
    output = cycle2._wire_good_output(q.build_analyzer_request(pack))
    mutate(output)
    findings = []
    analyzer = qfc.capped_analysis(lambda request: json.dumps(output), reserve=lambda key: None,
        settle=lambda key, status: None, captured={}, observe=lambda key, finding: findings.append(finding))
    assert q.run_contextual_analysis(pack, analysis=analyzer) == (None, 'invalid_output:' + reason)
    assert findings[0]['path'] == path and findings[0]['original'] is None
    assert 'opaque-private-body' not in json.dumps(findings)


@pytest.mark.parametrize('field', ['citation', 'summary', 'repeated_prefix'])
def test_observation_unknown_latin_is_diagnosable_without_semantic_acceptance(field):
    from tokmo.programs import quant_contextual_analysis as q
    pack = cycle2._pack()
    output = cycle2._wire_good_output(q.build_analyzer_request(pack))
    if field == 'citation':
        output['evidence_relations'][0]['evidence_ids'][0] = 'xYzAbCdEfGhIjKlMnOp'
    elif field == 'repeated_prefix':
        # Synthetic spelling; never copy the retained private citation.
        output['change_conditions'][0]['evidence_ids'] = ['E01', 'EE02']
    else:
        output['summary'] = 'xYzAbCdEfGhIjKlMnOp 수치 4'
    findings = []
    analyzer = qfc.capped_analysis(lambda request: json.dumps(output), reserve=lambda key: None,
        settle=lambda key, status: None, captured={}, observe=lambda key, finding: findings.append(finding))
    expected = 'free_text_number' if field == 'summary' else 'unknown_evidence_id'
    assert q.run_contextual_analysis(pack, analysis=analyzer)[1] == 'invalid_output:' + expected
    originals = {'citation': 'xYzAbCdEfGhIjKlMnOp', 'summary': 'xYzAbCdEfGhIjKlMnOp 수치 4',
                 'repeated_prefix': 'EE02'}
    assert findings[0]['original'] == originals[field]
    assert findings[0]['original_state'] == 'exact'
    if field == 'repeated_prefix':
        assert findings[0]['path'] == 'change_conditions[0].evidence_ids[1]'
        assert findings[0]['original_omitted'] is None
        assert findings[0]['allowed_ids'] == sorted(q._citation_map(pack))


@pytest.mark.parametrize('value,expected', [
    ('NVDA E01 token demand', 'NVDA E01 token demand'),
    ('E01 Bearer synthetic-access-token outlook', 'E01 [REDACTED] outlook'),
    ('E01 sk-syntheticcredentialvalue outlook', 'E01 [REDACTED] outlook'),
    ('E01 account_id: abc-xyz outlook', 'E01 [REDACTED] outlook'),
    ('E01 account_id abc-xyz outlook', 'E01 [REDACTED] outlook'),
    ('E01 private/file outlook', 'E01 [REDACTED] outlook'),
    ('E01 holdings: NVDA 44; outlook', 'E01 [REDACTED]; outlook'),
    ('E01 ../private/file outlook', 'E01 [REDACTED] outlook'),
    (r'E01 C:\Users\synthetic\file outlook', 'E01 [REDACTED] outlook'),
    ('E01 https://example.invalid/private outlook', 'E01 [REDACTED] outlook'),
    ('E01 owner-fixture quantity 9876.5 outlook', 'E01 [REDACTED] quantity [REDACTED] outlook'),
])
def test_observation_minimal_redaction_of_synthetic_sensitive_values(value, expected):
    from tokmo.programs import quant_contextual_analysis as q
    pack = cycle2._pack()
    pack['evidence'].append({'id': 'holding:synthetic', 'kind': 'holding',
                             'label': 'synthetic holding', 'scope_label': 'owner-fixture', 'quantity': '9876.5'})
    output = cycle2._wire_good_output(q.build_analyzer_request(pack))
    output['summary'] = value
    findings = []
    analyzer = qfc.capped_analysis(lambda request: json.dumps(output), reserve=lambda key: None,
        settle=lambda key, status: None, captured={}, observe=lambda key, finding: findings.append(finding))
    analysis, rejection = q.run_contextual_analysis(pack, analysis=analyzer)
    assert analysis is None and rejection.startswith('invalid_output:')
    finding = findings[0]
    assert finding['original'] == expected
    assert finding['original_state'] == ('exact' if value == expected else 'redacted')
    assert finding['original_omitted'] is None
    assert all(row['id'] != 'holding:synthetic' for row in finding['related_evidence'])


@pytest.mark.parametrize('value,expected', [
    ('평균 71500원 수량 30주', '평균 [REDACTED]원 수량 [REDACTED]주'),
    ('금액 123456789원 수량 987654321주', '금액 [REDACTED]원 수량 [REDACTED]주'),
    ('평균 9876.5원', '평균 [REDACTED]원'),
    ('E71500 30E _71500 30_ .71500 30.5', 'E71500 30E _71500 30_ .71500 30.5'),
    ('E123456789 123456789E _123456789 123456789_ .123456789 123456789.5',
     'E123456789 123456789E _123456789 123456789_ .123456789 123456789.5'),
])
def test_observation_korean_numeric_boundaries(value, expected):
    from tokmo.programs import quant_contextual_analysis as q
    pack = cycle2._pack()
    pack['evidence'].append({'id': 'holding:synthetic', 'kind': 'holding',
                             'label': 'synthetic holding', 'avg_price': 71500,
                             'quantity': 30, 'total_weight_pct': '9876.5'})
    output = cycle2._wire_good_output(q.build_analyzer_request(pack))
    output['summary'] = value
    findings = []
    analyzer = qfc.capped_analysis(lambda request: json.dumps(output), reserve=lambda key: None,
        settle=lambda key, status: None, captured={}, observe=lambda key, finding: findings.append(finding))
    analysis, rejection = q.run_contextual_analysis(pack, analysis=analyzer)
    assert analysis is None and rejection.startswith('invalid_output:')
    finding = findings[0]
    assert finding['original'] == expected
    assert finding['original_state'] == ('exact' if value == expected else 'redacted')
    assert finding['original_omitted'] is None
    assert all(row['id'] != 'holding:synthetic' for row in finding['related_evidence'])


@pytest.mark.parametrize('field', ['summary', 'counterevidence', 'evidence_relations'])
def test_prompt_distinguishes_input_labels_from_prose_permission(field):
    # Synthetic source-backed fallback label, never a retained private sentence.
    pack = cycle2._pack()
    pack['evidence'].append({'id': 'f:volume_change_20', 'kind': 'factor',
                             'label': 'volume_change_20', 'value': '-0.25',
                             'orientation': 'higher_is_better'})
    request = build_analyzer_request(pack)
    output = cycle2._wire_good_output(request)
    wire_id = qca._citation_map(pack)['f:volume_change_20']
    if field == 'summary':
        output[field] = 'volume_change_20 지표를 확인함'
    else:
        output[field][0]['statement'] = 'volume_change_20 지표를 확인함'
        output[field][0]['evidence_ids'] = [wire_id]
        if field == 'evidence_relations':
            output[field][0]['evidence_ids'].append(qca._citation_map(pack)['f:ret_20'])
    adapter = qca.analyzer_adapter(lambda request: json.dumps(output))
    assert _rejects(adapter(pack), pack) == 'free_text_number'
    if field == 'summary':
        output[field] = '거래량 감소를 확인함'
    else:
        output[field][0]['statement'] = '거래량 감소를 확인함'
    assert validate_analysis(adapter(pack), pack) is not None
    # The producer's evidence and exact wire citations remain intact.
    assert next(e for e in request['evidence_pack']['evidence']
                if e['id'] == wire_id)['label'] == 'volume_change_20'
    rules = '\n'.join(qfc.analyzer_prompt_rules(request))
    assert '입력 label·필드명은 서술 허용 지표명 목록이 아니다' in rules
    assert '숫자가 붙은 내부 지표명을 그대로 복사하지 않는다' in rules
    assert '해당 지표의 의미를 숫자 없는 정성 표현으로 설명하고 evidence_ids에 인용한다' in rules


def test_prompt_requires_source_meaning_beyond_citation_membership():
    rules = '\n'.join(qfc.analyzer_prompt_rules(build_analyzer_request(cycle2._pack())))
    assert '허용 id 인용만으로 문장의 의미가 입증되지는 않는다' in rules
    assert '중립 RSI를 과매도 근거로 바꾸지 않는다' in rules
    assert 'MACD의 음수는 부채가 아니라 지표의 부호' in rules


def test_actionable_v2_codec_keeps_v1_record_bytes_and_partial_lines():
    from tokmo.programs.quant_contextual_analysis import validate_analysis
    from tokmo.programs.quant_telegram_card import _analysis_lines
    pack = cycle2._pack()
    analysis = validate_analysis(cycle2._actionable_output(pack), pack)
    record = qfc.analysis_record(analysis)
    assert record['display_version'] == 2
    assert qfc.analysis_record(qfc.analysis_from_record(record)) == record
    # Existing v1 has string labels and no added version key: round trip stays exact.
    legacy = {**record, 'labels': {key: value['label'] for key, value in record['labels'].items()},
              'conditional_order_assessment': {'verdict': '해당 없음', **cycle2._statement('과거 판단', 'f:ret_120')}}
    legacy.pop('display_version')
    old = qfc.analysis_from_record(legacy)
    assert qfc.analysis_record(old) == legacy
    assert _analysis_lines(old)[0] == ANALYSIS_LABEL


@pytest.mark.parametrize('value,actual,flags,level_type,in_enum,extra', [
    (None, 'null', None, None, None, None), ('secret-value', 'str', None, None, None, None),
    ([{'secret-key': 'secret-value'}], 'list', None, None, None, None),
    (True, 'bool', None, None, None, None), (7, 'int', None, None, None, None),
    (7.5, 'float', None, None, None, None),
    ({}, 'dict', (False, False, False), 'missing', None, 0),
    ({'statement': 'safe'}, 'dict', (False, True, False), 'missing', None, 0),
    *[({'level': value, 'evidence_ids': [], 'secret-key': {'nested': 'secret-value'}},
       'dict', (True, False, True), kind, False, 1)
      for value, kind in [(None, 'null'), ([], 'list'), ({}, 'dict'), (True, 'bool'),
                          (7, 'int'), (7.5, 'float'), ('secret-value', 'str')]],
    ('missing', 'missing', None, None, None, None),
    ('missing-extra', 'missing', None, None, None, None),
])
def test_observation_confidence_fixed_shape(value, actual, flags, level_type, in_enum, extra):
    from tokmo.programs import quant_contextual_analysis as q
    pack = cycle2._pack()
    output = cycle2._wire_good_output(q.build_analyzer_request(pack))
    output['confidence'] = value
    if actual == 'missing':
        del output['confidence']
        if value == 'missing-extra':
            output.pop('summary')
            output['secret-key'] = 'secret-value'
    findings = []
    analyzer = qfc.capped_analysis(lambda request: json.dumps(output), reserve=lambda key: None,
        settle=lambda key, status: None, captured={}, observe=lambda key, finding: findings.append(finding))
    reason, path = ('required_sections', '$') if actual == 'missing' else ('confidence_shape', 'confidence')
    assert q.run_contextual_analysis(pack, analysis=analyzer) == (None, 'invalid_output:' + reason)
    assert len(findings) == 1
    finding = findings[0]
    assert (finding['class'], finding['path'], finding['original'], finding['original_state'],
            finding['original_omitted']) == (reason, path, None, 'shape_only', 'no_extracted_field')
    assert finding['confidence_shape'] == {'actual_type': actual,
        'expected_keys_present': dict(zip(('level', 'statement', 'evidence_ids'), flags or (None,) * 3)),
        'unexpected_key_count': extra, 'level_present': flags[0] if flags else None,
        'level_type': level_type, 'level_in_enum': in_enum}
    assert 'text_shape' not in finding and 'secret-' not in json.dumps(finding)


@pytest.mark.parametrize('value,kind,length,blank', [
    (None, 'null', None, None), ([{'secret-key': 'secret-value'}], 'list', None, None),
    ({'secret-key': 'secret-value'}, 'dict', None, None), (False, 'bool', None, None),
    (7, 'int', None, None), (7.5, 'float', None, None),
    ('secret-value' * 30, 'str', 360, False), (' ' * 241, 'str', 241, True),
    ('', None, 0, True), (' \t', None, 2, True), ('\v', 'str', 1, True),
])
def test_observation_text_shape_first_failure(value, kind, length, blank):
    from tokmo.programs import quant_contextual_analysis as q
    pack = cycle2._pack()
    output = cycle2._wire_good_output(q.build_analyzer_request(pack))
    output['change_conditions'].append(dict(output['change_conditions'][0], statement=value))
    output['confidence'] = None
    findings = []
    analyzer = qfc.capped_analysis(lambda request: json.dumps(output), reserve=lambda key: None,
        settle=lambda key, status: None, captured={}, observe=lambda key, finding: findings.append(finding))
    assert q.run_contextual_analysis(pack, analysis=analyzer) == (None, 'invalid_output:statement_text')
    assert len(findings) == 1
    finding = findings[0]
    assert finding['path'] == 'change_conditions[1].statement' and 'confidence_shape' not in finding
    assert finding.get('text_shape') == (None if kind is None else
        {'actual_type': kind, 'length': length, 'blank': blank})
    assert finding['original'] == ('[REDACTED]' if value == '\v' else value if kind is None else None)
    assert finding['original_state'] == ('redacted' if value == '\v' else 'exact' if kind is None else 'shape_only')
    assert finding['original_omitted'] == (None if kind is None or value == '\v' else 'oversize' if kind == 'str' else 'nontext')
    assert 'secret-' not in json.dumps(finding)


@pytest.mark.parametrize('case', ['text', 'confidence', 'extra', 'statement', 'valid', 'nonobject', 'conditional', 'top-extra', 'other'])
def test_observation_validator_root_and_fallback(case):
    from tokmo.programs import quant_contextual_analysis as q
    pack = cycle2._pack()
    output = cycle2._wire_good_output(q.build_analyzer_request(pack))
    if case == 'text':
        output['change_conditions'].append(dict(output['change_conditions'][0], statement=[]))
    elif case == 'confidence':
        output['confidence'] = None
    elif case == 'extra':
        output['confidence']['secret-key'] = 'secret-value'
    elif case == 'top-extra':
        output['secret-key'] = 'secret-value'
    elif case == 'statement':
        del output['change_conditions'][0]['statement']
    elif case == 'conditional':
        pack['evidence'].append({'id': 'order:synthetic', 'kind': 'conditional_order', 'side': 'unknown'})
        output['summary'] = '조건주문 매수 방향을 추론한다'
    parsed = q.analyzer_adapter(lambda request: json.dumps(output))(pack)
    assert parsed is not output
    if case == 'nonobject':
        parsed = []
    elif case == 'other':
        parsed['confidence'] = object()
    try:
        q.validate_analysis(parsed, pack)
    except q.AnalysisValidationError as error:
        finding = qfc.failure_observation(error, {'summary': 'unrelated'}, pack, 'synthetic')
        expected = {'text': 'change_conditions[1].statement', 'confidence': 'confidence',
                    'extra': 'confidence', 'statement': 'change_conditions[0]', 'nonobject': '$',
                    'conditional': 'summary', 'top-extra': '$', 'other': 'confidence'}
        assert finding['path'] == expected[case]
        assert ('confidence_shape' in finding) == (case in ('confidence', 'other'))
        assert ('text_shape' in finding) == (case == 'text')
        if case == 'other':
            assert finding['confidence_shape']['actual_type'] == 'other'
        if case == 'conditional':
            assert finding['original'] == output['summary']
    else:
        assert case == 'valid'
    fallback = qfc.failure_observation(q.AnalysisValidationError('conditional_side_inferred'),
                                      {'summary': 'fallback'}, pack, 'synthetic')
    assert fallback['original'] == 'fallback'
    for reason in ('confidence_shape', 'required_sections', 'statement_text'):
        fallback = qfc.failure_observation(q.AnalysisValidationError(reason), {}, pack, 'synthetic')
        assert 'confidence_shape' not in fallback and 'text_shape' not in fallback


def test_observation_old_sidecar_append_private_projection(tmp_path, capsys):
    h = _Harness(tmp_path, capsys)
    h.receipts.write_analysis(ACTIVATION, {'activation_id': ACTIVATION, 'status': 'RESERVED', 'cards': ALL_KEYS})
    old = dict(class_='confidence_shape', path='confidence', original=None, original_state='shape_only',
               original_omitted='no_extracted_field', linkage=OBSERVATION,
               code_sha256='a' * 64, prompt_sha256='b' * 64, validator_sha256='c' * 64)
    old['class'] = old.pop('class_')
    h.receipts.observe_failure(ACTIVATION, ALL_KEYS[0], old)
    from tokmo.programs import quant_contextual_analysis as q
    pack = cycle2._pack()
    output = cycle2._wire_good_output(q.build_analyzer_request(pack))
    output['confidence'] = {'level': 'secret-value', 'secret-key': ['secret-value']}
    analyzer = qfc.capped_analysis(lambda request: json.dumps(output), reserve=lambda key: None,
        settle=lambda key, status: None, captured={}, observe=lambda key, finding:
            h.receipts.observe_failure(ACTIVATION, ALL_KEYS[1], dict(old, **finding)))
    assert q.run_contextual_analysis(pack, analysis=analyzer) == (None, 'invalid_output:confidence_shape')
    private = h.receipts.observation_path(ACTIVATION)
    rows = json.loads(private.read_text())['failures']
    assert rows[ALL_KEYS[0]] == old and rows[ALL_KEYS[1]]['confidence_shape']['level_in_enum'] is False
    assert private.stat().st_mode & 0o777 == 0o600
    public = h.analysis_receipt()['observation']['failures']
    assert all(set(row) == {'class', 'path', 'linkage', 'code_sha256', 'prompt_sha256', 'validator_sha256'}
               for row in public.values())
    safe = h.receipts.analysis_path(ACTIVATION).read_text() + capsys.readouterr().out
    assert 'confidence_shape' not in json.dumps([list(row) for row in public.values()])
    assert 'text_shape' not in safe and 'actual_type' not in safe and 'secret-' not in safe + private.read_text()
    assert h.sent == []


@pytest.mark.parametrize("opt", [{}, {"canary_json_mode": True}])
def test_canary_json_mode_real_factory_and_authority(tmp_path, capsys, monkeypatch, opt):
    h = _Harness(tmp_path, capsys)
    h.reports(); h.arm()
    routing, quota = tmp_path / "routing.md", tmp_path / "quota.json"
    routing.write_text(FLASH_ONLY_DOC + EXACT_ROUTE_LINE)
    quota.write_text(json.dumps(_quota()))
    calls, factory = [], h.tool.build_live_analyzer_transport
    def spy(**kwargs):
        calls.append(kwargs)
        factory(**kwargs)  # real construction; no credential read or model call
        return h.transport()
    monkeypatch.setattr(h.tool, "build_live_analyzer_transport", spy)
    code = h.tool.main(["canary-preview", "--report-root", str(h.root), "--activation-id", ACTIVATION,
        "--routing-state", str(routing), "--quota-latest", str(quota)], environ=ENV, clock=lambda: NOW,
        gate_path=h.gate, receipt_root=h.receipt_root, canary_conditional_live=False, **opt, **h.paths)
    assert code == h.tool.EXIT_OK and len(calls) == 1
    assert calls[0]["json_mode"] is opt.get("canary_json_mode", False)
    assert h.analysis_receipt()["route_authority"]["routing_state"]["sha256"] == hashlib.sha256(routing.read_bytes()).hexdigest()
