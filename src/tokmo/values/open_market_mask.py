"""Immutable tick-bound open-market mask (SDS §6, Stage 2).

``OpenMarketMaskV1`` is computed exactly once per cadence tick, after the
independent market-local calendar refresh and session judgment. It carries the
versioned tick identity, the exact open-market subset, and each market's
local-date/session-verdict identity, sealed by one canonical digest. Every
downstream stage (holdings, quotes, trades, barrier, candidate/analysis,
receipts, material packs) binds to this exact object/digest instead of
reinterpreting session or calendar state; a missing or mismatched binding
fails closed before provider I/O or an analysis transition.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType

from tokmo.values.cadence_policy import CadenceTickV1
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    CurrentHoldingsV1,
    classify_market,
)
from tokmo.values.material_pack import canonical_encode
from tokmo.values.session_gate import (
    ENABLED_SESSION_KINDS,
    SESSION_REASON_TOKENS,
    Market,
    SessionVerdict,
)

MASK_DIGEST_DOMAIN = "tokmo-v3:open-market-mask"


class MaskBindingError(ValueError):
    """Missing/mismatched open-market mask identity; the boundary fails closed."""


def _aware(value: datetime, name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be an aware datetime")


@dataclass(frozen=True)
class MaskMarketEntryV1:
    """One market's local-date and session-verdict identity inside the mask."""

    market: Market
    local_date: date
    reason: str
    session_start_utc: datetime | None
    session_end_utc: datetime | None

    def __post_init__(self):
        if not isinstance(self.market, Market):
            raise ValueError("unknown market")
        if type(self.local_date) is not date or isinstance(self.local_date, datetime):
            raise ValueError("market local date must be a date")
        if self.reason not in SESSION_REASON_TOKENS:
            raise ValueError("unknown session reason")
        boundaries = (self.session_start_utc, self.session_end_utc)
        if (self.session_start_utc is None) != (self.session_end_utc is None):
            raise ValueError("session boundaries must be both present or absent")
        if self.reason == "open" and self.session_start_utc is None:
            raise ValueError("open entry requires session boundaries")
        if self.session_start_utc is not None:
            for boundary, name in zip(
                boundaries, ("session_start_utc", "session_end_utc")
            ):
                _aware(boundary, name)
            if self.session_start_utc >= self.session_end_utc:
                raise ValueError("session must have positive duration")


def _entry_payload(entry: MaskMarketEntryV1) -> dict:
    return {
        "market": entry.market.value,
        "local_date": entry.local_date.isoformat(),
        "reason": entry.reason,
        "session_start_utc": entry.session_start_utc,
        "session_end_utc": entry.session_end_utc,
    }


def mask_digest_v1(
    policy_version: str,
    scheduled_for_utc: datetime,
    tick_id: int,
    entries: Mapping[Market, MaskMarketEntryV1],
) -> str:
    payload = {
        "v": 1,
        "domain": MASK_DIGEST_DOMAIN,
        "policy_version": policy_version,
        "scheduled_for_utc": scheduled_for_utc,
        "tick_id": tick_id,
        # Bind the complete ordered collection-session policy into every mask
        # digest. Adding/removing a named session therefore changes audit
        # identity even when the currently open market subset is unchanged.
        "enabled_sessions": {
            market.value: [kind.value for kind in ENABLED_SESSION_KINDS[market]]
            for market in sorted(Market, key=lambda item: item.value)
        },
        "markets": sorted(
            market.value for market, entry in entries.items() if entry.reason == "open"
        ),
        "entries": {
            market.value: _entry_payload(entry) for market, entry in entries.items()
        },
    }
    return hashlib.sha256(canonical_encode(payload)).hexdigest()


@dataclass(frozen=True)
class OpenMarketMaskV1:
    """Closed immutable open-market mask for exactly one versioned tick."""

    policy_version: str
    scheduled_for_utc: datetime
    tick_id: int
    markets: frozenset[Market]
    entries: Mapping[Market, MaskMarketEntryV1]
    digest: str

    def __post_init__(self):
        if type(self.policy_version) is not str or not self.policy_version:
            raise ValueError("invalid policy version")
        _aware(self.scheduled_for_utc, "scheduled_for_utc")
        if type(self.tick_id) is not int or self.tick_id < 0:
            raise ValueError("tick_id must be a non-negative int")
        # B1: the mask binds the one canonical versioned tick identity — the
        # carrier constructor validates that tick_id derives from
        # (policy_version, scheduled slot), so no caller-chosen collision can
        # produce a self-consistent mask for a foreign tick.
        CadenceTickV1(self.policy_version, self.scheduled_for_utc, self.tick_id)
        if not isinstance(self.entries, Mapping):
            raise ValueError("entries must be a mapping")
        if set(self.entries) != set(Market):
            raise ValueError("mask requires an entry for every market")
        for market, entry in self.entries.items():
            if not isinstance(entry, MaskMarketEntryV1) or entry.market is not market:
                raise ValueError("entry market identity mismatch")
        frozen = MappingProxyType(
            {
                market: self.entries[market]
                for market in sorted(Market, key=lambda item: item.value)
            }
        )
        object.__setattr__(self, "entries", frozen)
        expected_markets = frozenset(
            market for market, entry in frozen.items() if entry.reason == "open"
        )
        if type(self.markets) is not frozenset or self.markets != expected_markets:
            raise ValueError("mask markets must equal the open entries exactly")
        expected_digest = mask_digest_v1(
            self.policy_version,
            self.scheduled_for_utc,
            self.tick_id,
            frozen,
        )
        if self.digest != expected_digest:
            raise ValueError("mask digest mismatch")

    @property
    def tick(self) -> CadenceTickV1:
        """The exact canonical tick carrier this mask is bound to."""
        return CadenceTickV1(
            self.policy_version,
            self.scheduled_for_utc,
            self.tick_id,
        )


def build_open_market_mask(
    *,
    policy_version: str,
    scheduled_for_utc: datetime,
    tick_id: int,
    verdicts,
    local_dates,
) -> OpenMarketMaskV1:
    """Build the tick's one mask from judged verdicts and market-local dates."""
    entries = {}
    for verdict in tuple(verdicts):
        if not isinstance(verdict, SessionVerdict):
            raise ValueError("mask requires typed session verdicts")
        if verdict.market in entries:
            raise ValueError("duplicate verdict market")
        session = verdict.session
        entries[verdict.market] = MaskMarketEntryV1(
            verdict.market,
            local_dates[verdict.market],
            verdict.reason,
            None if session is None else session.start_time,
            None if session is None else session.end_time,
        )
    if set(entries) != set(Market):
        raise ValueError("mask requires a verdict for every market")
    markets = frozenset(
        market for market, entry in entries.items() if entry.reason == "open"
    )
    digest = mask_digest_v1(policy_version, scheduled_for_utc, tick_id, entries)
    return OpenMarketMaskV1(
        policy_version,
        scheduled_for_utc,
        tick_id,
        markets,
        entries,
        digest,
    )


def require_mask_binding(
    mask, *, tick_id=None, policy_version=None, scheduled_for_utc=None, digest=None
) -> OpenMarketMaskV1:
    """Fail closed unless ``mask`` is a real mask matching every given identity."""
    if not isinstance(mask, OpenMarketMaskV1):
        raise MaskBindingError("open-market mask required")
    if tick_id is not None and mask.tick_id != tick_id:
        raise MaskBindingError("mask tick identity mismatch")
    if policy_version is not None and mask.policy_version != policy_version:
        raise MaskBindingError("mask policy version mismatch")
    if scheduled_for_utc is not None and mask.scheduled_for_utc != scheduled_for_utc:
        raise MaskBindingError("mask scheduled slot mismatch")
    if digest is not None and mask.digest != digest:
        raise MaskBindingError("mask digest mismatch")
    return mask


def require_tick_mask_binding(mask, tick) -> OpenMarketMaskV1:
    """Fail closed unless ``mask`` matches the canonical tick carrier exactly.

    Every production masked seam calls this before provider I/O or any
    receipt/lock/scan-state mutation: policy version, scheduled slot, tick id,
    and the recomputed digest are each compared independently (B1).
    """
    if not isinstance(tick, CadenceTickV1):
        raise MaskBindingError("canonical cadence tick identity required")
    if not isinstance(mask, OpenMarketMaskV1):
        raise MaskBindingError("open-market mask required")
    if mask.policy_version != tick.policy_version:
        raise MaskBindingError("mask policy version mismatch")
    if mask.scheduled_for_utc != tick.scheduled_for_utc:
        raise MaskBindingError("mask scheduled slot mismatch")
    if mask.tick_id != tick.tick_id:
        raise MaskBindingError("mask tick identity mismatch")
    if mask.digest != mask_digest_v1(
        mask.policy_version, mask.scheduled_for_utc, mask.tick_id, mask.entries
    ):
        raise MaskBindingError("mask digest mismatch")
    return mask


@dataclass(frozen=True)
class OpenMarketHoldingsProjectionV1:
    """Explicit open-market analysis projection of one canonical snapshot.

    The canonical account-wide snapshot (KR+US may coexist) stays complete in
    its store; this projection is what candidate/analysis inputs may see.
    Unknown-market holdings never fall back to a market — they are excluded
    and reported.
    """

    mask_digest: str
    tick_id: int
    accounts: tuple[AccountHoldingsV1, ...]
    admitted_markets: frozenset[Market]
    unknown_symbols: frozenset[str]


def project_open_market_holdings(
    state: CurrentHoldingsV1, mask
) -> OpenMarketHoldingsProjectionV1:
    """Project the canonical snapshot onto the mask's exact open markets."""
    require_mask_binding(mask)
    if not isinstance(state, CurrentHoldingsV1):
        raise ValueError("projection requires canonical current holdings")
    unknown = set()
    accounts = []
    for account in state.accounts:
        kept = []
        for item in account.holdings:
            market = classify_market(item.market_raw, item.market_field)
            if market is None:
                unknown.add(item.symbol)
            elif market in mask.markets:
                kept.append(item)
        accounts.append(
            AccountHoldingsV1(account.account_scope, account.ordinal, tuple(kept))
        )
    return OpenMarketHoldingsProjectionV1(
        mask.digest,
        mask.tick_id,
        tuple(accounts),
        mask.markets,
        frozenset(unknown),
    )
