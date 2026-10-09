"""Small price/quantity hypotheses shared by interactive and scheduled analysis.

A hypothesis is a calculation, never a funded order option or adopted choice.
"""

from decimal import Decimal, InvalidOperation


def decimal(value, *, zero=False):
    try:
        if isinstance(value, bool):
            raise ValueError("boolean amount")
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or (not zero and result == 0):
            raise ValueError("positive amount required")
        return result
    except (InvalidOperation, TypeError):
        raise ValueError("invalid hypothesis number") from None


def calculate_hypothesis(packet, values, *, origin, request=None):
    request = request or {}
    if (
        origin == "AI_PROPOSAL"
        and request.get("rotation")
        and values.get("entry_price") is None
    ):
        raise ValueError("rotation proposal requires explicit entry_price")
    currency = values.get("currency", packet["currency"])
    if currency != packet["currency"]:
        raise ValueError("hypothesis currency differs from instrument")
    quantity = decimal(values["quantity"])
    if packet["key"].startswith("KR:") and quantity != quantity.to_integral_value():
        raise ValueError("domestic hypothesis needs whole shares")
    if (
        origin == "AI_PROPOSAL"
        and request.get("quantity") is not None
        and quantity != decimal(request["quantity"])
    ):
        raise ValueError("proposal quantity differs from requested quantity")
    side = values.get("side", request.get("side", "NEW_BUY"))
    if side not in {"NEW_BUY", "SELL"}:
        raise ValueError("hypothesis side must be NEW_BUY/SELL")
    if origin == "AI_PROPOSAL" and request and side != request.get("side", "NEW_BUY"):
        raise ValueError("proposal side differs from request")
    if side == "SELL" and quantity > sum(
        (decimal(h["quantity"], zero=True) for h in packet["holdings"]), Decimal(0)
    ):
        raise ValueError("hypothesis sale exceeds existing holdings")
    price = values.get("entry_price") or packet.get("price")
    entry = decimal(price) if price is not None else None
    exit_price = (
        decimal(values["exit_price"]) if values.get("exit_price") is not None else None
    )
    budget = request.get("budget") if origin == "AI_PROPOSAL" else values.get("budget")
    if (
        origin == "AI_PROPOSAL"
        and values.get("budget") is not None
        and (
            budget is None
            or decimal(values["budget"], zero=True) != decimal(budget, zero=True)
        )
    ):
        raise ValueError("model cannot invent user budget")
    budget = decimal(budget, zero=True) if budget is not None else None
    amount = entry * quantity if entry is not None else None
    proceeds = exit_price * quantity if exit_price is not None else None
    profit = proceeds - amount if proceeds is not None and amount is not None else None
    if side == "SELL" and profit is not None:
        profit = -profit
    result = dict(
        origin=origin,
        side=side,
        currency=currency,
        quantity=str(quantity),
        entry_price=str(entry) if entry is not None else None,
        price_basis="PROPOSED"
        if origin == "AI_PROPOSAL"
        else "USER_ASSUMPTION"
        if values.get("entry_price") is not None
        else "OBSERVED_QUOTE",
        exit_price=str(exit_price) if exit_price is not None else None,
        entry_amount=str(amount) if amount is not None else None,
        exit_amount=str(proceeds) if proceeds is not None else None,
        gross_profit=str(profit) if profit is not None else None,
        gross_return=str(profit / amount) if profit is not None and amount else None,
        budget=str(budget) if budget is not None else None,
        budget_gap=str(budget - amount)
        if budget is not None and amount is not None
        else None,
        execution_eligible=False,
        fee_basis="수수료·세금 전 가정 계산",
        funding_note="가상 예산/가격 비교이며 실제 주문가능금액이나 체결·채택을 확인한 것이 아님",
    )
    holdings = packet.get("holdings", [])
    if (
        side == "NEW_BUY"
        and entry is not None
        and all(
            h.get("quantity") is not None
            and h.get("average_cost") is not None
            and h.get("quantity_status") != "UNRESOLVED"
            for h in holdings
        )
    ):
        held = sum((decimal(h["quantity"], zero=True) for h in holdings), Decimal(0))
        cost = sum(
            (
                decimal(h["quantity"], zero=True)
                * decimal(h["average_cost"], zero=True)
                for h in holdings
            ),
            Decimal(0),
        )
        result["average_cost_after"] = str((cost + amount) / (held + quantity))
        result.update(
            holding_before=str(held),
            holding_after=str(held + quantity),
            cost_basis_before=str(cost),
            cost_basis_after=str(cost + amount),
        )
        result["average_cost_basis"] = (
            "현재 입력의 보유수량·평단과 가상 매수가를 합산한 수수료 전 계산"
        )
    for total in packet.get("portfolio", {}).values():
        if (
            amount is None
            or total.get("scope") != packet["scope"]
            or not total.get("total")
        ):
            continue
        converted = amount
        if total["currency"] != currency:
            fx = packet.get("fx")
            if currency != "USD" or total["currency"] != "KRW" or not fx:
                continue
            converted *= decimal(fx["rate"])
        denominator = decimal(total["total"], zero=True)
        if denominator:
            result["new_money_fraction_of_confirmed_assets"] = str(
                converted / denominator
            )
            result["new_money_weight_basis"] = (
                "매수 전 "
                + total["basis"]
                + " 대비 가상 대금의 비율; 매수 후 종목 비중은 아님"
            )
        break
    for field in ("reason", "condition"):
        if field in values:
            if (
                not isinstance(values[field], str)
                or not values[field].strip()
                or len(values[field]) > 800
            ):
                raise ValueError("invalid hypothesis " + field)
            result[field] = values[field].strip()
    return result


def attach_request(packet, requests):
    matched = [
        r
        for r in requests
        if (r.get("key"), r.get("account"), r.get("scope"))
        == (packet["key"], packet["account"], packet["scope"])
    ]
    if len(matched) > 1:
        raise ValueError("duplicate analysis request")
    if not matched:
        return packet
    request = dict(matched[0])
    if request.get("currency", packet["currency"]) != packet["currency"]:
        raise ValueError("request currency differs from instrument")
    packet["user_request"] = request
    if request.get("quantity") is not None:
        packet["hypothesis"] = calculate_hypothesis(
            packet, request, origin="USER_ASSUMPTION"
        )
    return packet


def unit_values(packet):
    """Known quantities and amounts keep units; percentages are checked separately."""
    result = {unit: set() for unit in ("원", "달러", "주")}
    money_fields = {
        "price",
        "close",
        "previous_close",
        "lower",
        "upper",
        "entry_price",
        "exit_price",
        "price_lower",
        "price_upper",
        "average_cost",
        "average_cost_before",
        "average_cost_after",
        "amount",
        "entry_amount",
        "exit_amount",
        "gross_profit",
        "budget",
        "budget_gap",
        "available",
        "total_cash",
        "cash",
        "total",
        "equity",
        "market_value",
        "cost_basis_before",
        "cost_basis_after",
        "cash_delta",
        "cash_before",
        "cash_after",
        "risk_price",
        "new_money_loss_at_risk_price",
        "remaining_after_buys",
        "batch_remaining_cash",
        "portfolio_before",
        "portfolio_after",
    }
    quantity_fields = {"quantity", "sellable", "holding_before", "holding_after"}

    def walk(value, currency, path=()):
        if isinstance(value, list):
            for item in value:
                walk(item, currency, path)
        elif isinstance(value, dict):
            currency = value.get("currency", currency)
            for key, item in value.items():
                if key in {"evidence", "current_user_choices", "display_values"}:
                    continue
                money_currency = (
                    value.get("portfolio_currency", currency)
                    if key in {"portfolio_before", "portfolio_after"}
                    else currency
                )
                is_money = (
                    key in money_fields
                    or key.startswith(("sma_", "ema_", "atr_"))
                    or (key == "value" and "moving_averages" in path)
                )
                unit = (
                    "주"
                    if key in quantity_fields
                    else {"KRW": "원", "USD": "달러"}.get(money_currency)
                    if is_money
                    else None
                )
                if (
                    unit
                    and not isinstance(item, (list, dict, bool))
                    and item is not None
                ):
                    try:
                        number = Decimal(str(item))
                        if number.is_finite():
                            result[unit].add(number)
                    except InvalidOperation:
                        pass
                walk(item, currency, (*path, key))

    walk(packet, packet["currency"])
    for series in packet.get("investor_flows", {}).values():
        unit = {"KRW": "원", "shares": "주"}.get(series.get("unit"))
        if not unit:
            continue
        for row in [series.get("latest", {}), *series.get("windows", [])]:
            for value in row.get("net", {}).values():
                if value is None:
                    continue
                amount = Decimal(str(value))
                result[unit].update(
                    (amount, abs(amount))
                )  # '순매도 3만 주' is an unsigned magnitude.
    # ETF look-through is calculated in KRW even for a USD stock analysis.
    # Limit this to the typed ETF fields; identifiers and dates are not money.
    etf_money = {
        "priced_holdings_krw",
        "value_krw",
        "direct_krw",
        "via_etf_krw",
        "combined_krw",
        "amount_krw",
        "all_accounts_priced_holdings_krw",
    }

    def etf_amounts(value):
        if isinstance(value, list):
            for item in value:
                etf_amounts(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if (
                    key in etf_money
                    and item is not None
                    and not isinstance(item, (bool, list, dict))
                ):
                    try:
                        amount = Decimal(str(item))
                        if amount.is_finite():
                            result["원"].add(amount)
                    except InvalidOperation:
                        pass
                etf_amounts(item)

    etf_amounts(packet.get("etf_exposure", {}))
    if packet.get("key", "").startswith("KR:") and packet.get("currency") == "KRW":
        for key in ("nav", "provider_price"):
            value = packet.get("etf_context", {}).get("nav", {}).get(key)
            if value is None or isinstance(value, (bool, list, dict)):
                continue
            try:
                amount = Decimal(str(value))
                if amount.is_finite():
                    result["원"].add(amount)
            except InvalidOperation:
                pass
    return result
