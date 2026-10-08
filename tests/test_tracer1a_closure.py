"""Closure tests for the independent (SOL) review of Tracer 1a.

Each section pins one MATERIAL finding's resolution:
- M3: numeric semantic equality + deterministic canonical decimal serialization
- M4: full-schema semantic validation of persisted records, fail-closed
- M5: logger requires the exact caller fields and rejects prohibited content
- M6: flight recorder drains on successful flush, preserves buffer on failure
- N3: store-success/journal-failure surfaces a typed fail-closed program error
- plus OS config load-error secrecy and same-value/different-units conflict
"""
import json
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from tokmo.facts.eia import parse_wti_response
from tokmo.observability import TracerLogger
from tokmo.programs.tracer import AuditJournalError, run_tracer
from tokmo.records.fact_store import FactStore
from tokmo.runtime.config import ConfigError, load_config
from tokmo.values import SaveStatus, UnavailableFact, WtiFact, canonical_decimal

NOW = datetime(2026, 7, 27, 1, 2, 3, tzinfo=timezone.utc)
POLICY = "tracer1-v1"


def fact(value="78.420", *, period=date(2026, 7, 24), units="$/BBL"):
    return WtiFact("eia_wti_rwtc", "RWTC", period, Decimal(value), units, NOW)


def make_store(root, record_id="rec-1"):
    return FactStore(root / "facts.json", clock=lambda: NOW, id_factory=lambda: record_id)


def stored_records(root):
    return json.loads((root / "facts.json").read_text())["records"]


# --- M3: canonical decimal serialization -------------------------------------

def test_canonical_decimal_is_scale_and_exponent_independent():
    assert canonical_decimal(Decimal("78.420")) == "78.42"
    assert canonical_decimal(Decimal("78.42")) == "78.42"
    assert canonical_decimal(Decimal("7.842E+1")) == "78.42"
    assert canonical_decimal(Decimal("100")) == "100"
    assert canonical_decimal(Decimal("1E+2")) == "100"
    assert canonical_decimal(Decimal("-78.420")) == "-78.42"
    assert canonical_decimal(Decimal("0.00")) == "0"


@pytest.mark.parametrize("raw,expected", [
    ("1E-7", "0.0000001"),
    ("1.2300E-7", "0.000000123"),
    ("1E-20", "0.00000000000000000001"),
    ("1E+100", "1" + "0" * 100),
    ("-0", "0"),
    ("-0.000E+999999", "0"),
])
def test_canonical_decimal_expands_boundary_exponents_and_signed_zero(raw, expected):
    assert canonical_decimal(Decimal(raw)) == expected


def test_canonical_decimal_is_independent_of_decimal_context_precision():
    value = Decimal("123456789.123456789000")

    with localcontext() as context:
        context.prec = 3
        rendered = canonical_decimal(value)

    assert rendered == "123456789.123456789"


@pytest.mark.parametrize("first,second,expected", [
    ("1E-20", "0.0000000000000000000100", "0.00000000000000000001"),
    ("0.0000000000000000000100", "1E-20", "0.00000000000000000001"),
    ("1E+100", "10E+99", "1" + "0" * 100),
    ("10E+99", "1E+100", "1" + "0" * 100),
    ("-0", "0E+100", "0"),
    ("0E+100", "-0", "0"),
])
def test_boundary_decimal_storage_is_insertion_order_independent(
        tmp_path, first, second, expected):
    store = make_store(tmp_path)

    initial = store.save(fact(first), policy_version=POLICY)
    repeat = store.save(fact(second), policy_version=POLICY)

    assert (initial.status, repeat.status) == (SaveStatus.SAVED, SaveStatus.DUPLICATE)
    assert stored_records(tmp_path)[0]["value"] == expected


def test_canonical_decimal_rejects_non_finite_values():
    with pytest.raises(ValueError):
        canonical_decimal(Decimal("NaN"))
    with pytest.raises(ValueError):
        canonical_decimal(Decimal("Infinity"))


@pytest.mark.parametrize("first,second", [("78.420", "78.42"), ("78.42", "78.420")])
def test_stored_value_is_canonical_regardless_of_insertion_order(tmp_path, first, second):
    store = make_store(tmp_path)

    initial = store.save(fact(first), policy_version=POLICY)
    repeat = store.save(fact(second), policy_version=POLICY)

    assert (initial.status, repeat.status) == (SaveStatus.SAVED, SaveStatus.DUPLICATE)
    records = stored_records(tmp_path)
    assert len(records) == 1
    assert records[0]["value"] == "78.42"


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_eia_non_finite_values_are_parse_error(raw):
    body = json.dumps({"response": {"data": [
        {"period": "2026-07-24", "series": "RWTC", "value": raw, "units": "$/BBL"},
    ]}}).encode()

    result = parse_wti_response(body, observed_at=NOW)

    assert isinstance(result, UnavailableFact)
    assert result.reason == "parse_error"


# --- M4: full-schema semantic validation, fail-closed -------------------------

def stored_record(**overrides):
    record = {
        "record_id": "rec-0",
        "source": "eia_wti_rwtc",
        "series": "RWTC",
        "period": "2026-07-24",
        "value": "78.42",
        "units": "$/BBL",
        "observed_at": "2026-07-27T01:02:03+00:00",
        "stored_at": "2026-07-27T01:02:03+00:00",
        "policy_version": POLICY,
    }
    record.update(overrides)
    return record


@pytest.mark.parametrize("overrides", [
    {"record_id": ""},
    {"source": ""},
    {"source": "somewhere_else"},
    {"series": "BAD"},
    {"units": "BBL"},
    {"period": "not-a-date"},
    {"value": "not-a-decimal"},
    {"value": "NaN"},
    {"value": "78.420"},  # numerically fine but not the canonical serialization
    {"observed_at": "yesterday"},
    {"stored_at": ""},
    {"policy_version": ""},
])
def test_semantically_corrupt_full_schema_record_fails_closed(tmp_path, overrides):
    path = tmp_path / "facts.json"
    payload = json.dumps({"records": [stored_record(**overrides)]})
    path.write_text(payload)

    result = make_store(tmp_path).save(fact(period=date(2026, 7, 25)), policy_version=POLICY)

    assert result.status is SaveStatus.FAILED
    assert result.reason == "store_corrupt"
    assert (tmp_path / "facts.json.diagnostic.json").exists()
    assert path.read_text() == payload


def test_valid_full_schema_record_still_loads(tmp_path):
    path = tmp_path / "facts.json"
    path.write_text(json.dumps({"records": [stored_record()]}))

    result = make_store(tmp_path, record_id="rec-2").save(
        fact(period=date(2026, 7, 25)), policy_version=POLICY
    )

    assert result.status is SaveStatus.SAVED
    assert [record["record_id"] for record in stored_records(tmp_path)] == ["rec-0", "rec-2"]


def test_same_value_different_units_is_conflict_and_preserves_original(tmp_path):
    store = make_store(tmp_path)
    store.save(fact(), policy_version=POLICY)

    result = store.save(fact(units="USD/bbl"), policy_version=POLICY)

    assert result.status is SaveStatus.CONFLICT
    assert result.reason == "identity_conflict"
    records = stored_records(tmp_path)
    assert len(records) == 1
    assert records[0]["units"] == "$/BBL"
    assert records[0]["value"] == "78.42"


# --- M5: logger field completeness and value secrecy --------------------------

LOG_FIELDS = ("run_id", "stage", "action", "reason", "source", "decision_id")


def log_fields(**overrides):
    fields = {
        "run_id": "run-1", "stage": "decision", "action": "SAVE",
        "reason": "saved", "source": "eia_wti_rwtc", "decision_id": "dec-1",
    }
    fields.update(overrides)
    return fields


@pytest.mark.parametrize("missing", LOG_FIELDS)
def test_logger_rejects_every_missing_required_field(tmp_path, missing):
    logger = TracerLogger(tmp_path, clock=lambda: NOW)
    fields = log_fields()
    fields.pop(missing)

    with pytest.raises(ValueError):
        logger.info(**fields)

    assert not (tmp_path / "events.jsonl").exists()


PROHIBITED_VALUES = [
    "https://api.example.invalid/v2?api_key=s3cr3t",
    "http://example.invalid",
    "host?query=1",
    "a&b",
    "key=value",
    "API_KEY",
    "an-apikey-here",
    "auth-token",
    "client-secret",
    "Bearer abc",
    "password1",
    "credential-blob",
]


@pytest.mark.parametrize("field", LOG_FIELDS)
@pytest.mark.parametrize("value", PROHIBITED_VALUES)
def test_logger_rejects_prohibited_content_in_every_field(tmp_path, field, value):
    logger = TracerLogger(tmp_path, clock=lambda: NOW)

    with pytest.raises(ValueError) as caught:
        logger.info(**log_fields(**{field: value}))

    assert value not in str(caught.value)
    assert not (tmp_path / "events.jsonl").exists()


def test_debug_buffer_applies_the_same_field_and_content_policy(tmp_path):
    logger = TracerLogger(tmp_path, clock=lambda: NOW)

    with pytest.raises(ValueError):
        logger.debug(run_id="run-1")
    with pytest.raises(ValueError):
        logger.debug(**log_fields(reason="https://example.invalid/?api_key=x"))

    assert len(logger.buffer) == 0


def test_logger_rejects_non_string_values_except_none_decision_id(tmp_path):
    logger = TracerLogger(tmp_path, clock=lambda: NOW)

    with pytest.raises(ValueError):
        logger.info(**log_fields(reason=123))
    logger.info(**log_fields(decision_id=None))

    events = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(events) == 1


# --- M6: flight recorder drain/preserve lifecycle -----------------------------

def test_successful_flush_drains_and_incidents_stay_independent(tmp_path):
    logger = TracerLogger(tmp_path, clock=lambda: NOW)
    logger.debug(**log_fields(stage="first", action="A"))
    logger.error(**log_fields(stage="first", action="FAIL"))
    logger.debug(**log_fields(stage="second", action="B"))
    logger.error(**log_fields(stage="second", action="FAIL"))

    lines = [json.loads(line) for line in (tmp_path / "incident.jsonl").read_text().splitlines()]

    assert [(line["stage"], line["action"]) for line in lines] == [
        ("first", "A"), ("first", "FAIL"),
        ("second", "B"), ("second", "FAIL"),
    ]
    assert len(logger.buffer) == 0


def test_failed_flush_preserves_buffer_for_the_next_attempt(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("")  # a file where the log directory should be
    logger = TracerLogger(blocker / "logs", clock=lambda: NOW)
    logger.debug(**log_fields(stage="pre", action="A"))

    with pytest.raises(OSError):
        logger.flush_rca("run-1")

    assert len(logger.buffer) == 1

    logger.root = tmp_path / "logs"
    logger.flush_rca("run-1")

    rca = (tmp_path / "logs" / "rca.jsonl").read_text()
    assert '"stage":"pre"' in rca
    assert len(logger.buffer) == 0


def test_partial_flush_retry_does_not_duplicate_persisted_prefix(tmp_path, monkeypatch):
    logger = TracerLogger(tmp_path / "logs", clock=lambda: NOW)
    logger.debug(**log_fields(stage="first", action="A"))
    logger.debug(**log_fields(stage="second", action="B"))
    real_append = logger._append
    incident_writes = 0

    def fail_after_one_incident_write(name, event):
        nonlocal incident_writes
        if name == "rca.jsonl":
            incident_writes += 1
            if incident_writes == 2:
                raise OSError("injected partial flush failure")
        real_append(name, event)

    monkeypatch.setattr(logger, "_append", fail_after_one_incident_write)
    with pytest.raises(OSError):
        logger.flush_rca("run-1")

    monkeypatch.setattr(logger, "_append", real_append)
    logger.flush_rca("run-1")

    lines = [json.loads(line) for line in
             (tmp_path / "logs" / "rca.jsonl").read_text().splitlines()]
    assert [line["stage"] for line in lines] == ["first", "second", "rca"]
    assert len(logger.buffer) == 0


# --- N3 / closure 7: store-success + journal-failure is typed and fail-closed -

class ExplodingJournal:
    def append(self, record):
        raise OSError("append failed")


def test_journal_failure_after_save_raises_typed_error_and_logs(tmp_path):
    logger = TracerLogger(tmp_path / "logs", clock=lambda: NOW)

    with pytest.raises(AuditJournalError) as caught:
        run_tracer(
            fact(), store=make_store(tmp_path), journal=ExplodingJournal(),
            clock=lambda: NOW, id_factory=lambda: "dec-1", run_id="run-1",
            logger=logger,
        )

    # The saved fact is real and must not be silently disowned...
    assert stored_records(tmp_path)[0]["value"] == "78.42"
    # ...but the program surfaces the incomplete audit instead of a normal result.
    events = (tmp_path / "logs" / "events.jsonl").read_text()
    assert "journal_append_failed" in events
    assert '"level":"ERROR"' in events
    assert "78.42" not in str(caught.value)


def test_journal_failure_without_logger_still_raises_typed_error(tmp_path):
    with pytest.raises(AuditJournalError):
        run_tracer(
            UnavailableFact("eia_wti_rwtc", "no_published_data", NOW),
            store=make_store(tmp_path), journal=ExplodingJournal(),
            clock=lambda: NOW, id_factory=lambda: "dec-1", run_id="run-1",
        )

    assert not (tmp_path / "facts.json").exists()


def test_journal_failure_is_not_masked_by_logger_validation_failure(tmp_path):
    logger = TracerLogger(tmp_path / "logs", clock=lambda: NOW)

    with pytest.raises(AuditJournalError) as caught:
        run_tracer(
            fact(), store=make_store(tmp_path), journal=ExplodingJournal(),
            clock=lambda: NOW, id_factory=lambda: "dec-1",
            run_id="https://prohibited.invalid", logger=logger,
        )

    assert str(caught.value) == "journal_append_failed"


def test_journal_failure_is_not_masked_by_logger_io_failure(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    logger = TracerLogger(blocker / "logs", clock=lambda: NOW)

    with pytest.raises(AuditJournalError) as caught:
        run_tracer(
            fact(), store=make_store(tmp_path), journal=ExplodingJournal(),
            clock=lambda: NOW, id_factory=lambda: "dec-1", run_id="run-1",
            logger=logger,
        )

    assert str(caught.value) == "journal_append_failed"


# --- config OS load-error secrecy ---------------------------------------------

def test_config_os_load_error_is_typed_and_leaks_nothing(tmp_path):
    path = tmp_path / "tracer.env"
    path.write_text("MODE=tracer\nEIA_API_KEY=super-secret-value\n")
    path.chmod(0o000)
    try:
        with pytest.raises(ConfigError) as caught:
            load_config(path)
    finally:
        path.chmod(0o600)

    assert caught.value.reason == "config_load_error"
    assert "super-secret-value" not in str(caught.value)
    assert "super-secret" not in repr(caught.value)
