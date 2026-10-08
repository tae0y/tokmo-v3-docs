"""KIS authentication and read-only ETF connection: no real credentials."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import io
import json

import pytest

from tokmo.values.kis_etf import KisCredentials, KisRequest, KisResponse, KisError
from tokmo.facts.kis_etf import build_request, parse_etf_response
from tokmo.facts.kis_etf_live import KisLiveTransport, load_kis_credentials
from tokmo.programs.kis_etf import KisEtfClient
from tokmo.records.kis_state import KisState


NOW = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
CREDS = KisCredentials("fixture-key", "fixture-secret")


def response(data, status=200):
    return KisResponse(status, json.dumps(data).encode(), "")


def test_wire_surface_contains_only_auth_and_two_quote_reads():
    method, path, params, body = build_request(KisRequest("auth", CREDS))
    assert method == "POST" and path == "/oauth2/tokenP"
    assert body == {"grant_type": "client_credentials", "appkey": "fixture-key", "appsecret": "fixture-secret"}
    for operation, market, tr_id in (("components", "J", "FHKST121600C0"), ("nav", "E", "FHPST02440100")):
        method, path, params, body = build_request(KisRequest(operation, CREDS, "fixture-token", "0138Y0"))
        assert method == "GET" and body is None and params["FID_COND_MRKT_DIV_CODE"] == market
        assert "/quotations/" in path and params["FID_INPUT_ISCD"] == "0138Y0"
    for request in (KisRequest("order", CREDS), KisRequest("nav", CREDS, "token", "../../orders")):
        with pytest.raises(KisError): build_request(request)
    assert "fixture" not in repr(CREDS) + repr(KisRequest("auth", CREDS))


def test_time_only_nav_is_not_assigned_the_retrieval_date():
    parsed = parse_etf_response("nav", "251600", response({"rt_cd": "0", "output": [
        {"bsop_hour": "153000", "nav": "10000.00", "stck_prpr": "10100", "dprt": "1.0"}]}), received_at=NOW)
    assert parsed["source_date"] is None and parsed["freshness"] == "UNVERIFIED"
    assert parsed["rows"][0]["source_time"] == "15:30:00"
    assert parsed["rows"][0]["computed_gap_pct"] == "1.00"


def test_components_keep_provider_weights_and_unknown_coverage():
    parsed = parse_etf_response("components", "251600", response({"rt_cd": "0", "output1": {"etf_cnfg_issu_cnt": "3"},
        "output2": [{"stck_shrn_iscd": "005930", "hts_kor_isnm": "삼성전자", "etf_cnfg_issu_rlim": "42.5", "etf_vltn_amt": "100"}]}), received_at=NOW)
    assert parsed["rows"][0]["provider_weight_pct"] == "42.5"
    assert parsed["coverage"] == "PARTIAL" and parsed["reported_component_count"] == "3"
    assert parsed["weight_basis"] == "PROVIDER_UNVERIFIED"


def test_empty_and_provider_error_are_not_zero_nav():
    parsed = parse_etf_response("nav", "251600", response({"rt_cd": "0", "output": []}), received_at=NOW)
    assert parsed["status"] == "NO_DATA" and parsed["rows"] == []
    with pytest.raises(KisError) as raised:
        parse_etf_response("nav", "251600", response({"rt_cd": "1", "msg_cd": "EGW00001", "msg1": "fixture-secret"}), received_at=NOW)
    assert "fixture-secret" not in str(raised.value) and raised.value.code == "EGW00001"


@pytest.mark.parametrize("nav", ["NaN", "Infinity", "-1", "0"])
def test_bad_nav_does_not_produce_a_gap(nav):
    with pytest.raises(KisError):
        parse_etf_response("nav", "251600", response({"rt_cd": "0", "output": [{"bsop_hour": "153000", "nav": nav, "stck_prpr": "100"}]}), received_at=NOW)


def test_credentials_are_private_and_not_interpreted_as_shell(tmp_path):
    path = tmp_path / ".env.kis"
    path.write_text('KIS_APP_KEY=fixture-key\nKIS_APP_SECRET=fixture-secret=\n')
    path.chmod(0o600)
    assert load_kis_credentials(path).app_secret == "fixture-secret="
    path.chmod(0o644)
    with pytest.raises(KisError): load_kis_credentials(path)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(KisError): load_kis_credentials(link)


def test_one_auth_is_reused_and_persisted_across_clients(tmp_path):
    calls = []
    def transport(request):
        calls.append(request.operation)
        if request.operation == "auth":
            return response({"access_token": "fixture-token", "token_type": "Bearer", "expires_in": 86400,
                             "access_token_token_expired": "2026-09-12 21:00:00"})
        return response({"rt_cd": "0", "output": [{"bsop_hour": "153000", "nav": "100", "stck_prpr": "101"}]})
    def client():
        return KisEtfClient(credentials=CREDS, state=KisState(tmp_path / "private"), transport=transport,
                            clock=lambda: NOW, pacer=lambda seconds: None, max_requests=3)
    assert client().fetch("nav", "251600")["status"] == "OK"
    assert client().fetch("nav", "0138Y0")["status"] == "OK"
    assert calls == ["auth", "nav", "nav"]
    fact = json.loads((tmp_path / "private" / "nav-251600.json").read_text())
    assert "fixture" not in json.dumps(fact)
    assert (tmp_path / "private" / "token.json").stat().st_mode & 0o777 == 0o600


def test_auth_rejection_never_retries_or_fetches_data(tmp_path):
    calls = []
    def transport(request):
        calls.append(request.operation)
        return response({"error_code": "EGW00123", "error_description": "fixture-secret"}, 403)
    client = KisEtfClient(credentials=CREDS, state=KisState(tmp_path / "private"), transport=transport,
                         clock=lambda: NOW, pacer=lambda seconds: None, max_requests=5)
    for _ in range(2):
        with pytest.raises(KisError): client.fetch("nav", "251600")
    assert calls == ["auth"]


def test_request_budget_covers_auth_and_data(tmp_path):
    calls = []
    def transport(request):
        calls.append(request.operation)
        return response({"access_token": "fixture-token", "token_type": "Bearer", "expires_in": 3600})
    client = KisEtfClient(credentials=CREDS, state=KisState(tmp_path / "private"), transport=transport,
                         clock=lambda: NOW, pacer=lambda seconds: None, max_requests=1)
    with pytest.raises(KisError, match="request_budget"):
        client.fetch("nav", "251600")
    assert calls == ["auth"]


def test_real_transport_headers_and_fixed_origin_without_sockets():
    class Reply(io.BytesIO):
        status = 200
        headers = {}
    class Opener:
        def open(self, request, timeout):
            assert request.full_url.startswith("https://openapi.koreainvestment.com:9443/uapi/etfetn/v1/quotations/")
            assert request.method == "GET" and timeout == 10
            assert request.get_header("Tr_id") == "FHPST02440100"
            assert request.get_header("Authorization") == "Bearer fixture-token"
            return Reply(b'{"rt_cd":"0","output":[]}')
    transport = KisLiveTransport(opener=Opener())
    assert transport(KisRequest("nav", CREDS, "fixture-token", "251600")).status == 200


def test_partial_weight_sum_of_100_is_not_total_fund_exposure():
    data = {"rt_cd": "0", "output1": {"etf_cnfg_issu_cnt": "14"}, "output2": [
        {"stck_shrn_iscd": str(i), "hts_kor_isnm": "반환된 일부 자산", "etf_cnfg_issu_rlim": w}
        for i, w in enumerate(("94.67", "2.68", "2.65"))]}
    fact = parse_etf_response("components", "0138Y0", response(data), received_at=NOW)
    assert fact["returned_weight_sum_pct"] == "100.00"
    assert fact["coverage"] == "PARTIAL" and fact["weight_basis"] == "PROVIDER_UNVERIFIED"
    assert all("weight_pct" not in row for row in fact["rows"])


def test_duplicate_json_keys_and_invalid_time_are_rejected():
    with pytest.raises(KisError):
        parse_etf_response("nav", "251600", KisResponse(200, b'{"rt_cd":"0","rt_cd":"1","output":[]}'), received_at=NOW)
    with pytest.raises(KisError):
        parse_etf_response("nav", "251600", response({"rt_cd": "0", "output": [{"bsop_hour": "256199", "nav": "100", "stck_prpr": "100"}]}), received_at=NOW)


def test_private_echo_in_quote_is_not_persisted(tmp_path):
    def transport(request):
        if request.operation == "auth":
            return response({"access_token": "fixture-token", "token_type": "Bearer", "expires_in": 3600})
        return response({"rt_cd": "0", "output": [], "unexpected": "fixture-secret"})
    client = KisEtfClient(credentials=CREDS, state=KisState(tmp_path / "private"), transport=transport,
                         clock=lambda: NOW, pacer=lambda seconds: None, max_requests=2)
    with pytest.raises(KisError, match="private_value_in_quote"):
        client.fetch("nav", "251600")
    assert not (tmp_path / "private" / "source-nav-251600.json").exists()
