#!/usr/bin/env python3
"""PAPER CLI consuming credential-free facts; no separate source authentication."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from tokmo.programs.opportunity_rotation import Paper, build_packet, pending
from tokmo.programs.quant_decision_context import collect_decision_context
from tokmo.records.quant_decision_reports import atomic_private
from tokmo.values.opportunity_rotation import BENCHMARK, KST, START_DATE, Costs, instant

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_ROOT = PROJECT_ROOT / "var/data/opportunity-rotation/paper"


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    return json.loads(path.read_text(), object_pairs_hook=unique)


def now():
    return datetime.now(KST)


def connect(args):
    # Deprecated: the former live branches below are unreachable. Kept only
    # for compatibility with old invocations, which fail before any source I/O.
    raise ValueError("STANDALONE_TOSS_DISABLED: use existing Pulse captured facts")


def attach_context(data, root):
    universe = {c["symbol"]: (c["name"],) for c in data["candidates"]}
    for c in data["candidates"]:
        evidence, status = collect_decision_context(
            root,
            dict(market="KR", symbol=c["symbol"]),
            as_of=instant(data["as_of"]),
            universe=universe,
        )
        c.update(evidence=evidence, context_status=status)
        for key, kind in (("news", "news"), ("disclosure", "disclosure")):
            if any(e["kind"] == kind for e in evidence):
                c["missing"] = [m for m in c["missing"] if m != key]
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="기회 탐색 PAPER — 실제 주문/계좌 연결 없음"
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=DEFAULT_STATE_ROOT,
        help="기본값 PROJECT/var/data/opportunity-rotation/paper; 별도 경로는 검증용",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument(
        "--costs",
        type=Path,
        required=True,
        help="label 및 buy/sell/stock_tax/benchmark_tax_rate 명시 JSON",
    )
    for name in ("discover", "advance"):
        command = sub.add_parser(name)
        group = command.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--source-file", type=Path, help="명시적 보존/합성 사실 replay, 출처 유지"
        )
        group.add_argument(
            "--toss",
            action="store_true",
            help="폐기된 독립 조회: 항상 거부; Pulse captured 자료와 step 사용",
        )
        command.add_argument(
            "--token-stdin", action="store_true", help="폐기된 인자; stdin을 읽지 않음"
        )
        command.add_argument(
            "--max-source-calls", type=int, help="폐기된 독립 조회 예산 인자"
        )
        if name == "discover":
            command.add_argument("--output", type=Path, required=True)
            command.add_argument(
                "--context-root",
                type=Path,
                default=PROJECT_ROOT / "var/data",
                help="기존 NEWS/OpenDART read-only 자료 root",
            )
    decide = sub.add_parser("decide")
    decide.add_argument("--packet", type=Path, required=True)
    decide.add_argument("--decision-file", type=Path, required=True)
    decide.add_argument(
        "--at", help="격리된 검증 상태에서만 사용하는 명시적 replay 시각"
    )
    cancel = sub.add_parser("cancel")
    cancel.add_argument("--id", required=True)
    cancel.add_argument("--reason", required=True)
    step = sub.add_parser("step")
    step.add_argument(
        "--captured",
        type=Path,
        default=PROJECT_ROOT / "var/data/opportunity-rotation/captured.json",
    )
    step.add_argument(
        "--manifest",
        type=Path,
        default=PROJECT_ROOT / "ops/opportunity-rotation-product-approval.json",
    )
    step.add_argument("--context-root", type=Path, default=PROJECT_ROOT / "var/data")
    step.add_argument("--at", help="temporary-state replay only")
    step.add_argument(
        "--exception-file",
        type=Path,
        help="structured material official evidence id and reason",
    )
    sub.add_parser("report")
    sub.add_parser("journal")
    args = parser.parse_args(argv)
    source = None
    succeeded = False
    try:
        paper = Paper(args.state_root)
        if args.command == "init":
            result = paper.initialize(Costs(**read_json(args.costs)))
        elif args.command in ("discover", "advance"):
            state = paper.state()
            if args.source_file:
                if args.state_root.resolve() == DEFAULT_STATE_ROOT.resolve():
                    raise ValueError(
                        "source-file replay requires temporary --state-root"
                    )
                data = read_json(args.source_file)
            else:
                source = connect(args)
                if args.command == "discover":
                    tracked = set(state["positions"]) | {
                        p["symbol"] for p in pending(state)
                    }
                    data = attach_context(
                        source.discover(held_symbols=tuple(sorted(tracked))),
                        args.context_root,
                    )
                else:
                    end = now()
                    start = (
                        instant(state["as_of"])
                        if state["as_of"]
                        else instant(START_DATE + "T00:00:00+09:00")
                    )
                    if state["benchmark"] is None:
                        start = min(start, instant(START_DATE + "T00:00:00+09:00"))
                    for p in pending(state):
                        start = min(start, instant(p["proposed_at"]))
                    symbols = (
                        set(state["positions"])
                        | {p["symbol"] for p in pending(state)}
                        | {BENCHMARK}
                    )
                    data = source.capture(symbols=symbols, start=start, end=end)
            if args.command == "discover":
                result = build_packet(data, state)
                atomic_private(args.output, result)
                atomic_private(
                    args.state_root / "packets" / (result["id"] + ".json"), result
                )
            else:
                result = paper.advance(data)
        elif args.command == "step":
            from opportunity_rotation_model import ProductModel

            from tokmo.programs.opportunity_rotation_schedule import run_slot

            if args.at and args.state_root.resolve() == DEFAULT_STATE_ROOT.resolve():
                raise ValueError("replay --at requires temporary --state-root")
            clock = (lambda: instant(args.at)) if args.at else now
            captured = read_json(args.captured) if args.captured.exists() else {}
            observed = clock()
            if "market" in captured:
                if instant(captured["market"]["as_of"]) > observed:
                    raise ValueError("future captured market")
                paper.advance(captured["market"], observed_at=observed.isoformat())
            if "discovery" not in captured:
                outcome = dict(
                    status="DATA_NOT_READY", reason="capture acquisition incomplete"
                )
            else:
                data = attach_context(captured["discovery"], args.context_root)
                model = ProductModel(
                    manifest=args.manifest, receipt_root=args.state_root / "receipts"
                )
                outcome = run_slot(
                    paper,
                    data,
                    clock=clock,
                    model=model,
                    exceptional=read_json(args.exception_file)
                    if args.exception_file
                    else None,
                )
            result = dict(decision=outcome, portfolio=paper.report())
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            return 2 if outcome["status"] == "FAILED" else 0
        elif args.command == "decide":
            if args.at and args.state_root.resolve() == DEFAULT_STATE_ROOT.resolve():
                raise ValueError("replay --at requires temporary --state-root")
            result = paper.decide(
                read_json(args.packet),
                read_json(args.decision_file),
                args.at or now().isoformat(),
            )
        elif args.command == "cancel":
            paper.cancel(args.id, now().isoformat(), args.reason)
            result = paper.report()
        elif args.command == "journal":
            result = paper.state()
        else:
            result = paper.report()
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        succeeded = True
        return 0
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(
            json.dumps(dict(status="FAILED", error=str(exc)), ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    finally:
        if source is not None:
            receipt = dict(
                at=now().isoformat(),
                command=args.command,
                budget=source.max_calls,
                calls=source.calls,
                status="RETURNED" if succeeded else "FAILED",
                note="계좌/토큰 저장 없음; source failure 뒤 재시도 없음",
            )
            name = "source-" + now().strftime("%Y%m%dT%H%M%S%f") + ".json"
            atomic_private(args.state_root / "receipts" / name, receipt)


if __name__ == "__main__":
    raise SystemExit(main())
