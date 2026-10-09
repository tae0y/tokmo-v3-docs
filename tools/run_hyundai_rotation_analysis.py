#!/usr/bin/env python3
"""Analyze Hyundai with existing Quant and publish the file contract.

Installing this entry in cron or pointing it at the live decision directory is
a separate activation step. --close-only never invokes a model.
"""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tokmo.programs.quant_decision_inputs import load_local_snapshot
from tokmo.programs.rotation_analysis import (
    RotationAnalysisError,
    policy_digest,
    run_rotation_analysis,
    window,
)
from tokmo.programs.rotation_analysis_delivery import make_notifier, make_warning
from tokmo.programs.rotation_quant_entry import ExistingQuantAnalyzer
from tokmo.records.quant_decision_reports import atomic_private
from tokmo.records.rotation_analysis import RotationAnalysisStore
from tokmo.records.rotation_analysis_inputs import (
    ConfirmedMarketClosure,
    read_rotation_context,
    saved_market_day,
)
from tokmo.values.quant_decisions import instant
from tokmo.values.rotation_policy import load_policies


def select_policy(state_dir):
    policies = load_policies((Path(state_dir) / "experiments.json").read_text())
    targets = [p for p in policies if p.market == "KR" and p.symbol == "005380"]
    if len(targets) != 1:
        raise ValueError("hyundai_policy_ambiguous")
    return targets[0]


def group_sender(data_root):
    from tokmo.facts.telegram_dialogue_live import send_dialogue_message
    from tokmo.programs.news_delivery_route import build_news_sender

    def missing(*args, **kwargs):
        raise ValueError("tokmo_group_route_missing")

    return build_news_sender(data_root, missing, send_dialogue_message)


def run_with_wait(run, *, clock, sleep, watch):
    result = run()
    while watch and result["status"] in {"WAITING_APPROVAL", "RUNNING", "BUSY"}:
        now = clock()
        _, _, deadline = window(now)
        # A delivered proposal carries its own 30-minute window in valid_until;
        # anything else ends at the analysis close.
        if result["status"] == "WAITING_APPROVAL" and result.get("valid_until"):
            deadline = instant(result["valid_until"])
        remaining = (deadline - now).total_seconds()
        if remaining > 0:
            sleep(min(15, remaining))
        result = run()
        # Another live process holds the lock. Its own process deadline and the
        # scheduled close check own termination; don't spin or replace it.
        if remaining <= 0 and result["status"] == "BUSY":
            break
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir",
        type=Path,
        required=True,
        help="runner state; decisions are written beneath it",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "var/data",
    )
    project = Path(__file__).resolve().parents[1]
    parser.add_argument("--project-root", type=Path, default=project)
    parser.add_argument(
        "--quant-entry", type=Path, default=project / "tools/run_quant_decisions.py"
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Quant model spec; default var/data/models.json quant.rotation_entry, else codex:gpt-6-astra",
    )
    parser.add_argument("--quant-python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--watch",
        action="store_true",
        help="wait on local approval state until the 10:40 analysis close or the delivered proposal's 30-minute window ends; sends the 5-minute reminder",
    )
    parser.add_argument(
        "--close-only",
        action="store_true",
        help="close uncompleted day at deadline; never start analysis",
    )
    args = parser.parse_args(argv)
    clock = lambda: datetime.now(UTC)
    now = clock()
    day, _, deadline = window(now)
    if args.close_only and now < deadline:
        print(json.dumps({"status": "NOT_DUE"}))
        return 0
    sender = group_sender(args.data_root)
    try:
        policy = select_policy(args.state_dir)
        store = RotationAnalysisStore(args.state_dir / "decisions")
        private = (
            args.state_dir / "analysis" / f"{day.isoformat()}-{policy.experiment_id}"
        )
        market = {}

        def check_market():
            try:
                market.update(
                    saved_market_day(args.data_root, now=clock(), market=policy.market)
                )
            except ConfirmedMarketClosure:
                raise
            except Exception as error:
                atomic_private(
                    private / "input-error.json",
                    {"error": type(error).__name__, "detail": str(error)[:800]},
                )
                raise RotationAnalysisError("input_preparation_failed") from error

        def inputs():
            try:
                context = read_rotation_context(
                    args.state_dir,
                    policy,
                    now=clock(),
                    regular_open_at=market["regular_open_at"],
                    last_closed_at=market["last_closed_at"],
                )
                data = load_local_snapshot(
                    args.project_root,
                    args.data_root,
                    as_of=clock(),
                    supplement=args.data_root / "ops/quant-inputs/latest.json",
                )
                atomic_private(private / "context.json", context)
                return data, context
            except Exception as error:
                atomic_private(
                    private / "input-error.json",
                    {"error": type(error).__name__, "detail": str(error)[:800]},
                )
                raise RotationAnalysisError("input_preparation_failed") from error

        if args.model is None:
            from tokmo.values.model_registry import (
                load_registry,
                registry_path,
                single_model,
            )

            args.model = single_model(
                load_registry(registry_path(args.project_root)),
                "quant",
                "rotation_entry",
            ).name
        quant = ExistingQuantAnalyzer(
            python=args.quant_python,
            entry=args.quant_entry,
            root=private,
            model=args.model,
        )

        def analyze(data, until):
            if args.close_only:
                raise RotationAnalysisError("close_only")
            result = quant(data, until)
            if policy_digest(select_policy(args.state_dir)) != policy_digest(policy):
                raise RotationAnalysisError("policy_changed_during_analysis")
            return result

        run = lambda: run_rotation_analysis(
            policy=policy,
            store=store,
            load_inputs=inputs,
            analyze=analyze,
            clock=clock,
            notify=make_notifier(sender),
            check_market=check_market,
            warn=make_warning(sender),
        )
        result = run_with_wait(
            run, clock=clock, sleep=time.sleep, watch=args.watch and not args.close_only
        )
        receipt = store.delivery_receipt(policy, day) or {}
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "trade_date": day.isoformat(),
                    "path": str(store.path(policy, day)),
                    "notification": receipt.get("status"),
                }
            )
        )
        return (
            2
            if result["status"] in {"FAILED", "EXPIRED"}
            or receipt.get("status") == "FAILED"
            else 0
        )
    except Exception as error:
        # Preparation before a valid policy/store is available is still visible.
        kind = type(error).__name__
        try:
            sender(
                "[톡모 · 현대차 회전 1주] 분석 실행 준비 실패. 오늘 거래를 보류해. 원인: "
                + kind
            )
        except Exception:
            pass
        print(json.dumps({"status": "FAILED", "error": kind}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
