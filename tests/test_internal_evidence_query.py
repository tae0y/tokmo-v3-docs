"""Internal-first evidence Stage 1: bounded read-only local lookup.

Plan/SDS 2026-09-04 (internal-first evidence Stage 1). Every store is a
tmp_path fake built through the canonical writers; the autouse conftest
sandbox already forbids sockets, and the static seals below pin that the
program and CLI carry zero network/model/Telegram/order/ledger surface.
"""
from datetime import datetime, date, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from tokmo.programs.external_news_storage import (
    bootstrap_external_news, save_external_news,
)
import tokmo.programs.internal_evidence_query as ieq
from tokmo.programs.internal_evidence_query import (
    CONTRACT_ID, MAX_LIMIT, MAX_SPAN_DAYS, SUPPORTED_FAMILIES,
    EvidenceQueryV1, classify_url_kind, expand_query_terms,
    run_internal_evidence_query,
)
from tokmo.programs.news_topic_discovery import _PUBLIC_ALIASES
from tokmo.records.news_episode_store import NewsEpisodeStore
from tokmo.records.opendart_disclosures import DisclosureStore
from tokmo.values.external_news import NewsWrite
from tokmo.values.news_discovery import load_public_aliases
from tokmo.values.news_event import Evidence

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "tools" / "run_internal_evidence_query.py"
PROGRAM = ROOT / "src" / "tokmo" / "programs" / "internal_evidence_query.py"
NOW = datetime(2026, 9, 3, 6, 0, tzinfo=timezone.utc)
FROM, TO = date(2026, 9, 1), date(2026, 9, 3)


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "run_internal_evidence_query_tool", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _news_db(tmp_path, rows, name="external.sqlite3"):
    """rows: (url, title, summary, published_at[, publisher, site])."""
    path = tmp_path / "news" / name
    bootstrap_external_news(path, now=NOW - timedelta(days=30))
    ids = {}
    for row in rows:
        url, title, summary, published = row[:4]
        publisher = row[4] if len(row) > 4 else None
        site = row[5] if len(row) > 5 else None
        receipt = save_external_news(path, NewsWrite(
            canonical_url=url, title=title, summary_text=summary,
            publisher_name=publisher, publisher_site_url=site,
            observed_at=published, published_at=published))
        ids[title] = receipt.news_document_id
    return path, ids


def _episode_db(tmp_path, *, document_ids=(), entity_key="ent:sk-hynix",
                candidate=True):
    path = tmp_path / "episodes" / "news.sqlite"
    store = NewsEpisodeStore(path)
    episode = None
    if document_ids:
        episode = store.upsert_with_cause(
            entity_key, tuple(Evidence(doc, "CLAIM", None)
                              for doc in document_ids),
            NOW, cause_key=entity_key)
    if candidate:
        run = ("ndr-run1", (NOW - timedelta(days=3)).isoformat(),
               NOW.isoformat(), "c" * 64, "p" * 64, 3, 3, 0, "OK",
               NOW.isoformat(), NOW.isoformat())
        payload = json.dumps({"v": 2, "document_ids": list(document_ids)})
        store.record_discovery_run(run, [(
            "ndc-cand1", "ndr-run1", "DIRECT_HOLDING", entity_key,
            "sk하이닉스", 3, 2, payload, "SURFACED",
            episode.episode_id if episode else None,
            NOW.isoformat(), NOW.isoformat())], now=NOW)
    store.close()
    return path, (episode.episode_id if episode else None)


def _opendart_db(tmp_path, rows):
    path = tmp_path / "opendart" / "opendart-bulk.sqlite3"
    store = DisclosureStore.open(path)
    for rcept_no, stock_code, corp_name, report_nm, rcept_dt in rows:
        store.connection.execute(
            "insert into disclosures values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rcept_no, "C" + rcept_no[-6:], corp_name, stock_code, "Y",
             report_nm, corp_name, rcept_dt, None, "{}", "h" * 64,
             NOW.isoformat(), "run-1"))
    store.connection.commit()
    store.close()
    return path


def _query(**overrides):
    values = {"date_from": FROM, "date_to": TO, "terms": (), "symbols": (),
              "identities": (), "families": SUPPORTED_FAMILIES, "limit": 20}
    values.update(overrides)
    return EvidenceQueryV1(**values)


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")
                  if not p.name.endswith(("-wal", "-shm")))


def _walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


HYNIX_ROWS = (
    ("https://news.example/hynix-1", "SK하이닉스 자사주 소각 결의",
     "SK하이닉스[000660]가 자사주 소각을 결의했다", NOW - timedelta(days=1),
     "연합뉴스", "https://www.yna.co.kr"),
    ("https://news.example/other-1", "지하닉스 유통망 점검",
     "지방 유통 점검", NOW - timedelta(days=1)),
    ("https://news.example/other-2", "삼성전자 신규 팹 착공",
     None, NOW - timedelta(days=1)),
)


# ---------------------------------------------------------------- A) aliases

def test_public_catalog_resolves_hynix_variants_and_rejects_compounds():
    aliases, _ = load_public_aliases(
        ROOT / "src" / "tokmo" / "values" / "news_discovery_aliases.json")
    assert {"SK하이닉스", "하이닉스", "하닉"} <= set(aliases["ent:sk-hynix"])
    for term in ("SK하이닉스", "하이닉스", "하닉", "SK 하이닉스"):
        expansion = expand_query_terms(terms=(term,))
        assert expansion["entity_keys"] == ["ent:sk-hynix"], term
        assert "하닉" in expansion["expanded_terms"]
        assert "sk하이닉스" in expansion["expanded_terms"]
    for compound in ("지하닉스", "하이닉공업", "하닉전자"):
        expansion = expand_query_terms(terms=(compound,))
        assert expansion["entity_keys"] == [], compound
        assert expansion["expanded_terms"] == [compound]


def test_symbol_with_official_identity_merges_into_canonical_entity():
    expansion = expand_query_terms(
        symbols=("000660",), identities=(("000660", ("SK하이닉스",)),))
    assert expansion["entity_keys"] == ["ent:sk-hynix"]
    assert "000660" in expansion["expanded_terms"]
    assert "하닉" in expansion["expanded_terms"]
    assert not any(key.endswith("000660") for key in expansion["entity_keys"])
    assert expansion["ungrounded_inputs"] == []


def test_duplicate_alias_keeps_symbol_ambiguous_and_ungrounded(
        tmp_path, monkeypatch):
    """Review M2: a supplied official name equal to aliases of several
    entities never grounds its symbol and never expands. The production
    catalog is untouched — the collision is injected on the module only."""
    duplicated = dict(ieq._PUBLIC_ALIASES)
    duplicated["ent:dual-a"] = ("듀얼", "dual alpha")
    duplicated["ent:dual-b"] = ("듀얼", "dual beta")
    monkeypatch.setattr(ieq, "_PUBLIC_ALIASES", duplicated)
    expansion = expand_query_terms(
        symbols=("DUAL",), identities=(("DUAL", ("듀얼",)),))
    assert expansion["entity_keys"] == []
    assert expansion["ambiguous_inputs"] == ["듀얼"]
    assert expansion["ungrounded_inputs"] == ["DUAL"]
    assert expansion["expanded_terms"] == ["dual", "듀얼"]
    news, _ = _news_db(tmp_path, (
        ("https://d.example/1", "듀얼 신제품 공개", None, NOW - timedelta(days=1)),
        ("https://d.example/2", "dual alpha expands", None,
         NOW - timedelta(days=1)),
    ))
    result = run_internal_evidence_query(
        _query(symbols=("DUAL",), identities=(("DUAL", ("듀얼",)),),
               families=("news_raw",)), news_db=news)
    # Literal search is preserved for both the symbol and the supplied name;
    # nothing from either colliding entity's other aliases is pulled in.
    assert sorted(doc["title"] for doc in
                  result["families"]["news_raw"]["documents"]) == [
        "dual alpha expands", "듀얼 신제품 공개"]
    assert all(set(doc["matched_terms"]) <= {"dual", "듀얼"}
               for doc in result["families"]["news_raw"]["documents"])
    codes = {(item["code"], item["detail"]) for item in result["limitations"]}
    assert {("ALIAS_AMBIGUOUS", "듀얼"), ("IDENTITY_UNGROUNDED", "DUAL")} <= codes
    assert "ent:dual-a" not in _PUBLIC_ALIASES


# --------------------------------------------------------- B) raw discovery

def test_whole_window_is_scanned_across_fetch_batches(tmp_path, monkeypatch):
    """Review M1: no semantic scan cap. Batching is mechanical; every
    in-window match is counted and the newest ``limit`` rows return."""
    monkeypatch.setattr(ieq, "FETCH_BATCH_ROWS", 2)
    rows = tuple((f"https://b.example/{i}", f"하이닉스 배치 {i:02d}", None,
                  NOW - timedelta(hours=i)) for i in range(7))
    rows += (("https://b.example/x", "무관 기사", None, NOW - timedelta(hours=3)),)
    news, _ = _news_db(tmp_path, rows)
    result = run_internal_evidence_query(
        _query(terms=("하이닉스",), families=("news_raw",), limit=3),
        news_db=news)
    family = result["families"]["news_raw"]
    assert family["store_health"] == "OK"
    assert family["counts"] == {"scanned": 8, "matched": 7, "returned": 3,
                                "truncated": 4}
    assert [doc["title"] for doc in family["documents"]] == [
        "하이닉스 배치 00", "하이닉스 배치 01", "하이닉스 배치 02"]
    assert {item["code"] for item in result["limitations"]} == {
        "RESULT_CAP_TRUNCATED"}
    assert not any("PARTIAL" in str(value) for value in _walk_keys(result))

def test_query_hynix_discovers_stored_sk_hynix_article_boundary_safe(tmp_path):
    news, ids = _news_db(tmp_path, HYNIX_ROWS)
    result = run_internal_evidence_query(
        _query(terms=("하이닉스",), families=("news_raw",)), news_db=news)
    family = result["families"]["news_raw"]
    assert family["store_health"] == "OK"
    titles = [doc["title"] for doc in family["documents"]]
    assert titles == ["SK하이닉스 자사주 소각 결의"]
    doc = family["documents"][0]
    assert doc["document_id"] == ids["SK하이닉스 자사주 소각 결의"]
    assert doc["url"] == "https://news.example/hynix-1"
    assert doc["publisher_name"] == "연합뉴스"
    assert doc["publisher_site_url"] == "https://www.yna.co.kr"
    assert doc["url_kind"] == "DIRECT"
    assert "sk하이닉스" in doc["matched_terms"]
    assert "000660" not in doc["matched_terms"]
    assert family["counts"] == {"scanned": 3, "matched": 1, "returned": 1,
                                "truncated": 0}
    # 하닉 as the query head never reaches the 지하닉스 compound.
    result = run_internal_evidence_query(
        _query(terms=("하닉",), families=("news_raw",)), news_db=news)
    assert [doc["title"] for doc in
            result["families"]["news_raw"]["documents"]] == [
                "SK하이닉스 자사주 소각 결의"]


def test_avgo_discovers_broadcom_only_with_grounded_identity(tmp_path):
    news, _ = _news_db(tmp_path, (
        ("https://news.example/avgo-1", "Broadcom raises AI revenue outlook",
         None, NOW - timedelta(days=1)),
        ("https://news.example/avgo-2", "브로드컴 HBM 수요 급증 전망",
         None, NOW - timedelta(days=1)),
        ("https://news.example/avgo-3", "AVGO shares jump after earnings",
         None, NOW - timedelta(days=1)),
    ))
    catalog_before = json.dumps(_PUBLIC_ALIASES, sort_keys=True)
    bare = run_internal_evidence_query(
        _query(symbols=("AVGO",), families=("news_raw",)), news_db=news)
    assert [doc["title"] for doc in bare["families"]["news_raw"]["documents"]] \
        == ["AVGO shares jump after earnings"]
    assert bare["expansion"]["ungrounded_inputs"] == ["AVGO"]
    assert any(item["code"] == "IDENTITY_UNGROUNDED"
               and item["detail"] == "AVGO" for item in bare["limitations"])
    grounded = run_internal_evidence_query(
        _query(symbols=("AVGO",),
               identities=(("AVGO", ("Broadcom", "브로드컴")),),
               families=("news_raw",)), news_db=news)
    titles = sorted(doc["title"] for doc in
                    grounded["families"]["news_raw"]["documents"])
    assert titles == ["AVGO shares jump after earnings",
                      "Broadcom raises AI revenue outlook",
                      "브로드컴 HBM 수요 급증 전망"]
    assert grounded["expansion"]["entity_keys"] == []
    assert grounded["expansion"]["ungrounded_inputs"] == []
    assert json.dumps(_PUBLIC_ALIASES, sort_keys=True) == catalog_before
    assert "broadcom" not in json.dumps(_PUBLIC_ALIASES).casefold()


def test_date_boundaries_and_result_caps_are_deterministic(tmp_path):
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    end_exclusive = datetime(2026, 9, 4, tzinfo=timezone.utc)
    news, _ = _news_db(tmp_path, (
        ("https://t.example/a", "하이닉스 경계 A", None, start),
        ("https://t.example/b", "하이닉스 경계 B", None,
         start - timedelta(microseconds=1)),
        ("https://t.example/c", "하이닉스 경계 C", None,
         end_exclusive - timedelta(microseconds=1)),
        ("https://t.example/d", "하이닉스 경계 D", None, end_exclusive),
    ))
    first = run_internal_evidence_query(
        _query(terms=("하이닉스",), families=("news_raw",)), news_db=news)
    family = first["families"]["news_raw"]
    assert [doc["title"] for doc in family["documents"]] == [
        "하이닉스 경계 C", "하이닉스 경계 A"]
    assert family["counts"]["scanned"] == 2
    capped = run_internal_evidence_query(
        _query(terms=("하이닉스",), families=("news_raw",), limit=1),
        news_db=news)
    counts = capped["families"]["news_raw"]["counts"]
    assert counts == {"scanned": 2, "matched": 2, "returned": 1,
                      "truncated": 1}
    assert any(item["code"] == "RESULT_CAP_TRUNCATED"
               for item in capped["limitations"])
    again = run_internal_evidence_query(
        _query(terms=("하이닉스",), families=("news_raw",)), news_db=news)
    assert json.dumps(first, sort_keys=True) == json.dumps(again, sort_keys=True)
    with pytest.raises(ValueError, match="DATE_RANGE_INVERTED"):
        _query(terms=("x",), date_from=TO, date_to=FROM)
    with pytest.raises(ValueError, match="DATE_SPAN_EXCEEDED"):
        _query(terms=("x",), date_from=FROM,
               date_to=FROM + timedelta(days=MAX_SPAN_DAYS + 1))
    with pytest.raises(ValueError, match="LIMIT_OUT_OF_RANGE"):
        _query(terms=("x",), limit=0)
    with pytest.raises(ValueError, match="LIMIT_OUT_OF_RANGE"):
        _query(terms=("x",), limit=MAX_LIMIT + 1)
    with pytest.raises(ValueError, match="NO_QUERY_INPUT"):
        _query()


# --------------------------------------------- B) episode metadata secondary

def test_raw_news_returned_without_episode_metadata_then_linked_secondary(
        tmp_path):
    news, ids = _news_db(tmp_path, HYNIX_ROWS)
    doc_id = ids["SK하이닉스 자사주 소각 결의"]
    absent = run_internal_evidence_query(
        _query(terms=("하닉",)), news_db=news)
    assert absent["families"]["news_raw"]["documents"][0]["document_id"] == doc_id
    episode_family = absent["families"]["news_episode"]
    assert episode_family["role"] == "SECONDARY"
    assert episode_family["store_health"] == "STORE_NOT_CONFIGURED"
    assert episode_family["linked_episodes"] == []
    assert any(item["family"] == "news_episode"
               and item["code"] == "STORE_NOT_CONFIGURED"
               for item in absent["limitations"])
    assert any("news_episode" in gap for gap in absent["residual_gaps"])

    episodes, episode_id = _episode_db(tmp_path, document_ids=(doc_id,))
    linked = run_internal_evidence_query(
        _query(terms=("하닉",)), news_db=news, episode_db=episodes)
    family = linked["families"]["news_episode"]
    assert family["store_health"] == "OK" and family["role"] == "SECONDARY"
    assert [item["episode_id"] for item in family["linked_episodes"]] == [
        episode_id]
    episode = family["linked_episodes"][0]
    assert episode["document_ids"] == [doc_id]
    assert episode["state"] == "INVESTIGATING"
    assert episode["claim_statuses"] == ["CLAIM"]
    assert [c["candidate_id"] for c in family["discovery_candidates"]] == [
        "ndc-cand1"]
    assert family["discovery_candidates"][0]["entity_key"] == "ent:sk-hynix"
    # Raw documents are unchanged by the presence of metadata.
    assert linked["families"]["news_raw"] == absent["families"]["news_raw"]


# ------------------------------------------- B) typed store degradation

def test_missing_malformed_locked_stores_degrade_without_files(tmp_path):
    news, _ = _news_db(tmp_path, HYNIX_ROWS)
    opendart = _opendart_db(tmp_path, (
        ("20260902000111", "000660", "에스케이하이닉스",
         "주요사항보고서(자기주식소각결정)", "20260902"),))
    missing = tmp_path / "nowhere" / "news.sqlite"
    garbage = tmp_path / "garbage.sqlite3"
    garbage.write_bytes(b"this is not a database at all" * 40)
    before = _tree(tmp_path)
    wrong_schema = opendart  # a real sqlite file with the wrong tables
    result = run_internal_evidence_query(
        _query(terms=("하닉",), symbols=("000660",)),
        news_db=missing, episode_db=garbage, opendart_db=opendart)
    assert result["families"]["news_raw"]["store_health"] == "STORE_ABSENT"
    assert result["families"]["news_raw"]["documents"] == []
    assert result["families"]["news_episode"]["store_health"] == \
        "STORE_MALFORMED"
    assert result["families"]["opendart"]["store_health"] == "OK"
    assert [row["rcept_no"] for row in
            result["families"]["opendart"]["disclosures"]] == [
                "20260902000111"]
    codes = {(item["family"], item["code"]) for item in result["limitations"]}
    assert ("news_raw", "STORE_ABSENT") in codes
    assert ("news_episode", "STORE_MALFORMED") in codes
    assert _tree(tmp_path) == before and not missing.parent.exists()

    mismatch = run_internal_evidence_query(
        _query(terms=("하닉",)), news_db=wrong_schema)
    assert mismatch["families"]["news_raw"]["store_health"] == "SCHEMA_MISMATCH"

    writer = sqlite3.connect(news, isolation_level=None)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        locked = run_internal_evidence_query(
            _query(terms=("하닉",), families=("news_raw",)), news_db=news)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert locked["families"]["news_raw"]["store_health"] == \
        "STORE_BUSY_OR_LOCKED"
    assert _tree(tmp_path) == before
    # Never a fallback-denial output: no permission boolean, no verdict key.
    for outcome in (result, mismatch, locked):
        keys = {key.upper() for key in _walk_keys(outcome)}
        assert not any(token in key for key in keys
                       for token in ("SUFFICIENT", "PERMIT", "ALLOW", "DENY",
                                     "VERDICT"))
        assert not any(isinstance(value, bool) for value in outcome.values())
        assert outcome["limitations"] and "residual_gaps" in outcome


def test_google_redirect_url_is_preserved_and_flagged_discovery_only(tmp_path):
    redirect = ("https://news.google.com/rss/articles/CBMiXYZ?oc=5")
    news, _ = _news_db(tmp_path, (
        (redirect, "하이닉스 HBM4 양산 개시", None, NOW - timedelta(days=1),
         "연합뉴스", "https://www.yna.co.kr"),
        ("https://www.yna.co.kr/view/AKR1", "하이닉스 HBM4 양산 개시 원문",
         None, NOW - timedelta(days=1), "연합뉴스", "https://www.yna.co.kr"),
    ))
    result = run_internal_evidence_query(
        _query(terms=("하이닉스",), families=("news_raw",)), news_db=news)
    by_title = {doc["title"]: doc for doc in
                result["families"]["news_raw"]["documents"]}
    google = by_title["하이닉스 HBM4 양산 개시"]
    assert google["url"].startswith("https://news.google.com/rss/articles/")
    assert google["url_kind"] == "GOOGLE_REDIRECT"
    assert google["publisher_name"] == "연합뉴스"
    assert google["publisher_site_url"] == "https://www.yna.co.kr"
    assert by_title["하이닉스 HBM4 양산 개시 원문"]["url_kind"] == "DIRECT"
    assert classify_url_kind("https://news.google.com/x") == "GOOGLE_REDIRECT"
    assert classify_url_kind("https://www.google.com/x") == "DIRECT"
    assert classify_url_kind("not a url") == "DIRECT"
    assert any(item["code"] == "GOOGLE_REDIRECT_DISCOVERY_ONLY"
               for item in result["limitations"])


# --------------------------------------------------- B) official local store

def test_opendart_family_reads_kr_symbols_and_flags_unsupported(tmp_path):
    opendart = _opendart_db(tmp_path, (
        ("20260902000111", "000660", "에스케이하이닉스",
         "주요사항보고서(자기주식소각결정)", "20260902"),
        ("20260831000222", "000660", "에스케이하이닉스",
         "기타경영사항", "20260831"),
        ("20260902000333", "005930", "삼성전자", "임원변경", "20260902"),
    ))
    result = run_internal_evidence_query(
        _query(symbols=("000660", "AVGO"), families=("opendart", "sec")),
        opendart_db=opendart)
    family = result["families"]["opendart"]
    assert family["store_health"] == "OK"
    rows = family["disclosures"]
    assert [row["rcept_no"] for row in rows] == ["20260902000111"]
    assert rows[0]["stock_code"] == "000660"
    assert rows[0]["report_nm"] == "주요사항보고서(자기주식소각결정)"
    assert rows[0]["rcept_dt"] == "2026-09-02"
    assert rows[0]["provenance"] == {"store": "opendart",
                                     "table": "disclosures",
                                     "rcept_no": "20260902000111",
                                     "first_run_id": "run-1"}
    assert family["counts"] == {"matched": 1, "returned": 1, "truncated": 0}
    codes = {(item["family"], item["code"], item["detail"])
             for item in result["limitations"]}
    assert ("opendart", "NON_KR_SYMBOL_SKIPPED", "AVGO") in codes
    assert ("sec", "UNSUPPORTED_FAMILY", "sec") in codes
    assert "sec" not in result["families"]
    assert "news_raw" not in result["families"]


def test_opendart_symbol_batching_dedupe_and_malformed_day_guard(
        tmp_path, monkeypatch):
    monkeypatch.setattr(ieq, "SYMBOL_BATCH", 1)
    opendart = _opendart_db(tmp_path, (
        ("20260902000111", "000660", "에스케이하이닉스", "A", "20260902"),
        ("20260901000222", "005930", "삼성전자", "B", "20260901"),
        ("20260903000333", "035420", "네이버", "C", "20260903"),
    ))
    result = run_internal_evidence_query(
        _query(symbols=("000660", "005930", "035420", "AVGO", "AVGO",
                        "000660"), families=("opendart",)),
        opendart_db=opendart)
    family = result["families"]["opendart"]
    assert [row["rcept_no"] for row in family["disclosures"]] == [
        "20260903000333", "20260902000111", "20260901000222"]
    assert family["counts"] == {"matched": 3, "returned": 3, "truncated": 0}
    skipped = [item for item in result["limitations"]
               if item["code"] == "NON_KR_SYMBOL_SKIPPED"]
    assert [item["detail"] for item in skipped] == ["AVGO"]
    connection = sqlite3.connect(opendart)
    # A malformed day that still passes the lexical window filter.
    connection.execute("UPDATE disclosures SET rcept_dt='20260902X'"
                       " WHERE rcept_no='20260902000111'")
    connection.commit()
    connection.close()
    malformed = run_internal_evidence_query(
        _query(symbols=("000660",), families=("opendart",)),
        opendart_db=opendart)
    assert malformed["families"]["opendart"]["store_health"] == \
        "STORE_MALFORMED"
    assert malformed["families"]["opendart"]["disclosures"] == []


# ------------------------------------------------- C) zero-external seal

def test_program_has_zero_external_surface():
    """The transplanted program carries no network/model/Telegram/order/
    ledger/write surface, opens SQLite read-only only after ``is_file()``,
    and never creates a store. (The CLI tool is intentionally NOT part of
    the canonical integration — the Quant seam calls the library.)"""
    source = PROGRAM.read_text()
    lowered = source.casefold()
    for token in ("urllib.request", "urllib3", "http.client", "socket",
                  "requests", "telegram", "tokmo.facts", "subprocess",
                  "composition", "trade_ledger", "playbook", "toss_orders",
                  "conditional", "receipt", "insert into", "update ",
                  "delete from", "create table", "immutable=1",
                  "mode=rwc", 'mode=rw"', "mode=rw'"):
        assert token not in lowered, token
    assert "mode=ro" in source
    assert "is_file()" in source
    assert not SCRIPT.exists()
