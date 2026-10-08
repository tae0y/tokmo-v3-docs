import json
import os
import stat
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message

import pytest

from tokmo.composition import (
    MAX_ATTEMPTS, STATE_NAME, LiveRunError, run_live, setup_live,
)
from tokmo.facts.eia import (
    REQUEST_TIMEOUT_SECONDS, WTI_QUERY_MULTIMAP, _LiveTransportFailure,
    _LiveTransportSuccess, build_wti_request,
)
from tokmo.facts.eia_live import (
    EIA_HOST, EIA_PATH, MAX_BODY_BYTES, EiaLiveTransport, build_live_opener,
)

NOW = datetime(2026, 7, 27, 1, 2, 3, tzinfo=timezone.utc)
SECRET = "fixture-secret-never-print"
BODY = b'{"response":{"data":[{"period":"2026-07-24","series":"RWTC","value":"78.42","units":"$/BBL"}]}}'


def prepare(home):
    setup_live(home=home)
    config = home / ".config" / "tokmo-v3" / "tokmo.env"
    config.write_text(f"MODE=tracer-live\nEIA_API_KEY={SECRET}\n")
    os.chmod(config, 0o600)
    return home / ".config" / "tokmo-v3" / STATE_NAME


def invoke(home, transport, now=NOW):
    ids = iter(["record", "decision"])
    return run_live(
        home=home, transport=transport, clock=lambda: now,
        id_factory=ids.__next__, run_id="run",
    )


def state(path):
    return json.loads(path.read_text())


def test_setup_is_network_zero_idempotent_and_only_modes_tokmo_dirs(tmp_path):
    config_parent = tmp_path / ".config"
    local_parent = tmp_path / ".local"
    config_parent.mkdir(mode=0o755)
    local_parent.mkdir(mode=0o755)

    setup_live(home=tmp_path)
    setup_live(home=tmp_path)

    assert stat.S_IMODE(config_parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(local_parent.stat().st_mode) == 0o755
    for path in (
        config_parent / "tokmo-v3",
        local_parent / "share" / "tokmo-v3",
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE((config_parent / "tokmo-v3" / STATE_NAME).stat().st_mode) == 0o600


def test_success_uses_same_tracer_path_and_stops_future_calls(tmp_path):
    state_path = prepare(tmp_path)
    calls = []

    def transport(request):
        calls.append(request)
        return _LiveTransportSuccess(BODY)

    result = invoke(tmp_path, transport)
    assert (result.code, result.decision_action, result.decision_reason) == (
        "success", "SAVE", "saved")
    assert len(calls) == 1
    assert state(state_path)["success"] is True
    assert (tmp_path / ".local" / "share" / "tokmo-v3" / "facts.json").exists()
    assert (tmp_path / ".local" / "share" / "tokmo-v3" / "decisions.jsonl").exists()

    with pytest.raises(LiveRunError, match="already_succeeded"):
        invoke(tmp_path, transport, NOW + timedelta(seconds=5))
    assert len(calls) == 1


def test_start_is_written_before_opener_and_never_refunded(tmp_path):
    state_path = prepare(tmp_path)

    def transport(_request):
        assert state(state_path)["total_started"] == 1
        raise RuntimeError(f"must not escape {SECRET}")

    with pytest.raises(LiveRunError) as caught:
        invoke(tmp_path, transport)
    assert caught.value.code == "protocol_failure"
    saved = state(state_path)
    assert saved["total_started"] == 1
    assert SECRET not in repr(caught.value)
    assert SECRET not in state_path.read_text()


def test_spacing_blocks_without_call_and_allows_at_five_seconds(tmp_path):
    state_path = prepare(tmp_path)
    transport = lambda _request: _LiveTransportFailure("timeout")
    with pytest.raises(LiveRunError, match="timeout"):
        invoke(tmp_path, transport)
    calls = []
    with pytest.raises(LiveRunError, match="not_due"):
        invoke(tmp_path, lambda request: calls.append(request), NOW + timedelta(seconds=4))
    assert calls == []
    with pytest.raises(LiveRunError, match="timeout"):
        invoke(tmp_path, transport, NOW + timedelta(seconds=5))
    assert state(state_path)["total_started"] == 2


def test_ceiling_is_eleven_started_and_twelfth_is_network_zero(tmp_path):
    state_path = prepare(tmp_path)
    payload = state(state_path)
    payload.update(total_started=MAX_ATTEMPTS, last_started_at=NOW.isoformat())
    state_path.write_text(json.dumps(payload))
    os.chmod(state_path, 0o600)
    calls = []
    with pytest.raises(LiveRunError, match="ceiling_exhausted"):
        invoke(tmp_path, lambda request: calls.append(request), NOW + timedelta(seconds=5))
    assert calls == []


@pytest.mark.parametrize("content", [b"{", b"[]", b'{"total_started":0}', b"x" * 20_000])
def test_malformed_state_fails_closed_with_no_network(tmp_path, content):
    state_path = prepare(tmp_path)
    state_path.write_bytes(content)
    os.chmod(state_path, 0o600)
    calls = []
    with pytest.raises(LiveRunError, match="private_state_invalid"):
        invoke(tmp_path, lambda request: calls.append(request))
    assert calls == []


def test_429_persists_simple_bounded_retry_after(tmp_path):
    state_path = prepare(tmp_path)
    with pytest.raises(LiveRunError, match="http_429"):
        invoke(tmp_path, lambda _: _LiveTransportFailure("http_429", 120))
    assert state(state_path)["retry_after_seconds"] == 120
    with pytest.raises(LiveRunError, match="not_due"):
        invoke(tmp_path, lambda _: pytest.fail("network"), NOW + timedelta(seconds=119))


def test_request_repr_and_artifacts_never_contain_key(tmp_path):
    request = build_wti_request(api_key=SECRET)
    assert SECRET not in repr(request)
    state_path = prepare(tmp_path)
    with pytest.raises(LiveRunError):
        invoke(tmp_path, lambda _: _LiveTransportFailure("auth_rejection"))
    artifacts = [path for path in tmp_path.rglob("*") if path.is_file()
                 and path.name != "tokmo.env"]
    assert artifacts
    assert all(SECRET.encode() not in path.read_bytes() for path in artifacts)
    assert SECRET not in state_path.read_text()


def test_live_rejects_non_private_config_before_network(tmp_path):
    prepare(tmp_path)
    os.chmod(tmp_path / ".config" / "tokmo-v3" / "tokmo.env", 0o644)
    calls = []
    with pytest.raises(LiveRunError, match="private_state_invalid"):
        invoke(tmp_path, lambda request: calls.append(request))
    assert calls == []


class Response:
    status = 200

    def __init__(self, body=BODY, headers=None):
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


def test_transport_makes_exact_official_get_once():
    transport = EiaLiveTransport()
    opener = CapturingOpener(Response())
    transport.opener = opener
    outcome = transport(build_wti_request(api_key=SECRET))
    request, timeout = opener.calls[0]

    assert isinstance(outcome, _LiveTransportSuccess)
    assert len(opener.calls) == 1
    assert request.get_method() == "GET"
    assert timeout == REQUEST_TIMEOUT_SECONDS == 10
    assert request.full_url.startswith(f"https://{EIA_HOST}{EIA_PATH}?")
    assert tuple(urllib_parse_pairs(request.full_url)[:-1]) == WTI_QUERY_MULTIMAP
    assert urllib_parse_pairs(request.full_url)[-1] == ("api_key", SECRET)


def urllib_parse_pairs(url):
    import urllib.parse
    return urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)


def test_opener_has_empty_proxy_policy_and_redirects_are_not_followed():
    opener = build_live_opener()
    assert not any(handler.__class__.__name__ == "ProxyHandler"
                   for handler in opener.handlers)
    redirect = urllib.error.HTTPError(
        "masked", 302, "masked", Message(), None)
    transport = EiaLiveTransport()
    transport.opener = CapturingOpener(redirect)
    assert transport(build_wti_request(api_key=SECRET)).code == "redirect"


@pytest.mark.parametrize(
    "response,code",
    [
        (Response(b"x" * (MAX_BODY_BYTES + 1)), "body_too_large"),
        (Response(BODY, Message()), None),
    ],
)
def test_body_limit(response, code):
    if code is None:
        response.headers["Content-Encoding"] = "gzip"
        code = "unsupported_content_encoding"
    transport = EiaLiveTransport()
    transport.opener = CapturingOpener(response)
    assert transport(build_wti_request(api_key=SECRET)).code == code
