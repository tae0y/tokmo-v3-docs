"""Injected Stage3 FRED operation composition."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tokmo.facts.fred import parse_fred_response
from tokmo.facts.fred_live import FredTransportOutcome
from tokmo.records.fred_material_store import FredMaterialStore, Reservation
from tokmo.values.fred_macro import (
    CAMPAIGN_CODE,
    R2_CAMPAIGN_CODE,
    R3_CAMPAIGN_CODE,
    R4_CAMPAIGN_CODE,
    ExecutionMode,
    FredRequest,
)


def _quarantine_for_reservation(
    store: FredMaterialStore, reservation: Reservation, code: str
) -> None:
    connection = store._connect()
    try:
        row = connection.execute(
            "SELECT campaign_code FROM attempts WHERE invocation_identity=?",
            (reservation.invocation_identity,),
        ).fetchone()
    finally:
        connection.close()
    if row is not None and row[0] == R2_CAMPAIGN_CODE:
        store.quarantine_campaign(R2_CAMPAIGN_CODE, code)
    elif row is not None and row[0] == R3_CAMPAIGN_CODE:
        store.quarantine_campaign(R3_CAMPAIGN_CODE, code)
    elif row is not None and row[0] == R4_CAMPAIGN_CODE:
        store.quarantine_campaign(R4_CAMPAIGN_CODE, code)
    elif row is not None and row[0] in (CAMPAIGN_CODE, "FIXTURE"):
        store.quarantine(code)
    elif row is not None:
        raise ValueError("campaign_mismatch")
    else:
        store.quarantine(code)


@dataclass(frozen=True)
class FredRunOutcome:
    code: str
    ordinal: int | None
    material_digests: tuple[str, ...] = ()
    fixture_evidence: str = "FIXTURE_ONLY"
    store_evidence: str | None = None


def _run_reserved(
    *,
    store: FredMaterialStore,
    request: FredRequest,
    reservation: Reservation,
    api_key: str,
    transport,
    clock,
) -> FredRunOutcome:
    try:
        live = transport(request, api_key)
    except Exception:
        store.settle(reservation, "FAILED")
        return FredRunOutcome("transport_failure", reservation.ordinal)
    if not isinstance(live, FredTransportOutcome) or live.code != "success":
        store.settle(reservation, "FAILED")
        return FredRunOutcome(
            getattr(live, "code", "transport_failure"), reservation.ordinal
        )
    try:
        page = parse_fred_response(request, live.body)
    except ValueError:
        store.settle(reservation, "FAILED")
        return FredRunOutcome("response_contract_failure", reservation.ordinal)
    if not page.rows:
        store.settle(reservation, "FAILED")
        return FredRunOutcome("empty_page", reservation.ordinal)
    fetched_at = clock()
    if (
        type(fetched_at) is not datetime
        or fetched_at.tzinfo is None
        or fetched_at.utcoffset() is None
    ):
        store.settle(reservation, "FAILED")
        return FredRunOutcome("clock_failure", reservation.ordinal)
    try:
        digests = store.store_page(
            page.rows,
            reservation=reservation,
            operation=request.operation.value,
            fetched_at=fetched_at,
            provider_count=page.count,
            completeness=page.completeness.value,
            completeness_evidence=page.completeness_evidence,
        )
    except ValueError as error:
        code = str(error)
        if code == "durable_success_evidence_failure":
            _quarantine_for_reservation(store, reservation, code)
            return FredRunOutcome(code, reservation.ordinal)
        if code == "settlement_conflict":
            _quarantine_for_reservation(store, reservation, code)
            raise
        try:
            store.settle(reservation, "FAILED")
        except ValueError as settlement:
            if str(settlement) == "settlement_conflict":
                _quarantine_for_reservation(store, reservation, "settlement_conflict")
                raise
            raise
        return FredRunOutcome("store_failure", reservation.ordinal)
    except Exception:
        store.settle(reservation, "FAILED")
        return FredRunOutcome("store_failure", reservation.ordinal)
    return FredRunOutcome(
        "durable_success", reservation.ordinal, digests, store_evidence="STORE_VERIFIED"
    )


def run_reserved_fred_operation(
    *,
    root: Path,
    request: FredRequest,
    reservation: Reservation,
    api_key_provider,
    transport,
    clock,
) -> FredRunOutcome:
    """Consume one pre-committed LIVE reservation without reserving again."""
    store = FredMaterialStore(root)
    store.validate_live_reservation(reservation, request)
    try:
        api_key = api_key_provider()
    except Exception:
        store.settle(reservation, "FAILED")
        _quarantine_for_reservation(store, reservation, "credential_failure")
        return FredRunOutcome("credential_failure", reservation.ordinal)
    return _run_reserved(
        store=store,
        request=request,
        reservation=reservation,
        api_key=api_key,
        transport=transport,
        clock=clock,
    )


def run_fred_operation(
    *,
    root: Path,
    request: FredRequest,
    api_key: str,
    transport,
    clock,
    execution_mode: ExecutionMode = ExecutionMode.FIXTURE,
    started_at: datetime | None = None,
) -> FredRunOutcome:
    store = FredMaterialStore(root)
    store.assert_mode_allowed(execution_mode)
    store.reconcile_unknown()
    reservation = store.reserve(
        request, execution_mode=execution_mode, started_at=started_at
    )
    if reservation.code != "reserved":
        return FredRunOutcome(reservation.code, reservation.ordinal)
    return _run_reserved(
        store=store,
        request=request,
        reservation=reservation,
        api_key=api_key,
        transport=transport,
        clock=clock,
    )
