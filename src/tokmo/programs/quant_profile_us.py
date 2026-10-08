"""US profile adapter: the existing V1 lane presented as the profile contract.

Reuses the production seams byte-for-byte (§4): cohort, plan, transport,
assembly, candles, and fundamentals all run through the untouched V1 modules
(`run_quant_membership_acquisition` / `run_quant_recurring_shadow`). The only
new code is this thin adapter plus a receipt bridge that lands the V1 lane's
claim/finish discipline in the fresh profile store and derives the normalized
US membership material (§9) from the acquisition's typed listing
observations, including the exact ``sec_ticker_form`` boundary mapping.
"""

from dataclasses import dataclass
from hashlib import sha256

from tokmo.facts.quant_membership import sec_ticker_form
from tokmo.facts.quant_recurring_membership import membership_operation_plan
from tokmo.programs.quant_recurring_shadow import (
    QuantMembershipOutcomeV1,
    run_quant_membership_acquisition,
    run_quant_recurring_shadow,
)
from tokmo.records.quant_profile_receipts import (
    QuantProfileReceiptStore,
    QuantProfileResultV1,
)
from tokmo.values.quant_multifactor_core import MarketV1
from tokmo.values.quant_profiles import (
    QuantProfileDueContextV1,
    QuantProfileDueDecisionV1,
    QuantProfileDueIdentityV1,
    encode_membership_material,
)
from tokmo.values.quant_recurring import (
    QUANT_RECURRING_PROGRAM_ID,
    QUANT_RECURRING_UNIVERSE_POLICY_VERSION,
    QuantDueContextV1,
    QuantDueDecisionV1,
    QuantDueIdentityV1,
    membership_plan_starts,
)


class UsProfileRefusal(ValueError):
    pass


def us_profile_membership_plan(symbols, session_date):
    """Byte-equal to the existing V1 plan — same function, no re-derivation."""
    return membership_operation_plan(symbols, session_date)


def us_membership_material(
    *,
    profile_identity: QuantProfileDueIdentityV1,
    acquisition,
    symbols,
) -> bytes:
    """Normalized US membership material (§9) from the typed listing rows."""
    if acquisition is None:
        raise UsProfileRefusal("us_material_unavailable")
    listings = {row.symbol: row for row in acquisition.listings}
    members = []
    observed = []
    for symbol in sorted(symbols):
        listing = listings.get(symbol)
        if listing is None:
            raise UsProfileRefusal("us_material_listing_absent")
        observed.append(listing.observed_at)
        members.append({
            "symbol": symbol,
            "venue": listing.venue.value,
            "security_type": listing.security_class.value,
            "status": listing.financial_status_token,
            "sec_ticker_form": sec_ticker_form(symbol),
        })
    return encode_membership_material(
        profile_id=profile_identity.profile_id,
        policy_version=profile_identity.policy_version,
        completed_session_date=profile_identity.completed_session_date,
        request_identity=profile_identity.acquisition_id,
        observed_at=max(observed),
        members=tuple(members),
    )


class V1ReceiptBridgeV1:
    """Presents the V1 receipts protocol over the fresh profile store.

    The V1 lane claims and finishes exactly as today; every row lands under
    the profile identity in the fresh profile store. The V1 evidence DB is never
    opened. Result rows gain the profile acquisition id and the normalized
    membership material derived from the captured acquisition.
    """

    def __init__(
        self,
        store: QuantProfileReceiptStore,
        profile_identity: QuantProfileDueIdentityV1,
    ):
        if not isinstance(store, QuantProfileReceiptStore):
            raise UsProfileRefusal("bridge_store")
        self._store = store
        self._identity = profile_identity
        self.acquisition = None

    def acquire_identity_lease(self, identity_digest):
        del identity_digest
        return self._store.acquire_identity_lease(
            self._identity.identity_digest
        )

    def claim(self, identity, *, owner_invocation_id, owner_lease_key,
              claimed_at, identity_lease):
        del identity
        return self._store.claim(
            self._identity,
            owner_invocation_id=owner_invocation_id,
            owner_lease_key=owner_lease_key,
            claimed_at=claimed_at,
            identity_lease=identity_lease,
        )

    def read_due(self, identity_digest):
        del identity_digest
        return self._store.read_due(self._identity.identity_digest)

    def finish(self, identity_digest, *, state, reason_code, finished_at,
               result=None):
        del identity_digest
        profile_result = None
        if result is not None:
            material = us_membership_material(
                profile_identity=self._identity,
                acquisition=self.acquisition,
                symbols=result.symbols,
            )
            profile_result = QuantProfileResultV1(
                identity_digest=self._identity.identity_digest,
                acquisition_id=self._identity.acquisition_id,
                membership_holdings_digest=result.membership_holdings_digest,
                holdings_captured_at=result.holdings_captured_at,
                membership_starts=result.membership_starts,
                candle_page1_starts=result.candle_page1_starts,
                candle_page2_starts=result.candle_page2_starts,
                cohort_size=result.cohort_size,
                admissible_count=result.admissible_count,
                provenance_digest=result.provenance_digest,
                membership_material=material,
                membership_material_digest=sha256(material).hexdigest(),
                pack_digest=result.pack_digest,
                degraded=result.degraded,
                limitations=result.limitations,
                symbols=result.symbols,
            )
        self._store.finish(
            self._identity.identity_digest,
            state=state,
            reason_code=reason_code,
            finished_at=finished_at,
            result=profile_result,
        )


@dataclass(frozen=True, slots=True)
class UsProfileExecutionV1:
    context: QuantProfileDueContextV1
    membership: QuantMembershipOutcomeV1
    bridge: V1ReceiptBridgeV1


def _v1_context(context: QuantProfileDueContextV1, policy) -> QuantDueContextV1:
    identity = QuantDueIdentityV1(
        QUANT_RECURRING_PROGRAM_ID,
        context.identity.policy_version,
        MarketV1.US_NYSE_NASDAQ,
        context.identity.completed_session_date,
        QUANT_RECURRING_UNIVERSE_POLICY_VERSION,
    )
    return QuantDueContextV1(
        QuantDueDecisionV1.DUE,
        identity,
        context.completed_session_date,
        context.completed_session_date,
        context.predicted_symbols,
        context.deadline_margin_seconds,
        membership_plan_starts(len(context.predicted_symbols)),
        policy.request_limits.membership_starts,
    )


def run_us_profile_membership(
    *, root, deps, policy, tick, executor, context, receipts,
) -> UsProfileExecutionV1:
    if (
        context.decision is not QuantProfileDueDecisionV1.DUE
        or context.identity is None
    ):
        raise UsProfileRefusal("context_not_due")
    bridge = V1ReceiptBridgeV1(receipts, context.identity)
    membership = run_quant_membership_acquisition(
        root=root,
        deps=deps,
        policy=policy,
        tick=tick,
        executor=executor,
        context=_v1_context(context, policy),
        receipts=bridge,
    )
    bridge.acquisition = membership.acquisition
    return UsProfileExecutionV1(context, membership, bridge)


def run_us_profile_shadow(
    *, root, deps, policy, tick, executor, token,
    execution: UsProfileExecutionV1,
) -> tuple[str, str]:
    return run_quant_recurring_shadow(
        root=root,
        deps=deps,
        policy=policy,
        tick=tick,
        executor=executor,
        token=token,
        membership=execution.membership,
        receipts=execution.bridge,
    )
