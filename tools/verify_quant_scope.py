#!/usr/bin/env python3
"""Finite model checks for saved synthetic holding, ETF and discovery cases."""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tokmo.programs.quant_decision_analysis import build_report, render_report
from tokmo.programs.quant_rotation_scenario import after_selected_sales
from tokmo.records.quant_decision_reports import atomic_private
from tokmo.values.quant_decisions import parse_snapshot


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--case", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--model", default="glm-5.3")
    p.add_argument("--model-env", type=Path, default=Path.home() / ".hermes/.env")
    p.add_argument("--max-calls", type=int, default=12)
    a = p.parse_args(argv)
    case = json.loads(a.case.read_text())
    if (
        a.max_calls < 1
        or not case.get("cases")
        or any(c["snapshot"].get("synthetic") is not True for c in case["cases"])
    ):
        p.error("가상 사례와 유한 호출 상한이 필요합니다")
    if a.output.exists() and any(a.output.iterdir()):
        p.error("이전 결과를 덮어쓰지 않습니다")
    a.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_private(a.output / "case.json", case)
    from quant_decision_models import QuantModel

    delegate = QuantModel(env_file=a.model_env, model=a.model, max_tokens=5000)
    calls = []

    def model(prompt):
        if len(calls) >= a.max_calls:
            raise RuntimeError("사례 검증 호출 상한 도달")
        row = dict(
            model=a.model,
            request=prompt,
            status="STARTED",
            started_at=datetime.now(UTC).isoformat(),
        )
        calls.append(row)
        atomic_private(a.output / "calls.json", calls)
        try:
            result = delegate(prompt)
            row.update(delegate.calls[-1])
            return result
        finally:
            if row["status"] == "STARTED" and delegate.calls:
                row.update(delegate.calls[-1])
            atomic_private(a.output / "calls.json", calls)

    results = []

    def save(name, report):
        atomic_private(a.output / (name + ".json"), report)
        path = a.output / (name + ".txt")
        path.write_text(render_report(report) + "\n")
        path.chmod(0o600)
        return dict(
            name=name,
            passed=bool(report["results"])
            and all(r["status"] == "ANALYZED" for r in report["results"]),
            rows=[
                dict(
                    key=r["key"],
                    scope=r["scope"],
                    status=r["status"],
                    action=r["analysis"]["action"] if r["analysis"] else None,
                    errors=r["errors"],
                )
                for r in report["results"]
            ],
        )

    for c in case["cases"]:
        snapshot = parse_snapshot(c["snapshot"])
        report = build_report(snapshot, model=model, only_keys=c["only_keys"])
        results.append(save(c["name"], report))
        if c.get("rotation"):
            projected, rotation = after_selected_sales(snapshot, report)
            atomic_private(a.output / (c["name"] + "-rotation.json"), rotation)
            if projected:
                alternative = build_report(
                    projected,
                    model=model,
                    include_holdings=False,
                    exclude_new=[s["key"] for s in rotation["sold"]],
                )
                results.append(save(c["name"] + "-replacement", alternative))
            else:
                results.append(
                    dict(
                        name=c["name"] + "-replacement",
                        passed=False,
                        reason="모델이 매도안을 선택하지 않아 교체 흐름 미실행",
                    )
                )
        summary = dict(
            cases=results,
            calls=len(calls),
            reported_tokens=sum(
                r.get("usage", {}).get("total_tokens", 0) for r in calls
            ),
            actual_sends=0,
            actual_orders=0,
            classification="synthetic",
            pass_requires_text_assessment=True,
        )
        atomic_private(a.output / "report.json", summary)
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    return 0 if all(r["passed"] for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
