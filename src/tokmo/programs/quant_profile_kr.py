"""KR profile runtime: one STOCKS batch acquisition + evidence terminal.

Membership is exactly one ``MEMBERSHIP_TOSS_STOCKS`` wire through the shared
executor; admission is the typed fail-closed ``StockInfo`` gate over the
production-pinned vocabulary; the compliant terminal is truthfully
``NO_PACK kr_fundamentals_paused`` after candle admission evidence (§5.3).
This module consumes only typed values and injected deps — no live
transport, no source parser.
"""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from tokmo.facts.market_regime_toss_input import completed_session_boundary
from tokmo.facts.quant_kr_membership import (
    admit_kr_membership,
    kr_material_members,
    kr_membership_operation_plan,
    kr_provenance_digest,
    predict_kr_personal_cohort,
    verify_kr_cohort_echo,
)
from tokmo.facts.quant_recurring_membership import quant_candle_native_identity
from tokmo.programs.quant_multifactor_inputs import (
    page2_request_identity,
    requires_page2,
)
from tokmo.programs.quant_recurring_shadow import (
    _fresh_holdings_or_none,
    _raw_candle_page,
    _validate_candle_outcome,
)
from tokmo.records.quant_profile_receipts import QuantProfileResultV1
from tokmo.values.ops_kernel import CadenceBudgetExceeded
from tokmo.values.quant_multifactor_provenance import CandleRequestIdentityV1
from tokmo.values.quant_profiles import (
    QuantProfileDueContextV1,
    QuantProfileDueDecisionV1,
    encode_membership_material,
)
from tokmo.values.quant_recurring import QuantTerminalStateV1
from tokmo.values.session_gate import Market


class KrProfileRefusal(ValueError):
    pass


class _KrCandleBudgetExceeded(CadenceBudgetExceeded):
    def __init__(self):
        super().__init__("quant daily-candle ceiling reached")


@dataclass(frozen=True, slots=True)
class KrProfileMembershipOutcomeV1:
    context: QuantProfileDueContextV1
    infos: tuple
    observed_at: datetime | None
    membership_starts: int
    status: str
    reason: str


def run_kr_profile_membership(
    *,
    deps,
    policy,
    tick,
    executor,
    token,
    context,
    receipts,
) -> KrProfileMembershipOutcomeV1:
    del tick
    if (
        context.decision is not QuantProfileDueDecisionV1.DUE
        or context.identity is None
    ):
        raise KrProfileRefusal("context_not_due")
    if context.membership_starts_ceiling != policy.request_limits.membership_starts:
        raise KrProfileRefusal("membership_budget_binding")
    identity = context.identity
    plan = kr_membership_operation_plan(
        context.predicted_symbols,
        identity.completed_session_date,
    )
    if len(plan) != context.membership_plan_starts:
        raise KrProfileRefusal("membership_plan_starts")

    lease = receipts.acquire_identity_lease(identity.identity_digest)
    if lease is None:
        return KrProfileMembershipOutcomeV1(
            context,
            (),
            None,
            0,
            "skipped",
            "identity_in_progress",
        )
    starts = 0
    with lease:
        claim = receipts.claim(
            identity,
            owner_invocation_id=f"tick-{identity.identity_digest[:16]}",
            owner_lease_key=lease.key,
            claimed_at=deps.clock(),
            identity_lease=lease,
        )
        if claim.state is not QuantTerminalStateV1.CLAIMED:
            return KrProfileMembershipOutcomeV1(
                context,
                (),
                None,
                0,
                "skipped",
                "already_terminal",
            )
        operation, native_identity = plan[0]
        cohort = tuple(sorted(context.predicted_symbols))
        try:

            def invoke(started_claim):
                nonlocal starts
                starts += 1
                outcome = deps.stocks_membership_fetch(
                    claim=started_claim,
                    bearer=token.bearer(),
                    symbols=cohort,
                )
                code = getattr(outcome, "code", None)
                if code != "success":
                    raise ValueError(
                        "unknown" if type(code) is not str or not code else code
                    )
                return outcome

            outcome = executor.execute(operation, native_identity, invoke)
            observed_at = deps.clock()
            infos = outcome.value
            if type(infos) is not tuple:
                raise ValueError("stocks_value")
            verify_kr_cohort_echo(infos, cohort)
        except CadenceBudgetExceeded:
            receipts.finish(
                identity.identity_digest,
                state=QuantTerminalStateV1.FAILED,
                reason_code="membership_budget_exhausted",
                finished_at=deps.clock(),
            )
            return KrProfileMembershipOutcomeV1(
                context,
                (),
                None,
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
            return KrProfileMembershipOutcomeV1(
                context,
                (),
                None,
                starts,
                "failed",
                reason,
            )
    return KrProfileMembershipOutcomeV1(
        context,
        infos,
        observed_at,
        starts,
        "ok",
        "acquired",
    )


def _kr_result(
    *,
    membership,
    admitted,
    membership_digest,
    holdings_captured_at,
    provenance_digest,
    page1_starts,
    page2_starts,
    limitations,
) -> QuantProfileResultV1:
    identity = membership.context.identity
    cohort = tuple(sorted(membership.context.predicted_symbols))
    material = encode_membership_material(
        profile_id=identity.profile_id,
        policy_version=identity.policy_version,
        completed_session_date=identity.completed_session_date,
        request_identity=kr_membership_operation_plan(
            cohort,
            identity.completed_session_date,
        )[0][1],
        observed_at=membership.observed_at,
        members=kr_material_members(admitted),
    )
    return QuantProfileResultV1(
        identity_digest=identity.identity_digest,
        acquisition_id=identity.acquisition_id,
        membership_holdings_digest=membership_digest,
        holdings_captured_at=holdings_captured_at,
        membership_starts=membership.membership_starts,
        candle_page1_starts=page1_starts,
        candle_page2_starts=page2_starts,
        cohort_size=len(cohort),
        admissible_count=len(admitted),
        provenance_digest=provenance_digest,
        membership_material=material,
        membership_material_digest=sha256(material).hexdigest(),
        pack_digest="",
        degraded=True,
        limitations=tuple(limitations),
        symbols=cohort,
    )


def _settle_no_pack(
    *,
    receipts,
    membership,
    admitted,
    membership_digest,
    holdings_captured_at,
    provenance_digest,
    reason,
    page1_starts=0,
    page2_starts=0,
    deps,
):
    result = _kr_result(
        membership=membership,
        admitted=admitted,
        membership_digest=membership_digest,
        holdings_captured_at=holdings_captured_at,
        provenance_digest=provenance_digest,
        page1_starts=page1_starts,
        page2_starts=page2_starts,
        limitations=(reason.upper(),),
    )
    receipts.finish(
        membership.context.identity.identity_digest,
        state=QuantTerminalStateV1.NO_PACK,
        reason_code=reason,
        finished_at=deps.clock(),
        result=result,
    )


def _fetch_kr_candle_pages(
    *,
    deps,
    executor,
    bearer,
    member,
    session_date,
    remaining_starts,
    starts,
):
    if remaining_starts < 1:
        raise _KrCandleBudgetExceeded()
    page1_request = CandleRequestIdentityV1(
        "qt",
        member.symbol,
        member.market,
        "1d",
        200,
        False,
        1,
        None,
    )
    page1_native_identity = quant_candle_native_identity(
        market=Market.KR,
        symbol=member.symbol,
        session_date=session_date,
        page_index=1,
    )

    def fetch_page1(claim):
        return _validate_candle_outcome(
            deps.stock_candles_fetch(
                claim=claim,
                bearer=bearer,
                symbol=member.symbol,
                before=completed_session_boundary(Market.KR, session_date),
                adjusted=False,
            )
        )

    starts["page1"] += 1
    page1 = _raw_candle_page(
        page1_request,
        executor.execute(
            "STOCK_CANDLES_DAILY",
            page1_native_identity,
            fetch_page1,
        ),
    )
    if not requires_page2(page1):
        return
    if remaining_starts < 2:
        raise _KrCandleBudgetExceeded()
    page2_request = page2_request_identity(page1)
    page2_native_identity = quant_candle_native_identity(
        market=Market.KR,
        symbol=member.symbol,
        session_date=session_date,
        page_index=2,
    )

    def fetch_page2(claim):
        return _validate_candle_outcome(
            deps.stock_candles_fetch(
                claim=claim,
                bearer=bearer,
                symbol=member.symbol,
                before=page2_request.before,
                adjusted=False,
            )
        )

    starts["page2"] += 1
    _raw_candle_page(
        page2_request,
        executor.execute(
            "STOCK_CANDLES_DAILY",
            page2_native_identity,
            fetch_page2,
        ),
    )


def run_kr_profile_shadow(
    *,
    root: Path,
    deps,
    policy,
    tick,
    executor,
    token,
    membership: KrProfileMembershipOutcomeV1,
    receipts,
) -> tuple[str, str]:
    del tick
    if membership.status != "ok":
        return "skipped", membership.reason
    identity = membership.context.identity
    cohort = tuple(sorted(membership.context.predicted_symbols))
    session_date = identity.completed_session_date

    admission = admit_kr_membership(
        membership.infos,
        expected_symbols=cohort,
    )
    if not admission.admitted:
        # nothing admissible: no truthful material row is representable
        receipts.finish(
            identity.identity_digest,
            state=QuantTerminalStateV1.FAILED,
            reason_code="admission_refused",
            finished_at=deps.clock(),
        )
        return "failed", "admission_refused"

    provenance_digest = kr_provenance_digest(
        members=admission.admitted,
        request_identity=kr_membership_operation_plan(
            cohort,
            session_date,
        )[0][1],
        observed_at=membership.observed_at,
    )
    holdings, holdings_bytes, holdings_reason = _fresh_holdings_or_none(
        root=root,
        deps=deps,
        membership=SimpleNamespace(
            payloads=(SimpleNamespace(observed_at=membership.observed_at),)
        ),
    )
    if holdings is None:
        _settle_no_pack(
            receipts=receipts,
            membership=membership,
            admitted=admission.admitted,
            membership_digest=sha256(b"").hexdigest(),
            holdings_captured_at=deps.clock(),
            provenance_digest=provenance_digest,
            reason=holdings_reason,
            deps=deps,
        )
        return "skipped", holdings_reason
    membership_digest = sha256(holdings_bytes).hexdigest()
    if holdings_reason is not None:
        _settle_no_pack(
            receipts=receipts,
            membership=membership,
            admitted=admission.admitted,
            membership_digest=membership_digest,
            holdings_captured_at=holdings.captured_at,
            provenance_digest=provenance_digest,
            reason=holdings_reason,
            deps=deps,
        )
        return "skipped", holdings_reason
    if predict_kr_personal_cohort(holdings) != cohort:
        _settle_no_pack(
            receipts=receipts,
            membership=membership,
            admitted=admission.admitted,
            membership_digest=membership_digest,
            holdings_captured_at=holdings.captured_at,
            provenance_digest=provenance_digest,
            reason="cohort_mismatch",
            deps=deps,
        )
        return "skipped", "cohort_mismatch"
    if admission.refusals:
        _settle_no_pack(
            receipts=receipts,
            membership=membership,
            admitted=admission.admitted,
            membership_digest=membership_digest,
            holdings_captured_at=holdings.captured_at,
            provenance_digest=provenance_digest,
            reason="kr_member_inadmissible",
            deps=deps,
        )
        return "skipped", "kr_member_inadmissible"

    try:
        bearer = token.bearer()
    except Exception:
        receipts.finish(
            identity.identity_digest,
            state=QuantTerminalStateV1.FAILED,
            reason_code="token_unavailable",
            finished_at=deps.clock(),
        )
        return "failed", "token_unavailable"

    starts = {"page1": 0, "page2": 0}
    try:
        for member in admission.admitted:
            _fetch_kr_candle_pages(
                deps=deps,
                executor=executor,
                bearer=bearer,
                member=member,
                session_date=session_date,
                remaining_starts=(
                    policy.request_limits.quant_daily_candle_starts
                    - starts["page1"]
                    - starts["page2"]
                ),
                starts=starts,
            )
    except _KrCandleBudgetExceeded:
        # M1: operational budget exhaustion is a truthful FAILED terminal,
        # never a NO_PACK insufficiency — the tick becomes PARTIAL.
        receipts.finish(
            identity.identity_digest,
            state=QuantTerminalStateV1.FAILED,
            reason_code="candle_budget_exhausted",
            finished_at=deps.clock(),
        )
        return "failed", "candle_budget_exhausted"
    except Exception:
        # M1: a candle provider/contract exception is a truthful FAILED
        # terminal — the tick becomes PARTIAL.
        receipts.finish(
            identity.identity_digest,
            state=QuantTerminalStateV1.FAILED,
            reason_code="candle_unavailable",
            finished_at=deps.clock(),
        )
        return "failed", "candle_unavailable"

    # KR fundamentals are PAUSED (§5.3): the compliant terminal is truthfully
    # NO_PACK after full membership + candle admission evidence.
    result = _kr_result(
        membership=membership,
        admitted=admission.admitted,
        membership_digest=membership_digest,
        holdings_captured_at=holdings.captured_at,
        provenance_digest=provenance_digest,
        page1_starts=starts["page1"],
        page2_starts=starts["page2"],
        limitations=("KR_FUNDAMENTALS_PAUSED",),
    )
    receipts.finish(
        identity.identity_digest,
        state=QuantTerminalStateV1.NO_PACK,
        reason_code="kr_fundamentals_paused",
        finished_at=deps.clock(),
        result=result,
    )
    return "ok", "kr_fundamentals_paused"
