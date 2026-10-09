import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tokmo import composition
from tokmo.composition import (
    KRX_MAX_STARTS,
    KRX_STATE_NAME,
    LiveRunError,
    run_krx,
    setup_krx,
)
from tokmo.facts.krx import (
    _KrxTransportFailure,
    _KrxTransportSuccess,
    build_daily_request,
    parse_daily_trade,
    query_daily_trade,
)
from tokmo.facts.krx_live import KRX_HOST, KRX_PATH, KrxLiveTransport
from tokmo.records.fact_store import FactStore
from tokmo.runtime.config import ConfigError, load_krx_config
from tokmo.values import KrxDailyTradeFact, SaveStatus, UnavailableFact

BODY = (Path(__file__).parent / "fixtures" / "krx_daily_success.json").read_bytes()
NOW = datetime(2026, 8, 3, 3, tzinfo=UTC)
KEY = "fixture-krx-secret"


class Response:
    def __init__(self, body=BODY, status=200, encoding=None):
        self.body, self.status, self.encoding = body, status, encoding

    def read(self, size):
        return self.body[:size]

    def getheader(self, name):
        return self.encoding if name == "Content-Encoding" else None


class Connection:
    def __init__(self, response=Response()):
        self.response, self.requests, self.getresponses, self.closed = (
            response,
            [],
            0,
            False,
        )

    def request(self, method, path, *, headers):
        self.requests.append((method, path, headers))

    def getresponse(self):
        self.getresponses += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self):
        self.closed = True


class ConnectionFactory:
    def __init__(self, connection):
        self.connection, self.calls = connection, []

    def __call__(self, host, *, timeout):
        self.calls.append((host, timeout))
        return self.connection


def prepare(home):
    setup_krx(home=home)
    path = home / ".config/tokmo-v3/krx.env"
    path.write_text(f"KRX_API_KEY={KEY}\n")
    os.chmod(path, 0o600)
    return home / ".config/tokmo-v3" / KRX_STATE_NAME


def invoke(home, transport, now=NOW):
    ids = iter(["record", "decision"])
    return run_krx(
        home=home,
        base_date="20260731",
        transport=transport,
        clock=lambda: now,
        id_factory=ids.__next__,
        run_id="run",
    )


def test_parser_selects_exact_target_and_types_fields():
    fact = parse_daily_trade(BODY, requested_date="20260731", observed_at=NOW)
    assert isinstance(fact, KrxDailyTradeFact)
    assert fact.issue_code == "000660" and fact.close_price.as_tuple().exponent == 0
    assert fact.sector_type_name == ""
    assert fact.accumulated_volume == 3123456
    assert KEY not in repr(fact)


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "date", "market", "numeric", "schema"]
)
def test_parser_fails_closed(mutation):
    payload = json.loads(BODY)
    target = payload["OutBlock_1"][0]
    if mutation == "missing":
        payload["OutBlock_1"] = payload["OutBlock_1"][1:]
    elif mutation == "duplicate":
        payload["OutBlock_1"].append(dict(target))
    elif mutation == "date":
        target["BAS_DD"] = "20260730"
    elif mutation == "market":
        target["MKT_NM"] = "KOSDAQ"
    elif mutation == "numeric":
        target["MKTCAP"] = "not-a-number"
    else:
        target["EXTRA"] = "x"
    result = parse_daily_trade(
        json.dumps(payload).encode(), requested_date="20260731", observed_at=NOW
    )
    assert isinstance(result, UnavailableFact)


def test_parser_ignores_non_dict_and_malformed_unrelated_rows():
    payload = json.loads(BODY)
    payload["OutBlock_1"] = [
        None,
        {"ISU_CD": "005930"},
        payload["OutBlock_1"][0],
        "bad",
    ]
    result = parse_daily_trade(
        json.dumps(payload).encode(), requested_date="20260731", observed_at=NOW
    )
    assert isinstance(result, KrxDailyTradeFact)


def test_parser_counts_exact_target_duplicates_before_strict_validation():
    payload = json.loads(BODY)
    payload["OutBlock_1"] = [{"ISU_CD": "000660"}, payload["OutBlock_1"][0]]
    result = parse_daily_trade(
        json.dumps(payload).encode(), requested_date="20260731", observed_at=NOW
    )
    assert isinstance(result, UnavailableFact) and result.reason == "contract_mismatch"


def test_empty_outblock_is_provider_no_data():
    result = parse_daily_trade(
        b'{"OutBlock_1":[]}', requested_date="20260731", observed_at=NOW
    )
    assert isinstance(result, UnavailableFact) and result.reason == "no_published_data"
    query = query_daily_trade(
        base_date="20260731",
        api_key=KEY,
        transport=lambda request: _KrxTransportSuccess(b'{"OutBlock_1":[]}'),
        clock=lambda: NOW,
    )
    assert query.attempt.code == "provider_no_data"


def test_wire_is_exact_one_get_header_only_and_no_proxy_redirect_retry():
    connection = Connection()
    factory = ConnectionFactory(connection)
    transport = KrxLiveTransport(connection_factory=factory)
    result = transport(build_daily_request(base_date="20260731", api_key=KEY))
    method, path, headers = connection.requests[0]
    assert isinstance(result, _KrxTransportSuccess) and factory.calls == [
        (KRX_HOST, 10)
    ]
    assert method == "GET" and path == f"{KRX_PATH}?basDd=20260731"
    assert (
        list(headers) == ["AUTH_KEY", "Accept-Encoding"] and headers["AUTH_KEY"] == KEY
    )
    assert (
        len(connection.requests) == 1
        and connection.getresponses == 1
        and connection.closed
    )
    assert KEY not in path and KEY not in repr(
        build_daily_request(base_date="20260731", api_key=KEY)
    )


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "auth_rejection"),
        (403, "auth_rejection"),
        (429, "http_429"),
        (503, "http_5xx"),
    ],
)
def test_closed_http_outcomes(status, code):
    transport = KrxLiveTransport(
        connection_factory=ConnectionFactory(Connection(Response(status=status)))
    )
    assert (
        transport(build_daily_request(base_date="20260731", api_key=KEY)).code == code
    )


def test_krx_same_market_content_dedups_across_observation_and_policy_metadata(
    tmp_path,
):
    fact = parse_daily_trade(BODY, requested_date="20260731", observed_at=NOW)
    ids = iter(["first", "unused"])
    store = FactStore(
        tmp_path / "facts.json", clock=lambda: NOW, id_factory=ids.__next__
    )
    assert store.save(fact, policy_version="v1").status is SaveStatus.SAVED
    repeated = parse_daily_trade(
        BODY, requested_date="20260731", observed_at=NOW + timedelta(hours=1)
    )
    assert store.save(repeated, policy_version="v2").status is SaveStatus.DUPLICATE


def test_config_is_private_and_rejects_missing_empty_placeholder(tmp_path):
    path = tmp_path / "krx.env"
    with pytest.raises(ConfigError, match="config_missing"):
        load_krx_config(path)
    for text, reason in [
        ("OTHER=x\n", "config_missing"),
        ("KRX_API_KEY=\n", "config_invalid"),
        ("KRX_API_KEY=replace-me\n", "config_invalid"),
    ]:
        path.write_text(text)
        with pytest.raises(ConfigError, match=reason):
            load_krx_config(path)
    path.write_text(f"KRX_API_KEY={KEY}\n")
    assert KEY not in repr(load_krx_config(path))


def test_krx_setup_cli_sanitizes_type_error(monkeypatch, tmp_path, capsys):
    def broken_setup(*, home):
        raise TypeError("must not escape")

    monkeypatch.setattr(composition, "setup_krx", broken_setup)
    assert composition.main(["krx-setup", "--home", str(tmp_path)]) == 1
    assert capsys.readouterr().out == "krx-tracer setup=private_state_invalid\n"


def test_campaign_write_before_io_spacing_stop_and_save(tmp_path):
    state_path = prepare(tmp_path)

    def transport(_request):
        state = json.loads(state_path.read_text())
        assert state["total_started"] == 1 and state["last_outcome"] == "started"
        return _KrxTransportSuccess(BODY)

    result = invoke(tmp_path, transport)
    assert result.decision_action == "SAVE" and result.fact.issue_code == "000660"
    assert (tmp_path / ".local/share/tokmo-v3/krx_facts.json").exists()
    with pytest.raises(LiveRunError, match="not_due"):
        invoke(tmp_path, transport, NOW + timedelta(seconds=1))
    state = json.loads(state_path.read_text())
    state.update(
        total_started=KRX_MAX_STARTS,
        last_started_at=(NOW - timedelta(seconds=3)).isoformat(),
    )
    state_path.write_text(json.dumps(state))
    os.chmod(state_path, 0o600)
    with pytest.raises(LiveRunError, match="ceiling_exhausted"):
        invoke(tmp_path, transport)


def test_weekend_rejected_before_network(tmp_path):
    prepare(tmp_path)
    calls = []
    with pytest.raises(LiveRunError, match="invalid_date"):
        run_krx(
            home=tmp_path,
            base_date="20260802",
            transport=lambda r: calls.append(r),
            clock=lambda: NOW,
            id_factory=lambda: "x",
            run_id="run",
        )
    assert calls == []


def test_auth_rejection_persists_stop_and_next_run_makes_zero_transport_calls(tmp_path):
    state_path = prepare(tmp_path)
    calls = []

    def rejected(request):
        calls.append(request)
        return _KrxTransportFailure("auth_rejection")

    with pytest.raises(LiveRunError, match="auth_rejection"):
        invoke(tmp_path, rejected)
    assert json.loads(state_path.read_text())["last_outcome"] == "auth_rejection"
    assert json.loads(state_path.read_text())["total_started"] == 1
    with pytest.raises(LiveRunError, match="fair_access_stop"):
        invoke(tmp_path, rejected, NOW + timedelta(hours=1))
    assert len(calls) == 1


def test_documented_reset_is_manual_validated_and_does_not_happen_automatically(
    tmp_path,
):
    runbook = (Path(__file__).parents[1] / "KRX_RUNBOOK.md").read_text()
    assert (
        "backup" in runbook and "last_outcome" in runbook and "total_started" in runbook
    )
    state_path = prepare(tmp_path)
    state = json.loads(state_path.read_text())
    state.update(
        last_outcome="auth_rejection",
        total_started=1,
        last_started_at=NOW.isoformat(),
        requested_base_date="20260731",
    )
    state_path.write_text(json.dumps(state))
    os.chmod(state_path, 0o600)
    calls = []
    with pytest.raises(LiveRunError, match="fair_access_stop"):
        invoke(
            tmp_path, lambda request: calls.append(request), NOW + timedelta(hours=1)
        )
    assert calls == [] and json.loads(state_path.read_text()) == state


def test_krx_transport_declares_no_write_method():
    source = (Path(__file__).parents[1] / "src/tokmo/facts/krx_live.py").read_text()
    for method in (
        'method="POST"',
        'method="PUT"',
        'method="PATCH"',
        'method="DELETE"',
    ):
        assert method not in source
