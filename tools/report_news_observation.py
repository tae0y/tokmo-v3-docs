#!/usr/bin/env python3
"""Small, read-only stabilization report. Sending is explicit and once per day."""

import argparse
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from report_news_decisions import connect, report

from tokmo.records.news_decision_store import timestamp

KOREA = ZoneInfo("Asia/Seoul")


def read_json(path):
    return json.loads(Path(path).read_text())


def save_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def display_time(value):
    return timestamp(value).astimezone(KOREA).strftime("%Y-%m-%d %H:%M")


def previous_end(folder, cutoff, now):
    """A missed report stays in the next report's window, including late sends."""
    starts = [timestamp(cutoff)]
    for path in folder.glob("daily-*.receipt.json"):
        try:
            receipt = read_json(path)
            if receipt["status"] == "SENT" and timestamp(receipt["until"]) < now:
                starts.append(timestamp(receipt["until"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return max(starts)


def days(start, end):
    day = start.astimezone(UTC).date()
    while day <= end.astimezone(UTC).date():
        yield day
        day += timedelta(days=1)


def health(root, hermes, *, since, now, pulse_dir=None):
    details, issues = {}, []

    def section(label, read):
        try:
            details[label] = read()
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            details[label] = {"unavailable": type(exc).__name__}
            issues.append(f"{label} 기록 확인 불가")

    def fresh(label, at, minutes=10):
        age = (now - timestamp(at)).total_seconds()
        if age < -60 or age > minutes * 60:
            issues.append(f"{label} 기록 지연(마지막 {display_time(at)})")

    def worker():
        value = read_json(root / "news/decision-worker/latest.json")
        fresh("분석 작업", value["started_at"])
        if value["status"] != "OK":
            issues.append("최근 분석 작업 오류")
        if any(
            item.get("status") != "OK" for item in value.get("sources", {}).values()
        ):
            issues.append("분석용 입력 읽기 오류")
        if value.get("delivery", {}).get("activation_reason"):
            issues.append("뉴스 발송 조건 미충족")
        return {
            k: value.get(k)
            for k in ("status", "started_at", "input_states", "pending_proposals")
        }

    def sources():
        latest, counts = {}, Counter()
        folder = root / "news/group_runtime/receipts"
        # Only the recent primary receipts, not the large legacy receipt archive.
        for day in days(since, now):
            for path in sorted(
                folder.glob(f"news5-primary-v1:{day.isoformat()}T*.json")
            ):
                at = timestamp(path.name.split(":", 1)[1].rsplit(":", 1)[0])
                if not since <= at < now:
                    continue
                value = read_json(path)
                group = path.stem.rsplit(":", 1)[1]
                latest[group] = {"at": at.isoformat(), "status": value["status"]}
                counts[value["status"]] += 1
        for group, label in (
            ("A", "연합뉴스 증시"),
            ("B", "JTBC 속보"),
            ("C", "연합뉴스 산업"),
        ):
            if group not in latest:
                issues.append(f"{label} 수집 기록 없음")
            else:
                fresh(label, latest[group]["at"], 20)
                if latest[group]["status"] not in (
                    "SUCCEEDED",
                    "SUCCEEDED_WITH_CONFLICTS",
                ):
                    issues.append(f"{label} 최근 수집 실패")
        return {"latest": latest, "runs": dict(counts)}

    def disclosure():
        with closing(connect(root / "opendart-bulk.sqlite3")) as db:
            row = db.execute(
                "SELECT status,started_at FROM runs WHERE bgn_de=? AND end_de=? ORDER BY started_at DESC LIMIT 1",
                (now.astimezone(KOREA).strftime("%Y%m%d"),) * 2,
            ).fetchone()
        if row is None:
            issues.append("당일 공시 조회 기록 없음")
            return {}
        fresh("당일 공시 조회", row["started_at"], 20)
        if row["status"] != "complete":
            issues.append("최근 공시 조회 미완료")
        return dict(row)

    def engine():
        config = read_json(root / "news_decision.json")
        gate = read_json(root / "news_activation.json")
        if not all(
            (config.get("enabled"), config.get("delivery_enabled"), gate.get("enabled"))
        ):
            issues.append("뉴스 분석 또는 발송 꺼짐")
        continuous = (
            gate.get("continuous") is True
            and gate.get("window_end") is None
            and gate.get("delivery_mode") == "TELEGRAM_LIVE"
        )
        if not continuous and timestamp(gate["window_end"]) <= now:
            issues.append("뉴스 운영 승인 기간 만료")
        with closing(connect(root / "stores/news_episodes.sqlite3")) as db:
            trip = db.execute(
                "SELECT reason FROM live_delivery_trips WHERE activation_id=?",
                (gate["activation_id"],),
            ).fetchone()
            states = dict(
                db.execute(
                    "SELECT state,count(*) FROM news_decision_inputs GROUP BY state"
                )
            )
            stale_calls = db.execute(
                "SELECT count(*) FROM news_decision_calls WHERE status='STARTED' AND julianday(started_at)<julianday(?)",
                ((now - timedelta(minutes=10)).isoformat(),),
            ).fetchone()[0]
        if trip:
            issues.append("현재 뉴스 발송이 오류로 중지됨")
        if states.get("ERROR"):
            issues.append(f"미처리 분석 오류 {states['ERROR']}건")
        if stale_calls:
            issues.append(f"10분 넘게 종료되지 않은 AI 호출 {stale_calls}건")
        with closing(connect(root / "ops/ops_kernel.sqlite3")) as db:
            unsent = dict(
                db.execute(
                    "SELECT status,count(*) FROM alert_outbox WHERE alert_id LIKE 'nda-%' AND status!='SENT' GROUP BY status"
                )
            )
        if unsent:
            issues.append(
                "미발송 메시지 "
                + ", ".join(f"{key} {count}개" for key, count in unsent.items())
            )
        return {
            "input_states": states,
            "unsent_parts": unsent,
            "window_end": gate.get("window_end"),
            "continuous": continuous,
        }

    def pulse():
        counts, last_delivery = Counter(), None
        for day in days(since, now):
            for path in sorted(
                (pulse_dir or hermes / "runtime/tokmo-v3/pulse").glob(
                    day.strftime("%Y%m%d") + "T*/stdout.log"
                )
            ):
                at = datetime.strptime(path.parent.name, "%Y%m%dT%H%M").replace(
                    tzinfo=UTC
                )
                if not since <= at < now:
                    continue
                try:
                    value = read_json(path)
                except (OSError, ValueError):
                    if (now - at).total_seconds() > 120:
                        counts["unreadable"] += 1
                    continue
                counts["runs"] += 1
                if value.get("outcome") == "FAILED":
                    counts["failed"] += 1
                    if value.get("reason") == "cadence_not_complete":
                        counts["cadence_not_complete"] += 1
                if any(
                    s.get("stage") == "news_delivery" and s.get("status") == "complete"
                    for s in value.get("stages", [])
                ):
                    last_delivery = at.isoformat()
        if last_delivery:
            fresh("뉴스 발송 단계", last_delivery)
        else:
            issues.append("뉴스 발송 단계 완료 기록 없음")
        if counts["failed"]:
            issues.append(f"공용 실행기 일부 회차 실패 {counts['failed']}회")
        if counts["unreadable"]:
            issues.append(f"공용 실행기 결과 확인 불가 {counts['unreadable']}회")
        return {**counts, "last_news_delivery": last_delivery}

    for label, reader in (
        ("분석", worker),
        ("뉴스 수집", sources),
        ("공시 수집", disclosure),
        ("운영 설정", engine),
        ("발송 실행", pulse),
    ):
        section(label, reader)
    return {"issues": issues, "details": details}


def build_report(root, hermes, *, now, final=False, pulse_dir=None):
    root, hermes, now = Path(root), Path(hermes), timestamp(now)
    folder = root / "news/decision-worker/observations"
    try:
        cutoff = timestamp(read_json(root / "news_decision.json")["cutoff"])
    except (OSError, ValueError, KeyError, TypeError):
        cutoff = now - timedelta(days=1)
    start = cutoff if final else previous_end(folder, cutoff, now)
    result = {
        "since": start.isoformat(),
        "until": now.isoformat(),
        "final": final,
        "health": health(root, hermes, since=start, now=now, pulse_dir=pulse_dir),
    }
    try:
        result["metrics"] = report(
            root / "stores/news_episodes.sqlite3",
            root / "ops/ops_kernel.sqlite3",
            since=start,
            until=now,
        )
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        result["metrics"] = None
        result["health"]["issues"].append(
            f"분석·발송 집계 확인 불가({type(exc).__name__})"
        )
    result["body"] = format_report(result)
    return result


def format_report(value):
    issues = value["health"]["issues"]
    metrics = value["metrics"]
    lines = [
        "[중요 뉴스 안정화 " + ("종합 점검]" if value["final"] else "일일 점검]"),
        f"{display_time(value['since'])} ~ {display_time(value['until'])}",
        "",
        "운영 상태: "
        + (
            "확인할 문제가 있습니다."
            if issues
            else "현재 수집·분석·발송 경로에 이상 징후가 없습니다."
        ),
    ]
    sources = value["health"]["details"].get("뉴스 수집", {})
    if sources.get("runs"):
        runs = sources["runs"]
        failures = sum(
            count
            for status, count in runs.items()
            if status not in ("SUCCEEDED", "SUCCEEDED_WITH_CONFLICTS")
        )
        lines.append(f"주요 뉴스 조회 {sum(runs.values()):,}회 · 실패 {failures:,}회")
    disclosure = value["health"]["details"].get("공시 수집", {})
    if disclosure.get("status") == "complete":
        lines.append(
            "최근 당일 공시 조회 완료: " + display_time(disclosure["started_at"])
        )
    if metrics:
        states = metrics["input_states"]
        lines += [
            f"분석 입력 {sum(states.values()):,}건 · 처리 완료 {states.get('DONE', 0):,}건 · 대기 {states.get('READY', 0):,}건 · 오류 {states.get('ERROR', 0):,}건",
            f"실제 중요 알림 {metrics['sent_alerts']:,}건 발송",
            f"AI 호출 {metrics['model_calls']:,}회 · 실패 {metrics['failed_model_calls']:,}회 · 진행 중 {metrics['in_progress_model_calls']:,}회",
            f"토큰: 확인된 사용 {metrics['tokens_reported']:,}개 · 사용량 미확인분 예약/추정 {metrics['tokens_reserved_or_estimated']:,}개",
        ]
        delay = metrics["urgent_collected_to_sent_seconds"]
        if delay["samples"]:
            lines.append(
                f"긴급 알림 수집→발송: {delay['samples']}건, 최대 {delay['max']:.0f}초"
            )
        else:
            lines.append("긴급 알림 지연: 실제 발송 사례 없음")
    if issues:
        lines += ["", "확인할 내용:"] + ["• " + issue for issue in issues[:10]]
    pulse = value["health"]["details"].get("발송 실행", {})
    if pulse.get("last_news_delivery"):
        lines.append(
            "최근 뉴스 발송 단계 완료: " + display_time(pulse["last_news_delivery"])
        )
    lines += [
        "",
        "알림 품질: "
        + (
            "발송 사례가 없어 실제 탐지 품질은 아직 평가하지 못했습니다."
            if not metrics or not metrics["sent_alerts"]
            else "발송 건수만으로 적중·누락을 판정할 수 없습니다. 실제 사건과 대조가 필요합니다."
        ),
        "놓친 뉴스나 불필요한 알림이 있으면 이 그룹에 알려주세요. 재현 검증 사례로 확인할 수 있습니다.",
    ]
    if value["final"]:
        gate = value["health"]["details"].get("운영 설정", {}).get("window_end")
        lines.append(
            "정기 안정화 보고는 이번으로 종료합니다."
            + (f" 현재 뉴스 운영 승인 만료: {display_time(gate)}." if gate else "")
        )
    return "\n".join(lines)


def deliver_once(value, folder, sender):
    """Never retry an uncertain Telegram send; retain its report for inspection."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    key = ("final-" if value["final"] else "daily-") + timestamp(
        value["until"]
    ).astimezone(KOREA).strftime("%Y%m%d")
    path = folder / (key + ".receipt.json")
    receipt = {
        "status": "STARTED",
        "since": value["since"],
        "until": value["until"],
        "report": key + ".json",
    }
    try:
        with path.open("x") as stream:
            json.dump(receipt, stream)
    except FileExistsError:
        return read_json(path) | {"already_attempted": True}
    save_json(folder / receipt["report"], value)
    try:
        receipt["provider_message_id"] = str(sender(value["body"]))
        receipt["status"] = "SENT"
    except Exception as exc:
        receipt.update(status="UNKNOWN", error_class=type(exc).__name__)
    save_json(path, receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--hermes", required=True, type=Path)
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args(argv)
    from tokmo.programs.news_decision_config import (
        delivery_window_open,
        read_decision_config,
    )

    if args.send and not delivery_window_open(
        read_decision_config(args.root), datetime.now(UTC)
    ):
        return 0
    value = build_report(
        args.root,
        args.hermes,
        now=datetime.now(UTC),
        final=args.final,
        pulse_dir=Path(__file__).resolve().parents[1] / "var/runtime/pulse",
    )
    if not args.send:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    from tokmo.facts.telegram_dialogue_live import send_dialogue_message
    from tokmo.programs.news_delivery_route import build_news_sender

    sender = build_news_sender(args.root, None, send_dialogue_message)
    if sender is None:
        raise ValueError("NEWS group route required for stabilization feedback")
    if not delivery_window_open(read_decision_config(args.root), datetime.now(UTC)):
        return 0
    result = deliver_once(
        value, args.root / "news/decision-worker/observations", sender
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "SENT" else 2


if __name__ == "__main__":
    raise SystemExit(main())
