"""Hardening tests added during final contract review of the Tracer 1a draft.

Each test either exposed a real gap in the draft (RED before the fix) or pins
contract coverage that the draft implemented but never asserted.
"""
import json
import os
import stat
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from tokmo.facts.eia import HttpResponse, get_wti, parse_wti_response
from tokmo.observability import TracerLogger
from tokmo.programs.tracer import run_tracer
from tokmo.records.decision_journal import DecisionJournal
from tokmo.records.fact_store import FactStore
from tokmo.values import DecisionRecord, SaveStatus, UnavailableFact, WtiFact

NOW = datetime(2026, 7, 27, 1, 2, 3, tzinfo=timezone.utc)
SECRET = "fixture-key"
BODY = b'{"response":{"data":[{"period":"2026-07-24","series":"RWTC","value":"78.420","units":"$/BBL"}]}}'
POLICY = "tracer1-v1"


def fact(value="78.420"):
    return WtiFact("eia_wti_rwtc", "RWTC", date(2026, 7, 24), Decimal(value), "$/BBL", NOW)


def decision_record(decision_id="d-1"):
    return DecisionRecord(
        decision_id, "run", "eia_wti_rwtc:RWTC:2026-07-24", "SAVE", "saved", NOW, NOW, POLICY
    )


# --- secret boundary -------------------------------------------------------

def test_transport_receives_key_separately_from_request_identity():
    seen = []

    def transport(request):
        seen.append(request)
        return HttpResponse(200, BODY, {})

    result = get_wti(api_key=SECRET, transport=transport, clock=lambda: NOW)

    assert isinstance(result, WtiFact)
    request = seen[0]
    assert request.api_key == SECRET
    assert "api_key" not in request.params
    assert SECRET not in repr(request)
    assert SECRET not in str(request)


# --- EIA payload shape hardening -------------------------------------------

@pytest.mark.parametrize("body", [
    b'{"response":{"data":["not-a-row"]}}',
    b'{"response":{"data":[null]}}',
    b'{"response":{"data":"not-a-list"}}',
])
def test_eia_malformed_row_shapes_return_contract_mismatch(body):
    result = parse_wti_response(body, observed_at=NOW)
    assert isinstance(result, UnavailableFact)
    assert result.reason == "contract_mismatch"


# --- fact store fail-closed hardening ---------------------------------------

@pytest.mark.parametrize("payload", [
    '{"records":[{"source":"eia_wti_rwtc"}]}',
    '{"records":["not-a-record"]}',
    '{"records":[null]}',
])
def test_malformed_store_records_fail_closed_with_diagnostic(tmp_path, payload):
    path = tmp_path / "facts.json"
    path.write_text(payload)

    result = FactStore(path, clock=lambda: NOW, id_factory=lambda: "r").save(
        fact(), policy_version=POLICY
    )

    assert result.status is SaveStatus.FAILED
    assert result.reason == "store_corrupt"
    assert (tmp_path / "facts.json.diagnostic.json").exists()
    assert path.read_text() == payload


def test_same_numeric_value_with_different_precision_is_duplicate_not_conflict(tmp_path):
    store = FactStore(tmp_path / "facts.json", clock=lambda: NOW, id_factory=lambda: "r")
    store.save(fact("78.420"), policy_version=POLICY)

    second = store.save(fact("78.42"), policy_version=POLICY)

    assert second.status is SaveStatus.DUPLICATE
    assert json.loads((tmp_path / "facts.json").read_text())["records"][0]["value"] == "78.42"


# --- typed-value allowlists --------------------------------------------------

def test_unavailable_reason_outside_contract_allowlist_is_rejected():
    with pytest.raises(ValueError):
        UnavailableFact("eia_wti_rwtc", "mystery_reason", NOW)


def test_decision_action_outside_save_skip_is_rejected():
    with pytest.raises(ValueError):
        DecisionRecord("d", "run", "ref", "SEND", "saved", NOW, NOW, POLICY)


# --- observability redaction and incident flush ------------------------------

def test_logger_rejects_fields_outside_contract_schema(tmp_path):
    logger = TracerLogger(tmp_path, clock=lambda: NOW)
    with pytest.raises(ValueError):
        logger.info(run_id="run", url="https://example.invalid/?api_key=x")
    assert not (tmp_path / "events.jsonl").exists()


def test_failed_save_emits_error_and_flushes_flight_recorder(tmp_path):
    (tmp_path / "facts.json").write_text("bad")
    logger = TracerLogger(tmp_path / "logs", clock=lambda: NOW)
    logger.debug(
        run_id="run", stage="fixture", action="READ",
        reason="sanitized_fixture", source="eia_wti_rwtc", decision_id=None,
    )

    result = run_tracer(
        fact(),
        store=FactStore(tmp_path / "facts.json", clock=lambda: NOW, id_factory=lambda: "r"),
        journal=DecisionJournal(tmp_path / "d.jsonl"),
        clock=lambda: NOW, id_factory=lambda: "d", run_id="run", logger=logger,
    )

    assert result.save_result.status is SaveStatus.FAILED
    incident = (tmp_path / "logs" / "incident.jsonl").read_text()
    assert "sanitized_fixture" in incident
    assert "store_corrupt" in incident


# --- journal durability -------------------------------------------------------

def test_journal_append_fsyncs_file_and_directory(tmp_path, monkeypatch):
    synced_inodes = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        synced_inodes.append(os.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    DecisionJournal(tmp_path / "d.jsonl").append(decision_record())

    assert (tmp_path / "d.jsonl").stat().st_ino in synced_inodes
    assert tmp_path.stat().st_ino in synced_inodes


# --- coverage pinned during review (draft already satisfied these) -----------

def test_journal_lines_are_each_valid_single_line_json(tmp_path):
    journal = DecisionJournal(tmp_path / "d.jsonl")
    journal.append(decision_record("d-1"))
    journal.append(decision_record("d-2"))

    lines = (tmp_path / "d.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["decision_id"] for line in lines] == ["d-1", "d-2"]


def test_fact_store_journal_and_diagnostic_artifacts_are_private(tmp_path):
    store_path = tmp_path / "facts.json"
    FactStore(store_path, clock=lambda: NOW, id_factory=lambda: "r").save(
        fact(), policy_version=POLICY
    )
    journal_path = tmp_path / "decisions.jsonl"
    DecisionJournal(journal_path).append(decision_record())
    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_text("bad")
    FactStore(corrupt_path, clock=lambda: NOW, id_factory=lambda: "r").save(
        fact(), policy_version=POLICY
    )

    diagnostic_path = tmp_path / "corrupt.json.diagnostic.json"
    for path in (store_path, journal_path, diagnostic_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
