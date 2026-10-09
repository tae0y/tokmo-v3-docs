"""NEWS producer wiring RED suite (SDS 2026-08-21 §12.2–§12.7).

Covers due/드레인/discovery, candidate lifecycle/crash windows (>6h included),
EXPIRED invariants, morning-hold replay, model/에피소드/outbox, privacy
sentinels, producer isolation, flock overlap (R-LCK-1), stale-comment, and
two-lane fairness/starvation (R-FAIR-1..4).
"""

import dataclasses
import fcntl
import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tokmo.programs.news_alert_delivery import run_alert_delivery
from tokmo.programs.news_event_admission import (
    reconcile_news_outbox,
    run_news_event_admission,
)
from tokmo.programs.news_production import (
    NewsProducerDepsV1,
    production_stage_report,
    render_news_alert_body,
    run_news_production,
)
from tokmo.records.external_news_query import read_discovery_documents_v1
from tokmo.records.news_episode_store import (
    NewsEpisodeStore,
    NewsEpisodeStoreError,
)
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.news_classifier import (
    DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
    NewsClassifierFailure,
)
from tokmo.values.news_config import (
    DISCOVERY_MAX_DOCUMENTS,
    PRODUCER_CANDIDATE_ATTEMPT_MAX,
    PRODUCER_CANDIDATE_TTL_SECONDS,
    PRODUCER_CANDIDATES_PER_TICK,
    PRODUCER_EXPIRY_PER_TICK,
    PRODUCER_INTERVAL_SECONDS,
    PRODUCER_LEASE_SECONDS,
    PRODUCER_RECOVERY_BACKOFF_CAP_SECONDS,
)
from tokmo.values.ops_kernel import ProducerRole, ReplayResult

UTC = UTC
KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)  # slot boundary; 18:00 KST
CUTOFF = datetime(2026, 8, 1, tzinfo=UTC)
SEEN = (NOW - timedelta(hours=1)).isoformat()


def _n5(path, docs):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE news_documents(news_document_id TEXT, title TEXT,"
        " title_fingerprint TEXT, content_hash TEXT, source_id TEXT,"
        " first_seen_at TEXT, revision_no INTEGER, summary_text TEXT,"
        " published_at TEXT, evidence_role TEXT, publisher_name TEXT)"
    )
    # Doc tuples: (doc_id, title, source, seen[, summary[, role[, publisher]]])
    for doc_id, title, source, seen, *rest in docs:
        summary = rest[0] if len(rest) > 0 else None
        role = rest[1] if len(rest) > 1 else "FACT_DISCOVERY"
        publisher = rest[2] if len(rest) > 2 else None
        connection.execute(
            "INSERT INTO news_documents VALUES(?,?,?,NULL,?,?,1,?,NULL,?,?)",
            (doc_id, title, f"fp-{doc_id}", source, seen, summary, role, publisher),
        )
    connection.commit()
    connection.close()


_ORIGIN_PUBLISHERS = ("연합뉴스", "JTBC", "BBC")


def _cluster(term, count=3, seen=SEEN):
    # Publishers cycle the three known origins so the D-5 origin-count
    # thresholds mirror the old per-source diversity.
    return [
        (
            f"{term}-{i}",
            f"{term} 급증",
            f"src-{term}-{i}",
            seen,
            None,
            "FACT_DISCOVERY",
            _ORIGIN_PUBLISHERS[i % 3],
        )
        for i in range(count)
    ]


# Corpus terms must stay hangul-only: digits split into separate tokens and
# several hangul particles are suffix-stripped by the tokenizer.
_HANGUL_DIGITS = {
    "0": "영",
    "1": "일",
    "2": "둘",
    "3": "삼",
    "4": "넷",
    "5": "오",
    "6": "육",
    "7": "칠",
    "8": "팔",
    "9": "구",
}


def _hterm(prefix, number):
    return prefix + "".join(_HANGUL_DIGITS[ch] for ch in str(number))


def _ok_classifier(calls=None, event_class="ORDINARY"):
    def call(request):
        if calls is not None:
            calls.append(request)
        return json.dumps(
            {
                "provider": request.provider,
                "model_id": request.model_id,
                "citations": [],
                "event_class": event_class,
            }
        )

    return call


def _unbound(request):
    raise NewsClassifierFailure("MODEL_UNAVAILABLE")


def _bundle(
    tmp_path,
    *,
    classifier,
    corpus=(),
    cutoff=CUTOFF,
    terms=frozenset(),
    max_rows=None,
    n5=None,
):
    store = NewsEpisodeStore(tmp_path / "stores" / "news_episodes.sqlite3")
    ops = OpsKernelStore(
        tmp_path / "ops" / "ops_kernel.sqlite3", stable_phase_ids=frozenset()
    )
    n5_path = n5 if n5 is not None else tmp_path / "n5.sqlite3"
    if corpus is not None and not n5_path.exists():
        _n5(n5_path, corpus)
    reads = []

    def reader(window_from):
        reads.append(window_from)
        return read_discovery_documents_v1(
            n5_path,
            window_from=window_from,
            max_rows=max_rows or DISCOVERY_MAX_DOCUMENTS,
        )

    deps = NewsProducerDepsV1(
        episode_store=store,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        documents_reader=reader,
        holding_terms=lambda: frozenset(terms),
        classifier_call=classifier,
        classifier_config_path=tmp_path / "news_classifier_model.json",
        admitted_catalog={DEFAULT_NEWS_CLASSIFIER_MODEL_ID: "openai-codex"},
        gate_cutoff=cutoff,
        slot_seconds=PRODUCER_INTERVAL_SECONDS,
        candidates_per_tick=PRODUCER_CANDIDATES_PER_TICK,
        lease_seconds=PRODUCER_LEASE_SECONDS,
        backoff_cap_seconds=PRODUCER_RECOVERY_BACKOFF_CAP_SECONDS,
        ttl_seconds=PRODUCER_CANDIDATE_TTL_SECONDS,
        attempt_max=PRODUCER_CANDIDATE_ATTEMPT_MAX,
    )
    return deps, reads


def _tick(deps, now):
    return run_news_production(producer_deps=deps, now=now)


def test_news_alert_body_is_human_readable_and_never_internal_json():
    # S3 갱신 (SDS 2026-09-01): the body now carries the confirmation label
    # line; the WEAK/CORE combination stays a pure-render exercise.
    body = render_news_alert_body(
        term="ent:nvidia",
        event_class="CORE_BREAKING",
        materiality="WEAK",
        lane="WHOLE_CORPUS_FREQUENCY",
        stories=11,
        sources=2,
        headline="엔비디아 신형 GPU 공개",
        confirmation_state="MULTI_MEDIA_UNCONFIRMED",
    )
    assert body.startswith("📰 NVIDIA 뉴스 동향")
    assert "사건: 엔비디아 신형 GPU 공개" in body
    assert "기사 11건 · 출처 2곳" in body
    assert "확인: 복수 언론 보도, 공식 확인 전" in body
    assert "판정:" not in body and "대상:" not in body
    assert "긴급 알림 아님" not in body
    assert not body.lstrip().startswith("{")
    assert '"candidate"' not in body


def test_news_alert_body_fails_closed_without_meaningful_statement():
    base = dict(
        term="ent:nvidia",
        event_class="ORDINARY",
        materiality="WEAK",
        lane="WHOLE_CORPUS_FREQUENCY",
        stories=7,
        sources=2,
    )
    assert render_news_alert_body(**base, headline=None) is None
    assert render_news_alert_body(**base, headline="  ") is None
    # A statement made only of generic function words is not an event.
    assert (
        render_news_alert_body(**base, headline="stocks could rise more than this")
        is None
    )


def _mark_slot(store, now):
    slot = (
        int(now.timestamp()) // PRODUCER_INTERVAL_SECONDS
    ) * PRODUCER_INTERVAL_SECONDS
    window_to = datetime.fromtimestamp(slot, UTC)
    if store.discovery_run_exists_for_window(window_to.isoformat()):
        return
    store.connection.execute(
        "INSERT INTO discovery_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"ndr-slotmark-{slot}",
            window_to.isoformat(),
            window_to.isoformat(),
            "0" * 64,
            "0" * 64,
            0,
            0,
            0,
            "OK",
            now.isoformat(),
            now.isoformat(),
        ),
    )
    store.connection.commit()


def _seed(
    store,
    terms,
    *,
    now,
    created=None,
    updated=None,
    observed=None,
    state="SURFACED",
    headline="STORED",
):
    created = created or now
    updated = updated or created
    run_id = (
        "ndr-"
        + hashlib.sha256(
            f"seed\0{now.isoformat()}\0{','.join(terms)}".encode()
        ).hexdigest()[:24]
    )
    store.connection.execute(
        "INSERT INTO discovery_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            now.isoformat(),
            now.isoformat(),
            "1" * 64,
            "2" * 64,
            3,
            3,
            0,
            "OK",
            now.isoformat(),
            now.isoformat(),
        ),
    )
    ids = []
    for term in terms:
        cid = "ndc-" + hashlib.sha256(f"{run_id}\0{term}".encode()).hexdigest()[:24]
        payload = {
            "v": 1,
            "document_ids": [f"{term}-0", f"{term}-1", f"{term}-2"],
            "latest_observed_at": (observed or now).isoformat(),
        }
        if headline is not None:
            payload["headline"] = (
                f"{term} 수주 확대 공시" if headline == "STORED" else headline
            )
        evidence = json.dumps(payload, separators=(",", ":"))
        store.connection.execute(
            "INSERT INTO discovery_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                cid,
                run_id,
                "WHOLE_CORPUS_FREQUENCY",
                None,
                term,
                3,
                3,
                evidence,
                state,
                None,
                created.isoformat(),
                updated.isoformat(),
            ),
        )
        ids.append(cid)
    store.connection.commit()
    return ids


def _states(store):
    counts = {}
    for state, n in store.connection.execute(
        "SELECT state,count(*) FROM discovery_candidates GROUP BY state"
    ):
        counts[state] = n
    return counts


def _candidate_state(store, cid):
    row = store.connection.execute(
        "SELECT state FROM discovery_candidates WHERE candidate_id=?", (cid,)
    ).fetchone()
    return None if row is None else row[0]


def _call_count(store):
    return store.connection.execute("SELECT count(*) FROM classifier_calls").fetchone()[
        0
    ]


def _expired_invariant_violations(store):
    return store.connection.execute("""
        SELECT count(*) FROM discovery_candidates d
         WHERE d.state='EXPIRED'
           AND (d.episode_id IS NOT NULL
                OR EXISTS(SELECT 1 FROM classifier_calls c
                           WHERE c.candidate_id=d.candidate_id
                             AND c.state='SETTLED' AND c.exit='OK'))
        """).fetchone()[0]


def _raise_before(monkeypatch, store, name, *, once=True, when=None):
    original = getattr(store, name)
    state = {"armed": True}

    def wrapper(*args, **kwargs):
        if state["armed"] and (when is None or when(*args, **kwargs)):
            if once:
                state["armed"] = False
            raise RuntimeError(f"injected before {name}")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, name, wrapper)
    return state


# ---------------------------------------------------------------- §12.2 due


def test_r_due_1_same_slot_single_discovery_and_not_due(tmp_path):
    deps, reads = _bundle(tmp_path, classifier=_ok_classifier(), corpus=[])
    results = [_tick(deps, NOW + timedelta(seconds=60 * i)) for i in range(3)]
    store = deps.episode_store
    assert store.connection.execute(
        "SELECT count(*) FROM discovery_runs"
    ).fetchone() == (1,)
    assert len(reads) == 1
    assert results[0].due is True
    for late in results[1:]:
        assert late.due is False
        assert production_stage_report(late) == ("skipped", "not_due")


def test_r_due_2_unchanged_corpus_two_slots_two_runs(tmp_path):
    deps, reads = _bundle(
        tmp_path, classifier=_ok_classifier(), corpus=_cluster("듀얼슬롯테스트")
    )
    _tick(deps, NOW)
    second = _tick(deps, NOW + timedelta(seconds=PRODUCER_INTERVAL_SECONDS))
    assert second.due is True
    rows = deps.episode_store.connection.execute(
        "SELECT run_id FROM discovery_runs ORDER BY window_to"
    ).fetchall()
    assert len(rows) == 2 and rows[0] != rows[1]


def test_r_due_3_restart_does_not_rescan_recorded_slot(tmp_path):
    deps, reads = _bundle(tmp_path, classifier=_ok_classifier(), corpus=[])
    _tick(deps, NOW)
    assert len(reads) == 1
    reopened, reads2 = _bundle(tmp_path, classifier=_ok_classifier())
    result = _tick(reopened, NOW + timedelta(seconds=120))
    assert result.due is False and len(reads2) == 0


def test_r_due_4_open_candidates_drain_without_discovery(tmp_path):
    deps, reads = _bundle(tmp_path, classifier=_ok_classifier())
    store = deps.episode_store
    _seed(store, ("드레인일", "드레인이", "드레인삼"), now=NOW - timedelta(seconds=60))
    _mark_slot(store, NOW)
    result = _tick(deps, NOW)
    status, reason = production_stage_report(result)
    assert reason != "not_due"
    assert result.drained == 2
    assert len(reads) == 0


# ---------------------------------------------------------------- §12.2 cap


def test_r_cap_1_row_cap_exceeded_is_typed_row(tmp_path):
    corpus = _cluster("캡초과", count=6)
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier(), corpus=corpus, max_rows=5)
    result = _tick(deps, NOW)
    store = deps.episode_store
    assert result.discovery_status == "CAP_EXCEEDED"
    assert store.connection.execute("SELECT status FROM discovery_runs").fetchall() == [
        ("CAP_EXCEEDED",)
    ]
    assert store.connection.execute(
        "SELECT count(*) FROM discovery_candidates"
    ).fetchone() == (0,)


def test_r_cap_2_reader_failures_are_typed_rows_not_fake_success(tmp_path):
    cases = []
    # absent
    root = tmp_path / "absent"
    deps, _ = _bundle(root, classifier=_ok_classifier(), corpus=None)
    cases.append((deps, "STORE_UNAVAILABLE", None))
    # locked
    root = tmp_path / "locked"
    deps_locked, _ = _bundle(root, classifier=_ok_classifier(), corpus=_cluster("잠김"))
    locker = sqlite3.connect(root / "n5.sqlite3")
    locker.execute("BEGIN EXCLUSIVE")
    cases.append((deps_locked, "BUSY_OR_LOCKED", locker))
    # missing table
    root = tmp_path / "notable"
    root.mkdir()
    empty = sqlite3.connect(root / "n5.sqlite3")
    empty.execute("CREATE TABLE unrelated(x)")
    empty.commit()
    empty.close()
    deps_missing, _ = _bundle(root, classifier=_ok_classifier(), corpus=None)
    cases.append((deps_missing, "SCHEMA_MISMATCH", None))
    # corrupt header
    root = tmp_path / "corrupt"
    root.mkdir()
    (root / "n5.sqlite3").write_bytes(b"not-a-database-header" * 4)
    deps_corrupt, _ = _bundle(root, classifier=_ok_classifier(), corpus=None)
    cases.append((deps_corrupt, "CORRUPT", None))
    try:
        for deps, expected, _holder in cases:
            result = _tick(deps, NOW)
            assert result.discovery_status == expected
            assert deps.episode_store.connection.execute(
                "SELECT status FROM discovery_runs"
            ).fetchall() == [(expected,)]
    finally:
        locker.close()


def test_r_cap_3_dedicated_cap_corpus_proves_nonzero_drop(tmp_path):
    corpus = []
    for token_no in range(20):
        corpus.extend(_cluster(_hterm("캡픽스처", token_no)))
    deps, _ = _bundle(
        tmp_path,
        classifier=_ok_classifier(),
        corpus=corpus,
        terms=frozenset(_hterm("캡픽스처", token_no) for token_no in range(20)),
    )
    _tick(deps, NOW)
    store = deps.episode_store
    dropped, candidates = store.connection.execute(
        "SELECT dropped_over_cap,"
        " (SELECT count(*) FROM discovery_candidates)"
        " FROM discovery_runs"
    ).fetchone()
    assert dropped == 8 and candidates == 12


def test_r_cap_4_healthy_corpus_zero_drop_is_execution_evidence(tmp_path):
    deps, _ = _bundle(
        tmp_path, classifier=_ok_classifier(), corpus=_cluster("건강코퍼스")
    )
    _tick(deps, NOW)
    row = deps.episode_store.connection.execute(
        "SELECT status,scanned_documents,dropped_over_cap FROM discovery_runs"
    ).fetchone()
    assert row[0] == "OK" and row[1] > 0 and row[2] == 0


# ------------------------------------------------------- §12.3 drain / life


def test_r_drn_1_twelve_candidates_drain_in_six_ticks(tmp_path):
    corpus = []
    for token_no in range(12):
        corpus.extend(_cluster(_hterm("드레인론", token_no)))
    calls = []
    deps, _ = _bundle(
        tmp_path,
        classifier=_ok_classifier(calls),
        corpus=corpus,
        terms=frozenset(_hterm("드레인론", token_no) for token_no in range(12)),
    )
    store = deps.episode_store
    first = _tick(deps, NOW)
    assert first.due is True and first.drained == 0
    for i in range(1, 7):
        result = _tick(deps, NOW + timedelta(seconds=60 * i))
        assert result.claims_attempted == 2
        assert result.claims_won == 2
        assert result.claims_won_fresh == 2
        assert result.claims_won_recovery == 0
        assert result.borrowed_slots == 1
        assert result.drained == 2
    states = _states(store)
    assert states.get("SURFACED", 0) == 0
    assert states.get("MERGED_INTO_EPISODE", 0) + states.get("DROPPED_NOISE", 0) == 12
    settled = store.connection.execute(
        "SELECT candidate_id,count(*) FROM classifier_calls"
        " WHERE state='SETTLED' AND exit='OK' GROUP BY candidate_id"
    ).fetchall()
    assert len(settled) == 12 and all(n == 1 for _cid, n in settled)
    assert store.connection.execute(
        "SELECT count(*) FROM discovery_runs"
    ).fetchone() == (1,)


def test_r_drn_2_terminal_candidates_are_never_reselected(tmp_path):
    calls = []
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier(calls))
    store = deps.episode_store
    _seed(store, ("재선택금지",), now=NOW - timedelta(seconds=60))
    _mark_slot(store, NOW)
    _tick(deps, NOW)
    assert len(calls) == 1
    after = _tick(deps, NOW + timedelta(seconds=60))
    assert after.claims_attempted == 0 and len(calls) == 1


def test_r_lif_1_second_claim_cas_loses_silently(tmp_path):
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier())
    store = deps.episode_store
    (cid,) = _seed(store, ("이중클레임",), now=NOW - timedelta(seconds=120))
    expected = store.connection.execute(
        "SELECT updated_at FROM discovery_candidates WHERE candidate_id=?", (cid,)
    ).fetchone()[0]
    assert store.claim_candidate(cid, expected_updated_at=expected, now=NOW) is True
    assert store.claim_candidate(cid, expected_updated_at=expected, now=NOW) is False


def test_r_lif_2_lease_window_blocks_reclaim(tmp_path):
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier())
    store = deps.episode_store
    (cid,) = _seed(store, ("리스보호",), now=NOW, state="ADMITTED_FOR_INVESTIGATION")
    _mark_slot(store, NOW + timedelta(seconds=60))
    result = _tick(deps, NOW + timedelta(seconds=60))
    assert result.claims_attempted == 0
    assert _candidate_state(store, cid) == "ADMITTED_FOR_INVESTIGATION"


def test_r_lif_3_backoff_delay_gates_recovery_reclaim(tmp_path):
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier())
    store = deps.episode_store
    (cid,) = _seed(
        store,
        ("백오프복구",),
        now=NOW,
        created=NOW - timedelta(seconds=120),
        updated=NOW,
        state="ADMITTED_FOR_INVESTIGATION",
    )
    early = NOW + timedelta(seconds=240)
    _mark_slot(store, early)
    not_ready = _tick(deps, early)
    assert not_ready.claims_attempted == 0
    ready = NOW + timedelta(seconds=301)
    _mark_slot(store, ready)
    result = _tick(deps, ready)
    assert result.claims_won_recovery == 1


def test_r_lif_4_attempt_cap_expires_without_model_call(tmp_path):
    calls = []

    def deferred(request):
        calls.append(request)
        raise NewsClassifierFailure("MODEL_UNAVAILABLE")

    deps, _ = _bundle(tmp_path, classifier=deferred)
    store = deps.episode_store
    (cid,) = _seed(store, ("시도소진",), now=NOW - timedelta(seconds=60))
    for i in range(3):
        now = NOW + timedelta(seconds=60 * i)
        _mark_slot(store, now)
        _tick(deps, now)
    assert len(calls) == 3 and _candidate_state(store, cid) == "SURFACED"
    final = NOW + timedelta(seconds=240)
    _mark_slot(store, final)
    _tick(deps, final)
    assert len(calls) == 3
    assert _candidate_state(store, cid) == "EXPIRED"
    assert store.connection.execute(
        "SELECT count(*) FROM classifier_calls WHERE candidate_id=?", (cid,)
    ).fetchone() == (3,)


def test_r_lif_5_daily_cap_releases_without_new_rows(tmp_path):
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier())
    store = deps.episode_store
    for i in range(96):
        store.plan_classifier_call(
            {
                "call_id": f"pre-{i:03d}",
                "classification_identity": "c" * 64,
                "attempt_no": i + 1,
                "purpose": "EVENT_CLASS",
                "candidate_id": "ndc-preexisting",
                "episode_id": None,
                "provider": "openai-codex",
                "model_id": DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
                "model_contract_digest": "d" * 64,
                "prompt_policy_digest": "e" * 64,
                "input_evidence_digest": "f" * 64,
                "output_schema_id": "news-classifier-output-v1",
            },
            now=NOW,
        )
    (cid,) = _seed(store, ("예산소진",), now=NOW - timedelta(seconds=60))
    _mark_slot(store, NOW)
    result = _tick(deps, NOW)
    assert result.released_candidates == 1
    assert _candidate_state(store, cid) == "SURFACED"
    assert _call_count(store) == 96


def test_r_lif_6_expiry_eligible_candidates_are_never_claimed(tmp_path):
    calls = []
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier(calls))
    store = deps.episode_store
    old = NOW - timedelta(seconds=PRODUCER_CANDIDATE_TTL_SECONDS + 3600)
    (surfaced,) = _seed(store, ("만료표류일",), now=old)
    (admitted,) = _seed(
        store,
        ("만료표류이",),
        now=old + timedelta(seconds=1),
        state="ADMITTED_FOR_INVESTIGATION",
    )
    _mark_slot(store, NOW)
    result = _tick(deps, NOW)
    assert result.claims_attempted == 0 and result.claims_won == 0
    assert result.expired_candidates == 2
    assert _candidate_state(store, surfaced) == "EXPIRED"
    assert _candidate_state(store, admitted) == "EXPIRED"
    assert calls == []


def test_r_lif_7_rejected_and_muted_become_dropped_noise(tmp_path):
    calls = []
    deps, _ = _bundle(
        tmp_path, classifier=_ok_classifier(calls), cutoff=NOW - timedelta(minutes=5)
    )
    store = deps.episode_store
    (cid,) = _seed(
        store,
        ("컷오프이전",),
        now=NOW - timedelta(seconds=60),
        observed=NOW - timedelta(hours=2),
    )
    _mark_slot(store, NOW)
    _tick(deps, NOW)
    assert _candidate_state(store, cid) == "DROPPED_NOISE"
    assert store.connection.execute(
        "SELECT count(*) FROM alert_revisions"
    ).fetchone() == (0,)
    # MUTED episode: identical cause reappears → DROPPED_NOISE, zero alerts.
    deps2, _ = _bundle(tmp_path / "muted", classifier=_ok_classifier())
    store2 = deps2.episode_store
    (first,) = _seed(store2, ("뮤트원인",), now=NOW - timedelta(seconds=120))
    _mark_slot(store2, NOW)
    _tick(deps2, NOW)
    assert _candidate_state(store2, first) == "MERGED_INTO_EPISODE"
    episode_id = store2.connection.execute(
        "SELECT episode_id FROM discovery_candidates WHERE candidate_id=?", (first,)
    ).fetchone()[0]
    store2.mute_episode(episode_id, "operator mute", now=NOW)
    (again,) = _seed(store2, ("뮤트원인",), now=NOW + timedelta(seconds=60))
    later = NOW + timedelta(seconds=120)
    _mark_slot(store2, later)
    _tick(deps2, later)
    assert _candidate_state(store2, again) == "DROPPED_NOISE"
    outbox = (
        deps2.ops_kernel._connect()
        .execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_EVENT'")
        .fetchone()[0]
    )
    assert outbox == 1  # only the first, merged candidate alerted


# ------------------------------------------------------------ §12.3 crash


def test_r_crs_1_started_residue_is_reconciled_without_recall(tmp_path):
    calls = []
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier(calls))
    store = deps.episode_store
    store.plan_classifier_call(
        {
            "call_id": "ncl-crashed",
            "classification_identity": "a" * 64,
            "attempt_no": 1,
            "purpose": "EVENT_CLASS",
            "candidate_id": "ndc-crashed",
            "episode_id": None,
            "provider": "openai-codex",
            "model_id": DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
            "model_contract_digest": "d" * 64,
            "prompt_policy_digest": "e" * 64,
            "input_evidence_digest": "f" * 64,
            "output_schema_id": "news-classifier-output-v1",
        },
        now=NOW,
    )
    store.start_classifier_call("ncl-crashed", now=NOW)
    _mark_slot(store, NOW)
    result = _tick(deps, NOW)
    assert result.reconciled_calls == 1
    assert store.connection.execute(
        "SELECT state FROM classifier_calls WHERE call_id='ncl-crashed'"
    ).fetchone() == ("UNKNOWN",)
    assert calls == []


def test_r_crs_2_after_tx_c_crash_converges_via_outbox_stage(tmp_path):
    store = NewsEpisodeStore(tmp_path / "news.sqlite")
    ops = OpsKernelStore(
        tmp_path / "ops" / "kernel.sqlite", stable_phase_ids=frozenset()
    )
    episode = store.upsert("txc-crash", (), NOW)
    draft = {
        "episode_id": episode.episode_id,
        "revision_no": 1,
        "revision_kind": "INITIAL",
        "body_text": "body",
        "portfolio_context_digest": "b" * 64,
        "confidence": 70,
        "material_change_kind": "FIRST_ADMISSION",
        "material_change_observed_at": NOW,
        "links": (),
    }
    with pytest.raises(RuntimeError):
        run_news_event_admission(
            store=store,
            ops_kernel=ops,
            role=ProducerRole.HERMES,
            draft=draft,
            activation_enabled=False,
            now=NOW,
            inject_crash_at="after_tx_c",
        )
    reconcile_news_outbox(
        store=store,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        activation_enabled=False,
        now=NOW,
    )
    assert tuple(
        ops._connect()
        .execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_EVENT'")
        .fetchone()
    ) == (1,)


def _crash_setup(
    tmp_path, monkeypatch, crash_method, *, when=None, term="크래시윈도우"
):
    calls = []
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier(calls))
    store = deps.episode_store
    (cid,) = _seed(store, (term,), now=NOW - timedelta(seconds=60))
    _mark_slot(store, NOW)
    _raise_before(monkeypatch, store, crash_method, when=when)
    crashed = _tick(deps, NOW)
    assert crashed.candidate_failures == 1
    return deps, store, cid, calls


def test_r_crs_3_claim_then_crash_recovers_with_one_call(tmp_path, monkeypatch):
    deps, store, cid, calls = _crash_setup(
        tmp_path, monkeypatch, "plan_classifier_call"
    )
    assert _candidate_state(store, cid) == "ADMITTED_FOR_INVESTIGATION"
    row = store.connection.execute(
        "SELECT episode_id FROM discovery_candidates WHERE candidate_id=?", (cid,)
    ).fetchone()
    assert row == (None,) and _call_count(store) == 0
    resume = NOW + timedelta(seconds=301)
    _mark_slot(store, resume)
    _tick(deps, resume)
    assert _candidate_state(store, cid) == "MERGED_INTO_EPISODE"
    assert len(calls) == 1


def test_r_crs_4_settled_then_crash_replays_without_model_call(tmp_path, monkeypatch):
    deps, store, cid, calls = _crash_setup(tmp_path, monkeypatch, "upsert_with_cause")
    assert len(calls) == 1
    resume = NOW + timedelta(seconds=301)
    _mark_slot(store, resume)
    _tick(deps, resume)
    assert len(calls) == 1
    assert store.connection.execute("SELECT count(*) FROM episodes").fetchone() == (1,)
    assert _candidate_state(store, cid) == "MERGED_INTO_EPISODE"


def test_settled_generic_candidate_replays_before_new_prefilter(tmp_path, monkeypatch):
    from tokmo.programs import news_production

    real_gate = news_production.candidate_requires_classifier
    monkeypatch.setattr(
        news_production, "candidate_requires_classifier", lambda *args, **kwargs: True
    )
    deps, store, cid, calls = _crash_setup(
        tmp_path, monkeypatch, "upsert_with_cause", term="this"
    )
    monkeypatch.setattr(news_production, "candidate_requires_classifier", real_gate)
    assert len(calls) == 1
    resume = NOW + timedelta(seconds=301)
    _mark_slot(store, resume)
    _tick(deps, resume)
    assert len(calls) == 1
    assert _candidate_state(store, cid) == "MERGED_INTO_EPISODE"


def test_r_crs_5_upsert_then_crash_bounds_revisions(tmp_path, monkeypatch):
    deps, store, cid, calls = _crash_setup(
        tmp_path, monkeypatch, "attach_candidate_episode"
    )
    resume = NOW + timedelta(seconds=301)
    _mark_slot(store, resume)
    _tick(deps, resume)
    assert store.connection.execute("SELECT count(*) FROM episodes").fetchone() == (1,)
    assert (
        store.connection.execute("SELECT count(*) FROM episode_revisions").fetchone()[0]
        <= 2
    )
    assert store.connection.execute(
        "SELECT count(*) FROM episode_evidence"
    ).fetchone() == (3,)
    assert len(calls) == 1


def test_r_crs_6_pre_admission_crash_forbids_premature_merge(tmp_path, monkeypatch):
    deps, store, cid, calls = _crash_setup(
        tmp_path, monkeypatch, "admit_alert_revision"
    )
    assert _candidate_state(store, cid) != "MERGED_INTO_EPISODE"
    resume = NOW + timedelta(seconds=301)
    _mark_slot(store, resume)
    _tick(deps, resume)
    assert _candidate_state(store, cid) == "MERGED_INTO_EPISODE"
    assert store.connection.execute(
        "SELECT count(*) FROM alert_revisions"
    ).fetchone() == (1,)


def _long_downtime_resume(deps, store):
    resume = NOW + timedelta(hours=7)
    _mark_slot(store, resume)
    return resume, _tick(deps, resume)


def test_r_crs_7_wp5_seven_hour_restart_is_not_expired(tmp_path, monkeypatch):
    deps, store, cid, calls = _crash_setup(tmp_path, monkeypatch, "upsert_with_cause")
    resume, result = _long_downtime_resume(deps, store)
    assert _candidate_state(store, cid) == "MERGED_INTO_EPISODE"
    assert result.expired_candidates == 0 and len(calls) == 1


def test_r_crs_8_wp6_seven_hour_restart_converges_same_episode(tmp_path, monkeypatch):
    deps, store, cid, calls = _crash_setup(
        tmp_path, monkeypatch, "attach_candidate_episode"
    )
    resume, result = _long_downtime_resume(deps, store)
    assert _candidate_state(store, cid) == "MERGED_INTO_EPISODE"
    assert store.connection.execute("SELECT count(*) FROM episodes").fetchone() == (1,)
    assert (
        store.connection.execute("SELECT count(*) FROM episode_revisions").fetchone()[0]
        <= 2
    )
    assert len(calls) == 1


def test_r_crs_9_wp7_seven_hour_restart_goes_direct_to_admission(tmp_path, monkeypatch):
    deps, store, cid, calls = _crash_setup(
        tmp_path, monkeypatch, "admit_alert_revision"
    )
    assert (
        store.connection.execute(
            "SELECT episode_id FROM discovery_candidates WHERE candidate_id=?", (cid,)
        ).fetchone()[0]
        is not None
    )
    resume, result = _long_downtime_resume(deps, store)
    assert _candidate_state(store, cid) == "MERGED_INTO_EPISODE"
    assert store.connection.execute(
        "SELECT count(*) FROM alert_revisions"
    ).fetchone() == (1,)
    assert len(calls) == 1


def test_r_crs_10_wp8_seven_hour_restart_replays_admission(tmp_path, monkeypatch):
    calls = []
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier(calls))
    store = deps.episode_store
    (cid,) = _seed(store, ("티엑스씨보존",), now=NOW - timedelta(seconds=60))
    _mark_slot(store, NOW)
    original = deps.ops_kernel.record_news_alert_outbox
    state = {"armed": True}

    def crash_outbox(*args, **kwargs):
        if state["armed"]:
            state["armed"] = False
            raise RuntimeError("injected before outbox")
        return original(*args, **kwargs)

    monkeypatch.setattr(deps.ops_kernel, "record_news_alert_outbox", crash_outbox)
    crashed = _tick(deps, NOW)
    assert crashed.candidate_failures == 1
    assert store.connection.execute(
        "SELECT delivery_state FROM alert_revisions"
    ).fetchall() == [("PENDING_OUTBOX",)]
    resume, result = _long_downtime_resume(deps, store)
    assert _candidate_state(store, cid) == "MERGED_INTO_EPISODE"
    assert store.connection.execute(
        "SELECT count(*) FROM alert_revisions"
    ).fetchone() == (1,)
    assert tuple(
        deps.ops_kernel._connect()
        .execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_EVENT'")
        .fetchone()
    ) == (1,)
    assert len(calls) == 1


def test_r_crs_11_wp9_seven_hour_restart_replays_morning_hold(tmp_path, monkeypatch):
    hold_now = datetime(2026, 8, 20, 17, 0, tzinfo=UTC)  # 02:00 KST
    calls = []
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier(calls))
    store = deps.episode_store
    (cid,) = _seed(store, ("아침보류복원",), now=hold_now - timedelta(seconds=60))
    _mark_slot(store, hold_now)
    state = {"n": 0}
    original = store.finalize_candidate

    def crash_finalize(*args, **kwargs):
        if state["n"] == 0 and args and args[1] == "MERGED_INTO_EPISODE":
            state["n"] = 1
            raise RuntimeError("injected after hold")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "finalize_candidate", crash_finalize)
    crashed = _tick(deps, hold_now)
    assert crashed.candidate_failures == 1
    assert store.connection.execute(
        "SELECT count(*) FROM morning_hold_queue"
    ).fetchone() == (1,)
    resume = hold_now + timedelta(hours=7)  # 09:00 KST: hold already released
    _mark_slot(store, resume)
    result = _tick(deps, resume)
    assert result.released_holds == 1
    later = resume + timedelta(seconds=60)
    _mark_slot(store, later)
    _tick(deps, later)
    assert _candidate_state(store, cid) == "MERGED_INTO_EPISODE"
    assert store.connection.execute(
        "SELECT count(*) FROM alert_revisions WHERE revision_kind='MORNING_COALESCED'"
    ).fetchone() == (1,)
    assert len(calls) == 1


def test_r_crs_12_uncheckpointed_over_ttl_expires_without_claims(tmp_path, monkeypatch):
    calls = []
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier(calls))
    store = deps.episode_store
    start = NOW - timedelta(hours=7)
    (wp3,) = _seed(store, ("더블유피삼",), now=start - timedelta(seconds=60))
    _mark_slot(store, start)
    # W-P3: crash right after claim
    _raise_before(monkeypatch, store, "plan_classifier_call")
    _tick(deps, start)
    assert _candidate_state(store, wp3) == "ADMITTED_FOR_INVESTIGATION"
    # W-P4: crash between start and settle for a second candidate; wp3 is
    # inside its lease window so only wp4 is claimable on this tick.
    (wp4,) = _seed(store, ("더블유피사",), now=start + timedelta(seconds=30))
    _raise_before(monkeypatch, store, "settle_classifier_call")
    _tick(deps, start + timedelta(seconds=60))
    assert _candidate_state(store, wp4) == "ADMITTED_FOR_INVESTIGATION"
    started = store.connection.execute(
        "SELECT count(*) FROM classifier_calls WHERE state='STARTED'"
    ).fetchone()[0]
    assert started == 1
    calls.clear()
    resume = NOW + timedelta(seconds=1)
    _mark_slot(store, resume)
    result = _tick(deps, resume)
    assert result.reconciled_calls == 1
    assert result.claims_attempted == 0 and result.claims_won == 0
    assert result.expired_candidates >= 2
    assert _candidate_state(store, wp3) == "EXPIRED"
    assert _candidate_state(store, wp4) == "EXPIRED"
    assert calls == []


# ---------------------------------------------------------- §12.3 expiry


def test_r_exp_1_expired_invariant_holds_after_mixed_lifecycle(tmp_path, monkeypatch):
    deps, _ = _bundle(tmp_path, classifier=_unbound)
    store = deps.episode_store
    _seed(store, ("불변만료일",), now=NOW - timedelta(seconds=120))
    old = NOW - timedelta(seconds=PRODUCER_CANDIDATE_TTL_SECONDS + 600)
    _seed(store, ("불변만료이",), now=old)
    for i in range(5):
        now = NOW + timedelta(seconds=60 * i)
        _mark_slot(store, now)
        _tick(deps, now)
    assert _states(store).get("EXPIRED", 0) >= 1
    assert _expired_invariant_violations(store) == 0


def test_r_exp_2_checkpointed_over_ttl_drain_beats_expiry(tmp_path, monkeypatch):
    calls = []
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier(calls))
    store = deps.episode_store
    start = NOW - timedelta(hours=7)
    _mark_slot(store, start)
    (attached,) = _seed(store, ("부착보호",), now=start - timedelta(seconds=60))
    (settled,) = _seed(store, ("세틀보호",), now=start - timedelta(seconds=59))
    admit_state = _raise_before(monkeypatch, store, "admit_alert_revision")
    upsert_state = _raise_before(
        monkeypatch,
        store,
        "upsert_with_cause",
        when=lambda key, *a, **k: key == "세틀보호",
    )
    _tick(deps, start)  # attached: C4 crash; settled: C3 crash
    assert (
        store.connection.execute(
            "SELECT episode_id FROM discovery_candidates WHERE candidate_id=?",
            (attached,),
        ).fetchone()[0]
        is not None
    )
    assert (
        store.connection.execute(
            "SELECT episode_id FROM discovery_candidates WHERE candidate_id=?",
            (settled,),
        ).fetchone()[0]
        is None
    )
    stale_terms = tuple(f"무체크{i:02d}" for i in range(21))
    _seed(store, stale_terms, now=start - timedelta(seconds=58))
    calls.clear()
    _mark_slot(store, NOW)
    result = _tick(deps, NOW)
    assert result.claims_won == 2
    assert result.claims_won_recovery == 2
    assert result.borrowed_slots == 1
    assert result.expired_candidates == PRODUCER_EXPIRY_PER_TICK
    assert calls == []
    assert _candidate_state(store, attached) == "MERGED_INTO_EPISODE"
    assert _candidate_state(store, settled) == "MERGED_INTO_EPISODE"
    assert _states(store).get("EXPIRED", 0) == PRODUCER_EXPIRY_PER_TICK
    assert _expired_invariant_violations(store) == 0
    follow = NOW + timedelta(seconds=60)
    _mark_slot(store, follow)
    result2 = _tick(deps, follow)
    assert result2.expired_candidates == 1
    assert _states(store).get("EXPIRED", 0) == 21


# ---------------------------------------------------- §12.4 morning hold


def test_r_mrn_1_identical_hold_replay_is_duplicate(tmp_path):
    store = NewsEpisodeStore(tmp_path / "news.sqlite")
    episode = store.upsert("hold-replay", (), NOW)
    release = datetime(2026, 8, 21, 23, tzinfo=UTC)
    first = store.hold_morning_revision(
        episode.episode_id, 1, "hold body", now=NOW, release_not_before=release
    )
    second = store.hold_morning_revision(
        episode.episode_id,
        1,
        "hold body",
        now=NOW + timedelta(seconds=5),
        release_not_before=release,
    )
    assert first[1] is ReplayResult.CREATED
    assert second[1] is ReplayResult.DUPLICATE
    assert second[0] == first[0]
    assert store.connection.execute(
        "SELECT count(*) FROM alert_revisions"
    ).fetchone() == (1,)
    assert store.connection.execute(
        "SELECT count(*) FROM morning_hold_queue"
    ).fetchone() == (1,)


def test_r_mrn_2_conflicting_hold_body_fails_closed(tmp_path):
    store = NewsEpisodeStore(tmp_path / "news.sqlite")
    episode = store.upsert("hold-conflict", (), NOW)
    release = datetime(2026, 8, 21, 23, tzinfo=UTC)
    store.hold_morning_revision(
        episode.episode_id, 1, "original", now=NOW, release_not_before=release
    )
    with pytest.raises(NewsEpisodeStoreError):
        store.hold_morning_revision(
            episode.episode_id, 1, "tampered", now=NOW, release_not_before=release
        )
    assert store.connection.execute(
        "SELECT count(*) FROM alert_revisions"
    ).fetchone() == (1,)


def test_r_mrn_3_released_hold_is_never_reheld(tmp_path):
    store = NewsEpisodeStore(tmp_path / "news.sqlite")
    episode = store.upsert("hold-released", (), NOW)
    release = NOW - timedelta(hours=1)
    store.hold_morning_revision(
        episode.episode_id,
        1,
        "사건: 야간 상태 확인",
        now=NOW - timedelta(hours=2),
        release_not_before=release,
    )
    released = store.release_morning_holds(now_kst=NOW.astimezone(KST))
    assert len(released) == 1
    replay = store.hold_morning_revision(
        episode.episode_id,
        1,
        "사건: 야간 상태 확인",
        now=NOW,
        release_not_before=release,
    )
    assert replay[1] is ReplayResult.DUPLICATE
    assert store.connection.execute(
        "SELECT count(*) FROM morning_hold_queue"
    ).fetchone() == (0,)
    assert store.connection.execute(
        "SELECT count(*) FROM alert_revisions WHERE revision_kind='MORNING_COALESCED'"
    ).fetchone() == (1,)
    assert store.connection.execute(
        "SELECT delivery_state FROM alert_revisions WHERE revision_no=1"
    ).fetchone() == ("SUPPRESSED",)


def test_release_suppresses_boilerplate_and_releases_meaningful(tmp_path):
    """Morning-alert repair: pure generic/no-value held items never reach
    the morning release; meaningful items release without the legacy
    integration prefix and with their held bytes intact."""
    store = NewsEpisodeStore(tmp_path / "news.sqlite")
    release = NOW - timedelta(hours=1)
    generic = store.upsert("generic-hold", (), NOW)
    store.hold_morning_revision(
        generic.episode_id,
        1,
        "📰 could 뉴스 동향\n대상: could\n판정: 긴급 알림 아님 · 아침 브리핑 대상\n"
        "근거: 기사 7건 · 출처 2곳",
        now=NOW - timedelta(hours=2),
        release_not_before=release,
    )
    legacy = store.upsert("legacy-json-hold", (), NOW)
    store.hold_morning_revision(
        legacy.episode_id,
        1,
        '{"v": 1, "candidate": "ndc-x", "stories": 7}',
        now=NOW - timedelta(hours=2),
        release_not_before=release,
    )
    real = store.upsert("real-hold", (), NOW)
    held_body = (
        "📰 NVIDIA 뉴스 동향\n사건: 엔비디아 신형 GPU 공개\n"
        "근거: 기사 3건 · 출처 2곳 · 전체 뉴스 빈도에서 발견"
    )
    store.hold_morning_revision(
        real.episode_id,
        1,
        held_body,
        now=NOW - timedelta(hours=2),
        release_not_before=release,
    )
    released = store.release_morning_holds(now_kst=NOW.astimezone(KST))
    assert len(released) == 1
    assert released[0]["episode_id"] == real.episode_id
    (body,) = store.connection.execute(
        "SELECT body_text FROM alert_revisions WHERE delivery_state='PENDING_OUTBOX'"
    ).fetchone()
    assert body == held_body
    assert "밤사이 최신 material 상태 통합:" not in body
    # Suppressed, not deleted: history rows survive, queue is drained.
    for episode in (generic, legacy):
        assert store.connection.execute(
            "SELECT delivery_state FROM alert_revisions WHERE episode_id=?",
            (episode.episode_id,),
        ).fetchall() == [("SUPPRESSED",)]
    assert store.connection.execute(
        "SELECT count(*) FROM morning_hold_queue"
    ).fetchone() == (0,)


def test_candidate_without_stored_headline_fails_closed_no_alert(tmp_path):
    """No stored evidence statement → no alert, no hold; the episode and
    classifier history still land (fail closed, never invent summaries)."""
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier())
    store = deps.episode_store
    (cid,) = _seed(
        store, ("헤드라인없음",), now=NOW - timedelta(seconds=60), headline=None
    )
    _mark_slot(store, NOW)
    _tick(deps, NOW)
    assert _candidate_state(store, cid) == "MERGED_INTO_EPISODE"
    assert store.connection.execute(
        "SELECT count(*) FROM alert_revisions"
    ).fetchone() == (0,)
    assert store.connection.execute(
        "SELECT count(*) FROM morning_hold_queue"
    ).fetchone() == (0,)
    assert store.connection.execute("SELECT count(*) FROM episodes").fetchone() == (1,)


def test_taxonomy_admission_runs_on_due_tick_and_is_replay_idempotent(tmp_path):
    corpus = [
        (f"nv-{i}", f"엔비디아(NVDA) 실적 발표 {i}", f"src-{i % 3}", SEEN)
        for i in range(3)
    ]
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier(), corpus=corpus)
    result = _tick(deps, NOW)
    store = deps.episode_store
    unit = store.taxonomy_unit("ENTITY_ALIAS", "nvda")
    assert unit["state"] == "VERIFIED_AUTO"
    assert unit["canonical_key"] == "ent:nvidia"
    assert result.taxonomy_admitted >= 1
    assert "nvda" in store.verified_entity_aliases()["ent:nvidia"]
    decisions = store.connection.execute(
        "SELECT count(*) FROM taxonomy_decisions"
    ).fetchone()[0]
    _tick(deps, NOW + timedelta(seconds=PRODUCER_INTERVAL_SECONDS))
    assert (
        store.connection.execute("SELECT count(*) FROM taxonomy_decisions").fetchone()[
            0
        ]
        == decisions
    )


def test_r_adm_2_ordinary_at_2am_kst_is_held_morning(tmp_path):
    night = datetime(2026, 8, 20, 17, 0, tzinfo=UTC)  # 02:00 KST
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier())
    store = deps.episode_store
    (cid,) = _seed(store, ("야간보류",), now=night - timedelta(seconds=60))
    _mark_slot(store, night)
    _tick(deps, night)
    assert _candidate_state(store, cid) == "MERGED_INTO_EPISODE"
    assert store.connection.execute(
        "SELECT count(*) FROM morning_hold_queue"
    ).fetchone() == (1,)
    assert store.connection.execute(
        "SELECT count(*) FROM alert_revisions WHERE delivery_state='PENDING_OUTBOX'"
    ).fetchone() == (0,)


def test_r_adm_3_morning_release_coalesces_overnight_holds(tmp_path):
    store = NewsEpisodeStore(tmp_path / "news.sqlite")
    episode = store.upsert("overnight", (), NOW)
    release = datetime(2026, 8, 20, 23, 0, tzinfo=UTC)  # 08:00 KST
    for revision_no, hour in ((1, 17), (2, 18), (3, 19)):
        store.hold_morning_revision(
            episode.episode_id,
            revision_no,
            f"사건: 야간 상태 {revision_no}",
            now=datetime(2026, 8, 20, hour, tzinfo=UTC),
            release_not_before=release,
        )
    released = store.release_morning_holds(now_kst=release.astimezone(KST))
    assert len(released) == 1
    assert store.connection.execute(
        "SELECT count(*) FROM alert_revisions WHERE delivery_state='PENDING_OUTBOX'"
    ).fetchone() == (1,)
    assert store.connection.execute(
        "SELECT count(*) FROM alert_revisions WHERE delivery_state='SUPPRESSED'"
    ).fetchone() == (3,)


# ---------------------------------------------- §12.5 model/episode/outbox


def test_r_mdl_1_unbound_stub_defers_with_single_shadow_ops_alert(tmp_path):
    deps, _ = _bundle(tmp_path, classifier=_unbound)
    store = deps.episode_store
    _seed(store, ("미바인딩",), now=NOW - timedelta(seconds=60))
    _mark_slot(store, NOW)
    result = _tick(deps, NOW)
    assert result.released_candidates == 1
    assert store.connection.execute(
        "SELECT count(*) FROM classifier_outages WHERE status='OPEN'"
    ).fetchone() == (1,)
    rows = [
        tuple(row)
        for row in deps.ops_kernel._connect().execute(
            "SELECT channel FROM alert_outbox WHERE origin='NEWS_OPS'"
        )
    ]
    assert rows == [("SHADOW",)]
    assert store.connection.execute("SELECT count(*) FROM episodes").fetchone() == (0,)


def test_r_mdl_2_same_outage_stays_deduped_across_ticks(tmp_path):
    deps, _ = _bundle(tmp_path, classifier=_unbound)
    store = deps.episode_store
    _seed(store, ("아웃티지중복",), now=NOW - timedelta(seconds=60))
    for i in range(5):
        now = NOW + timedelta(seconds=60 * i)
        _mark_slot(store, now)
        _tick(deps, now)
    assert (
        deps.ops_kernel._connect()
        .execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_OPS'")
        .fetchone()[0]
        == 1
    )


def test_r_mdl_4_malformed_model_config_is_fail_closed(tmp_path):
    calls = []
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier(calls))
    deps.classifier_config_path.write_text('{"unexpected":"key"}')
    store = deps.episode_store
    (cid,) = _seed(store, ("설정오류",), now=NOW - timedelta(seconds=60))
    _mark_slot(store, NOW)
    result = _tick(deps, NOW)
    assert calls == []
    assert _candidate_state(store, cid) == "SURFACED"
    assert store.connection.execute(
        "SELECT failure_class FROM classifier_outages WHERE status='OPEN'"
    ).fetchall() == [("CONFIG_INVALID",)]


def test_r_mdl_5_success_clears_open_outage_silently(tmp_path):
    deps, _ = _bundle(tmp_path, classifier=_unbound)
    store = deps.episode_store
    _seed(store, ("복구성공",), now=NOW - timedelta(seconds=60))
    _mark_slot(store, NOW)
    _tick(deps, NOW)
    assert store.connection.execute(
        "SELECT count(*) FROM classifier_outages WHERE status='OPEN'"
    ).fetchone() == (1,)
    healthy, _ = _bundle(tmp_path, classifier=_ok_classifier())
    later = NOW + timedelta(seconds=60)
    _mark_slot(healthy.episode_store, later)
    _tick(healthy, later)
    assert healthy.episode_store.connection.execute(
        "SELECT count(*) FROM classifier_outages WHERE status='OPEN'"
    ).fetchone() == (0,)
    assert (
        healthy.ops_kernel._connect()
        .execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_OPS'")
        .fetchone()[0]
        == 1
    )


def test_r_mdl_6_candidate_stage_call_rows_have_no_episode(tmp_path):
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier())
    store = deps.episode_store
    _seed(store, ("호출행바인딩",), now=NOW - timedelta(seconds=60))
    _mark_slot(store, NOW)
    _tick(deps, NOW)
    rows = store.connection.execute(
        "SELECT candidate_id,episode_id FROM classifier_calls"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] is not None and rows[0][1] is None


def test_r_epi_1_same_cause_across_slots_merges_into_one_episode(tmp_path):
    deps, _ = _bundle(
        tmp_path,
        classifier=_ok_classifier(),
        corpus=_cluster("동일원인"),
        terms=frozenset({"동일원인"}),
    )
    store = deps.episode_store
    _tick(deps, NOW)
    _tick(deps, NOW + timedelta(seconds=60))
    slot2 = NOW + timedelta(seconds=PRODUCER_INTERVAL_SECONDS)
    _tick(deps, slot2)
    _tick(deps, slot2 + timedelta(seconds=60))
    assert store.connection.execute("SELECT count(*) FROM episodes").fetchone() == (1,)
    assert store.connection.execute("SELECT revision FROM episodes").fetchone() == (2,)
    linked = store.connection.execute(
        "SELECT DISTINCT episode_id FROM discovery_candidates"
        " WHERE episode_id IS NOT NULL"
    ).fetchall()
    assert len(linked) == 1


def test_r_epi_2_distinct_causes_make_distinct_episodes(tmp_path):
    corpus = _cluster("원인갑") + _cluster("원인동")
    deps, _ = _bundle(
        tmp_path,
        classifier=_ok_classifier(),
        corpus=corpus,
        terms=frozenset({"원인갑", "원인동"}),
    )
    store = deps.episode_store
    _tick(deps, NOW)
    _tick(deps, NOW + timedelta(seconds=60))
    keys = store.connection.execute(
        "SELECT DISTINCT episode_key FROM episodes"
    ).fetchall()
    assert len(keys) == 2


def test_r_obx_1_shadow_rows_are_structurally_unclaimable(tmp_path):
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier())
    store = deps.episode_store
    _seed(store, ("섀도전달",), now=NOW - timedelta(seconds=60))
    _mark_slot(store, NOW)
    _tick(deps, NOW)
    sends = []
    outcome = run_alert_delivery(
        ops_kernel=deps.ops_kernel,
        role=ProducerRole.HERMES,
        telegram_send=lambda body, reply_markup=None: sends.append(body),
        keyboard_resolver=lambda alert_id: None,
        delivery_binder=store.bind_delivery_by_ops_alert,
        now=NOW,
    )
    assert outcome["claimed"] == 0 and sends == []


def test_r_obx_2_duplicate_admission_is_replay_not_second_row(tmp_path):
    store = NewsEpisodeStore(tmp_path / "news.sqlite")
    ops = OpsKernelStore(
        tmp_path / "ops" / "kernel.sqlite", stable_phase_ids=frozenset()
    )
    episode = store.upsert("obx-replay", (), NOW)
    draft = {
        "episode_id": episode.episode_id,
        "revision_no": 1,
        "revision_kind": "INITIAL",
        "body_text": "body",
        "portfolio_context_digest": "b" * 64,
        "confidence": 70,
        "material_change_kind": "FIRST_ADMISSION",
        "material_change_observed_at": NOW,
        "links": (),
    }
    for _ in range(2):
        run_news_event_admission(
            store=store,
            ops_kernel=ops,
            role=ProducerRole.HERMES,
            draft=dict(draft),
            activation_enabled=False,
            now=NOW,
        )
    assert store.connection.execute(
        "SELECT count(*) FROM alert_revisions"
    ).fetchone() == (1,)
    assert tuple(
        ops._connect().execute("SELECT count(*) FROM alert_outbox").fetchone()
    ) == (1,)


# --------------------------------------------------------- §12.6 privacy

SENTINEL_NAME = "사용자비밀보유종목구구칠"
SENTINEL_QUANTITY = "777888999"


def test_r_prv_3_4_sentinels_never_reach_classifier_payload(tmp_path):
    requests = []

    def capture(request):
        requests.append(request)
        return json.dumps(
            {
                "provider": request.provider,
                "model_id": request.model_id,
                "citations": [],
                "event_class": "ORDINARY",
            }
        )

    deps, _ = _bundle(
        tmp_path,
        classifier=capture,
        corpus=_cluster("삼성전자"),
        terms=frozenset({"삼성전자", SENTINEL_NAME, SENTINEL_QUANTITY}),
    )
    store = deps.episode_store
    _tick(deps, NOW)
    _tick(deps, NOW + timedelta(seconds=60))
    lanes = store.connection.execute("SELECT lane FROM discovery_candidates").fetchall()
    assert lanes == [("DIRECT_HOLDING",)]  # local holding use stays alive
    assert len(requests) == 1
    blob = json.dumps(
        [dataclasses.asdict(request) for request in requests],
        ensure_ascii=False,
        sort_keys=True,
    )
    assert SENTINEL_NAME not in blob
    assert SENTINEL_QUANTITY not in blob
    # SDS 2026-09-01 §D.4: ``lane``/``holdings_relation`` are part of the
    # approved bounded evidence contract, so the DIRECT tokens are no longer
    # forbidden; names, quantities, scopes, and paths still never leak.
    for token in ("account", "scope", "pension", ".json", ".sqlite3", str(tmp_path)):
        assert token not in blob, token
    assert "play" + "book" not in blob


# ---------------------------------------- §12.6 isolation / lock / stale


def test_r_iso_1_producer_stage_failure_keeps_tick_alive(tmp_path, monkeypatch):
    from tokmo.programs import integrated_pulse
    from tokmo.programs.integrated_pulse import run_pulse_once

    fixture_deps = integrated_pulse.build_fixture_pulse_deps(
        fixture={}, clock=lambda: NOW, root=tmp_path
    )
    deps = dataclasses.replace(fixture_deps, news_producer_deps=object())
    monkeypatch.setattr(
        integrated_pulse,
        "run_news_production",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    result = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=NOW,
        observed_now_utc=NOW,
        deps=deps,
    )
    stages = {item["stage"]: item for item in result.stages}
    assert stages["news_producer"]["status"] == "failed"
    assert stages["news_producer"]["reason"] == "failed_exception"
    assert result.outcome == "PARTIAL"
    assert "dialogue" in stages and stages["dialogue"]["status"] != "failed"


def _gate_payload(producer_mode="SHADOW"):
    return json.dumps(
        {
            "enabled": True,
            "cutoff": CUTOFF.isoformat(),
            "approved_by": "woong",
            "producer_mode": producer_mode,
        }
    )


def _production_root(tmp_path, corpus):
    from tokmo.programs.pulse_production_deps import (
        build_pulse_production_deps,
    )
    from tokmo.values.session_gate import Market

    gate = tmp_path / "news_activation.json"
    gate.write_text(_gate_payload())
    os.chmod(gate, 0o600)
    _n5(tmp_path / "news" / "external_news_v4.sqlite3", corpus)
    deps = build_pulse_production_deps(
        root=tmp_path,
        clock=lambda: NOW,
        watchlist_symbols={Market.KR: frozenset(), Market.US: frozenset()},
        dialogue_cutoff=NOW,
    )
    assert deps.news_producer_deps is not None
    return deps


def test_r_lck_1_held_flock_means_zero_producer_contact(tmp_path):
    from tokmo.programs.integrated_pulse import run_pulse_once

    deps = _production_root(tmp_path, _cluster("락중첩"))
    store = deps.news_producer_deps.episode_store
    (cid,) = _seed(store, ("락중첩후보",), now=NOW - timedelta(seconds=60))
    before = store.connection.execute(
        "SELECT updated_at FROM discovery_candidates WHERE candidate_id=?", (cid,)
    ).fetchone()[0]
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    holder = os.open(runtime / "pulse-60s.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        result = run_pulse_once(
            root=tmp_path,
            policy_version="pulse-60s-v1",
            scheduled_for_utc=NOW,
            observed_now_utc=NOW,
            deps=deps,
        )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    assert result.outcome == "OVERLAP" and result.reason == "lease_held"
    assert result.stages == ()
    assert store.connection.execute(
        "SELECT count(*) FROM discovery_runs"
    ).fetchone() == (1,)  # seed only
    assert (
        store.connection.execute(
            "SELECT updated_at FROM discovery_candidates WHERE candidate_id=?", (cid,)
        ).fetchone()[0]
        == before
    )
    assert _call_count(store) == 0
    # Positive control: with the lock free the same tick runs the stage.
    released = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=NOW + timedelta(seconds=60),
        observed_now_utc=NOW + timedelta(seconds=60),
        deps=deps,
    )
    stages = {item["stage"] for item in released.stages}
    assert "news_producer" in stages


def test_r_stl_1_stale_deferred_comment_is_gone():
    text = (
        Path(__file__).parents[1] / "src" / "tokmo" / "values" / "news_config.py"
    ).read_text()
    assert "no production discovery callsite exists yet" not in text


# ------------------------------------------------------- §12.7 fairness


def _protect(
    monkeypatch,
    store,
    protected_terms=("보호갑", "보호을"),
    settle_only=("보호을",),
    crash_settle_only_once=True,
    failing_episodes=None,
):
    """P1-style candidates fail permanently at C4; P2 also crashes its first
    C3 so it starts as a settled-OK-only checkpoint (SDS R-FAIR-1)."""
    failing_episodes = set() if failing_episodes is None else failing_episodes
    original_admit = store.admit_alert_revision

    def selective_admit(revision, **kwargs):
        if revision["episode_id"] in failing_episodes:
            raise RuntimeError("permanent C4 infrastructure failure")
        return original_admit(revision, **kwargs)

    monkeypatch.setattr(store, "admit_alert_revision", selective_admit)
    original_upsert = store.upsert_with_cause
    armed = {term: crash_settle_only_once for term in settle_only}

    def tracking_upsert(episode_key, *args, **kwargs):
        if armed.get(episode_key):
            armed[episode_key] = False
            raise RuntimeError("injected C3 crash")
        episode = original_upsert(episode_key, *args, **kwargs)
        if episode_key in protected_terms:
            failing_episodes.add(episode.episode_id)
        return episode

    monkeypatch.setattr(store, "upsert_with_cause", tracking_upsert)
    return failing_episodes


def _fairness_env(tmp_path, monkeypatch):
    calls = []
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier(calls))
    store = deps.episode_store
    pa, pb = sorted(_seed(store, ("보호갑", "보호을"), now=NOW - timedelta(seconds=60)))
    _protect(monkeypatch, store)
    _mark_slot(store, NOW)
    setup = _tick(deps, NOW)
    assert setup.candidate_failures == 2
    assert _candidate_state(store, pa) == "ADMITTED_FOR_INVESTIGATION"
    assert _candidate_state(store, pb) == "ADMITTED_FOR_INVESTIGATION"
    return deps, store, calls, (pa, pb)


def test_r_fair_1_permanent_recovery_failures_never_starve_fresh(tmp_path, monkeypatch):
    deps, store, calls, (pa, pb) = _fairness_env(tmp_path, monkeypatch)
    setup_calls = len(calls)
    fresh_ids = []
    for i in range(1, 31):
        now = NOW + timedelta(seconds=60 * i)
        fresh_ids.extend(
            _seed(store, (f"신규흐름{i:02d}",), now=now - timedelta(seconds=1))
        )
        _mark_slot(store, now)
        result = _tick(deps, now)
        assert result.candidate_failures == result.claims_won_recovery
        if result.candidate_failures:
            status, reason = production_stage_report(result)
            assert status == "partial"
            assert f"candidate_failures:{result.candidate_failures}" in reason
    for cid in fresh_ids:
        assert _candidate_state(store, cid) == "MERGED_INTO_EPISODE"
    assert _states(store).get("EXPIRED", 0) == 0
    assert _candidate_state(store, pa) == "ADMITTED_FOR_INVESTIGATION"
    assert _candidate_state(store, pb) == "ADMITTED_FOR_INVESTIGATION"
    assert len(calls) == setup_calls + len(fresh_ids)


def _assert_backoff_schedule(store, observed, protected, setup_claim_time):
    """Claim gaps must follow min(3600, max(300, age-at-previous-claim))
    with next-60s-tick tolerance (SDS §6.6 C1 / R-FAIR-2)."""
    for cid in protected:
        created = datetime.fromisoformat(
            store.connection.execute(
                "SELECT created_at FROM discovery_candidates WHERE candidate_id=?",
                (cid,),
            ).fetchone()[0]
        )
        times = [setup_claim_time] + [when for when, who in observed if who == cid]
        for previous, current in zip(times, times[1:]):
            age = (previous - created).total_seconds()
            expected_delay = min(
                PRODUCER_RECOVERY_BACKOFF_CAP_SECONDS, max(PRODUCER_LEASE_SECONDS, age)
            )
            gap = (current - previous).total_seconds()
            # Tolerance: exact-boundary julianday eligibility may slip one
            # 60s tick (SDS §6.6 C1), and losing the single recovery slot to
            # the other ready candidate costs at most one more tick.
            assert expected_delay - 1 <= gap <= expected_delay + 121, (
                cid,
                gap,
                expected_delay,
            )


def test_r_fair_2_rotation_and_bounded_age_based_backoff(tmp_path, monkeypatch):
    deps, store, calls, (pa, pb) = _fairness_env(tmp_path, monkeypatch)
    observed = []
    original_claim = store.claim_candidate
    clockbox = {"now": NOW}

    def spying_claim(cid, **kwargs):
        won = original_claim(cid, **kwargs)
        if won and cid in (pa, pb):
            observed.append((clockbox["now"], cid))
        return won

    monkeypatch.setattr(store, "claim_candidate", spying_claim)
    for i in range(1, 76):
        now = NOW + timedelta(seconds=60 * i)
        clockbox["now"] = now
        _seed(store, (f"공정신규{i:03d}",), now=now - timedelta(seconds=1))
        _mark_slot(store, now)
        _tick(deps, now)
    assert len(observed) >= 6
    for left, right in zip(observed, observed[1:]):
        assert left[1] != right[1]  # strict updated_at-ASC rotation
    _assert_backoff_schedule(store, observed, (pa, pb), NOW)


def test_r_fair_2_cap_holds_for_old_candidates(tmp_path, monkeypatch):
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier())
    store = deps.episode_store
    old_created = NOW - timedelta(seconds=7200)
    (cid,) = _seed(
        store,
        ("고령후보",),
        now=NOW,
        created=old_created,
        updated=NOW,
        state="ADMITTED_FOR_INVESTIGATION",
    )
    just_under = NOW + timedelta(seconds=PRODUCER_RECOVERY_BACKOFF_CAP_SECONDS - 60)
    _mark_slot(store, just_under)
    assert _tick(deps, just_under).claims_attempted == 0
    # exact-boundary eligibility is judged within the next 60s tick
    past_cap = NOW + timedelta(seconds=PRODUCER_RECOVERY_BACKOFF_CAP_SECONDS + 60)
    _mark_slot(store, past_cap)
    assert _tick(deps, past_cap).claims_won_recovery == 1


def test_r_fair_3_borrowing_is_bidirectional(tmp_path):
    # (a) recovery empty, two fresh
    deps, _ = _bundle(tmp_path / "fresh", classifier=_ok_classifier())
    store = deps.episode_store
    _seed(store, ("차용신규일", "차용신규이"), now=NOW - timedelta(seconds=60))
    _mark_slot(store, NOW)
    result = _tick(deps, NOW)
    assert result.claims_won == 2
    assert result.claims_won_fresh == 2 and result.borrowed_slots == 1
    # (b) fresh empty, two retry-ready recovery
    deps2, _ = _bundle(tmp_path / "recovery", classifier=_ok_classifier())
    store2 = deps2.episode_store
    _seed(
        store2,
        ("차용복구일", "차용복구이"),
        now=NOW - timedelta(seconds=400),
        state="ADMITTED_FOR_INVESTIGATION",
    )
    _mark_slot(store2, NOW)
    result2 = _tick(deps2, NOW)
    assert result2.claims_won == 2
    assert result2.claims_won_recovery == 2 and result2.borrowed_slots == 1


def test_r_fair_4_lane_schedule_survives_restart(tmp_path, monkeypatch):
    deps, store, calls, (pa, pb) = _fairness_env(tmp_path, monkeypatch)
    observed = []
    clockbox = {"now": NOW}

    def install_spy(target_store):
        original_claim = target_store.claim_candidate

        def spying_claim(cid, **kwargs):
            won = original_claim(cid, **kwargs)
            if won and cid in (pa, pb):
                observed.append((clockbox["now"], cid))
            return won

        monkeypatch.setattr(target_store, "claim_candidate", spying_claim)

    install_spy(store)
    active = deps
    restart_observed = None
    for index, i in enumerate(range(1, 31)):
        now = NOW + timedelta(seconds=60 * i)
        if index == 12:  # restart mid-progression: durable columns only
            restart_observed = len(observed)
            reopened, _ = _bundle(tmp_path, classifier=_ok_classifier(calls))
            store = reopened.episode_store
            failing = {
                row[0]
                for row in store.connection.execute(
                    "SELECT DISTINCT episode_id FROM discovery_candidates"
                    " WHERE candidate_id IN (?,?)",
                    (pa, pb),
                )
                if row[0]
            }
            _protect(
                monkeypatch,
                store,
                crash_settle_only_once=False,
                failing_episodes=failing,
            )
            install_spy(store)
            active = reopened
        clockbox["now"] = now
        _seed(store, (f"재시작신규{index:02d}",), now=now - timedelta(seconds=1))
        _mark_slot(store, now)
        _tick(active, now)
    assert restart_observed is not None
    assert len(observed) >= 4
    assert restart_observed >= 2  # claims happened before the restart
    assert len(observed) > restart_observed  # and continue after it
    for left, right in zip(observed, observed[1:]):
        assert left[1] != right[1]  # rotation continuity across restart
    _assert_backoff_schedule(store, observed, (pa, pb), NOW)


# ------------------------- M1: stored non-title evidence reaches discovery


def test_reader_supplies_stored_summary_for_alias_discovery(tmp_path):
    n5 = tmp_path / "n5.sqlite3"
    _n5(
        n5,
        [
            (
                "d1",
                "하닉 주주환원 시동",
                "src-1",
                SEEN,
                "SK하이닉스[000660] 자사주 매입 발표",
            )
        ],
    )
    read = read_discovery_documents_v1(
        n5, window_from=NOW - timedelta(hours=2), max_rows=10
    )
    assert read.status == "OK"
    (doc,) = read.documents
    assert doc.summary == "SK하이닉스[000660] 자사주 매입 발표"


def test_m1_production_tick_admits_summary_alias(tmp_path):
    # S3 갱신: 하닉 is now a PUBLIC catalog alias (SDS §F.2), so the
    # contraction-admission seam is proven with a not-yet-known variant.
    corpus = [
        (
            "nv-0",
            "엔비디 신형 가속기 공개",
            "src-0",
            SEEN,
            "엔비디아 신형 GPU 라인업 발표",
        ),
        (
            "nv-1",
            "엔비디 데이터센터 수주 확대",
            "src-1",
            SEEN,
            "엔비디아 데이터센터 매출 급증",
        ),
    ]
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier(), corpus=corpus)
    _tick(deps, NOW)
    store = deps.episode_store
    unit = store.taxonomy_unit("ENTITY_ALIAS", "엔비디")
    assert unit["state"] == "VERIFIED_AUTO"
    assert unit["canonical_key"] == "ent:nvidia"


# ------------- S1 (SDS 2026-09-01 W1–W3): evidence truthfulness plumbing —
# the reader carries role/publisher identity and discovery persists per-doc
# titles/sources/roles (payload v:2) while v:1 rows stay drainable (J-13).


def test_s1_reader_carries_evidence_role_and_publisher(tmp_path):
    n5 = tmp_path / "n5.sqlite3"
    _n5(
        n5,
        [
            (
                "d1",
                "SK하이닉스 자사주 소각 결정",
                "yonhap",
                SEEN,
                None,
                "FACT_DISCOVERY",
                "연합뉴스",
            ),
            (
                "d2",
                "삼성 소각 여력 분석",
                "investing",
                SEEN,
                None,
                "OPINION_DISCOVERY",
                "Investing.com",
            ),
        ],
    )
    read = read_discovery_documents_v1(
        n5, window_from=NOW - timedelta(hours=2), max_rows=10
    )
    assert read.status == "OK"
    by_id = {doc.document_id: doc for doc in read.documents}
    assert by_id["d1"].evidence_role == "FACT_DISCOVERY"
    assert by_id["d1"].publisher_name == "연합뉴스"
    assert by_id["d2"].evidence_role == "OPINION_DISCOVERY"
    assert by_id["d2"].publisher_name == "Investing.com"


def test_s1_discovery_persists_payload_v2_with_per_doc_provenance(tmp_path):
    newest = (NOW - timedelta(minutes=30)).isoformat()
    corpus = [
        (
            "hv-0",
            "하이닉스증거 소각 결정",
            "yonhap",
            SEEN,
            None,
            "FACT_DISCOVERY",
            "연합뉴스",
        ),
        (
            "hv-1",
            "하이닉스증거 소각 공시 임박",
            "jtbc",
            SEEN,
            None,
            "FACT_DISCOVERY",
            "JTBC",
        ),
        (
            "hv-2",
            "하이닉스증거 소각 기대 분석",
            "investing",
            newest,
            None,
            "OPINION_DISCOVERY",
            "Investing.com",
        ),
    ]
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier(), corpus=corpus)
    _tick(deps, NOW)
    store = deps.episode_store
    (payload_raw,) = store.connection.execute(
        "SELECT evidence_story_keys_json FROM discovery_candidates"
        " WHERE phrase_text='하이닉스증거'"
    ).fetchone()
    payload = json.loads(payload_raw)
    assert payload["v"] == 2
    # v:1-compatible fields survive for the rollback read path.
    assert set(payload["document_ids"]) == {"hv-0", "hv-1", "hv-2"}
    assert payload["latest_observed_at"] == newest
    assert payload["headline"]
    docs = payload["docs"]
    assert len(docs) == 3
    for entry in docs:
        assert set(entry) == {"title", "source_id", "role", "publisher", "observed_at"}
        assert len(entry["title"].encode("utf-8")) <= 200
    roles = {entry["source_id"]: entry["role"] for entry in docs}
    assert roles["yonhap"] == "FACT_DISCOVERY"
    assert roles["investing"] == "OPINION_DISCOVERY"
    publishers = {entry["source_id"]: entry["publisher"] for entry in docs}
    assert publishers["yonhap"] == "연합뉴스"
    # The newest observed document leads the stored provenance.
    assert docs[0]["source_id"] == "investing"
    assert docs[0]["observed_at"] == newest


def test_s1_candidate_documents_reads_v2_and_falls_back_v1():
    from tokmo.programs.news_production import _candidate_documents

    v2 = {
        "evidence_story_keys_json": json.dumps(
            {
                "v": 2,
                "document_ids": ["a", "b"],
                "latest_observed_at": SEEN,
                "headline": "사건 제목",
                "docs": [
                    {
                        "title": "사건 제목",
                        "source_id": "yonhap",
                        "role": "FACT_DISCOVERY",
                        "publisher": "연합뉴스",
                        "observed_at": SEEN,
                    }
                ],
            }
        )
    }
    ids, latest, headline, docs = _candidate_documents(v2)
    assert ids == ("a", "b") and latest == SEEN and headline == "사건 제목"
    assert docs[0]["role"] == "FACT_DISCOVERY"
    v1 = {
        "evidence_story_keys_json": json.dumps(
            {
                "v": 1,
                "document_ids": ["a"],
                "latest_observed_at": SEEN,
                "headline": "구형",
            }
        )
    }
    ids, latest, headline, docs = _candidate_documents(v1)
    assert ids == ("a",) and headline == "구형" and docs == ()
    legacy = {"evidence_story_keys_json": json.dumps(["a", "b"])}
    assert _candidate_documents(legacy) == (("a", "b"), None, None, ())


def _ok_v2_classifier(
    calls=None,
    *,
    event_type="OTHER",
    confirmation_claim="MULTI_MEDIA_UNCONFIRMED",
    surprise_claim="UNKNOWN",
    actor="이벤트주체",
    relevance_claim="NONE",
    relevance_citations=(),
):
    """Contract-r3 fact extraction fake (SDS §D.4 + A1 §Q.2)."""

    def call(request):
        if calls is not None:
            calls.append(request)
        return json.dumps(
            {
                "provider": request.provider,
                "model_id": request.model_id,
                "event_type": event_type,
                "confirmation_claim": confirmation_claim,
                "surprise_claim": surprise_claim,
                "actor": actor,
                "citations": [],
                "relevance_claim": relevance_claim,
                "relevance_citations": list(relevance_citations),
            }
        )

    return call


# Titles keep every non-alias token unique so exactly ONE candidate
# (ent:sk-hynix, via the public colloquial aliases) surfaces.
_HYNIX_CORPUS = [
    ("sk-0", "하닉 급등세일", "yonhap", SEEN, None, "FACT_DISCOVERY", "연합뉴스"),
    ("sk-1", "SK하닉 급등세이", "jtbc", SEEN, None, "FACT_DISCOVERY", "JTBC"),
    (
        "sk-2",
        "하이닉스 급등세삼",
        "investing",
        SEEN,
        None,
        "OPINION_DISCOVERY",
        "Investing.com",
    ),
]


def _drain_all(deps, *, start, ticks=8):
    for i in range(ticks):
        _tick(deps, start + timedelta(seconds=60 * (i + 1)))


# -------- S3 (SDS 2026-09-01 §D.4/§E, J-2..J-5/J-7/J-12): deterministic
# judgment wiring — truthful evidence v2, downgrade-only claims, code-owned
# event class, persisted episode class, and the confirmation label line.


def test_s3_classifier_evidence_is_bounded_v2_projection(tmp_path):
    requests = []
    deps, _ = _bundle(
        tmp_path,
        classifier=_ok_v2_classifier(requests),
        corpus=_HYNIX_CORPUS,
        terms=frozenset({"에스케이하이닉스"}),
    )
    _tick(deps, NOW)
    _drain_all(deps, start=NOW, ticks=3)
    assert len(requests) == 1
    evidence = requests[0].evidence
    headline_keys = {key for key in evidence if key.startswith("headline_")}
    # r3 (A3 §V1): official evidence is NOT classifier input — the key is
    # gone from the request/digest entirely.
    assert (
        set(evidence)
        == {
            "candidate",
            "lane",
            "holdings_relation",
            "stories",
            "sources",
            "fact_sources",
            "opinion_sources",
        }
        | headline_keys
    )
    assert "story_keys" not in evidence
    assert "official_evidence" not in evidence
    # Display name, never the ent:* key (W7/F.3).
    assert evidence["candidate"] == "sk hynix"
    assert evidence["lane"] == "DIRECT_HOLDING"
    assert evidence["holdings_relation"] == "DIRECT"
    assert evidence["fact_sources"] == "2"
    assert evidence["opinion_sources"] == "1"
    assert {
        "headline_1",
        "headline_1_src",
        "headline_2",
        "headline_2_src",
        "headline_3",
        "headline_3_src",
    } <= headline_keys
    assert "급등세" in evidence["headline_1"]
    source_id, role, publisher, observed = evidence["headline_1_src"].split("|")
    assert role in ("FACT_DISCOVERY", "OPINION_DISCOVERY")
    assert observed == SEEN
    for key in headline_keys:
        assert len(evidence[key].encode("utf-8")) <= 200
    canonical = json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert len(canonical.encode("utf-8")) <= 8192


def test_j2_daytime_multi_media_flows_ordinary_with_label(tmp_path):
    deps, _ = _bundle(
        tmp_path,
        classifier=_ok_v2_classifier(event_type="CORPORATE_ACTION_DECIDED"),
        corpus=_HYNIX_CORPUS,
        terms=frozenset({"에스케이하이닉스"}),
    )
    deps = dataclasses.replace(deps, delivery_activation=lambda now: True)
    _tick(deps, NOW)  # 18:00 KST — daytime
    _drain_all(deps, start=NOW, ticks=3)
    store = deps.episode_store
    (event_class,) = store.connection.execute(
        "SELECT event_class FROM episodes"
    ).fetchone()
    assert event_class == "ORDINARY"  # no official ref → never promoted
    (body,) = store.connection.execute(
        "SELECT body_text FROM alert_revisions"
    ).fetchone()
    assert "확인: 복수 언론 보도, 공식 확인 전" in body
    assert "판정:" not in body
    rows = [
        tuple(row)
        for row in deps.ops_kernel._connect().execute(
            "SELECT channel,severity FROM alert_outbox WHERE origin='NEWS_EVENT'"
        )
    ]
    assert rows == [("TELEGRAM", "ORDINARY")]
    assert store.connection.execute(
        "SELECT count(*) FROM morning_hold_queue"
    ).fetchone() == (0,)


def test_j2_night_multi_media_without_official_is_held_morning(tmp_path):
    night = datetime(2026, 8, 20, 17, 0, tzinfo=UTC)  # 02:00 KST
    seen = (night - timedelta(hours=1)).isoformat()
    corpus = [
        (doc_id, title, source, seen, summary, role, publisher)
        for doc_id, title, source, _seen, summary, role, publisher in _HYNIX_CORPUS
    ]
    deps, _ = _bundle(
        tmp_path,
        classifier=_ok_v2_classifier(event_type="CORPORATE_ACTION_DECIDED"),
        corpus=corpus,
        terms=frozenset({"에스케이하이닉스"}),
    )
    deps = dataclasses.replace(deps, delivery_activation=lambda now: True)
    _tick(deps, night)
    _drain_all(deps, start=night, ticks=3)
    store = deps.episode_store
    assert store.connection.execute("SELECT event_class FROM episodes").fetchone() == (
        "ORDINARY",
    )
    assert store.connection.execute(
        "SELECT count(*) FROM morning_hold_queue"
    ).fetchone() == (1,)
    assert (
        deps.ops_kernel._connect()
        .execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_EVENT'")
        .fetchone()[0]
        == 0
    )


def test_j12_injected_v1_core_label_never_promotes_or_interrupts(tmp_path):
    night = datetime(2026, 8, 20, 17, 0, tzinfo=UTC)  # 02:00 KST
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier(event_class="CORE_BREAKING"))
    deps = dataclasses.replace(deps, delivery_activation=lambda now: True)
    store = deps.episode_store
    _seed(store, ("모델주장코어",), now=night - timedelta(seconds=60))
    _mark_slot(store, night)
    _tick(deps, night)
    assert store.connection.execute("SELECT event_class FROM episodes").fetchone() == (
        "ORDINARY",
    )
    # Night + ORDINARY: held for the morning digest, no immediate outbox.
    assert store.connection.execute(
        "SELECT count(*) FROM morning_hold_queue"
    ).fetchone() == (1,)
    assert (
        deps.ops_kernel._connect()
        .execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_EVENT'")
        .fetchone()[0]
        == 0
    )


def test_s3_systemic_high_propagation_interrupts_at_night(tmp_path):
    night = datetime(2026, 8, 20, 17, 0, tzinfo=UTC)  # 02:00 KST
    seen = (night - timedelta(hours=1)).isoformat()
    corpus = [
        (
            f"war-{i}",
            "시장충격 관련 긴급조치 확산발생",
            f"src-{i}",
            seen,
            None,
            "FACT_DISCOVERY",
            _ORIGIN_PUBLISHERS[i],
        )
        for i in range(3)
    ]
    deps, _ = _bundle(
        tmp_path,
        classifier=_ok_v2_classifier(
            event_type="WAR_MARKET_DISRUPTION", surprise_claim="SURPRISE"
        ),
        corpus=corpus,
    )
    deps = dataclasses.replace(deps, delivery_activation=lambda now: True)
    _tick(deps, night)
    _drain_all(deps, start=night, ticks=3)
    store = deps.episode_store
    assert ("CORE_BREAKING",) in store.connection.execute(
        "SELECT event_class FROM episodes"
    ).fetchall()
    rows = [
        tuple(row)
        for row in deps.ops_kernel._connect().execute(
            "SELECT channel,severity FROM alert_outbox WHERE origin='NEWS_EVENT'"
        )
    ]
    assert ("TELEGRAM", "CORE_BREAKING") in rows


# -------- S4 (SDS 2026-09-01 §G.3, J-1/J-9): the injected read-only
# OpenDART seam is the only OFFICIAL_CONFIRMED path; with a matching
# disclosure the direct-holding decided corporate action becomes a truthful
# CORE_BREAKING that may interrupt at night.


_HYNIX_ACTION_CORPUS = [
    (
        "sk-0",
        "하닉 자사주 소각 결의 급등세일",
        "yonhap",
        SEEN,
        None,
        "FACT_DISCOVERY",
        "연합뉴스",
    ),
    ("sk-1", "SK하닉 급등세이", "jtbc", SEEN, None, "FACT_DISCOVERY", "JTBC"),
    (
        "sk-2",
        "하이닉스 급등세삼",
        "investing",
        SEEN,
        None,
        "OPINION_DISCOVERY",
        "Investing.com",
    ),
]


def _hynix_official_deps(tmp_path, *, corpus, disclosure_rows, classifier=None):
    from datetime import date as date_type

    from tokmo.programs.news_official_evidence import (
        build_official_evidence_reader,
        build_official_receipt_sink,
    )
    from tokmo.records.opendart_disclosures import DisclosureStore

    db = tmp_path / "opendart-bulk.sqlite3"
    store = DisclosureStore.open(db)
    for rcept_no, stock_code, report_nm, rcept_dt in disclosure_rows:
        store.connection.execute(
            "insert into disclosures values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rcept_no,
                "C" + rcept_no[-6:],
                "에스케이하이닉스",
                stock_code,
                "Y",
                report_nm,
                "회사",
                rcept_dt,
                None,
                "{}",
                "h" * 64,
                NOW.isoformat(),
                "run-1",
            ),
        )
    store.connection.commit()
    store.close()
    lookups = []
    reader = build_official_evidence_reader(
        db, receipt_sink=build_official_receipt_sink(tmp_path)
    )

    def official_evidence(stock_codes, date_from, date_to, allowed_families, **kwargs):
        assert isinstance(date_from, date_type)
        lookups.append((tuple(stock_codes), frozenset(allowed_families)))
        return reader(stock_codes, date_from, date_to, allowed_families, **kwargs)

    deps, _ = _bundle(
        tmp_path,
        classifier=classifier
        or _ok_v2_classifier(
            event_type="CORPORATE_ACTION_DECIDED",
            confirmation_claim="MULTI_MEDIA_UNCONFIRMED",
        ),
        corpus=corpus,
        terms=frozenset({"에스케이하이닉스"}),
    )
    deps = dataclasses.replace(
        deps,
        delivery_activation=lambda now: True,
        identity_pairs=lambda: {
            "ent:sk-hynix": ("000660", "SK하이닉스", "에스케이하이닉스")
        },
        official_evidence=official_evidence,
    )
    return deps, lookups


def test_j1_official_disclosure_makes_truthful_core_breaking(tmp_path):
    deps, lookups = _hynix_official_deps(
        tmp_path,
        corpus=_HYNIX_ACTION_CORPUS,
        disclosure_rows=[
            ("20260821000123", "000660", "주요사항보고서(자기주식소각결정)", "20260821")
        ],
    )
    _tick(deps, NOW)  # 18:00 KST — daytime
    _drain_all(deps, start=NOW, ticks=3)
    store = deps.episode_store
    assert len(lookups) == 1
    codes, families = lookups[0]
    assert codes == ("000660",)
    # §Z3: the query itself carried the article/event action intersection.
    assert families == frozenset({"BUYBACK", "BUYBACK_RETIREMENT"})
    (event_class,) = store.connection.execute(
        "SELECT event_class FROM episodes"
    ).fetchone()
    assert event_class == "CORE_BREAKING"
    (body,) = store.connection.execute(
        "SELECT body_text FROM alert_revisions"
    ).fetchone()
    assert "확인: 공식 확인(공시·1차 출처)" in body
    rows = [
        tuple(row)
        for row in deps.ops_kernel._connect().execute(
            "SELECT channel,severity FROM alert_outbox WHERE origin='NEWS_EVENT'"
        )
    ]
    assert rows == [("TELEGRAM", "CORE_BREAKING")]


def test_j9_official_core_interrupts_at_night(tmp_path):
    night = datetime(2026, 8, 20, 14, 0, tzinfo=UTC)  # 23:00 KST
    seen = (night - timedelta(hours=1)).isoformat()
    corpus = [
        (doc_id, title, source, seen, summary, role, publisher)
        for doc_id, title, source, _seen, summary, role, publisher in _HYNIX_ACTION_CORPUS
    ]
    deps, _lookups = _hynix_official_deps(
        tmp_path,
        corpus=corpus,
        disclosure_rows=[
            ("20260820000123", "000660", "주요사항보고서(자기주식소각결정)", "20260820")
        ],
    )
    _tick(deps, night)
    _drain_all(deps, start=night, ticks=3)
    store = deps.episode_store
    assert store.connection.execute("SELECT event_class FROM episodes").fetchone() == (
        "CORE_BREAKING",
    )
    # Night + CORE + immediate-eligible: PENDING now, never held (결정 2).
    assert store.connection.execute(
        "SELECT count(*) FROM morning_hold_queue"
    ).fetchone() == (0,)
    rows = [
        tuple(row)
        for row in deps.ops_kernel._connect().execute(
            "SELECT channel,severity FROM alert_outbox WHERE origin='NEWS_EVENT'"
        )
    ]
    assert rows == [("TELEGRAM", "CORE_BREAKING")]


def test_jb2_incompatible_action_keeps_media_confirmation(tmp_path):
    # Article says M&A (합병); the same-day filing is a dividend decision —
    # same company, same window, NO promotion (§Z3).
    corpus = [
        (
            "ma-0",
            "하닉 합병 추진 결의 보도",
            "yonhap",
            SEEN,
            None,
            "FACT_DISCOVERY",
            "연합뉴스",
        ),
        (
            "ma-1",
            "SK하닉 합병 검토 급물살",
            "jtbc",
            SEEN,
            None,
            "FACT_DISCOVERY",
            "JTBC",
        ),
    ]
    deps, lookups = _hynix_official_deps(
        tmp_path,
        corpus=corpus,
        disclosure_rows=[
            ("20260821000200", "000660", "현금ㆍ현물배당결정", "20260821")
        ],
    )
    _tick(deps, NOW)
    result = None
    for i in range(3):
        result = _tick(deps, NOW + timedelta(seconds=60 * (i + 1)))
    assert lookups  # the query ran with the M&A family…
    (body,) = deps.episode_store.connection.execute(
        "SELECT body_text FROM alert_revisions"
    ).fetchone()
    # …but the incompatible ref never promoted: media label preserved.
    assert "확인: 복수 언론 보도, 공식 확인 전" in body
    assert "공식 확인(공시·1차 출처)" not in body
    assert deps.episode_store.connection.execute(
        "SELECT event_class FROM episodes"
    ).fetchone() == ("ORDINARY",)


def test_jb9_empty_action_family_event_type_skips_query(tmp_path):
    deps, lookups = _hynix_official_deps(
        tmp_path,
        corpus=_HYNIX_ACTION_CORPUS,
        disclosure_rows=[
            ("20260821000123", "000660", "주요사항보고서(자기주식소각결정)", "20260821")
        ],
        classifier=_ok_v2_classifier(event_type="CONSUMER_MILESTONE"),
    )
    _tick(deps, NOW)
    result = None
    for i in range(3):
        result = _tick(deps, NOW + timedelta(seconds=60 * (i + 1)))
    assert lookups == []  # promotion-ineligible event type → query 0
    assert result.official_attempts == 0
    assert deps.episode_store.connection.execute(
        "SELECT event_class FROM episodes"
    ).fetchone() == ("ORDINARY",)


def test_jb3_non_direct_candidate_never_queries(tmp_path):
    # No holdings/identity — the 소각 article is not a direct-holding
    # candidate, so identity binding blocks the query entirely (§Z4).
    corpus = [
        (
            f"nd-{i}",
            "무관기업 자사주 소각 결의",
            f"src-{i}",
            SEEN,
            None,
            "FACT_DISCOVERY",
            publisher,
        )
        for i, publisher in enumerate(("연합뉴스", "JTBC", "BBC"))
    ]
    deps, lookups = _hynix_official_deps(
        tmp_path,
        corpus=corpus,
        disclosure_rows=[
            ("20260821000123", "000660", "주요사항보고서(자기주식소각결정)", "20260821")
        ],
    )
    deps = dataclasses.replace(
        deps, holding_terms=lambda: frozenset(), identity_pairs=dict
    )
    _tick(deps, NOW)
    result = None
    for i in range(4):
        result = _tick(deps, NOW + timedelta(seconds=60 * (i + 1)))
    assert lookups == []
    assert result.official_attempts == 0


def test_jb5_model_official_claim_without_ref_never_promotes(tmp_path):
    deps, lookups = _hynix_official_deps(
        tmp_path,
        corpus=_HYNIX_ACTION_CORPUS,
        disclosure_rows=[],
        classifier=_ok_v2_classifier(
            event_type="CORPORATE_ACTION_DECIDED",
            confirmation_claim="OFFICIAL_CONFIRMED",
        ),
    )
    _tick(deps, NOW)
    _drain_all(deps, start=NOW, ticks=3)
    store = deps.episode_store
    (body,) = store.connection.execute(
        "SELECT body_text FROM alert_revisions"
    ).fetchone()
    assert "공식 확인(공시·1차 출처)" not in body
    assert store.connection.execute("SELECT event_class FROM episodes").fetchone() == (
        "ORDINARY",
    )


def test_jb7_counters_one_attempt_and_no_post_settle_requery(tmp_path):
    deps, lookups = _hynix_official_deps(
        tmp_path,
        corpus=_HYNIX_ACTION_CORPUS,
        disclosure_rows=[
            ("20260821000123", "000660", "주요사항보고서(자기주식소각결정)", "20260821")
        ],
    )
    _tick(deps, NOW)
    totals = {
        "official_attempts": 0,
        "official_local_reads": 0,
        "official_http_starts": 0,
        "official_refs_returned": 0,
        "official_refs_correlated": 0,
        "official_failures": 0,
    }
    for i in range(1, 7):
        result = _tick(deps, NOW + timedelta(seconds=60 * i))
        for name in totals:
            totals[name] += getattr(result, name)
    # exactly one drain settled the ent candidate → exactly one attempt,
    # one local read, zero http starts, one correlated ref, zero failures.
    assert totals == {
        "official_attempts": 1,
        "official_local_reads": 1,
        "official_http_starts": 0,
        "official_refs_returned": 1,
        "official_refs_correlated": 1,
        "official_failures": 0,
    }
    assert len(lookups) == 1  # MERGED candidates never re-query


def test_jb11_async_first_drain_defers_lookup_until_settlement(tmp_path):
    from tokmo.programs.news_classifier_worker import run_classifier_worker

    deps, lookups = _hynix_official_deps(
        tmp_path,
        corpus=_HYNIX_ACTION_CORPUS,
        disclosure_rows=[
            ("20260821000123", "000660", "주요사항보고서(자기주식소각결정)", "20260821")
        ],
    )
    store = deps.episode_store
    deps = dataclasses.replace(deps, classifier_queue=store.enqueue_classifier_work)
    _tick(deps, NOW)
    first = _tick(deps, NOW + timedelta(seconds=60))
    # §V1 step 2: the async first drain DEFERRED — zero official reads.
    assert first.official_attempts == 0 and lookups == []
    assert store.classifier_work_counts().get("PENDING", 0) >= 1
    worker = run_classifier_worker(
        store=store,
        ops_kernel=deps.ops_kernel,
        role=deps.role,
        classifier_call=_ok_v2_classifier(
            event_type="CORPORATE_ACTION_DECIDED",
            confirmation_claim="MULTI_MEDIA_UNCONFIRMED",
        ),
        config_path=None,
        admitted_catalog={DEFAULT_NEWS_CLASSIFIER_MODEL_ID: "openai-codex"},
        now=NOW + timedelta(seconds=90),
        lease_owner="jb11",
    )
    assert worker["settled_ok"] >= 1
    later = NOW + timedelta(seconds=400)  # candidate lease elapsed
    totals = 0
    for i in range(6):
        result = _tick(deps, later + timedelta(seconds=60 * i))
        totals += result.official_attempts
    # §V1 steps 3–5: settled replay drove EXACTLY ONE post-settlement
    # lookup and the correlated ref promoted.
    assert totals == 1 and len(lookups) == 1
    episode_classes = {
        row[0]
        for row in store.connection.execute(
            "SELECT event_class FROM episodes"
        ).fetchall()
    }
    assert "CORE_BREAKING" in episode_classes


def test_s4_official_seam_field_defaults_inert():
    import dataclasses as dataclasses_module

    fields = {
        field.name: field for field in dataclasses_module.fields(NewsProducerDepsV1)
    }
    assert "official_evidence" in fields
    assert fields["official_evidence"].default is None


# -------- S6 (SDS A1 §R + A3 §V8/A5, JA-7/8): origin-normalized
# independence — dual feeds of one origin and unknown publishers can never
# manufacture MULTI confirmation or discovery-source diversity.


def test_ja7_yonhap_dual_feed_is_one_origin_single_media(tmp_path):
    from tokmo.values.news_publisher_origins import persisted_source_id

    market = persisted_source_id("connector:yonhap-market-rss")
    industry = persisted_source_id("connector:yonhap-industry-rss")
    # Same story republished across the two Yonhap feeds (distinct story
    # fingerprints/doc ids, one real origin) + one action token headline.
    corpus = [
        (
            "yy-0",
            "하닉 자사주 소각 결의",
            market,
            SEEN,
            None,
            "FACT_DISCOVERY",
            "연합뉴스",
        ),
        (
            "yy-1",
            "하닉 자사주 소각 결의 확정",
            industry,
            SEEN,
            None,
            "FACT_DISCOVERY",
            "연합뉴스",
        ),
    ]
    deps, _ = _bundle(
        tmp_path,
        classifier=_ok_v2_classifier(
            event_type="CORPORATE_ACTION_DECIDED",
            confirmation_claim="MULTI_MEDIA_UNCONFIRMED",
        ),
        corpus=corpus,
        terms=frozenset({"에스케이하이닉스"}),
    )
    deps = dataclasses.replace(deps, delivery_activation=lambda now: True)
    _tick(deps, NOW)
    _drain_all(deps, start=NOW, ticks=4)
    store = deps.episode_store
    bodies = [
        row[0]
        for row in store.connection.execute(
            "SELECT body_text FROM alert_revisions"
        ).fetchall()
    ]
    assert bodies  # daytime observation is preserved (결정 1)
    for body in bodies:
        # ONE origin: never a MULTI label, and the claim cannot raise it.
        assert "복수 언론 보도" not in body
        assert "확인: 단일 언론 보도, 미확인" in body


def test_ja8_unknown_origins_collapse_and_never_reach_propagation(tmp_path):
    night = datetime(2026, 8, 20, 17, 0, tzinfo=UTC)  # 02:00 KST
    seen = (night - timedelta(hours=1)).isoformat()
    # Three unresolved sources/publishers: systemic event_type + 3 feeds,
    # but ONE unknown origin unit → propagation never fires (§R.2/§V8).
    corpus = [
        (
            f"uk-{i}",
            "시장충격 급증",
            f"src-{i}",
            seen,
            None,
            "FACT_DISCOVERY",
            f"무명매체{i}",
        )
        for i in range(3)
    ]
    deps, _ = _bundle(
        tmp_path,
        classifier=_ok_v2_classifier(
            event_type="WAR_MARKET_DISRUPTION", surprise_claim="SURPRISE"
        ),
        corpus=corpus,
    )
    deps = dataclasses.replace(deps, delivery_activation=lambda now: True)
    _tick(deps, night)
    _drain_all(deps, start=night, ticks=4)
    store = deps.episode_store
    classes = {
        row[0]
        for row in store.connection.execute(
            "SELECT event_class FROM episodes"
        ).fetchall()
    }
    assert "CORE_BREAKING" not in classes
    assert (
        deps.ops_kernel._connect()
        .execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_EVENT'")
        .fetchone()[0]
        == 0
    )


def test_ja9_yonhap_dual_feed_and_unrelated_chulbeom_regression(tmp_path):
    """§S 필수 회귀 (the observed live noise shape): the same Yonhap story
    on both feeds (one origin) plus three unrelated `출범` stories yields
    candidates 0 · GLM 0 · Telegram 0; the coherent multi-origin cluster
    with a shared anchor is still admitted normally."""
    from tokmo.values.news_publisher_origins import persisted_source_id

    market = persisted_source_id("connector:yonhap-market-rss")
    industry = persisted_source_id("connector:yonhap-industry-rss")
    corpus = [
        # one real story, republished across the two Yonhap feeds
        (
            "yn-0",
            "지역재단 출범 준비 본격화",
            market,
            SEEN,
            None,
            "FACT_DISCOVERY",
            "연합뉴스",
        ),
        (
            "yn-1",
            "지역재단 출범 준비 박차",
            industry,
            SEEN,
            None,
            "FACT_DISCOVERY",
            "연합뉴스",
        ),
        # three unrelated stories sharing ONLY the token 출범
        ("un-0", "동호회 출범 선언", "src-a", SEEN, None, "FACT_DISCOVERY", "JTBC"),
        ("un-1", "협의체 출범 논의", "src-b", SEEN, None, "FACT_DISCOVERY", "BBC"),
        ("un-2", "신당 출범 임박", "src-c", SEEN, None, "FACT_DISCOVERY", "연합뉴스"),
    ]
    calls = []
    deps, _ = _bundle(tmp_path, classifier=_ok_v2_classifier(calls), corpus=corpus)
    deps = dataclasses.replace(deps, delivery_activation=lambda now: True)
    _tick(deps, NOW)
    _drain_all(deps, start=NOW, ticks=4)
    store = deps.episode_store
    assert store.connection.execute(
        "SELECT count(*) FROM discovery_candidates"
    ).fetchone() == (0,)
    assert calls == []  # GLM 0
    assert store.connection.execute(
        "SELECT count(*) FROM classifier_work_queue"
    ).fetchone() == (0,)
    assert (
        deps.ops_kernel._connect()
        .execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_EVENT'")
        .fetchone()[0]
        == 0
    )  # Telegram 0
    # Control: coherent multi-origin shared-anchor cluster still admits.
    control = [
        (
            f"ok-{i}",
            "출범 관련 긴급조치 확산발생",
            f"src-{i}",
            SEEN,
            None,
            "FACT_DISCOVERY",
            _ORIGIN_PUBLISHERS[i % 3],
        )
        for i in range(3)
    ]
    deps2, _ = _bundle(
        tmp_path / "control", classifier=_ok_v2_classifier(), corpus=control
    )
    deps2 = dataclasses.replace(deps2, delivery_activation=lambda now: True)
    _tick(deps2, NOW)
    _drain_all(deps2, start=NOW, ticks=3)
    assert (
        deps2.episode_store.connection.execute(
            "SELECT count(*) FROM alert_revisions"
        ).fetchone()[0]
        >= 1
    )


# -------- S8 (SDS A1 §Q, JA-11..13): cited AI relevance may promote the
# relevance axis only — never identity, officialness, or night CORE.


def test_ja11_cited_indirect_relevance_adds_label_only(tmp_path):
    # v:2 evidence docs (headline_1 key exists) via the corpus path.
    corpus = [
        (
            "rv-0",
            "협력사 물량 확대 수혜 전망",
            "yonhap",
            SEEN,
            None,
            "FACT_DISCOVERY",
            "연합뉴스",
        ),
        (
            "rv-1",
            "협력사 물량 확대 공식화",
            "jtbc",
            SEEN,
            None,
            "FACT_DISCOVERY",
            "JTBC",
        ),
        ("rv-2", "협력사 물량 확대 지속", "bbc", SEEN, None, "FACT_DISCOVERY", "BBC"),
    ]
    deps, _ = _bundle(
        tmp_path,
        classifier=_ok_v2_classifier(
            event_type="OTHER",
            relevance_claim="INDIRECT_MATERIAL",
            relevance_citations=("headline_1",),
        ),
        corpus=corpus,
    )
    deps = dataclasses.replace(deps, delivery_activation=lambda now: True)
    store = deps.episode_store
    _tick(deps, NOW)
    _drain_all(deps, start=NOW, ticks=4)
    bodies = [
        row[0]
        for row in store.connection.execute(
            "SELECT body_text FROM alert_revisions"
        ).fetchall()
    ]
    assert any("관련: 간접 연관(모델 인용)" in body for body in bodies)
    # relevance never touches the event class (§Q.3 axis separation).
    classes = {
        row[0]
        for row in store.connection.execute(
            "SELECT event_class FROM episodes"
        ).fetchall()
    }
    assert classes == {"ORDINARY"}


def test_ja12_uncited_relevance_claim_never_promotes(tmp_path):
    deps, _ = _bundle(
        tmp_path,
        classifier=_ok_v2_classifier(
            relevance_claim="INDIRECT_MATERIAL", relevance_citations=()
        ),
    )
    deps = dataclasses.replace(deps, delivery_activation=lambda now: True)
    store = deps.episode_store
    _seed(store, ("무인용관련",), now=NOW - timedelta(seconds=60))
    _mark_slot(store, NOW)
    _tick(deps, NOW)
    (body,) = store.connection.execute(
        "SELECT body_text FROM alert_revisions"
    ).fetchone()
    assert "관련:" not in body
    # and the claim can never fabricate DIRECT identity (JA-12 부속)
    assert "포트폴리오 직접 관련" not in body


def test_ja13_relevance_alone_never_interrupts_at_night(tmp_path):
    night = datetime(2026, 8, 20, 14, 0, tzinfo=UTC)  # 23:00 KST
    seen = (night - timedelta(hours=1)).isoformat()
    corpus = [
        (
            f"th-{i}",
            "테마수혜 확대 지속",
            f"src-{i}",
            seen,
            None,
            "FACT_DISCOVERY",
            publisher,
        )
        for i, publisher in enumerate(("연합뉴스", "JTBC", "BBC"))
    ]
    deps, _ = _bundle(
        tmp_path,
        classifier=_ok_v2_classifier(
            event_type="OTHER",
            relevance_claim="THEMATIC_MATERIAL",
            relevance_citations=("headline_1",),
        ),
        corpus=corpus,
    )
    deps = dataclasses.replace(deps, delivery_activation=lambda now: True)
    store = deps.episode_store
    _tick(deps, night)
    _drain_all(deps, start=night, ticks=4)
    assert (
        store.connection.execute("SELECT count(*) FROM morning_hold_queue").fetchone()[
            0
        ]
        >= 1
    )
    assert (
        deps.ops_kernel._connect()
        .execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_EVENT'")
        .fetchone()[0]
        == 0
    )


def test_j7_two_daytime_episodes_get_separate_pending_rows(tmp_path):
    deps, _ = _bundle(tmp_path, classifier=_ok_v2_classifier())
    deps = dataclasses.replace(deps, delivery_activation=lambda now: True)
    store = deps.episode_store
    _seed(store, ("주간건일", "주간건이"), now=NOW - timedelta(seconds=60))
    _mark_slot(store, NOW)
    _tick(deps, NOW)  # 18:00 KST — daytime, per-episode contract (결정 1)
    rows = [
        tuple(row)
        for row in deps.ops_kernel._connect().execute(
            "SELECT alert_id,channel FROM alert_outbox WHERE origin='NEWS_EVENT'"
        )
    ]
    assert len(rows) == 2
    assert len({alert_id for alert_id, _channel in rows}) == 2
    assert all(channel == "TELEGRAM" for _alert_id, channel in rows)


def test_s3_set_episode_event_class_is_vocab_guarded(tmp_path):
    store = NewsEpisodeStore(tmp_path / "news.sqlite")
    episode = store.upsert("event-class-helper", (), NOW)
    store.set_episode_event_class(episode.episode_id, "CORE_BREAKING", now=NOW)
    assert store.connection.execute(
        "SELECT event_class FROM episodes WHERE episode_id=?", (episode.episode_id,)
    ).fetchone() == ("CORE_BREAKING",)
    store.set_episode_event_class(episode.episode_id, "ORDINARY", now=NOW)
    assert store.connection.execute(
        "SELECT event_class FROM episodes WHERE episode_id=?", (episode.episode_id,)
    ).fetchone() == ("ORDINARY",)
    with pytest.raises(NewsEpisodeStoreError):
        store.set_episode_event_class(episode.episode_id, "SEVERE", now=NOW)
    with pytest.raises(NewsEpisodeStoreError):
        store.set_episode_event_class("nep-absent", "ORDINARY", now=NOW)


def test_s3_render_body_carries_confirmation_label_line():
    from tokmo.values.news_digest import morning_digest_eligible

    body = render_news_alert_body(
        term="ent:sk-hynix",
        event_class="ORDINARY",
        materiality="DIRECT_HOLDING",
        lane="DIRECT_HOLDING",
        stories=3,
        sources=2,
        headline="SK하이닉스 자사주 소각 결의 보도",
        confirmation_state="MULTI_MEDIA_UNCONFIRMED",
    )
    assert body.splitlines()[-1] == "확인: 복수 언론 보도, 공식 확인 전"
    assert "판정:" not in body and "대상:" not in body
    assert morning_digest_eligible(body)
    # An unknown state fails closed to no label line, never a crash.
    unlabelled = render_news_alert_body(
        term="ent:sk-hynix",
        event_class="ORDINARY",
        materiality="WEAK",
        lane="WHOLE_CORPUS_FREQUENCY",
        stories=3,
        sources=2,
        headline="SK하이닉스 자사주 소각 결의 보도",
        confirmation_state="NOT_A_STATE",
    )
    assert "확인:" not in unlabelled


def test_s2_prompt_policy_digest_is_v2_and_identity_axis_moves():
    """SDS §D.4 M-1/§H.1-1: the v2 semantics ride the prompt-policy digest
    (identity axis), never the persisted output_schema_id."""
    from tokmo.programs.news_production import (
        _MODEL_CONTRACT_DIGEST,
        _PROMPT_POLICY_DIGEST,
    )

    expected = hashlib.sha256(
        json.dumps(
            # A1 §Q.2/A3 §V1: the r3 contract (relevance keys + official-key
            # removal) opens the single new identity space of this delta.
            {"v": 3, "policy": "news-producer-event-class-r3"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    legacy = hashlib.sha256(
        json.dumps(
            {"v": 1, "policy": "news-producer-event-class-v1"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert _PROMPT_POLICY_DIGEST == expected
    assert _PROMPT_POLICY_DIGEST != legacy
    # The persisted schema-id contract stays the v1 literal.
    contract = hashlib.sha256(
        json.dumps(
            {"v": 1, "contract": "news-classifier-output-v1"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert _MODEL_CONTRACT_DIGEST == contract


def test_j13_legacy_v1_payload_candidate_drains_without_crash(tmp_path):
    calls = []
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier(calls))
    store = deps.episode_store
    # _seed writes a v:1 payload — the legacy-row shape this guards.
    (cid,) = _seed(store, ("구형페이로드",), now=NOW - timedelta(seconds=60))
    _mark_slot(store, NOW)
    result = _tick(deps, NOW)
    assert result.candidate_failures == 0
    assert _candidate_state(store, cid) == "MERGED_INTO_EPISODE"
    assert len(calls) == 1


# --------------------- M2: canonical identity resolution for holdings pairs


def test_m2_holdings_enrich_existing_canonical_registry_identity():
    from tokmo.programs.pulse_production_deps import (
        resolve_news_identity_pairs,
    )

    pairs = resolve_news_identity_pairs([("000660", "KR", ("SK하이닉스",))])
    assert "ent:sk-hynix" in pairs
    assert "ent:000660" not in pairs
    assert "000660" in pairs["ent:sk-hynix"]
    assert "SK하이닉스" in pairs["ent:sk-hynix"]


def test_m2_unmatched_pair_gets_market_qualified_identity_never_bare():
    from tokmo.programs.pulse_production_deps import (
        resolve_news_identity_pairs,
    )

    pairs = resolve_news_identity_pairs([("005930", "KR", ("삼성전자",))])
    assert set(pairs) == {"ent:kr:005930"}
    assert "005930" in pairs["ent:kr:005930"]
    assert "삼성전자" in pairs["ent:kr:005930"]


def test_m2_us_kr_symbol_collision_never_merges():
    from tokmo.programs.pulse_production_deps import (
        resolve_news_identity_pairs,
    )

    pairs = resolve_news_identity_pairs(
        [("QQQ", "KR", ("케이알상장지수",)), ("QQQ", "US", ("Invesco QQQ",))]
    )
    assert set(pairs) == {"ent:kr:qqq", "ent:us:qqq"}
    assert pairs["ent:kr:qqq"] != pairs["ent:us:qqq"]


def test_m2_unknown_market_without_registry_match_fails_closed():
    from tokmo.programs.pulse_production_deps import (
        resolve_news_identity_pairs,
    )

    assert resolve_news_identity_pairs([("XYZ", None, ())]) == {}
    # A registry match still resolves without a market token.
    pairs = resolve_news_identity_pairs([("NVDA", None, ("엔비디아",))])
    assert set(pairs) == {"ent:nvidia"}


# ------------------ M3: approved-source interest-term enumeration (typed)


def _watchlist_dir(tmp_path, kr=(), us=()):
    directory = tmp_path / "watchlists"
    directory.mkdir(parents=True, exist_ok=True)
    for market, entries in (("KR", kr), ("US", us)):
        payload = {
            "schema_version": 1,
            "market": market,
            "entries": [
                {
                    "symbol": symbol,
                    "name": name,
                    "status": "active",
                    "added_at": "2026-08-01T00:00:00+00:00",
                    "source": "user",
                }
                for symbol, name in sorted(entries)
            ],
        }
        (directory / f"{market}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    return directory


class _TypedHypothesisOps:
    """Stub over the canonical read-only ops-kernel hypothesis enumerators."""

    def __init__(self, index_keys):
        from types import SimpleNamespace

        self._rows = (SimpleNamespace(hypothesis_id="hypothesis:" + "a" * 24),)
        self._index = tuple(index_keys)

    def read_open_hypotheses(self):
        return self._rows

    def read_hypothesis_index(self, hypothesis_id):
        return self._index


def test_m3_watchlist_identities_enumerator_reads_symbol_and_name(tmp_path):
    from tokmo.values.session_gate import Market
    from tokmo.watchlists import load_watchlist_identities

    directory = _watchlist_dir(
        tmp_path, kr=(("000660", "SK하이닉스"),), us=(("NVDA", "엔비디아"),)
    )
    identities = load_watchlist_identities(directory)
    assert identities[Market.KR] == (("000660", "SK하이닉스"),)
    assert identities[Market.US] == (("NVDA", "엔비디아"),)


def test_m3_interest_terms_enumerate_watchlist_and_youtube_typed_lanes(tmp_path):
    from tokmo.programs.pulse_production_deps import (
        enumerate_news_interest_terms,
    )
    from tokmo.values.ops_kernel import HypothesisIndexKeyV1, IndexKeyKind

    directory = _watchlist_dir(
        tmp_path, kr=(("000660", "SK하이닉스"),), us=(("NVDA", "엔비디아"),)
    )
    ops = _TypedHypothesisOps(
        (
            HypothesisIndexKeyV1(IndexKeyKind.ENTITY, "entity:sk-hynix"),
            HypothesisIndexKeyV1(IndexKeyKind.METRIC, "metric:hbm-capex"),
            HypothesisIndexKeyV1(IndexKeyKind.EFFECT, "effect:dram-price-up"),
            HypothesisIndexKeyV1(
                IndexKeyKind.SOURCE_EVENT, "source-event:earnings-call"
            ),
        )
    )
    typed = enumerate_news_interest_terms(ops, directory)
    assert {"000660", "SK하이닉스", "NVDA", "엔비디아", "sk-hynix"} <= set(
        typed["ENTITY"]
    )
    assert "hbm-capex" in typed["METRIC"]
    assert "dram-price-up" in typed["EFFECT"]
    assert "earnings-call" in typed["CATALYST"]
    # Typed non-entity lanes never leak into the entity lane.
    assert "hbm-capex" not in typed["ENTITY"]


def test_m3_interest_terms_fail_closed_on_absent_or_malformed(tmp_path):
    from tokmo.programs.pulse_production_deps import (
        enumerate_news_interest_terms,
    )

    # Absent watchlist directory and no ops kernel → nothing, no crash.
    assert enumerate_news_interest_terms(None, None) == {}
    assert enumerate_news_interest_terms(None, tmp_path / "absent-watchlists") == {}
    # Malformed watchlist file contributes nothing.
    directory = tmp_path / "watchlists"
    directory.mkdir()
    (directory / "KR.json").write_text("{broken", encoding="utf-8")
    (directory / "US.json").write_text("{broken", encoding="utf-8")
    assert enumerate_news_interest_terms(None, directory) == {}


# --------------------- M4: single combined morning digest per release cycle


def _hold(store, key, body, *, observed, release, revision_no=1):
    episode = store.upsert(key, (), observed)
    store.hold_morning_revision(
        episode.episode_id, revision_no, body, now=observed, release_not_before=release
    )
    return episode


def _digest_bodies(store):
    return [
        row[0]
        for row in store.connection.execute(
            "SELECT body_text FROM alert_revisions"
            " WHERE delivery_state='PENDING_OUTBOX'"
        ).fetchall()
    ]


def test_m4_multiple_eligible_holds_release_one_combined_digest(tmp_path):
    store = NewsEpisodeStore(tmp_path / "news.sqlite")
    release = NOW - timedelta(hours=1)
    _hold(
        store,
        "다이제스트일",
        "📰 NVIDIA 뉴스 동향\n사건: 엔비디아 신형 GPU 공개\n근거: 기사 3건 · 출처 2곳",
        observed=NOW - timedelta(hours=3),
        release=release,
    )
    _hold(
        store,
        "다이제스트이",
        "📰 SK하이닉스 뉴스 동향\n사건: SK하이닉스 HBM 증설 확정\n근거: 기사 4건 · 출처 2곳",
        observed=NOW - timedelta(hours=2),
        release=release,
    )
    released = store.release_morning_holds(now_kst=NOW.astimezone(KST))
    assert len(released) == 1
    (body,) = _digest_bodies(store)
    assert "사건: SK하이닉스 HBM 증설 확정" in body
    assert "사건: 엔비디아 신형 GPU 공개" in body
    # Ordered by material observed time, latest first.
    assert body.index("SK하이닉스 HBM") < body.index("엔비디아 신형")
    for forbidden in ("대상:", "판정:", "밤사이 최신 material 상태 통합:"):
        assert forbidden not in body
    assert not body.lstrip().startswith("{")
    # All held revisions suppressed (history preserved), queue drained.
    assert store.connection.execute(
        "SELECT count(*) FROM alert_revisions WHERE delivery_state='HELD_MORNING'"
    ).fetchone() == (0,)
    assert store.connection.execute(
        "SELECT count(*) FROM morning_hold_queue"
    ).fetchone() == (0,)
    # Replay: retries never duplicate the combined digest.
    assert store.release_morning_holds(now_kst=NOW.astimezone(KST)) == ()
    assert len(_digest_bodies(store)) == 1


def test_m4_one_outbox_item_for_many_holds_and_replay_zero_extra(tmp_path):
    store = NewsEpisodeStore(tmp_path / "stores" / "news_episodes.sqlite3")
    ops = OpsKernelStore(
        tmp_path / "ops" / "ops_kernel.sqlite3", stable_phase_ids=frozenset()
    )
    release = NOW - timedelta(hours=1)
    for i, key in enumerate(("아웃박스일", "아웃박스이", "아웃박스삼")):
        _hold(
            store,
            key,
            f"📰 {key} 뉴스 동향\n사건: {key} 수주 확대 공시\n근거: 기사 3건 · 출처 2곳",
            observed=NOW - timedelta(hours=3 - i),
            release=release,
        )
    released = store.release_morning_holds(now_kst=NOW.astimezone(KST))
    assert len(released) == 1
    reconcile_news_outbox(
        store=store,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        activation_enabled=False,
        now=NOW,
    )
    assert tuple(
        ops._connect()
        .execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_EVENT'")
        .fetchone()
    ) == (1,)
    # Replay of the full release+reconcile cycle adds nothing.
    assert store.release_morning_holds(now_kst=NOW.astimezone(KST)) == ()
    reconcile_news_outbox(
        store=store,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        activation_enabled=False,
        now=NOW,
    )
    assert tuple(
        ops._connect()
        .execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_EVENT'")
        .fetchone()
    ) == (1,)


def test_m4_latest_state_per_episode_only_and_generic_omitted(tmp_path):
    store = NewsEpisodeStore(tmp_path / "news.sqlite")
    release = NOW - timedelta(hours=1)
    episode = _hold(
        store,
        "갱신사건",
        "📰 갱신사건 뉴스 동향\n사건: 갱신사건 1차 보도\n근거: 기사 3건 · 출처 2곳",
        observed=NOW - timedelta(hours=4),
        release=release,
    )
    store.hold_morning_revision(
        episode.episode_id,
        2,
        "📰 갱신사건 뉴스 동향\n사건: 갱신사건 최종 확정 보도\n근거: 기사 5건 · 출처 3곳",
        now=NOW - timedelta(hours=2),
        release_not_before=release,
    )
    _hold(
        store,
        "제이사건",
        "📰 제이사건 뉴스 동향\n사건: 제이사건 규제 결정\n근거: 기사 3건 · 출처 2곳",
        observed=NOW - timedelta(hours=3),
        release=release,
    )
    _hold(
        store,
        "일반사건",
        "📰 could 뉴스 동향\n대상: could\n판정: 긴급 알림 아님\n근거: 기사 7건 · 출처 2곳",
        observed=NOW - timedelta(hours=2),
        release=release,
    )
    released = store.release_morning_holds(now_kst=NOW.astimezone(KST))
    assert len(released) == 1
    (body,) = _digest_bodies(store)
    assert "갱신사건 최종 확정 보도" in body
    assert "갱신사건 1차 보도" not in body
    assert "제이사건 규제 결정" in body
    assert "could" not in body and "판정:" not in body


def test_m4_empty_eligible_set_releases_zero(tmp_path):
    store = NewsEpisodeStore(tmp_path / "news.sqlite")
    release = NOW - timedelta(hours=1)
    _hold(
        store,
        "무의미일",
        "📰 could 뉴스 동향\n대상: could\n판정: 긴급 알림 아님",
        observed=NOW - timedelta(hours=3),
        release=release,
    )
    _hold(
        store,
        "무의미이",
        '{"v": 1, "stories": 7}',
        observed=NOW - timedelta(hours=2),
        release=release,
    )
    assert store.release_morning_holds(now_kst=NOW.astimezone(KST)) == ()
    assert _digest_bodies(store) == []
    assert store.connection.execute(
        "SELECT count(*) FROM morning_hold_queue"
    ).fetchone() == (0,)
    assert store.connection.execute(
        "SELECT count(*) FROM alert_revisions WHERE delivery_state='SUPPRESSED'"
    ).fetchone() == (2,)


def test_m4_digest_is_bounded_and_overflow_never_yields_second_digest(tmp_path):
    """B1 (2026-08-22 reclosure): >MAX eligible holds still release exactly
    ONE Telegram-visible digest for the whole morning. Overflow beyond the
    digest bounds is dequeued and suppressed in the same transaction —
    auditable, never deleted, and never eligible for a later tick."""
    from tokmo.values.news_digest import MORNING_DIGEST_MAX_ITEMS

    store = NewsEpisodeStore(tmp_path / "stores" / "news_episodes.sqlite3")
    ops = OpsKernelStore(
        tmp_path / "ops" / "ops_kernel.sqlite3", stable_phase_ids=frozenset()
    )
    release = NOW - timedelta(hours=1)
    total = MORNING_DIGEST_MAX_ITEMS + 2
    for i in range(total):
        _hold(
            store,
            _hterm("경계사건", i),
            f"📰 {_hterm('경계사건', i)} 뉴스 동향\n"
            f"사건: {_hterm('경계사건', i)} 수주 확대 공시\n근거: 기사 3건 · 출처 2곳",
            observed=NOW - timedelta(hours=6, minutes=-i),
            release=release,
        )
    first = store.release_morning_holds(now_kst=NOW.astimezone(KST))
    assert len(first) == 1
    (body,) = _digest_bodies(store)
    assert body.count("사건: ") == MORNING_DIGEST_MAX_ITEMS
    # One morning digest per approved contract: the overflow is dequeued
    # with the selected members, so the queue is empty after the release.
    assert store.connection.execute(
        "SELECT count(*) FROM morning_hold_queue"
    ).fetchone() == (0,)
    assert store.connection.execute(
        "SELECT count(*) FROM alert_revisions WHERE delivery_state='HELD_MORNING'"
    ).fetchone() == (0,)
    # Omitted overflow is auditable on the released digest's provenance.
    links = json.loads(
        store.connection.execute(
            "SELECT links_json FROM alert_revisions"
            " WHERE revision_kind='MORNING_COALESCED'"
        ).fetchone()[0]
    )
    omitted = [entry for entry in links if entry.get("omitted") == "DIGEST_OVERFLOW"]
    assert len(omitted) == total - MORNING_DIGEST_MAX_ITEMS
    assert len(links) - len(omitted) == MORNING_DIGEST_MAX_ITEMS
    # Overflow history is preserved: SUPPRESSED with held bytes intact.
    for entry in omitted:
        state, held_body = store.connection.execute(
            "SELECT delivery_state,body_text FROM alert_revisions"
            " WHERE alert_revision_id=?",
            (entry["alert_revision_id"],),
        ).fetchone()
        assert state == "SUPPRESSED"
        assert "사건: " in held_body
    # Exactly one visible outbox item across first and subsequent ticks.
    reconcile_news_outbox(
        store=store,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        activation_enabled=False,
        now=NOW,
    )
    assert tuple(
        ops._connect()
        .execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_EVENT'")
        .fetchone()
    ) == (1,)
    assert store.release_morning_holds(now_kst=NOW.astimezone(KST)) == ()
    reconcile_news_outbox(
        store=store,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        activation_enabled=False,
        now=NOW,
    )
    assert tuple(
        ops._connect()
        .execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_EVENT'")
        .fetchone()
    ) == (1,)
    assert store.connection.execute(
        "SELECT count(*) FROM alert_revisions WHERE revision_kind='MORNING_COALESCED'"
    ).fetchone() == (1,)


def test_m4_production_tick_releases_single_digest(tmp_path):
    deps, _ = _bundle(tmp_path, classifier=_ok_classifier())
    store = deps.episode_store
    release = NOW - timedelta(hours=1)
    for i, key in enumerate(("틱사건일", "틱사건이")):
        _hold(
            store,
            key,
            f"📰 {key} 뉴스 동향\n사건: {key} 수주 확대 공시\n근거: 기사 3건 · 출처 2곳",
            observed=NOW - timedelta(hours=3 - i),
            release=release,
        )
    _mark_slot(store, NOW)
    result = _tick(deps, NOW)
    assert result.released_holds == 1
    assert len(_digest_bodies(store)) == 1
