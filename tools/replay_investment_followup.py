#!/usr/bin/env python3
"""Replay small chronological cases through record → news → quant → local receipt.

Case files and temporary stores are reusable. There is no live sender or
production database path in this runner. Do not supply future snapshots.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from quant_decision_models import QuantModel

from tokmo.programs.investment_followup import (
    deliver_pending,
    record_text,
)
from tokmo.programs.investment_followup_worker import run_tick
from tokmo.records.investment_followup import InvestmentFollowupStore
from tokmo.values.quant_decisions import instant, parse_snapshot


class ExactRequestReuse:
    """Small replay-only cache. Changed prompt/history means a new model call."""

    def __init__(self, model, source, role):
        self.model = model.model
        self.backend = model
        self.calls = []
        self.cached = {}
        self.reasoning_effort = getattr(model, "reasoning_effort", None)
        source = Path(source)
        report = json.loads((source / "report.json").read_text())
        # Older replay reports list main-model calls before screen-model calls.
        roles = report.get("model_roles") or {
            "analysis": report["model_calls"][0]["model"],
            "screen": report["model_calls"][-1]["model"],
        }
        if roles[role] != self.model:
            raise ValueError("재사용할 모델이 다릅니다")
        saved_effort = (report.get("model_reasoning_efforts") or {}).get(role)
        if self.model.startswith("codex:") and saved_effort != self.reasoning_effort:
            # A different/unknown reasoning setting is not the same experiment.
            # Run fresh calls, keeping reuse available for the unchanged role.
            return
        for path in source.glob("*/followup.sqlite3"):
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                for key, request, response, status, usage in conn.execute(
                    "SELECT id,request,response,status,usage FROM investment_calls WHERE response IS NOT NULL ORDER BY rowid"
                ):
                    if (key.startswith("match:")) != (role == "screen"):
                        continue
                    self.cached.setdefault(
                        request,
                        (
                            json.loads(response),
                            str(path),
                            key,
                            status,
                            json.loads(usage or "{}"),
                        ),
                    )
            finally:
                conn.close()

    def __call__(self, prompt):
        if prompt in self.cached:
            raw, path, key, status, usage = self.cached[prompt]
            self.calls.append(
                {
                    "model": self.model,
                    "reasoning_effort": self.reasoning_effort,
                    "status": "REUSED_EXACT_REQUEST",
                    "usage": {"total_tokens": 0},
                    "original_usage": usage,
                    "source_database": path,
                    "source_call": key,
                    "original_status": status,
                }
            )
            return deepcopy(raw)
        value = self.backend(prompt)
        self.calls.append(deepcopy(self.backend.calls[-1]))
        return value


def run_case(case_path, output, *, model, screen_model=None, branches=None):
    case_path = Path(case_path)
    output = Path(output)
    case = json.loads(case_path.read_text())
    if case.get("synthetic") is not True:
        raise ValueError("이 runner는 가상 선택/계좌임을 명시한 사례를 받습니다")
    if output.exists() and any(output.iterdir()):
        raise ValueError("보존된 재현을 덮어쓰지 않습니다. 새 출력 폴더를 지정하세요")
    output.mkdir(parents=True, exist_ok=True)
    base = json.loads((case_path.parent / case["snapshot"]).resolve().read_text())
    report = {
        "case": case["id"],
        "synthetic": True,
        "transport": "LOCAL_REPLAY_ONLY",
        "branches": [],
        "model_roles": {
            "analysis": getattr(model, "model", None),
            "screen": getattr(screen_model or model, "model", None),
        },
        "model_reasoning_efforts": {
            "analysis": getattr(model, "reasoning_effort", None),
            "screen": getattr(screen_model or model, "reasoning_effort", None),
        },
    }
    for branch in case["branches"]:
        if branches and branch["id"] not in branches:
            continue
        folder = output / branch["id"]
        folder.mkdir()
        store = InvestmentFollowupStore(folder / "followup.sqlite3")
        db = sqlite3.connect(folder / "news.sqlite3")
        db.execute("create table news_decision_events(id text primary key,state text)")
        data = deepcopy(base)
        data["synthetic"] = True
        for item in data["instruments"]:
            rename = case.get("instruments", {}).get(
                item["market"] + ":" + item["symbol"]
            )
            if rename:
                item.update(name=rename["name"], symbol=rename["symbol"])
        for p in data["positions"]:
            rename = case.get("instruments", {}).get(p["key"])
            if rename:
                p["key"] = p["key"].split(":")[0] + ":" + rename["symbol"]
            if "quantity" in branch:
                p["quantity"] = p["sellable"] = str(branch["quantity"])
        for cash in data["cash"]:
            if cash["currency"] == "KRW" and "cash_krw" in branch:
                cash["available"] = cash["total_cash"] = str(branch["cash_krw"])
        if branch.get("quantity") == 0:
            data["positions"] = []
        snapshot = parse_snapshot(data)
        at = instant(data["as_of"])
        catalog = {
            "accounts": [{"account": "personal-a", "scope": "personal"}],
            "instruments": {i.key: i.name for i in snapshot.instruments},
        }
        result = record_text(
            store,
            text=branch["text"],
            source_id="replay:" + branch["id"],
            at=at,
            catalog=catalog,
            model=model,
        )
        entry = {"id": branch["id"], "record": result, "steps": []}
        report["branches"].append(entry)
        if not result.get("choice"):
            entry["error"] = "CHOICE_NOT_RECORDED"
        else:
            for step in case["steps"]:
                at = instant(step["at"])
                data["as_of"] = at.isoformat()
                for item in data["instruments"]:
                    item["price_at"] = at.isoformat()
                    if item["symbol"] == case["target_symbol"]:
                        item["price"] = str(step["price"])
                        item["evidence"] = [
                            {
                                "id": "business-current",
                                "kind": "business",
                                "text": step["business"],
                                "source": "합성 기업 자료",
                                "available_at": at.isoformat(),
                            }
                        ]
                for p in data["positions"]:
                    p["observed_at"] = at.isoformat()
                for c in data["cash"]:
                    c["observed_at"] = at.isoformat()
                news = step["event"]
                event = {
                    "id": news["id"],
                    "updated_at": at.isoformat(),
                    "decision": news["decision"],
                    "evidence": [
                        {
                            "id": news["evidence_id"],
                            "observed_at": at.isoformat(),
                            "title": news["decision"]["title"],
                            "text": news["text"],
                            "publisher": "합성 공식 발표",
                            "source_kind": "NEWS",
                        }
                    ],
                }
                event["decision"]["evidence_ids"] = [news["evidence_id"]]
                db.execute(
                    "insert into news_decision_events values(?,?) on conflict(id) do update set state=excluded.state",
                    (event["id"], json.dumps(event, ensure_ascii=False)),
                )
                db.commit()
                tick = run_tick(
                    store,
                    news_path=folder / "news.sqlite3",
                    snapshot=parse_snapshot(data),
                    at=at,
                    model=model,
                    screen_model=screen_model,
                    max_reviews=10,
                )
                local = []

                def sender(body):
                    local.append(body)
                    return "replay:" + branch["id"] + ":" + step["id"]

                delivery = deliver_pending(store, at=at, sender=sender)
                latest = store.latest_review(result["choice"]["id"], at=at)
                entry["steps"].append(
                    {
                        "id": step["id"],
                        "tick": tick,
                        "delivery": delivery,
                        "messages": local,
                        "assessment": latest["assessment"] if latest else None,
                        "selected_option": latest.get("selected_option")
                        if latest
                        else None,
                    }
                )
                (output / "report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n"
                )
                print(
                    json.dumps(
                        {
                            "branch": branch["id"],
                            "step": step["id"],
                            "reviewed": len(tick["reviewed"]),
                            "sent_locally": len(local),
                            "errors": tick["errors"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        store.close()
        db.close()
    models = [model] + (
        [screen_model] if screen_model and screen_model is not model else []
    )
    report["model_calls"] = [c for m in models for c in getattr(m, "calls", [])]
    report["usage"] = {
        "calls": len(report["model_calls"]),
        "tokens": sum(
            c.get("usage", {}).get("total_tokens", 0) for c in report["model_calls"]
        ),
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    lines = ["가상 선택/계좌의 시간순 후속 검증 · 실제 메시지 발송 없음"]
    for b in report["branches"]:
        lines += [
            "",
            b["id"],
            b["record"].get("choice", {}).get("raw_text", "기록 실패"),
        ]
        for s in b["steps"]:
            lines += ["", s["id"], *s["messages"]]
    (output / "examples.md").write_text("\n".join(lines) + "\n")
    for p in output.rglob("*"):
        if p.is_file():
            p.chmod(0o600)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--case", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--model", default="glm-5.3")
    p.add_argument("--screen-model", default="glm-5.3")
    p.add_argument(
        "--analysis-reasoning-effort",
        default="medium",
        choices=("low", "medium", "high", "xhigh", "max"),
    )
    p.add_argument("--model-env", type=Path, default=Path.home() / ".hermes/.env")
    p.add_argument("--branch", action="append", help="변경 영향이 있는 분기만 재검증")
    p.add_argument(
        "--reuse-calls-from",
        type=Path,
        help="같은 모델·완전히 같은 요청만 저장 응답 재사용. 변경된 앞선 판단은 새 호출",
    )
    a = p.parse_args()
    model = QuantModel(
        env_file=a.model_env,
        model=a.model,
        max_tokens=6000,
        reasoning_effort=a.analysis_reasoning_effort,
    )
    screen = QuantModel(env_file=a.model_env, model=a.screen_model, max_tokens=3000)
    if a.reuse_calls_from:
        model = ExactRequestReuse(model, a.reuse_calls_from, "analysis")
        screen = ExactRequestReuse(screen, a.reuse_calls_from, "screen")
    report = run_case(
        a.case, a.output, model=model, screen_model=screen, branches=a.branch
    )
    print(json.dumps(report["usage"]))
    return (
        2
        if any(
            b.get("error") or any(s["tick"]["errors"] for s in b["steps"])
            for b in report["branches"]
        )
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
