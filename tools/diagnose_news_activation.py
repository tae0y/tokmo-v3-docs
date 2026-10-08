"""Bounded activation diagnostics. Telegram metadata reads only; no message delivery."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from validate_news_activation import FOLDER, BoundedModel, write
from tokmo.programs.news_delivery_route import read_group_identity


def telegram():
    path = FOLDER / "telegram-read-budget.json"
    budget = json.loads(path.read_text()) if path.exists() else {
        "cap": 6, "basis": "2 * three normal bot/chat/membership metadata reads",
        "attempts": [], "retry": 0}
    route = json.loads((Path.home() / ".local/share/tokmo-v3/news_delivery_route.json").read_text())
    if route.get("destination") != "TOKMO_GROUP":
        raise ValueError("unexpected destination")
    token = read_group_identity(route["credential_file"])
    bot_id = None
    for method in ("getMe", "getChat", "getChatMember"):
        if len(budget["attempts"]) >= budget["cap"]:
            raise ValueError("metadata read cap exhausted")
        parameters = {} if method == "getMe" else {"chat_id": route["chat_id"]}
        if method == "getChatMember":
            parameters["user_id"] = bot_id
        result = {"method": method, "at": datetime.now(timezone.utc).isoformat(), "status": "STARTED"}
        budget["attempts"].append(result); write(path, budget)
        # Same urllib system TLS/proxy behavior as the live Telegram transport.
        request = Request("https://api.telegram.org/bot" + token + "/" + method + "?" + urlencode(parameters))
        try:
            with urlopen(request, timeout=10) as response:
                payload = json.loads(response.read(65537))
            result.update(status="OK" if payload.get("ok") is True else "REJECTED",
                          error_code=payload.get("error_code"))
            value = payload.get("result", {})
            if method == "getMe":
                bot_id = value.get("id")
                result["is_bot"] = value.get("is_bot")
            elif method == "getChat":
                result.update(chat_type=value.get("type"), permissions=value.get("permissions", {}))
            else:
                result.update(member_status=value.get("status"), can_send_messages=value.get("can_send_messages"))
        except HTTPError as exc:
            result.update(status="ERROR", error_class="HTTPError", http_status=exc.code)
        except URLError as exc:
            result.update(status="ERROR", error_class="URLError", reason_class=type(exc.reason).__name__)
        except Exception as exc:
            result.update(status="ERROR", error_class=type(exc).__name__)
        write(path, budget)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if result["status"] != "OK" or (method == "getMe" and not bot_id):
            return 2
    return 0


def model():
    """One explicit reproduction; preserve raw output privately, never repair it implicitly."""
    output = FOLDER / "failed-screen-reproduction.json"
    if output.exists():
        raise ValueError("diagnostic already completed")
    import news_decision_models as models
    db = sqlite3.connect((FOLDER / "counterfactual.sqlite3").as_uri() + "?mode=ro", uri=True)
    try:
        row = db.execute("SELECT request FROM news_decision_calls WHERE status='ERROR' ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        db.close()
    request = json.loads(row[0])
    captured = {"observed_at": datetime.now(timezone.utc).isoformat(), "request": request,
                "purpose": "single reproduction of failed screen; no delivery or state replay"}
    original_chat, original_capture = models.zai_chat_once, models._Capture

    class Capture(original_capture):
        def open(self, request, timeout):
            response = super().open(request, timeout)
            original_read = response.read

            def read(size=-1):
                raw = original_read(size)
                try:
                    body = json.loads(raw)
                    captured["finish_reason"] = body["choices"][0].get("finish_reason")
                    captured["usage"] = body.get("usage")
                except (ValueError, KeyError, IndexError, TypeError):
                    captured["envelope_valid"] = False
                return raw
            response.read = read
            return response

    def chat(*args, **kwargs):
        value = original_chat(*args, **kwargs)
        captured["raw_content"] = value
        write(output, captured)
        return value

    models._Capture, models.zai_chat_once = Capture, chat
    ledger = json.loads((FOLDER / "model-budget.json").read_text())
    try:
        answer = BoundedModel(models.build_models()[0], ledger)(request)
        captured["parsed_response"] = answer
        captured["status"] = "PARSED"
    except ValueError as exc:
        captured.update(status="ERROR", reason=str(exc))
    finally:
        models._Capture, models.zai_chat_once = original_capture, original_chat
        write(output, captured)
    print(json.dumps({key: captured.get(key) for key in ("status", "reason", "finish_reason", "usage")}))
    return 0 if captured["status"] == "PARSED" else 2


def documents(version="v1"):
    """Analyze two actual documents acquired tonight, independently of past-day counts.

    Candidate selection is bypassed deliberately: this checks explanation quality only.
    Ordinary engine validation and its single correction limit remain in force.
    """
    from news_decision_models import build_models
    from tokmo.programs.news_decision_engine import NewsDecisionEngine, doc_key, identity
    from tokmo.records.news_decision_store import NewsDecisionStore, kst
    suffix = "" if version == "v1" else "-" + version
    path = FOLDER / ("document-explanations" + suffix + ".sqlite3")
    if path.exists():
        raise ValueError("document analysis already attempted")
    case = json.loads((FOLDER / "case.json").read_text())
    dart = json.loads((FOLDER.parent / "document-20260907000167-20260907T195725.json").read_text())
    listing = next(row["observation"] for row in case["observations"]
                   if row["observation"]["id"].startswith("dart:20260907000167:"))
    sec = json.loads(sorted(FOLDER.glob("sec-document-0001045810-26-000078-*.json"))[-1].read_text())
    sources = [("Hanwha Robotics", listing, dart, dart["observed_at"]),
               ("NVIDIA", sec["listing"], sec["document"], sec["observed_at"])]
    ledger = json.loads((FOLDER / "model-budget.json").read_text())
    screen, analyze = [BoundedModel(callback, ledger) for callback in build_models()]
    from validate_news_activation import code_hashes
    report = {"scope": "two actual source-enriched explanation checks; no screen-quality or historical-count claim",
              "code_hashes": code_hashes(), "version": version,
              "sending": "unavailable", "cases": []}
    for title, listing, body, at in sources:
        # One isolated event history per source prevents accidental cross-case comparison.
        store = NewsDecisionStore(path if title == "Hanwha Robotics" else FOLDER / ("document-explanations-sec" + suffix + ".sqlite3"))
        now = datetime.now(timezone.utc)
        doc = dict(listing, id=doc_key(listing["id"]) + ":document:" + identity(body),
                   text=body["text"], source_document_sha256=body["source_document_sha256"],
                   content_scope="original_document", body_verified=True,
                   observed_at=kst(at), parent_evidence_id=listing["id"])
        for key in ("document_scope", "attachments_verified"):
            if key in body:
                doc[key] = body[key]
        row = {"title": title, "at": kst(now), "decisions": [], "alerts": [], "failures": [], "calls": 0}
        try:
            store.ingest([listing, doc], now)
            engine = NewsDecisionEngine(store, screen, analyze, clock=lambda: datetime.now(timezone.utc),
                                        context_reader=lambda _: {})
            engine._analyze([listing, doc], None, now, row)
            row["events"] = store.events()
        except Exception as exc:
            row["failures"].append({"reason": type(exc).__name__})
        finally:
            store.close()
            report["cases"].append(row)
            report["model_attempts_cumulative"] = ledger["used"]
            write(FOLDER / ("document-explanations" + suffix + ".json"), report)
        print(json.dumps({"title": title, "calls": row["calls"], "failures": row["failures"],
                          "decisions": row["decisions"]}, ensure_ascii=False), flush=True)
        if row["failures"]:
            return 2
    return 0


def discovery_flow():
    """Actual screen + thin analysis + stored public source + reanalysis; never a sender."""
    from news_decision_models import build_models
    from tokmo.programs.news_decision_engine import NewsDecisionEngine
    from tokmo.records.news_decision_store import NewsDecisionStore
    from validate_news_activation import code_hashes
    path = FOLDER / "discovery-flow.sqlite3"
    if path.exists():
        raise ValueError("discovery flow already attempted")
    case = json.loads((FOLDER / "case.json").read_text())
    listing = next(row["observation"] for row in case["observations"] if row["observation"]["id"].startswith("dart:20260907000167:"))
    body = json.loads((FOLDER.parent / "document-20260907000167-20260907T195725.json").read_text())
    ledger = json.loads((FOLDER / "model-budget.json").read_text())
    callbacks = [BoundedModel(callback, ledger) for callback in build_models()]
    reads = []
    def document(receipt):
        if receipt != body["receipt"]:
            raise ValueError("unexpected source identity")
        reads.append(receipt)
        return body  # Public document already acquired; no extra source request.
    store = NewsDecisionStore(path)
    report = {"scope": "current-time first-discovery integration scenario; no past-day count claim",
              "code_hashes": code_hashes(), "sending": "unavailable", "new_source_network_calls": 0}
    try:
        engine = NewsDecisionEngine(store, *callbacks, clock=lambda: datetime.now(timezone.utc), document_reader=document)
        report["initial"] = engine.tick([listing], datetime.now(timezone.utc))
        report["enriched"] = engine.tick([], datetime.now(timezone.utc))
        report["events"] = store.events()
        report["source_reads"] = reads
        report["model_calls"] = store.connection.execute("SELECT count(*) FROM news_decision_calls").fetchone()[0]
        report["pass"] = bool(len(reads) == 1 and not report["initial"]["alerts"] and not report["initial"]["failures"]
                              and not report["enriched"]["failures"] and any(e["decision"]["notify"] and e.get("source_enrichment", {}).get("status") == "READY" for e in report["events"]))
        write(FOLDER / "discovery-flow.json", report)
    finally:
        store.close()
    print(json.dumps({"pass": report["pass"], "model_calls": report["model_calls"], "source_reads": len(reads),
                      "decisions": report["enriched"]["decisions"]}, ensure_ascii=False))
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    actions = {"telegram": telegram, "model": model, "documents": documents,
               "documents-v2": lambda: documents("v2"), "documents-closure": lambda: documents("closure"),
               "documents-final": lambda: documents("final"), "documents-reviewed": lambda: documents("reviewed"),
               "discovery-flow": discovery_flow}
    if len(sys.argv) != 2 or sys.argv[1] not in actions:
        raise SystemExit("choose telegram, model or documents")
    raise SystemExit(actions[sys.argv[1]]())
