import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from test_kis_etf_consumers import saved

from tokmo.programs.kis_etf_refresh import refresh_etf_inputs
from tokmo.values.kis_etf import KisError

AT = datetime(2026, 9, 11, 1, 0, tzinfo=UTC)


def config(root):
    (root / "kis").mkdir(mode=0o700, exist_ok=True)
    (root / "kis/integration.json").write_text(
        json.dumps(
            dict(
                enabled=True,
                symbols=["251600", "0138Y0"],
                credential_project_root="/private/project",
            )
        )
    )


def factory(calls, *, fail=False):
    def build(**kwargs):
        assert kwargs["max_requests"] <= 5
        client = SimpleNamespace(requests=0, auth_requests=0)

        def fetch(operation, symbol):
            calls.append((operation, symbol))
            client.requests += 1
            if fail:
                raise KisError("provider_rejected")
            return {"status": "OK"}

        client.fetch = fetch
        return client

    return build


def test_missing_configuration_and_offhours_do_not_construct_client(tmp_path):
    def no(**kwargs):
        raise AssertionError("no network")

    assert (
        refresh_etf_inputs(tmp_path, at=AT, client_factory=no)["status"] == "DISABLED"
    )
    config(tmp_path)
    assert (
        refresh_etf_inputs(tmp_path, at=AT + timedelta(hours=10), client_factory=no)[
            "status"
        ]
        == "OUTSIDE_POLL_WINDOW"
    )


def test_success_and_failure_both_consume_the_same_poll_window(tmp_path):
    config(tmp_path)
    calls = []
    first = refresh_etf_inputs(
        tmp_path, at=AT, client_factory=factory(calls, fail=True)
    )
    assert first["status"] == "DEGRADED" and len(calls) == 1
    assert (
        refresh_etf_inputs(
            tmp_path, at=AT + timedelta(minutes=1), client_factory=factory(calls)
        )["status"]
        == "CACHED"
    )
    assert len(calls) == 1
    # Authentication/provider failure is not retried by subsequent minute ticks.
    later = refresh_etf_inputs(
        tmp_path, at=AT + timedelta(minutes=5), client_factory=factory(calls)
    )
    assert later["requests"] == 2  # NAV next interval; components wait their own day.


def test_fresh_observation_reused_without_issuing_token(tmp_path):
    config(tmp_path)
    saved(tmp_path, "251600", at=AT)
    saved(tmp_path, "0138Y0", at=AT)
    calls = []
    result = refresh_etf_inputs(tmp_path, at=AT, client_factory=factory(calls))
    assert result["status"] == "CACHED" and not calls


def test_shared_refresh_lock_prevents_duplicate_clients(tmp_path):
    from tokmo.records.kis_state import KisState

    config(tmp_path)
    (tmp_path / "kis").chmod(0o700)
    with KisState(tmp_path / "kis").locked("refresh"):
        result = refresh_etf_inputs(tmp_path, at=AT, client_factory=factory([]))
    assert result["status"] == "BUSY"


def test_corrupt_attempt_state_is_not_a_new_budget(tmp_path):
    config(tmp_path)
    (tmp_path / "kis/refresh.json").write_text("{bad")
    result = refresh_etf_inputs(tmp_path, at=AT, client_factory=factory([]))
    assert result["status"] == "DEGRADED" and result["requests"] == 0


def test_disabled_config_rejects_expanded_symbols_and_secret_keys(tmp_path):
    config(tmp_path)
    path = tmp_path / "kis/integration.json"
    value = json.loads(path.read_text())
    value["app_secret"] = "do-not-accept"
    path.write_text(json.dumps(value))
    assert (
        refresh_etf_inputs(tmp_path, at=AT, client_factory=factory([]))["status"]
        == "DEGRADED"
    )


def test_repeated_success_has_no_network_in_same_window(tmp_path):
    config(tmp_path)
    calls = []
    first = refresh_etf_inputs(tmp_path, at=AT, client_factory=factory(calls))
    assert first["status"] == "UPDATED" and len(calls) == 4
    assert (
        refresh_etf_inputs(
            tmp_path, at=AT + timedelta(seconds=1), client_factory=factory(calls)
        )["status"]
        == "CACHED"
    )
    assert len(calls) == 4
    state = json.loads((tmp_path / "kis/refresh.json").read_text())
    assert state["total_requests"] == 4 and state["last_run"]["request_cap"] == 5


def test_runtime_entrypoints_use_small_kis_composition():
    import ast

    project = Path(__file__).parents[1]
    for name in [
        "tools/run_news_decision_worker.py",
        "tools/run_quant_decisions.py",
        "tools/check_kis_etf.py",
    ]:
        imports = [
            n
            for n in ast.walk(ast.parse((project / name).read_text()))
            if isinstance(n, ast.ImportFrom)
        ]
        assert any(n.module == "tokmo.kis_composition" for n in imports)
        assert not any(
            n.module == "tokmo.composition"
            and any(
                a.name in {"refresh_kis_etf_inputs", "build_kis_etf_client"}
                for a in n.names
            )
            for n in imports
        )
