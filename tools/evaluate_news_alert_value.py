"""Bounded fresh-model check of six synthetic, public-information-shaped NEWS cases.

Only --live starts model calls: six analyses, at most one correction each (12 maximum).
No account input, source fetch, production store, outbox, sender or runtime worker.
"""

import argparse
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from news_decision_models import build_models
from validate_news_activation import FOLDER, BoundedModel

from tokmo.programs.news_decision_engine import RECORDED_TIME, NewsDecisionEngine
from tokmo.records.news_decision_store import NewsDecisionStore, timestamp


def cases():
    at = "2026-09-07T15:45:00+09:00"

    def source(key, text, title, **fields):
        return (
            dict(
                id=key,
                text=text,
                title=title,
                observed_at=at,
                published_at=at,
                source_kind="NEWS",
                publisher="합성 평가 출처",
                url="https://example.invalid/" + key,
            )
            | fields
        )

    def prior(body, evidence=None):
        return {
            "id": "synthetic-event",
            "evidence": evidence or [],
            "updated_at": "2026-09-07T15:00:00+09:00",
            "decision": {
                "title": "이전 알림",
                "facts": body,
                "importance": "MEDIUM",
                "open": True,
            },
            "last_sent": {
                "alert_id": "synthetic-prior",
                "body": body,
                "delivered_at": "2026-09-07T15:01:00+09:00",
            },
            "pending_alerts": [],
        }

    listing = source(
        "dart:20260907900123:first:abc",
        "한빛로보틱스 / 유상증자 결정 / 기타법인 / 종목코드 없음",
        "한빛로보틱스 유상증자 결정",
        source_kind="DISCLOSURE",
        content_scope="listing_only",
        body_verified=False,
        published_at=None,
    )
    old = source(
        "war-old",
        "동쪽 항로에만 제한 통항을 허용했다. 서쪽 항로는 계속 봉쇄 중이다.",
        "당국 통항 공지",
        source_kind="OFFICIAL",
        observed_at="2026-09-07T14:00:00+09:00",
        published_at="2026-09-07T14:00:00+09:00",
    )
    return [
        {"id": "thin-discovery", "expected_notify": False, "evidence": [listing]},
        {
            "id": "grounded-discovery",
            "expected_notify": True,
            "evidence": [
                source(
                    "dart:20260907900123:document:def",
                    "한빛로보틱스는 공장 자동화용 협동로봇 제조사다. 공시 원문: 200억원 증자를 결의했다. 설비 증설 150억원, 연구개발 50억원에 사용한다. "
                    "현재 연간 생산능력 1000대를 2500대로 늘릴 계획이며 신규 해외 고객 공급계약에 대응하기 위한 것이다. 기존 연매출 400억원이다. 증자는 결의 단계로 납입은 미완료다.",
                    "한빛로보틱스 유상증자 원문",
                    source_kind="DISCLOSURE",
                    content_scope="original_document",
                    body_verified=True,
                )
            ],
        },
        {
            "id": "war-repeat",
            "expected_notify": False,
            "prior": prior(
                "미국과 이란의 선박 공격으로 유가가 상승했으며 호르무즈 통항량이 5월 이래 최저다. 공급 차질 위험을 점검해야 한다."
            ),
            "evidence": [
                source(
                    "war-rewrite",
                    "기존 선박 공격과 통항 감소를 다시 설명하는 해설이다. 새로운 공격·가격·통항 수치나 정책 변화는 없다.",
                    "호르무즈 공급 위험 지속",
                )
            ],
        },
        {
            "id": "distinct-regional-risk",
            "expected_notify": True,
            "prior": prior("호르무즈에서 선박 공격이 발생해 원유 운송 위험이 커졌다."),
            "evidence": [
                source(
                    "airspace",
                    "당국은 이스라엘·레바논 교전 확대에 따라 지금부터 민간 항공 영공을 전면 폐쇄한다고 공식 발표했다. "
                    "이전에는 민간 항공편이 운항했다. 주요 항공사들이 유럽-아시아 항공편을 취소하거나 우회한다고 발표했다.",
                    "교전 확대로 영공 폐쇄",
                    source_kind="OFFICIAL",
                )
            ],
        },
        {
            "id": "meaningful-close",
            "expected_notify": True,
            "prior": prior(
                "코스피는 3.34% 상승 개장했고 장 초반 6900선이다. 종가와 환율 움직임을 확인할 필요가 있다."
            ),
            "evidence": [
                source(
                    "kospi-close",
                    "거래소 공식 마감 자료: 코스피는 전일 대비 4.61% 오른 6995에 거래를 마쳤다. "
                    "개장 때 3.34% 상승에서 상승폭이 확대됐다. 원달러 환율도 18원 하락해 1380원에 마감했다. 오전 예상이 아니라 종가 확정 수치다.",
                    "코스피 4.61% 상승 마감",
                    source_kind="MARKET",
                    source_authority="OFFICIAL",
                )
            ],
        },
        {
            "id": "old-evidence-correction",
            "expected_notify": True,
            "prior": prior(
                "당국이 모든 항로를 개방해 해협 봉쇄가 전면 해제됐다. 공급 차질 위험이 사라졌다.",
                [old],
            ),
            "evidence": [old],
        },
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--case", action="append", choices=[row["id"] for row in cases()]
    )
    args = parser.parse_args(argv)
    selected = [row for row in cases() if not args.case or row["id"] in args.case]
    if not args.live:
        print(
            json.dumps(
                {
                    "cases": [row["id"] for row in selected],
                    "maximum_model_calls": 2 * len(selected),
                    "payload": "synthetic evidence; no account data",
                    "sending": "unavailable",
                }
            )
        )
        return 0
    folder = ROOT / ".hermes" / "evidence" / "news-value-20260907"
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / (
        "model-evaluation-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + ".json"
    )
    _, model = build_models(timeout=90)
    ledger = json.loads((FOLDER / "model-budget.json").read_text())
    model = BoundedModel(model, ledger)
    results, used = [], 0

    def bounded(request):
        nonlocal used
        if used >= 2 * len(selected):
            raise ValueError("evaluation_call_cap")
        used += 1
        return model(request)

    bounded.model = model.model
    bounded.max_output_tokens = model.max_output_tokens
    with tempfile.TemporaryDirectory(dir=folder, prefix="evaluation-") as temp:
        for index, case in enumerate(selected):
            store = NewsDecisionStore(Path(temp) / (str(index) + ".sqlite3"))
            try:
                if case.get("prior"):
                    store.save_event(case["prior"])

                def screen(req):
                    return {
                        "candidates": [
                            {
                                "evidence_ids": [row["id"] for row in req["evidence"]],
                                "event_id": case.get("prior", {}).get("id"),
                            }
                        ],
                        "ignored": [],
                    }

                engine = NewsDecisionEngine(store, screen, bounded, clock=RECORDED_TIME)
                result = engine.tick(
                    case["evidence"], timestamp("2026-09-07T15:45:00+09:00")
                )
                calls = [
                    dict(zip(("request", "response", "error"), row))
                    for row in store.connection.execute(
                        "SELECT request,response,error FROM news_decision_calls WHERE stage='analyze' ORDER BY rowid"
                    )
                ]
                decision = result["decisions"][0] if result["decisions"] else {}
                results.append(
                    {
                        "case": case["id"],
                        "expected_notify": case["expected_notify"],
                        "result": result,
                        "choice_matches": not result["failures"]
                        and decision.get("notify") == case["expected_notify"],
                        "calls": calls,
                    }
                )
                output.write_text(
                    json.dumps(
                        {"synthetic_only": True, "model_calls": used, "cases": results},
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                )
                print(
                    json.dumps(
                        {
                            "case": case["id"],
                            "notify": decision.get("notify"),
                            "matches": results[-1]["choice_matches"],
                            "model_calls": used,
                        }
                    ),
                    flush=True,
                )
            finally:
                store.close()
    print(
        json.dumps(
            {
                "report": str(output),
                "model_calls": used,
                "matches": sum(row["choice_matches"] for row in results),
            }
        )
    )
    return 0 if all(row["choice_matches"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
