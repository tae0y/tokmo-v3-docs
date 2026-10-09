"""Conditional-order submission detection lane (conditional SDS v0.4 §4).

The trade journal keeps collecting from the regular order surface only —
this lane never writes it. It scans the windowless CONDITIONAL_ORDERS list
with a cursor-advance pattern (§3.1(c)): exactly one wire per tick, one
scope by fair rotation, the OPEN/CLOSED status alternating by tick parity,
limit 20, page cursor persisted per status so a longer list drains across
same-status ticks and otherwise re-observes the latest page (restart
idempotent). The detection signal is a leg's ``triggered_order_id``
null→value transition against the durable observation snapshot; the
G1-probe-confirmed migration identity (triggered_order_id ==
source_record_key) makes the merge primary key. Before alerting, the
reverse-arrival pre-check (§3.1(d)) suppresses legs whose migrated regular
event already exists. Unmappable market strings alert nothing and are
surfaced as counts only.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from tokmo.values.account_source_profile import (
    derive_account_source_profiles,
    toss_orders_target_scopes,
)
from tokmo.values.current_holdings import CurrentHoldingsV1
from tokmo.values.instrument_display import order_alert_title
from tokmo.values.pulse_policy import PulseTickV1
from tokmo.values.trade_event import (
    conditional_alert_id,
)

CONDITIONAL_PAGE_LIMIT = 20
CONDITIONAL_PROMPT_KIND = "conditional_fill_question"
_PROMPT_VERSION = "v1"
_SCAN_STATUSES = ("OPEN", "CLOSED")
_MARKET_CLASSES = frozenset({"KR", "US"})


@dataclass(frozen=True)
class PulseConditionalPageOutcomeV1:
    """Injected CONDITIONAL_ORDERS page outcome (fixture or composition)."""

    code: str
    conditional_orders: tuple = ()
    next_cursor: "str | None" = None
    has_next: bool = False


@dataclass(frozen=True)
class ConditionalStageResult:
    status: str
    reason: str
    tick_id: int
    scanned_status: str
    observed_legs: int
    transitions: int
    alerts_created: int
    suppressed_reverse: int
    mapping_failures: int
    questions_enqueued: int
    wire_starts: int


def build_conditional_fill_question(
    *,
    account_scope: str,
    symbol: str,
    market_class: "str | None" = None,
    instrument_name: "str | None" = None,
    recommendation: "str | None" = None,
) -> str:
    """Render submission, never infer execution from a triggered order ID.

    The legacy function/prompt-kind names remain for stored reply compatibility.
    Quantity, price and fill claims require the regular-order execution record.
    A missing verified Korean identity falls back to the bare symbol.
    ``account_scope`` stays accepted for caller compatibility and is never
    exposed in this compact question.
    """
    del account_scope
    recommendation_line = (
        "- 플레이북 정보 부족으로 추천문구 없음"
        if recommendation is None
        else f"- {recommendation}"
    )
    return "\n".join(
        [
            order_alert_title(market_class, symbol, instrument_name),
            "조건에 도달해 주문이 접수됐어요.",
            "체결 여부는 아직 확인되지 않았어요.",
            "",
            "추천문구",
            recommendation_line,
            "",
            "답장으로 남긴 매매 사유는 체결 확인 전까지 임시 보관해요.",
        ]
    )


def _legs(order):
    """Leg 0 always exists; leg 1 only when a real second condition does
    (absent and MISSING-sentinel seconds carry no status attribute)."""
    legs = [(0, order.first)]
    second = getattr(order, "second", None)
    if second is not None and hasattr(second, "status"):
        legs.append((1, second))
    return legs


def _triggered_id(condition) -> "str | None":
    value = getattr(condition, "triggered_order_id", None)
    return value if isinstance(value, str) and value else None


def run_conditional_detection(
    *,
    tick: PulseTickV1,
    holdings_reader: Callable,
    native_refs_reader: Callable,
    token_provider: Callable,
    conditional_orders_fetch: Callable,
    dialogue_store,
    clock: Callable[[], datetime],
    now: datetime,
    wire_recorder: Callable,
    playbook_root=None,
) -> ConditionalStageResult:
    if not isinstance(tick, PulseTickV1):
        raise ValueError("conditional detection requires a PulseTickV1")
    del clock, now  # detected_at instants come from the store's own clock

    def _result(
        status,
        reason,
        *,
        scanned_status="",
        observed=0,
        transitions=0,
        alerts=0,
        suppressed=0,
        mapping_failures=0,
        enqueued=0,
        wires=0,
    ):
        return ConditionalStageResult(
            status,
            reason,
            tick.tick_id,
            scanned_status,
            observed,
            transitions,
            alerts,
            suppressed,
            mapping_failures,
            enqueued,
            wires,
        )

    try:
        holdings = holdings_reader()
    except Exception:
        holdings = None
    if not isinstance(holdings, CurrentHoldingsV1):
        return _result("skipped", "skipped_holdings_absent")
    try:
        refs = native_refs_reader()
    except Exception:
        refs = None
    ref_scopes = frozenset() if refs is None else frozenset(refs.scopes())
    profiles = derive_account_source_profiles(holdings, native_ref_scopes=ref_scopes)
    target_scopes = list(toss_orders_target_scopes(profiles))
    if not target_scopes:
        return _result("skipped", "skipped_no_target_scopes")

    scope = target_scopes[tick.tick_id % len(target_scopes)]
    scan_status = _SCAN_STATUSES[tick.tick_id % len(_SCAN_STATUSES)]
    state = dialogue_store.read_conditional_scan_state()
    observations = state["observations"]
    cursors = state["cursors"]

    token_provider()
    wire_recorder("CONDITIONAL_ORDERS", f"conditional:{scan_status}:0")
    try:
        outcome = conditional_orders_fetch(
            account_scope=scope,
            native_ref=refs.reveal(scope),
            status=scan_status,
            cursor=cursors.get(scan_status),
            limit=CONDITIONAL_PAGE_LIMIT,
        )
    except Exception:
        return _result(
            "failed", "failed_provider_other", scanned_status=scan_status, wires=1
        )
    code = getattr(outcome, "code", None)
    if code != "success":
        return _result(
            "failed", f"failed_provider_{code}", scanned_status=scan_status, wires=1
        )

    observed = 0
    transitions = 0
    alerts_created = 0
    suppressed = 0
    mapping_failures = 0
    enqueued = 0
    for order in outcome.conditional_orders:
        market_class = order.market if order.market in _MARKET_CLASSES else None
        for leg_index, condition in _legs(order):
            observed += 1
            key = f"{order.conditional_order_id}:{leg_index}"
            previous = observations.get(key)
            previous_id = (
                None if previous is None else previous.get("triggered_order_id")
            )
            current_id = _triggered_id(condition)
            is_transition = (
                previous is not None and previous_id is None and current_id is not None
            )
            if is_transition:
                transitions += 1
                if (
                    dialogue_store.find_event_by_source_record_key(current_id)
                    is not None
                ):
                    # §3.1(d): the migrated regular event arrived first —
                    # its question flow already ran; alert nothing.
                    suppressed += 1
                elif market_class is None:
                    # free-string market with no KR/US mapping: no alert,
                    # counted only (§4.1).
                    mapping_failures += 1
                else:
                    alert_id = conditional_alert_id(
                        order.conditional_order_id, leg_index, condition.status
                    )
                    created = dialogue_store.record_conditional_alert(
                        alert_id,
                        conditional_order_id=order.conditional_order_id,
                        leg_index=leg_index,
                        triggered_order_id=current_id,
                        account_scope=scope,
                        symbol=order.symbol,
                        market_class=market_class,
                        order_type=order.order_type,
                        quantity_ordered=order.quantity,
                        prev_status=(
                            previous.get("status") if previous else "UNOBSERVED"
                        ),
                        new_status=condition.status,
                    )
                    if created == "created":
                        alerts_created += 1
                        # A triggered order is not a fill. Keep correlation
                        # evidence, but never enqueue a submission message.
            observations[key] = {
                "status": condition.status,
                "triggered_order_id": current_id,
            }
    cursors[scan_status] = (
        outcome.next_cursor if outcome.has_next and outcome.next_cursor else None
    )
    dialogue_store.write_conditional_scan_state(
        {"observations": observations, "cursors": cursors}
    )
    return _result(
        "saved",
        "saved",
        scanned_status=scan_status,
        observed=observed,
        transitions=transitions,
        alerts=alerts_created,
        suppressed=suppressed,
        mapping_failures=mapping_failures,
        enqueued=enqueued,
        wires=1,
    )
