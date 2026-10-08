"""Frozen-input end-to-end dry-run over the archived 2026-08-13 report fixture.

Network-zero by the autouse conftest sandbox. Reads only repo fixtures —
never the production runtime archive under ~/.local/share — and writes only
to a tmp_path store.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.programs.youtube_tracking import (
    admit_report_candidates,
    parse_tracking_candidates,
    provenance_from_archive_meta,
)
from tokmo.programs.material_recall import (
    recall_for_deltas,
    record_would_have_sent_shadow,
)
from tokmo.values.analysis_admission import MaterialDeltaV1
from tokmo.values.analysis_contracts import (
    ChangeBasis,
    EvidenceRefV1,
    FreshnessState,
    MaterialKindV1,
    TrustClass,
)

FIXTURES = Path(__file__).parent / "fixtures" / "youtube"
NOW = datetime(2026, 8, 13, 5, 30, tzinfo=timezone.utc)

SYNTHETIC_REPORT = """❓ 오늘 물어볼 질문 3개
...
=== TRACKING_CANDIDATE BEGIN ===
STATEMENT: 싱가포르 테마색 국부펀드의 삼성전자·SK하이닉스 편입은 국내 반도체 장기 성장 가설의 지속 신호다
TRIGGER: EVENT_OCCUR | entity:005980:sovereign_fund_stake_change OR entity:000660:sovereign_fund_stake_change
SUPPORT: yt:2026-08-13:00:14;yt:2026-08-13:00:21
COUNTER: metric:005980:net_foreign_outflow_10d
INVALIDATE: disclosure:005980:fund_exit
NEXT_CHECK: 2026-09-13T00:00:00Z
TOUCH: PERSONAL
KEYS: entity:005980, entity:000660, metric:semis:sovereign_flow
=== TRACKING_CANDIDATE END ===
📜 주장 원장
..."""


def test_archived_fixture_dry_run_admission_recall_shadow(tmp_path):
    meta = json.loads(
        (FIXTURES / "2026-08-13_excerpt.meta.json").read_text(),
    )
    excerpt = (FIXTURES / "2026-08-13_excerpt.txt").read_text()
    assert excerpt.startswith("[00:14]")
    assert meta["video_id"] == "YlVhQO-Fkc8"

    provenance = provenance_from_archive_meta(meta)
    candidates = parse_tracking_candidates(
        SYNTHETIC_REPORT, source_ref=provenance.source_ref,
        revision=provenance.revision, observed_at=NOW,
    )
    assert len(candidates) == 1

    store = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    outcome = admit_report_candidates(store, candidates, now=NOW)
    assert (outcome.created, outcome.duplicates, outcome.failures) == (1, 0, 0)

    # Replay the same archived report: durable side effects must be zero.
    replay = admit_report_candidates(store, candidates, now=NOW)
    assert (replay.created, replay.duplicates) == (0, 1)
    hypothesis_id = store.read_open_hypotheses()[0].hypothesis_id
    assert len(store.read_hypothesis_sightings(hypothesis_id)) == 1

    # New material arrives for the same entity → reverse-index recall.
    evidence = EvidenceRefV1(
        evidence_id="ev-quote-1",
        kind=MaterialKindV1.QUOTE_STATE,
        source="toss-quotes",
        source_record_key="005980",
        observed_at=NOW,
        fetched_at=NOW,
        stored_at=NOW,
        acquired_at=NOW,
        content_hash="d" * 64,
        schema_version=1,
        parser_version="p1",
        trust_class=TrustClass.OFFICIAL_PROVIDER,
        freshness_state=FreshnessState.FRESH,
        expires_at=NOW + timedelta(days=1),
    )
    material = MaterialDeltaV1(
        subject_id="subject-1",
        material_kind=MaterialKindV1.QUOTE_STATE,
        market="KR",
        symbol="005980",
        tick_id=4242,
        material_binding_digest="b" * 64,
        evidence_refs=(evidence,),
        current_fields={"last_price_text": "80000"},
        baseline_fields={"last_price_text": "76000"},
        change_basis=ChangeBasis.BASELINE_DELTA,
        freshness_state=FreshnessState.FRESH,
        created_at=NOW,
    )
    recalls = recall_for_deltas(store, (material,), now=NOW)
    assert [recall.hypothesis_id for recall in recalls] == [hypothesis_id]

    shadow = record_would_have_sent_shadow(
        store, recalls, (material,), now=NOW,
    )
    assert shadow.new_records == 1
    replay_shadow = record_would_have_sent_shadow(
        store, recalls, (material,), now=NOW + timedelta(hours=1),
    )
    assert replay_shadow.new_records == 0
    assert replay_shadow.duplicates == 1

    rows = store.read_shadow_recall(hypothesis_id)
    assert len(rows) == 1
    assert rows[0].would_have_sent is False
    store.close()
