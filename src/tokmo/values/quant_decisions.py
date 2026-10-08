"""Inputs for account-aware investment proposals, independent of transport.

Amounts are Decimal; available cash and total cash are different fields.
JSON fixtures and live captures use the same contract. No order can be
constructed from this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from zoneinfo import ZoneInfo


def number(value, *, minimum=None):
    if isinstance(value, bool):
        raise ValueError("boolean is not a financial amount")
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise ValueError("invalid financial amount") from None
    if not result.is_finite() or (minimum is not None and result < minimum):
        raise ValueError("financial amount out of range")
    return result


def instant(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return result


@dataclass(frozen=True)
class Evidence:
    id: str
    kind: str
    text: str
    available_at: datetime
    source: str
    scope: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Instrument:
    symbol: str
    market: str
    name: str
    currency: str
    kind: str
    price: Decimal | None
    price_at: datetime | None
    bars: tuple[dict, ...] = ()
    sector: str | None = None
    evidence: tuple[Evidence, ...] = ()
    metrics: dict = field(default_factory=dict)
    lot_size: Decimal = Decimal(1)
    tick_size: Decimal | None = None
    pension_eligible: bool | None = None
    sources: tuple[str, ...] = ()

    @property
    def key(self):
        return f"{self.market}:{self.symbol}"


@dataclass(frozen=True)
class Position:
    account: str
    scope: str
    key: str
    quantity: Decimal
    average_cost: Decimal | None
    sellable: Decimal | None = None
    observed_at: datetime | None = None
    sellable_at: datetime | None = None
    captured_at: datetime | None = None
    source_status: str = 'PROVIDED'
    quantity_status: str = 'PROVIDED'


@dataclass(frozen=True)
class Cash:
    account: str
    scope: str
    currency: str
    available: Decimal
    observed_at: datetime
    source: str
    total_cash: Decimal | None = None
    basis: str = "cash_buying_power"


@dataclass(frozen=True)
class ExtraCapital:
    account: str
    scope: str
    currency: str
    amount: Decimal
    label: str = "이번 분석의 추가 입금 가정"


@dataclass(frozen=True)
class DecisionSnapshot:
    as_of: datetime
    instruments: tuple[Instrument, ...]
    positions: tuple[Position, ...]
    cash: tuple[Cash, ...]
    fx: dict | None = None
    extra: tuple[ExtraCapital, ...] = ()
    account_summaries: tuple[dict, ...] = ()
    fees: dict = field(default_factory=dict)
    orders: tuple[dict, ...] = ()
    limitations: tuple[str, ...] = ()
    synthetic: bool = False
    scenario_label: str | None = None
    user_choices: tuple[dict, ...] = ()
    analysis_requests: tuple[dict, ...] = ()
    investor_flows: dict = field(default_factory=dict)


def parse_snapshot(data):
    if data.get("schema_version") != 1:
        raise ValueError("unsupported decision snapshot")
    now = instant(data["as_of"])
    instruments = []
    for row in data["instruments"]:
        market, symbol = row["market"], row["symbol"]
        if market not in {"KR", "US"} or not re.fullmatch(r"[A-Za-z0-9.-]{1,24}", symbol):
            raise ValueError("invalid instrument identity")
        currency = row.get("currency", "KRW" if market == "KR" else "USD")
        if currency != ("KRW" if market == "KR" else "USD"):
            raise ValueError("market and currency disagree")
        evidence = tuple(Evidence(e["id"], e["kind"], str(e["text"])[:6000], instant(e["available_at"]), e["source"], e.get("scope"), dict(e.get('metadata') or {}))
                         for e in row.get("evidence", []) if instant(e["available_at"]) <= now)
        price_at = instant(row["price_at"]) if row.get("price_at") else None
        price = number(row["price"], minimum=Decimal("0.00000001")) if row.get("price") is not None else None
        if price_at is None or price_at > now:
            price = None
        bars = []
        for b in row.get("bars", []):
            at = instant(b["available_at"])
            if at > now:
                continue
            session=date.fromisoformat(b['date'])
            if session>at.astimezone(ZoneInfo('Asia/Seoul' if market=='KR' else 'America/New_York')).date():
                raise ValueError('bar available before its session')
            if type(b.get('adjusted',False)) is not bool:raise ValueError('adjustment flag must be boolean')
            values = {key: number(b[key], minimum=Decimal("0.00000001")) for key in ("open", "high", "low", "close")}
            if not values["low"] <= min(values["open"], values["close"]) <= max(values["open"], values["close"]) <= values["high"]:
                raise ValueError("invalid OHLC bar")
            bars.append(dict(values, date=b["date"], available_at=at, adjusted=b.get("adjusted", False),
                             volume=number(b.get("volume", 0), minimum=0)))
        if len({b["date"] for b in bars}) != len(bars):
            raise ValueError("duplicate price bar")
        bars.sort(key=lambda b: b["date"])
        lot = number(row.get("lot_size", "1"), minimum=Decimal("0.00000001"))
        tick = number(row["tick_size"], minimum=Decimal("0.00000001")) if row.get("tick_size") is not None else None
        metrics = {k: v for k, v in row.get("metrics", {}).items()
                   if isinstance(v, dict) and v.get("available_at") and instant(v["available_at"]) <= now}
        history = row.get('metrics', {}).get('price_history')
        # Legacy capture metadata has no clock; its completed bars establish
        # availability. Other metrics retain their point-in-time requirement.
        if isinstance(history, dict) and not history.get('available_at') and bars:
            metrics['price_history'] = dict(history, available_at=max(b['available_at'] for b in bars).isoformat())
        history = metrics.get('price_history')
        if history:
            start, end, sessions = (history.get(k) for k in ('requested_start','requested_end','requested_sessions'))
            if (start is not None and date.fromisoformat(start).isoformat() != start
                    or end is not None and date.fromisoformat(end).isoformat() != end
                    or sessions is not None and (type(sessions) is not int or sessions < 1 or start is not None)):
                raise ValueError('invalid_history_metadata')
            bars = [b for b in bars if (start is None or b['date'] >= start) and (end is None or b['date'] <= end)]
            if sessions is not None:
                bars = bars[-sessions:]
            metrics['price_history'] = dict(history, analysis_count=len(bars),
                analysis_start=bars[0]['date'] if bars else None, analysis_end=bars[-1]['date'] if bars else None)
        instruments.append(Instrument(symbol, market, row.get("name", symbol), currency, row.get("kind", "stock"),
            price, price_at, tuple(bars), row.get("sector"), evidence, metrics, lot, tick,
            row.get("pension_eligible"), tuple(row.get("sources", []))))
    by_key = {i.key: i for i in instruments}
    if len(by_key) != len(instruments):
        raise ValueError("duplicate instrument")
    positions = []
    for p in data.get("positions", []):
        at = instant(p["observed_at"])
        if at > now:
            raise ValueError("future holdings snapshot")
        qty = number(p["quantity"], minimum=0)
        sellable = number(p["sellable"], minimum=0) if p.get("sellable") is not None else None
        if p["key"] not in by_key or p["scope"] not in {"personal", "retirement"}:
            raise ValueError("unknown holding identity")
        if sellable is not None and sellable > qty:
            raise ValueError("sellable exceeds holdings")
        sellable_at=instant(p['sellable_at']) if p.get('sellable_at') else None
        if sellable_at is not None and sellable_at>now:
            raise ValueError('future sellable snapshot')
        captured=instant(p['captured_at']) if p.get('captured_at') else at
        if captured>now:raise ValueError('future holdings capture')
        quantity_status=p.get('quantity_status','PROVIDED')
        if quantity_status not in {'PROVIDED','DATED','CALCULATED','UNRESOLVED'}:
            raise ValueError('invalid holdings quantity status')
        positions.append(Position(p["account"], p["scope"], p["key"], qty, number(p["average_cost"], minimum=0) if p.get('average_cost') is not None else None, sellable, at, sellable_at,
                                  captured,p.get('source_status','PROVIDED'),quantity_status))
    identities = [(p.account, p.key) for p in positions]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate account holding")
    cash = []
    for c in data.get("cash", []):
        at = instant(c["observed_at"])
        if at > now:
            raise ValueError("future cash snapshot")
        cash.append(Cash(c["account"], c["scope"], c["currency"], number(c["available"], minimum=0), at,
            c["source"], number(c["total_cash"], minimum=0) if c.get("total_cash") is not None else None, c.get("basis", "cash_buying_power")))
    if len({(c.account, c.currency) for c in cash}) != len(cash):
        raise ValueError("duplicate account cash")
    extra = tuple(ExtraCapital(e["account"], e["scope"], e["currency"], number(e["amount"], minimum=0), e.get("label", "이번 분석의 추가 입금 가정"))
                  for e in data.get("extra_capital", []))
    if len({(e.account, e.currency) for e in extra}) != len(extra):
        raise ValueError("duplicate extra capital")
    scopes = {}
    for row in (*positions, *cash, *extra):
        if row.scope not in {"personal", "retirement"} or not row.account:
            raise ValueError("invalid account scope")
        if row.account in scopes and scopes[row.account] != row.scope:
            raise ValueError("account belongs to different scopes")
        scopes[row.account] = row.scope
        if isinstance(row, (Cash, ExtraCapital)) and row.currency not in {"KRW", "USD"}:
            raise ValueError("unsupported funding currency")
    summaries=[]
    for row in data.get('account_summaries',[]):
        row=dict(row)
        if row.get('scope') not in {'personal','retirement'} or row.get('currency') not in {'KRW','USD'} or not row.get('source'):
            raise ValueError('invalid account summary provenance')
        if scopes.get(row['account'],row['scope'])!=row['scope']:raise ValueError('summary account scope mismatch')
        scopes[row['account']]=row['scope']
        row['observed_at']=instant(row['observed_at'])
        if row['observed_at']>now:raise ValueError('future account summary')
        row['other_assets']=number(row.get('other_assets',0),minimum=0)
        summaries.append(row)
    if len({(s['account'],s['currency']) for s in summaries})!=len(summaries):raise ValueError('duplicate account summary')
    limitations = list(data.get("limitations", []))
    fx = data.get("fx")
    if fx:
        try:
            fx = dict(fx, rate=number(fx["rate"], minimum=Decimal("0.00000001")), observed_at=instant(fx["observed_at"]))
            if fx.get("base_currency") != "USD" or fx.get("quote_currency") != "KRW" or not fx.get("source") or fx["observed_at"] > now:
                raise ValueError("invalid FX evidence")
        except (ValueError, KeyError, TypeError):
            fx = None
            limitations.append("환율 근거를 확인하지 못해 국내·미국을 원래 통화로 분리했습니다.")
    requests=data.get('analysis_requests',[])
    if not isinstance(requests,list):raise ValueError('analysis requests must be a list')
    requested=set()
    by_key={i.key:i for i in instruments}
    for request in requests:
        if not isinstance(request,dict):raise ValueError('analysis request must be object')
        identity=tuple(request.get(k) for k in ('key','account','scope'))
        if (identity[0] not in by_key or scopes.get(identity[1])!=identity[2]
                or identity in requested):raise ValueError('unknown or duplicate analysis request target')
        requested.add(identity)
        if request.get('currency',by_key[identity[0]].currency)!=by_key[identity[0]].currency:
            raise ValueError('request currency differs from instrument')
        if request.get('side','NEW_BUY') not in {'NEW_BUY','SELL'}:raise ValueError('invalid request side')
        for field in ('quantity','entry_price','exit_price','budget'):
            if request.get(field) is not None:
                value=number(request[field],minimum=0)
                if field!='budget' and value<=0:raise ValueError('positive request '+field+' required')
    return DecisionSnapshot(now, tuple(instruments), tuple(positions), tuple(cash), fx, extra,
        tuple(summaries), data.get("fees", {}), tuple(data.get("orders", [])),
        tuple(limitations), bool(data.get("synthetic", False)), data.get('scenario_label'),
        tuple(c for c in data.get('user_choices',[]) if instant(c['recorded_at'])<=now),
        tuple(requests), dict(data.get('investor_flows') or {}))
