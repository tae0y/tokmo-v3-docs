"""OpenDART live boundary tests: wire-exact transport plus the bounded
probe campaign (ceiling 30, global 2s spacing, durable probes[] written
before I/O, no refund, tranche gates at 10/20, provider/auth stop)."""

import io
import json
import os
import stat
import urllib.error
import zipfile
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path

import pytest

from tokmo.composition import (
    DART_MAX_STARTS,
    DART_STATE_NAME,
    LiveRunError,
    checkpoint_dart,
    run_dart,
    setup_dart,
)
from tokmo.facts.opendart import (
    CORP_CODE_XML_NAME,
    REQUEST_TIMEOUT_SECONDS,
    _OpenDartTransportFailure,
    _OpenDartTransportSuccess,
    build_corp_code_request,
    build_list_request,
)
from tokmo.facts.opendart_live import (
    MAX_JSON_BODY_BYTES,
    MAX_ZIP_BODY_BYTES,
    OpenDartLiveTransport,
)
from tokmo.runtime.config import ConfigError, load_opendart_config

FIXTURES = Path(__file__).parent / "fixtures"
CORPCODE_XML = (FIXTURES / "opendart_corpcode_success.xml").read_bytes()
LIST_BODY = (FIXTURES / "opendart_list_success.json").read_bytes()
NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
KEY_SECRET = "fixture-opendart-key-0000000000000000000"
CORP = "00164779"


def zip_body():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(CORP_CODE_XML_NAME, CORPCODE_XML)
    return buffer.getvalue()


ZIP_BODY = zip_body()


def prepare(home):
    setup_dart(home=home)
    config = home / ".config" / "tokmo-v3" / "opendart.env"
    config.write_text(f"MODE=opendart-tracer\nOPENDART_API_KEY={KEY_SECRET}\n")
    os.chmod(config, 0o600)
    return home / ".config" / "tokmo-v3" / DART_STATE_NAME


def invoke(home, endpoint, transport, now=NOW):
    ids = iter(["record", "decision"])
    return run_dart(
        home=home, endpoint=endpoint, transport=transport,
        clock=lambda: now, id_factory=ids.__next__, run_id="run",
    )


def state(path):
    return json.loads(path.read_text())


def confirm_corpcode(home, now=NOW):
    return invoke(home, "corpcode", lambda _r: _OpenDartTransportSuccess(ZIP_BODY), now)


def probe_rows(count, outcome="timeout"):
    base = NOW - timedelta(seconds=3600)
    return [{"sequence": index + 1, "endpoint_id": "opendart_corp_code",
             "purpose": "confirm_000660_corp_mapping_schema",
             "started_at": (base + timedelta(seconds=2 * index)).isoformat(),
             "outcome": outcome} for index in range(count)]


def write_state(path, **changes):
    payload = state(path)
    payload.update(changes)
    path.write_text(json.dumps(payload))
    os.chmod(path, 0o600)


def record_checkpoint(home, tranche, new_information=None):
    checkpoint_dart(
        home=home, tranche=tranche, confirmed="confirmed-notes",
        changed="changed-notes", unresolved="named-schema-hypothesis",
        why_more_calls="why-more-calls", tests_green=True,
        no_scope_expansion=True, new_information=new_information,
        clock=lambda: NOW - timedelta(seconds=120))


# --- transport wire behavior -------------------------------------------------

class Response:
    status = 200

    def __init__(self, body=ZIP_BODY, headers=None):
        self.body = body
        self.headers = headers or Message()

    def read(self, size):
        return self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class CapturingOpener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_corp_code_transport_makes_exact_official_get_once():
    transport = OpenDartLiveTransport()
    opener = CapturingOpener(Response())
    transport.opener = opener
    outcome = transport(build_corp_code_request(api_key=KEY_SECRET))
    request, timeout = opener.calls[0]

    assert isinstance(outcome, _OpenDartTransportSuccess)
    assert len(opener.calls) == 1
    assert request.get_method() == "GET"
    assert timeout == REQUEST_TIMEOUT_SECONDS == 10
    assert request.full_url == (
        f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={KEY_SECRET}")
    assert request.get_header("Accept-encoding") == "identity"


def test_list_transport_uses_explicit_descending_first_page_query():
    transport = OpenDartLiveTransport()
    opener = CapturingOpener(Response(LIST_BODY))
    transport.opener = opener
    outcome = transport(build_list_request(
        corp_code=CORP, begin_date="20260428", end_date="20260727",
        api_key=KEY_SECRET))
    request, _timeout = opener.calls[0]

    assert isinstance(outcome, _OpenDartTransportSuccess)
    assert request.full_url == (
        "https://opendart.fss.or.kr/api/list.json"
        f"?corp_code={CORP}&bgn_de=20260428&end_de=20260727"
        f"&sort=date&sort_mth=desc&page_no=1&page_count=1&crtfc_key={KEY_SECRET}")


@pytest.mark.parametrize("corp_code,begin,end", [
    (None, "20260428", "20260727"),
    ("164779", "20260428", "20260727"),
    (CORP, "2026-04-28", "20260727"),
    (CORP, "20260428", "202607"),
])
def test_list_transport_rejects_bad_identity_without_network(corp_code, begin, end):
    transport = OpenDartLiveTransport()
    opener = CapturingOpener(Response())
    transport.opener = opener
    outcome = transport(build_list_request(
        corp_code=corp_code, begin_date=begin, end_date=end, api_key=KEY_SECRET))
    assert outcome.code == "protocol_failure"
    assert opener.calls == []


def test_opener_has_empty_proxy_policy_and_redirects_are_not_followed():
    transport = OpenDartLiveTransport()
    assert not any(handler.__class__.__name__ == "ProxyHandler"
                   for handler in transport.opener.handlers)
    redirect = urllib.error.HTTPError("masked", 302, "masked", Message(), None)
    transport.opener = CapturingOpener(redirect)
    assert transport(build_corp_code_request(api_key=KEY_SECRET)).code == "redirect"


@pytest.mark.parametrize("status,code", [
    (403, "auth_rejection"), (429, "http_429"), (503, "http_5xx"),
])
def test_http_errors_map_to_closed_codes(status, code):
    error = urllib.error.HTTPError("masked", status, "masked", Message(), None)
    transport = OpenDartLiveTransport()
    transport.opener = CapturingOpener(error)
    assert transport(build_corp_code_request(api_key=KEY_SECRET)).code == code


def test_http_429_carries_provider_retry_after():
    headers = Message()
    headers["Retry-After"] = "120"
    error = urllib.error.HTTPError("masked", 429, "masked", headers, None)
    transport = OpenDartLiveTransport()
    transport.opener = CapturingOpener(error)
    outcome = transport(build_corp_code_request(api_key=KEY_SECRET))
    assert (outcome.code, outcome.retry_after) == ("http_429", 120)


def test_per_endpoint_body_caps_and_content_encoding_fail_closed():
    transport = OpenDartLiveTransport()
    transport.opener = CapturingOpener(Response(b"x" * (MAX_ZIP_BODY_BYTES + 1)))
    assert transport(build_corp_code_request(
        api_key=KEY_SECRET)).code == "body_too_large"

    transport.opener = CapturingOpener(Response(b"x" * (MAX_JSON_BODY_BYTES + 1)))
    assert transport(build_list_request(
        corp_code=CORP, begin_date="20260428", end_date="20260727",
        api_key=KEY_SECRET)).code == "body_too_large"

    gzipped = Response()
    gzipped.headers["Content-Encoding"] = "gzip"
    transport.opener = CapturingOpener(gzipped)
    assert transport(build_corp_code_request(
        api_key=KEY_SECRET)).code == "unsupported_content_encoding"


# --- opendart config loader --------------------------------------------------

def test_opendart_config_loader_reasons(tmp_path):
    with pytest.raises(ConfigError, match="config_missing"):
        load_opendart_config(tmp_path / "absent.env")
    path = tmp_path / "opendart.env"
    path.write_text("OTHER=1\n")
    with pytest.raises(ConfigError, match="config_missing"):
        load_opendart_config(path)
    path.write_text("OPENDART_API_KEY=replace-me\n")
    with pytest.raises(ConfigError, match="config_invalid"):
        load_opendart_config(path)
    path.write_text(f"MODE=opendart-tracer\nOPENDART_API_KEY={KEY_SECRET}\n")
    config = load_opendart_config(path)
    assert config.api_key == KEY_SECRET
    assert KEY_SECRET not in repr(config)


# --- bounded probe campaign state machine ------------------------------------

def test_setup_dart_is_idempotent_and_private(tmp_path):
    setup_dart(home=tmp_path)
    setup_dart(home=tmp_path)
    state_path = tmp_path / ".config" / "tokmo-v3" / DART_STATE_NAME
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700
    assert state(state_path)["probes"] == []


def test_corpcode_probe_stores_mapping_and_hypothesis_is_not_repeated(tmp_path):
    state_path = prepare(tmp_path)
    result = confirm_corpcode(tmp_path)
    assert (result.code, result.endpoint, result.started) == ("success", "corpcode", 1)
    assert (result.corp_code, result.corp_name) == (CORP, "SK하이닉스")
    saved = state(state_path)
    assert saved["corp_code"] == CORP
    assert saved["probes"][0]["endpoint_id"] == "opendart_corp_code"
    assert saved["probes"][0]["purpose"] == "confirm_000660_corp_mapping_schema"
    assert saved["probes"][0]["outcome"] == "success"

    calls = []
    with pytest.raises(LiveRunError, match="already_confirmed"):
        invoke(tmp_path, "corpcode", lambda r: calls.append(r),
               NOW + timedelta(seconds=2))
    assert calls == []


def test_list_probe_requires_mapping_first_network_zero(tmp_path):
    prepare(tmp_path)
    calls = []
    with pytest.raises(LiveRunError, match="mapping_missing"):
        invoke(tmp_path, "list", lambda r: calls.append(r))
    assert calls == []


def test_list_probe_uses_bounded_window_and_runs_same_tracer_boundary(tmp_path):
    state_path = prepare(tmp_path)
    confirm_corpcode(tmp_path)
    requests = []

    def transport(request):
        requests.append(request)
        return _OpenDartTransportSuccess(LIST_BODY)

    result = invoke(tmp_path, "list", transport, NOW + timedelta(seconds=2))

    assert (result.code, result.started) == ("success", 2)
    assert (requests[0].begin_date, requests[0].end_date) == ("20260428", "20260727")
    assert (result.decision_action, result.decision_reason) == ("SAVE", "saved")
    assert result.disclosure.receipt_number == "20260727000169"
    data_dir = tmp_path / ".local" / "share" / "tokmo-v3"
    assert (data_dir / "opendart_facts.json").exists()
    assert (data_dir / "opendart_decisions.jsonl").exists()
    saved = state(state_path)
    assert saved["list_confirmed"] is True
    assert saved["probes"][1]["purpose"] == "confirm_latest_disclosure_schema"

    calls = []
    with pytest.raises(LiveRunError, match="already_confirmed"):
        invoke(tmp_path, "list", lambda r: calls.append(r),
               NOW + timedelta(seconds=10))
    assert calls == []


def test_probe_record_is_written_before_opener_and_never_refunded(tmp_path):
    state_path = prepare(tmp_path)

    def transport(_request):
        saved = state(state_path)
        assert len(saved["probes"]) == 1
        assert saved["probes"][0]["outcome"] == "started"
        assert saved["probes"][0]["purpose"] == "confirm_000660_corp_mapping_schema"
        raise RuntimeError(f"must not escape {KEY_SECRET}")

    with pytest.raises(LiveRunError) as caught:
        invoke(tmp_path, "corpcode", transport)
    assert caught.value.code == "protocol_failure"
    saved = state(state_path)
    assert len(saved["probes"]) == 1
    assert saved["probes"][0]["outcome"] == "protocol_failure"
    assert KEY_SECRET not in state_path.read_text()


def test_two_second_global_spacing_applies_across_endpoints(tmp_path):
    state_path = prepare(tmp_path)
    confirm_corpcode(tmp_path)
    calls = []
    with pytest.raises(LiveRunError, match="not_due"):
        invoke(tmp_path, "list", lambda r: calls.append(r),
               NOW + timedelta(seconds=1))
    assert calls == []
    with pytest.raises(LiveRunError, match="timeout"):
        invoke(tmp_path, "list", lambda _r: _OpenDartTransportFailure("timeout"),
               NOW + timedelta(seconds=2))
    assert len(state(state_path)["probes"]) == 2


def test_ceiling_thirty_started_makes_next_network_zero(tmp_path):
    state_path = prepare(tmp_path)
    write_state(state_path, probes=probe_rows(DART_MAX_STARTS))
    record_checkpoint(tmp_path, 2)
    record_checkpoint(tmp_path, 3, new_information="calls-11-20-new-info")
    calls = []
    with pytest.raises(LiveRunError, match="ceiling_exhausted"):
        invoke(tmp_path, "corpcode", lambda r: calls.append(r),
               NOW + timedelta(seconds=60))
    assert calls == []


def test_tranche_two_gate_blocks_call_eleven_until_checkpoint(tmp_path):
    state_path = prepare(tmp_path)
    write_state(state_path, probes=probe_rows(10))
    calls = []
    with pytest.raises(LiveRunError, match="tranche_gate"):
        invoke(tmp_path, "corpcode", lambda r: calls.append(r))
    assert calls == []

    record_checkpoint(tmp_path, 2)
    result = confirm_corpcode(tmp_path)
    assert (result.code, result.started) == ("success", 11)


def test_tranche_three_gate_requires_second_checkpoint(tmp_path):
    state_path = prepare(tmp_path)
    write_state(state_path, probes=probe_rows(20))
    record_checkpoint(tmp_path, 2)
    calls = []
    with pytest.raises(LiveRunError, match="tranche_gate"):
        invoke(tmp_path, "corpcode", lambda r: calls.append(r))
    assert calls == []

    record_checkpoint(tmp_path, 3, new_information="calls-11-20-new-info")
    result = confirm_corpcode(tmp_path)
    assert (result.code, result.started) == ("success", 21)


def test_checkpoint_without_new_information_cannot_open_tranche_three(tmp_path):
    prepare(tmp_path)
    with pytest.raises(LiveRunError, match="private_state_invalid"):
        record_checkpoint(tmp_path, 3)


@pytest.mark.parametrize("content", [b"{", b"[]", b'{"probes":[]}', b"x" * 70_000])
def test_malformed_state_fails_closed_with_no_network(tmp_path, content):
    state_path = prepare(tmp_path)
    state_path.write_bytes(content)
    os.chmod(state_path, 0o600)
    calls = []
    with pytest.raises(LiveRunError, match="private_state_invalid"):
        invoke(tmp_path, "corpcode", lambda r: calls.append(r))
    assert calls == []


def test_http_stop_outcomes_stop_future_probes(tmp_path):
    prepare(tmp_path)
    with pytest.raises(LiveRunError, match="http_429"):
        invoke(tmp_path, "corpcode",
               lambda _r: _OpenDartTransportFailure("http_429", retry_after=120))
    calls = []
    with pytest.raises(LiveRunError, match="fair_access_stop"):
        invoke(tmp_path, "corpcode", lambda r: calls.append(r),
               NOW + timedelta(seconds=3600))
    assert calls == []


def test_provider_status_key_rejection_stops_future_probes(tmp_path):
    prepare(tmp_path)
    error_body = (b"<result><status>010</status><message>masked</message></result>")
    with pytest.raises(LiveRunError, match="provider_key_rejected"):
        invoke(tmp_path, "corpcode",
               lambda _r: _OpenDartTransportSuccess(error_body))
    calls = []
    with pytest.raises(LiveRunError, match="fair_access_stop"):
        invoke(tmp_path, "corpcode", lambda r: calls.append(r),
               NOW + timedelta(seconds=60))
    assert calls == []


def test_provider_retry_after_overrides_two_second_spacing(tmp_path):
    state_path = prepare(tmp_path)
    with pytest.raises(LiveRunError, match="timeout"):
        invoke(tmp_path, "corpcode", lambda _r: _OpenDartTransportFailure("timeout"))
    write_state(state_path, retry_after_seconds=120)
    calls = []
    with pytest.raises(LiveRunError, match="not_due"):
        invoke(tmp_path, "corpcode", lambda r: calls.append(r),
               NOW + timedelta(seconds=60))
    assert calls == []


def test_non_private_opendart_config_is_rejected_before_network(tmp_path):
    prepare(tmp_path)
    os.chmod(tmp_path / ".config" / "tokmo-v3" / "opendart.env", 0o644)
    calls = []
    with pytest.raises(LiveRunError, match="private_state_invalid"):
        invoke(tmp_path, "corpcode", lambda r: calls.append(r))
    assert calls == []


def test_no_artifact_ever_contains_the_api_key(tmp_path):
    prepare(tmp_path)
    confirm_corpcode(tmp_path)
    invoke(tmp_path, "list", lambda _r: _OpenDartTransportSuccess(LIST_BODY),
           NOW + timedelta(seconds=2))
    artifacts = [path for path in tmp_path.rglob("*")
                 if path.is_file() and path.name != "opendart.env"]
    assert artifacts
    assert all(KEY_SECRET.encode() not in path.read_bytes() for path in artifacts)
