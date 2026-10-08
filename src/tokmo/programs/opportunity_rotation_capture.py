"""Resumable public facts using Pulse's explicit remaining wire/time allowance.

A paused discovery/candle page is replayed from credential-free wire results.
Only the next absent response consumes a wire. This is a small product capture,
not a token service; all auth, deadlines and total limits belong to Pulse.
"""
from datetime import date, timedelta
import json
from pathlib import Path

from tokmo.facts.opportunity_rotation import TossOpportunitySource, SourceUnavailable, valid_daily, daily_through
from tokmo.facts.toss import _TossTransportSuccess
from tokmo.facts.toss_reads import REQUEST_TIMEOUT_SECONDS
from tokmo.programs.opportunity_rotation import pending
from tokmo.records.opportunity_rotation import PaperStore
from tokmo.records.quant_decision_reports import atomic_private
from tokmo.values.opportunity_rotation import BENCHMARK, START_DATE, digest, instant, number


class CaptureDeferred(Exception):
    pass


def quant_daily(root, now):
    """Read only public instrument fields from the existing official capture."""
    from tokmo.programs.quant_decision_inputs import verified_price_history
    path = Path(root)/'ops/quant-inputs/latest.json'
    try:
        saved = json.loads(path.read_text()) if path.exists() else {}
        instruments = saved.get('instruments',[]) if isinstance(saved,dict) else []
        if not isinstance(instruments,list):return {}
    except (ValueError,OSError):
        return {}
    result = {}
    for item in instruments:
        if not isinstance(item,dict):continue
        try:
            history = verified_price_history(item)
            # This store's existing producer establishes candle provenance;
            # require its official metadata too, or explicit candle provenance.
            if not history or history.get('source') not in (None,'Toss STOCK_CANDLES 1d adjusted') or (
                    history.get('source') is None and
                    item.get('metrics',{}).get('shares_outstanding',{}).get('source')!='Toss GET /api/v1/stocks'):
                continue
            row = dict(symbol=item['symbol'],market=item['market'],currency=item['currency'],adjusted=True,
                completed_session=history['requested_end'],observed_at=history['available_at'],
                source='Toss STOCK_CANDLES 1d adjusted',exhausted=history['stop_reason']=='SOURCE_EXHAUSTED',
                bars=[{k:b[k] for k in ('date','open','high','low','close','volume')} for b in item['bars'][-200:]])
            if any(instant(b['available_at'])>instant(now) for b in item['bars']):
                continue
            if valid_daily(row,item['symbol'],now):result[item['symbol']]=row
        except (KeyError,ValueError,TypeError,AttributeError):
            continue
    return result


def capture_consumer(*, data_root, transport, price_transport, clock, sleeper):
    root = Path(data_root)/'opportunity-rotation'
    path = root/'captured.json'
    store = PaperStore(root/'paper')

    def due(now):
        observed = instant(now)
        if not store.path.is_file() or observed.date().isoformat() < START_DATE:
            return False
        saved = json.loads(path.read_text()) if path.exists() else {}
        if saved.get('blocked'):
            return False
        if saved.get('resume_after') and observed < instant(saved['resume_after']):
            return False
        session = saved.get('discovery',{}).get('session')
        if session and session['date']==observed.date().isoformat():
            return instant(session['open'])-timedelta(hours=1) <= observed <= instant(session['close'])+timedelta(hours=1)
        # Only acquisition bootstrapping; trading still requires official calendar.
        return 8 <= observed.hour <= 16

    def consume(*, existing_token, now, deadline, wire_recorder, remaining_budget):
        if type(remaining_budget) is not int or remaining_budget < 0:
            raise ValueError('explicit nonnegative remaining budget required')
        if not store.path.is_file() or instant(now).date().isoformat() < START_DATE:
            return dict(status='NO_PAPER',calls=0)
        state = store.read()
        capture = json.loads(path.read_text()) if path.exists() else dict(work={})
        if capture.get('blocked'):
            return dict(status='BLOCKED',calls=0)
        observed = instant(now)
        if capture.get('resume_after') and observed < instant(capture['resume_after']):
            return dict(status='DEFERRED',calls=0)
        if capture.get('day') != observed.date().isoformat():
            capture.update(day=observed.date().isoformat(),work={
                'discovery':dict(job=None,cache={k:v for k,v in capture.get('work',{}).get('discovery',{}).get('cache',{}).items()
                    if v.get('operation')=='STOCK_CANDLES' and v.get('params',{}).get('interval')=='1d'})})
        tracked = sorted(set(state['positions']) | {p['symbol'] for p in pending(state)})
        unsettled = [c for c in state.get('daily_closes',{}).values() if c['status']=='PENDING_DATA']
        started = []
        daily_cache = capture.setdefault('daily',{})
        for code,row in quant_daily(data_root,clock()).items():
            previous = daily_cache.get(code,{})
            if not valid_daily(previous,code,clock()) or row['completed_session']>previous['completed_session']:
                daily_cache[code] = row

        def acquire(mode, allowance):
            work = capture['work'].setdefault(mode,dict(cache={},job=None))
            job = work.get('job')
            if job is None:
                start = min([p['proposed_at'] for p in pending(state)] or [observed.replace(hour=9,minute=0,second=0,microsecond=0).isoformat()])
                if state['benchmark'] is None:
                    start = min(start,START_DATE+'T00:00:00+09:00')
                job = dict(mode=mode,start=start,end=observed.replace(second=0,microsecond=0).isoformat(),tracked=tracked)
                if mode=='market' and unsettled:
                    job['start'] = min([start] + [(instant(c['valuation_at'])-timedelta(minutes=1)).isoformat() for c in unsettled])
                    job['tracked'] = sorted(set(tracked) | {code for c in unsettled for code in c['required_symbols'] if code not in c['marks']})
                work['job'] = job
            if mode=='discovery':
                job['tracked'] = sorted(set(job['tracked']) | set(tracked))
            raw_response, active = {}, {}
            limit = len(started)+allowance

            def read(request):
                key = active['key']
                if key in work['cache']:
                    return _TossTransportSuccess(body=json.dumps(work['cache'][key]['body']).encode())
                sleeper(2)  # Also separate the first start from prior normal work.
                if (deadline-clock()).total_seconds()<REQUEST_TIMEOUT_SECONDS+1:
                    raise CaptureDeferred()
                wire_recorder('OPPORTUNITY_READ',key)
                started.append(active['operation'])
                result = (price_transport if active['operation']=='CURRENT_PRICES' else transport)(request)
                if isinstance(result,_TossTransportSuccess):
                    body = json.loads(result.body)
                    if existing_token in json.dumps(body):
                        raise ValueError('credential in public response')
                    raw_response[key] = body
                return result

            class ResumableSource(TossOpportunitySource):
                def _request(self,operation,**kwargs):
                    params = {k:v.isoformat() if hasattr(v,'isoformat') else v for k,v in kwargs.items()}
                    key = digest(dict(operation=operation,params=params))
                    if key not in work['cache'] and (len(started)>=limit or (deadline-clock()).total_seconds()<REQUEST_TIMEOUT_SECONDS+3):
                        raise CaptureDeferred()
                    active.update(key=key,operation=operation)
                    result = super()._request(operation,**kwargs)
                    if key in raw_response:
                        work['cache'][key] = dict(operation=operation,params=params,body=raw_response[key],observed_at=clock().isoformat())
                    if operation=='STOCK_CANDLES' and kwargs.get('interval')=='1d':
                        self.daily_observed_at = work['cache'][key]['observed_at']
                    return result

                def daily(self,code,completed,*,count=200):
                    prior = daily_cache.get(code,{})
                    selected = daily_through(prior,code,completed,clock())
                    if selected is not None:
                        return selected
                    if valid_daily(prior,code,clock()):
                        previous = date.fromisoformat(prior['completed_session'])
                        if previous<completed:
                            gap = min(200,(completed-previous).days+1)
                            recent = super().daily(code,completed,count=gap)
                            recent['observed_at'] = self.daily_observed_at
                            old = {b['date']:b for b in prior['bars']}
                            overlap = [b for b in recent['bars'] if b['date'] in old]
                            if overlap and all(old[b['date']]==b for b in overlap):
                                old.update({b['date']:b for b in recent['bars']})
                                merged = dict(recent,bars=[old[d] for d in sorted(old)][-200:],
                                    exhausted=prior['exhausted'],
                                    prior_observed_at=prior['observed_at'])
                                if valid_daily(merged,code,clock()):
                                    daily_cache[code] = merged
                                    return merged
                            # No verified overlap or changed adjustments: full page.
                    row = super().daily(code,completed)
                    params = dict(symbol=code,interval='1d',count=200,adjusted=True,
                        before=instant(completed.isoformat()+'T23:59:59+09:00').isoformat())
                    cached = work['cache'].get(digest(dict(operation='STOCK_CANDLES',params=params)))
                    if cached:row['observed_at'] = cached['observed_at']
                    if valid_daily(row,code,clock()):
                        daily_cache[code] = row
                    else:
                        row['bars'] = []
                        row['exhausted'] = False  # Invalid data is never terminal-empty evidence.
                        work['cache'].pop(digest(dict(operation='STOCK_CANDLES',params=params)),None)
                    return row

                def minutes(self,code,start,end):
                    # Reuse only actual completed candles. Stop the checkpoint at
                    # the first missing minute, so late bars are fetched again.
                    prior = {instant(b['at']):b for b in capture.get('market',{}).get('bars',[])
                             if b['symbol']==code and start<instant(b['at'])<=end}
                    cursor = start
                    while cursor+timedelta(minutes=1) in prior:
                        cursor += timedelta(minutes=1)
                    if cursor < end:
                        for row in super().minutes(code,cursor,end):
                            ts = instant(row['at'])
                            if ts in prior and prior[ts]!=row:
                                raise ValueError('conflicting completed candle')
                            prior[ts] = row
                    return [prior[ts] for ts in sorted(prior)]

            source = ResumableSource(token=existing_token,transport=read,price_transport=read,
                clock=clock,max_calls=max(1,len(work['cache'])+allowance),pace=lambda:None)
            try:
                if mode=='discovery':
                    data = source.discover(held_symbols=job['tracked'])
                    # Refresh provider quotes after cold histories finish; never
                    # make an old quote fresh by changing its event timestamp.
                    if (any(c.get('quote') and not timedelta(0)<=instant(clock())-instant(c['quote']['at'])<=timedelta(minutes=3) for c in data['candidates'])
                            or any(not r.get('at') or not timedelta(0)<=instant(clock())-instant(r['at'])<=timedelta(minutes=3) for r in data['rankings_as_of'])):
                        work['cache'] = {k:v for k,v in work['cache'].items() if v['operation'] not in ('CURRENT_PRICES','RANKINGS','STOCKS')}
                        raise CaptureDeferred()
                    data['acquisition_started_at'] = job['end']
                    capture['discovery'] = data
                else:
                    data = source.capture(symbols=set(job['tracked'])|{BENCHMARK},start=job['start'],end=job['end'])
                    # All symbols share the real cutoff. Observation time never
                    # replaces valuation/mark/candle event time.
                    data['as_of'] = clock().isoformat()
                    capture['market'] = data
                work['job'] = None
                work['cache'] = {k:v for k,v in work['cache'].items()
                    if v['operation']=='KR_MARKET_CALENDAR' or
                    (v['operation']=='STOCK_CANDLES' and v['params'].get('interval')=='1d')}
                return 'CAPTURED'
            except CaptureDeferred:
                return 'DEFERRED'

        status = 'DEFERRED'
        try:
            if remaining_budget:
                # Reserve time as well as wires for discovery. A long minute
                # backlog cannot repeatedly exhaust the deadline ahead of it.
                discovery_status = acquire('discovery',min(2,max(1,remaining_budget//3)))
                market_status = acquire('market',remaining_budget-len(started))
                status = 'MARKET_CAPTURED' if market_status=='CAPTURED' else market_status
                if len(started)<remaining_budget and discovery_status!='CAPTURED':
                    discovery_status = acquire('discovery',remaining_budget-len(started))
                if discovery_status=='CAPTURED':
                    status = discovery_status
        except Exception as error:
            # These are existing closed source codes, never exception payloads.
            transient = isinstance(error,SourceUnavailable) and error.code in ('http_429','http_5xx','timeout','connection_failure')
            capture['last_error'] = dict(at=clock().isoformat(),code=error.code if isinstance(error,SourceUnavailable) else type(error).__name__,
                transient=transient,recovered_at=None)
            if transient:
                capture['resume_after'] = max(observed.replace(second=0,microsecond=0)+timedelta(minutes=1),
                    instant(clock())+timedelta(seconds=error.retry_after or 0)).isoformat()
            else:
                capture['blocked'] = capture['last_error']['code']
            status = 'FAILED'
        if status!='FAILED' and started and capture.get('last_error',{}).get('transient'):
            if capture['last_error']['recovered_at'] is None:
                capture['last_error']['recovered_at'] = clock().isoformat()
            capture.pop('resume_after',None)
        # Promotion can precede a deferred discovery; retain newer partial input.
        work = capture['work'].get('discovery',{})
        work['cache'] = {k:v for k,v in work.get('cache',{}).items()
            if not (v['operation']=='STOCK_CANDLES' and v['params'].get('interval')=='1d'
                and valid_daily(daily_cache.get(v['params']['symbol'],{}),v['params']['symbol'],clock())
                and v['params']['before'][:10]<=daily_cache[v['params']['symbol']]['completed_session'])}
        capture['receipt'] = dict(at=clock().isoformat(),status=status,operations=started,
                                  additional_token_issuance=0,remaining_budget=remaining_budget)
        atomic_private(path,capture)
        return dict(status=status,calls=len(started))
    consume.due = due
    return consume
