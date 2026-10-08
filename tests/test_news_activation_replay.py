"""Check counterfactual accounting without a sender, real provider receipts or model calls."""
from datetime import timedelta
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

from tokmo.records.news_decision_store import NewsDecisionStore, timestamp
from test_news_decision_engine import Engine, observation, decision, screen
from test_news_decision_s3 import DAY
from validate_news_activation import context_at, virtual_delivery


def test_counterfactual_uses_its_selected_body_for_next_judgment(tmp_path):
    store = NewsDecisionStore(tmp_path / "counterfactual.sqlite3")
    engine = Engine(store, screen, lambda req: decision([req["evidence"][0]["id"]], facts="최초 확인한 변화"))
    engine.tick([observation("a", DAY)], DAY)
    delivered = virtual_delivery(engine, DAY)
    assert len(delivered) == 1 and delivered[0]["simulation"] == "COUNTERFACTUAL_ONLY"
    assert virtual_delivery(engine, DAY) == []
    seen = []
    engine.analyze = lambda req: seen.append(req) or decision(["b"], change="NONE", notify=False)
    engine.tick([observation("b", DAY + timedelta(minutes=1))], DAY + timedelta(minutes=1))
    assert seen[0]["events"][0]["last_sent"]["body"] == delivered[0]["body"]
    assert store.events()[0]["last_sent"]["simulation"] == "COUNTERFACTUAL_ONLY"
    assert not list(tmp_path.glob('*ops*'))
    store.close()


def test_morning_digest_counts_once_and_clears_every_constituent(tmp_path):
    store = NewsDecisionStore(tmp_path / "counterfactual.sqlite3")
    at = timestamp('2026-09-07T02:00:00+09:00')
    engine = Engine(store, lambda req: {"candidates": [{"evidence_ids": [r["id"]]} for r in req["evidence"]], "ignored": []},
        lambda req: decision([req["evidence"][0]["id"]], importance="MEDIUM", urgency="NORMAL"))
    engine.tick([observation("a", at), observation("b", at)], at)
    assert virtual_delivery(engine, at) == []
    morning = timestamp('2026-09-07T08:00:00+09:00')
    engine.tick([], morning)
    delivered = virtual_delivery(engine, morning)
    assert len(delivered) == 1 and delivered[0]["kind"] == "MORNING_DIGEST"
    assert len(store.events()) == 2 and all(not e['pending_alerts'] for e in store.events())
    assert all('evidence_ids' not in e['last_sent'] for e in store.events())
    assert virtual_delivery(engine, morning) == []
    seen = []
    engine.analyze = lambda req: seen.append(req) or decision(["c"], change="NONE", notify=False)
    engine._analyze([observation("c", morning)], store.events()[0], morning,
                    {"calls": 0, "decisions": [], "alerts": [], "failures": []})
    assert seen[0]["comparison"]["delivered"]["cited_ids"] == "UNKNOWN"
    store.close()


def test_context_uses_only_information_available_at_that_time():
    rows = [{"available_at": DAY.isoformat(), "context": {"holdings": []}},
            {"available_at": (DAY + timedelta(minutes=1)).isoformat(), "context": {"holdings": ["later holding"]}}]
    assert context_at(rows, DAY - timedelta(seconds=1)) == {"portfolio_status": "UNAVAILABLE_AT_THIS_TIME"}
    assert context_at(rows, DAY) == {"holdings": []}


def test_parallel_validation_cannot_reset_or_overspend_shared_cap(tmp_path, monkeypatch):
    import validate_news_activation as tool
    monkeypatch.setattr(tool, "FOLDER", tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    quota = tmp_path / ".hermes/cron/state/model-quota-latest.json"
    quota.parent.mkdir(parents=True)
    quota.write_text(json.dumps({"observed_at": datetime.now(timezone.utc).isoformat(),
                                "providers": {"zai": {"ok": True, "windows": [{"remaining": 95}]}}}))
    tool.write(tmp_path / "model-budget.json", {"used": tool.MODEL_CAP - 2, "inherited": 12})
    calls = []
    def callback(request):
        calls.append(request)
        return {"ok": True}
    callback.model, callback.max_output_tokens = "fake", 1
    def run(_):
        try:
            return tool.BoundedModel(callback, {"used": 0})(dict(now=DAY.isoformat()))
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, range(3)))
    assert len(calls) == 2 and results.count("activation_model_cap") == 1
    assert json.loads((tmp_path / "model-budget.json").read_text())["used"] == tool.MODEL_CAP
