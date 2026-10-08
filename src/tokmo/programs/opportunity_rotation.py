"""KR opportunity discovery packet → verified decision → isolated PAPER accounting."""
from copy import deepcopy
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal

from tokmo.programs.quant_discovery_features import compute_price_features
from tokmo.records.opportunity_rotation import PaperStore
from tokmo.values.quant_discovery import PriceBarV1, PriceSeriesV1, QuantMarketProfileV1
from tokmo.values.opportunity_rotation import (
    BENCHMARK, START_DATE, INITIAL, PRINCIPAL_CAP, Costs, digest, fields, instant,
    number, require_text, ratio, rounded, symbol, text_amount as money, validate_proposal,
    validate_session, won, PROPOSAL_FIELDS, final_bar_at,
)


def pending(state, side=None):
    return [p for p in state['proposals'].values() if p['status'] == 'PENDING' and (side is None or p['side'] == side)]


def position(state, code):
    lots = state['positions'].get(code, [])
    return dict(quantity=sum(l['quantity'] for l in lots),
                principal=sum((number(l['principal']) for l in lots), Decimal(0)),
                buy_cost_basis=sum((number(l['buy_fee']) for l in lots), Decimal(0)))


def available(state):
    return number(state['cash']) - sum((number(p['reserved']) for p in pending(state, 'BUY')), Decimal(0))


def _check(state):
    if available(state) < 0 or number(state['cash']) < 0:
        raise ValueError('insufficient cash including assumed costs')
    codes = set(state['positions']) | {p['symbol'] for p in pending(state, 'BUY')}
    if len(codes) > 5:
        raise ValueError('maximum five held or pending symbols')
    for code in codes:
        principal = position(state, code)['principal'] + sum(
            (number(p['price']) * p['quantity'] for p in pending(state, 'BUY') if p['symbol'] == code), Decimal(0))
        if principal > PRINCIPAL_CAP:
            raise ValueError('per-symbol acquisition principal cap')
    for code in {p['symbol'] for p in pending(state, 'SELL')}:
        if sum(p['quantity'] for p in pending(state, 'SELL') if p['symbol'] == code) > position(state, code)['quantity']:
            raise ValueError('sell quantity already reserved or unavailable')


def _propose(state, proposals, session):
    start, end = validate_session(session)
    costs = Costs(**state['costs'])
    state.setdefault('sessions', {})[session['date']] = deepcopy(session)
    seen = set()
    event_at = state.get('event_at')
    new_times = []
    for raw in sorted(proposals, key=lambda p: p['id']):
        validate_proposal(raw)
        if raw['id'] in seen:
            raise ValueError('duplicate proposal in batch')
        seen.add(raw['id'])
        if raw['id'].endswith(':protection'):
            raise ValueError('reserved protection identity')
        fingerprint = digest(raw)
        old = state['proposals'].get(raw['id'])
        if old:
            if old['fingerprint'] != fingerprint:
                raise ValueError('proposal ID conflict')
            continue
        at = instant(raw['proposed_at'])
        if at.date().isoformat() < START_DATE or at.date() != start.date() or at >= end or (event_at and at < instant(event_at)):
            raise ValueError('proposal outside current decision day or before processed time')
        new_times.append(at)
        principal = number(raw['price']) * raw['quantity']
        reserve = principal + costs.buy(principal) if raw['side'] == 'BUY' else Decimal(0)
        assumed_proceeds = number(raw['assumed_exit_price']) * raw['quantity']
        sell_fee, tax = costs.sell(assumed_proceeds)
        risk = max(Decimal(0), principal - assumed_proceeds) + costs.buy(principal) + sell_fee + tax
        state['proposals'][raw['id']] = dict(deepcopy(raw), status='PENDING', reserved=money(reserve),
            fingerprint=fingerprint, session=deepcopy(session), assumed_risk_amount=money(risk),
            risk_basis='제안가 대비 가정 청산가 및 비용; 갭 최대손실 보장 아님')
    _check(state)
    if new_times:
        state['event_at'] = max(new_times).isoformat()


def _next_minute(state, order):
    cursor = instant(order.get('reconciled_until', order['proposed_at']))
    if cursor.second or cursor.microsecond:
        cursor = cursor.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(370):
        key = cursor.date().isoformat()
        session = state.get('sessions', {}).get(key)
        if session:
            opening, closing = validate_session(session)
            cursor = max(cursor, opening)
            if cursor < final_bar_at(session):
                return cursor + timedelta(minutes=1)
        elif key not in state.get('closed_dates', []):
            return None
        cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
    return None


def _reconciled(state, order, at):
    end = instant(at).replace(second=0, microsecond=0)
    session = order['session'] if order['side']=='BUY' else state.get('sessions',{}).get(end.date().isoformat())
    if session and instant(at) >= instant(session['close']):
        # At the close itself the auction is still unobserved until its final
        # END bar arrives; after it, never demand tomorrow's protection bar.
        end = final_bar_at(session)
    next_at = _next_minute(state, order)
    return next_at is not None and next_at > end or instant(order.get('reconciled_until', order['proposed_at'])) >= end


def _cancel(state, identity, at, reason, *, conditional=False):
    require_text(reason)
    order = state['proposals'].get(identity)
    if order is None:
        raise ValueError('unknown proposal')
    if instant(at) < instant(order['proposed_at']) or (state.get('event_at') and instant(at) < instant(state['event_at'])):
        raise ValueError('backdated cancellation')
    if order['status'] != 'PENDING':
        raise ValueError('proposal is already terminal; nothing cancelled')
    if not conditional and not _reconciled(state, order, at):
        raise ValueError('reconcile validity window before cancellation')
    if order['status'] == 'PENDING':
        order.update(status='CANCELLED', cancelled_at=at, cancellation_reason=reason, reserved='0')
        state['event_at'] = at


def _fill(state, order, bar, observed_at, exit_condition=None):
    costs = Costs(**state['costs'])
    code, quantity = order['symbol'], order['quantity']
    price = number(order['price'])
    if exit_condition == 'STOP':
        price = number(order['stop_price'])
    if order['side'] == 'SELL' and (order['trigger'] == 'BELOW' or exit_condition == 'STOP'):
        price = min(price, number(bar['open']))  # gap through stop: never guarantee trigger price
    principal = price * quantity
    fee, tax = (costs.buy(principal), Decimal(0)) if order['side'] == 'BUY' else costs.sell(principal)
    label = instant(bar['at'])
    closing = instant(state['sessions'][label.date().isoformat()]['close'])
    event_at = min(label,closing).isoformat()
    fill = dict(id=order['id'], symbol=code, side=order['side'], quantity=quantity,
                price=money(price), at=event_at, bar_end_at=bar['at'],
                event_start_at=(label-timedelta(minutes=1)).isoformat(), event_end_at=event_at,
                event_time_kind='CLOSING_AUCTION' if label>closing else 'MINUTE_INTERVAL',
                observed_at=observed_at, fee=money(fee), tax=money(tax), reason=order['reason'],
                expected_move=order['expected_move'], invalidation=order['invalidation'], review_at=order['review_at'],
                assumed_risk_amount=order['assumed_risk_amount'], target=order['target'], weak_reason=order['weak_reason'])
    if exit_condition:
        fill['exit_condition'] = exit_condition
    if order['side'] == 'BUY':
        state['cash'] = money(number(state['cash']) - principal - fee)
        state['positions'].setdefault(code, []).append(dict(quantity=quantity, principal=money(principal),
            buy_fee=money(fee), at=event_at, bar_end_at=bar['at'], proposal_id=order['id']))
    else:
        # FIFO, consuming only lots from earlier completed minutes. Fee allocation
        # rounds each partial release; the final release takes the exact remainder.
        lots = state['positions'][code]
        left, basis, buy_fee, seconds = quantity, Decimal(0), Decimal(0), Decimal(0)
        portions = []
        for lot in lots:
            if not left or instant(lot.get('bar_end_at',lot['at'])) >= label:
                continue
            take = min(left, lot['quantity'])
            fraction = Decimal(take) / lot['quantity']
            released = rounded(number(lot['principal']) * fraction)
            fee_basis = rounded(number(lot['buy_fee']) * fraction)
            duration = int((instant(event_at) - instant(lot['at'])).total_seconds())
            portions.append(dict(quantity=take, bought_at=lot['at'], holding_seconds=duration,
                                 principal=money(released), buy_fee=money(fee_basis)))
            lot.update(quantity=lot['quantity'] - take, principal=money(number(lot['principal']) - released),
                       buy_fee=money(number(lot['buy_fee']) - fee_basis))
            left -= take; basis += released; buy_fee += fee_basis; seconds += duration * take
        if left:
            raise ValueError('sell requires holdings from earlier minutes')
        state['positions'][code] = [l for l in lots if l['quantity']]
        if not state['positions'][code]:
            del state['positions'][code]
        gross = principal - basis
        net = gross - buy_fee - fee - tax
        fill.update(principal_basis=money(basis), buy_cost_basis=money(buy_fee), gross_pnl=money(gross),
                    net_pnl=money(net), price_return=ratio(gross / basis), lots=portions,
                    holding_seconds=int(seconds / quantity))
        state['realized_pnl'] = money(number(state['realized_pnl']) + net)
        state['cash'] = money(number(state['cash']) + principal - fee - tax)
    state['trading_costs'] = money(number(state['trading_costs']) + fee + tax)
    state['fills'].append(fill)
    order.update(status='FILLED', reserved='0', filled_at=event_at, filled_bar_end_at=bar['at'])
    if order['side'] == 'BUY' and 'protection' in order:
        protection = order['protection']
        exit_order = {k:deepcopy(order[k]) for k in PROPOSAL_FIELDS}
        exit_order.update(id=order['id']+':protection', side='SELL', trigger='ABOVE',
            price=protection['target_price'], stop_price=protection['stop_price'],
            status='PENDING', reserved='0', session=deepcopy(order['session']),
            activated_at=event_at, reconciled_until=bar['at'],
            fingerprint=digest(protection), assumed_risk_amount=order['assumed_risk_amount'],
            parent_id=order['id'], risk_basis=order['risk_basis'])
        state['proposals'][exit_order['id']] = exit_order
    _check(state)


def _report(state):
    marks, valuation = state['marks'], state['valuation_at']
    costs = Costs(**state['costs'])
    cash = number(state['cash'])
    value, basis, buy_basis, liquidation = Decimal(0), Decimal(0), Decimal(0), Decimal(0)
    positions, missing = {}, []
    for code, lots in sorted(state['positions'].items()):
        pos = position(state, code)
        quote = marks.get(code)
        valid = quote and quote['at'] == valuation
        marked = number(quote['price']) * pos['quantity'] if valid else None
        if not valid:
            missing.append(code)
        else:
            value += marked; liquidation += sum(costs.sell(marked))
        basis += pos['principal']; buy_basis += pos['buy_cost_basis']
        positions[code] = {k: money(v) if isinstance(v, Decimal) else v for k, v in pos.items()}
        positions[code].update(mark=quote if valid else None, market_value=money(marked) if valid else None,
            average_acquisition_price=money(pos['principal']/pos['quantity']),
            cost_inclusive_average_price=money((pos['principal']+pos['buy_cost_basis'])/pos['quantity']),
            gross_unrealized_pnl=money(marked-pos['principal']) if valid else None,
            price_return=ratio(marked/pos['principal']-1) if valid else None,
            unrealized_pnl=money(marked - pos['principal'] - pos['buy_cost_basis']) if valid else None,
            lots=[dict(l, holding_seconds=int((instant(state['as_of'])-instant(l['at'])).total_seconds())) for l in lots])
    nav = None if missing else cash + value
    benchmark = deepcopy(state['benchmark'])
    if benchmark is None:
        benchmark = dict(status='PENDING_OPEN', symbol=BENCHMARK, start_date=START_DATE,
                         reason='2026-09-28 공식 정규장 첫 분봉 시가 확인 대기')
    else:
        quote = marks.get(BENCHMARK)
        if quote and quote['at'] == valuation:
            marked = number(quote['price']) * benchmark['quantity']
            benchmark_nav = number(benchmark['cash']) + marked
            estimate = sum(costs.sell(marked, benchmark=True))
            benchmark.update(status='VALUED', nav=money(benchmark_nav),
                return_=ratio(benchmark_nav / INITIAL - 1), valuation_at=valuation,
                hypothetical_liquidation_cost=money(estimate), liquidation_nav=money(benchmark_nav-estimate))
            benchmark['return'] = benchmark.pop('return_')
        else:
            benchmark.update(status='MISSING_MARK', nav=None, return_=None)
            benchmark['return'] = benchmark.pop('return_')
    open_ids = {l['proposal_id'] for lots in state['positions'].values() for l in lots}
    active_ids = open_ids | {p['id'] for p in pending(state)}
    due = sorted(i for i in active_ids if state['as_of'] and instant(state['proposals'][i]['review_at']) <= instant(state['as_of']))
    return dict(mode='PAPER', initial_cash=money(INITIAL), as_of=state['as_of'], valuation_at=valuation,
        cash=state['cash'], available_cash=money(available(state)), reserved_cash=money(cash-available(state)),
        nav=money(nav) if nav is not None else None, total_return=ratio(nav/INITIAL-1) if nav is not None else None,
        gross_total_pnl=money(nav-INITIAL+number(state['trading_costs'])) if nav is not None else None,
        realized_pnl=state['realized_pnl'], unrealized_pnl=money(value-basis-buy_basis) if not missing else None,
        trading_costs=state['trading_costs'], cash_fraction=ratio(cash/nav) if nav else None,
        hypothetical_liquidation_cost=money(liquidation) if not missing else None,
        liquidation_nav=money(nav-liquidation) if nav is not None else None,
        costs=state['costs'], cost_rounding='각 매수 수수료/매도 수수료/세금 별 원 단위 ROUND_HALF_UP',
        positions=positions, missing_marks=missing, benchmark=benchmark,
        observed_max_drawdown=state['max_drawdown'], drawdown_basis='완전한 가격 표본의 NAV; 장중 연속 최대낙폭 아님',
        samples=len(state['samples']), closed_trades=[f for f in state['fills'] if f['side']=='SELL'],
        pending=deepcopy(pending(state)), review_due=due,
        reconciliation=state.get('reconciliation', {'status':'NOT_CHECKED'}),
        settlement=state.get('settlement', {'status':'NOT_CLOSED'}),
        comparison='동일 valuation_at의 평가만 비교; 미확인 가격과 미래 시초가 대체 없음')


def _validate_market(state, market, observed_at):
    fields(market, ('as_of','sessions','bars','coverage','valuation_at','marks','source'), ('closed_dates',))
    now = instant(observed_at)
    if instant(market['as_of']) > now:
        raise ValueError('future captured market')
    require_text(market['source'])
    if instant(market['valuation_at']) > instant(market['as_of']):
        raise ValueError('future valuation')
    captured = state.get('captured_at',state['as_of'])
    if captured and instant(market['as_of']) <= instant(captured) or state['as_of'] and now <= instant(state['as_of']):
        raise ValueError('out-of-order or revised advance; replay original batch only')
    if state.get('event_at') and now < instant(state['event_at']):
        raise ValueError('advance predates last PAPER event')
    if any(now < instant(p['proposed_at']) for p in state['proposals'].values()):
        raise ValueError('advance predates proposal')
    sessions = {}
    for s in market['sessions']:
        validate_session(s)
        if s['date'] in sessions:
            raise ValueError('duplicate session')
        sessions[s['date']] = s
    for q in market['marks'].values():
        fields(q, ('price','at')); won(q['price'], positive=True)
        if instant(q['at']) > instant(market['as_of']):
            raise ValueError('future quote')
    for p in pending(state):
        if not any(c['symbol'] == p['symbol'] for c in market['coverage']):
            raise ValueError('minute coverage missing')
    previous_sessions = state.get('sessions', {})
    for key, session in sessions.items():
        if key in previous_sessions and previous_sessions[key] != session:
            raise ValueError('official session changed')
    bars = {}
    for b in market['bars']:
        fields(b, ('symbol','at','open','high','low','close','volume','currency','interval','adjusted'))
        symbol(b['symbol']); ts = instant(b['at'])
        prices = [won(b[k], positive=True) for k in ('open','high','low','close')]
        if (b['currency']!='KRW' or b['interval']!='1m' or b['adjusted'] is not False or ts.second or ts.microsecond
                or number(b['volume']) < 0 or not prices[2] <= min(prices[0],prices[3]) <= max(prices[0],prices[3]) <= prices[1]):
            raise ValueError('invalid unadjusted KRW minute candle')
        key = (b['at'], b['symbol'])
        if key in bars and bars[key] != b:
            raise ValueError('conflicting duplicate minute')
        bars[key] = b
    return now, sessions, sorted(bars.values(), key=lambda b:(instant(b['at']),b['symbol']))


def _consume_bars(state, bars, market, observed_at):
    for b in bars:
        ts = instant(b['at'])
        session = state['sessions'][ts.date().isoformat()]
        opening, closing = validate_session(session)
        if state['benchmark'] is None and b['symbol']==BENCHMARK and session['date']==START_DATE and ts==opening+timedelta(minutes=1) and number(b['volume'])>0:
            costs = Costs(**state['costs']); price = number(b['open']); qty = int(INITIAL // price)
            while qty and price*qty + costs.buy(price*qty) > INITIAL:
                qty -= 1
            fee = costs.buy(price*qty)
            state['benchmark'] = dict(symbol=BENCHMARK, start_date=START_DATE, open_price=b['open'], quantity=qty,
                cash=money(INITIAL-price*qty-fee), buy_fee=money(fee), open_at=opening.isoformat(),
                observed_at=observed_at, source=market['source'])
        for p in sorted(pending(state), key=lambda p:(instant(p['proposed_at']),p['id'])):
            if p['symbol'] != b['symbol'] or _next_minute(state,p) != ts:
                continue
            if p['side']=='BUY' and ts > final_bar_at(p['session']):
                continue
            p['reconciled_until'] = b['at']
            stop_hit = 'stop_price' in p and number(b['low']) <= number(p['stop_price'])
            hit = number(b['low']) <= number(p['price']) if p['trigger']=='BELOW' else number(b['high']) >= number(p['price'])
            if not (hit or stop_hit) or number(b['volume']) == 0:
                continue
            if p['side']=='SELL' and sum(l['quantity'] for l in state['positions'].get(p['symbol'],[]) if instant(l.get('bar_end_at',l['at'])) < ts) < p['quantity']:
                continue
            _fill(state, p, b, observed_at, 'STOP' if stop_hit else 'TARGET' if 'stop_price' in p else None)


def _sample(state, at, nav, benchmark_nav):
    state['samples'] = sorted([s for s in state['samples'] if s['at'] != at] +
        [dict(at=at, nav=money(nav), benchmark_nav=benchmark_nav)], key=lambda s:instant(s['at']))
    peak, drawdown = INITIAL, Decimal(0)
    for sample in state['samples']:
        value = number(sample['nav'])
        peak = max(peak, value)
        drawdown = max(drawdown, (peak-value)/peak)
    state.update(peak_nav=money(peak), max_drawdown=ratio(drawdown))


def _settle_closes(state, bars, observed_at):
    """Keep official close facts; value dated fills, never today's holdings."""
    closes = state.setdefault('daily_closes', {})
    for day, session in sorted(state['sessions'].items()):
        if day < START_DATE or instant(observed_at) < instant(session['close']):
            continue
        cutoff = final_bar_at(session)
        if day not in closes and any(s['at']==cutoff.isoformat() and s['benchmark_nav'] is not None for s in state['samples']):
            continue  # A complete legacy close sample is already settled.
        close = closes.setdefault(day, dict(date=day, status='PENDING_DATA',
            valuation_at=cutoff.isoformat(), marks={}, required_symbols=[BENCHMARK]))
        if close['status'] == 'SETTLED':
            continue
        for bar in bars:
            if instant(bar['at']) == cutoff and number(bar['volume']) > 0:
                prior = close['marks'].get(bar['symbol'])
                if prior is not None and prior != bar['close']:
                    raise ValueError('previously observed close revised')
                close['marks'][bar['symbol']] = bar['close']
        cash, quantities = INITIAL, {}
        for fill in state['fills']:
            if instant(fill.get('bar_end_at',fill['at'])) > cutoff:
                continue
            signed = fill['quantity'] if fill['side']=='BUY' else -fill['quantity']
            quantities[fill['symbol']] = quantities.get(fill['symbol'],0) + signed
            cash -= number(fill['price'])*signed + number(fill['fee']) + number(fill['tax'])
        close['required_symbols'] = sorted({BENCHMARK} | {c for c,q in quantities.items() if q})
        complete = all(c in close['marks'] for c in close['required_symbols']) and state['benchmark'] is not None
        complete = complete and all(_reconciled(state,p,cutoff) for p in pending(state) if instant(p['proposed_at']) < cutoff)
        complete = complete and not any(s['status']=='WAITING_DATA' and instant(s.get('effective_at',s['at'])) <= cutoff
            for s in state.get('slots',{}).values())
        close['observed_at'] = observed_at
        if complete:
            nav = cash + sum(number(close['marks'][c])*q for c,q in quantities.items() if q)
            benchmark = state['benchmark']
            benchmark_nav = number(benchmark['cash']) + number(close['marks'][BENCHMARK])*benchmark['quantity']
            _sample(state, cutoff.isoformat(), nav, money(benchmark_nav))
            close['status'] = 'SETTLED'
    if closes:
        ordered = [closes[d] for d in sorted(closes)]
        latest = next((c for c in ordered if c['status']=='PENDING_DATA'), ordered[-1])
        state['settlement'] = {k:latest[k] for k in ('date','status','observed_at','valuation_at')}


def _advance(state, market, observed_at=None):
    observed_at = observed_at or market['as_of']
    identity = digest(market)
    if identity in state['advances']:
        return state
    now, sessions, bars = _validate_market(state, market, observed_at)
    state.setdefault('sessions', {}).update(deepcopy(sessions))
    state['closed_dates'] = sorted(set(state.get('closed_dates', [])) | set(market.get('closed_dates', [])))
    cache = state.setdefault('minute_bars', {})
    for b in bars:
        ts = instant(b['at'])
        session = state['sessions'].get(ts.date().isoformat())
        if not session:
            continue
        opening, closing = validate_session(session)
        if not opening < ts <= final_bar_at(session) or ts > instant(market['as_of']):
            continue
        key = b['symbol'] + ':' + b['at']
        if key in cache and cache[key] != b:
            raise ValueError('previously observed candle revised')
        cache[key] = deepcopy(b)
    waiting = next((k for k,v in state.get('slots',{}).items() if v['status']=='WAITING_DATA'), None)
    if waiting:
        _resolve_saved(state, waiting, now)
    saved = state.get('slots',{}).get(waiting,{})
    boundary = instant(saved['effective_at']) if saved.get('status')=='WAITING_DATA' else None
    ordered = sorted(cache.values(), key=lambda b:(instant(b['at']), b['symbol']))
    if boundary:
        _consume_bars(state, [b for b in ordered if instant(b['at']) <= boundary], market, observed_at)
        _resolve_saved(state, waiting, now)
    if saved.get('status') != 'WAITING_DATA':
        _consume_bars(state, ordered, market, observed_at)
    for p in pending(state,'BUY'):
        if now >= instant(p['session']['close']) and _reconciled(state,p,p['session']['close']):
            p.update(status='EXPIRED', reserved='0', expired_at=p['session']['close'], observed_at=observed_at)
    state['reconciliation'] = dict(status='PENDING_GAPS' if any(not _reconciled(state,p,observed_at) for p in pending(state)) else 'COMPLETE',
        observed_at=observed_at, pending=[p['id'] for p in pending(state) if not _reconciled(state,p,observed_at)])
    # Keep only unresolved minutes; fills/checkpoints retain processed evidence.
    needed = {}
    future = [p for p in saved.get('changes',{}).values() if 'proposed_at' in p] if saved.get('status')=='WAITING_DATA' else []
    for order in pending(state) + future:
        next_at = _next_minute(state,order)
        if next_at is not None:
            needed[order['symbol']] = min(needed.get(order['symbol'],next_at),next_at)
    state['minute_bars'] = {k:b for k,b in cache.items() if b['symbol'] in needed and instant(b['at']) >= needed[b['symbol']]}
    state.update(as_of=observed_at, captured_at=market['as_of'], event_at=observed_at, valuation_at=market['valuation_at'], marks=deepcopy(market['marks']))
    state['advances'].append(identity)
    report = _report(state)
    today = state['sessions'].get(instant(market['valuation_at']).date().isoformat())
    if (report['nav'] is not None and state['reconciliation']['status']=='COMPLETE'
            and not (today and instant(market['valuation_at'])==final_bar_at(today))
            and not any(s['at']==market['valuation_at'] for s in state['samples'])):
        _sample(state, market['valuation_at'], number(report['nav']), report['benchmark'].get('nav'))
    _settle_closes(state, ordered, observed_at)
    _check(state)
    return state


class Paper:
    def __init__(self, root):
        self.store = PaperStore(root)

    def initialize(self, costs):
        if not isinstance(costs, Costs):
            raise ValueError('labelled Costs required')
        def create(old):
            if old is not None:
                raise ValueError('PAPER already initialized; no reset or deposits')
            return dict(version=1, cash=money(INITIAL), costs=asdict(costs), positions={}, proposals={}, fills=[],
                        realized_pnl='0', trading_costs='0', decisions={}, advances=[], as_of=None, event_at=None,
                        valuation_at=None, marks={}, benchmark=None, peak_nav=money(INITIAL), max_drawdown='0', samples=[])
        self.store.transaction(create, initialize=True)
        return self.report()

    def state(self):
        return self.store.read()

    def report(self):
        return _report(self.state())

    def propose(self, proposals, session):
        def update(s):
            _propose(s, proposals, session)
            return s
        self.store.transaction(update)

    def cancel(self, identity, at, reason):
        def update(s):
            _cancel(s, identity, at, reason)
            return s
        self.store.transaction(update)

    def advance(self, market, *, observed_at=None):
        self.store.transaction(lambda s: _advance(s, market, observed_at))
        return self.report()

    def decide(self, packet, decision, at):
        def update(s):
            _decide(s, packet, decision, at)
            return s
        self.store.transaction(update)
        return self.report()


def _entry_ready(candidate, completed):
    """Recompute entry eligibility from the bound history, not a derived flag."""
    try:
        bars = candidate['bars']
        return bool(candidate['eligible'] is True and bars and candidate.get('quote')
            and number(candidate['quote']['price'])>0 and bars[-1]['date']==completed
            and candidate['history']['sha256']==digest(bars)
            and candidate['history']['observations']==len(bars))
    except (KeyError,TypeError,ValueError):
        return False


def build_packet(discovery, state):
    """No ranking cutoff or composite score. Facts and preferences stay separate."""
    result = {k:deepcopy(discovery[k]) for k in ('as_of','session','completed_session','source','universe','rankings_as_of','candidates','exclusions','acquisition_started_at') if k in discovery}
    result['candidates'] = [{k:v for k,v in item.items() if k in ('symbol','name','eligible','ranking','quote','bars','missing','evidence','context_status','metadata','history_source','history_observed_at','history_provenance')} for item in result['candidates']]
    priority = set(state['positions']) | {p['symbol'] for p in pending(state)}
    result['candidates'].sort(key=lambda c:(c['symbol'] not in priority,c['symbol']))
    as_of = instant(result['as_of']); validate_session(result['session'])
    if date.fromisoformat(result['completed_session']) > as_of.date():
        raise ValueError('future completed session')
    codes = set()
    for item in result['candidates']:
        code = symbol(item['symbol'])
        if code in codes:
            raise ValueError('duplicate candidate')
        codes.add(code)
        if item.get('quote'):
            won(item['quote']['price'], positive=True)
            if instant(item['quote']['at']) > as_of:
                raise ValueError('future quote in packet')
        bars = tuple(PriceBarV1(date.fromisoformat(b['date']), *(number(b[k]) for k in ('open','high','low','close','volume')), None) for b in item['bars'])
        features = {}
        if bars:
            outcome = compute_price_features(PriceSeriesV1(code,QuantMarketProfileV1.KR,bars), completed_session_date=date.fromisoformat(result['completed_session']))
            features = {f.name:dict(value=money(f.raw_value) if f.raw_value is not None else None,
                                    status=f.status.value, observations=f.observed_count, minimum=f.minimum_count) for f in outcome.features.factors}
        item['features'] = features
        item['history'] = dict(observations=len(bars), first_session=item['bars'][0]['date'] if bars else None,
            last_session=item['bars'][-1]['date'] if bars else None, completed_session=result['completed_session'],
            sha256=digest(item['bars']), interval='1d', adjusted=True,
            calendar_continuity='NOT_VERIFIED; factor status/counts and missing fields describe available history')
        item['range_reference'] = dict(low=min((b['low'] for b in item['bars']),key=number),
            high=max((b['high'] for b in item['bars']),key=number), basis='제공 완료 일봉의 관측 범위; 지지/저항 확정 아님') if bars else None
        item['trade_ready'] = _entry_ready(item,result['completed_session'])
    result.update(version=1, automation='COMMON_PRODUCT_GATE_REQUIRED', portfolio_revision=digest(state),
        portfolio=_report(state), preferences=dict(normal_target='+3%', weak_target='+2%, 약세 사유 필수',
        sell_gate='수익률 제한 없음: 손절/본전/교체 허용', cash='현금 대기 허용',
        review='재검토일은 강제 청산일이 아님'),
        instruction='보유+매수예약 최대 5종목, 종목당 보유 원금+매수예약 원금 100만원. 비용 포함 현재 가용현금 안에서 정수 주식만 제안. '
                    '보유와 미체결을 먼저 모두 검토하고 신규 기회를 비교. BUY는 protection {target_price, stop_price} 필수. SELL은 stop_price로 같은 수량의 대안 청산 가능. 명시적인 cancel_ids는 재조정 후 해제. 모든 보유를 ENTER/ADD/HOLD/EXIT/SWITCH/WATCH와 현금 대기로 비교. 짧은 근거, 기대, 무효 조건과 재검토 시점 기록. '
                    '뉴스 원문 사실과 기대를 구분하고 결측·중복 노출을 설명. 순위만으로 매수하지 말 것. SWITCH는 매도 후 새 packet에서 매수 판단.',
        decision_contract=dict(required=['id','packet_id','author','verified_by','verified_at','assessments','cash_reason'],
            model_response_fields=['cash_reason','assessments'],
            assessment_actions=['ENTER','ADD','HOLD','EXIT','SWITCH','WATCH','CANCEL'],
            proposal_fields=['id','symbol','side','trigger','price','quantity','reason','expected_move','invalidation','review_at','assumed_exit_price','target','weak_reason'], buy_required=['protection'],
            protection_fields=['target_price','stop_price'], sell_optional=['stop_price'], assessment_optional=['cancel_ids'],
            response='Return exactly cash_reason (nonempty text) and assessments (list of objects). '
                'The adapter supplies the other required decision envelope fields; do not return them. '
                'action_fields lists every required assessment field for each action; only cancel_ids is optional for all actions. '
                'No other assessment fields: expected_move, invalidation and review_at belong inside a trading proposal, never WATCH/HOLD.',
            action_fields=dict(
                ENTER=['symbol','action','reason','proposal'], ADD=['symbol','action','reason','proposal'],
                EXIT=['symbol','action','reason','proposal'],
                SWITCH=['symbol','action','reason','proposal','replacement_symbol','replacement_reason'],
                WATCH=['symbol','action','reason'], HOLD=['symbol','action','reason'],
                CANCEL=['symbol','action','reason','proposal_id']),
            assessment_meanings='symbol is a six-character string of digits or uppercase A-Z, known in candidates or holdings; '
                'at most one assessment per symbol. reason is nonempty text. ENTER/WATCH require no current holding; '
                'ADD/HOLD/EXIT/SWITCH require a holding; CANCEL has no holding requirement. '
                'Review every held or pending symbol; other candidates need not each have an assessment. '
                'An empty assessments list is valid when there are no holdings or pending proposals. '
                'ENTER/ADD use a BUY proposal for the same symbol and require candidate trade_ready. EXIT/SWITCH use SELL for the same symbol. '
                'SWITCH requires replacement_symbol naming a trade_ready candidate and nonempty replacement_reason; '
                'it proposes only the sale, with replacement buying considered in a new packet. '
                'CANCEL proposal_id names an existing pending proposal for this symbol. '
                'cancel_ids is a list of unique existing pending proposal IDs for this symbol, processed before the action; '
                'do not cancel the same ID again via proposal_id. Cancellation requires validity reconciliation before release.',
            proposal_enums=dict(side=['BUY','SELL'], trigger=['BELOW','ABOVE'], target=['NORMAL','WEAK','NONE']),
            proposal_meanings=dict(
                fields='Every proposal_fields entry is required, including weak_reason. Do not return proposed_at; the application supplies it. '
                    'BUY additionally requires protection, with exactly target_price and stop_price. '
                    'SELL may additionally have stop_price. No other proposal fields.',
                id='Nonempty text ID; unique within the proposal batch, never ending in :protection. '
                    'An existing ID is accepted only for the identical proposal including application-supplied proposed_at; conflicts fail.',
                side_trigger='ENTER/ADD: BUY with BELOW only. EXIT/SWITCH: SELL with ABOVE or BELOW. '
                    'BELOW means downward price reach; ABOVE means upward price reach. LIMIT is not a trigger value. '
                    'price is the proposed entry/sale trigger price; quantity is the number of shares.',
                text='reason, expected_move and invalidation are nonempty text: rationale, expected development, and invalidating condition.',
                amounts='quantity must be a positive JSON integer, not boolean, float or string. '
                    'price, assumed_exit_price, protection.target_price, protection.stop_price and optional stop_price are positive whole KRW: '
                    'exact numeric strings or JSON integers, never booleans or JSON floats. '
                    'Numbers must be finite, at most 1e20, with at most 12 decimal places in their representation; fractional KRW is invalid.',
                assumed_exit_price='Assumed liquidation price used to estimate downside plus costs; for BUY, assumed_exit_price < price. '
                    'It is not a take-profit price or a guaranteed maximum loss. SELL requires a positive whole KRW value without that inequality. '
                    'BUY assumed_exit_price need not equal protection.stop_price.',
                protection='BUY only: 0 < protection.stop_price < price < protection.target_price. '
                    'Standing protection is required even with target NONE. Optional top-level stop_price is SELL only, '
                    'requires trigger ABOVE and 0 < stop_price < price; it is an alternative exit for the same quantity.',
                target='NORMAL, WEAK or NONE on either side; +3% and +2% are preferences, not enum values or mandatory price ratios. '
                    'weak_reason must be nonempty text for WEAK and JSON null for NORMAL/NONE. '
                    'SELL has no minimum return: loss, breakeven and replacement sales are allowed.'),
            timestamps='review_at must be an ISO-8601 timestamp with timezone offset and satisfy review_at > proposed_at. '
                'It is a review time, not forced liquidation, and need not be before the session close. '
                'proposed_at is the actual decision application boundary, not packet.as_of or a quote/snapshot time. '
                'In the scheduler, verified_at is actual model completion time and effective_at is the next whole minute after completion; '
                'proposed_at is fixed to effective_at, strictly before session close, even if reconciliation delays application. '
                'Choose review_at after that boundary allowing for response latency; the application does not move review_at forward. '
                'Direct decisions use their supplied application time. Verification must satisfy packet.as_of <= verified_at <= application boundary; '
                'new proposals must be on the packet session date, on/after the PAPER start date and before session close, '
                'never before the already processed event time. Do not backdate or invent missing market data.',
            budget='BUY reserves price * quantity plus assumed buy fees from available cash; unfilled SELL proceeds are unavailable. '
                'Held plus BUY-pending symbols are limited to five and held principal plus BUY-pending principal to KRW 1000000 per symbol. '
                'SELL quantities together with existing pending sells cannot exceed held quantity; cancel/reconcile any conflicting reservations.'))
    result['id'] = digest(result)
    return result


def decision_basis(state):
    """Economic inputs that must survive a saved response's reconciliation."""
    return digest(dict(cash=state['cash'], costs=state['costs'], positions=state['positions'],
        decisions=state['decisions'], proposals={i:{k:v for k,v in p.items() if k!='reconciled_until'}
            for i,p in state['proposals'].items()}))


def _resolve_saved(state, key, now):
    """Apply only an already validated, durable prospective plan; never re-date it."""
    saved = state['slots'][key]
    if saved['status'] != 'WAITING_DATA':
        return state
    if state.get('event_at') and now < instant(state['event_at']):
        raise ValueError('backdated transition observation')
    saved['checked_at'] = now.isoformat()
    if 'effective_at' not in saved or 'changes' not in saved:
        saved.update(status='SUPERSEDED', reason='legacy response has no prospective intent')
    elif saved['basis'] != decision_basis(state):
        saved.update(status='SUPERSEDED', reason='portfolio changed before transition')
    elif now >= instant(saved['effective_at']) and all(_reconciled(state,p,saved['effective_at']) for p in pending(state)):
        for identity, change in saved['changes'].items():
            state['proposals'].setdefault(identity,{}).update(deepcopy(change), applied_at=now.isoformat())
        decision = saved['decision']
        state['decisions'][key] = dict(deepcopy(decision), fingerprint=digest(decision),
            submitted_at=saved['recorded_at'], effective_at=saved['effective_at'], applied_at=now.isoformat())
        state['event_at'] = max(now,instant(state.get('event_at') or now)).isoformat()
        _check(state)
        saved.update(status='DECIDED', reason=None, applied_at=now.isoformat())
    return state


def _decide(state, packet, decision, at, *, reconciled=False, prospective=False):
    fields(decision, ('id','packet_id','author','verified_by','verified_at','assessments','cash_reason'))
    for key in ('id','author','verified_by','cash_reason'):
        require_text(decision[key])
    if packet.get('id') != digest({k:v for k,v in packet.items() if k!='id'}) or decision['packet_id'] != packet['id']:
        raise ValueError('packet integrity or binding failure')
    identity = digest(decision)
    old = state['decisions'].get(decision['id'])
    if old:
        if old['fingerprint'] != identity:
            raise ValueError('decision ID conflict')
        return
    now = instant(at)
    if state.get('event_at') and now < instant(state['event_at']):
        raise ValueError('backdated decision')
    revision_matches = packet['portfolio_revision'] == digest(state)
    if reconciled:
        saved = state.get('slots',{}).get(decision['id'],{})
        revision_matches = (saved.get('status')=='WAITING_DATA' and saved.get('packet')==packet
            and saved.get('decision')==decision and saved.get('basis')==decision_basis(state))
    if prospective and (not reconciled or saved.get('effective_at') != at
            or not instant(saved['recorded_at']) == instant(decision['verified_at']) < now
            or now != instant(saved['recorded_at']).replace(second=0,microsecond=0)+timedelta(minutes=1)):
        raise ValueError('prospective decision requires a fixed future boundary')
    if (not revision_matches or now.date().isoformat()!=packet['session']['date']
            or not instant(packet['as_of']) <= instant(decision['verified_at']) <= now):
        raise ValueError('stale portfolio, date or verification')
    candidates = {c['symbol']:c for c in packet['candidates']}
    if not isinstance(decision['assessments'], list):
        raise ValueError('assessments list required')
    covered, proposals = set(), []
    for assessment in decision['assessments']:
        fields(assessment, ('symbol','action','reason'), ('proposal','proposal_id','replacement_symbol','replacement_reason','cancel_ids'))
        code = symbol(assessment['symbol']); action = assessment['action']; require_text(assessment['reason'])
        if code in covered or code not in candidates and code not in state['positions']:
            raise ValueError('duplicate or unknown assessment symbol')
        covered.add(code)
        if action not in ('ENTER','ADD','HOLD','EXIT','SWITCH','WATCH','CANCEL'):
            raise ValueError('unknown decision action')
        permitted = {'symbol','action','reason'}
        if action in ('ENTER','ADD','EXIT','SWITCH'):
            permitted.add('proposal')
        if action=='SWITCH':
            permitted.update(('replacement_symbol','replacement_reason'))
        if action=='CANCEL':
            permitted.add('proposal_id')
        if 'cancel_ids' in assessment:
            permitted.add('cancel_ids')
            if not isinstance(assessment['cancel_ids'],list) or len(set(assessment['cancel_ids'])) != len(assessment['cancel_ids']):
                raise ValueError('unique cancel_ids required')
            for identity in assessment['cancel_ids']:
                if state['proposals'].get(identity,{}).get('symbol') != code:
                    raise ValueError('cancellation symbol mismatch')
                _cancel(state,identity,at,assessment['reason'],conditional=prospective)
        fields(assessment, permitted)
        held = bool(position(state,code)['quantity'])
        if action in ('ADD','HOLD','EXIT','SWITCH') and not held or action in ('ENTER','WATCH') and held:
            raise ValueError('action conflicts with holding state')
        if action=='SWITCH':
            replacement = candidates.get(assessment.get('replacement_symbol'))
            if not replacement or not replacement['trade_ready'] or not _entry_ready(replacement,packet['completed_session']):
                raise ValueError('replacement candidate not ready')
            require_text(assessment.get('replacement_reason'))
        if action=='CANCEL':
            p = state['proposals'].get(assessment.get('proposal_id'))
            if not p or p['symbol'] != code:
                raise ValueError('cancellation binding failure')
            _cancel(state,p['id'],at,assessment['reason'],conditional=prospective)
        elif action in ('ENTER','ADD','EXIT','SWITCH'):
            fields(assessment['proposal'], set(PROPOSAL_FIELDS)-{'proposed_at'}, ('protection','stop_price'))
            p = dict(assessment.get('proposal') or {}, proposed_at=at)
            validate_proposal(p)
            side = 'BUY' if action in ('ENTER','ADD') else 'SELL'
            if p['symbol']!=code or p['side']!=side:
                raise ValueError('proposal action mismatch')
            if side=='BUY' and 'protection' not in p:
                raise ValueError('buy plan requires standing protection')
            if side=='BUY' and (not candidates.get(code,{}).get('trade_ready')
                    or not _entry_ready(candidates[code],packet['completed_session'])):
                raise ValueError('candidate missing entry facts')
            proposals.append(p)
        elif set(assessment) - {'cancel_ids'} != {'symbol','action','reason'}:
            raise ValueError('non-trading assessment contains trade fields')
    if (set(state['positions']) | {p['symbol'] for p in pending(state)}) - covered:
        raise ValueError('all holdings and pending proposals require review; hold/watch is valid')
    _propose(state, proposals, packet['session'])
    state['decisions'][decision['id']] = dict(deepcopy(decision), fingerprint=identity, submitted_at=at)
    state['event_at'] = at
