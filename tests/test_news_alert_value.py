"""Behavioral contracts with declared fake judgments; these do not measure model quality."""
from datetime import timedelta
from io import BytesIO
from zipfile import ZipFile

import pytest

from tokmo.facts.opendart_document_live import OpenDartDocumentReader, parse_document
from tokmo.programs.news_decision_engine import model_evidence
from tokmo.records.news_decision_store import NewsDecisionStore, kst
from test_news_decision_engine import Engine, observation, decision, screen, followup
from test_news_decision_s3 import DAY, scene, tick, update, send_all
from tokmo.programs.news_decision_outbox import queue_proposals, reconcile_deliveries
from test_news_decision_s3 import ROLE

RECEIPT = "20260907900123"


def listing(receipt=RECEIPT):
    return dict(observation("dart:" + receipt + ":first:abc", DAY, "DISCLOSURE"),
                title="새 로봇 기업 유상증자", text="제목 목록만 확인", content_scope="listing_only", body_verified=False,
                stock_code=None, publication_date="20260907", published_at=None)


def deferred(ids, **extra):
    return decision(ids, notify=False, importance="MEDIUM", urgency="NORMAL", confirmation="OFFICIAL",
                    why_now="관심 후보지만 아직 규모와 용도를 확인하지 못함", next_check="공시 원문의 조달 규모와 자금 용도 확인") | extra


def test_important_but_thin_first_discovery_survives_restart_and_enrichment(tmp_path):
    row = listing()
    store = NewsDecisionStore(tmp_path / "state.db")
    calls = []
    body = "공장 자동화 로봇을 만드는 신규 기업. 200억원을 생산 설비 증설과 개발에 사용한다고 공시했다."
    engine = Engine(store, screen, lambda req: deferred([row["id"]]),
                    document_reader=lambda receipt: calls.append(receipt) or {"text": body})
    before = engine.tick([row], DAY)
    assert not before["alerts"] and not before["failures"] and calls == [RECEIPT]
    event = store.events()[0]
    assert event["decision"]["importance"] == "MEDIUM" and event["decision"]["change"] == "NEW"
    assert event["source_enrichment"]["status"] == "READY" and len(event["evidence"]) == 2
    original, full = event["evidence"]
    assert original["text"] == row["text"] and not original["body_verified"]
    assert full["body_verified"] and full["parent_evidence_id"] == row["id"] and full["observed_at"] == kst(DAY)
    store.close()
    store = NewsDecisionStore(tmp_path / "state.db")
    seen = []
    engine = Engine(store, screen, lambda req: seen.append(req) or decision([full["id"]], importance="MEDIUM", urgency="NORMAL",
        title="새 로봇 기업, 200억원 생산 설비·개발 투자 공시", confirmation="OFFICIAL", facts=body,
        interpretation="이번 조달과 생산 확대 계획이 연결돼 새 관심 후보를 이해할 근거가 생겼다"))
    result = engine.tick([], DAY + timedelta(minutes=1))
    assert len(result["alerts"]) == 1 and not result["failures"]
    assert seen[0]["review_reason"] == "PERIODIC_RECHECK" and any(item["id"] == full["id"] for item in seen[0]["evidence"])
    assert "200억원" in result["alerts"][0]["body"] and "상장" not in result["alerts"][0]["body"]
    store.close()


def test_enrichment_failure_is_visible_and_waits_for_existing_due_time(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    calls = []
    def unavailable(receipt):
        calls.append(receipt)
        raise ValueError("document_unavailable_013")
    row = listing()
    engine = Engine(store, screen, lambda req: deferred([row["id"]]), document_reader=unavailable)
    result = engine.tick([row], DAY)
    event = store.events()[0]
    assert not result["alerts"] and event["source_enrichment"]["reason"] == "document_unavailable_013"
    assert event["decision"]["open"] and len(event["evidence"]) == 1
    # New worker instance still cannot repeat the lookup before the event's ordinary 24h recheck.
    engine = Engine(store, screen, lambda req: deferred([row["id"]]), document_reader=unavailable)
    assert engine.tick([], DAY + timedelta(minutes=1))["calls"] == 0
    engine.tick([], DAY + timedelta(days=1))
    assert calls == [RECEIPT, RECEIPT] and len(store.events()[0]["evidence"]) == 1
    store.close()


def test_low_thin_candidate_can_be_enriched_without_forcing_its_importance(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    row = listing()
    reads = []
    engine = Engine(store, screen, lambda req: deferred([row["id"]], importance="LOW"),
                    document_reader=lambda receipt: reads.append(receipt) or {"text": "운영자금 230억 원, 기존 두 주주가 비례 참여하며 지분율 변동 없음"})
    first = engine.tick([row], DAY)
    event = store.events()[0]
    assert not first["alerts"] and reads == [RECEIPT]
    assert event["decision"]["importance"] == "LOW" and event["source_enrichment"]["status"] == "READY"
    seen = []
    engine.analyze = lambda req: seen.append(req) or decision([event["evidence"][-1]["id"]], importance="MEDIUM", facts="운영자금 230억 원")
    result = engine.tick([], DAY + timedelta(minutes=1))
    assert len(result["alerts"]) == 1 and len(seen) == 2 and not result["failures"]
    assert len(reads) == 1 and seen[1]["correction"]["source_grounding_review"]
    store.close()


def test_same_document_reused_by_another_candidate_keeps_first_observation(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    for minute in range(2):
        row = listing() | {"id": "dart:" + RECEIPT + ":" + str(minute) + ":abc", "observed_at": kst(DAY + timedelta(minutes=minute))}
        engine = Engine(store, lambda req: {"candidates": [{"evidence_ids": [row["id"]], "event_id": None, "comparison_event_ids": [], "event_reason": "같은 문서의 별개 분석 사례"}], "ignored": []},
            lambda req: deferred([req["evidence"][0]["id"]]), document_reader=lambda receipt: {"text": "같은 공시 원문"})
        engine.tick([row], DAY + timedelta(minutes=minute))
    documents = [e["evidence"][-1] for e in store.events()]
    assert len(documents) == 2 and documents[0] == documents[1]
    assert documents[0]["observed_at"] == kst(DAY) and store.counts() == {"DONE": 3}
    store.close()


def test_source_read_cap_is_across_ticks_and_does_not_require_holdings(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    calls = []
    engine = Engine(store, lambda req: {"candidates": [{"evidence_ids": [req["evidence"][0]["id"]], "event_id": None, "comparison_event_ids": [], "event_reason": "기존 공시와 별개의 공시"}], "ignored": []},
                    lambda req: deferred([req["evidence"][0]["id"]]), document_reader=lambda receipt: calls.append(receipt) or {"text": "검증한 원문"})
    engine.tick([listing()], DAY)
    engine.tick([listing("20260907900456")], DAY + timedelta(minutes=1))
    assert len(calls) == 1 and len(store.events()) == 2
    assert any(event.get("source_enrichment", {}).get("status") == "PASS_LIMIT" for event in store.events())
    store.close()


def test_notify_false_never_cancels_pending_and_useful_medium_update_stays_possible(tmp_path):
    store, ops, engine = scene(tmp_path)
    first = tick(engine, "a", 0)["alerts"][0]
    held = tick(engine, "b", 1, update(("b",), notify=False, importance="HIGH"))
    assert not held["alerts"] and engine.pending_alerts()[0]["id"] == first["id"]
    send_all(engine, ops, DAY + timedelta(minutes=2))
    result = tick(engine, "c", 3, update(("c",), notify=True, importance="MEDIUM", urgency="NORMAL"))
    assert len(result["alerts"]) == 1 and result["alerts"][0]["importance"] == "MEDIUM"
    assert "b" in store.events()[0]["unsent_evidence"]["ids"]
    store.close(); ops.close()


def test_false_then_first_release_without_new_evidence_is_not_permanently_lost(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    engine = Engine(store, screen, lambda req: decision(notify=False))
    assert not engine.tick([observation(at=DAY)], DAY)["alerts"]
    engine.analyze = lambda req: decision(change="NONE", notify=True)
    assert len(engine.tick([], DAY + timedelta(hours=6))["alerts"]) == 1
    store.close()


def test_earlier_completed_latest_proposal_is_not_false_pending_after_late_old_send(tmp_path):
    store, ops, engine = scene(tmp_path)
    a = tick(engine, "a", 0)["alerts"][0]
    queue_proposals(engine, ops, ROLE, cutoff=DAY, activation_enabled=True)
    b = tick(engine, "b", 1, update(("b",)))["alerts"][0]
    queue_proposals(engine, ops, ROLE, cutoff=DAY, activation_enabled=True)
    refs = store.events()[0]["delivery_refs"]
    for _ in range(2):
        ops.claim_pending_telegram(ROLE, DAY + timedelta(minutes=2))
    ops.mark_sent(ROLE, refs[b["id"]][0], DAY + timedelta(minutes=3), "101")
    reconcile_deliveries(engine, ops)
    ops.mark_sent(ROLE, refs[a["id"]][0], DAY + timedelta(minutes=4), "102")
    reconcile_deliveries(engine, ops)
    event = store.events()[0]
    assert event["last_sent"]["alert_id"] == a["id"] and event["last_alert"]["id"] == b["id"]
    assert not engine._pending_bodies(event) and event["completed_proposal_id"] == b["id"]
    store.close(); ops.close()


@pytest.mark.parametrize("bad", [None, "true", 1])
def test_notify_contract_gets_only_one_correction_then_visible_error(tmp_path, bad):
    store = NewsDecisionStore(tmp_path / "state.db")
    seen = []
    engine = Engine(store, screen, lambda req: seen.append(req) or decision(notify=bad))
    result = engine.tick([observation(at=DAY)], DAY)
    assert len(seen) == 2 and result["input_states"] == {"ERROR": 1} and not result["alerts"]
    assert seen[1]["correction"]["reason"] == "notify must be boolean"
    store.close()


@pytest.mark.parametrize("broken", [
    {"candidates": [], "ignored": ["dart:20260907900123:first:abc"]},
    {"candidates": [{"evidence_ids": ["dart:20260907900123:first:def"]}], "ignored": []},
])
def test_exact_id_and_ignored_shape_correction_contains_allowlist(tmp_path, broken):
    store = NewsDecisionStore(tmp_path / "state.db")
    row = listing()
    seen = []
    def classify(req):
        seen.append(req)
        if len(seen) == 1:
            return broken
        repair = req["correction"]
        assert repair["allowed_evidence_ids"] == [row["id"]]
        assert "response_shape" in repair and repair["allowed_event_ids"] == []
        return {"candidates": [], "ignored": [{"evidence_id": row["id"], "reason": "근거 부족"}]}
    result = Engine(store, classify, lambda req: pytest.fail("unexpected analysis")).tick([row], DAY)
    assert not result["failures"] and result["input_states"] == {"DONE": 1} and len(seen) == 2
    store.close()


def zipped(entries):
    data = BytesIO()
    with ZipFile(data, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return data.getvalue()


def test_official_xml_preserves_labels_table_values_and_marks_model_clipping():
    raw = zipped({RECEIPT + ".xml": "<DOCUMENT><TITLE>조달</TITLE><TR><TD>용도</TD><TD>설비 증설</TD></TR></DOCUMENT>"})
    result = parse_document(raw, RECEIPT)
    assert result["text"] == "조달\n용도\n설비 증설" and len(result["source_document_sha256"]) == 64
    view = model_evidence([dict(listing(), content_scope="original_document", text="x" * 12001)], text_limit=4000)[0]
    assert len(view["text"]) == 12000 and view["text_truncated"]
    with pytest.raises(ValueError, match="013"):
        parse_document(b"<result><status>013</status><message>no data</message></result>", RECEIPT)
    with pytest.raises(ValueError, match="ambiguous"):
        parse_document(zipped({"one.xml": "<x/>", "two.xml": "<x/>"}), RECEIPT)
    with pytest.raises(Exception):
        parse_document(zipped({RECEIPT + ".xml": '<!DOCTYPE x [<!ENTITY x "hidden">]><x>&x;</x>'}), RECEIPT)


def test_official_reader_is_lazy_fixed_get_and_sanitizes_provider_errors():
    touched = []
    class Opener:
        def open(self, req, timeout):
            from urllib.parse import urlsplit, parse_qs
            target = urlsplit(req.full_url)
            assert target.hostname == "opendart.fss.or.kr" and target.path == "/api/document.xml"
            assert parse_qs(target.query)["rcept_no"] == [RECEIPT] and req.method == "GET"
            touched.append("wire")
            raise OSError("provider URL includes private-example-key")
    reader = OpenDartDocumentReader(lambda: touched.append("auth") or "private-example-key", opener_factory=Opener)
    assert touched == []
    with pytest.raises(ValueError, match="invalid_document_receipt"):
        reader("https://example.invalid/")
    assert touched == []
    with pytest.raises(ValueError) as caught:
        reader(RECEIPT)
    assert str(caught.value) == "document_transport_unavailable" and touched == ["auth", "wire"]
