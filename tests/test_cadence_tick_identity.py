"""Stage 2 repair B1: one canonical versioned cadence tick identity.

``CadenceTickV1`` is the only tick identity carrier: ``tick_id`` is derived
from ``(policy_version, scheduled_for_utc)`` through the central policy
registry, constructors validate the derivation, and every production masked
seam (holdings/quotes/trades collectors and the analysis barrier) compares
policy version + scheduled slot + tick id + digest against the bound
``OpenMarketMaskV1`` before any provider I/O, receipt/lock/scan-state
mutation, or analysis transition. A self-consistent mask for a different
policy or slot — even with the same attempted ``tick_id`` — is rejected.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType, SimpleNamespace

import pytest

from tokmo.programs.analysis_barrier import assemble_analysis_materials
from tokmo.programs.holdings_sync import run_holdings_sync
from tokmo.programs.quotes_sync import run_quotes_sync
from tokmo.programs.trades_sync import run_trades_sync
from tokmo.records.order_scan_state import OrderScanStateStore
from tokmo.values import cadence_policy
from tokmo.values.cadence_policy import (
    CADENCE_POLICY_V1,
    CadencePolicyV1,
    CadenceTickV1,
    cadence_tick_v1,
)
from tokmo.values.open_market_mask import (
    MaskBindingError,
    build_open_market_mask,
    require_tick_mask_binding,
)
from tokmo.values.session_gate import Market, SessionVerdict
from tokmo.values.toss_market_calendar import CalendarSession

POLICY = "cadence-300s-v1"
SLOT = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
TICK = cadence_tick_v1(POLICY, SLOT)

SESSION = CalendarSession(
    datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc),
    datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc),
)


def mask_at(tick, kr_reason="after_close", us_reason="open"):
    verdicts = (
        SessionVerdict(Market.KR, kr_reason == "open", kr_reason, SESSION),
        SessionVerdict(Market.US, us_reason == "open", us_reason, SESSION),
    )
    return build_open_market_mask(
        policy_version=tick.policy_version,
        scheduled_for_utc=tick.scheduled_for_utc, tick_id=tick.tick_id,
        verdicts=verdicts,
        local_dates={Market.KR: date(2026, 8, 4), Market.US: date(2026, 8, 3)},
    )


US_ONLY = mask_at(TICK)
NEXT_TICK = cadence_tick_v1(POLICY, SLOT + timedelta(seconds=300))


# -- CadenceTickV1: canonical derivation only --------------------------------------


def test_tick_id_is_derived_from_policy_and_slot():
    assert TICK.tick_id == int(SLOT.timestamp()) // 300
    assert TICK.policy_version == POLICY
    assert TICK.scheduled_for_utc == SLOT


def test_caller_chosen_tick_id_is_rejected():
    with pytest.raises(ValueError):
        CadenceTickV1(POLICY, SLOT, TICK.tick_id + 1)
    with pytest.raises(ValueError):
        CadenceTickV1(POLICY, SLOT, 100)


def test_unknown_policy_version_is_rejected():
    with pytest.raises(ValueError):
        CadenceTickV1("cadence-999s-vX", SLOT, TICK.tick_id)


def test_unaligned_or_non_utc_slot_is_rejected():
    with pytest.raises(ValueError):
        cadence_tick_v1(POLICY, SLOT + timedelta(seconds=60))
    with pytest.raises(ValueError):
        cadence_tick_v1(POLICY, SLOT.replace(microsecond=1))
    with pytest.raises(ValueError):
        cadence_tick_v1(POLICY, SLOT.replace(tzinfo=None))
    # aware but not the canonical UTC representation
    kst = SLOT.astimezone(timezone(timedelta(hours=9)))
    with pytest.raises(ValueError):
        cadence_tick_v1(POLICY, kst)


# -- mask binds the exact canonical carrier ----------------------------------------


def test_mask_rejects_non_canonical_tick_identity():
    with pytest.raises(ValueError):
        build_open_market_mask(
            policy_version=POLICY, scheduled_for_utc=SLOT,
            tick_id=TICK.tick_id + 1,
            verdicts=(
                SessionVerdict(Market.KR, False, "after_close", SESSION),
                SessionVerdict(Market.US, True, "open", SESSION),
            ),
            local_dates={Market.KR: date(2026, 8, 4), Market.US: date(2026, 8, 3)},
        )


def test_mask_exposes_the_canonical_tick_carrier():
    assert US_ONLY.tick == TICK


def test_require_tick_mask_binding_accepts_exact_identity():
    assert require_tick_mask_binding(US_ONLY, TICK) is US_ONLY


def test_require_tick_mask_binding_rejects_foreign_slot_mask():
    with pytest.raises(MaskBindingError):
        require_tick_mask_binding(US_ONLY, NEXT_TICK)
    with pytest.raises(MaskBindingError):
        require_tick_mask_binding(mask_at(NEXT_TICK), TICK)


def test_require_tick_mask_binding_requires_canonical_carrier():
    with pytest.raises(MaskBindingError):
        require_tick_mask_binding(US_ONLY, SimpleNamespace(
            policy_version=POLICY, scheduled_for_utc=SLOT, tick_id=TICK.tick_id,
        ))
    with pytest.raises(MaskBindingError):
        require_tick_mask_binding("not-a-mask", TICK)


def foreign_policy_mask_with_same_tick_id(monkeypatch):
    """A self-consistent mask under a different policy whose derivation lands
    on the same attempted tick_id (60s policy at tick_id*60)."""
    foreign = CadencePolicyV1(
        policy_version="cadence-60s-test", cadence_seconds=60,
        max_start_lag_seconds=12, child_timeout_seconds=50,
        termination_margin_seconds=10,
    )
    monkeypatch.setattr(
        cadence_policy, "CADENCE_POLICY_REGISTRY",
        MappingProxyType({
            CADENCE_POLICY_V1.policy_version: CADENCE_POLICY_V1,
            foreign.policy_version: foreign,
        }),
    )
    slot60 = datetime.fromtimestamp(TICK.tick_id * 60, tz=timezone.utc)
    foreign_tick = cadence_tick_v1("cadence-60s-test", slot60)
    assert foreign_tick.tick_id == TICK.tick_id
    return mask_at(foreign_tick)


def test_same_tick_id_foreign_policy_mask_is_rejected(monkeypatch):
    foreign_mask = foreign_policy_mask_with_same_tick_id(monkeypatch)
    assert foreign_mask.tick_id == TICK.tick_id
    with pytest.raises(MaskBindingError):
        require_tick_mask_binding(foreign_mask, TICK)


# -- seams: mismatch => provider 0 / mutation 0 ------------------------------------


class ReceiptSink:
    def __init__(self):
        self.value = None

    def read(self):
        return self.value

    def replace(self, value):
        pytest.fail("receipt must not mutate on identity mismatch")


class NoStore:
    def replace(self, *args, **kwargs):
        pytest.fail("store must not mutate on identity mismatch")


class NoPacks:
    def write(self, pack):
        pytest.fail("pack must not be written on identity mismatch")

    def list_packs(self, job):
        pytest.fail("packs must not be read on identity mismatch")

    def reap_orphans(self, *args):
        pytest.fail("packs must not be reaped on identity mismatch")


def _seam_masks(monkeypatch):
    return [mask_at(NEXT_TICK), foreign_policy_mask_with_same_tick_id(monkeypatch)]


def test_holdings_seam_rejects_every_foreign_identity(tmp_path, monkeypatch):
    for index, mask in enumerate(_seam_masks(monkeypatch)):
        receipts = ReceiptSink()
        with pytest.raises(MaskBindingError):
            run_holdings_sync(
                tick=TICK, mask=mask,
                holdings_provider=lambda: pytest.fail("provider must not run"),
                store=NoStore(), pack_writer=NoPacks(), receipt_store=receipts,
                reservation_seal=lambda *args: None,
                incident_writer=lambda *args: None,
                clock=lambda: SLOT, now=SLOT,
                job_lock_path=tmp_path / f"h{index}.lock",
            )
        assert receipts.value is None
        assert not (tmp_path / f"h{index}.lock").exists()


def test_quotes_seam_rejects_every_foreign_identity(tmp_path, monkeypatch):
    for index, mask in enumerate(_seam_masks(monkeypatch)):
        receipts = ReceiptSink()
        with pytest.raises(MaskBindingError):
            run_quotes_sync(
                tick=TICK, mask=mask, holdings=None,
                quotes_provider=lambda batch: pytest.fail("provider must not run"),
                quote_store=lambda outcome: pytest.fail("store must not mutate"),
                pack_writer=NoPacks(), receipt_store=receipts,
                reservation_seal=lambda *args: None,
                incident_writer=lambda *args: None,
                clock=lambda: SLOT, now=SLOT,
                job_lock_path=tmp_path / f"q{index}.lock",
            )
        assert receipts.value is None
        assert not (tmp_path / f"q{index}.lock").exists()


def test_trades_seam_rejects_every_foreign_identity(tmp_path, monkeypatch):
    for index, mask in enumerate(_seam_masks(monkeypatch)):
        receipts = ReceiptSink()
        scan_path = tmp_path / f"scan{index}.sqlite3"
        with pytest.raises(MaskBindingError):
            run_trades_sync(
                tick=TICK, mask=mask, holdings=None,
                orders_provider=lambda **kwargs: pytest.fail("provider must not run"),
                detail_provider=lambda **kwargs: pytest.fail("detail must not run"),
                store=NoStore(),
                scan_state=OrderScanStateStore(scan_path, clock=lambda: SLOT),
                pack_writer=NoPacks(), receipt_store=receipts,
                reservation_seal=lambda *args: pytest.fail("no reservation"),
                incident_writer=lambda *args: None,
                regular_session_dates=SimpleNamespace(
                    regular_session_dates=lambda *args: pytest.fail("no calendar"),
                ),
                clock=lambda: SLOT, now=SLOT,
                job_lock_path=tmp_path / f"t{index}.lock",
            )
        assert receipts.value is None
        state = OrderScanStateStore(scan_path, clock=lambda: SLOT)
        assert state.read_incomplete_retry() == ()
        assert not (tmp_path / f"t{index}.lock").exists()


def test_barrier_seam_rejects_every_foreign_identity(monkeypatch):
    for mask in _seam_masks(monkeypatch):
        with pytest.raises(MaskBindingError):
            assemble_analysis_materials(
                tick=TICK, mask=mask,
                receipt_readers={
                    job: (lambda job=job: pytest.fail("receipts must not be read"))
                    for job in __import__(
                        "tokmo.values.material_pack", fromlist=["JobKind"]
                    ).JobKind
                },
                pack_reader=lambda *args: pytest.fail("packs must not be read"),
                now=SLOT,
            )
