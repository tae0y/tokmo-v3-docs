"""Inactive Quant recurring cadence seams with injected provider effects."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from tokmo.facts.market_regime_toss_input import completed_session_boundary
from tokmo.facts.quant_membership import (
    parse_nasdaqlisted,
    parse_otherlisted,
    parse_sec_issuer,
    parse_sec_registry,
)
from tokmo.facts.quant_recurring_membership import (
    DEADLINE_MARGIN_SECONDS,
    PER_START_BUDGET_SECONDS,
    EndpointKindV1,
    OfficialMembershipRequestV1,
    membership_native_identity,
    membership_operation_plan,
    predict_us_personal_cohort,
    quant_candle_native_identity,
    sufficient_deadline,
)
from tokmo.programs.market_regime_shadow import resolve_completed_session_by_market
from tokmo.programs.quant_membership import assemble_us_personal_membership_material
from tokmo.programs.quant_multifactor_core import evaluate_quant_multifactor_core_v1
from tokmo.programs.quant_multifactor_inputs import (
    assemble_quant_multifactor_input,
    page2_request_identity,
    requires_page2,
)
from tokmo.records.current_holdings_store import CurrentHoldingsStore
from tokmo.records.fundamental_material_store import read_admissible_materials_ro
from tokmo.records.official_membership_observation_store import (
    OfficialMembershipObservationStore,
)
from tokmo.records.quant_recurring_receipts import QuantRecurringResultV1
from tokmo.records.regular_session_history import RegularSessionHistoryStore
from tokmo.values.cadence_policy import CadencePolicyV1
from tokmo.values.current_holdings import canonical_holdings_bytes
from tokmo.values.official_membership_observation import (
    MembershipPolicyV1,
    ObservationSourceKindV1,
    ObservedSourcePayloadV1,
    OfficialMembershipAcquisitionV1,
    RequestIdentityV1,
)
from tokmo.values.ops_kernel import CadenceBudgetExceeded
from tokmo.values.quant_multifactor_core import MarketV1
from tokmo.values.quant_multifactor_provenance import (
    AssemblyInputV1,
    CandleRequestIdentityV1,
    MemberAssemblyInputV1,
    RawCandlePageV1,
    RawCandleV1,
)
from tokmo.values.quant_recurring import (
    QUANT_RECURRING_ADJUSTMENT_VERSION,
    QUANT_RECURRING_PROGRAM_ID,
    QUANT_RECURRING_UNIVERSE_ID,
    QUANT_RECURRING_UNIVERSE_POLICY_VERSION,
    QuantDueContextV1,
    QuantDueDecisionV1,
    QuantDueIdentityV1,
    QuantSessionBindingV1,
    QuantTerminalStateV1,
    QuantTickCalendarInputV1,
    membership_plan_starts,
    quant_due_max_increment,
)
from tokmo.values.session_gate import MARKET_TIMEZONE, Market


class QuantRecurringRefusal(ValueError):
    pass


class _QuantCandleBudgetExceeded(CadenceBudgetExceeded):
    def __init__(self):
        super().__init__("quant daily-candle ceiling reached")


@dataclass(slots=True)
class _QuantCandleStarts:
    page1: int = 0
    page2: int = 0


@dataclass(frozen=True, slots=True)
class QuantMembershipOutcomeV1:
    identity: QuantDueIdentityV1
    acquisition: OfficialMembershipAcquisitionV1 | None
    payloads: tuple[ObservedSourcePayloadV1, ...]
    predicted_symbols: tuple[str, ...]
    membership_starts: int
    status: str
    reason: str


def _context(
    decision: QuantDueDecisionV1,
    *,
    membership_starts_ceiling,
    completed_session_date=None,
    ny_date=None,
    predicted_symbols=(),
    deadline_margin_seconds=0,
) -> QuantDueContextV1:
    return QuantDueContextV1(
        decision,
        None,
        completed_session_date,
        ny_date,
        predicted_symbols,
        deadline_margin_seconds,
        membership_plan_starts(len(predicted_symbols)),
        membership_starts_ceiling,
    )


def _parse_boundary(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("calendar_boundary")
    return parsed


def evaluate_quant_due(
    *,
    root: Path,
    deps,
    policy: CadencePolicyV1,
    tick,
    calendar: QuantTickCalendarInputV1,
    cache_store,
    receipts,
) -> QuantDueContextV1:
    limits = policy.request_limits
    if limits.membership_starts <= 0 or limits.quant_daily_candle_starts <= 0:
        return _context(
            QuantDueDecisionV1.POLICY_ZERO,
            membership_starts_ceiling=limits.membership_starts,
        )

    scheduled_for_utc = tick.scheduled_for_utc
    ny_date = scheduled_for_utc.astimezone(ZoneInfo(MARKET_TIMEZONE[Market.US])).date()
    if (
        not calendar.us_refresh_succeeded
        or not calendar.mask_bound
        or calendar.us_cache_trade_date != ny_date
    ):
        return _context(
            QuantDueDecisionV1.NO_COMPLETED_SESSION,
            membership_starts_ceiling=limits.membership_starts,
            ny_date=ny_date,
        )
    try:
        cache = cache_store.read()
        entry = None if cache is None else cache.us
        if entry is None or entry.trade_date != ny_date.isoformat():
            return _context(
                QuantDueDecisionV1.NO_COMPLETED_SESSION,
                membership_starts_ceiling=limits.membership_starts,
                ny_date=ny_date,
            )
        regular = entry.sessions.get("regularMarket")
        if regular is None:
            return _context(
                QuantDueDecisionV1.NO_COMPLETED_SESSION,
                membership_starts_ceiling=limits.membership_starts,
                ny_date=ny_date,
            )
        official_close = _parse_boundary(regular.end)
        if scheduled_for_utc < official_close:
            return _context(
                QuantDueDecisionV1.NO_COMPLETED_SESSION,
                membership_starts_ceiling=limits.membership_starts,
                ny_date=ny_date,
            )
        history = RegularSessionHistoryStore(
            Path(root) / "ops" / "regular_session_history.sqlite3",
            clock=deps.clock,
        )
        today_row = history.read(Market.US, ny_date)
        completed = resolve_completed_session_by_market(
            scheduled_for_utc=scheduled_for_utc,
            market=Market.US,
            today_row=today_row,
            history_store=history,
            official_close=official_close,
        )
    except Exception:
        return _context(
            QuantDueDecisionV1.NO_COMPLETED_SESSION,
            membership_starts_ceiling=limits.membership_starts,
            ny_date=ny_date,
        )
    if completed is None:
        return _context(
            QuantDueDecisionV1.NO_COMPLETED_SESSION,
            membership_starts_ceiling=limits.membership_starts,
            ny_date=ny_date,
        )
    if ny_date != completed:
        return _context(
            QuantDueDecisionV1.NY_DATE_MISMATCH,
            membership_starts_ceiling=limits.membership_starts,
            completed_session_date=completed,
            ny_date=ny_date,
        )
    if not calendar.us_in_effective_mask:
        return _context(
            QuantDueDecisionV1.NO_POST_CLOSE_OPEN_WINDOW,
            membership_starts_ceiling=limits.membership_starts,
            completed_session_date=completed,
            ny_date=ny_date,
        )

    identity = QuantDueIdentityV1(
        QUANT_RECURRING_PROGRAM_ID,
        policy.policy_version,
        MarketV1.US_NYSE_NASDAQ,
        completed,
        QUANT_RECURRING_UNIVERSE_POLICY_VERSION,
    )
    receipt_store = receipts() if callable(receipts) else receipts
    if (
        receipt_store is not None
        and receipt_store.read_due(identity.identity_digest) is not None
    ):
        return _context(
            QuantDueDecisionV1.ALREADY_TERMINAL,
            membership_starts_ceiling=limits.membership_starts,
            completed_session_date=completed,
            ny_date=ny_date,
        )
    observations_path = Path(root) / "official-membership" / "observations.sqlite3"
    if (
        observations_path.exists()
        and OfficialMembershipObservationStore(observations_path).read_acquisition(
            identity.acquisition_id
        )
        is not None
    ):
        return _context(
            QuantDueDecisionV1.ALREADY_TERMINAL,
            membership_starts_ceiling=limits.membership_starts,
            completed_session_date=completed,
            ny_date=ny_date,
        )
    try:
        prior = CurrentHoldingsStore(
            Path(root) / "stores" / "current_holdings.json",
            clock=deps.clock,
        ).read()
    except Exception:
        prior = None
    predicted_symbols = predict_us_personal_cohort(prior)
    if not 1 <= len(predicted_symbols) <= 8:
        return _context(
            QuantDueDecisionV1.COHORT_UNPREDICTABLE,
            membership_starts_ceiling=limits.membership_starts,
            completed_session_date=completed,
            ny_date=ny_date,
        )
    plan_starts = membership_plan_starts(len(predicted_symbols))
    if plan_starts > limits.membership_starts:
        return _context(
            QuantDueDecisionV1.MEMBERSHIP_BUDGET_UNSATISFIABLE,
            membership_starts_ceiling=limits.membership_starts,
            completed_session_date=completed,
            ny_date=ny_date,
            predicted_symbols=predicted_symbols,
        )
    planned_starts = quant_due_max_increment(len(predicted_symbols))
    observed_now = deps.clock()
    remaining = (
        policy.child_timeout_seconds
        - (observed_now - scheduled_for_utc).total_seconds()
    )
    required = planned_starts * PER_START_BUDGET_SECONDS + DEADLINE_MARGIN_SECONDS
    deadline_margin = int(remaining - required)
    if not sufficient_deadline(
        policy=policy,
        scheduled_for_utc=scheduled_for_utc,
        observed_now_utc=observed_now,
        planned_starts=planned_starts,
    ):
        return _context(
            QuantDueDecisionV1.INSUFFICIENT_DEADLINE,
            membership_starts_ceiling=limits.membership_starts,
            completed_session_date=completed,
            ny_date=ny_date,
            predicted_symbols=predicted_symbols,
            deadline_margin_seconds=deadline_margin,
        )
    return QuantDueContextV1(
        QuantDueDecisionV1.DUE,
        identity,
        completed,
        ny_date,
        predicted_symbols,
        deadline_margin,
        plan_starts,
        limits.membership_starts,
    )


def _request_payload(
    *,
    acquisition_id,
    source_kind,
    request,
    subject_key,
    started_count,
    outcome,
) -> ObservedSourcePayloadV1:
    parts = request.url.split("/", 3)
    return ObservedSourcePayloadV1(
        acquisition_id=acquisition_id,
        source_kind=source_kind,
        request_identity=RequestIdentityV1(
            method="GET",
            host=parts[2],
            path_template="/" + parts[3],
            source_schema_version="official-membership-source-v1",
        ),
        subject_key=subject_key,
        started_count=started_count,
        observed_at=outcome.observed_at,
        payload_bytes=outcome.body,
    )


def _expected_membership_plan(symbols, binding):
    pairs = (
        (
            "MEMBERSHIP_NASDAQ_LISTED",
            membership_native_identity(
                endpoint="nasdaqlisted", session_date=binding.session_date
            ),
        ),
        (
            "MEMBERSHIP_OTHER_LISTED",
            membership_native_identity(
                endpoint="otherlisted", session_date=binding.session_date
            ),
        ),
        (
            "MEMBERSHIP_SEC_REGISTRY",
            membership_native_identity(
                endpoint="registry", session_date=binding.session_date
            ),
        ),
    )
    return pairs + tuple(
        (
            "MEMBERSHIP_SEC_ISSUER",
            membership_native_identity(
                endpoint="issuer",
                session_date=binding.session_date,
                symbol=symbol,
            ),
        )
        for symbol in sorted(symbols)
    )


def run_quant_membership_acquisition(
    *,
    root: Path,
    deps,
    policy: CadencePolicyV1,
    tick,
    executor,
    context: QuantDueContextV1,
    receipts,
) -> QuantMembershipOutcomeV1:
    del root, tick
    if context.decision is not QuantDueDecisionV1.DUE or context.identity is None:
        raise QuantRecurringRefusal("context_not_due")
    if (
        context.membership_plan_starts
        != membership_plan_starts(len(context.predicted_symbols))
        or context.membership_starts_ceiling != policy.request_limits.membership_starts
    ):
        raise QuantRecurringRefusal("membership_budget_binding")
    binding = QuantSessionBindingV1(context.identity)
    plan = membership_operation_plan(context.predicted_symbols, binding.session_date)
    if len(plan) != context.membership_plan_starts:
        raise QuantRecurringRefusal("membership_plan_starts")
    if plan != _expected_membership_plan(context.predicted_symbols, binding):
        raise QuantRecurringRefusal("session_binding")

    identity = context.identity
    lease = receipts.acquire_identity_lease(identity.identity_digest)
    if lease is None:
        return QuantMembershipOutcomeV1(
            identity,
            None,
            (),
            context.predicted_symbols,
            0,
            "skipped",
            "identity_in_progress",
        )
    payloads = []
    starts = 0
    acquisition = None
    with lease:
        claim = receipts.claim(
            identity,
            owner_invocation_id=f"tick-{identity.identity_digest[:16]}",
            owner_lease_key=lease.key,
            claimed_at=deps.clock(),
            identity_lease=lease,
        )
        if claim.state is not QuantTerminalStateV1.CLAIMED:
            return QuantMembershipOutcomeV1(
                identity,
                None,
                (),
                context.predicted_symbols,
                0,
                "skipped",
                "already_terminal",
            )
        try:

            def fetch(index, request, source_kind, subject_key):
                nonlocal starts
                operation, native_identity = plan[index]

                def invoke(started_claim):
                    nonlocal starts
                    starts += 1
                    outcome = deps.membership_fetch(
                        claim=started_claim, identity=request
                    )
                    if not getattr(outcome, "ok", False):
                        raise ValueError("source_failure")
                    return outcome

                outcome = executor.execute(
                    operation,
                    native_identity,
                    invoke,
                )
                payloads.append(
                    _request_payload(
                        acquisition_id=identity.acquisition_id,
                        source_kind=source_kind,
                        request=request,
                        subject_key=subject_key,
                        started_count=index + 1,
                        outcome=outcome,
                    )
                )
                return outcome

            listed = fetch(
                0,
                OfficialMembershipRequestV1(EndpointKindV1.NASDAQ_LISTED),
                ObservationSourceKindV1.NASDAQ_LISTED,
                "nasdaqlisted",
            )
            other = fetch(
                1,
                OfficialMembershipRequestV1(EndpointKindV1.OTHER_LISTED),
                ObservationSourceKindV1.OTHER_LISTED,
                "otherlisted",
            )
            registry_payload = fetch(
                2,
                OfficialMembershipRequestV1(EndpointKindV1.SEC_REGISTRY),
                ObservationSourceKindV1.SEC_REGISTRY,
                "registry",
            )
            symbols = context.predicted_symbols
            listings = (
                *parse_nasdaqlisted(
                    listed.body,
                    observed_at=listed.observed_at,
                    expected_symbols=symbols,
                ),
                *parse_otherlisted(
                    other.body,
                    observed_at=other.observed_at,
                    expected_symbols=symbols,
                ),
            )
            registry_rows = parse_sec_registry(
                registry_payload.body,
                observed_at=registry_payload.observed_at,
                expected_symbols=symbols,
            )
            registry_by_symbol = {row.symbol: row for row in registry_rows}
            issuer_rows = []
            for index, symbol in enumerate(sorted(symbols), start=3):
                registry = registry_by_symbol[symbol]
                issuer_payload = fetch(
                    index,
                    OfficialMembershipRequestV1(
                        EndpointKindV1.SEC_SUBMISSIONS, registry.cik
                    ),
                    ObservationSourceKindV1.SEC_ISSUER,
                    symbol,
                )
                issuer_rows.append(
                    parse_sec_issuer(
                        issuer_payload.body,
                        observed_at=issuer_payload.observed_at,
                        expected_symbol=symbol,
                        expected_cik=registry.cik,
                        expected_exchange=registry.exchange_token,
                    )
                )
            acquisition = OfficialMembershipAcquisitionV1(
                acquisition_id=identity.acquisition_id,
                membership_policy=MembershipPolicyV1.OBSERVED_CURRENT_LISTING_V1,
                security_policy_version="official-security-name-v1",
                source_contract_version="official-membership-source-v1",
                sic_snapshot=deps.sic_snapshot_loader(),
                listings=tuple(listings),
                registry_rows=registry_rows,
                issuer_rows=tuple(issuer_rows),
            )
        except CadenceBudgetExceeded:
            receipts.finish(
                identity.identity_digest,
                state=QuantTerminalStateV1.FAILED,
                reason_code="membership_budget_exhausted",
                finished_at=deps.clock(),
            )
            return QuantMembershipOutcomeV1(
                identity,
                None,
                tuple(payloads),
                context.predicted_symbols,
                starts,
                "failed",
                "membership_budget_exhausted",
            )
        except Exception as error:
            detail = getattr(error, "args", ())
            detail_text = (
                detail[0] if detail and isinstance(detail[0], str) else "unknown"
            )
            reason = f"source_failure:{detail_text}"[:48]
            receipts.finish(
                identity.identity_digest,
                state=QuantTerminalStateV1.FAILED,
                reason_code=reason,
                finished_at=deps.clock(),
            )
            return QuantMembershipOutcomeV1(
                identity,
                None,
                tuple(payloads),
                context.predicted_symbols,
                starts,
                "failed",
                reason,
            )
    return QuantMembershipOutcomeV1(
        identity,
        acquisition,
        tuple(payloads),
        context.predicted_symbols,
        starts,
        "ok",
        "acquired",
    )


def _fresh_holdings_or_none(*, root, deps, membership):
    path = Path(root) / "stores" / "current_holdings.json"
    try:
        first_bytes = path.read_bytes()
        holdings = CurrentHoldingsStore(path, clock=deps.clock).read()
        second_bytes = path.read_bytes()
    except Exception:
        return None, None, "holdings_unavailable"
    if (
        holdings is None
        or first_bytes != second_bytes
        or first_bytes != canonical_holdings_bytes(holdings)
    ):
        return None, None, "holdings_mutated"
    observed = tuple(payload.observed_at for payload in membership.payloads)
    if not observed or any(instant > holdings.captured_at for instant in observed):
        return holdings, first_bytes, "holdings_identity"
    return holdings, first_bytes, None


def _observation_store(root, deps):
    return OfficialMembershipObservationStore(
        Path(root) / "official-membership" / "observations.sqlite3",
        stamp_factory=deps.clock,
    )


def _result(
    *,
    membership,
    holdings,
    membership_digest,
    provenance_digest,
    pack_digest,
    membership_starts,
    page1_starts,
    page2_starts,
    admissible_count,
    degraded,
    limitations,
    holdings_captured_at=None,
):
    captured_at = holdings.captured_at if holdings is not None else holdings_captured_at
    if captured_at is None:
        raise ValueError("holdings_captured_at")
    return QuantRecurringResultV1(
        identity_digest=membership.identity.identity_digest,
        acquisition_id=membership.identity.acquisition_id,
        membership_holdings_digest=membership_digest,
        holdings_captured_at=captured_at,
        membership_starts=membership_starts,
        candle_page1_starts=page1_starts,
        candle_page2_starts=page2_starts,
        cohort_size=len(membership.predicted_symbols),
        admissible_count=admissible_count,
        provenance_digest=provenance_digest,
        pack_digest=pack_digest,
        degraded=degraded,
        limitations=tuple(limitations),
        symbols=tuple(sorted(membership.predicted_symbols)),
    )


def _settle_no_pack(
    *,
    receipts,
    membership,
    holdings,
    membership_digest,
    provenance_digest,
    reason,
    page1_starts=0,
    page2_starts=0,
    admissible_count=0,
    deps,
):
    receipts.finish(
        membership.identity.identity_digest,
        state=QuantTerminalStateV1.NO_PACK,
        reason_code=reason,
        finished_at=deps.clock(),
        result=_result(
            membership=membership,
            holdings=holdings,
            membership_digest=membership_digest,
            provenance_digest=provenance_digest,
            pack_digest="",
            membership_starts=membership.membership_starts,
            page1_starts=page1_starts,
            page2_starts=page2_starts,
            admissible_count=admissible_count,
            degraded=True,
            limitations=(reason.upper(),),
            holdings_captured_at=deps.clock(),
        ),
    )
    return "skipped", reason


def _raw_candle_page(request, outcome):
    rows = tuple(
        RawCandleV1(
            candle.timestamp,
            Decimal(candle.close_price),
            candle.currency,
        )
        for candle in outcome.value.candles
    )
    return RawCandlePageV1(request, rows, len(rows))


def _validate_candle_outcome(outcome):
    if getattr(outcome, "code", None) != "success":
        raise ValueError("candle_provider_failed")
    if getattr(getattr(outcome, "value", None), "candles", None) is None:
        raise ValueError("candle_contract_mismatch")
    return outcome


def _fetch_candle_pages(
    *,
    deps,
    executor,
    bearer,
    binding,
    symbol,
    remaining_starts,
    starts,
):
    if remaining_starts < 1:
        raise _QuantCandleBudgetExceeded()
    page1_request = CandleRequestIdentityV1(
        "qt", symbol, MarketV1.US_NYSE_NASDAQ, "1d", 200, False, 1, None
    )
    page1_native_identity = quant_candle_native_identity(
        market=Market.US,
        symbol=symbol,
        session_date=binding.session_date,
        page_index=1,
    )

    def fetch_page1(claim):
        return _validate_candle_outcome(
            deps.stock_candles_fetch(
                claim=claim,
                bearer=bearer,
                symbol=symbol,
                before=completed_session_boundary(Market.US, binding.session_date),
                adjusted=False,
            )
        )

    starts.page1 += 1
    page1 = _raw_candle_page(
        page1_request,
        executor.execute(
            "STOCK_CANDLES_DAILY",
            page1_native_identity,
            fetch_page1,
        ),
    )
    if not requires_page2(page1):
        return page1, None
    if remaining_starts < 2:
        raise _QuantCandleBudgetExceeded()
    page2_request = page2_request_identity(page1)
    page2_native_identity = quant_candle_native_identity(
        market=Market.US,
        symbol=symbol,
        session_date=binding.session_date,
        page_index=2,
    )

    def fetch_page2(claim):
        return _validate_candle_outcome(
            deps.stock_candles_fetch(
                claim=claim,
                bearer=bearer,
                symbol=symbol,
                before=page2_request.before,
                adjusted=False,
            )
        )

    starts.page2 += 1
    page2 = _raw_candle_page(
        page2_request,
        executor.execute(
            "STOCK_CANDLES_DAILY",
            page2_native_identity,
            fetch_page2,
        ),
    )
    return page1, page2


def run_quant_recurring_shadow(
    *,
    root: Path,
    deps,
    policy: CadencePolicyV1,
    tick,
    executor,
    token,
    membership: QuantMembershipOutcomeV1,
    receipts,
) -> tuple[str, str]:
    del tick
    if membership.status != "ok" or membership.acquisition is None:
        return "skipped", membership.reason
    binding = QuantSessionBindingV1(membership.identity)
    holdings, holdings_bytes, holdings_reason = _fresh_holdings_or_none(
        root=root, deps=deps, membership=membership
    )
    if holdings is None:
        return _settle_no_pack(
            receipts=receipts,
            membership=membership,
            holdings=None,
            membership_digest=sha256(b"").hexdigest(),
            provenance_digest=sha256(holdings_reason.encode("ascii")).hexdigest(),
            reason=holdings_reason,
            deps=deps,
        )
    membership_digest = sha256(holdings_bytes).hexdigest()
    if holdings_reason is not None:
        _observation_store(root, deps).append_failure(
            acquisition=membership.acquisition,
            payloads=membership.payloads,
            reason="assembly_failure",
            detail="sanitized",
        )
        return _settle_no_pack(
            receipts=receipts,
            membership=membership,
            holdings=holdings,
            membership_digest=membership_digest,
            provenance_digest=sha256(holdings_reason.encode("ascii")).hexdigest(),
            reason=holdings_reason,
            deps=deps,
        )
    assembled_membership = assemble_us_personal_membership_material(
        holdings=holdings,
        evaluation_as_of=holdings.captured_at,
        acquisition=membership.acquisition,
    )
    if tuple(sorted(proof.symbol for proof in assembled_membership.proofs)) != tuple(
        sorted(membership.predicted_symbols)
    ) or len(assembled_membership.materials) != len(membership.predicted_symbols):
        _observation_store(root, deps).append_failure(
            acquisition=membership.acquisition,
            payloads=membership.payloads,
            reason="assembly_failure",
            detail="sanitized",
        )
        return _settle_no_pack(
            receipts=receipts,
            membership=membership,
            holdings=holdings,
            membership_digest=membership_digest,
            provenance_digest=sha256(b"cohort_mismatch").hexdigest(),
            reason="cohort_mismatch",
            deps=deps,
        )
    _observation_store(root, deps).append_success(
        acquisition=membership.acquisition,
        payloads=membership.payloads,
        materials=assembled_membership.materials,
    )

    fundamentals = {}
    try:
        for proof in assembled_membership.proofs:
            fundamentals[proof.symbol] = read_admissible_materials_ro(
                Path(root) / "stores" / "fundamental_materials.sqlite3",
                canonical_symbol=proof.symbol,
                market=MarketV1.US_NYSE_NASDAQ,
                as_of=holdings.captured_at,
                limit=64,
            )
    except Exception as error:
        reason = (
            "fundamental_store_absent"
            if type(error).__name__ == "FundamentalStoreAbsentError"
            else "fundamental_store_corrupt"
        )
        return _settle_no_pack(
            receipts=receipts,
            membership=membership,
            holdings=holdings,
            membership_digest=membership_digest,
            provenance_digest=sha256(reason.encode("ascii")).hexdigest(),
            reason=reason,
            deps=deps,
        )
    if any(not values for values in fundamentals.values()):
        return _settle_no_pack(
            receipts=receipts,
            membership=membership,
            holdings=holdings,
            membership_digest=membership_digest,
            provenance_digest=sha256(b"admissible_member_count").hexdigest(),
            reason="admissible_member_count",
            deps=deps,
        )
    try:
        bearer = token.bearer()
    except Exception:
        receipts.finish(
            membership.identity.identity_digest,
            state=QuantTerminalStateV1.FAILED,
            reason_code="token_unavailable",
            finished_at=deps.clock(),
        )
        return "failed", "token_unavailable"

    proof_by_symbol = {proof.symbol: proof for proof in assembled_membership.proofs}
    members = []
    candle_starts = _QuantCandleStarts()
    try:
        for symbol in sorted(proof_by_symbol):
            page1, page2 = _fetch_candle_pages(
                deps=deps,
                executor=executor,
                bearer=bearer,
                binding=binding,
                symbol=symbol,
                remaining_starts=(
                    policy.request_limits.quant_daily_candle_starts
                    - candle_starts.page1
                    - candle_starts.page2
                ),
                starts=candle_starts,
            )
            members.append(
                MemberAssemblyInputV1(
                    proof_by_symbol[symbol],
                    tuple(fundamentals[symbol]),
                    page1,
                    page2,
                )
            )
    except _QuantCandleBudgetExceeded:
        return _settle_no_pack(
            receipts=receipts,
            membership=membership,
            holdings=holdings,
            membership_digest=membership_digest,
            provenance_digest=sha256(b"candle_budget_exhausted").hexdigest(),
            reason="candle_budget_exhausted",
            page1_starts=candle_starts.page1,
            page2_starts=candle_starts.page2,
            deps=deps,
        )
    except Exception:
        return _settle_no_pack(
            receipts=receipts,
            membership=membership,
            holdings=holdings,
            membership_digest=membership_digest,
            provenance_digest=sha256(b"candle_unavailable").hexdigest(),
            reason="candle_unavailable",
            page1_starts=candle_starts.page1,
            page2_starts=candle_starts.page2,
            deps=deps,
        )
    assembled = assemble_quant_multifactor_input(
        AssemblyInputV1(
            holdings,
            holdings.captured_at,
            tuple(members),
            QUANT_RECURRING_UNIVERSE_ID,
            QUANT_RECURRING_UNIVERSE_POLICY_VERSION,
            QUANT_RECURRING_ADJUSTMENT_VERSION,
        )
    )
    if assembled.cohort_input is None:
        return _settle_no_pack(
            receipts=receipts,
            membership=membership,
            holdings=holdings,
            membership_digest=membership_digest,
            provenance_digest=assembled.provenance_digest,
            reason="admissible_member_count",
            page1_starts=candle_starts.page1,
            page2_starts=candle_starts.page2,
            admissible_count=assembled.admissible_count,
            deps=deps,
        )
    pack = evaluate_quant_multifactor_core_v1(assembled.cohort_input)
    receipts.finish(
        membership.identity.identity_digest,
        state=QuantTerminalStateV1.SUCCEEDED,
        reason_code="degraded_pack" if pack.cohort_size < pack.min_cohort else "pack",
        finished_at=deps.clock(),
        result=_result(
            membership=membership,
            holdings=holdings,
            membership_digest=membership_digest,
            provenance_digest=assembled.provenance_digest,
            pack_digest=pack.pack_digest,
            membership_starts=membership.membership_starts,
            page1_starts=candle_starts.page1,
            page2_starts=candle_starts.page2,
            admissible_count=assembled.admissible_count,
            degraded=pack.cohort_size < pack.min_cohort,
            limitations=pack.limitations,
        ),
    )
    return (
        "ok",
        "degraded_pack" if pack.cohort_size < pack.min_cohort else "pack",
    )
