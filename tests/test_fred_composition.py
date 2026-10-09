import json
from datetime import UTC, datetime

from tokmo.facts.fred_live import FredLiveTransport, FredTransportOutcome
from tokmo.fred_composition import run_fred_operation, run_reserved_fred_operation
from tokmo.records.fred_material_store import FredMaterialStore
from tokmo.values.fred_macro import (
    R2_CAMPAIGN_CODE,
    ExecutionMode,
    FredOperation,
    FredRequest,
)


class FakeTransport:
    def __init__(self, body):
        self.body, self.starts = body, 0

    def __call__(self, request, key):
        self.starts += 1
        return FredTransportOutcome("success", self.body)


def test_reserve_before_one_start_store_and_replay_zero_starts(tmp_path):
    body = json.dumps(
        {
            "realtime_start": "2026-01-01",
            "realtime_end": "2026-01-02",
            "seriess": [
                {
                    "id": "PAYEMS",
                    "realtime_start": "2026-01-01",
                    "realtime_end": "2026-01-02",
                    "title": "All Employees",
                    "observation_start": "1939-01-01",
                    "observation_end": "2025-12-01",
                    "frequency": "Monthly",
                    "frequency_short": "M",
                    "units": "Thousands of Persons",
                    "units_short": "Thous.",
                    "seasonal_adjustment": "Seasonally Adjusted",
                    "seasonal_adjustment_short": "SA",
                    "last_updated": "2026-01-01 00:00:00-06",
                    "popularity": 90,
                }
            ],
        }
    ).encode()
    transport = FakeTransport(body)
    instants = iter((datetime(2026, 1, 2, tzinfo=UTC),))
    request = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    first = run_fred_operation(
        root=tmp_path / "private",
        request=request,
        api_key="a" * 32,
        transport=transport,
        clock=lambda: next(instants),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = run_fred_operation(
        root=tmp_path / "private",
        request=request,
        api_key="a" * 32,
        transport=transport,
        clock=lambda: (_ for _ in ()).throw(AssertionError("clock")),
        started_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
    )
    assert first.code == "durable_success"
    assert second.code == "already_attempted_fixture"
    assert transport.starts == 1


def test_composition_failure_codes_are_truthful(tmp_path, monkeypatch):
    request = FredRequest(FredOperation.SERIES, series_id="PAYEMS")

    class Raising:
        def __call__(self, request, key):
            raise RuntimeError("secret raw body")

    failed = run_fred_operation(
        root=tmp_path / "transport",
        request=request,
        api_key="a" * 32,
        transport=Raising(),
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert failed.code == "transport_failure"
    invalid = run_fred_operation(
        root=tmp_path / "response",
        request=request,
        api_key="a" * 32,
        transport=FakeTransport(b"not-json"),
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert invalid.code == "response_contract_failure"
    assert "secret" not in repr(failed)
    body = json.dumps(
        {
            "realtime_start": "2026-01-01",
            "realtime_end": "2026-01-02",
            "seriess": [
                {
                    "id": "PAYEMS",
                    "realtime_start": "2026-01-01",
                    "realtime_end": "2026-01-02",
                    "title": "All Employees",
                    "observation_start": "1939-01-01",
                    "observation_end": "2025-12-01",
                    "frequency": "Monthly",
                    "frequency_short": "M",
                    "units": "Thousands",
                    "units_short": "Thous.",
                    "seasonal_adjustment": "Seasonally Adjusted",
                    "seasonal_adjustment_short": "SA",
                    "last_updated": "2026-01-01 00:00:00-06",
                    "popularity": 90,
                }
            ],
        }
    ).encode()
    monkeypatch.setattr(
        "tokmo.records.fred_material_store.write_atomic_json",
        lambda *a, **k: (_ for _ in ()).throw(OSError("evidence failure")),
    )
    evidence_failure = run_fred_operation(
        root=tmp_path / "evidence",
        request=request,
        api_key="a" * 32,
        transport=FakeTransport(body),
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert evidence_failure.code == "durable_success_evidence_failure"


def test_reserved_live_validates_before_provider_and_never_reserves_twice(tmp_path):
    root = tmp_path / "live"
    store = FredMaterialStore(root)
    store.ensure_live_campaign()
    request = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    reservation = store.reserve(
        request,
        execution_mode=ExecutionMode.LIVE,
        started_at=datetime(2026, 8, 12, tzinfo=UTC),
    )
    calls = []
    outcome = run_reserved_fred_operation(
        root=root,
        request=request,
        reservation=reservation,
        api_key_provider=lambda: calls.append("key") or "a" * 32,
        transport=FakeTransport(b"not-json"),
        clock=lambda: datetime(2026, 8, 12, tzinfo=UTC),
    )
    assert calls == ["key"] and outcome.code == "response_contract_failure"
    assert store.counts()["attempts"] == 1 and store.counts()["failed"] == 1

    invalid = reservation.__class__("reserved", "f" * 64, 2)
    try:
        run_reserved_fred_operation(
            root=root,
            request=request,
            reservation=invalid,
            api_key_provider=lambda: (_ for _ in ()).throw(AssertionError("provider")),
            transport=FakeTransport(b"not-json"),
            clock=lambda: None,
        )
    except ValueError as error:
        assert str(error) == "invalid_live_reservation"
    else:
        raise AssertionError("invalid reservation accepted")


def test_r2_transport_category_is_preserved_and_only_r2_is_quarantined(tmp_path):
    root = tmp_path / "r2"
    store = FredMaterialStore(root)
    store.ensure_live_campaign(campaign_code=R2_CAMPAIGN_CODE)
    request = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    reservation = store.reserve_campaign(
        R2_CAMPAIGN_CODE, 1, request, started_at=datetime(2026, 8, 12, tzinfo=UTC)
    )
    outcome = run_reserved_fred_operation(
        root=root,
        request=request,
        reservation=reservation,
        api_key_provider=lambda: "a" * 32,
        transport=lambda *_: FredTransportOutcome("provider_bad_request_unknown"),
        clock=lambda: datetime(2026, 8, 12, tzinfo=UTC),
    )
    assert outcome.code == "provider_bad_request_unknown"
    assert store.campaign_state_for(R2_CAMPAIGN_CODE) == ("ACTIVE", None)
    store.quarantine_campaign(R2_CAMPAIGN_CODE, outcome.code)
    assert store.campaign_state_for(R2_CAMPAIGN_CODE) == (
        "QUARANTINED",
        "provider_bad_request_unknown",
    )
    assert store.campaign_state() is None


def test_production_transport_interrupt_is_failed_but_outer_interrupt_stays_reserved(
    tmp_path,
):
    class InterruptingOpener:
        def open(self, request, timeout):
            raise KeyboardInterrupt()

    root = tmp_path / "inside"
    store = FredMaterialStore(root)
    store.ensure_live_campaign()
    request = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    reservation = store.reserve(
        request,
        execution_mode=ExecutionMode.LIVE,
        started_at=datetime(2026, 8, 12, tzinfo=UTC),
    )
    outcome = run_reserved_fred_operation(
        root=root,
        request=request,
        reservation=reservation,
        api_key_provider=lambda: "a" * 32,
        transport=FredLiveTransport(opener=InterruptingOpener()),
        clock=lambda: None,
    )
    assert outcome.code == "protocol_failure" and store.counts()["failed"] == 1

    body = json.dumps(
        {
            "realtime_start": "2026-01-01",
            "realtime_end": "2026-01-02",
            "seriess": [
                {
                    "id": "PAYEMS",
                    "realtime_start": "2026-01-01",
                    "realtime_end": "2026-01-02",
                    "title": "All Employees",
                    "observation_start": "1939-01-01",
                    "observation_end": "2025-12-01",
                    "frequency": "Monthly",
                    "frequency_short": "M",
                    "units": "Thousands",
                    "units_short": "Thous.",
                    "seasonal_adjustment": "Seasonally Adjusted",
                    "seasonal_adjustment_short": "SA",
                    "last_updated": "2026-01-01 00:00:00-06",
                    "popularity": 90,
                }
            ],
        }
    ).encode()
    root = tmp_path / "outside"
    store = FredMaterialStore(root)
    store.ensure_live_campaign()
    reservation = store.reserve(
        request,
        execution_mode=ExecutionMode.LIVE,
        started_at=datetime(2026, 8, 12, tzinfo=UTC),
    )
    try:
        run_reserved_fred_operation(
            root=root,
            request=request,
            reservation=reservation,
            api_key_provider=lambda: "a" * 32,
            transport=FakeTransport(body),
            clock=lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("outer interrupt swallowed")
    assert store.counts()["reserved"] == 1
    assert (
        store.reconcile_unknown(before=datetime(2026, 8, 12, 0, 0, 1, tzinfo=UTC)) == 1
    )
    assert store.counts()["unknown"] == 1
