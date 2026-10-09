import pytest
from news_decision_models import response_schema
from test_news_decision_engine import decision, observation
from test_news_evening_digest import at

from tokmo.programs.news_decision_engine import evening_digest_alerts, validate_decision
from tokmo.programs.news_decision_outbox import _digest_parts


def test_title_and_interest_line_survive_block_splitting():
    alerts = [
        dict(
            id=str(n),
            event_id="e" + str(n),
            created_at=at("18:00").isoformat(),
            evening_slot=at("18:00").isoformat(),
            digest_hour=18,
            headline="새 계약 소식 " + str(n),
            body="unused",
            digest_summary="새 고객을 확보하며 사업 확장의 근거가 생겼고, 실제 매출로 이어지는지 살펴볼 소식.",
        )
        for n in range(20)
    ]
    digest = evening_digest_alerts(alerts)
    assert len(digest["entries"]) == 20
    parts = _digest_parts(digest["body"], limit=200)
    assert len(parts) > 1 and "".join(parts) == digest["body"]
    for item in digest["entries"]:
        block = f"{item['number']}. {item['title']}\n   - {item['summary']}"
        assert sum(block in part for part in parts) == 1
    assert all(len(part.encode("utf-16-le")) // 2 <= 200 for part in parts)


@pytest.mark.parametrize(
    "summary",
    [
        None,
        "",
        123,
        "짧은 의미",
        "확인되지 않은 공급 관계는 추가 점검이 필요하다. " * 5,
    ],
)
def test_display_length_or_missing_field_does_not_change_notification_decision(summary):
    source = observation("a", at("10:00"))
    raw = decision(["a"], digest_summary=summary)
    parsed = validate_decision(raw, [source], None)
    assert (
        parsed["notify"] == raw["notify"] and parsed["importance"] == raw["importance"]
    )
    if isinstance(summary, str) and summary:
        assert (
            parsed["digest_summary"] == summary.strip()
        )  # no claim-changing character cut
    else:
        assert "digest_summary" not in parsed


def test_nullable_copy_field_is_part_of_existing_final_model_response_only():
    assert response_schema("analyze")["properties"]["digest_summary"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }
    assert "digest_summary" not in response_schema("screen")["properties"]
