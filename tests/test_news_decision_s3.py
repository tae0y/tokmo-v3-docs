"""S3: compare with the actual delivered body and relevant proposal bodies, not with ids.

Every model response here is a stub: these cases verify wiring (what reaches the model, what the
engine does with a structurally valid answer). None of them proves model quality. Historical cases
replay real recorded rows from a private pin (gitignored); they skip when the pin is absent.
"""
from datetime import timedelta
import json
from pathlib import Path

import pytest

from tokmo.programs.news_decision_engine import NewsDecisionEngine, doc_key, evidence_delta, validate_decision
from tokmo.programs.news_decision_outbox import queue_proposals, reconcile_deliveries
from tokmo.records.news_decision_store import NewsDecisionStore, kst, timestamp
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import ProducerRole
from test_news_decision_engine import Engine, NOW, decision, deliver, observation, screen

ROLE = ProducerRole.HERMES
DAY = timestamp("2026-09-01T10:00:00+09:00")  # daytime: nothing is held for the morning
PIN = Path(__file__).resolve().parents[1] / ".hermes/evidence/news-time-event-20260907-s3/historical-20260907.json"
pinned = pytest.mark.skipif(not PIN.exists(), reason="private historical pin absent (.hermes/evidence is gitignored; no data is invented)")


def material(*ids, prior="이전 전달 주장", new="정정된 해석", why="기준선 본문이 다루지 않음"):
    return {"prior_claim": prior, "new_fact": new, "support_ids": list(ids), "why_not_covered": why}


def update(ids, *support, **over):
    return decision(ids) | {"change": "UPDATE", "material_change": material(*(support or ids))} | over


def none(ids):
    return decision(ids) | {"change": "NONE", "material_change": None}


def scene(tmp_path, analyze=None, **kw):
    store = NewsDecisionStore(tmp_path / "state.db")
    ops = OpsKernelStore(tmp_path / "ops.db", stable_phase_ids=frozenset())
    return store, ops, Engine(store, screen, analyze or (lambda request: decision()), **kw)


def send_all(engine, ops, at):
    queue_proposals(engine, ops, ROLE, cutoff=DAY - timedelta(days=1), activation_enabled=True)
    deliver(ops, engine, at=at)
    reconcile_deliveries(engine, ops)


def tick(engine, key, minutes, response=None, at=DAY):
    if response is not None:
        engine.analyze = lambda request: response
    return engine.tick([observation(key, at + timedelta(minutes=minutes))], at + timedelta(minutes=minutes))


def test_doc_key_is_provenance_only():
    assert doc_key("news:abc:7") == "news:abc" and doc_key("dart:20260907000125:first:0a1b") == "dart:20260907000125"
    assert doc_key("dart:20260907000126:first:0a1b") != doc_key("dart:20260907000125:first:0a1b") and doc_key("w1") == "w1"


def test_same_time_receipts_keep_both_bodies_and_latest_proposal(tmp_path):
    store,ops,engine=scene(tmp_path)
    first=tick(engine,'a',0)['alerts'][0]
    send_all(engine,ops,DAY)
    second=tick(engine,'b',0,update(('a','b'),'b',facts='새로운 변화'))['alerts'][0]
    send_all(engine,ops,DAY)
    event=store.events()[0]
    assert [r['alert_id'] for r in event['delivered_history']]==[first['id'],second['id']]
    assert event['last_sent']['alert_id']==second['id']
    reconcile_deliveries(engine,ops)
    assert store.events()[0]['delivered_history']==event['delivered_history']
    store.close();ops.close()


def test_delta_reads_stored_originals_and_names_its_basis():
    old = observation("news:d:1") | {"title": "휴전 합의", "text": "본문 " * 1500 + "끝"}
    new = dict(old, id="news:d:2", title="합의 철회·공격 재개")  # same url/publisher/text: a title-only revision
    far = dict(new, id="news:d:3", text=old["text"][:-1] + "수정")
    delta = {row["id"]: row for row in evidence_delta([old, new, far], [old, new, far], ["news:d:1"])}
    assert delta["news:d:2"]["version_prev"] == "news:d:1" and delta["news:d:2"]["version_prev_basis"] == "DELIVERED"
    assert delta["news:d:2"]["changed_fields"] == ["title"] and delta["news:d:2"]["title_prev"] == "휴전 합의" and not delta["news:d:2"]["cited_in_delivered"]
    # Delivered-relative and observation-relative deltas are separate; a change past 4000 chars is excerpted, not just flagged.
    assert delta["news:d:3"]["version_prev"] == "news:d:1" and delta["news:d:3"]["changed_fields"] == ["text", "title"]
    assert delta["news:d:3"]["observed_prev"] == "news:d:2" and delta["news:d:3"]["changed_since_observed"] == ["text"]
    assert delta["news:d:3"]["text_change"]["visible_in_model_text"] is False and "수정" in delta["news:d:3"]["text_change"]["new"]
    assert delta["news:d:1"]["cited_in_delivered"] is True and delta["news:d:1"]["version_prev_basis"] == "NONE"
    unknown = evidence_delta([new], [old, new], "UNKNOWN")[0]
    assert unknown["cited_in_delivered"] == "UNKNOWN" and unknown["version_prev_basis"] == "OBSERVATION" and unknown["changed_fields"] == ["title"]


def test_baseline_makes_material_change_mandatory_even_for_new_and_old_ids_never_suppress(tmp_path):
    store, ops, engine = scene(tmp_path)
    first = tick(engine, "a", 0)["alerts"][0]
    send_all(engine, ops, DAY + timedelta(minutes=1))
    seen = []
    def analyze(request):
        seen.append(request)
        return decision(("a",)) if "correction" not in request else decision(("a",)) | {"material_change": material("a", new="같은 증거의 해석 정정")}
    engine.analyze = analyze
    result = tick(engine, "b", 2)
    # NEW with a delivered baseline enters the correction path; a grounded change citing only the
    # already-cited id is then accepted: old ids are flags, not a suppression predicate.
    assert [item["status"] for item in result["corrections"]] == ["OK"] and len(result["alerts"]) == 1 and not result["failures"]
    assert seen[0]["comparison"]["coverage"]["baseline_applicable"] is True and seen[0]["events"][0]["last_sent"]["body"] == first["body"].replace('\nhttps://example.invalid/a', '')
    assert [row["cited_in_delivered"] for row in seen[0]["comparison"]["evidence_delta"]] == [True, False]
    assert result["alerts"][0]["body"].startswith("[후속]") and "normalizations" not in result
    assert result["input_states"] == {"DONE": 2}
    engine.analyze = lambda request: decision(("c",)) | {"change": "UPDATE"}
    assert tick(engine, "c", 3)["input_states"] == {"DONE": 2, "ERROR": 1}
    engine.analyze = lambda request: decision(("d",)) | {"change": "NONE", "material_change": material("d")}
    assert tick(engine, "d", 4)["failures"][0]["reason"] == "material_change must be null for NONE"
    with pytest.raises(ValueError, match="support_ids"):
        validate_decision(decision(("a",)) | {"change": "UPDATE", "material_change": material("zzz")}, [observation("a")], None, baseline_applicable=True)
    assert validate_decision(decision(("a",)), [observation("a")], None)["material_change"] is None
    store.close(); ops.close()


def test_unqueued_ordinary_pending_is_replaced_and_covering_none_keeps_it(tmp_path):
    store, ops, engine = scene(tmp_path)
    first = tick(engine, "a", 0)["alerts"][0]
    seen = []
    engine.analyze = lambda request: seen.append(request) or none(("a", "b"))
    assert not tick(engine, "b", 1)["alerts"]
    assert seen[0]["comparison"]["pending"][0]["body"] == first["body"] and seen[0]["comparison"]["pending"][0]["state"] == "PENDING"
    assert [alert["id"] for alert in engine.pending_alerts()] == [first["id"]]
    second = tick(engine, "c", 2, update(("a", "c"), "c", new="아직 미발송 변화까지 담은 일관 본문"))["alerts"][0]
    event = store.events()[0]
    assert [alert["id"] for alert in engine.pending_alerts()] == [second["id"]]
    assert "replaced_proposal_ids" not in event  # No unread, unbounded diagnostic history.
    assert queue_proposals(engine, ops, ROLE, cutoff=DAY - timedelta(days=1), activation_enabled=True)["queued"] == 1
    # Once QUEUED the proposal is immutable; an ordinary material update is eligible now, not deferred.
    third = tick(engine, "d", 3, update(("a", "d"), "d", importance="MEDIUM", urgency="NORMAL"))["alerts"][0]
    event = store.events()[0]
    assert third["id"] != second["id"] and event["delivery_refs"].keys() == {second["id"]} and event["next_check_at"] != kst(DAY + timedelta(minutes=3))
    assert event["comparison_proposals"][0]["id"] == second["id"] and event["comparison_proposals"][0]["body"] == second["body"]
    store.close(); ops.close()


def test_same_batch_same_event_candidates_are_analyzed_once_but_unattached_ones_are_not_merged(tmp_path):
    store, ops, engine = scene(tmp_path)
    tick(engine, "a", 0)
    event_id = store.events()[0]["id"]
    seen = []
    engine.screen = lambda request: {"candidates": [{"evidence_ids": ["b"], "event_id": event_id, "comparison_event_ids": [], "event_reason": "합성 사례의 같은 사건 부착 또는 별개 사건 검토"}, {"evidence_ids": ["c"], "event_id": event_id, "comparison_event_ids": [], "event_reason": "합성 사례의 같은 사건 부착 또는 별개 사건 검토"},
                                                    {"evidence_ids": ["ship"], "event_id": None, "comparison_event_ids": [], "event_reason": "합성 사례의 같은 사건 부착 또는 별개 사건 검토"}, {"evidence_ids": ["arms"], "event_id": None, "comparison_event_ids": [], "event_reason": "합성 사례의 같은 사건 부착 또는 별개 사건 검토"}], "ignored": []}
    engine.analyze = lambda request: seen.append(sorted(item["id"] for item in request["evidence"])) or (none(("a",)) if request["events"] else decision((request["evidence"][0]["id"],)))
    at = DAY + timedelta(minutes=1)
    result = engine.tick([observation(key, at) for key in ("b", "c", "ship", "arms")], at)
    assert seen == [["a", "b", "c"], ["ship"], ["arms"]] and len(store.events()) == 3 and result["input_states"] == {"DONE": 5}
    store.close(); ops.close()


def test_lagging_delivery_transitions_have_distinct_ids_and_retries_reuse(tmp_path):
    store, ops, engine = scene(tmp_path)
    sent = tick(engine, "l", 0, decision(("l",)))["alerts"][0]
    send_all(engine, ops, DAY + timedelta(minutes=1))
    a = tick(engine, "a", 2, update(("l", "a"), "a"))["alerts"][0]
    queue_proposals(engine, ops, ROLE, cutoff=DAY - timedelta(days=1), activation_enabled=True)
    b = tick(engine, "b", 3, update(("l", "b"), "b", facts="반전 보도"))["alerts"][0]
    # A again while last_sent is still L: same content as A, distinguished by its predecessor B.
    a_again = tick(engine, "a2", 4, update(("l", "a"), "a"))["alerts"][0]
    # Same legacy prose as A but a changed support version is a different grounded transition.
    a_v2 = tick(engine, "a:2", 5, update(("l", "a:2"), "a:2"))["alerts"][0]
    ids = [sent["id"], a["id"], b["id"], a_again["id"], a_v2["id"]]
    assert len(set(ids)) == 5 and a_again["predecessor_id"] == b["id"] and a["baseline_alert_id"] == sent["id"]
    before = store.events()[0]
    # Exact retry of the last accepted transition (same recorded call) reuses the proposal: no new id, no rerender.
    retry = tick(engine, "a:2", 5, update(("l", "a:2"), "a:2"))
    assert retry["calls"] == 0 and not retry["alerts"] and store.events()[0] == before
    store.close()
    store = NewsDecisionStore(tmp_path / "state.db")
    engine = Engine(store, screen, lambda request: update(("l", "a:2"), "a:2"))
    assert not engine.tick([], DAY + timedelta(minutes=6))["alerts"] and store.events()[0]["pending_alerts"] == before["pending_alerts"]
    # Each unqueued ordinary proposal was replaced by the next accepted one; A stayed QUEUED and immutable.
    assert [item["id"] for item in before["pending_alerts"]] == [a_v2["id"]]
    assert "replaced_proposal_ids" not in before
    assert queue_proposals(engine, ops, ROLE, cutoff=DAY - timedelta(days=1), activation_enabled=True)["queued"] == 1
    assert store.events()[0]["last_sent"]["alert_id"] == sent["id"] and set(store.events()[0]["delivery_refs"]) == {sent["id"], a["id"], a_v2["id"]}
    store.close(); ops.close()


def test_same_prose_changed_meaning_and_timestamp_only_context(tmp_path):
    context = {"holdings": [{"symbol": "005930"}], "observed_at": DAY.isoformat()}
    store, ops, engine = scene(tmp_path, context_reader=lambda at: dict(context))
    tick(engine, "a", 0)
    send_all(engine, ops, DAY + timedelta(minutes=1))
    base = update(("a",), "a", importance="HIGH", urgency="NORMAL")
    first = tick(engine, "b", 2, base)["alerts"][0]
    # Equal legacy signature, grounded urgency change: allowed (the old gate would have cancelled it).
    urgent = tick(engine, "c", 3, base | {"urgency": "IMMEDIATE", "material_change": material("a", new="긴급 반전")})["alerts"][0]
    assert urgent["id"] != first["id"] and urgent["urgency"] == "IMMEDIATE"
    # Only the context observation timestamp moved with the same accepted transition: reused, not a new proposal.
    context["observed_at"] = (DAY + timedelta(minutes=4)).isoformat()
    result = tick(engine, "d", 4, base | {"urgency": "IMMEDIATE", "material_change": material("a", new="긴급 반전")})
    assert not result["alerts"] and result["normalizations"][0]["reason"].startswith("same accepted transition")
    # Different wording with no material change is nothing.
    assert not tick(engine, "e", 5, none(("a",)) | {"facts": "다른 표현의 같은 사실"})["alerts"]
    # A real portfolio change makes the same old evidence newly consequential: a new transition.
    context["holdings"] = []
    assert tick(engine, "f", 6, base | {"urgency": "IMMEDIATE", "material_change": material("a", new="긴급 반전")})["alerts"][0]["id"] not in {first["id"], urgent["id"]}
    store.close(); ops.close()


def test_unsent_important_evidence_is_kept_in_view_and_empty_recheck_carries_delta(tmp_path):
    store, ops, engine = scene(tmp_path)
    sent = tick(engine, "l", 0, decision(("l",)))["alerts"][0]
    send_all(engine, ops, DAY + timedelta(minutes=1))
    night = timestamp("2026-09-01T23:00:00+09:00")
    tick(engine, "x", 0, update(("l", "x"), "x", urgency="NORMAL"), at=night)  # important, held for the morning: not sent
    assert not store.events()[0].get("pending_alerts") and store.events()[0]["held_until"]
    event_id = store.events()[0]["id"]
    engine.screen = lambda request: {"candidates": [{"evidence_ids": [item["id"] for item in request["evidence"]], "event_id": event_id, "comparison_event_ids": [], "event_reason": "합성 사례의 같은 사건 부착 또는 별개 사건 검토"}], "ignored": []}
    engine.analyze = lambda request: none(("l",))
    for index in range(9):
        engine.tick([observation(f"noise{index}", night + timedelta(minutes=1 + index))], night + timedelta(minutes=1 + index))
    seen = []
    engine.analyze = lambda request: seen.append(request) or update(("l", "x", "y"), "x", "y", urgency="NORMAL")
    engine.tick([observation("y", night + timedelta(minutes=20))], night + timedelta(minutes=20))
    shown = [item["id"] for item in seen[0]["evidence"]]
    assert "x" in shown and "l" in shown and "y" in shown and seen[0]["comparison"]["coverage"]["evidence_omitted"] >= 1
    assert seen[0]["events"][0]["last_sent"]["body"] == sent["body"].replace('\nhttps://example.invalid/l', '')
    release = timestamp("2026-09-02T08:00:00+09:00")
    engine.analyze = lambda request: seen.append(request) or update(("l", "x", "y"), "x", "y", urgency="NORMAL")
    morning = engine.tick([], release)
    assert seen[1]["review_reason"] == "MORNING_RELEASE" and {row["id"] for row in seen[1]["comparison"]["evidence_delta"]} == set(item["id"] for item in seen[1]["evidence"])
    assert morning["alerts"][0]["kind"] == "MORNING_DIGEST" and store.events()[0]["unsent_evidence"]["ids"] == ["l", "x", "y"]
    store.close(); ops.close()


def test_cross_event_sent_bodies_reach_screen_and_analyze_within_bounds(tmp_path):
    store, ops, engine = scene(tmp_path)
    split = lambda request: {"candidates": [{"evidence_ids": [item["id"]], "event_id": None, "comparison_event_ids": [], "event_reason": "합성 사례의 같은 사건 부착 또는 별개 사건 검토"} for item in request["evidence"]], "ignored": []}
    engine.screen, engine.analyze = split, lambda request: decision((request["evidence"][0]["id"],)) | {"open": False}
    keys = [f"news:doc{index}:1" for index in range(9)] + ["news:lone:1"]
    engine.tick([observation(key, DAY) for key in keys], DAY)
    send_all(engine, ops, DAY + timedelta(minutes=1))
    events = {event["evidence"][0]["id"]: event for event in store.events()}
    seen = {}
    def screen_model(request):
        seen["screen"] = request
        return {"candidates": [{"evidence_ids": ["news:doc0:2", "fresh"], "event_id": None, "comparison_event_ids": [events["news:lone:1"]["id"]], "event_reason": "별개 사건의 실제 전달 본문과 비교"}], "ignored": []}
    def analyze_model(request):
        seen["analyze"] = request
        return decision(("news:doc0:2", "fresh")) | {"material_change": material("fresh", why="다른 사건 발송 본문과 비교함")}
    engine.screen, engine.analyze = screen_model, analyze_model
    later = DAY + timedelta(hours=2)
    result = engine.tick([observation("news:doc0:2", later), observation("fresh", later)], later)
    assert len(result["alerts"]) == 1 and len(store.events()) == 11
    hint = next(item for item in seen["screen"]["evidence"] if item["id"] == "news:doc0:2")["known_event_ids"]
    assert hint == [events["news:doc0:1"]["id"]] and len(seen["screen"]["sent_bodies"]) == 8
    bodies = seen["analyze"]["comparison"]["other_events"]
    assert bodies[0]["event_id"] == events["news:doc0:1"]["id"] and bodies[0]["direct_overlap"] is True and bodies[0]["body"] == events["news:doc0:1"]["last_sent"]["body"]
    assert any(row["event_id"] == events["news:lone:1"]["id"] and row["direct_overlap"] is False for row in bodies)
    assert seen["analyze"]["comparison"]["coverage"] == {"baseline_applicable": True, "delivered_versions": "NONE", "pending_omitted": 0, "other_events_omitted": 0, "evidence_omitted": 0}
    # Overflow: nine direct-overlap sent events keep the bound and report the omission instead of implying coverage.
    engine.screen = lambda request: {"candidates": [{"evidence_ids": [item["id"] for item in request["evidence"]], "event_id": None, "comparison_event_ids": [], "event_reason": "합성 사례의 같은 사건 부착 또는 별개 사건 검토"}], "ignored": []}
    engine.analyze = lambda request: seen.__setitem__("overflow", request) or none(tuple(item["id"] for item in request["evidence"]))
    engine.tick([observation(key.replace(":1", ":4"), later + timedelta(minutes=2)) for key in keys[:9]], later + timedelta(minutes=2))
    assert len(seen["overflow"]["comparison"]["other_events"]) == 8 and seen["overflow"]["comparison"]["coverage"]["other_events_omitted"] >= 1
    # A legacy proposal whose body was never stored stays UNKNOWN (null), never rebuilt from the decision.
    event = store.events()[0]
    event["last_alert"], event["last_delivery"] = {"id": "decision-alert-legacy", "created_at": kst(DAY)}, "PENDING"
    store.save_event(event)
    engine.screen = lambda request: {"candidates": [{"evidence_ids": ["z"], "event_id": event["id"], "comparison_event_ids": [], "event_reason": "합성 사례의 같은 사건 부착 또는 별개 사건 검토"}], "ignored": []}
    engine.analyze = lambda request: seen.__setitem__("legacy", request) or none(("z",))
    engine.tick([observation("z", later + timedelta(minutes=3))], later + timedelta(minutes=3))
    legacy = seen["legacy"]["comparison"]["pending"][0]
    assert (legacy["alert_id"], legacy["state"], legacy["body"]) == ("decision-alert-legacy", "PENDING", None)
    brief = NewsDecisionEngine._brief([dict(id=f"e{index}", updated_at=kst(DAY + timedelta(minutes=index)), decision={"title": "t", "facts": "f", "open": True}) for index in range(101)], reserve={"e0"})
    assert len(brief) == 100 and brief[0]["event_id"] == "e0"
    store.close(); ops.close()


class Pin:
    """Recorded production rows (mode=ro extraction). ``state`` rebuilds an event as the engine had
    it before a call: evidence attached earlier (stored originals), the latest complete actual send
    before that instant with the body the user read, and the prior context exactly as the recorded
    request carried it. Nothing later is substituted back."""
    def __init__(self):
        data = json.loads(PIN.read_text())
        self.calls = {row["id"]: row for row in data["calls"]}
        self.events = {row["id"]: json.loads(row["state"]) for row in data["events"]}
        self.inputs = {row["id"]: json.loads(row["payload"]) for row in data["inputs"]}
        self.outbox = {row["alert_id"]: row for row in data["outbox"]}

    def call(self, prefix):
        row = next(value for key, value in self.calls.items() if key.startswith(prefix))
        return json.loads(row["request"]), json.loads(row["response"]), timestamp(row["started_at"])

    def selected(self, screen_prefix, event_id):
        return [key for cand in self.call(screen_prefix)[1]["candidates"] if cand.get("event_id") == event_id for key in cand["evidence_ids"]]

    def state(self, event_id, before, prior=None):
        current = self.events[event_id]
        base = dict(prior) if prior else {key: value for key, value in current.items() if key not in ("last_sent", "pending_alerts", "delivery_refs", "evidence", "queued_evidence")}
        refs = {aid: parts for aid, parts in current.get("delivery_refs", {}).items() if all(timestamp(self.outbox[ref]["created_at"]) < before for ref in parts)}
        sends = []
        for aid, parts in refs.items():
            rows = [self.outbox[ref] for ref in parts]
            if all(row["status"] == "SENT" and row["delivered_at"] and timestamp(row["delivered_at"]) < before for row in rows):
                sends.append((max(timestamp(row["delivered_at"]) for row in rows), aid, rows))
        state = dict(base, id=event_id, evidence=[item for item in current["evidence"] if timestamp(item["observed_at"]) < before], delivery_refs=refs, pending_alerts=[])
        if sends:
            delivered, aid, rows = max(sends)
            created, body = timestamp(rows[0]["created_at"]), "".join(json.loads(row["body_evidence_json"])[0] for row in rows)
            # The proposal's own cited ids: the analyze call(s) of that tick whose title the delivered body carries.
            # Ambiguous or absent → UNVERIFIED, exactly as the runtime marks proposals queued before queued_evidence.
            cited = set()
            for row in self.calls.values():
                request, response = json.loads(row["request"]), json.loads(row["response"] or "null")
                if row["stage"] == "analyze" and row["status"] == "OK" and timestamp(row["started_at"]) == created and response.get("title") in body \
                        and (request.get("events") or [{"id": None}])[0].get("id") in (event_id, None):
                    cited.add(tuple(response["evidence_ids"]))
            state["last_sent"] = {"alert_id": aid, "body": body, "parts": len(rows), "delivered_at": kst(delivered), "outbox_refs": list(refs[aid])}
            state["last_sent"].update({"evidence_ids": list(cited.pop())} if len(cited) == 1 else {"evidence_snapshot": "UNVERIFIED"})
        return state


E1184, EA779 = "event-1184e0c8177199af0978681a", "event-a779ca5b0dc1bf07ed548f95"


def historical(tmp_path, pin, event_id, analyze_prefix, screen_prefix, others=()):
    request, response, at = pin.call(analyze_prefix)
    store = NewsDecisionStore(tmp_path / "state.db")
    store.save_event(pin.state(event_id, at, prior=request["events"][0] if request.get("events") else None))
    for other in others:
        store.save_event(pin.state(other, at))
    selected = pin.selected(screen_prefix, event_id)
    seen = []
    engine = Engine(store, lambda req: {"candidates": [{"evidence_ids": [item["id"] for item in req["evidence"]], "event_id": event_id, "comparison_event_ids": [], "event_reason": "합성 사례의 같은 사건 부착 또는 별개 사건 검토"}], "ignored": []},
                    # Historical responses test state mechanics only; this explicit adaptation is not a model verdict.
                    lambda req: seen.append(req) or dict(engine.stub(req), notify=True), context_reader=lambda now: dict(request.get("context", {})))
    return store, engine, seen, [pin.inputs[key] for key in selected], at, response


@pinned
def test_p1_1184_1211_old_ids_do_not_decide_and_second_batch_sees_the_queued_body(tmp_path):
    pin = Pin()
    store, engine, seen, selected, at, recorded = historical(tmp_path, pin, E1184, "69e90e51", "6b231db6")
    assert [item["id"][-16:] for item in selected] == ["90daf0cb4dbc92:4"]
    sent_before = store.events()[0]["last_sent"]
    assert "0272c363" in sent_before["alert_id"] and sent_before["evidence_ids"] == ["news:b85a64fa728940e58990daf0cb4dbc92:3"]  # 10:41 "긴장 완화" send
    # Historical classification stays unresolved: both semantic outcomes are wired, neither is asserted as truth.
    engine.stub = lambda req: dict(recorded, change="NONE", material_change=None)
    assert not engine.tick(selected, at)["alerts"]
    delta = {row["id"][-16:]: row for row in seen[0]["comparison"]["evidence_delta"]}
    assert delta["90daf0cb4dbc92:4"]["version_prev"].endswith(":3") and delta["90daf0cb4dbc92:4"]["version_prev_basis"] == "DELIVERED" and delta["90daf0cb4dbc92:4"]["changed_fields"] == ["text", "title"]
    assert delta["90daf0cb4dbc92:3"]["cited_in_delivered"] is True and delta["f09e2cd6fba61f:1"]["cited_in_delivered"] is False
    assert seen[0]["events"][0]["last_sent"]["body"] == sent_before["body"] and seen[0]["comparison"]["coverage"]["delivered_versions"] == "KNOWN"
    store.mark([item["id"] for item in selected], "READY")
    published = []
    engine.on_urgent = lambda alert: published.append(alert["id"])
    engine.stub = lambda req: dict(recorded, material_change=material(*recorded["evidence_ids"], prior="긴장 완화 조짐", new="상호 공격 재개로 이전 완화 해석 정정"))
    corrected = engine.tick(selected, at + timedelta(seconds=1))
    assert len(corrected["alerts"]) == 1 and corrected["alerts"][0]["body"].startswith("[후속]") and published == [corrected["alerts"][0]["id"]]
    # Second batch of the same run (recorded dd1798f8): the queued body is now in view. NONE keeps one proposal.
    store.save_event(dict(store.events()[0], delivery_refs={corrected["alerts"][0]["id"]: ["nda-x"]}, pending_alerts=[], last_delivery="QUEUED"))
    second = [pin.inputs[key] for key in pin.selected("aff29572", E1184)]
    _, repeat, _ = pin.call("dd1798f8")
    engine.stub = lambda req: dict(repeat, change="NONE", material_change=None)
    assert not engine.tick(second, at + timedelta(seconds=2))["alerts"]
    assert seen[-1]["comparison"]["pending"][0]["body"] == corrected["alerts"][0]["body"] and seen[-1]["comparison"]["pending"][0]["state"] == "QUEUED"
    # With the recorded second response (different prose, same cited ids) plus a material_change the
    # engine would still accept a second proposal: the repeat is stopped only by the model returning NONE.
    store.mark([item["id"] for item in second], "READY")
    engine.stub = lambda req: dict(repeat, material_change=material(*repeat["evidence_ids"]))
    assert len(engine.tick(second, at + timedelta(seconds=3))["alerts"]) == 1
    store.close()


@pinned
def test_p2_a779_1211_split_batches_one_followup_then_none_or_replacement(tmp_path):
    pin = Pin()
    store, engine, seen, selected, at, recorded = historical(tmp_path, pin, EA779, "f3a9199d", "6b231db6")
    assert "c4c53989" in store.events()[0]["last_sent"]["alert_id"] and len(selected) == 7
    engine.stub = lambda req: dict(recorded, material_change=material("news:be456cff7a79488d8115543887c1c671:1", new="호르무즈 통항량 최저 등 후속 사실"))
    first = engine.tick(selected, at)["alerts"]
    assert len(first) == 1 and first[0]["body"].startswith("[후속]")
    second = [pin.inputs[key] for key in pin.selected("aff29572", EA779)]
    _, repeat, _ = pin.call("ed4d2ad0")
    engine.stub = lambda req: dict(repeat, change="NONE", material_change=None)
    assert not engine.tick(second, at + timedelta(seconds=1))["alerts"] and [alert["id"] for alert in engine.pending_alerts()] == [first[0]["id"]]
    assert seen[-1]["comparison"]["pending"][0]["body"] == first[0]["body"] and seen[-1]["comparison"]["pending"][0]["state"] == "PENDING"
    store.mark([item["id"] for item in second], "READY")
    engine.stub = lambda req: dict(repeat, material_change=material(*repeat["evidence_ids"]))
    replaced = engine.tick(second, at + timedelta(seconds=2))["alerts"]
    assert [alert["id"] for alert in engine.pending_alerts()] == [replaced[0]["id"]]
    assert replaced[0]["id"] != first[0]["id"] and "replaced_proposal_ids" not in store.events()[0]
    # Same-batch variant: both candidates name a779 → one analysis over the merged inputs.
    store.mark([item["id"] for item in selected + second], "READY")
    engine.screen = lambda req: {"candidates": [{"evidence_ids": [item["id"] for item in selected], "event_id": EA779, "comparison_event_ids": [], "event_reason": "합성 사례의 같은 사건 부착 또는 별개 사건 검토"}, {"evidence_ids": [item["id"] for item in second], "event_id": EA779, "comparison_event_ids": [], "event_reason": "합성 사례의 같은 사건 부착 또는 별개 사건 검토"}], "ignored": []}
    calls = len(seen)
    engine.stub = lambda req: dict(repeat, change="NONE", material_change=None)
    engine.tick([], at + timedelta(seconds=3))
    assert len(seen) == calls + 1 and len(seen[-1]["evidence"]) >= 8
    store.close()


@pinned
def test_p3_a779_1311_both_actual_sent_bodies_reach_analyze(tmp_path):
    pin = Pin()
    store, engine, seen, selected, at, recorded = historical(tmp_path, pin, EA779, "5d2c65eb", "1f2193b4", others=(E1184,))
    pairs = {"625fb8d07a95433fb3cd634d7412e386": (30, 29), "63d13320f0d1497493132c873e30c6be": (3, 2), "6d686ea9805347eebcdeb617fc29759a": (17, 16), "8158f678c6ef47a9bbf2e00c8975d1d1": (4, 3),
             "96b1b346c17b46fd8be0bfebb877464a": (4, 3), "9f2ffa963d824491b16de39e0eb584f1": (12, 11), "be456cff7a79488d8115543887c1c671": (3, 2), "d2b02e15ff494cbbbfeb7524fe554de0": (10, 9)}
    assert sorted(item["id"] for item in selected) == sorted(f"news:{doc}:{new}" for doc, (new, _) in pairs.items())
    a779, e1184 = {event["id"]: event for event in store.events()}[EA779], {event["id"]: event for event in store.events()}[E1184]
    assert "9dd722e5" in a779["last_sent"]["alert_id"] and a779["last_delivery"] == "PENDING" and "body" not in a779["last_alert"]  # 12:41 proposal body was never stored
    assert "0d3e8b63" in e1184["last_sent"]["alert_id"]
    engine.stub = lambda req: dict(recorded, change="NONE", material_change=None)
    assert not engine.tick(selected, at)["alerts"] and engine.pending_alerts() == []
    request = seen[0]
    assert request["events"][0]["last_sent"]["body"] == a779["last_sent"]["body"]
    other = request["comparison"]["other_events"]
    assert [(row["event_id"], row["direct_overlap"]) for row in other] == [(E1184, True)] and other[0]["body"] == e1184["last_sent"]["body"] and other[0]["alert_id"] == e1184["last_sent"]["alert_id"]
    assert request["comparison"]["pending"][0]["alert_id"] == a779["last_alert"]["id"] and request["comparison"]["pending"][0]["body"] is None
    delta = {row["id"]: row for row in request["comparison"]["evidence_delta"]}
    for doc, (new, old) in pairs.items():
        row, a, b = delta[f"news:{doc}:{new}"], pin.inputs[f"news:{doc}:{new}"], pin.inputs[f"news:{doc}:{old}"]
        # Observation-relative: title and text alternate, url/published stay (the recorded cosmetic repetition).
        assert row["observed_prev"] == f"news:{doc}:{old}" and row["changed_since_observed"] == ["text", "title"] and a["url"] == b["url"] and a["published_at"] == b["published_at"]
        assert a["title"] != b["title"] and (row["title_prev"], row["title_new"]) == (pin.inputs[row["version_prev"]]["title"], a["title"])
        # Delivered-relative: against the version the 12:15 send cited when it cited that document, never the latest observation.
        assert row["version_prev_basis"] == ("DELIVERED" if any(key.startswith(f"news:{doc}:") for key in a779["last_sent"]["evidence_ids"]) else "OBSERVATION")
        assert row["changed_fields"] == sorted(key for key in ("title", "text", "url", "published_at", "publisher") if pin.inputs[row["version_prev"]].get(key) != a.get(key))
    assert delta["news:b85a64fa728940e58990daf0cb4dbc92:5"]["cited_in_delivered"] is False
    # Screen proposing a new event for the same story: a779 reaches analyze by direct document overlap; 1184 shares no
    # document with this batch (finite selection limit), so the screen must nominate it from the sent bodies it was shown.
    store.mark([item["id"] for item in selected], "READY")
    screens = []
    engine.screen = lambda req: screens.append(req) or {"candidates": [{"evidence_ids": [item["id"] for item in req["evidence"]], "event_id": None, "comparison_event_ids": [E1184], "event_reason": "별개 사건으로 유지하며 전달 본문 비교"}], "ignored": []}
    fresh = {"evidence_ids": [key for key in recorded["evidence_ids"] if key in {item["id"] for item in selected}]}  # a new event sees only the batch
    engine.stub = lambda req: dict(recorded, change="NEW", material_change=None, **fresh) if "correction" not in req else dict(recorded, change="NONE", material_change=None, **fresh)
    result = engine.tick(selected, at + timedelta(seconds=1))
    assert result["corrections"][0]["status"] == "OK" and not result["alerts"] and not result["failures"]
    assert {row["body"] for row in screens[0]["sent_bodies"]} >= {a779["last_sent"]["body"], e1184["last_sent"]["body"]}
    assert [(row["event_id"], row["direct_overlap"]) for row in seen[-1]["comparison"]["other_events"]] == [(EA779, True), (E1184, False)]
    assert {row["body"] for row in seen[-1]["comparison"]["other_events"]} == {a779["last_sent"]["body"], e1184["last_sent"]["body"]}
    store.close()


@pinned
def test_p4p5_a779_1341_cosmetic_revisions_delta_and_restart_stability(tmp_path):
    pin = Pin()
    store, engine, seen, selected, at, recorded = historical(tmp_path, pin, EA779, "2bc85359", "b4b837c0")  # the corrected 13:41 screen call
    event = store.events()[0]
    assert "4ffb549b" in event["last_sent"]["alert_id"] and event["last_sent"]["alert_id"] == event["last_alert"]["id"] and len(selected) == 8
    engine.stub = lambda req: recorded  # the recorded 13:41 NONE
    assert not engine.tick(selected, at)["alerts"]
    delta = {row["id"]: row for row in seen[0]["comparison"]["evidence_delta"]}
    kinds = []
    for item in selected:
        row = delta[item["id"]]
        expected = sorted(key for key in ("title", "text", "url", "published_at", "publisher") if pin.inputs[row["observed_prev"]].get(key) != item.get(key))
        assert row["changed_since_observed"] == expected and row["version_prev"] != item["id"] and not {"url", "published_at"} & set(row["changed_fields"])
        kinds.append(tuple(expected))
    # Seven publisher-tail alternations and one identical re-observation under a new revision: reported as stored, never fabricated.
    assert sorted(kinds) == [()] + [("text", "title")] * 7
    cited = {key for key in event["last_sent"]["evidence_ids"]}
    assert {delta[item["id"]]["version_prev_basis"] for item in selected if any(key.startswith(doc_key(item["id"]) + ":") for key in cited)} == {"DELIVERED"}
    after = store.events()[0]
    assert after["next_check_at"] == kst(at + timedelta(hours=6)) and not after.get("comparison_proposals") and not after.get("pending_alerts")
    store.close()
    for minutes in (1, 30):
        store = NewsDecisionStore(tmp_path / "state.db")
        engine = Engine(store, lambda req: pytest.fail("no screen without inputs"), lambda req: pytest.fail("not due"))
        assert engine.tick(selected, at + timedelta(minutes=minutes))["calls"] == 0 and store.events()[0] == after  # re-seen revisions are the same observations
        store.close()


@pinned
def test_transition_no_burst_on_production_snapshot(tmp_path):
    pin = Pin()
    store = NewsDecisionStore(tmp_path / "state.db")
    for event in pin.events.values():
        store.save_event(event)
    before = store.events()
    due = [timestamp(event[key]) for event in before for key in ("held_until", "next_check_at") if event.get(key)]
    engine = Engine(store, lambda req: pytest.fail("no screen"), lambda req: pytest.fail("not due"))
    result = engine.tick([], min(due) - timedelta(seconds=1))
    assert result["calls"] == 0 and not result["alerts"] and store.events() == before
    store.close()
