"""Receipt-once, immutable-pack-only analysis material barrier.

Production entry: ``assemble_analysis_materials`` — the canonical
``CadenceTickV1`` and the tick's ``OpenMarketMaskV1`` are mandatory, the mask
is the one session/date authority (open markets and each market's local date),
and market-local dates of captured instants come from IANA ``ZoneInfo``
conversion only. The historical (pre-mask) assembly lives in the sealed
legacy module ``tokmo.programs.legacy_g2``.
"""

from datetime import datetime, timedelta
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

from tokmo.values.analysis_barrier import (
    AnalysisBarrierResultV1, MarketReadiness, VerifiedMaterialV1,
)
from tokmo.values.cadence_policy import CadenceTickV1
from tokmo.values.material_pack import JobKind, MaterialPackV1
from tokmo.values.open_market_mask import (
    MaskBindingError, require_tick_mask_binding,
)
from tokmo.values.ops_tick import TickId
from tokmo.values.receipt_v1 import ReceiptV1
from tokmo.values.session_gate import MARKET_TIMEZONE, Market


# Unforgeable module-private capability (Stage 2 repair M1): the mask-less
# legacy assembly mode exists only behind this exact token, presented solely
# by the sealed ``tokmo.programs.legacy_g2`` module. A production caller
# passing ``mask=None`` — with or without a forged token — fails closed
# before any receipt/pack read or analysis transition.
_LEGACY_G2_CAPABILITY = object()


def _require_masked_or_legacy(tick, mask, legacy_market_date_ok, legacy):
    legacy_mode = legacy is _LEGACY_G2_CAPABILITY
    if tick is None:
        raise PermissionError("tick identity is mandatory")
    if (mask is None) != legacy_mode:
        raise PermissionError(
            "mask-less operation is sealed to the legacy G2 capability"
        )
    if (legacy_market_date_ok is not None) != legacy_mode:
        raise PermissionError("legacy market-date freshness is legacy-only")


def assemble_analysis_materials(
    *, tick, mask,
    receipt_readers: Mapping[JobKind, Callable[[], ReceiptV1]],
    pack_reader: Callable[[str, str], MaterialPackV1], now: datetime,
) -> AnalysisBarrierResultV1:
    """Production Stage 2 analysis barrier.

    Policy version, scheduled slot, tick id, and digest are all compared
    before any receipt/pack read or analysis transition (B1/M1). Every
    consumed material/receipt must carry an admissibility proof bound to the
    current exact mask (B4), and market-date freshness uses the mask entry's
    local date with ``ZoneInfo`` conversion (M2).
    """
    require_tick_mask_binding(mask, tick)
    return _assemble(
        tick=tick, open_markets=mask.markets, receipt_readers=receipt_readers,
        pack_reader=pack_reader, now=now, mask=mask, legacy_market_date_ok=None,
    )


def _assemble(
    *, tick, open_markets: frozenset[Market],
    receipt_readers: Mapping[JobKind, Callable[[], ReceiptV1]],
    pack_reader: Callable[[str, str], MaterialPackV1], now: datetime,
    mask, legacy_market_date_ok, legacy=None,
) -> AnalysisBarrierResultV1:
    _require_masked_or_legacy(tick, mask, legacy_market_date_ok, legacy)
    if not isinstance(tick, (TickId, CadenceTickV1)):
        raise TypeError("tick must be a tick identity")
    mask_digest = None if mask is None else mask.digest
    if type(open_markets) is not frozenset or not open_markets <= frozenset(Market):
        raise ValueError("invalid open markets")
    if set(receipt_readers) != set(JobKind):
        raise ValueError("exactly three receipt readers required")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    readiness = {market: MarketReadiness.SKIP_CLOSED for market in Market}
    stale_jobs = {market: frozenset() for market in Market}
    material_ticks = {job: None for job in JobKind}
    if not open_markets:
        return AnalysisBarrierResultV1(
            tick.tick_id, {}, readiness, stale_jobs, material_ticks, (), frozenset(),
            mask_digest=mask_digest,
        )

    materials = {}
    receipts = {}
    incidents = []
    for job in JobKind:
        try:
            receipt = receipt_readers[job]()
            if not isinstance(receipt, ReceiptV1) or receipt.job is not job:
                raise ValueError("receipt identity")
            success = receipt.last_success
            if success is None:
                raise ValueError("last success absent")
            pack = pack_reader(success.pack_path, success.pack_sha256)
            if (not isinstance(pack, MaterialPackV1) or pack.job is not job
                    or pack.tick_id != success.tick_id
                    or pack.captured_at != success.captured_at):
                raise ValueError("pack identity")
            if not set(pack.market_partition) <= {market.value for market in Market}:
                raise ValueError("pack partition")
            if mask is not None:
                # Same-tick material must have been produced under exactly
                # this mask; a closed-market fact or a foreign/absent digest
                # fails the tick closed instead of being silently filtered.
                same_tick_pack = pack.tick_id == mask.tick_id
                same_tick_attempt = receipt.attempt.tick_id == mask.tick_id
                if same_tick_pack:
                    if pack.mask_digest != mask.digest:
                        raise MaskBindingError(
                            "same-tick material mask digest mismatch")
                    if not {Market(value) for value in pack.market_partition} <= mask.markets:
                        raise MaskBindingError(
                            "material contains a market outside the mask")
                if same_tick_attempt and receipt.mask_digest != mask.digest:
                    raise MaskBindingError(
                        "same-tick receipt mask digest mismatch")
                if not (same_tick_pack and same_tick_attempt):
                    # A prior-tick pack/receipt carries no admissibility
                    # proof bound to the current exact mask: it is excluded
                    # here, before any candidate/analysis transition, so a
                    # closed-market row from a historical pack can never be
                    # filtered late or become a candidate.
                    incidents.append(f"{job.value}_material_unbound")
                    continue
            receipts[job] = receipt
            materials[job] = VerifiedMaterialV1(
                job, pack.tick_id, pack.captured_at, success.pack_sha256,
                pack.content_hash, pack.payload, pack.market_partition,
            )
            material_ticks[job] = pack.tick_id
        except MaskBindingError:
            raise
        except ValueError:
            incidents.append(f"{job.value}_material_unavailable")

    if mask is not None:
        def _market_date_ok(captured_at, market):
            # M2: the mask entry's market-local date is the one session-date
            # authority; captured instants convert through IANA ZoneInfo.
            zone = ZoneInfo(MARKET_TIMEZONE[market])
            return captured_at.astimezone(zone).date() == mask.entries[market].local_date
    else:
        def _market_date_ok(captured_at, market):
            return legacy_market_date_ok(captured_at, market, now)

    holdings = materials.get(JobKind.HOLDINGS)
    holdings_receipt = receipts.get(JobKind.HOLDINGS)
    for market in Market:
        if market not in open_markets:
            continue
        if holdings is None or holdings_receipt is None:
            readiness[market] = MarketReadiness.UNAVAILABLE
            incidents.append(f"{market.value.lower()}_holdings_unavailable")
            continue
        freshness = holdings_receipt.markets.get(market.value)
        if (freshness is None or freshness.last_success_tick_id is None
                or freshness.captured_at is None
                or market.value not in holdings.market_partition
                or freshness.last_success_tick_id != holdings.pack_tick_id
                or not _market_date_ok(freshness.captured_at, market)):
            readiness[market] = MarketReadiness.SKIP_HOLDINGS_MISSING
            incidents.append(f"{market.value.lower()}_holdings_missing")
            continue
        if freshness.captured_at < tick.start_utc - timedelta(seconds=600):
            readiness[market] = MarketReadiness.SKIP_HOLDINGS_STALE
            incidents.append(f"{market.value.lower()}_holdings_stale")
            continue
        readiness[market] = MarketReadiness.READY
        stale = set()
        for job in (JobKind.QUOTES, JobKind.TRADES):
            material = materials.get(job)
            receipt = receipts.get(job)
            fresh = None if receipt is None else receipt.markets.get(market.value)
            if (material is None or fresh is None or fresh.captured_at is None
                    or market.value not in material.market_partition
                    or fresh.last_success_tick_id != material.pack_tick_id
                    or fresh.captured_at < tick.start_utc - timedelta(seconds=600)
                    or not _market_date_ok(fresh.captured_at, market)):
                stale.add(job)
        stale_jobs[market] = frozenset(stale)

    proceed = frozenset(
        market for market, state in readiness.items()
        if state is MarketReadiness.READY
    )
    return AnalysisBarrierResultV1(
        tick.tick_id, materials, readiness, stale_jobs, material_ticks,
        tuple(dict.fromkeys(incidents)), proceed, mask_digest=mask_digest,
    )
