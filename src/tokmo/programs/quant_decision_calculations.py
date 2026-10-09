"""Deterministic price/size alternatives, with explicit funding provenance."""

from __future__ import annotations

import math
import statistics
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from hashlib import sha256

from tokmo.programs.quant_discovery_features import compute_price_features
from tokmo.values.quant_decisions import instant, number
from tokmo.values.quant_discovery import PriceBarV1, PriceSeriesV1, QuantMarketProfileV1

D = Decimal


def plain(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    return value


def features(instrument):
    # Q5: this existing indicator contract uses at most 200 sessions.
    # The selected analysis span remains on Instrument and in price_context.
    bars = instrument.bars[-200:]
    if not bars or not all(b["adjusted"] for b in bars):
        return {}
    series = PriceSeriesV1(
        instrument.symbol,
        QuantMarketProfileV1(instrument.market),
        tuple(
            PriceBarV1(
                date.fromisoformat(b["date"]),
                b["open"],
                b["high"],
                b["low"],
                b["close"],
                b["volume"],
                None,
            )
            for b in bars
        ),
    )
    return {
        f.name: f.raw_value
        for f in compute_price_features(
            series, completed_session_date=series.bars[-1].session_date
        ).features.factors
    }


def round_price(instrument, value, *, up=False):
    if instrument.tick_size is not None:
        tick = instrument.tick_size
    elif instrument.market == "US":
        tick = D("0.01")
    elif instrument.kind == "etf":
        tick = D(5)
    else:
        tick = next(
            (
                D(unit)
                for limit, unit in [
                    (2000, 1),
                    (5000, 5),
                    (20000, 10),
                    (50000, 50),
                    (200000, 100),
                    (500000, 500),
                ]
                if value < D(limit)
            ),
            D(1000),
        )
    return max(
        tick,
        (value / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
        * tick,
    )


def price_ranges(instrument):
    price, bars = instrument.price, instrument.bars
    if price is None:
        return []
    tick = instrument.tick_size or (D("0.01") if instrument.currency == "USD" else D(1))

    def rounded(x, up=False):
        return round_price(instrument, x, up=up)

    # No current-price-only percentage band is sold as a valuation estimate.
    if len(bars) < 21 or not all(b["adjusted"] for b in bars):
        return [
            {
                "id": "reference",
                "lower": rounded(price),
                "upper": rounded(price, True),
                "condition": "참고 현재가. 진입 조건을 정할 수정 일봉이 필요합니다.",
                "risk_price": None,
                "entry_ready": False,
            }
        ]
    recent = bars[-20:]
    sma20 = sum(b["close"] for b in recent) / D(20)
    true_ranges = [
        max(
            b["high"] - b["low"],
            abs(b["high"] - prev["close"]),
            abs(b["low"] - prev["close"]),
        )
        for prev, b in zip(bars[-15:-1], bars[-14:])
    ]
    atr = sum(true_ranges) / D(len(true_ranges))
    if atr <= 0:
        atr = tick
    support, resistance = min(b["low"] for b in recent), max(b["high"] for b in recent)
    risk_price = rounded(max(tick, min(support, price - 2 * atr)))
    ranges = [
        {
            "id": "participate",
            "lower": rounded(max(tick, price - atr / 4)),
            "upper": rounded(price, True),
            "condition": "현재 구간에서 분할 참여. 사업 근거와 계좌 부담을 함께 확인합니다.",
            "risk_price": risk_price,
            "entry_ready": True,
        }
    ]
    if sma20 < price:
        lower, upper = max(tick, sma20 - atr / 2), min(price, sma20 + atr / 4)
        ranges.append(
            {
                "id": "pullback",
                "lower": rounded(lower),
                "upper": rounded(upper, True),
                "condition": "이 구간까지 조정된 뒤 지지·반등을 확인할 때 검토합니다.",
                "risk_price": rounded(max(tick, min(support, lower - atr))),
                "entry_ready": False,
            }
        )
    else:
        ranges.append(
            {
                "id": "recovery",
                "lower": rounded(sma20, True),
                "upper": rounded(sma20 + atr / 4, True),
                "condition": "20일 평균을 회복한 종가와 거래량 개선을 확인한 뒤 검토합니다.",
                "risk_price": risk_price,
                "entry_ready": False,
            }
        )
    ranges.append(
        {
            "id": "breakout",
            "lower": rounded(resistance + tick, True),
            "upper": rounded(resistance + atr / 4 + tick, True),
            "condition": "최근 20일 고점 돌파를 종가·거래량으로 확인한 뒤 검토합니다.",
            "risk_price": rounded(max(tick, resistance - 2 * atr)),
            "entry_ready": False,
        }
    )
    return ranges


def return_observations(instrument, *, weekly, as_of):
    rows = [b for b in instrument.bars if b["adjusted"]]
    prices = {}
    for b in rows:
        day = date.fromisoformat(b["date"])
        if weekly:
            week = day.isocalendar()[:2]
            # A developing week never masquerades as a completed common week.
            if week >= as_of.date().isocalendar()[:2]:
                continue
            prices[week] = b["close"]
        else:
            prices[day.isoformat()] = b["close"]
    keys = sorted(prices)
    return {
        keys[n]: float(prices[keys[n]] / prices[keys[n - 1]] - 1)
        for n in range(1, len(keys))
    }


def relationships(snapshot, instrument, *, scope=None):
    by_key = {i.key: i for i in snapshot.instruments}
    result = []
    for key in sorted(
        {
            p.key
            for p in snapshot.positions
            if p.quantity > 0
            and p.key != instrument.key
            and (scope is None or p.scope == scope)
        }
    ):
        other = by_key[key]
        weekly = instrument.market != other.market
        a = return_observations(instrument, weekly=weekly, as_of=snapshot.as_of)
        b = return_observations(other, weekly=weekly, as_of=snapshot.as_of)
        common = sorted(a.keys() & b.keys())
        minimum = 12 if weekly else 30
        corr = None
        if len(common) >= minimum:
            try:
                corr = statistics.correlation(
                    [a[k] for k in common], [b[k] for k in common]
                )
            except statistics.StatisticsError:
                pass
        result.append(
            {
                "key": key,
                "name": other.name,
                "correlation": None if corr is None else round(corr, 4),
                "samples": len(common),
                "interval": "weekly" if weekly else "daily",
                "minimum": minimum,
                "same_sector": bool(
                    instrument.sector and instrument.sector == other.sector
                ),
                "basis": "수정주가의 현지통화 수익률 · 환율 변동 미포함",
                "from": str(common[0]) if common else None,
                "until": str(common[-1]) if common else None,
            }
        )
    return result


def convert(snapshot, amount, currency, target="KRW"):
    if currency == target:
        return amount
    if snapshot.fx is None:
        return None
    return (
        amount * snapshot.fx["rate"]
        if target == "KRW"
        else amount / snapshot.fx["rate"]
    )


def portfolio_totals(snapshot, *, include_extra=False):
    by_key = {i.key: i for i in snapshot.instruments}
    totals = {}
    for scope in ("personal", "retirement"):
        positions = [p for p in snapshot.positions if p.scope == scope]
        cash = [c for c in snapshot.cash if c.scope == scope]
        currencies = {by_key[p.key].currency for p in positions} | {
            c.currency for c in cash
        }
        currencies |= {
            e.currency for e in snapshot.extra if include_extra and e.scope == scope
        }
        currencies |= {
            s["currency"] for s in snapshot.account_summaries if s.get("scope") == scope
        }
        if not currencies:
            continue
        target = "KRW" if snapshot.fx or currencies <= {"KRW"} else None
        for currency in [target] if target else sorted(currencies):
            rows = [
                p for p in positions if target or by_key[p.key].currency == currency
            ]
            cash_rows = [c for c in cash if target or c.currency == currency]
            missing_quotes = [p.key for p in rows if by_key[p.key].price is None]
            missing_quantities = [
                p.key for p in rows if p.quantity_status == "UNRESOLVED"
            ]
            equity = sum(
                (
                    convert(
                        snapshot,
                        p.quantity * by_key[p.key].price,
                        by_key[p.key].currency,
                        currency,
                    )
                    for p in rows
                    if by_key[p.key].price is not None
                    and p.quantity_status != "UNRESOLVED"
                ),
                D(0),
            )
            cash_value = sum(
                (
                    convert(
                        snapshot,
                        c.total_cash if c.total_cash is not None else c.available,
                        c.currency,
                        currency,
                    )
                    for c in cash_rows
                ),
                D(0),
            )
            extra_value = sum(
                (
                    convert(snapshot, e.amount, e.currency, currency)
                    for e in snapshot.extra
                    if include_extra
                    and e.scope == scope
                    and (target or e.currency == currency)
                ),
                D(0),
            )
            known_accounts = {c.account for c in cash_rows}
            complete = (
                not missing_quotes
                and not missing_quantities
                and all(p.account in known_accounts for p in rows)
                and all(c.total_cash is not None for c in cash_rows)
            )
            other_assets = D(0)
            for summary in snapshot.account_summaries:
                if summary.get("scope") != scope or (
                    not target and summary.get("currency") != currency
                ):
                    continue
                if instant(summary["observed_at"]) > snapshot.as_of:
                    raise ValueError("future account summary")
                other_assets += convert(
                    snapshot,
                    number(summary.get("other_assets", 0), minimum=0),
                    summary["currency"],
                    currency,
                )
            if scope == "retirement":
                complete_accounts = {
                    s["account"]
                    for s in snapshot.account_summaries
                    if s.get("scope") == scope and s.get("complete") is True
                }
                complete = (
                    complete
                    and ({p.account for p in rows} | known_accounts)
                    <= complete_accounts
                )
            key = f"{scope}:{currency}"
            totals[key] = {
                "scope": scope,
                "currency": currency,
                "equity": equity,
                "cash": cash_value,
                "extra": extra_value,
                "other_assets": other_assets,
                "total": equity + cash_value + extra_value + other_assets,
                "complete": complete,
                "missing_quotes": missing_quotes,
                "missing_quantities": missing_quantities,
                "basis": "미확정 수량 종목 제외 · 확인된 자산 기준 참고"
                if missing_quantities
                else "계좌 전체"
                if complete
                else "확인된 보유자산·가용 자금 기준 참고",
                "summary_dates": [
                    instant(s["observed_at"]).isoformat()
                    for s in snapshot.account_summaries
                    if s.get("scope") == scope
                ],
                "merged_markets": target == "KRW"
                and "USD" in currencies
                and "KRW" in currencies,
            }
    return totals


def fee_rate(snapshot, account, market):
    key = f"{account}:{market}"
    row = snapshot.fees.get(key)
    if isinstance(row, dict) and instant(row["observed_at"]) <= snapshot.as_of:
        rate = number(row["rate"], minimum=0)
        if rate < 1:
            return rate, "조회 매매수수료 반영 · 세금/기타 비용 별도"
    return D("0.003"), "수수료·비용 여유 0.3% 가정 (실제 수수료 아님)"


def portfolio_risk(
    snapshot,
    instrument,
    scope,
    *,
    signed_quantity=D(0),
    after_total=None,
    include_extra=False,
):
    """Fixed-weight weekly historical variability, not forecast risk.

    Only report whole connected equity risk when every valued holding has
    enough common completed weeks. Cash contributes zero price return;
    other assets/FX are excluded and disclosed rather than guessed.
    """
    by_key = {i.key: i for i in snapshot.instruments}
    if any(
        p.scope == scope and p.quantity_status == "UNRESOLVED"
        for p in snapshot.positions
    ):
        return {
            "available": False,
            "reason": "일부 보유수량이 미확정되어 계좌 위험 합산 미확인",
        }
    currencies = {
        by_key[p.key].currency for p in snapshot.positions if p.scope == scope
    }
    if len(currencies | {instrument.currency}) > 1 and snapshot.fx is None:
        return {
            "available": False,
            "reason": "국내·미국 위험 합산에 필요한 환율 미확인",
        }
    currency = "KRW" if snapshot.fx or instrument.currency == "KRW" else "USD"
    total = portfolio_totals(snapshot, include_extra=include_extra).get(
        f"{scope}:{currency}"
    )
    denominator = (
        after_total if after_total is not None else total["total"] if total else D(0)
    )
    if denominator <= 0:
        return {"available": False, "reason": "평가 기준 자산 미확인"}
    quantities = {}
    for p in snapshot.positions:
        if p.scope == scope:
            quantities[p.key] = quantities.get(p.key, D(0)) + p.quantity
    quantities[instrument.key] = quantities.get(instrument.key, D(0)) + signed_quantity
    active = {k: v for k, v in quantities.items() if v > 0}
    observations = {
        k: return_observations(by_key[k], weekly=True, as_of=snapshot.as_of)
        for k in active
    }
    common = (
        set.intersection(*(set(r) for r in observations.values()))
        if observations
        else set()
    )
    if len(common) < 12 or any(by_key[k].price is None for k in active):
        return {
            "available": False,
            "reason": "모든 보유종목의 공통 주간 수정주가가 부족함",
            "weeks": len(common),
        }
    weights = {
        k: float(
            convert(snapshot, q * by_key[k].price, by_key[k].currency, currency)
            / denominator
        )
        for k, q in active.items()
    }
    series = [
        sum(weights[k] * observations[k][week] for k in active)
        for week in sorted(common)
    ]
    return {
        "available": True,
        "annualized_volatility": round(statistics.stdev(series) * math.sqrt(52), 6),
        "weeks": len(common),
        "equity_weight": round(sum(weights.values()), 6),
        "basis": "현재 비중을 과거 공통 주간 수익률에 적용한 비교 · 환율 변동·기타자산 변동 미포함 · 미래 예측 아님",
    }


def size_options(snapshot, instrument, account, scope, *, include_extra=False):
    totals = portfolio_totals(snapshot, include_extra=include_extra)
    total_key = (
        f"{scope}:KRW"
        if snapshot.fx or instrument.currency == "KRW"
        else f"{scope}:USD"
    )
    total = totals.get(total_key)
    if not total or instrument.price is None:
        return []
    base = total["total"]
    if base <= 0:
        return []
    currency = instrument.currency
    position = next(
        (
            p
            for p in snapshot.positions
            if p.account == account and p.key == instrument.key
        ),
        None,
    )
    if position and position.quantity_status == "UNRESOLVED":
        return []
    held = position.quantity if position else D(0)
    available = next(
        (
            c.available
            for c in snapshot.cash
            if c.account == account and c.currency == currency
        ),
        None,
    )
    extra = sum(
        (
            e.amount
            for e in snapshot.extra
            if include_extra and e.account == account and e.currency == currency
        ),
        D(0),
    )
    # A foreign-currency cash balance is not an implicit currency conversion.
    budget = (
        (available + extra) if available is not None else (extra if extra > 0 else None)
    )
    fee, fee_basis = fee_rate(snapshot, account, instrument.market)
    to_base = lambda amount: convert(snapshot, amount, currency, total["currency"])
    all_held = sum(
        (
            p.quantity
            for p in snapshot.positions
            if p.scope == scope and p.key == instrument.key
        ),
        D(0),
    )
    weight_before = to_base(all_held * instrument.price) / base
    label = "extra" if include_extra else "cash"
    result = []
    risk_before = portfolio_risk(
        snapshot, instrument, scope, include_extra=include_extra
    )

    def option(action, qty, band, scale, amount, cash_delta):
        signed = qty if action == "BUY" else -qty
        traded = to_base(signed * instrument.price)
        # Mark holdings at the same snapshot price; exchange transaction cash
        # at the upper/lower execution assumption, including costs.
        after_base = base + traded + to_base(cash_delta)
        value_after = to_base((all_held + signed) * instrument.price)
        identity = f"{account}|{instrument.key}|{label}|{action}|{band['id']}|{qty}"
        conditional = any(
            o.get("account") == account
            and o.get("key") in {instrument.key, "*"}
            and o.get("kind") in {"conditional", "unavailable"}
            for o in snapshot.orders
        )
        condition = band["condition"] + (
            " 기존 조건주문과 함께 실행하는 안이 아닙니다. 기존 주문을 유지할지 이 안으로 바꿀지 먼저 결정합니다."
            if conditional
            else ""
        )
        average_before = position.average_cost if position and held > 0 else None
        average_after = (
            (held * (average_before or D(0)) + amount) / (held + qty)
            if action == "BUY" and (not held or average_before is not None)
            else average_before
            if action == "SELL" and held > qty
            else None
        )
        return {
            "id": sha256(identity.encode()).hexdigest()[:16],
            "account": account,
            "scope": scope,
            "key": instrument.key,
            "scenario": label,
            "action": action,
            "range_id": band["id"],
            "price_lower": band["lower"],
            "price_upper": band["upper"],
            "condition": condition,
            "entry_ready": band["entry_ready"] and not conditional,
            "requires_order_review": conditional,
            "risk_price": band["risk_price"],
            "quantity": qty,
            "amount": amount,
            "currency": currency,
            "size_label": scale,
            "portion_of_holding": qty / held if action == "SELL" and held > 0 else None,
            "remaining_portion_of_holding": (held - qty) / held
            if action == "SELL" and held > 0
            else None,
            "holding_before": held,
            "holding_after": held + signed,
            "average_cost_before": average_before,
            "average_cost_after": average_after,
            "average_cost_basis": "매수 가격대 상단 체결 가정 · 수수료 제외"
            if action == "BUY"
            else "잔여 보유분 평단 · 수수료 제외",
            "weight_before": weight_before,
            "weight_after": value_after / after_base if after_base > 0 else None,
            "cash_before": budget,
            "cash_after": budget + cash_delta if budget is not None else None,
            "cash_delta": cash_delta,
            "fee_basis": fee_basis,
            "portfolio_basis": total["basis"],
            "portfolio_currency": total["currency"],
            "portfolio_after": after_base,
            "risk_before": risk_before,
            "risk_after": portfolio_risk(
                snapshot,
                instrument,
                scope,
                signed_quantity=signed,
                after_total=after_base,
                include_extra=include_extra,
            ),
            "stress_loss_15pct": value_after * D("0.15"),
            "new_money_loss_at_risk_price": qty
            * max(D(0), band["upper"] - band["risk_price"])
            if action == "BUY" and band["risk_price"]
            else None,
        }

    bands = price_ranges(instrument)
    if (
        budget is not None
        and budget > 0
        and (scope != "retirement" or instrument.pension_eligible is True)
    ):
        # A comparison menu, not a fixed allocation mandate. The analyst may
        # select none. Avoid confusing the affordable maximum with a target.
        for band in bands:
            if band["id"] == "reference":
                continue
            seen = set()
            for fraction, scale in (
                (None, "최소 매매 단위"),
                (D("0.025"), "소규모"),
                (D("0.05"), "중간 규모"),
                (D("0.10"), "큰 규모"),
            ):
                target_native = (
                    band["upper"] * (1 + fee) * instrument.lot_size
                    if fraction is None
                    else convert(snapshot, base * fraction, total["currency"], currency)
                )
                qty = (
                    min(budget, target_native)
                    / (band["upper"] * (1 + fee))
                    / instrument.lot_size
                ).to_integral_value(rounding=ROUND_FLOOR) * instrument.lot_size
                if (
                    qty == 0
                    and budget >= band["upper"] * (1 + fee) * instrument.lot_size
                ):
                    qty = instrument.lot_size
                    scale = "최소 매매 단위 · 비교 비중보다 큼"
                if qty <= 0 or qty in seen:
                    continue
                seen.add(qty)
                amount = qty * band["upper"]
                result.append(
                    option("BUY", qty, band, scale, amount, -amount * (1 + fee))
                )
    sale_limit = position.sellable if position else None
    estimated_sale = bool(
        position
        and sale_limit is None
        and scope == "retirement"
        and position.quantity_status == "CALCULATED"
    )
    if estimated_sale:
        sale_limit = held
    if position and sale_limit is not None and sale_limit > 0:
        sell_price = round_price(instrument, instrument.price)
        band = {
            "id": "reduce",
            "lower": sell_price,
            "upper": sell_price,
            "condition": "현재가 기준 매도 규모 비교. 실제 체결가는 달라질 수 있습니다."
            + (
                " 거래 반영 계산 수량 기준이며 실행 전 증권사 매도가능수량 확인이 필요합니다."
                if estimated_sale
                else ""
            ),
            "risk_price": None,
            "entry_ready": not estimated_sale,
        }
        seen = set()
        quantities = [
            (sale_limit * fraction / instrument.lot_size).to_integral_value(
                rounding=ROUND_FLOOR
            )
            * instrument.lot_size
            for fraction in (D("0.25"), D("0.5"), D(1))
        ]
        for request in snapshot.analysis_requests:
            if (
                request.get("key"),
                request.get("account"),
                request.get("scope"),
                request.get("side"),
            ) == (instrument.key, account, scope, "SELL"):
                requested = (
                    D(str(request["quantity"]))
                    if request.get("quantity") is not None
                    else D(0)
                )
                if (
                    0 < requested <= min(sale_limit, held)
                    and requested % instrument.lot_size == 0
                ):
                    quantities.insert(0, requested)
        for qty in quantities:
            if qty <= 0 or qty in seen:
                continue
            seen.add(qty)
            amount = qty * sell_price
            scale = (
                "전량 검토"
                if qty == held
                else "절반 조절"
                if qty * 2 == held
                else "일부 조절"
            )
            candidate = option("SELL", qty, band, scale, amount, amount * (1 - fee))
            candidate["requires_broker_confirmation"] = estimated_sale
            result.append(candidate)
    return result
