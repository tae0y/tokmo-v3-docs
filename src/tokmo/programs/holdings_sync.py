"""Collector A: publish a global latest holdings capture through injected seams."""

import fcntl
import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from tokmo.programs.pension_materialization import build_pension_provenance
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeCollisionError,
    AccountScopeDuplicateError,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)
from tokmo.values.material_pack import (
    COMPOSITE_HOLDINGS_PARSER_VERSION,
    JobKind,
    build_material_pack_v1,
    canonical_pack_file_bytes,
)
from tokmo.values.open_market_mask import (
    project_open_market_holdings,
    require_tick_mask_binding,
)
from tokmo.values.ops_kernel import (
    OpsKernelBudgetExceeded,
    OpsKernelConflictError,
    OpsKernelCooldownActive,
)
from tokmo.values.pension_holdings import (
    PensionHoldingsRevisionV1,
    pension_account_scope,
    pension_raw_identity_fingerprint,
)
from tokmo.values.receipt_v1 import (
    AttemptStatus,
    AttemptV1,
    LastSuccessV1,
    MarketFreshness,
    ReceiptV1,
)


class _Store(Protocol):
    def replace(self, state, *, identity_fingerprints): ...


@dataclass(frozen=True)
class HoldingsSyncResult:
    status: str
    reason: str
    tick_id: int
    pack_path: str | None
    pack_sha256: str | None
    captured_at: datetime | None
    mask_digest: str | None = None
    projection: object | None = None


_AUTH = frozenset({"auth_rejection", "forbidden"})
_CONTRACT = frozenset({"contract_mismatch", "account_count_unexpected"})


def _map_holdings_outcome_reason(code: str) -> str:
    if code == "success":
        return "saved"
    if code == "http_429":
        return "failed_provider_rate_limit"
    if code in _AUTH:
        return "failed_provider_auth"
    if code in _CONTRACT:
        return "failed_provider_contract"
    if code == "ceiling_exceeded":
        return "failed_ceiling"
    return "failed_provider_other"


def _market_states(session_result, mask):
    """Market open flags: mask when bound (never reinterpreted), else verdicts."""
    if mask is not None:
        return {market.value: market in mask.markets for market in mask.entries}
    return {verdict.market.value: verdict.open for verdict in session_result.verdicts}


def _markets(states, tick_id=None, captured_at=None):
    return {
        market: MarketFreshness(
            market,
            "open" if is_open else "closed",
            tick_id,
            captured_at,
        )
        for market, is_open in states.items()
    }


def _publish_attempt(
    receipt_store, states, job, tick_id, status, reason, clock, mask_digest=None
):
    attempt = AttemptV1(tick_id, status, reason, clock())
    prev = receipt_store.read()
    prior = {} if prev is None else prev.markets
    markets = {
        market: MarketFreshness(
            market,
            "open" if is_open else "closed",
            (None if prior.get(market) is None else prior[market].last_success_tick_id),
            (None if prior.get(market) is None else prior[market].captured_at),
        )
        for market, is_open in states.items()
    }
    if prev is None:
        receipt = ReceiptV1(
            1, job, attempt, None, markets, None, mask_digest=mask_digest
        )
    else:
        receipt = ReceiptV1(
            prev.schema_version,
            prev.job,
            attempt,
            prev.last_success,
            markets,
            prev.completeness,
            mask_digest=mask_digest,
        )
    receipt_store.replace(receipt)


def _payload(state, pension_revision=None, *, tick_now=None):
    payload = {
        "accounts": [
            {
                "account_scope": account.account_scope,
                "account_type_label": state.identity_map[
                    account.account_scope
                ].account_type_label
                or "",
                "ordinal": account.ordinal,
                "holdings": [
                    {
                        "symbol": item.symbol,
                        "quantity": item.quantity,
                        "avg_price": item.avg_price,
                        "currency": item.currency,
                        "market_raw": item.market_raw,
                        "market_field": item.market_field,
                    }
                    for item in account.holdings
                ],
            }
            for account in state.accounts
        ],
        "global_inventory": True,
    }
    if pension_revision is None:
        return payload
    for account in payload["accounts"]:
        account["source_provenance"] = (
            "PENSION_EXCEL"
            if account["account_scope"] == pension_account_scope()
            else "TOSS_PERSONAL"
        )
        if account["source_provenance"] == "PENSION_EXCEL":
            provenance = build_pension_provenance(
                pension_revision,
                tick_now=tick_now,
            )
            account.update(
                {
                    "pension_revision_id": provenance["revision_id"],
                    "pension_source_as_of": provenance["source_as_of"],
                    "pension_source_age_days": provenance["source_age_days"],
                    "pension_freshness_class": provenance["freshness_class"],
                    "pension_semantic_digest": provenance["semantic_digest"],
                }
            )
    payload["payload_version"] = 2
    payload["pension_provenance"] = build_pension_provenance(
        pension_revision,
        tick_now=tick_now,
    )
    return payload


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


def _run_holdings_sync(
    *,
    tick,
    session_result,
    holdings_provider: Callable,
    store: _Store,
    pack_writer,
    receipt_store,
    reservation_seal,
    incident_writer: Callable,
    clock: Callable[[], datetime],
    now: datetime,
    mask=None,
    pension_revision: PensionHoldingsRevisionV1 | None = None,
):
    del reservation_seal
    states = _market_states(session_result, mask)
    digest = None if mask is None else mask.digest

    def _attempt(status, reason):
        _publish_attempt(
            receipt_store,
            states,
            JobKind.HOLDINGS,
            tick.tick_id,
            status,
            reason,
            clock,
            mask_digest=digest,
        )

    open_now = bool(mask.markets) if mask is not None else session_result.any_open
    if not open_now:
        _attempt(AttemptStatus.SKIPPED, "skipped_market_closed")
        return HoldingsSyncResult(
            "skipped",
            "skipped_market_closed",
            tick.tick_id,
            None,
            None,
            None,
            mask_digest=digest,
        )
    try:
        outcome = holdings_provider()
    except (OpsKernelBudgetExceeded, OpsKernelCooldownActive, OpsKernelConflictError):
        reason = "failed_ceiling"
        _attempt(AttemptStatus.FAILED, reason)
        return HoldingsSyncResult("failed", reason, tick.tick_id, None, None, None)
    except AccountScopeDuplicateError:
        reason = "failed_provider_contract"
        _attempt(AttemptStatus.FAILED, reason)
        return HoldingsSyncResult("failed", reason, tick.tick_id, None, None, None)
    except AccountScopeCollisionError:
        incident_writer("holdings-identity-collision", frozenset())
        reason = "failed_store"
        _attempt(AttemptStatus.FAILED, reason)
        return HoldingsSyncResult("failed", reason, tick.tick_id, None, None, None)
    except Exception:
        reason = "failed_provider_other"
        _attempt(AttemptStatus.FAILED, reason)
        return HoldingsSyncResult("failed", reason, tick.tick_id, None, None, None)
    if outcome.code != "success":
        reason = _map_holdings_outcome_reason(outcome.code)
        _attempt(AttemptStatus.FAILED, reason)
        return HoldingsSyncResult("failed", reason, tick.tick_id, None, None, None)
    if (
        len(outcome.facts) != outcome.account_count
        or len(outcome.account_identities) != outcome.account_count
        or len({identity.account_scope for identity in outcome.account_identities})
        != outcome.account_count
    ):
        reason = "failed_provider_contract"
        _attempt(AttemptStatus.FAILED, reason)
        return HoldingsSyncResult("failed", reason, tick.tick_id, None, None, None)
    identities = {item.account_scope: item for item in outcome.account_identities}
    accounts = list(
        AccountHoldingsV1(
            identity.account_scope,
            fact.account_ordinal,
            tuple(
                HoldingItemV1(
                    item.symbol,
                    item.quantity,
                    item.avg_price,
                    item.currency,
                    item.market_raw,
                    item.market_field,
                )
                for item in fact.holdings
            ),
        )
        for fact, identity in zip(outcome.facts, outcome.account_identities)
    )
    fingerprints = dict(outcome._identity_fingerprints)
    if pension_revision is not None:
        ordinal = len(accounts) + 1
        scope = pension_account_scope()
        identities[scope] = AccountScopeIdentityV1(
            scope,
            "pension-excel-v1",
            ordinal,
            None,
        )
        accounts.append(AccountHoldingsV1(scope, ordinal, pension_revision.holdings))
        fingerprints[scope] = pension_raw_identity_fingerprint()
    try:
        state = CurrentHoldingsV1(
            1,
            tuple(accounts),
            True,
            outcome.facts[0].captured_at,
            identities,
        )
    except (IndexError, TypeError, ValueError):
        reason = "failed_provider_contract"
        _attempt(AttemptStatus.FAILED, reason)
        return HoldingsSyncResult("failed", reason, tick.tick_id, None, None, None)
    try:
        store.replace(
            state,
            identity_fingerprints=fingerprints,
        )
    except Exception as error:
        if isinstance(error, AccountScopeCollisionError):
            incident_writer(
                "holdings-identity-collision",
                frozenset(account.account_scope for account in state.accounts),
            )
        _attempt(AttemptStatus.FAILED, "failed_store")
        return HoldingsSyncResult(
            "failed",
            "failed_store",
            tick.tick_id,
            None,
            None,
            None,
        )
    open_partition = tuple(
        sorted(market for market, is_open in states.items() if is_open)
    )
    projection = None if mask is None else project_open_market_holdings(state, mask)
    captured = state.captured_at
    pack = build_material_pack_v1(
        schema_version=1,
        job=JobKind.HOLDINGS,
        tick_id=tick.tick_id,
        market_partition=open_partition,
        payload=_payload(state, pension_revision, tick_now=now),
        captured_at=captured,
        source_timestamps={
            "provider": _iso(captured),
            "fetched": _iso(captured),
            "stored": _iso(clock()),
        },
        parser_version=(
            COMPOSITE_HOLDINGS_PARSER_VERSION
            if pension_revision is not None
            else "toss-holdings-v1"
        ),
        mask_digest=digest,
    )
    try:
        path = pack_writer.write(pack)
    except Exception:
        _attempt(AttemptStatus.FAILED, "failed_pack")
        return HoldingsSyncResult(
            "failed",
            "failed_pack",
            tick.tick_id,
            None,
            None,
            None,
        )
    pack_sha = hashlib.sha256(canonical_pack_file_bytes(pack)).hexdigest()
    completed = clock()
    receipt_store.replace(
        ReceiptV1(
            1,
            JobKind.HOLDINGS,
            AttemptV1(tick.tick_id, AttemptStatus.SAVED, "saved", completed),
            LastSuccessV1(tick.tick_id, str(path), pack_sha, captured, completed),
            _markets(states, tick.tick_id, captured),
            None,
            mask_digest=digest,
        )
    )
    recent = sorted(
        (item[0] for item in pack_writer.list_packs(JobKind.HOLDINGS)),
        reverse=True,
    )[:12]
    pack_writer.reap_orphans(JobKind.HOLDINGS, frozenset(recent), frozenset())
    return HoldingsSyncResult(
        "saved",
        "saved",
        tick.tick_id,
        str(path),
        pack_sha,
        captured,
        mask_digest=digest,
        projection=projection,
    )


def _try_job_lock(job_lock_path: Path):
    fd = None
    try:
        path = Path(job_lock_path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        if fd is not None:
            os.close(fd)
        return None
    except (OSError, TypeError, ValueError):
        if fd is not None:
            os.close(fd)
        return -1


# Unforgeable module-private capability (Stage 2 repair M1): the mask-less
# legacy mode of the locked implementation exists only behind this exact
# token, presented solely by the sealed ``tokmo.programs.legacy_g2`` module.
# A production caller passing ``mask=None`` — with or without a forged token —
# fails closed before any lock, provider I/O, or receipt mutation.
_LEGACY_G2_CAPABILITY = object()


def _require_masked_or_legacy(tick, mask, session_result, legacy):
    legacy_mode = legacy is _LEGACY_G2_CAPABILITY
    if tick is None:
        raise PermissionError("tick identity is mandatory")
    if (mask is None) != legacy_mode:
        raise PermissionError(
            "mask-less operation is sealed to the legacy G2 capability"
        )
    if not legacy_mode and session_result is not None:
        raise PermissionError("session verdict reinterpretation is legacy-only")


def run_holdings_sync(
    *,
    tick,
    mask,
    holdings_provider: Callable,
    store: _Store,
    pack_writer,
    receipt_store,
    reservation_seal,
    incident_writer: Callable,
    clock: Callable[[], datetime],
    now: datetime,
    job_lock_path: Path,
    pension_revision: PensionHoldingsRevisionV1 | None = None,
):
    """Production Stage 2 holdings collector.

    The canonical ``CadenceTickV1`` and the tick's ``OpenMarketMaskV1`` are
    mandatory; policy version, scheduled slot, tick id, and digest are all
    compared before any provider I/O, lock, or receipt mutation (B1/M1).
    """
    require_tick_mask_binding(mask, tick)
    return _locked_holdings_sync(
        tick=tick,
        session_result=None,
        holdings_provider=holdings_provider,
        store=store,
        pack_writer=pack_writer,
        receipt_store=receipt_store,
        reservation_seal=reservation_seal,
        incident_writer=incident_writer,
        clock=clock,
        now=now,
        job_lock_path=job_lock_path,
        mask=mask,
        pension_revision=pension_revision,
    )


def _locked_holdings_sync(
    *,
    tick,
    session_result,
    holdings_provider: Callable,
    store: _Store,
    pack_writer,
    receipt_store,
    reservation_seal,
    incident_writer: Callable,
    clock: Callable[[], datetime],
    now: datetime,
    job_lock_path: Path,
    mask,
    legacy=None,
    pension_revision: PensionHoldingsRevisionV1 | None = None,
):
    _require_masked_or_legacy(tick, mask, session_result, legacy)
    states = _market_states(session_result, mask)
    digest = None if mask is None else mask.digest
    fd = _try_job_lock(job_lock_path)
    if fd == -1:
        _publish_attempt(
            receipt_store,
            states,
            JobKind.HOLDINGS,
            tick.tick_id,
            AttemptStatus.FAILED,
            "failed_store",
            clock,
            mask_digest=digest,
        )
        return HoldingsSyncResult(
            "failed",
            "failed_store",
            tick.tick_id,
            None,
            None,
            None,
        )
    if fd is None:
        _publish_attempt(
            receipt_store,
            states,
            JobKind.HOLDINGS,
            tick.tick_id,
            AttemptStatus.SKIPPED,
            "skipped_lock_contention",
            clock,
            mask_digest=digest,
        )
        return HoldingsSyncResult(
            "skipped",
            "skipped_lock_contention",
            tick.tick_id,
            None,
            None,
            None,
        )
    try:
        return _run_holdings_sync(
            tick=tick,
            session_result=session_result,
            holdings_provider=holdings_provider,
            store=store,
            pack_writer=pack_writer,
            receipt_store=receipt_store,
            reservation_seal=reservation_seal,
            incident_writer=incident_writer,
            clock=clock,
            now=now,
            mask=mask,
            pension_revision=pension_revision,
        )
    finally:
        if fd >= 0:
            os.close(fd)
