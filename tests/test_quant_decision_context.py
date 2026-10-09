"""Regression cases for source depth, point-in-time context and price periods."""

import json
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from test_quant_decisions import response, sample_input
from test_quant_local_context import _dart_store, _news_document, _news_store

from tokmo.programs.quant_decision_analysis import (
    analysis_display_values,
    make_packet,
    reusable_analysis,
)
from tokmo.programs.quant_decision_context import (
    TOTAL_TEXT_LIMIT,
    _cached_disclosures,
    collect_decision_context,
    company_match,
    replace_local_context,
)
from tokmo.programs.quant_price_context import evidence_coverage, price_context
from tokmo.values.quant_decisions import parse_snapshot

NOW = datetime(2026, 9, 4, 1, tzinfo=UTC)
ITEM = dict(symbol="000001", market="KR", name="테스트전자")
UNIVERSE = {"000001": ("테스트전자",)}


def test_saved_english_identity_reaches_local_news_without_market_collision(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from tokmo.programs import quant_decision_inputs as inputs

    monkeypatch.setattr(
        inputs.CurrentHoldingsStore,
        "read",
        lambda self: SimpleNamespace(captured_at=NOW, accounts=[]),
    )
    store, _ = _news_store(tmp_path)
    _news_document(store, "a" * 32, "Example Aerospace announces a new contract")
    _news_document(
        store,
        "b" * 32,
        "Example Aerospace later report",
        first_seen="2026-09-04T02:00:00.000000+00:00",
    )
    rows = [
        dict(
            symbol="SAMPLE",
            market="US",
            name="가상항공",
            english_name="Example Aerospace",
        ),
        dict(
            symbol="SAMPLE",
            market="KR",
            name="별개기업",
            english_name="Different Company",
        ),
        dict(symbol="IT", market="US", name="가상정보"),
    ]
    data = inputs.load_local_snapshot(
        tmp_path, tmp_path, as_of=NOW, supplement={"instruments": rows}
    )
    us, kr, missing = data["instruments"]
    assert len(us["evidence"]) == 1 and "new contract" in us["evidence"][0]["text"]
    assert kr["evidence"] == [] and missing["evidence"] == []
    assert "identity:ENGLISH_NAME_MISSING" in missing["sources"]


def test_missing_short_ticker_name_is_not_an_official_company_alias(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from tokmo.programs import quant_decision_inputs as inputs

    monkeypatch.setattr(
        inputs.CurrentHoldingsStore,
        "read",
        lambda self: SimpleNamespace(captured_at=NOW, accounts=[]),
    )
    store, _ = _news_store(tmp_path)
    _news_document(store, "a" * 32, "IT spending is growing across the industry")
    data = inputs.load_local_snapshot(
        tmp_path,
        tmp_path,
        as_of=NOW,
        supplement={"instruments": [dict(symbol="IT", market="US")]},
    )
    assert data["instruments"][0]["evidence"] == []


def collect(root, **kwargs):
    return collect_decision_context(root, ITEM, as_of=NOW, universe=UNIVERSE, **kwargs)


def test_company_particles_preserve_identity_without_accepting_subsidiary():
    assert company_match("HD현대중공업이 협상안을 제시했다", ["HD현대중공업"])
    assert company_match("HD현대중공업에서 발표", ["HD현대중공업"])
    assert not company_match("HD현대중공업터보기계 공장", ["HD현대중공업"])
    assert company_match("Visa quarterly results", ["Visa"])
    assert not company_match("Televisa quarterly results", ["Visa"])


def test_news_summary_and_followup_are_preserved_in_source_order(tmp_path):
    store, path = _news_store(tmp_path)
    _news_document(store, "a" * 32, "테스트전자 노조 다음 주 파업 예고")
    _news_document(
        store, "b" * 32, "노사 합의 타결", first_seen="2026-09-03T00:00:00.000000+00:00"
    )
    with sqlite3.connect(path) as c:
        c.execute(
            "UPDATE news_documents SET summary_text='테스트전자가 협상을 타결하고 파업을 철회했다' WHERE news_document_id=?",
            ("b" * 32,),
        )
    evidence, status = collect(tmp_path)
    assert len(evidence) == 2
    assert "예고" in evidence[0]["text"]
    assert "철회" in evidence[1]["text"]
    assert evidence[1]["metadata"]["content_extent"] == "source_summary"
    assert evidence[1]["available_at"].startswith("2026-09-03")
    assert status["news"] == "OK"


def test_body_revision_cannot_leak_into_earlier_analysis(tmp_path):
    store, path = _news_store(tmp_path)
    _news_document(store, "a" * 32, "테스트전자 공급 계약")
    with sqlite3.connect(path) as c:
        c.execute(
            "UPDATE news_documents SET body_text='계약이 변경됐다',content_state='BODY_AVAILABLE',license_state='PERMITTED',revision_no=2,last_seen_at='2026-09-04T02:00:00.000000+00:00'"
        )
    evidence, _ = collect(tmp_path)
    assert evidence == []
    later, _ = collect_decision_context(
        tmp_path, ITEM, as_of=NOW + timedelta(hours=2), universe=UNIVERSE
    )
    assert later[0]["metadata"]["content_extent"] == "body_excerpt"
    assert later[0]["metadata"]["history_scope"] == "current_revision_only"
    assert later[0]["available_at"].startswith("2026-09-04T02:")


def test_future_publication_is_excluded_and_republication_deduplicated(tmp_path):
    store, path = _news_store(tmp_path)
    for char in "abc":
        _news_document(store, char * 32, "테스트전자 계약 체결")
    with sqlite3.connect(path) as c:
        c.execute(
            "UPDATE news_documents SET published_at='2026-09-04T02:00:00.000000+00:00' WHERE news_document_id=?",
            ("a" * 32,),
        )
    evidence, _ = collect(tmp_path)
    assert len(evidence) == 1
    assert evidence[0]["metadata"]["document_id"] != "a" * 32


def test_disclosure_collection_time_is_not_analysis_time_and_cached_body_is_exact(
    tmp_path,
):
    receipt = "20260901000001"
    path = _dart_store(tmp_path, (receipt, "000001"))
    path.rename(tmp_path / "opendart-bulk.sqlite3")
    with sqlite3.connect(tmp_path / "opendart-bulk.sqlite3") as c:
        c.execute(
            "UPDATE disclosures SET first_observed_at='2026-09-01T03:00:00+00:00'"
        )
    cached = dict(
        id="dart:" + receipt + ":document:hash",
        stock_code="000001",
        source_kind="DISCLOSURE",
        content_scope="original_document",
        body_verified=True,
        observed_at="2026-09-03T03:00:00+00:00",
        text="생산라인 가동을 재개했다",
        source_document_sha256="a" * 64,
    )
    evidence, _ = collect(tmp_path, cached_documents=([cached], "OK"))
    e = evidence[0]
    assert e["metadata"]["published_date"] == "2026-09-01"
    assert e["metadata"]["first_seen_at"].startswith("2026-09-01")
    assert e["available_at"].startswith("2026-09-03T03:")
    assert "가동을 재개" in e["text"]
    assert e["metadata"]["source_document_sha256"] == "a" * 64
    assert e["metadata"]["content_extent"] == "body_excerpt"
    wrong = dict(cached, stock_code="000002")
    evidence, _ = collect(tmp_path, cached_documents=([wrong], "OK"))
    assert evidence[0]["metadata"]["content_extent"] == "listing_only"


def test_future_listing_revision_does_not_rewrite_past_filing(tmp_path):
    from tokmo.facts.opendart_bulk import Disclosure
    from tokmo.records.opendart_disclosures import DisclosureStore

    receipt = "20260901000001"
    path = _dart_store(tmp_path, (receipt, "000001"))
    store = DisclosureStore.open(path)
    later = NOW + timedelta(hours=1)
    run = store.claim("preflight", "20260801", "20260903", later)
    store.observe(
        run,
        Disclosure(
            "00126380",
            "테스트전자",
            "000001",
            "Y",
            "주요사항보고서 정정",
            receipt,
            "filer",
            receipt[:8],
            "정",
        ),
        later,
    )
    store.close()
    path.rename(tmp_path / "opendart-bulk.sqlite3")
    evidence, _ = collect(tmp_path)
    assert "정정" not in evidence[0]["metadata"]["title"]
    future, _ = collect_decision_context(tmp_path, ITEM, as_of=later, universe=UNIVERSE)
    assert "정정" in future[0]["metadata"]["title"]
    assert future[0]["metadata"]["listing_revision_at"] == later.isoformat()


def test_official_cache_does_not_read_model_verdict_or_future_body(tmp_path):
    path = tmp_path / "cache.sqlite3"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE news_decision_inputs (payload TEXT)")
        for index, (at, verified) in enumerate(
            [
                ("2026-09-03T03:00:00Z", True),
                ("2026-09-04T02:00:00Z", True),
                ("2026-09-03T03:00:00Z", False),
            ]
        ):
            c.execute(
                "INSERT INTO news_decision_inputs VALUES (?)",
                (
                    json.dumps(
                        dict(
                            id=str(index),
                            source_kind="DISCLOSURE",
                            content_scope="original_document",
                            body_verified=verified,
                            observed_at=at,
                            text="원문",
                        )
                    ),
                ),
            )
    cached, status = _cached_disclosures(path, NOW)
    assert status == "OK" and [v["id"] for v in cached] == ["0"]


def test_long_source_is_labeled_as_excerpt_and_input_is_bounded(tmp_path):
    store, path = _news_store(tmp_path)
    for index in range(15):
        doc_id = format(index, "x") * 32
        _news_document(store, doc_id, "테스트전자 보고 " + str(index))
    with sqlite3.connect(path) as c:
        c.execute(
            "UPDATE news_documents SET body_text=?,content_state='BODY_AVAILABLE',license_state='PERMITTED'",
            ("첫부분 " + "사업 내용 " * 2000 + " 마지막 정정 내용",),
        )
    evidence, status = collect(tmp_path)
    assert len(evidence) == 12
    assert sum(len(e["text"]) for e in evidence) <= TOTAL_TEXT_LIMIT
    assert all(e["metadata"]["excerpt_truncated"] for e in evidence)
    assert all("마지막 정정 내용" in e["text"] for e in evidence)
    assert status["news_selection"] == "RECENT_SUBSET"


def test_missing_stores_do_not_create_files(tmp_path):
    evidence, status = collect(tmp_path)
    assert not evidence and status["news"] == "STORE_ABSENT"
    assert status["dart"] == "STORE_ABSENT"
    assert list(tmp_path.iterdir()) == []


def test_unavailable_refresh_preserves_cached_evidence_with_explicit_status():
    cached = dict(
        id="local-" + "a" * 16,
        kind="news",
        text="기존 기사 요약",
        available_at=NOW.isoformat(),
    )
    selected = replace_local_context([cached], [], {"news": "STORE_BUSY_OR_LOCKED"})
    assert selected[0]["text"] == cached["text"]
    assert selected[0]["metadata"]["cached_fallback"] is True
    assert selected[0]["available_at"] == cached["available_at"]
    assert replace_local_context([cached], [], {"news": "OK"}) == []


def test_metadata_round_trip_and_changed_provenance_invalidates_reuse():
    data = sample_input()
    data["instruments"][0]["evidence"][0]["metadata"] = {
        "published_date": "2026-09-01",
        "content_extent": "listing_only",
    }
    snapshot = parse_snapshot(data)
    packet = make_packet(snapshot, snapshot.instruments[0], "personal-a", "personal")
    assert packet["evidence"][0]["metadata"]["published_date"] == "2026-09-01"
    previous = dict(status="ANALYZED", packet=packet, analysis=response(packet))
    assert reusable_analysis(packet, previous) is None  # legacy lacks contract/source
    packet.update(
        purpose="ordinary",
        analysis_contract={"schema": "quant-analysis-v1", "model": "synthetic"},
    )
    previous["analysis_source"] = "model"
    assert reusable_analysis(packet, previous)
    changed = deepcopy(packet)
    changed["evidence"][0]["metadata"]["published_date"] = "2026-09-02"
    assert reusable_analysis(changed, previous) is None


def test_shares_outstanding_alone_is_not_financial_statement_coverage():
    coverage = evidence_coverage(
        [], {"shares_outstanding": {"period": "2026-09-09", "value": "104961225"}}
    )
    assert coverage["financial_metrics"] == "NOT_PROVIDED"
    assert coverage["financial_periods"] == []
    assert coverage["other_metric_keys"] == ["shares_outstanding"]


def test_price_context_separates_daily_rebound_from_twenty_day_volume_and_live_quote():
    data = sample_input()
    raw = data["instruments"][0]
    for index, bar in enumerate(raw["bars"]):
        bar["volume"] = "20000" if index < 130 else "10000"
    raw["bars"][-1]["volume"] = "12000"
    # The completed close is above its rising 20-day average; a later quote
    # can be below that average. Neither proves a durable trend reversal.
    raw["price"] = "9000"
    snapshot = parse_snapshot(data)
    context = price_context(snapshot.instruments[0])
    assert context["completed_session"]["return_1"] > 0
    assert context["completed_session"]["volume_return_1"] == D(".2")
    assert context["moving_averages"]["20"]["completed_close_gap"] > 0
    assert context["moving_averages"]["20"]["latest_quote_gap"] < 0
    assert (
        context["feature_periods"]["volume_change_20"]["previous_to"]
        == raw["bars"][-21]["date"]
    )
    packet = make_packet(snapshot, snapshot.instruments[0], "personal-a", "personal")
    assert D(packet["features"]["volume_change_20"]) < 0
    assert (
        analysis_display_values(packet)[
            "price_context.completed_session.volume_return_1"
        ]
        == "20.0%"
    )
    assert packet["input_coverage"]["financial_metrics"] == "NOT_PROVIDED"
