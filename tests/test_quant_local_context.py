"""Focused tests for the read-only local context collectors (Quant contextual lane).

Bounded projection of the layered playbook (Layer-3 trade rationale) and the
local NEWS / OpenDART disclosure stores into ``ContextItemV1`` tuples for the
contextual evidence pack — through the existing canonical seams only, with
missing-store degradation and zero writes (playbook, Ledger, order, account,
Telegram) asserted on real production store shapes. Network-zero: every store
is a tmp_path fixture.
"""

from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import os
import re
import sqlite3

import pytest

from tokmo.facts.opendart_bulk import Disclosure
from tokmo.programs.quant_local_context import (
    MAX_CONTEXT_ITEMS,
    collect_dart_disclosure_items,
    collect_local_news_items,
    collect_playbook_context_items,
)
from tokmo.programs.quant_contextual_analysis import (
    NEWS_MAX_AGE_DAYS, ContextItemV1, build_evidence_pack,
)
from tokmo.programs.quant_telegram_card import CardContextV1
from tokmo.records.external_news import ExternalNewsStore
from tokmo.values.quant_discovery import DecisionCardV1, QuantMarketProfileV1

NOW = datetime(2026, 9, 4, 1, 0, tzinfo=timezone.utc)
SESSION = date(2026, 9, 3)

TOOL = Path(__file__).parents[1] / "tools" / "run_quant_telegram_card.py"


def _tool():
    spec = importlib.util.spec_from_file_location("run_quant_telegram_card", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- fixtures -----------------------------------------------------------------

PLAYBOOK = (
    "---\ninstrument: 테스트전자\n---\n"
    "<!-- HERMES_CURRENT_START -->\n## 1층\n판단: 장기 보유 유지\n<!-- HERMES_CURRENT_END -->\n"
    "<!-- USER_THESIS_START -->\n## 2층\n원문 그대로 둔다\n<!-- USER_THESIS_END -->\n"
    "## 3층 — 매매이력\n"
    '<a id="entry-b"></a>\n### 2026-09-01 — 매도 (2주 @ KRW 3000000)\n\n'
    "- `entry_id`: `b`\n- `source_kind`: `user_trade_reason`\n"
    "- `event_date`: `2026-09-01`\n"
    "- `trade_summary`: `SELL 2 000001 @ KRW 3000000 = 6000000, filled 2026-09-01`\n\n"
    '<!-- USER_RAW_START entry_id="b" -->\n비중 상한 초과로 일부 정리했다\n'
    '<!-- USER_RAW_END entry_id="b" -->\n\n'
    '<a id="entry-a"></a>\n### 2026-08-10 — 매수 (4주 @ KRW 2500000)\n\n'
    "- `entry_id`: `a`\n- `event_date`: `2026-08-10`\n"
    "- `trade_summary`: `BUY 4 000001 @ KRW 2500000 = 10000000, filled 2026-08-10`\n\n"
    '<!-- USER_RAW_START entry_id="a" -->\n조정 구간 분할 매수\n'
    '<!-- USER_RAW_END entry_id="a" -->\n'
)


def _playbook_root(tmp_path, text=PLAYBOOK):
    root = tmp_path / "playbooks"
    path = root / "personal" / "KR" / "000001.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return root


def _news_store(tmp_path):
    path = tmp_path / "news" / "external_news_v4.sqlite3"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)  # umask-proof the private-parent contract
    store = ExternalNewsStore(path)
    store.bootstrap(now=NOW)
    return store, path


def _news_document(store, document_id, title, *, first_seen="2026-09-02T00:00:00.000000+00:00",
                   published="2026-09-02T00:00:00.000000+00:00"):
    connection = sqlite3.connect(store.path)
    connection.execute(
        "INSERT OR IGNORE INTO sources(source_id,family_key,source_native_key,"
        "display_name,canonical_url,created_at,updated_at) VALUES(?, 'NEWS', ?, ?, ?, ?, ?)",
        ("s" * 64, "n-1", "src", "https://src.example", first_seen, first_seen))
    connection.execute(
        "INSERT INTO news_documents(news_document_id,source_id,source_article_id,"
        "canonical_url,canonical_url_hash,title_fingerprint,content_mode,title,"
        "content_state,license_state,evidence_role,alert_eligible,time_interpretation,"
        "first_seen_at,last_seen_at) VALUES(?,?,NULL,?,?,?,'NEWS',?,"
        " 'METADATA_ONLY','UNKNOWN','FACT_DISCOVERY',0,"
        " 'SOURCE_NATIVE',?,?)",
        (document_id, "s" * 64, f"https://src.example/{document_id}",
         "a" * 63 + document_id[0].lower() if document_id[0].isalnum() else "a" * 64,
         "b" * 64, title, first_seen, first_seen))
    connection.commit()
    connection.close()


def _dart_store(tmp_path, *rows):
    path = tmp_path / "private" / "opendart-bulk.sqlite3"
    from tokmo.records.opendart_disclosures import DisclosureStore
    store = DisclosureStore.open(path)
    run = store.claim("preflight", "20260801", "20260903", NOW)
    for receipt, stock in rows:
        store.observe(run, Disclosure("00126380", "테스트전자", stock, "Y",
                                      "주요사항보고서", receipt, "filer", receipt[:8], None), NOW)

    return path


# --- playbook Layer-3 projection ----------------------------------------------

def test_latest_trade_reason_projection(tmp_path):
    items = collect_playbook_context_items(_playbook_root(tmp_path), market="KR",
                                           symbol="000001", scope_dirs=("personal",))
    trade = [item for item in items if item.kind == "trade_reason"]
    assert len(trade) == 1
    item = trade[0]
    assert item.observed_on == "2026-09-01"          # latest entry = first anchor
    assert "매도" in item.summary and "비중 상한 초과로 일부 정리했다" in item.summary
    assert "분할 매수" not in item.summary            # only the latest entry
    assert item.source_label == "플레이북 3층"
    assert type(item) is ContextItemV1
    # structure-only current-layer item carries no raw text
    current = [item for item in items if item.kind == "playbook_current"]
    assert len(current) == 1 and "보유 유지" in current[0].summary
    for value in (item.summary, current[0].summary):
        assert "USER_RAW" not in value and "<!--" not in value and ".md" not in value


def test_playbook_missing_or_absent_degrades_to_empty(tmp_path):
    # no document at all
    assert collect_playbook_context_items(tmp_path / "playbooks", market="KR",
                                          symbol="000001", scope_dirs=("personal",)) == ()
    # unreadable document degrades without raising
    root = tmp_path / "playbooks2"
    path = root / "personal" / "KR" / "000001.md"
    path.parent.mkdir(parents=True)
    path.write_text("not utf-16 decodable as utf-8: \udcff", encoding="utf-8", errors="surrogateescape")
    assert collect_playbook_context_items(root, market="KR", symbol="000001",
                                          scope_dirs=("personal",)) == ()
    # document without Layer 3 has no trade_reason, keeps nothing invented
    root3 = tmp_path / "playbooks3"
    path3 = root3 / "personal" / "KR" / "000001.md"
    path3.parent.mkdir(parents=True)
    path3.write_text("---\n---\nno layers here\n", encoding="utf-8")
    assert collect_playbook_context_items(root3, market="KR", symbol="000001",
                                          scope_dirs=("personal",)) == ()


# --- local NEWS binding ----------------------------------------------------------

def test_local_news_binds_exact_instrument_only(tmp_path):
    store, path = _news_store(tmp_path)
    _news_document(store, "d" * 32, "테스트전자 주요사항보고서 제출")            # exact company
    _news_document(store, "e" * 32, "테스트전자화학 신제품 출시")                  # longer name: no match
    _news_document(store, "f" * 32, "KODEX 미국S&P500 TR 지수 추종")              # ETF token match

    universe = {"000001": ("테스트전자",), "420012": ("KODEX 미국S&P500TR", )}
    items = collect_local_news_items(path, market="KR", symbol="000001",
                                     official_names=("테스트전자",), universe=universe,
                                     completed_session_date=SESSION, max_items=MAX_CONTEXT_ITEMS)
    assert [item.kind for item in items] == ["news"]
    assert items[0].observed_on == "2026-09-02"
    assert items[0].summary == "테스트전자 주요사항보고서 제출"
    assert items[0].source_label == f"로컬 NEWS [{'d' * 32}]"
    etf = collect_local_news_items(path, market="KR", symbol="420012",
                                   official_names=("KODEX 미국S&P500TR",), universe=universe,
                                   completed_session_date=SESSION, max_items=MAX_CONTEXT_ITEMS)
    assert len(etf) == 1 and "KODEX" in etf[0].summary


def test_local_news_window_and_degradation(tmp_path):
    store, path = _news_store(tmp_path)
    _news_document(store, "a" * 32, "테스트전자 최근 보도", first_seen="2026-09-02T00:00:00.000000+00:00")
    _news_document(store, "b" * 32, "테스트전자 오래된 보도", first_seen="2026-08-01T00:00:00.000000+00:00")

    universe = {"000001": ("테스트전자",)}
    items = collect_local_news_items(path, market="KR", symbol="000001",
                                     official_names=("테스트전자",), universe=universe,
                                     completed_session_date=SESSION, max_items=5)
    assert len(items) == 1 and "최근" in items[0].summary  # > 14 days old excluded
    # absent store degrades to empty without raising
    assert collect_local_news_items(tmp_path / "absent.sqlite3", market="KR", symbol="000001",
                                    official_names=("테스트전자",), universe=universe,
                                    completed_session_date=SESSION, max_items=5) == ()


def test_news_revision_availability_is_not_backdated_to_first_collection(tmp_path):
    store,path=_news_store(tmp_path)
    _news_document(store,'a'*32,'테스트전자 실적 수정')
    import sqlite3
    with sqlite3.connect(path) as c:
        c.execute("UPDATE news_documents SET revision_no=2,last_seen_at='2026-09-03T00:00:00.000000+00:00'")
    items=collect_local_news_items(path,market='KR',symbol='000001',official_names=('테스트전자',),
        universe={'000001':('테스트전자',)},completed_session_date=SESSION,max_items=5)
    assert items[0].available_at=='2026-09-03T00:00:00.000000+00:00'
    assert items[0].observed_on=='2026-09-02'


def test_local_news_items_feed_the_evidence_pack_recent_gap(tmp_path):
    from tokmo.programs.quant_telegram_card import CardContextV1
    from tokmo.values.quant_discovery import DecisionCardV1, QuantMarketProfileV1
    store, path = _news_store(tmp_path)
    _news_document(store, "c" * 32, "테스트전자 실적 가이던스 상향")

    universe = {"000001": ("테스트전자",)}
    items = collect_local_news_items(path, market="KR", symbol="000001",
                                     official_names=("테스트전자",), universe=universe,
                                     completed_session_date=SESSION, max_items=5)
    card = DecisionCardV1("000001", QuantMarketProfileV1("KR"), "테스트전자", (), (), None, ())
    pack = build_evidence_pack(card, CardContextV1("000001"),
                               completed_session_date="2026-09-03", context_items=items)
    assert not any(gap["id"] == "gap:news_disclosure" for gap in pack["gaps"])
    assert any(item["id"].startswith("ctx:") and item["kind"] == "news" for item in pack["evidence"])


# --- Stage 2: delegation to the internal evidence query ---------------------------

def test_local_news_routes_through_internal_evidence_query(tmp_path, monkeypatch):
    """Stage-2 wiring: local NEWS projection delegates store access,
    whole-window scanning and alias expansion to
    ``run_internal_evidence_query`` (single call, session-anchored window)
    and keeps only the thin acceptance projection. Alias expansion is
    real: a supplied alias (``하닉``) surfaces ``SK하이닉스`` documents."""
    from tokmo.programs import quant_local_context as qlc
    store, path = _news_store(tmp_path)
    _news_document(store, "9" * 32, "SK하이닉스 자사주 소각 결의")

    calls = []
    original = qlc.run_internal_evidence_query

    def spy(query, **kwargs):
        calls.append(query)
        return original(query, **kwargs)

    monkeypatch.setattr(qlc, "run_internal_evidence_query", spy)
    items = collect_local_news_items(
        path, market="KR", symbol="000660", official_names=("하닉",),
        universe={"000660": ("하닉",)}, completed_session_date=SESSION,
        max_items=MAX_CONTEXT_ITEMS)
    assert len(calls) == 1
    assert calls[0].date_from == SESSION - timedelta(days=NEWS_MAX_AGE_DAYS)
    assert calls[0].date_to == SESSION
    assert calls[0].families == ("news_raw",)
    assert [item.kind for item in items] == ["news"]
    assert items[0].summary == "SK하이닉스 자사주 소각 결의"
    assert items[0].source_label == f"로컬 NEWS [{'9' * 32}]"


def test_dart_disclosures_route_through_internal_evidence_query(
        tmp_path, monkeypatch):
    """Stage-2 wiring: the OpenDART projection delegates stock-code +
    receipt-date filtering to ``run_internal_evidence_query`` (single
    call) and projects the same bounded ``ContextItemV1`` output."""
    from tokmo.programs import quant_local_context as qlc
    path = _dart_store(tmp_path, ("20260901삼전001", "000001"))
    calls = []
    original = qlc.run_internal_evidence_query

    def spy(query, **kwargs):
        calls.append(query)
        return original(query, **kwargs)

    monkeypatch.setattr(qlc, "run_internal_evidence_query", spy)
    items = collect_dart_disclosure_items(
        path, market="KR", symbol="000001", completed_session_date=SESSION)
    assert len(calls) == 1
    assert calls[0].families == ("opendart",)
    assert calls[0].symbols == ("000001",)
    assert calls[0].date_from == SESSION - timedelta(days=NEWS_MAX_AGE_DAYS)
    assert calls[0].date_to == SESSION
    assert len(items) == 1
    assert items[0].kind == "disclosure"
    assert items[0].source_label == "OpenDART 공시 [20260901삼전001]"


# --- M1: exact acceptance must apply BEFORE the Stage-1 result limit ---------------

def test_alias_crowding_exact_acceptance_applies_before_limit(tmp_path):
    """M1 repro: five NEWER loose `하닉` head-attach matches plus one OLDER
    exact `SK하이닉스` article, query alias ``하닉``, max_items=5. The exact
    same-entity acceptance predicate must run before the Stage-1 result
    limit — loose discovery matches may never crowd out the valid article
    (old behaviour: empty result)."""
    store, path = _news_store(tmp_path)
    for index in "12345":
        _news_document(store, index * 32, f"하닉스카페 신메뉴 {index}번 출시",
                       first_seen="2026-09-02T00:00:00.000000+00:00")
    _news_document(store, "9" * 32, "SK하이닉스 정확 기사",
                   first_seen="2026-08-25T00:00:00.000000+00:00")
    items = collect_local_news_items(path, market="KR", symbol="000660",
                                     official_names=("하닉",),
                                     universe={"000660": ("하닉",)},
                                     completed_session_date=SESSION, max_items=5)
    assert [item.summary for item in items] == ["SK하이닉스 정확 기사"]


def test_etf_cross_member_crowding_exact_acceptance_applies_before_limit(tmp_path):
    """M1 ETF repro: five NEWER same-brand other-member headlines plus one
    OLDER unique-attributable headline for the target ETF. Brand-token
    loose matches from another universe member may never prefill the cap
    and hide the valid article."""
    store, path = _news_store(tmp_path)
    for index in "12345":
        _news_document(store, index * 32, f"SOL 배당성장 이벤트 {index}일차",
                       first_seen="2026-09-02T00:00:00.000000+00:00")
    _news_document(store, "9" * 32, "SOL AI반도체 TOP2 순매수 집중",
                   first_seen="2026-08-25T00:00:00.000000+00:00")
    universe = {"374600": ("SOL AI반도체 TOP2",), "329300": ("SOL 배당성장",)}
    items = collect_local_news_items(path, market="KR", symbol="374600",
                                     official_names=("SOL AI반도체 TOP2",),
                                     universe=universe,
                                     completed_session_date=SESSION, max_items=5)
    assert [item.summary for item in items] == ["SOL AI반도체 TOP2 순매수 집중"]


# --- M2: explicit Stage-1 health → Quant status projection -------------------------

def _stage1_result(health, *, truncated=0, documents=()):
    return {"families": {"news_raw": {"store_health": health,
                                      "documents": list(documents),
                                      "counts": {"scanned": 0, "matched": 0,
                                                 "returned": 0,
                                                 "truncated": truncated}}},
            "expansion": {"expanded_terms": []}}


@pytest.mark.parametrize("health,expected", [
    ("OK", "OK"),
    ("STORE_ABSENT", "STORE_UNAVAILABLE"),
    ("STORE_NOT_CONFIGURED", "STORE_UNAVAILABLE"),
    ("STORE_OPEN_FAILED", "STORE_UNAVAILABLE"),
    ("SCHEMA_MISMATCH", "SCHEMA_MISMATCH"),
    ("STORE_BUSY_OR_LOCKED", "BUSY_OR_LOCKED"),
    ("STORE_MALFORMED", "CORRUPT"),
])
def test_news_stage1_health_maps_to_explicit_quant_status(
        tmp_path, monkeypatch, health, expected):
    from tokmo.programs import quant_local_context as qlc
    monkeypatch.setattr(qlc, "run_internal_evidence_query",
                        lambda query, **kwargs: _stage1_result(health))
    status: dict = {}
    items = collect_local_news_items(tmp_path / "n.db", market="KR", symbol="000660",
                                     official_names=("하닉",), universe={"000660": ("하닉",)},
                                     completed_session_date=SESSION, max_items=5,
                                     status_out=status)
    assert status["news"] == expected
    assert items == ()


@pytest.mark.parametrize("health,expected", [
    ("OK", "OK"),
    ("STORE_ABSENT", "STORE_UNAVAILABLE"),
    ("STORE_NOT_CONFIGURED", "STORE_UNAVAILABLE"),
    ("STORE_OPEN_FAILED", "STORE_UNAVAILABLE"),
    ("SCHEMA_MISMATCH", "SCHEMA_MISMATCH"),
    ("STORE_BUSY_OR_LOCKED", "BUSY_OR_LOCKED"),
    ("STORE_MALFORMED", "CORRUPT"),
])
def test_dart_stage1_health_maps_to_explicit_quant_status(
        tmp_path, monkeypatch, health, expected):
    from tokmo.programs import quant_local_context as qlc
    monkeypatch.setattr(
        qlc, "run_internal_evidence_query",
        lambda query, **kwargs: {"families": {"opendart": {
            "store_health": health, "disclosures": [],
            "counts": {"matched": 0, "returned": 0, "truncated": 0}}}})
    status: dict = {}
    items = collect_dart_disclosure_items(tmp_path / "d.db", market="KR", symbol="000660",
                                          completed_session_date=SESSION,
                                          max_items=5, status_out=status)
    assert status["dart"] == expected
    assert items == ()


def test_news_schema_mismatch_on_empty_sqlite_file(tmp_path):
    path = tmp_path / "empty.sqlite3"
    sqlite3.connect(path).close()  # real empty SQLite file: no schema_meta
    status: dict = {}
    items = collect_local_news_items(path, market="KR", symbol="000660",
                                     official_names=("하닉",), universe={"000660": ("하닉",)},
                                     completed_session_date=SESSION, max_items=5,
                                     status_out=status)
    assert status["news"] == "SCHEMA_MISMATCH"
    assert items == ()


def test_news_cap_truncation_reports_cap_exceeded_with_items(tmp_path):
    store, path = _news_store(tmp_path)
    for index in "123456":
        _news_document(store, index * 32, f"테스트전자 보도 {index}",
                       first_seen="2026-09-02T00:00:00.000000+00:00")
    status: dict = {}
    items = collect_local_news_items(path, market="KR", symbol="000001",
                                     official_names=("테스트전자",),
                                     universe={"000001": ("테스트전자",)},
                                     completed_session_date=SESSION, max_items=5,
                                     status_out=status)
    assert status["news"] == "CAP_EXCEEDED"
    assert len(items) == 5 and items[0].summary == "테스트전자 보도 1"


# --- OpenDART disclosure binding --------------------------------------------------

def test_dart_cap_truncation_reports_cap_exceeded_with_items(tmp_path):
    """M2 regression: a truncated OpenDART result keeps the returned
    disclosures and reports CAP_EXCEEDED instead of a plain OK."""
    path = _dart_store(tmp_path, *((f"20260901테스트{index}", "000001")
                                   for index in "123456"))
    status: dict = {}
    items = collect_dart_disclosure_items(path, market="KR", symbol="000001",
                                          completed_session_date=SESSION,
                                          max_items=5, status_out=status)
    assert status["dart"] == "CAP_EXCEEDED"
    assert len(items) == 5


def test_dart_disclosures_within_window_and_symbol(tmp_path):
    path = _dart_store(tmp_path, ("20260901삼전001", "000001"), ("20260810삼전002", "000001"),
                       ("20260901타사003", "005930"))
    items = collect_dart_disclosure_items(path, market="KR", symbol="000001",
                                          completed_session_date=SESSION)
    assert len(items) == 1
    assert items[0].kind == "disclosure" and items[0].observed_on == "2026-09-01"
    assert "주요사항보고서" in items[0].summary and items[0].source_label == "OpenDART 공시 [20260901삼전001]"
    # absent store degrades to empty
    assert collect_dart_disclosure_items(tmp_path / "absent.sqlite3", market="KR", symbol="000001",
                                         completed_session_date=SESSION) == ()


# --- zero writes / provenance ------------------------------------------------------

def test_collectors_never_write_any_source(tmp_path):
    playbook_root = _playbook_root(tmp_path)
    store, news_path = _news_store(tmp_path)
    _news_document(store, "d" * 32, "테스트전자 보도")

    dart_path = _dart_store(tmp_path, ("20260901삼전001", "000001"))
    universe = {"000001": ("테스트전자",)}

    def snapshot():
        snapshot_ = {}
        for file in (news_path, dart_path,
                     *playbook_root.rglob("*.md")):
            stat = file.stat()
            snapshot_[str(file)] = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
        for directory in (news_path.parent, dart_path.parent, playbook_root):
            snapshot_[str(directory) + ":entries"] = sorted(p.name for p in directory.iterdir())
        return snapshot_

    before = snapshot()
    collect_playbook_context_items(playbook_root, market="KR", symbol="000001",
                                   scope_dirs=("personal",))
    collect_local_news_items(news_path, market="KR", symbol="000001",
                             official_names=("테스트전자",), universe=universe,
                             completed_session_date=SESSION, max_items=5)
    collect_dart_disclosure_items(dart_path, market="KR", symbol="000001",
                                  completed_session_date=SESSION)
    assert snapshot() == before


def test_module_has_no_write_or_telegram_or_order_surface():
    source = (Path(__file__).parents[1] / "src" / "tokmo" / "programs"
              / "quant_local_context.py").read_text(encoding="utf-8")
    for banned in ("telegram", "send_dialogue", "order_scan", "trade_ledger_store",
                   "LedgerWriter", "os.remove", "unlink", "INSERT", "UPDATE ", "DELETE "):
        assert banned not in source, banned


def test_tool_wiring_builds_local_context_readonly(tmp_path):
    tool = _tool()
    playbook_root = _playbook_root(tmp_path)
    store, news_path = _news_store(tmp_path)
    _news_document(store, "d" * 32, "테스트전자 실적 발표")

    dart_path = _dart_store(tmp_path, ("20260901삼전001", "000001"))
    cards = (("KR", "000001", "테스트전자"),)
    universe = {"000001": ("테스트전자",)}
    items = tool.build_local_context_items(cards, universe=universe, completed_session_date=SESSION,
                                           playbook_root=playbook_root, news_path=news_path,
                                           dart_path=dart_path)
    assert set(items) == {("KR", "000001")}
    kinds = {item.kind for item in items[("KR", "000001")]}
    assert kinds == {"trade_reason", "playbook_current", "news", "disclosure"}
    # missing stores degrade to per-source availability markers, never raise
    items2 = tool.build_local_context_items(cards, universe=universe, completed_session_date=SESSION,
                                            playbook_root=tmp_path / "none", news_path=tmp_path / "none.db",
                                            dart_path=tmp_path / "none2.db")
    markers = items2[("KR", "000001")]
    assert {item.kind for item in markers} == {"source_unavailable"}
    assert {item.summary for item in markers} == {"news:STORE_UNAVAILABLE", "dart:STORE_UNAVAILABLE"}
    # valid zero-result stays visibly distinct: no marker when stores read OK
    empty_store, empty_news = _news_store(tmp_path / "empty")
    empty_dart = _dart_store(tmp_path / "empty2")
    items3 = tool.build_local_context_items(cards, universe=universe, completed_session_date=SESSION,
                                            playbook_root=tmp_path / "none", news_path=empty_news,
                                            dart_path=empty_dart)
    assert items3 == {("KR", "000001"): ()}


def test_local_context_render_operating_path_invocation(tmp_path, monkeypatch):
    """B1: render() with an injected analysis callable really invokes
    build_local_context_items on the located cards/session and feeds the
    items to build_analyses — no-send, no model/network by default."""
    import test_quant_telegram_card as card_tests
    tool = _tool()
    playbook_root = _playbook_root(tmp_path)
    store, news_path = _news_store(tmp_path)
    _news_document(store, "a" * 32, "테스트전자 실적 가이던스 상향")
    dart_path = _dart_store(tmp_path, ("20260901삼전001", "000001"))

    calls: dict = {"built": None}
    original = tool.build_local_context_items

    def spy(cards, **kwargs):
        calls["built"] = (tuple(cards), str(kwargs["completed_session_date"]))
        return original(cards, **kwargs)

    monkeypatch.setattr(tool, "build_local_context_items", spy)
    seen_packs = []

    def analysis(pack):
        seen_packs.append(pack)
        return None  # rejected output keeps the render no-delivery and cheap

    root = tmp_path / "reports"
    card_tests._store_report(root, "KR", (card_tests._card(
        "000001", "KR", "테스트전자", card_tests.VALUES_HOT, card_tests.ZONE),), "a")
    messages, summary = tool.render(
        root, now=NOW, analysis=analysis,
        holdings_path=tmp_path / "no_h.json", quotes_path=tmp_path / "no_q.sqlite3",
        playbook_root=playbook_root, watchlist_root=tmp_path / "watchlists",
        native_refs_path=tmp_path / "no_r.sqlite3",
        news_path=news_path, dart_path=dart_path)
    assert summary["evidence"]["analysis"]["enabled"] is True
    assert summary["evidence"]["local_context"] == {"cards": 1, "items": 1}
    # the located session (not "now") drove the binding
    assert calls["built"][0] == (("KR", "000001", "테스트전자"),)
    assert calls["built"][1] == "2026-09-02"
    kinds = [item["kind"] for item in seen_packs[0]["evidence"] if item["id"].startswith("ctx:")]
    assert "news" in kinds
    assert not any(gap["id"] == "gap:news_disclosure" for gap in seen_packs[0]["gaps"])


def test_source_unavailable_marker_qualifies_news_gap():
    from tokmo.programs.quant_contextual_analysis import ContextItemV1, build_evidence_pack
    from tokmo.programs.quant_telegram_card import CardContextV1
    from tokmo.values.quant_discovery import DecisionCardV1, QuantMarketProfileV1
    card = DecisionCardV1("000001", QuantMarketProfileV1("KR"), "테스트전자", (), (), None, ())
    pack = build_evidence_pack(card, CardContextV1("000001"),
                               completed_session_date="2026-09-03",
                               context_items=(ContextItemV1("source_unavailable", "news:STORE_UNAVAILABLE",
                                                             source_label="로컬 스토어 가용성"),))
    gap = next(gap for gap in pack["gaps"] if gap["id"] == "gap:news_disclosure")
    assert "확인 불가" in gap["detail"] and "STORE_UNAVAILABLE" in gap["detail"]


# --- M3: Layer-1 playbook summary numeric scrub ---------------------------------------

def test_layer1_summary_scrubs_dates_prices_quantities_amounts(tmp_path):
    playbook = (
        "---\ninstrument: 테스트전자\n---\n"
        "<!-- HERMES_CURRENT_START -->\n## 1층\n"
        "판단: 2026-09-01 기준 목표가 85000원, 4주 추가 매수 후 평단 82000원, 총 1200만원까지"
        " 유지하고 RSI 70 이상에서 절반 정리\n"
        "<!-- HERMES_CURRENT_END -->\n"
        "## 3층 — 매매이력\n"
        '<a id="entry-a"></a>\n### 2026-08-10 — 매수\n\n'
        "- `entry_id`: `a`\n- `event_date`: `2026-08-10`\n"
        "- `trade_summary`: `BUY 4 000001 @ KRW 2500000, filled 2026-08-10`\n\n"
        '<!-- USER_RAW_START entry_id="a" -->\n2026-08-10에 4주를 2500000원에 10000000원어치 샀다\n'
        '<!-- USER_RAW_END entry_id="a" -->\n'
    )
    items = collect_playbook_context_items(_playbook_root(tmp_path, playbook), market="KR",
                                           symbol="000001", scope_dirs=("personal",))
    for item in items:
        assert item.kind in ("playbook_current", "trade_reason")
        assert not re.search(r"\d", item.summary), item.summary
        assert "판단" in items[0].summary or "매수" in item.summary  # prose preserved
    current = next(item for item in items if item.kind == "playbook_current")
    for gone in ("2026", "85000", "82000", "1200", "70"):
        assert gone not in current.summary
    assert "판단:" in current.summary and "유지" in current.summary  # useful prose kept
    trade = next(item for item in items if item.kind == "trade_reason")
    assert not re.search(r"\d", trade.summary) and "샀다" in trade.summary


def test_pack_rejects_digit_carrying_playbook_context_summaries():
    from tokmo.programs.quant_contextual_analysis import ContextItemV1, build_evidence_pack
    from tokmo.programs.quant_telegram_card import CardContextV1
    from tokmo.values.quant_discovery import DecisionCardV1, QuantMarketProfileV1
    card = DecisionCardV1("000001", QuantMarketProfileV1("KR"), "테스트전자", (), (), None, ())
    for kind in ("playbook_current", "user_plan", "trade_reason"):
        with pytest.raises(ValueError, match="context_item_summary"):
            build_evidence_pack(card, CardContextV1("000001"),
                                completed_session_date="2026-09-03",
                                context_items=(ContextItemV1(kind, f"{kind} 1200만원까지 유지"),))
    # news/disclosure titles may keep their own digits (public text)
    pack = build_evidence_pack(card, CardContextV1("000001"),
                               completed_session_date="2026-09-03",
                               context_items=(ContextItemV1("news", "9월 1일 실적 발표"),))
    assert any(item["kind"] == "news" for item in pack["evidence"])


# --- m2: bounded provenance on source_label ------------------------------------------

def test_news_and_dart_items_carry_traceable_bounded_provenance(tmp_path):
    store, news_path = _news_store(tmp_path)
    document_id = "d" * 32
    _news_document(store, document_id, "테스트전자 실적 발표")
    items = collect_local_news_items(news_path, market="KR", symbol="000001",
                                     official_names=("테스트전자",), universe={"000001": ("테스트전자",)},
                                     completed_session_date=SESSION)
    assert len(items) == 1
    assert items[0].source_label == f"로컬 NEWS [{document_id}]"
    assert len(items[0].source_label) <= len("로컬 NEWS [") + 48 + 1

    dart_path = _dart_store(tmp_path, ("20260901삼전001", "000001"))
    disclosures = collect_dart_disclosure_items(dart_path, market="KR", symbol="000001",
                                                completed_session_date=SESSION)
    assert len(disclosures) == 1
    assert disclosures[0].source_label == "OpenDART 공시 [20260901삼전001]"

    # provenance reaches the pack as metadata (source), never the rendered label
    from tokmo.programs.quant_telegram_card import CardContextV1
    from tokmo.values.quant_discovery import DecisionCardV1, QuantMarketProfileV1
    from test_quant_telegram_card import _card, VALUES_CALM
    card = _card("000001", "KR", "테스트전자", VALUES_CALM)
    pack = build_evidence_pack(card, CardContextV1("000001"),
                               completed_session_date=str(SESSION), context_items=items + disclosures)
    ctx = {item["kind"]: item for item in pack["evidence"] if item["id"].startswith("ctx:")}
    assert ctx["news"]["source"] == f"로컬 NEWS [{document_id}]"
    assert ctx["disclosure"]["source"] == "OpenDART 공시 [20260901삼전001]"
    # user-facing label keeps no identifier digits
    assert ctx["news"]["label"] == "맥락1(로컬 NEWS)"
    assert "맥락" in ctx["disclosure"]["label"] and document_id not in ctx["disclosure"]["label"]
    from tokmo.programs.quant_contextual_analysis import validate_analysis
    labels = {item["id"]: item["label"] for item in pack["evidence"]}
    ids = sorted(item["id"] for item in pack["evidence"] if item["id"].startswith("ctx:"))
    output = {"symbol": "000001", "summary": "로컬 근거가 최근 사건과 이어진다",
              "conditional_order_assessment": None,
              "evidence_relations": [{"statement": "최근 보도가 판단 맥락과 이어진다", "evidence_ids": ids[:2]}],
              "alternatives": [{"statement": "보류도 선택지다", "evidence_ids": ids[:1]}],
              "counterevidence": [{"statement": "보도가 사실과 다를 수 있다", "evidence_ids": ids[:1]}],
              "assumptions": [], "falsifiers": [{"statement": "정정 보도가 나오면 바뀐다", "evidence_ids": ids[:1]}],
              "change_conditions": [{"statement": "장기 수익률이 양수가 아니면 재검토한다", "evidence_ids": ["f:ret_120"]}],
              "confidence": {"statement": "근거가 얇다", "level": "낮음", "evidence_ids": ids[:1]}}
    analysis = validate_analysis(output, pack)
    from tokmo.programs.quant_telegram_card import _actionable_analysis_lines
    rendered = "\n".join(_actionable_analysis_lines(analysis))
    assert document_id not in rendered and "20260901" not in rendered
    assert "로컬 NEWS" in rendered  # label still cites the source


# --- M2: per-market completed session binding ---------------------------------------

def test_build_local_context_items_accepts_per_card_session_mapping(tmp_path):
    tool = _tool()
    playbook_root = _playbook_root(tmp_path)
    store, news_path = _news_store(tmp_path)
    _news_document(store, "a" * 32, "테스트전자 최근 보도", first_seen="2026-08-30T00:00:00.000000+00:00")
    cards = (("KR", "000001", "테스트전자"), ("US", "NVDA", "엔비디아"))
    universe = {"000001": ("테스트전자",), "NVDA": ("엔비디아",)}
    # scalar stays supported: single date applied to every card
    scalar = tool.build_local_context_items(cards, universe=universe,
                                            completed_session_date=SESSION,
                                            playbook_root=playbook_root, news_path=news_path,
                                            dart_path=tmp_path / "none.db")
    assert scalar[("KR", "000001")][0].kind == "trade_reason"
    # per-card mapping: the KR card's own session drives its NEWS window
    per_card = tool.build_local_context_items(
        cards, universe=universe,
        completed_session_date={("KR", "000001"): SESSION, ("US", "NVDA"): date(2026, 9, 2)},
        playbook_root=playbook_root, news_path=news_path, dart_path=tmp_path / "none.db")
    assert per_card[("KR", "000001")] == scalar[("KR", "000001")]
    # a different KR session narrows the window past the document (window is
    # a floor: session 2026-09-14 → window_from 2026-08-31 excludes the
    # 2026-08-30 doc), proving the mapping — not a shared value — drove KR
    shifted = tool.build_local_context_items(
        cards, universe=universe,
        completed_session_date={("KR", "000001"): date(2026, 9, 14), ("US", "NVDA"): SESSION},
        playbook_root=playbook_root, news_path=news_path, dart_path=tmp_path / "none.db")
    assert shifted[("KR", "000001")] != per_card[("KR", "000001")]
    assert any(item.kind == "news" for item in per_card[("KR", "000001")])
    assert not any(item.kind == "news" for item in shifted[("KR", "000001")])


def test_render_binds_each_market_to_its_own_completed_session(tmp_path, monkeypatch):
    """M2: with two located markets on different sessions, render() must bind
    each market's cards with that market's own completed_session_date —
    never a collapsed shared minimum."""
    import test_quant_telegram_card as card_tests
    tool = _tool()
    playbook_root = _playbook_root(tmp_path)
    store, news_path = _news_store(tmp_path)
    # doc older than the KR session's 14-day window (window_from 2026-08-19):
    # excluded under the correct KR binding, but it would leak in if KR were
    # bound to the earlier US session (min collapse, window_from 2026-08-04)
    _news_document(store, "a" * 32, "테스트전자 오래된 보도",
                   first_seen="2026-08-10T00:00:00.000000+00:00")

    sessions: dict = {}
    original = tool.build_local_context_items

    def spy(cards, **kwargs):
        sessions[str(kwargs["completed_session_date"])] = tuple(cards)
        return original(cards, **kwargs)

    monkeypatch.setattr(tool, "build_local_context_items", spy)
    seen_packs = []

    def analysis(pack):
        seen_packs.append(pack)
        return None

    root = tmp_path / "reports"
    card_tests._store_report(root, "KR", (card_tests._card(
        "000001", "KR", "테스트전자", card_tests.VALUES_HOT, card_tests.ZONE),), "a")
    from tokmo.records.quant_reports import QuantReportStore
    from tokmo.values.quant_discovery import (
        QuantMarketProfileV1, QuantProductKind, QuantRunIdentityV1, QuantRunReportV1,
    )
    us_report = QuantRunReportV1(
        QuantRunIdentityV1(QuantProductKind.MARKET_DISCOVERY, QuantMarketProfileV1("US"),
                           date(2026, 8, 18), None, "quant-profile-v1", None,
                           "quant-report-v1", "b" * 64),
        (card_tests._card("NVDA", "US", "엔비디아", card_tests.VALUES_CALM),), (), (("RANKINGS", 1),))
    us_store = QuantReportStore(root)
    us_digest = us_store.append_if_absent(us_report)
    us_store.finish(us_report.identity, "SUCCEEDED_WITH_CARDS", us_report.operation_counts, us_digest)

    messages, summary = tool.render(
        root, now=NOW, analysis=analysis,
        holdings_path=tmp_path / "no_h.json", quotes_path=tmp_path / "no_q.sqlite3",
        playbook_root=playbook_root, watchlist_root=tmp_path / "watchlists",
        native_refs_path=tmp_path / "no_r.sqlite3",
        news_path=news_path, dart_path=tmp_path / "none.db")
    assert summary["evidence"]["local_context"] == {"cards": 2, "items": 2}
    # each market's own session drove its own binding — no shared minimum
    assert set(sessions) == {"2026-09-02", "2026-08-18"}
    assert sessions["2026-09-02"] == (("KR", "000001", "테스트전자"),)
    assert sessions["2026-08-18"] == (("US", "NVDA", "엔비디아"),)
    kr_pack = next(pack for pack in seen_packs if pack["symbol"] == "000001")
    assert not any(item["id"].startswith("ctx:") and item["kind"] == "news"
                   and "오래된 보도" in item["summary"] for item in kr_pack["evidence"])
