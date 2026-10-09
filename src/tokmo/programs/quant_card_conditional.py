"""Bounded OPEN conditional-order read for the Quant decision card (cycle 2).

The durable ``conditional_scan_state`` holds status/id only, so the card
needs its own read: exactly ONE ``CONDITIONAL_ORDERS`` page per mapped
Toss account scope, ``status=OPEN``, no cursor, ``limit`` ≤ 100, retry 0,
through the injected ``fetch`` callable (composition/tool supplies the
official Toss adapter; tests supply fixtures). Nothing is persisted here:
the caller receives typed per-scope outcomes and call counts only, and
the native account reference is revealed solely at the fetch boundary.

Fail-closed truth: an unmapped account, a provider failure, a truncated
list (``has_next``), an incomplete order detail or an order whose
top-level status is outside the provider vocabulary makes that scope
``unavailable`` — the card then says 조건주문 확인 불가 for every symbol and
never infers absence. The pinned Toss OpenAPI 1.2.5 excerpt
(``tests/fixtures/toss_remaining_reads_openapi_excerpt.json``) exposes no
BUY/SELL side on ``ConditionalOrderDetailResponse`` or
``ConditionalOrderCondition`` in the read-only excerpt, but the official POST
contract supplied by the user carries ``first/second.orderSide`` and defines
OCO as SELL+SELL and OTO as BUY+SELL.  The Facts layer preserves an
optional leg ``orderSide`` and owns the reusable
``resolve_conditional_order_side`` provider interpretation.  This Quant program
only selects account/symbol legs and projects the resolved fact into card
context.  SINGLE without the field remains unknown; nothing is derived from
holdings or prices.

Status vocabulary (Toss OpenAPI conditional-order list read): the
request ``status`` is a lifecycle GROUP filter (``OPEN`` | ``CLOSED``),
never an order status. An order in the ``OPEN`` group carries a top-level
``status`` in {WATCHING, PAUSED, ORDERING, ORDERED}; ``CLOSED`` orders
carry {COMPLETED, EXPIRED}. Leg-only states (HOLDING, CANCELED) never
appear at the top level. Production RCA 2026-09-04: two SK Hynix
(KR/000660) orders, each ``status=WATCHING``, were discarded because the
per-instrument filter compared the order status against the group filter
``"OPEN"`` — no live order can ever equal that string.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

from tokmo.facts.toss_reads import resolve_conditional_order_side
from tokmo.programs.quant_telegram_card import (
    ConditionalContextV1,
    ConditionalOrderContextV1,
    _private,
)
from tokmo.values.account_source_profile import (
    derive_account_source_profiles,
    toss_orders_target_scopes,
)
from tokmo.values.current_holdings import CurrentHoldingsV1

CONDITIONAL_PAGE_LIMIT = 100
CONDITIONAL_STATUS = (
    "OPEN"  # request-side lifecycle GROUP filter (OPEN | CLOSED), not an order status
)
# Top-level order statuses per Toss OpenAPI ConditionalOrderDetailResponse.status:
# the OPEN group (진행 중) and the CLOSED group (종료). Anything else is a
# vocabulary drift and fails the scope closed instead of being read as absent.
OPEN_ORDER_STATUSES = frozenset({"WATCHING", "PAUSED", "ORDERING", "ORDERED"})
CLOSED_ORDER_STATUSES = frozenset({"COMPLETED", "EXPIRED"})
_MARKETS = frozenset({"KR", "US"})
NO_WRITES = MappingProxyType(
    {"order": 0, "account": 0, "ledger": 0, "provider_payload_persisted": 0}
)


@dataclass(frozen=True)
class ScopeConditionalScanV1:
    account_scope: str
    status: str  # "ok" | "unavailable"
    reason: str | None
    orders: tuple = ()  # ConditionalOrderDetailResponse values (never persisted)


@dataclass(frozen=True)
class ConditionalScanV1:
    scopes: tuple
    provider_calls: int
    writes: MappingProxyType = NO_WRITES

    def unavailable_reasons(self) -> tuple:
        return tuple(
            f"{scan.account_scope}:{scan.reason}"
            for scan in self.scopes
            if scan.status != "ok"
        )


def _decimal(value) -> Decimal | None:
    if value is None or type(value) is not str:
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def collect_open_conditional_orders(*, holdings, refs, fetch) -> ConditionalScanV1:
    """One bounded OPEN page per mapped Toss scope; retry 0; nothing persisted."""
    if not isinstance(holdings, CurrentHoldingsV1):
        return ConditionalScanV1(
            (ScopeConditionalScanV1("*", "unavailable", "holdings_absent"),), 0
        )
    ref_scopes = frozenset() if refs is None else frozenset(refs.scopes())
    profiles = derive_account_source_profiles(holdings, native_ref_scopes=ref_scopes)
    mapped = set(toss_orders_target_scopes(profiles))
    scans = []
    calls = 0
    for profile in profiles:
        scope = profile.account_scope
        if not profile.supports_live_orders:
            continue  # pension/excel scopes carry no Toss conditional orders
        if scope not in mapped:
            scans.append(
                ScopeConditionalScanV1(scope, "unavailable", "account_unmapped")
            )
            continue
        calls += 1
        try:
            outcome = fetch(
                account_scope=scope,
                native_ref=refs.reveal(scope),
                status=CONDITIONAL_STATUS,
                cursor=None,
                limit=CONDITIONAL_PAGE_LIMIT,
            )
        except Exception:
            scans.append(
                ScopeConditionalScanV1(scope, "unavailable", "provider_failure")
            )
            continue
        code = getattr(outcome, "code", None)
        if code != "success":
            scans.append(
                ScopeConditionalScanV1(scope, "unavailable", f"provider_{code}")
            )
            continue
        if getattr(outcome, "has_next", False):
            scans.append(
                ScopeConditionalScanV1(scope, "unavailable", "list_truncated_has_next")
            )
            continue
        orders = tuple(getattr(outcome, "conditional_orders", ()))
        if any(not _complete(order) for order in orders):
            scans.append(
                ScopeConditionalScanV1(scope, "unavailable", "order_detail_incomplete")
            )
            continue
        if any(
            order.status not in OPEN_ORDER_STATUSES | CLOSED_ORDER_STATUSES
            for order in orders
        ):
            # a status outside the provider vocabulary can be neither ACTIVE nor
            # absent: the scope stays unread rather than silently dropping it
            scans.append(
                ScopeConditionalScanV1(scope, "unavailable", "order_status_unknown")
            )
            continue
        scans.append(ScopeConditionalScanV1(scope, "ok", None, orders))
    return ConditionalScanV1(tuple(scans), calls)


def _complete(order) -> bool:
    first = getattr(order, "first", None)
    second = getattr(order, "second", None)
    common = (
        all(
            isinstance(getattr(order, name, None), str) and getattr(order, name)
            for name in (
                "conditional_order_id",
                "type",
                "status",
                "symbol",
                "market",
                "quantity",
                "order_type",
            )
        )
        and first is not None
        and isinstance(getattr(first, "type", None), str)
        and isinstance(getattr(first, "status", None), str)
    )
    if not common or order.type not in {"SINGLE", "OCO", "OTO"}:
        return False
    if order.type in {"OCO", "OTO"}:
        return (
            order.order_type == "LIMIT"
            and second is not None
            and isinstance(getattr(second, "type", None), str)
            and isinstance(getattr(second, "status", None), str)
        )
    return not hasattr(second, "status")


def _legs(order):
    legs = [order.first]
    second = getattr(order, "second", None)
    if second is not None and hasattr(second, "status"):
        legs.append(second)
    return legs


def conditional_context_for(
    scan: ConditionalScanV1, *, market: str, symbol: str, scope_label
) -> ConditionalContextV1:
    """Per-instrument view: ACTIVE with legs, NONE only when every mapped
    scope was fully read, else UNAVAILABLE with the reasons.

    An order counts as active only when its top-level status is in the
    provider's OPEN-group vocabulary (WATCHING/PAUSED/ORDERING/ORDERED);
    COMPLETED/EXPIRED orders are excluded. Every matching order is kept —
    two live orders on the same instrument render as two entries."""
    if market not in _MARKETS:
        return ConditionalContextV1("UNAVAILABLE", "market_unmapped")
    orders = []
    for scope_scan in scan.scopes:
        if scope_scan.status != "ok":
            continue
        for order in scope_scan.orders:
            if (
                order.market != market
                or order.symbol != symbol
                or order.status not in OPEN_ORDER_STATUSES
            ):
                continue
            expire = getattr(order, "expire_date", None)
            expire_text = expire.isoformat() if hasattr(expire, "isoformat") else None
            for index, leg in enumerate(_legs(order)):
                orders.append(
                    ConditionalOrderContextV1(
                        scope_label(scope_scan.account_scope),
                        order.type,
                        order.order_type,
                        order.quantity,
                        leg.type,
                        leg.status,
                        _decimal(getattr(leg, "trigger_price", None)),
                        _decimal(getattr(leg, "order_price", None)),
                        expire_text,
                        resolve_conditional_order_side(
                            order.type,
                            index,
                            getattr(leg, "order_side", None),
                            getattr(order, "side", None),
                        ),
                    )
                )
    unavailable = tuple(
        f"{scope_label(scan_.account_scope)} {_reason_kr(scan_.reason)}"
        for scan_ in scan.scopes
        if scan_.status != "ok"
    )
    if orders:
        note = (
            None
            if not unavailable
            else "일부 계좌 확인 불가: " + ", ".join(unavailable)
        )
        return ConditionalContextV1("ACTIVE", note, tuple(orders))
    if unavailable or not scan.scopes:
        return ConditionalContextV1(
            "UNAVAILABLE", ", ".join(unavailable) or "매핑된 계좌 없음"
        )
    return ConditionalContextV1("NONE")


_REASON_KR = {
    "account_unmapped": "계좌 미매핑",
    "provider_failure": "제공자 호출 실패",
    "list_truncated_has_next": "목록 초과(hasNext)",
    "order_detail_incomplete": "주문 상세 불완전",
    "order_status_unknown": "주문 상태 어휘 미인식",
    "holdings_absent": "보유 스토어 없음",
}


def _reason_kr(reason) -> str:
    if reason in _REASON_KR:
        return _REASON_KR[reason]
    if isinstance(reason, str) and reason.startswith("provider_"):
        code = reason.removeprefix("provider_")
        return (
            f"제공자 응답 실패({code})"
            if code.isidentifier() and not _private(code)
            else "제공자 응답 실패"
        )
    return "확인 불가"
