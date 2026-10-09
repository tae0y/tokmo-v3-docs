#!/usr/bin/env python3
"""Reusable chronological Quant replay in a temporary report directory.

Case = {base: snapshot OR relative JSON file, steps: [{at, instruments,
cash?, extra_capital?, responses?, executions?: [{key, price, at}]}]}.
Each instrument update replaces fields by identity; evidence availability
is enforced by the shared parser. executions are hypothetical NEXT-time
fills, never broker calls. They are omitted when testing decisions only.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tokmo.programs.quant_decision_analysis import build_report
from tokmo.programs.quant_decision_calculations import portfolio_totals
from tokmo.records.quant_decision_reports import (
    DecisionReportStore,
    atomic_private,
    meaningful_changes,
)
from tokmo.values.quant_decisions import instant, number, parse_snapshot


def apply_step(data, step):
    data["as_of"] = step["at"]
    instruments = {f"{i['market']}:{i['symbol']}": i for i in data["instruments"]}
    for update in step.get("instruments", []):
        key = f"{update['market']}:{update['symbol']}"
        instruments[key] = dict(instruments.get(key, {}), **deepcopy(update))
    data["instruments"] = list(instruments.values())
    for name in ("fx", "cash", "account_summaries", "fees"):
        if name in step:
            data[name] = deepcopy(step[name])
    # Extras are per-step hypothetical scenarios, never injected as assets.
    data["extra_capital"] = deepcopy(step.get("extra_capital", []))
    for p in data.get("positions", []):
        p["observed_at"] = step["at"]
    for c in data.get("cash", []):
        c["observed_at"] = step["at"]


def fill_selected(data, report, executions):
    fills = []
    by_key = {e["key"]: e for e in executions}
    for row in report["results"]:
        o = row.get("selected_option")
        if not o or row["status"] != "ANALYZED" or o["scenario"] != "cash":
            continue
        execution = by_key.get(o["key"])
        if not execution:
            continue
        at = instant(execution["at"])
        if at <= instant(report["as_of"]):
            raise ValueError("execution must be after the decision")
        if o.get("requires_order_review") and not execution.get("orders_reconciled"):
            continue
        if not o["entry_ready"] and o["range_id"] not in execution.get(
            "conditions_met", []
        ):
            continue
        price = number(execution["price"], minimum=D(".00000001"))
        if o["action"] == "BUY" and price > D(o["price_upper"]):
            continue
        qty = D(o["quantity"])
        cash = next(
            (
                c
                for c in data["cash"]
                if c["account"] == o["account"] and c["currency"] == o["currency"]
            ),
            None,
        )
        if cash is None:
            continue
        fee = (
            abs(D(o["cash_delta"])) / D(o["amount"]) - 1
            if o["action"] == "BUY"
            else 1 - D(o["cash_delta"]) / D(o["amount"])
        )
        delta = (
            -qty * price * (1 + fee)
            if o["action"] == "BUY"
            else qty * price * (1 - fee)
        )
        if D(cash["available"]) + delta < 0:
            continue
        position = next(
            (
                p
                for p in data["positions"]
                if p["account"] == o["account"] and p["key"] == o["key"]
            ),
            None,
        )
        if o["action"] == "SELL" and (
            position is None
            or D(position["quantity"]) < qty
            or position.get("sellable") is None
            or D(position["sellable"]) < qty
        ):
            continue
        cash["available"] = str(D(cash["available"]) + delta)
        if cash.get("total_cash") is not None:
            cash["total_cash"] = str(D(cash["total_cash"]) + delta)
        if position is None:
            position = {
                "account": o["account"],
                "scope": o["scope"],
                "key": o["key"],
                "quantity": "0",
                "average_cost": "0",
                "sellable": "0",
                "observed_at": execution["at"],
            }
            data["positions"].append(position)
        old = D(position["quantity"])
        new = old + qty if o["action"] == "BUY" else old - qty
        realized = (
            qty * (price - D(position["average_cost"])) - qty * price * fee
            if o["action"] == "SELL"
            else -qty * price * fee
        )
        if o["action"] == "BUY":
            position["average_cost"] = str(
                (old * D(position["average_cost"]) + qty * price) / new
            )
        position["quantity"] = position["sellable"] = str(new)
        fills.append(
            {
                "account": o["account"],
                "scope": o["scope"],
                "currency": o["currency"],
                "key": o["key"],
                "action": o["action"],
                "quantity": str(qty),
                "price": str(price),
                "at": execution["at"],
                "cost": str(qty * price * fee),
                "realized_net": str(realized),
            }
        )
    return fills


def equity(data):
    totals = portfolio_totals(parse_snapshot(data))
    # Account/scope partitions stay separate; never add USD and KRW directly.
    return {key: row["total"] for key, row in totals.items()}


def replay(case, *, base, output, model=None):
    actual, buy_hold = deepcopy(base), deepcopy(base)
    reports = []
    fills = []
    curves = {}
    previous = None
    last_at = None
    flow_history = {}
    store = DecisionReportStore(output)
    for n, step in enumerate(case["steps"]):
        at = instant(step["at"])
        if last_at is not None and at <= last_at:
            raise ValueError("steps must be chronological")
        last_at = at
        if n and "cash" in step:
            raise ValueError(
                "cash resets would invalidate comparison; use explicit deposits"
            )
        for execution in step.get("executions", []):
            if n + 1 == len(case["steps"]) or instant(execution["at"]) > instant(
                case["steps"][n + 1]["at"]
            ):
                raise ValueError(
                    "a fill needs a later valuation step at or after its execution time"
                )
        apply_step(actual, step)
        apply_step(buy_hold, step)
        # Explicit deposits apply to both strategies; quote-only patches do
        # not reset the simulated strategy's cash after every trade.
        for deposit in step.get("deposits", []):
            for data in (actual, buy_hold):
                cash = next(
                    c
                    for c in data["cash"]
                    if c["account"] == deposit["account"]
                    and c["currency"] == deposit["currency"]
                )
                amount = number(deposit["amount"])
                cash["available"] = str(D(cash["available"]) + amount)
                if cash.get("total_cash") is not None:
                    cash["total_cash"] = str(D(cash["total_cash"]) + amount)
        snap = parse_snapshot(actual)
        for deposit in step.get("deposits", []):
            c = next(
                c
                for c in snap.cash
                if c.account == deposit["account"] and c.currency == deposit["currency"]
            )
            from tokmo.programs.quant_decision_calculations import convert

            partition = (
                f"{c.scope}:KRW" if snap.fx or c.currency == "KRW" else f"{c.scope}:USD"
            )
            flow_history[(n, partition)] = flow_history.get(
                (n, partition), D(0)
            ) + convert(
                snap, number(deposit["amount"]), c.currency, partition.split(":")[1]
            )
        report = build_report(
            snap,
            model=model,
            new_limit=case.get("new_limit", 8),
            recorded=step.get("responses") if model is None else None,
        )
        store.save(report)
        changes = meaningful_changes(report, previous)
        # This is a fake transport; night-pending decisions are not delivered.
        delivered = changes["disposition"] == "READY"
        if delivered:
            previous = report
        expected = step.get("expected", {})
        checks = []
        for key, allowed in expected.items():
            rows = [r for r in report["results"] if r["key"] == key]
            actions = [r["analysis"]["action"] for r in rows if r["analysis"]]
            checks.append(
                {
                    "key": key,
                    "allowed": allowed,
                    "actual": actions,
                    "passed": bool(actions) and all(a in allowed for a in actions),
                }
            )
        reports.append(
            {
                "step": n,
                "at": step["at"],
                "report_id": report["report_id"],
                "changes": changes,
                "fake_sent": delivered,
                "checks": checks,
                "failed_rows": sum(r["status"] == "FAILED" for r in report["results"]),
                "unanalysed_rows": sum(
                    r["status"] != "ANALYZED" for r in report["results"]
                ),
            }
        )
        values, baseline = equity(actual), equity(buy_hold)
        for key in values:
            curves.setdefault(key, []).append(
                {
                    "at": step["at"],
                    "proposed": str(values[key]),
                    "buy_hold": str(baseline.get(key, 0)),
                    "flow": str(flow_history.get((n, key), D(0))),
                }
            )
        new_fills = fill_selected(actual, report, step.get("executions", []))
        fills.extend(new_fills)
    metrics = {}
    for key, points in curves.items():

        def stats(field):
            vals = [D(p[field]) for p in points]
            peak = index = D(1)
            drawdown = D(0)
            returns = []
            for previous, p in zip(points, points[1:]):
                prior = D(previous[field])
                ret = (D(p[field]) - D(p["flow"])) / prior - 1 if prior > 0 else D(0)
                returns.append(ret)
                index *= 1 + ret
                peak = max(peak, index)
                if peak > 0:
                    drawdown = min(drawdown, index / peak - 1)
            return {
                "final_equity": str(vals[-1]),
                "max_drawdown": str(drawdown),
                "time_weighted_return": str(index - 1),
                "net_change_excluding_deposits": str(
                    vals[-1] - vals[0] - sum((D(p["flow"]) for p in points[1:]), D(0))
                ),
                "period_returns": [str(v) for v in returns],
            }

        metrics[key] = {"proposed": stats("proposed"), "buy_hold": stats("buy_hold")}
        scope, currency = key.split(":")
        final_snap = parse_snapshot(actual)
        from tokmo.programs.quant_decision_calculations import convert

        def converted(value, native):
            return convert(final_snap, D(value), native, currency)

        relevant = [
            f
            for f in fills
            if f["scope"] == scope and (final_snap.fx or f["currency"] == currency)
        ]
        by_key = {i.key: i for i in final_snap.instruments}
        unrealized = sum(
            (
                converted(
                    p.quantity * (by_key[p.key].price - p.average_cost),
                    by_key[p.key].currency,
                )
                for p in final_snap.positions
                if p.scope == scope
                and by_key[p.key].price is not None
                and (final_snap.fx or by_key[p.key].currency == currency)
            ),
            D(0),
        )
        metrics[key]["proposed"].update(
            realized_net=str(
                sum(
                    (converted(f["realized_net"], f["currency"]) for f in relevant),
                    D(0),
                )
            ),
            unrealized=str(unrealized),
            trade_count=len(relevant),
            costs=str(
                sum((converted(f["cost"], f["currency"]) for f in relevant), D(0))
            ),
        )
        positive = [
            (D(a), D(b))
            for a, b in zip(
                metrics[key]["proposed"]["period_returns"],
                metrics[key]["buy_hold"]["period_returns"],
            )
            if D(b) > 0
        ]
        metrics[key]["upside_participation"] = (
            str(
                sum((a for a, b in positive), D(0))
                / sum((b for a, b in positive), D(0))
            )
            if positive
            else None
        )
    summary = {
        "case": case.get("name", "unnamed"),
        "classification": case.get("classification", "synthetic"),
        "steps": reports,
        "fills": fills,
        "curves": curves,
        "metrics": metrics,
        "actual_sends": 0,
        "limitations": [
            "시나리오 체결은 사용자 실행을 가정한 비교입니다.",
            "관측 시점 사이의 저점은 포함하지 않습니다. 입출금은 각 관측 직전 발생한 것으로 조정합니다.",
            "실현손익은 매매 수수료를 포함합니다. 원화 손익 환산은 최종 평가 환율이며 별도 환차손익 귀속은 계산하지 않습니다.",
        ],
        "passed": bool(reports)
        and all(c["passed"] for r in reports for c in r["checks"])
        and not any(r["unanalysed_rows"] for r in reports),
    }
    atomic_private(Path(output) / "replay-summary.json", summary)
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--case", required=True, type=Path)
    p.add_argument("--mode", choices=("recorded", "live-model"), default="recorded")
    p.add_argument("--model", default="glm-5.3")
    p.add_argument("--model-env", type=Path, default=Path.home() / ".hermes/.env")
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--keep-state", action="store_true")
    args = p.parse_args(argv)
    case = json.loads(args.case.read_text())
    base = (
        case["base"]
        if isinstance(case["base"], dict)
        else json.loads((args.case.parent / case["base"]).read_text())
    )
    model = None
    if args.mode == "live-model":
        from quant_decision_models import QuantModel

        model = QuantModel(env_file=args.model_env, model=args.model)
    args.output.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="quant-replay-")
    root = Path(temporary.name)
    try:
        summary = replay(case, base=base, output=root, model=model)
        if model:
            summary["model_calls"] = model.calls
        if args.keep_state:
            import shutil

            dest = Path(tempfile.mkdtemp(prefix="case-", dir=args.output))
            for file in root.iterdir():
                shutil.copy2(file, dest / file.name)
            summary["state_directory"] = str(dest)
        atomic_private(args.output / "latest-replay-summary.json", summary)
        print(
            json.dumps(
                {k: summary[k] for k in ("case", "passed", "actual_sends", "metrics")},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if summary["passed"] else 2
    finally:
        temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
