"""Strict-TDD RED suite: composed 12man archived-report admission seam.

The seam under test composes the existing product seams only —
``provenance_from_archive_meta``, ``parse_report_tracking_blocks``,
``admit_report_candidates``, ``OpsKernelStore`` — into one deterministic,
clock-free program call that admits every closed TRACKING_CANDIDATE block
of one archived report and returns a bounded machine-readable read-back.

Every store lives under ``tmp_path``; the production runtime root is never
touched.
"""

import json
from datetime import UTC, datetime

import pytest

from tokmo.programs.youtube_report_admission import (
    admit_archived_report,
    observed_at_from_archive_meta,
)
from tokmo.programs.youtube_tracking import TrackingCandidateParseError
from tokmo.records.ops_kernel import OpsKernelStore


def make_meta(
    *,
    video_id="YlVhQO-Fkc8",
    published="2026-08-13T04:21:51+00:00",
    fetched_at="2026-08-13T05:15:02+00:00",
    digest="a" * 64,
):
    meta = {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": "[12시에 만나요] fixture",
        "published": published,
        "transcript_sha256": digest,
        "extraction": "innertube-android-srv3-v1",
        "caption_kind": "ko-asr",
    }
    if fetched_at is not None:
        meta["fetched_at"] = fetched_at
    return meta


def make_block(statement, entity, *, invalidate=None):
    invalidate = invalidate or f"disclosure:{entity}:exit"
    return f"""=== TRACKING_CANDIDATE BEGIN ===
STATEMENT: {statement}
TRIGGER: EVENT_OCCUR | entity:{entity}:stake_change
SUPPORT: yt:2026-08-13:00:14
INVALIDATE: {invalidate}
NEXT_CHECK: none
TOUCH: PERSONAL
KEYS: entity:{entity}
=== TRACKING_CANDIDATE END ==="""


SEED_STATEMENT = (
    "싱가포르 국부펀드의 삼성전자 장기 지분 보유는 반도체 장기 성장 베팅의 지속 신호다"
)

MALFORMED_BLOCK = """=== TRACKING_CANDIDATE BEGIN ===
STATEMENT: THESIS_UNARTICULATED
TRIGGER: EVENT_OCCUR | entity:005930:x
SUPPORT: yt:2026-08-13:00:14
INVALIDATE: metric:005930:y
NEXT_CHECK: none
TOUCH: NONE
KEYS: entity:005930
=== TRACKING_CANDIDATE END ==="""


@pytest.fixture()
def store(tmp_path):
    kernel = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    yield kernel
    kernel.close()


def seed_day_one(store):
    """Admit the day-one report so one hypothesis already exists."""
    meta = make_meta(
        video_id="seedvideo001",
        published="2026-08-12T04:20:00+00:00",
        fetched_at="2026-08-12T05:10:00+00:00",
        digest="b" * 64,
    )
    report = make_block(SEED_STATEMENT, "005930")
    result = admit_archived_report(store, meta=meta, report_text=report)
    assert result["admission"]["created"] == 1
    return result


# --- observed_at deterministic rule ----------------------------------------


def test_observed_at_prefers_fetched_at_over_published():
    observed = observed_at_from_archive_meta(make_meta())
    assert observed == datetime(2026, 8, 13, 5, 15, 2, tzinfo=UTC)


def test_observed_at_falls_back_to_published_only_when_fetched_absent():
    observed = observed_at_from_archive_meta(make_meta(fetched_at=None))
    assert observed == datetime(2026, 8, 13, 4, 21, 51, tzinfo=UTC)


def test_observed_at_present_but_invalid_fetched_fails_closed():
    with pytest.raises(TrackingCandidateParseError):
        observed_at_from_archive_meta(make_meta(fetched_at="2026-08-13 05:15:02"))
    with pytest.raises(TrackingCandidateParseError):
        observed_at_from_archive_meta(make_meta(fetched_at="not-a-time"))


# --- mixed outcome against a seeded store -----------------------------------


def test_mixed_report_two_new_plus_one_revision_against_seeded_store(store):
    seed_day_one(store)
    meta = make_meta()
    report = "\n".join(
        (
            make_block(
                SEED_STATEMENT,
                "005930",
                invalidate="disclosure:005930:exit;metric:005930:drop",
            ),
            make_block("현대차 미국 관세 유예는 완성차 마진 회복 신호다", "005380"),
            make_block("하이닉스 HBM 증설은 장비주 수주 선행 신호다", "000660"),
        )
    )
    result = admit_archived_report(store, meta=meta, report_text=report)
    assert result["source_ref"] == "YlVhQO-Fkc8"
    assert result["parsed"] == {"valid_candidates": 3, "malformed_blocks": 0}
    assert result["admission"]["created"] == 2
    assert (
        result["admission"]["revisions"] + result["admission"]["semantic_sightings"]
    ) == 1
    assert result["admission"]["duplicates"] == 0
    assert result["admission"]["admission_failures"] == 0
    statuses = sorted(entry["status"] for entry in result["candidates"])
    assert statuses == ["NEW", "NEW", "NEW_SIGHTING_OR_REVISION"]
    revision_entry = next(
        entry
        for entry in result["candidates"]
        if entry["status"] == "NEW_SIGHTING_OR_REVISION"
    )
    assert revision_entry["statement_excerpt"].startswith("싱가포르 국부펀드")
    # durable read-back, not a draft claim (delta contract §4)
    assert result["read_back"]["sightings_for_revision"] == 3
    assert result["read_back"]["source_revision_registered"] is True
    assert len(store.read_open_hypotheses()) == 3


# --- replay idempotency ------------------------------------------------------


def test_identical_replay_reports_duplicates_and_writes_no_new_rows(store):
    seed_day_one(store)
    meta = make_meta()
    report = "\n".join(
        (
            make_block(
                SEED_STATEMENT,
                "005930",
                invalidate="disclosure:005930:exit;metric:005930:drop",
            ),
            make_block("현대차 미국 관세 유예는 완성차 마진 회복 신호다", "005380"),
            make_block("하이닉스 HBM 증설은 장비주 수주 선행 신호다", "000660"),
        )
    )
    admit_archived_report(store, meta=meta, report_text=report)

    def snapshot():
        hypotheses = store.read_open_hypotheses()
        sightings = {
            row.hypothesis_id: len(store.read_hypothesis_sightings(row.hypothesis_id))
            for row in hypotheses
        }
        return (
            tuple(sorted(sightings.items())),
            len(store.read_hypothesis_relations()),
            len(store.read_merge_reviews()),
            len(store.read_candidate_failures()),
        )

    before = snapshot()
    replay = admit_archived_report(store, meta=meta, report_text=report)
    assert replay["admission"]["created"] == 0
    assert replay["admission"]["revisions"] == 0
    assert replay["admission"]["semantic_sightings"] == 0
    assert replay["admission"]["duplicates"] == 3
    assert all(entry["status"] == "DUPLICATE" for entry in replay["candidates"])
    assert replay["read_back"]["source_revision_registered"] is True
    assert snapshot() == before


# --- malformed sibling isolation / quarantine --------------------------------


def test_malformed_sibling_is_quarantined_and_valid_sibling_admitted(store):
    meta = make_meta()
    report = (
        MALFORMED_BLOCK
        + "\n"
        + make_block(
            "정상 형제 블록 가설",
            "005380",
        )
    )
    result = admit_archived_report(store, meta=meta, report_text=report)
    assert result["parsed"] == {"valid_candidates": 1, "malformed_blocks": 1}
    assert result["admission"]["created"] == 1
    assert result["quarantined"] == [
        {"status": "FAILURE", "error_kind": "unarticulated_statement"},
    ]
    rows = store.read_candidate_failures(source_ref="YlVhQO-Fkc8")
    assert len(rows) == 1
    assert result["read_back"]["failure_records_for_revision"] == 1
    # replay: quarantine stays reported and is not duplicated durably
    replay = admit_archived_report(store, meta=meta, report_text=report)
    assert replay["parsed"]["malformed_blocks"] == 1
    assert len(store.read_candidate_failures(source_ref="YlVhQO-Fkc8")) == 1


# --- zero candidate ----------------------------------------------------------


def test_zero_candidate_report_registers_nothing(store):
    result = admit_archived_report(
        store,
        meta=make_meta(),
        report_text="❓ 오늘 물어볼 질문 3개\n... 추적 후보 없음 ...",
    )
    assert result["parsed"] == {"valid_candidates": 0, "malformed_blocks": 0}
    assert result["candidates"] == []
    assert result["quarantined"] == []
    assert all(value == 0 for value in result["admission"].values())
    assert result["read_back"]["sightings_for_revision"] == 0
    assert result["read_back"]["source_revision_registered"] is False
    assert store.read_open_hypotheses() == ()


# --- bad provenance fails closed before any store interaction ----------------


class _ExplodingStore:
    """Any attribute access is a store interaction — always a test failure."""

    def __getattr__(self, name):
        raise AssertionError(f"store touched before validation: {name}")


def test_malformed_meta_fails_closed_before_store_access():
    broken = make_meta()
    del broken["transcript_sha256"]
    with pytest.raises(TrackingCandidateParseError):
        admit_archived_report(
            _ExplodingStore(),
            meta=broken,
            report_text=MALFORMED_BLOCK,
        )


def test_naive_fetched_at_fails_closed_before_store_access():
    with pytest.raises(TrackingCandidateParseError):
        admit_archived_report(
            _ExplodingStore(),
            meta=make_meta(fetched_at="2026-08-13T05:15:02"),
            report_text=make_block("정상 가설", "005380"),
        )


def test_non_mapping_meta_fails_closed_before_store_access():
    with pytest.raises(TrackingCandidateParseError):
        admit_archived_report(
            _ExplodingStore(),
            meta="not-a-mapping",
            report_text=make_block("정상 가설", "005380"),
        )


# --- JSON privacy and boundedness --------------------------------------------


def test_readback_json_is_bounded_and_never_leaks_private_identifiers(store):
    long_tail = "가" * 100
    blocks = [
        make_block(f"후보 {index:02d} {long_tail}", f"10{index:04d}")
        for index in range(20)
    ]
    result = admit_archived_report(
        store,
        meta=make_meta(),
        report_text="\n".join(blocks),
    )
    assert result["parsed"]["valid_candidates"] == 20
    assert len(result["candidates"]) == 16
    assert result["candidates_omitted"] == 4
    for entry in result["candidates"]:
        assert len(entry["statement_excerpt"]) <= 80
        assert set(entry) == {"status", "statement_excerpt"}
    dumped = json.dumps(result, ensure_ascii=False)
    for forbidden in (
        "/Users",
        "tokmo",
        "~/",
        ".local",
        "hypothesis:",
        "hypothesis_id",
        "sqlite",
    ):
        assert forbidden not in dumped
    # the JSON is renderer-ready: only closed status tokens appear
    assert {entry["status"] for entry in result["candidates"]} <= {
        "NEW",
        "NEW_SIGHTING_OR_REVISION",
        "DUPLICATE",
        "FAILURE",
    }
