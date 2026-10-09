"""Deterministic local fixture dependencies for the production cadence.

Builds a ``ProductionCadenceDepsV1`` whose per-wire providers are pure local
computation over one fixture mapping — no socket, no transport import, no
ambient clock. Every provider validates its executor-issued wire claim first
(``require_started_claim``), so even in fixture mode an unreceipted callback
is impossible, and the returned probe records exactly which admitted wires
ran. This is the CLI dry-run dependency mode and the test seam; the real
read-only builder lives in the composition root.
"""

import hashlib
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta

from tokmo.facts.toss import derive_account_scope
from tokmo.facts.toss_prices import TossPriceQueryOutcome
from tokmo.facts.toss_reads import TossReadOutcome
from tokmo.programs.cadence_wire import require_started_claim
from tokmo.programs.production_cadence import ProductionCadenceDepsV1
from tokmo.values.current_holdings import AccountScopeIdentityV1, HoldingItemV1
from tokmo.values.holdings import TossAccountHoldingsFact
from tokmo.values.market_observations import TossCurrentPriceFact
from tokmo.values.production_cadence import (
    CADENCE_MODE_LIVE_SHADOW,
    CadenceAccountsV1,
    null_lifecycle_sink,
)
from tokmo.values.session_gate import Market
from tokmo.values.toss_market_calendar import (
    CalendarSession,
    IntegratedHour,
    KrMarketCalendarResponse,
    KrMarketDay,
    UsMarketCalendarResponse,
    UsMarketDay,
)
from tokmo.values.toss_orders import Order, OrderExecution, PaginatedOrderResponse


class FixtureProbeV1:
    """Run-local evidence of which admitted wires actually executed."""

    def __init__(self):
        self.calls: dict[str, int] = {}
        self.quote_batches: list[tuple[str, ...]] = []
        self.claims: list[object] = []

    def record(self, operation: str, claim=None) -> None:
        self.calls[operation] = self.calls.get(operation, 0) + 1
        if claim is not None:
            self.claims.append(claim)


def _aware_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("fixture timestamps must be aware")
    return parsed


class _FixtureSessionDates:
    """Deterministic calendar-free session-date seam for the trades windows."""

    def regular_session_dates(self, market, anchor_date, count):
        del market
        return tuple(anchor_date - timedelta(days=offset) for offset in range(count))


def _calendar_day(market: Market, entry: Mapping, local_trade_date: date):
    if entry.get("fail"):
        raise RuntimeError("fixture calendar provider failure")
    returned_date = (
        date.fromisoformat(entry["date"]) if "date" in entry else local_trade_date
    )

    def fixture_session(prefix):
        if entry.get(f"{prefix}_start") is None:
            return None
        return CalendarSession(
            _aware_iso(entry[f"{prefix}_start"]),
            _aware_iso(entry[f"{prefix}_end"]),
        )

    if market is Market.KR:
        return KrMarketDay(
            returned_date,
            IntegratedHour(
                pre_market=fixture_session("pre"),
                regular_market=fixture_session("regular"),
                after_market=fixture_session("after"),
            ),
        )
    return UsMarketDay(
        returned_date,
        day_market=fixture_session("day"),
        pre_market=fixture_session("pre"),
        regular_market=fixture_session("regular"),
        after_market=fixture_session("after"),
    )


def _order_from_fixture(entry: Mapping, now: datetime) -> Order:
    quantity = entry.get("quantity", "1")
    return Order(
        entry["order_id"],
        entry["symbol"],
        entry.get("side", "SELL"),
        entry.get("order_type", "MARKET"),
        entry.get("time_in_force", "DAY"),
        entry.get("status", "FILLED"),
        quantity,
        entry["currency"],
        now,
        OrderExecution(
            filled_quantity=quantity,
            average_filled_price=entry.get("filled_price", "100"),
            filled_amount=entry.get("filled_amount", "100"),
            commission=None,
            tax=None,
            filled_at=now,
            settlement_date=None,
        ),
    )


def build_fixture_cadence_deps(
    *,
    fixture: Mapping,
    clock: Callable[[], datetime],
    lifecycle_sink=None,
    failure_sink=None,
) -> tuple[ProductionCadenceDepsV1, FixtureProbeV1]:
    """Build deterministic read-only deps plus their wire probe."""
    probe = FixtureProbeV1()
    token_value = fixture.get("token", "fixture-bearer")
    calendar = fixture.get("calendar", {})
    account_rows = tuple(fixture.get("accounts", ()))
    quotes = fixture.get("quotes", {})
    orders = fixture.get("orders", {})
    details = fixture.get("order_details", {})
    candles = fixture.get("stock_candles")

    def token_fetch(claim):
        require_started_claim(claim, "TOKEN")
        probe.record("TOKEN", claim)
        if fixture.get("token_fail"):
            raise RuntimeError("fixture token provider failure")
        return token_value

    def calendar_fetch(*, market, local_trade_date, receipt, bearer):
        del receipt
        if bearer != token_value:
            raise RuntimeError("fixture bearer mismatch")
        operation = f"CALENDAR_DAY_{market.value}"
        entry = calendar.get(market.value, {})
        # an optional per-local-date fact map serves history backfill walks;
        # dates outside the map fall back to the market's default entry
        entry = entry.get("days", {}).get(local_trade_date.isoformat(), entry)
        if entry.get("fail"):
            probe.record(operation)
            raise RuntimeError("fixture calendar provider failure")
        probe.record(operation)
        # G2.5: return a full CalendarResponse so the caller can preserve
        # next_business_day for the lookahead writer.
        today = _calendar_day(market, entry, local_trade_date)
        next_date_str = entry.get("next_date")
        if market is Market.KR:
            kr_today: KrMarketDay = today  # type: ignore[assignment]
            if next_date_str is not None:
                from datetime import date as _date_cls

                next_entry = {
                    "date": next_date_str,
                    **{
                        k: v
                        for k, v in entry.items()
                        if k not in ("fail", "next_date", "date", "days")
                    },
                }
                kr_next: KrMarketDay = _calendar_day(  # type: ignore[assignment]
                    market, next_entry, _date_cls.fromisoformat(next_date_str)
                )
            else:
                kr_next = kr_today
            return KrMarketCalendarResponse(
                today=kr_today,
                previous_business_day=kr_today,
                next_business_day=kr_next,
            )
        us_today: UsMarketDay = today  # type: ignore[assignment]
        if next_date_str is not None:
            from datetime import date as _date_cls

            next_entry = {
                "date": next_date_str,
                **{
                    k: v
                    for k, v in entry.items()
                    if k not in ("fail", "next_date", "date", "days")
                },
            }
            us_next: UsMarketDay = _calendar_day(  # type: ignore[assignment]
                market, next_entry, _date_cls.fromisoformat(next_date_str)
            )
        else:
            us_next = us_today
        return UsMarketCalendarResponse(
            today=us_today, previous_business_day=us_today, next_business_day=us_next
        )

    def accounts_fetch(*, claim, bearer):
        require_started_claim(claim, "ACCOUNTS")
        probe.record("ACCOUNTS", claim)
        if bearer != token_value:
            raise RuntimeError("fixture bearer mismatch")
        if fixture.get("accounts_fail"):
            if failure_sink is not None:
                category = str(fixture["accounts_fail"])
                if category not in {
                    "rate_limited",
                    "timeout",
                    "server_error",
                    "forbidden",
                    "unauthorized",
                    "parse_failed",
                    "provider_other",
                }:
                    category = "provider_other"
                failure_sink("ACCOUNTS", category)
            raise RuntimeError("fixture accounts provider failure")
        identities = tuple(
            AccountScopeIdentityV1(
                derive_account_scope(row["seed"]),
                row.get("label", "brokerage"),
                ordinal,
                None,
            )
            for ordinal, row in enumerate(account_rows, start=1)
        )
        return CadenceAccountsV1(
            identities,
            {
                identity.account_scope: row["seed"]
                for identity, row in zip(identities, account_rows)
            },
            {
                identity.account_scope: hashlib.sha256(row["seed"].encode()).hexdigest()
                for identity, row in zip(identities, account_rows)
            },
        )

    def account_holdings_fetch(*, claim, bearer, identity, native_ref, ordinal, count):
        require_started_claim(claim, "ACCOUNT_HOLDINGS")
        probe.record("ACCOUNT_HOLDINGS", claim)
        if bearer != token_value:
            raise RuntimeError("fixture bearer mismatch")
        if fixture.get("holdings_fail"):
            raise RuntimeError("fixture account holdings provider failure")
        del identity
        row = next(item for item in account_rows if item["seed"] == native_ref)
        holdings = tuple(
            HoldingItemV1(
                item["symbol"],
                item["quantity"],
                item["avg_price"],
                item["currency"],
                item.get("market_raw"),
                item.get("market_field"),
            )
            for item in row.get("holdings", ())
        )
        return TossAccountHoldingsFact(
            "toss-holdings-v1",
            clock(),
            ordinal,
            count,
            holdings,
        )

    def quotes_fetch(*, claim, bearer, symbols):
        require_started_claim(claim, "PRICE_BATCH")
        probe.record("PRICE_BATCH", claim)
        probe.quote_batches.append(tuple(symbols))
        if bearer != token_value:
            raise RuntimeError("fixture bearer mismatch")
        if fixture.get("quotes_fail"):
            raise RuntimeError("fixture quotes provider failure")
        now = clock()
        return TossPriceQueryOutcome(
            "success",
            tuple(
                TossCurrentPriceFact(
                    "toss-current-price-v1",
                    symbol,
                    quotes[symbol]["price"],
                    quotes[symbol]["currency"],
                    now,
                    now,
                )
                for symbol in symbols
            ),
        )

    def orders_page_fetch(
        *,
        claim,
        bearer,
        account_scope,
        native_ref,
        status,
        from_date,
        to_date,
        cursor,
        limit,
    ):
        require_started_claim(claim, "ORDERS_PAGE")
        probe.record("ORDERS_PAGE", claim)
        del account_scope, status, from_date, to_date, cursor, limit
        if bearer != token_value:
            raise RuntimeError("fixture bearer mismatch")
        now = clock()
        rows = tuple(
            _order_from_fixture(entry, now) for entry in orders.get(native_ref, ())
        )
        return TossReadOutcome("success", PaginatedOrderResponse(rows, None, False))

    def order_detail_fetch(*, claim, bearer, account_scope, native_ref, order_id):
        require_started_claim(claim, "ORDER_DETAIL")
        probe.record("ORDER_DETAIL", claim)
        del account_scope, native_ref
        if bearer != token_value:
            raise RuntimeError("fixture bearer mismatch")
        entry = details.get(order_id)
        if entry is None:
            return TossReadOutcome("not_found")
        return TossReadOutcome("success", _order_from_fixture(entry, clock()))

    def stock_candles_fetch(*, claim, bearer, symbol, before, adjusted):
        require_started_claim(claim, "STOCK_CANDLES_DAILY")
        probe.record("STOCK_CANDLES_DAILY", claim)
        if bearer != token_value:
            raise RuntimeError("fixture bearer mismatch")
        del symbol, before, adjusted
        return candles

    deps = ProductionCadenceDepsV1(
        mode=CADENCE_MODE_LIVE_SHADOW,
        clock=clock,
        token_fetch=token_fetch,
        calendar_fetch=calendar_fetch,
        accounts_fetch=accounts_fetch,
        account_holdings_fetch=account_holdings_fetch,
        quotes_fetch=quotes_fetch,
        orders_page_fetch=orders_page_fetch,
        order_detail_fetch=order_detail_fetch,
        stock_candles_fetch=stock_candles_fetch,
        regular_session_dates=_FixtureSessionDates(),
        lifecycle_sink=(
            null_lifecycle_sink if lifecycle_sink is None else lifecycle_sink
        ),
        watchlist_symbols={
            Market(key): frozenset(value)
            for key, value in fixture.get("watchlist_symbols", {}).items()
        },
    )
    return deps, probe
