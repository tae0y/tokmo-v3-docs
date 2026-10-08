"""Two-tier read-only OpenDART official-evidence pipeline (S4b).

SDS 2026-09-01 A2/A3/A4: correlation-gated refs, one bounded live start,
success-only transport-union unwrap, durable read-back receipts as the
sole live promotion authority, typed fail-closed lookup failures, and the
§V10 counter units. All fixtures synthetic — zero network.
"""
import json
from datetime import date, datetime, timezone
from pathlib import Path

from tokmo.facts.opendart import (
    _OpenDartTransportFailure,
    _OpenDartTransportSuccess,
    OpenDartRequest,
    build_list_request,
    parse_disclosure_list,
)
from tokmo.facts.opendart_live import OpenDartLiveTransport
from tokmo.programs.news_official_evidence import (
    OFFICIAL_EVIDENCE_MAX_REFS,
    OfficialLiveSeamV1,
    build_official_evidence_reader,
    build_official_receipt_sink,
)
from tokmo.records.opendart_disclosures import DisclosureStore

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
WINDOW = (date(2026, 8, 29), date(2026, 9, 1))
BUYBACK = frozenset({"BUYBACK_RETIREMENT", "BUYBACK"})


def _disclosure_db(tmp_path, rows):
    path = tmp_path / "opendart-bulk.sqlite3"
    store = DisclosureStore.open(path)
    for rcept_no, stock_code, report_nm, rcept_dt in rows:
        store.connection.execute(
            "insert into disclosures values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rcept_no, "C" + rcept_no[-6:], "회사", stock_code, "Y",
             report_nm, "회사", rcept_dt, None, "{}", "h" * 64,
             NOW.isoformat(), "run-1"))
    store.connection.commit()
    store.close()
    return path


def _live_body(*, rcept_no="20260901000777", corp_code="C000001",
               stock_code="000660",
               report_nm="주요사항보고서(자기주식소각결정)",
               rcept_dt="20260901", status="000"):
    return json.dumps({
        "status": status, "message": "정상", "page_no": 1,
        "list": [{"corp_code": corp_code, "corp_name": "회사",
                  "stock_code": stock_code, "corp_cls": "Y",
                  "report_nm": report_nm, "rcept_no": rcept_no,
                  "flr_nm": "회사", "rcept_dt": rcept_dt,
                  "rm": ""}]}).encode("utf-8")


def _seam(transport, credential=lambda: "test-key"):
    # §V5: the transport is a LAZY factory — construction happens inside
    # the guarded lookup, never at seam assembly.
    return OfficialLiveSeamV1(credential_loader=credential,
                              transport_factory=lambda: transport)


def _reader(tmp_path, rows, *, live=None, root=None):
    sink = build_official_receipt_sink(root) if root is not None else None
    return build_official_evidence_reader(
        _disclosure_db(tmp_path, rows), live=live, receipt_sink=sink)


def _receipts(root):
    directory = Path(root) / "news" / "official_evidence_receipts"
    return sorted(directory.glob("official-*.json")) \
        if directory.exists() else []


# ------------------------------------------------- tier 1 (local, JB-2/9)


def test_tier1_correlated_refs_within_window(tmp_path):
    reader = _reader(tmp_path, [
        ("20260901000123", "000660", "주요사항보고서(자기주식소각결정)",
         "20260901"),
        ("20260901000124", "000660", "임원ㆍ주요주주특정증권등소유상황보고서",
         "20260901"),
        ("20260710000001", "000660", "주요사항보고서(자기주식소각결정)",
         "20260710"),  # outside window
    ])
    outcome = reader(("000660",), *WINDOW, BUYBACK)
    assert [ref.ref_id for ref in outcome.refs] == ["20260901000123"]
    assert outcome.refs[0].matched_action == "BUYBACK_RETIREMENT"
    assert outcome.local_reads == 1 and outcome.http_starts == 0
    assert outcome.refs_returned == 1 and outcome.refs_correlated == 1
    assert outcome.failure is None


def test_jb2_incompatible_action_same_company_same_day_yields_no_ref(
        tmp_path):
    reader = _reader(tmp_path, [
        ("20260901000200", "000660", "현금ㆍ현물배당결정", "20260901")])
    # Article action family = M&A; the same-day dividend filing must never
    # correlate (§Z3: company/date alone is insufficient).
    outcome = reader(("000660",), *WINDOW,
                     frozenset({"MERGER_ACQUISITION"}))
    assert outcome.refs == ()
    assert outcome.refs_returned == 1 and outcome.refs_correlated == 0
    assert outcome.http_starts == 0  # window rows exist → tier 2 never fires
    assert outcome.failure is None


def test_tier1_is_bounded_to_eight_refs(tmp_path):
    rows = [(f"202609010001{i:02d}", "000660",
             "주요사항보고서(자기주식소각결정)", "20260901")
            for i in range(12)]
    outcome = _reader(tmp_path, rows)(("000660",), *WINDOW, BUYBACK)
    assert len(outcome.refs) == OFFICIAL_EVIDENCE_MAX_REFS == 8


def test_empty_codes_or_families_never_open_anything(tmp_path):
    reader = _reader(tmp_path, [])
    assert reader((), *WINDOW, BUYBACK).local_reads == 0
    assert reader(("000660",), *WINDOW, frozenset()).local_reads == 0


def test_store_degradation_is_typed_fail_closed(tmp_path):
    absent = build_official_evidence_reader(
        tmp_path / "missing.sqlite3",
        receipt_sink=build_official_receipt_sink(tmp_path / "root-a"))
    outcome = absent(("000660",), *WINDOW, BUYBACK)
    assert outcome.refs == () and outcome.failure == "STORE_UNAVAILABLE"
    assert len(_receipts(tmp_path / "root-a")) == 1
    corrupt_path = tmp_path / "corrupt.sqlite3"
    corrupt_path.write_bytes(b"not-a-database" * 8)
    corrupt = build_official_evidence_reader(corrupt_path)
    assert corrupt(("000660",), *WINDOW, BUYBACK).failure \
        == "STORE_UNAVAILABLE"


def test_report_patterns_never_apply_to_article_titles(tmp_path):
    outcome = _reader(tmp_path, [
        ("20260901000125", "000660", "속보 하이닉스 이슈 검토 보도",
         "20260901")])(("000660",), *WINDOW, BUYBACK)
    assert outcome.refs == () and outcome.refs_returned == 0


# --------------------------------------- tier 2 (live, JB-4/10/12/13/§V3)

# An out-of-window row keeps corp_code locally resolvable while leaving
# the window uncovered — the §Z2 tier-2 trigger.
_CORP_SEED = [("20260710000001", "000660",
               "임원ㆍ주요주주특정증권등소유상황보고서", "20260710")]


def test_jb10_tier2_fires_once_only_on_window_miss(tmp_path):
    calls = []

    def transport(request):
        calls.append(request)
        return _OpenDartTransportSuccess(body=_live_body())

    reader = _reader(tmp_path, _CORP_SEED, live=_seam(transport),
                     root=tmp_path / "root")
    outcome = reader(("000660",), *WINDOW, BUYBACK, candidate_id="ndc-1",
                     attempt_no=1, now=NOW,
                     event_type="CORPORATE_ACTION_DECIDED")
    assert len(calls) == 1
    assert outcome.local_reads == 1 and outcome.http_starts == 1
    assert [ref.ref_id for ref in outcome.refs] == ["20260901000777"]
    assert outcome.refs_correlated == 1 and outcome.failure is None
    # the §V1 request went through the REAL builder contract (JB-12)
    assert isinstance(calls[0], OpenDartRequest)
    assert calls[0].corp_code == "C000001"  # resolved from the local store
    assert calls[0].begin_date == "20260829"
    assert calls[0].end_date == "20260901"
    # §V3: the durable receipt exists and re-reads as the ref (decision 19)
    (receipt_path,) = _receipts(tmp_path / "root")
    payload = json.loads(receipt_path.read_text("utf-8"))
    assert payload["typed"] == "OK"
    assert payload["ref"]["ref_id"] == "20260901000777"
    assert payload["tier"] == "live"
    assert payload["event_type"] == "CORPORATE_ACTION_DECIDED"
    counters = payload["counters"]
    assert set(counters) == {
        "official_attempts", "official_local_reads",
        "official_http_starts", "official_refs_returned",
        "official_refs_correlated", "official_failures"}
    assert counters == {"official_attempts": 1,
                        "official_local_reads": 1,
                        "official_http_starts": 1,
                        "official_refs_returned": 1,
                        "official_refs_correlated": 1,
                        "official_failures": 0}


def test_v3_unwritable_receipt_blocks_live_promotion(tmp_path):
    def transport(request):
        return _OpenDartTransportSuccess(body=_live_body())

    # no receipt sink bound → read-back impossible → promotion refused
    reader = build_official_evidence_reader(
        _disclosure_db(tmp_path, _CORP_SEED), live=_seam(transport),
        receipt_sink=None)
    outcome = reader(("000660",), *WINDOW, BUYBACK, now=NOW)
    assert outcome.refs == () and outcome.failure == "RECEIPT_UNVERIFIED"


def test_jb4_typed_live_failures_fail_closed_with_receipts(tmp_path):
    def failing_credential():
        raise ValueError("config_mode")

    scenarios = [
        ("AUTH_UNAVAILABLE",
         _seam(lambda request: _OpenDartTransportSuccess(body=_live_body()),
               credential=failing_credential)),
        ("PROVIDER_TRANSPORT",
         _seam(lambda request: _OpenDartTransportFailure(code="timeout"))),
        ("PROVIDER_TRANSPORT",
         _seam(lambda request: (_ for _ in ()).throw(OSError("boom")))),
        ("PROVIDER_STATUS",
         _seam(lambda request: _OpenDartTransportSuccess(
             body=_live_body(status="020")))),
        ("SCHEMA_INVALID",
         _seam(lambda request: _OpenDartTransportSuccess(body=b"{broken"))),
    ]
    for index, (expected, seam) in enumerate(scenarios):
        root = tmp_path / f"root-{index}"
        reader = _reader(tmp_path / f"db-{index}", _CORP_SEED, live=seam,
                         root=root)
        outcome = reader(("000660",), *WINDOW, BUYBACK, now=NOW,
                         event_type="CORPORATE_ACTION_DECIDED")
        assert outcome.refs == (), expected
        assert outcome.failure == expected
        # §V10: http_starts counts ACTUAL transport invocations only — a
        # credential refusal never starts transport.
        expected_starts = 0 if expected == "AUTH_UNAVAILABLE" else 1
        assert outcome.http_starts == expected_starts, expected
        (receipt_path,) = _receipts(root)
        payload = json.loads(receipt_path.read_text("utf-8"))
        assert payload["typed"] == expected
        assert payload["tier"] == "live"
        assert payload["event_type"] == "CORPORATE_ACTION_DECIDED"
        counters = payload["counters"]
        assert set(counters) == {
            "official_attempts", "official_local_reads",
            "official_http_starts", "official_refs_returned",
            "official_refs_correlated", "official_failures"}
        assert counters["official_attempts"] == 1
        assert counters["official_failures"] == 1
        assert counters["official_http_starts"] == expected_starts


def test_jb13_latest_row_false_negative_is_accepted_and_local_resolves(
        tmp_path):
    # Live tier sees only the LATEST row: a newer incompatible filing
    # hides the older compatible one (accepted bounded false negative).
    def transport(request):
        return _OpenDartTransportSuccess(body=_live_body(
            report_nm="현금ㆍ현물배당결정", rcept_no="20260901000900"))

    live_only = _reader(tmp_path / "live", _CORP_SEED,
                        live=_seam(transport))
    outcome = live_only(("000660",), *WINDOW, BUYBACK, now=NOW)
    assert outcome.refs == () and outcome.failure is None
    assert outcome.refs_returned == 1 and outcome.refs_correlated == 0
    # The same disclosures loaded into the LOCAL store resolve correctly.
    local = _reader(tmp_path / "local", _CORP_SEED + [
        ("20260830000800", "000660", "주요사항보고서(자기주식소각결정)",
         "20260830"),
        ("20260901000900", "000660", "현금ㆍ현물배당결정", "20260901")])
    resolved = local(("000660",), *WINDOW, BUYBACK)
    assert [ref.ref_id for ref in resolved.refs] == ["20260830000800"]


def test_v10_patternless_live_report_is_not_a_returned_ref(tmp_path):
    def transport(request):
        return _OpenDartTransportSuccess(body=_live_body(
            report_nm="임원ㆍ주요주주특정증권등소유상황보고서",
            rcept_no="20260901000901"))

    reader = _reader(tmp_path, _CORP_SEED, live=_seam(transport))
    outcome = reader(("000660",), *WINDOW, BUYBACK, now=NOW)
    assert outcome.refs == () and outcome.failure is None
    # parsed fine, but no action pattern → refs_returned stays 0 (§V10).
    assert outcome.refs_returned == 0
    assert outcome.http_starts == 1


def test_jb12_interface_round_trip_matches_selected_family():
    # The production request builder emits exactly what the selected
    # transport consumes, and the fixture body round-trips through the
    # selected parser (compile-level + fixture proof, zero network).
    request = build_list_request(corp_code="C000777",
                                 begin_date="20260829",
                                 end_date="20260901", api_key="k")
    assert isinstance(request, OpenDartRequest)
    transport = OpenDartLiveTransport()
    assert callable(transport)
    parsed = parse_disclosure_list(_live_body(), corp_code="C000001",
                                   stock_code="000660", observed_at=NOW)
    assert parsed.receipt_number == "20260901000777"
    assert parsed.report_name == "주요사항보고서(자기주식소각결정)"


def test_jb8_worker_and_delivery_never_reach_official_evidence():
    import ast
    root = Path(__file__).parents[1] / "src" / "tokmo"
    for name in ("programs/news_classifier_worker.py",
                 "programs/news_alert_delivery.py",
                 "programs/news_event_admission.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        imports = {node.module or "" for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom)}
        imports |= {alias.name for node in ast.walk(tree)
                    if isinstance(node, ast.Import)
                    for alias in node.names}
        assert not any("official_evidence" in item or "opendart" in item
                       for item in imports), name


def test_news_modules_import_opendart_only_through_this_seam():
    """§K-1: across src/tokmo, a news_* module may IMPORT opendart code only
    in programs/news_official_evidence.py."""
    import ast
    root = Path(__file__).parents[1] / "src" / "tokmo"
    offenders = []
    for path in root.rglob("news_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {node.module or "" for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom)}
        imports |= {alias.name for node in ast.walk(tree)
                    if isinstance(node, ast.Import)
                    for alias in node.names}
        if any("opendart" in item for item in imports) \
                and path.name != "news_official_evidence.py":
            offenders.append(path)
    assert offenders == []
    seam = root / "programs" / "news_official_evidence.py"
    assert "opendart_disclosures" in seam.read_text(encoding="utf-8")
