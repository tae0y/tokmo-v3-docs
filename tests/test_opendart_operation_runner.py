import json

import pytest

from tools import opendart_bulk
from tools.run_opendart_operation import main


def test_success_is_silent(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("tools.run_opendart_operation.bulk_main", lambda argv, emit: 0)
    assert main(["--home", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_failure_is_nonzero_and_sanitized_stderr(monkeypatch, tmp_path, capsys):
    secret = "SHOULD_NOT_ESCAPE"
    def fail(argv, emit):
        emit(json.dumps({"status": "failed", "detail": secret}))
        return 2
    monkeypatch.setattr("tools.run_opendart_operation.bulk_main", fail)
    assert main(["--home", str(tmp_path)]) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"status":"failed"}\n'


@pytest.mark.parametrize("status", ["failed", "overflow", "incomplete",
                                     "skipped_overlap"])
def test_operate_noncomplete_is_nonzero(monkeypatch, tmp_path, capsys, status):
    monkeypatch.setattr(opendart_bulk, "execute_opendart_bulk", lambda **_kwargs: {
        "status": status, "run_id": "safe", "kind": "incremental",
        "begin": "20260803", "end": "20260803", "planned_pages": 1,
        "fetched_pages": 0, "new": 0, "duplicate": 0, "conflict": 0,
        "revision": 0,
    })
    assert opendart_bulk.main(["operate", "--home", str(tmp_path)]) == 2
    assert "failed" in capsys.readouterr().out


def test_exit_code_ignores_a_failed_enrichment_on_a_complete_bulk_tick(
        monkeypatch, tmp_path, capsys):
    """SDS §8: the runner exit code is a function of the bulk status only."""
    result = {"status": "complete", "partitions": [["20260903", "complete"]],
              "enrichment": {"status": "error"}}
    monkeypatch.setattr(opendart_bulk, "execute_opendart_bulk",
                        lambda **_kwargs: result)
    assert opendart_bulk.main(["operate", "--home", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["enrichment"] == {
        "status": "error"}
    assert main(["--home", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_direct_operate_success_emits_sanitized_aggregate(monkeypatch, tmp_path, capsys):
    result = {"status": "complete", "run_id": "safe", "kind": "incremental",
              "begin": "20260803", "end": "20260803", "planned_pages": 1,
              "fetched_pages": 1, "new": 2, "duplicate": 3, "conflict": 0,
              "revision": 0}
    monkeypatch.setattr(opendart_bulk, "execute_opendart_bulk",
                        lambda **_kwargs: result)
    assert opendart_bulk.main(["operate", "--home", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == result


@pytest.mark.parametrize("wrapper", ["bulk", "operation", "pilot", "sec"])
def test_collection_wrappers_keep_busy_cause_and_elapsed(tmp_path, monkeypatch, capsys, wrapper):
    from tokmo.records.private_sqlite import ImmediateTransactionTimeout
    from tools import run_opendart_pilot, run_sec_targeted_operation
    def fail(**kwargs):
        raise ImmediateTransactionTimeout("token=SECRET query=PRIVATE body=HIDDEN")
    monkeypatch.setattr(opendart_bulk, "execute_opendart_bulk", fail)
    monkeypatch.setattr(run_sec_targeted_operation, "run_sec_targeted_operation", fail)
    call, args = {
        "bulk": (opendart_bulk.main, ["operate"]), "operation": (main, []),
        "pilot": (run_opendart_pilot.main, []), "sec": (run_sec_targeted_operation.main, []),
    }[wrapper]
    assert call([*args, "--home", str(tmp_path)]) != 0
    captured = capsys.readouterr()
    text = captured.err or captured.out
    diagnostics = json.loads(text)["diagnostics"]
    assert diagnostics["cause"] == "STORAGE_BUSY"
    assert diagnostics["stage"] == "OPERATION"
    assert diagnostics["elapsed_ms"] >= 0
    assert diagnostics["received"] is None and diagnostics["persisted"] is None
    assert all(value not in text for value in ("SECRET", "PRIVATE", "HIDDEN", "Traceback"))


@pytest.mark.parametrize("outcome", ["partial", "error"])
def test_dart_today_partial_is_normal_but_exception_fails(tmp_path, monkeypatch, capsys, outcome):
    from types import SimpleNamespace
    from tools import run_news_disclosure_today as tool
    from tokmo.records.private_sqlite import prepare_private_path
    actual = tmp_path / "actual-data"
    actual.mkdir(mode=0o700)
    legacy = tmp_path / ".local/share/tokmo-v3"
    legacy.parent.mkdir(parents=True)
    legacy.symlink_to(actual, target_is_directory=True)
    opened = []
    def open_store(path):
        opened.append(path)
        assert not prepare_private_path(path).unsafe
        return SimpleNamespace(partitions_migrated=lambda: True, close=lambda: None)
    monkeypatch.setattr(tool, "read_decision_config", lambda root: {"enabled": True})
    monkeypatch.setattr(tool, "_require_private_directory", lambda path: None)
    monkeypatch.setattr(tool, "_require_private_credential", lambda path: None)
    monkeypatch.setattr(tool, "load_opendart_config", lambda path: SimpleNamespace(api_key="fixture"))
    monkeypatch.setattr(tool.DisclosureStore, "open", open_store)
    def collect(*args, **kwargs):
        if outcome == "error":
            raise RuntimeError("token=SECRET body=HIDDEN")
        return outcome
    monkeypatch.setattr(tool, "_collect_day", collect)
    assert tool.main(["--home", str(tmp_path)]) == (0 if outcome == "partial" else 2)
    assert opened == [actual / "opendart-bulk.sqlite3"]
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    if outcome == "partial":
        assert payload == {"source": "DART_TODAY", "status": "partial"}
    else:
        assert payload["status"] == "failed"
        assert payload["diagnostics"]["cause"] == "UNKNOWN"
    assert "SECRET" not in captured.out + captured.err


@pytest.mark.parametrize("reason,cause", [("http_5xx", "HTTP_5XX"), ("timeout", "TIMEOUT"),
    ("provider_020", "PROVIDER_020"), ("token=SECRET", "UNKNOWN")])
def test_dart_typed_failures_keep_only_closed_provider_reason(tmp_path, monkeypatch, capsys, reason, cause):
    from tokmo.programs.opendart_pilot import PilotError
    def fail(**kwargs):
        raise PilotError(reason)
    monkeypatch.setattr(opendart_bulk, "execute_opendart_bulk", fail)
    assert main(["--home", str(tmp_path)]) == 2
    text = capsys.readouterr().err
    assert json.loads(text)["diagnostics"]["cause"] == cause
    assert "SECRET" not in text


def test_dart_partial_page_failure_has_durable_progress(tmp_path, monkeypatch, capsys):
    from test_opendart_partitions import open_store, NOW, day_pages
    from tokmo.facts.opendart_bulk import parse_page
    from datetime import date, timedelta
    from tokmo.programs.opendart_pilot import _collect_day
    db = open_store(tmp_path)
    day = date(2026, 7, 1)
    db.migrate_partitions(day, NOW)
    body = day_pages("20260701", 1)[0][1]
    page = parse_page(body, 1)
    observe = db.observe
    def save(run_id, item, now):
        if item == page.rows[1]:
            raise RuntimeError("token=SECRET")
        return observe(run_id, item, now)
    monkeypatch.setattr(db, "observe", save)
    monkeypatch.setattr(opendart_bulk, "execute_opendart_bulk", lambda **kw:
        _collect_day(db, day, today=day, api_key="fixture", transport=lambda *a: body,
                     now=NOW, clock=lambda: NOW, deadline=NOW + timedelta(minutes=2)))
    assert main(["--home", str(tmp_path)]) == 2
    diagnostics = json.loads(capsys.readouterr().err)["diagnostics"]
    assert diagnostics["stage"] == "PERSIST"
    assert diagnostics["received"] == 100 and diagnostics["persisted"] == 1
    assert diagnostics["progress_scope"] == "page"
    assert db.connection.execute("SELECT status,new_count FROM runs").fetchall() == [("failed", 1)]
    db.close()


def test_dart_noncomplete_result_survives_operation_wrapper(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(opendart_bulk, "execute_opendart_bulk", lambda **kw: {
        "status": "incomplete", "new": 5, "duplicate": 2, "conflict": 0, "revision": 1})
    assert main(["--home", str(tmp_path)]) == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["outcome"] == "incomplete"
    assert payload["diagnostics"]["cause"] == "INCOMPLETE_STORAGE"
    assert payload["diagnostics"]["persisted"] == 8
