"""SEC live boundary tests: wire-exact transport plus the bounded probe
state machine (ceiling 10, 5s spacing, write-before-I/O, no refund,
hypothesis no-repeat, 403/429 fail-closed stop)."""

import json
import os
import stat
import urllib.error
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path

import pytest

from tokmo.composition import (
    SEC_MAX_STARTS,
    SEC_STATE_NAME,
    LiveRunError,
    run_sec,
    setup_sec,
)
from tokmo.facts.sec import (
    REQUEST_TIMEOUT_SECONDS,
    _SecTransportFailure,
    _SecTransportSuccess,
    build_submissions_request,
    build_tickers_request,
)
from tokmo.facts.sec_live import MAX_BODY_BYTES, SecLiveTransport
from tokmo.runtime.config import ConfigError, load_sec_config

FIXTURES = Path(__file__).parent / "fixtures"
TICKERS_BODY = (FIXTURES / "sec_company_tickers_success.json").read_bytes()
SUBMISSIONS_BODY = (FIXTURES / "sec_submissions_success.json").read_bytes()
NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
UA_SECRET = "TokmoTest/0.0 (fixture-contact@example.invalid)"
AAPL_CIK = "0000320193"


def prepare(home):
    setup_sec(home=home)
    config = home / ".config" / "tokmo-v3" / "sec.env"
    config.write_text(f"SEC_USER_AGENT={UA_SECRET}\n")
    os.chmod(config, 0o600)
    return home / ".config" / "tokmo-v3" / SEC_STATE_NAME


def invoke(home, endpoint, transport, now=NOW):
    ids = iter(["record", "decision"])
    return run_sec(
        home=home,
        endpoint=endpoint,
        transport=transport,
        clock=lambda: now,
        id_factory=ids.__next__,
        run_id="run",
    )


def state(path):
    return json.loads(path.read_text())


def confirm_tickers(home, now=NOW):
    return invoke(home, "tickers", lambda _r: _SecTransportSuccess(TICKERS_BODY), now)


# --- transport wire behavior -------------------------------------------------


class Response:
    status = 200

    def __init__(self, body=TICKERS_BODY, headers=None):
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


def test_tickers_transport_makes_exact_official_get_once():
    transport = SecLiveTransport()
    opener = CapturingOpener(Response())
    transport.opener = opener
    outcome = transport(build_tickers_request(user_agent=UA_SECRET))
    request, timeout = opener.calls[0]

    assert isinstance(outcome, _SecTransportSuccess)
    assert len(opener.calls) == 1
    assert request.get_method() == "GET"
    assert timeout == REQUEST_TIMEOUT_SECONDS == 10
    assert request.full_url == "https://www.sec.gov/files/company_tickers.json"
    assert request.get_header("User-agent") == UA_SECRET
    assert request.get_header("Accept-encoding") == "identity"


def test_submissions_transport_uses_zero_padded_cik_path():
    transport = SecLiveTransport()
    opener = CapturingOpener(Response(SUBMISSIONS_BODY))
    transport.opener = opener
    outcome = transport(build_submissions_request(cik=AAPL_CIK, user_agent=UA_SECRET))
    request, _timeout = opener.calls[0]

    assert isinstance(outcome, _SecTransportSuccess)
    assert request.full_url == f"https://data.sec.gov/submissions/CIK{AAPL_CIK}.json"


@pytest.mark.parametrize("cik", [None, "320193", "00003201x3", "0000320193 "])
def test_submissions_transport_rejects_bad_cik_without_network(cik):
    transport = SecLiveTransport()
    opener = CapturingOpener(Response())
    transport.opener = opener
    outcome = transport(build_submissions_request(cik=cik, user_agent=UA_SECRET))
    assert outcome.code == "protocol_failure"
    assert opener.calls == []


def test_opener_has_empty_proxy_policy_and_redirects_are_not_followed():
    transport = SecLiveTransport()
    assert not any(
        handler.__class__.__name__ == "ProxyHandler"
        for handler in transport.opener.handlers
    )
    redirect = urllib.error.HTTPError("masked", 301, "masked", Message(), None)
    transport.opener = CapturingOpener(redirect)
    assert transport(build_tickers_request(user_agent=UA_SECRET)).code == "redirect"


@pytest.mark.parametrize(
    "status,code",
    [
        (403, "auth_rejection"),
        (429, "http_429"),
        (503, "http_5xx"),
    ],
)
def test_http_errors_map_to_closed_codes(status, code):
    error = urllib.error.HTTPError("masked", status, "masked", Message(), None)
    transport = SecLiveTransport()
    transport.opener = CapturingOpener(error)
    assert transport(build_tickers_request(user_agent=UA_SECRET)).code == code


def test_body_cap_and_content_encoding_fail_closed():
    transport = SecLiveTransport()
    transport.opener = CapturingOpener(Response(b"x" * (MAX_BODY_BYTES + 1)))
    assert (
        transport(build_tickers_request(user_agent=UA_SECRET)).code == "body_too_large"
    )

    gzipped = Response()
    gzipped.headers["Content-Encoding"] = "gzip"
    transport.opener = CapturingOpener(gzipped)
    assert (
        transport(build_tickers_request(user_agent=UA_SECRET)).code
        == "unsupported_content_encoding"
    )


# --- sec config loader -------------------------------------------------------


def test_sec_config_loader_reasons(tmp_path):
    with pytest.raises(ConfigError, match="config_missing"):
        load_sec_config(tmp_path / "absent.env")
    path = tmp_path / "sec.env"
    path.write_text("OTHER=1\n")
    with pytest.raises(ConfigError, match="config_missing"):
        load_sec_config(path)
    path.write_text("SEC_USER_AGENT=replace-me\n")
    with pytest.raises(ConfigError, match="config_invalid"):
        load_sec_config(path)
    path.write_text("SEC_USER_AGENT=no-contact-address\n")
    with pytest.raises(ConfigError, match="config_invalid"):
        load_sec_config(path)
    path.write_text(f"SEC_USER_AGENT={UA_SECRET}\n")
    config = load_sec_config(path)
    assert config.user_agent == UA_SECRET
    assert UA_SECRET not in repr(config)


# --- bounded probe state machine --------------------------------------------


def test_setup_sec_is_idempotent_and_private(tmp_path):
    setup_sec(home=tmp_path)
    setup_sec(home=tmp_path)
    state_path = tmp_path / ".config" / "tokmo-v3" / SEC_STATE_NAME
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700
    assert state(state_path)["total_started"] == 0


def test_tickers_probe_stores_mapping_and_hypothesis_is_not_repeated(tmp_path):
    state_path = prepare(tmp_path)
    result = confirm_tickers(tmp_path)
    assert (result.code, result.endpoint, result.started) == ("success", "tickers", 1)
    assert (result.cik, result.company_name) == (AAPL_CIK, "Apple Inc.")
    saved = state(state_path)
    assert saved["aapl_cik"] == AAPL_CIK
    assert saved["last_endpoint"] == "sec_company_tickers"

    calls = []
    with pytest.raises(LiveRunError, match="already_confirmed"):
        invoke(
            tmp_path, "tickers", lambda r: calls.append(r), NOW + timedelta(seconds=5)
        )
    assert calls == []


def test_submissions_probe_requires_mapping_first_network_zero(tmp_path):
    prepare(tmp_path)
    calls = []
    with pytest.raises(LiveRunError, match="mapping_missing"):
        invoke(tmp_path, "submissions", lambda r: calls.append(r))
    assert calls == []


def test_submissions_probe_runs_same_tracer_boundary_and_confirms(tmp_path):
    state_path = prepare(tmp_path)
    confirm_tickers(tmp_path)
    result = invoke(
        tmp_path,
        "submissions",
        lambda _r: _SecTransportSuccess(SUBMISSIONS_BODY),
        NOW + timedelta(seconds=5),
    )

    assert (result.code, result.started) == ("success", 2)
    assert (result.decision_action, result.decision_reason) == ("SAVE", "saved")
    assert result.filing.accession_number == "0001140361-26-025622"
    data_dir = tmp_path / ".local" / "share" / "tokmo-v3"
    assert (data_dir / "sec_facts.json").exists()
    assert (data_dir / "sec_decisions.jsonl").exists()
    assert state(state_path)["submissions_confirmed"] is True

    calls = []
    with pytest.raises(LiveRunError, match="already_confirmed"):
        invoke(
            tmp_path,
            "submissions",
            lambda r: calls.append(r),
            NOW + timedelta(seconds=10),
        )
    assert calls == []


def test_attempt_is_written_before_opener_and_never_refunded(tmp_path):
    state_path = prepare(tmp_path)

    def transport(_request):
        saved = state(state_path)
        assert saved["total_started"] == 1
        assert saved["last_outcome"] == "started"
        assert saved["last_purpose"]
        raise RuntimeError(f"must not escape {UA_SECRET}")

    with pytest.raises(LiveRunError) as caught:
        invoke(tmp_path, "tickers", transport)
    assert caught.value.code == "protocol_failure"
    saved = state(state_path)
    assert saved["total_started"] == 1
    assert saved["last_outcome"] == "protocol_failure"
    assert UA_SECRET not in state_path.read_text()


def test_five_second_spacing_applies_across_endpoints(tmp_path):
    state_path = prepare(tmp_path)
    confirm_tickers(tmp_path)
    calls = []
    with pytest.raises(LiveRunError, match="not_due"):
        invoke(
            tmp_path,
            "submissions",
            lambda r: calls.append(r),
            NOW + timedelta(seconds=4),
        )
    assert calls == []
    with pytest.raises(LiveRunError, match="timeout"):
        invoke(
            tmp_path,
            "submissions",
            lambda _r: _SecTransportFailure("timeout"),
            NOW + timedelta(seconds=5),
        )
    assert state(state_path)["total_started"] == 2


def test_ceiling_ten_started_makes_eleventh_network_zero(tmp_path):
    state_path = prepare(tmp_path)
    payload = state(state_path)
    payload.update(
        total_started=SEC_MAX_STARTS,
        last_started_at=NOW.isoformat(),
        last_endpoint="sec_company_tickers",
        last_purpose="p",
        last_outcome="timeout",
    )
    state_path.write_text(json.dumps(payload))
    os.chmod(state_path, 0o600)
    calls = []
    with pytest.raises(LiveRunError, match="ceiling_exhausted"):
        invoke(
            tmp_path, "tickers", lambda r: calls.append(r), NOW + timedelta(seconds=60)
        )
    assert calls == []


@pytest.mark.parametrize(
    "content", [b"{", b"[]", b'{"total_started":0}', b"x" * 20_000]
)
def test_malformed_state_fails_closed_with_no_network(tmp_path, content):
    state_path = prepare(tmp_path)
    state_path.write_bytes(content)
    os.chmod(state_path, 0o600)
    calls = []
    with pytest.raises(LiveRunError, match="private_state_invalid"):
        invoke(tmp_path, "tickers", lambda r: calls.append(r))
    assert calls == []


@pytest.mark.parametrize("code", ["auth_rejection", "http_429"])
def test_fair_access_rejection_stops_future_probes(tmp_path, code):
    prepare(tmp_path)
    with pytest.raises(LiveRunError, match=code):
        invoke(tmp_path, "tickers", lambda _r: _SecTransportFailure(code))
    calls = []
    with pytest.raises(LiveRunError, match="fair_access_stop"):
        invoke(
            tmp_path, "tickers", lambda r: calls.append(r), NOW + timedelta(seconds=60)
        )
    assert calls == []


def test_non_private_sec_config_is_rejected_before_network(tmp_path):
    prepare(tmp_path)
    os.chmod(tmp_path / ".config" / "tokmo-v3" / "sec.env", 0o644)
    calls = []
    with pytest.raises(LiveRunError, match="private_state_invalid"):
        invoke(tmp_path, "tickers", lambda r: calls.append(r))
    assert calls == []


def test_no_artifact_ever_contains_the_user_agent(tmp_path):
    prepare(tmp_path)
    confirm_tickers(tmp_path)
    invoke(
        tmp_path,
        "submissions",
        lambda _r: _SecTransportSuccess(SUBMISSIONS_BODY),
        NOW + timedelta(seconds=5),
    )
    artifacts = [
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and path.name != "sec.env"
    ]
    assert artifacts
    assert all(UA_SECRET.encode() not in path.read_bytes() for path in artifacts)
