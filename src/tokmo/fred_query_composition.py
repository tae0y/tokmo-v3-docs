"""G1 fixture dependency assembly for the routine FRED query seam.

Delta SDS §2: composition may import every layer, but only to inject
dependencies. It owns no due policy and no product judgment. In G1 it
assembles a fixture transport, a synthetic credential and an injected temp
root only; no live transport module is imported here, so no provider,
credential file or production root is reachable from this seam. A later
production assembly is a separate delta.

The fixture transport is a type-parity stand-in for the live transport: the
same call signature, the same closed outcome-code vocabulary and the same
``code``/``body`` contract, so the adapter cannot tell them apart.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from tokmo.facts.fred_query import FredQueryAdapterV1, TerminalMonthGuardedPortV1
from tokmo.programs.macro_fred_inputs import run_macro_fred_enrichment_v1
from tokmo.records.fred_query_cache import FredQueryCacheV1
from tokmo.records.fred_schedule_sidecar import (
    ScheduleSidecarInvalid,
    load_schedule_sidecar_v1,
)
from tokmo.values.fred_query import (
    FredCoverageV1,
    FredQueryCacheCorruption,
    canonical_decimal,
)

# Type parity with the live transport's closed outcome vocabulary. The
# architecture seal asserts this stays byte-equal to the live code set
# without importing the live module here.
FIXTURE_TRANSPORT_FAILURE_CODES = frozenset(
    {
        "timeout",
        "connection_failure",
        "redirect",
        "auth_rejection",
        "bad_request",
        "http_429",
        "http_5xx",
        "body_too_large",
        "unsupported_content_encoding",
        "protocol_failure",
        "api_key_not_registered",
        "provider_bad_request_unknown",
    }
)
FIXTURE_API_KEY = "f" * 32


@dataclass(frozen=True)
class FredFixtureTransportOutcomeV1:
    code: str
    body: bytes | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.code not in FIXTURE_TRANSPORT_FAILURE_CODES | {"success"}:
            raise ValueError("closed_transport_code")
        if (self.code == "success") != (type(self.body) is bytes):
            raise ValueError("transport_body_contract")


class FredFixtureTransportV1:
    """Replays reviewed provider bodies in order; constructs no socket."""

    def __init__(self, *, outcomes: tuple):
        if type(outcomes) is not tuple or any(
            not isinstance(item, FredFixtureTransportOutcomeV1) for item in outcomes
        ):
            raise ValueError("closed_fixture_outcomes")
        self._outcomes = outcomes
        self.requests: list = []

    def __call__(self, request, api_key) -> FredFixtureTransportOutcomeV1:
        self.requests.append((request, api_key))
        if re.fullmatch(r"[a-z0-9]{32}", api_key) is None:
            return FredFixtureTransportOutcomeV1("protocol_failure")
        if len(self.requests) > len(self._outcomes):
            raise ValueError("fixture_outcomes_exhausted")
        return self._outcomes[len(self.requests) - 1]


def build_fixture_api_key_provider_v1():
    """A synthetic credential: no credential file is opened in G1."""

    def provide() -> str:
        return FIXTURE_API_KEY

    return provide


def build_fixture_fred_query_port_v1(
    *, transport, api_key_provider, clock
) -> FredQueryAdapterV1:
    return FredQueryAdapterV1(
        transport=transport, api_key_provider=api_key_provider, clock=clock
    )


def build_fixture_fred_query_transport_v1(*, outcomes) -> FredFixtureTransportV1:
    return FredFixtureTransportV1(outcomes=tuple(outcomes))


def build_fixture_fred_query_cache_v1(*, root, opened_at) -> FredQueryCacheV1:
    if not isinstance(root, Path):
        raise ValueError("injected_root_required")
    return FredQueryCacheV1(root, opened_at=opened_at)


@dataclass(frozen=True)
class FredQueryG1DepsV1:
    """The whole G1 fixture seam: no credential file, no production root."""

    port: FredQueryAdapterV1
    cache: FredQueryCacheV1
    transport: FredFixtureTransportV1


def build_g1_fixture_fred_query_deps_v1(*, root, outcomes, clock) -> FredQueryG1DepsV1:
    transport = build_fixture_fred_query_transport_v1(outcomes=outcomes)
    return FredQueryG1DepsV1(
        build_fixture_fred_query_port_v1(
            transport=transport,
            api_key_provider=build_fixture_api_key_provider_v1(),
            clock=clock,
        ),
        build_fixture_fred_query_cache_v1(root=root, opened_at=clock()),
        transport,
    )


# -- the production seam (SDS §4.4) ------------------------------------------


class FredStartBudget:
    """Authenticated FRED starts, spent before any socket is constructed.

    The cap is injected, not hard-coded: the caller uses ``maximum=1`` and the
    tool uses ``maximum=3``.
    """

    def __init__(self, *, maximum: int):
        self.maximum = maximum
        self.starts = 0

    def spend(self) -> None:
        if self.starts >= self.maximum:
            raise ValueError("fred_start_budget_exceeded")
        self.starts += 1


class BudgetedFredTransportV1:
    def __init__(self, transport, budget: FredStartBudget):
        self._transport = transport
        self._budget = budget

    def __call__(self, request, api_key):
        self._budget.spend()
        return self._transport(request, api_key)


@dataclass(frozen=True)
class FredQueryProductionDepsV1:
    """The production caller seam: injected root, sidecar, transport, key, clock."""

    root: Path
    sidecar_path: Path
    port: TerminalMonthGuardedPortV1
    budget: FredStartBudget
    clock: object


def build_production_fred_query_deps_v1(
    *, root: Path, sidecar_path: Path, transport, api_key_provider, clock
) -> FredQueryProductionDepsV1:
    """Assemble the production deps without opening the cache.

    The cache is deliberately NOT opened here: opening can raise, and every
    such raise must land inside the contained call, not inside dependency
    assembly.
    """
    budget = FredStartBudget(maximum=1)
    port = TerminalMonthGuardedPortV1(
        FredQueryAdapterV1(
            transport=BudgetedFredTransportV1(transport, budget),
            api_key_provider=api_key_provider,
            clock=clock,
        )
    )
    return FredQueryProductionDepsV1(
        root=root, sidecar_path=sidecar_path, port=port, budget=budget, clock=clock
    )


MACRO_FRED_RECEIPT_FIELDS: tuple[str, ...] = (
    "fred_status",
    "fred_schedule_source",
    "fred_release_slot",
    "fred_due_state",
    "fred_coverage",
    "fred_provider_starts",
    "fred_bls_starts",
    "fred_query_identity",
    "fred_revision_identity",
    "fred_provenance_digest",
    "fred_direction",
    "fred_current_value",
    "fred_current_date",
    "fred_prior_value",
    "fred_prior_date",
    "fred_error_class",
)


def _scalar(value) -> str | int | None:
    """Render any enrichment value as a JSON-safe scalar."""
    if value is None:
        return None
    if type(value) is int:
        return value
    # Enums carry ``.value`` as str.
    if hasattr(value, "value"):
        return str(value.value)
    from decimal import Decimal

    if type(value) is Decimal:
        return canonical_decimal(value)
    if type(value) is date:
        return value.isoformat()
    return str(value)


def build_macro_fred_caller_v1(deps: FredQueryProductionDepsV1):
    """Build a zero-argument callable that returns the FRED enrichment receipt.

    The returned callable **never raises**.  Every FRED/local/sidecar/cache
    exception is contained into closed scalar receipt fields.  It does not
    catch ``BaseException``, so ``KeyboardInterrupt``/``SystemExit`` still
    propagate.  Ceiling: 1 FRED observation start, 0 BLS starts.
    """

    def caller() -> dict:
        loaded = None
        try:
            now = deps.clock()
            # Step 1: reject a non-aware-UTC clock.
            from datetime import datetime

            if (
                type(now) is not datetime
                or now.tzinfo is None
                or now.utcoffset() is None
                or now.utcoffset().total_seconds() != 0
            ):
                raise ValueError("aware_utc_required")

            # Step 2: read the persisted sidecar (read-only).
            try:
                loaded = load_schedule_sidecar_v1(deps.sidecar_path, now=now)
            except ScheduleSidecarInvalid as error:
                return _receipt(
                    fred_status="SCHEDULE_INVALID",
                    fred_schedule_source="INVALID",
                    fred_provider_starts=deps.budget.starts,
                    fred_error_class=str(error),
                )
            if loaded is None:
                return _receipt(
                    fred_status="SCHEDULE_ABSENT",
                    fred_schedule_source="ABSENT",
                    fred_provider_starts=deps.budget.starts,
                )
            if loaded.expired:
                return _receipt(
                    fred_status="SCHEDULE_EXPIRED",
                    fred_schedule_source="EXPIRED",
                    fred_release_slot=loaded.schedule.release_slot,
                    fred_provider_starts=deps.budget.starts,
                )

            # Step 3: derive the query window.
            from tokmo.programs.macro_fred_inputs import derive_query_window_v1

            query, _ = derive_query_window_v1(
                release_date=loaded.release_date, as_of=now
            )

            # Step 4: open the cache (crash reconciliation point).
            cache = FredQueryCacheV1(deps.root, opened_at=now)

            # Step 5: run the enrichment program code (byte-unchanged).
            enrichment = run_macro_fred_enrichment_v1(
                query=query,
                schedule=loaded.schedule,
                cache=cache,
                query_port=deps.port,
                clock=deps.clock,
            )

            # Step 6: project onto receipt fields.
            fred_status = (
                "ENRICHED" if enrichment.coverage is FredCoverageV1.USED else "OMITTED"
            )
            return dict(
                fred_status=fred_status,
                fred_schedule_source="SIDECAR",
                fred_release_slot=loaded.schedule.release_slot,
                fred_due_state=enrichment.due_state.value,
                fred_coverage=enrichment.coverage.value,
                fred_provider_starts=enrichment.provider_starts,
                fred_bls_starts=0,
                fred_query_identity=enrichment.query_identity,
                fred_revision_identity=enrichment.revision_identity,
                fred_provenance_digest=enrichment.provenance_digest,
                fred_direction=(
                    _scalar(enrichment.direction)
                    if enrichment.direction is not None
                    else None
                ),
                fred_current_value=_scalar(enrichment.current_value),
                fred_current_date=_scalar(enrichment.current_date),
                fred_prior_value=_scalar(enrichment.prior_value),
                fred_prior_date=_scalar(enrichment.prior_date),
                fred_error_class=None,
            )
        except FredQueryCacheCorruption:
            return _receipt(
                fred_status="LOCAL_STATE_UNAVAILABLE",
                fred_schedule_source="SIDECAR" if loaded is not None else "NONE",
                fred_release_slot=(
                    loaded.schedule.release_slot if loaded is not None else None
                ),
                fred_provider_starts=deps.budget.starts,
                fred_error_class="FredQueryCacheCorruption",
            )
        except Exception as error:
            return _receipt(
                fred_status="CONTAINED_ERROR",
                fred_schedule_source="SIDECAR" if loaded is not None else "NONE",
                fred_release_slot=(
                    loaded.schedule.release_slot if loaded is not None else None
                ),
                fred_provider_starts=deps.budget.starts,
                fred_error_class=type(error).__name__,
            )

    return caller


def _receipt(
    *,
    fred_status,
    fred_schedule_source="NONE",
    fred_release_slot=None,
    fred_provider_starts=0,
    fred_error_class=None,
):
    """Build a zero-information default receipt with the given status."""
    return dict(
        fred_status=fred_status,
        fred_schedule_source=fred_schedule_source,
        fred_release_slot=fred_release_slot,
        fred_due_state=None,
        fred_coverage=None,
        fred_provider_starts=fred_provider_starts,
        fred_bls_starts=0,
        fred_query_identity=None,
        fred_revision_identity=None,
        fred_provenance_digest=None,
        fred_direction=None,
        fred_current_value=None,
        fred_current_date=None,
        fred_prior_value=None,
        fred_prior_date=None,
        fred_error_class=fred_error_class,
    )
