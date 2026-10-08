"""Read-only rotation outcome observer. Never produces a trading decision.

Source stores are opened read-only. Only the separate evaluation directory is
written. Target times are observations, not hypothetical executions.
"""
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
import csv
import fcntl
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from zoneinfo import ZoneInfo

from tokmo.records.quant_decision_reports import atomic_private
from tokmo.values.quant_decisions import instant

KST = ZoneInfo('Asia/Seoul')
HORIZON = 20


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('nonfinite_number')
    return result


def plain(value):
    return format(number(value).quantize(Decimal('0.000001')).normalize(), 'f')


def pct(value, base):
    return plain((number(value) / number(base) - 1) * 100)


def day_of(at):
    return instant(at).astimezone(KST).date().isoformat()


def evaluate(case, market, *, now):
    """Compare a frozen baseline with available sessions and observations."""
    if not case.get('eligible') or not case.get('reference_price') or not case.get('completed_at'):
        return {'status': 'NOT_APPLICABLE'}
    base = number(case['reference_price'])
    if base <= 0:
        return {'status': 'INVALID_REFERENCE'}
    started = instant(case['completed_at'])
    day = case['trade_date']
    # Session identity is not inferred by counting weekdays. Known calendar
    # days remain in this list even when a candle is missing.
    sessions = market['sessions']
    later = sorted(d for d in sessions if d > day)[:HORIZON]
    gaps=[]
    for a,b in zip([day]+later,later):
        if sessions.get(a,{}).get('next_session')==b:continue
        cursor=date.fromisoformat(a)+timedelta(days=1)
        while cursor<date.fromisoformat(b):
            if cursor.weekday()<5:gaps.append(cursor.isoformat())
            cursor+=timedelta(days=1)
    end_day = later[-1] if len(later) == HORIZON else None
    finished = bool(end_day and not gaps and instant(sessions[end_day]['end']) <= now)
    bars = {d: b for d, b in market['bars'].items()
            if d in later and instant(b['available_at']) <= now
            and instant(sessions[d]['end']) <= now}
    quotes = [q for q in market['quotes'].values()
              if started <= instant(q['at']) <= now
              and (day_of(q['at']) == day or day_of(q['at']) in later)
              and (not end_day or instant(q['at']) <= instant(sessions[end_day]['end']))]
    quotes.sort(key=lambda q: instant(q['at']))

    def mark(n):
        if len(later) < n:
            return {'status': 'PENDING_CALENDAR', 'date': None}
        target = later[n-1]
        if any(g<target for g in gaps):
            return {'status':'INCOMPLETE_CALENDAR','date':None}
        if instant(sessions[target]['end']) > now:
            return {'status': 'PENDING', 'date': target}
        if target not in bars:
            return {'status': 'MISSING', 'date': target}
        b = bars[target]
        return dict(status='OBSERVED', date=target, price=b['close'],
                    return_pct=pct(b['close'], base), source=b.get('source'),
                    available_at=b['available_at'])

    lows = [(d, number(b['low'])) for d, b in bars.items()]
    lows += [(day_of(q['at']), number(q['price'])) for q in quotes]
    worst = min([base] + [price for _, price in lows])
    missing = [d for d in later if instant(sessions[d]['end']) <= now and d not in bars]
    targets = {}
    for threshold in (2, 3):
        price = base * (1 + Decimal(threshold)/100)
        origin_bar = market['bars'].get(day)
        origin_unknown = (not origin_bar or instant(origin_bar['available_at']) > now
                          or number(origin_bar['high']) >= price)
        hits = [(d, None, 'trading_day') for d,b in bars.items() if number(b['high']) >= price]
        hits += [(day_of(q['at']), q['at'], 'sampled_observation') for q in quotes if number(q['price']) >= price]
        if not hits:
            status = 'TRACKING' if not finished else 'INCOMPLETE_DATA' if missing or origin_unknown else 'NOT_REACHED_IN_WINDOW'
            targets[str(threshold)] = dict(status=status, threshold_price=plain(price),
                                          analysis_day_touch_unknown=origin_unknown)
            continue
        first_day = min(h[0] for h in hits)
        samples = [h for h in hits if h[0] == first_day and h[1]]
        hit = min(samples,key=lambda h:instant(h[1])) if samples else (first_day,None,'trading_day')
        strict = [v for d,v in lows if d < first_day]
        if hit[1]:
            strict += [number(q['price']) for q in quotes if instant(q['at']) < instant(hit[1])]
        including = [v for d,v in lows if d <= first_day]
        targets[str(threshold)] = dict(status='OBSERVED', threshold_price=plain(price),
            trade_date=first_day, at=hit[1], precision=hit[2],
            elapsed_trading_days=None if any(g<first_day for g in gaps) else 0 if first_day == day else later.index(first_day)+1,
            observed_elapsed_seconds=(instant(hit[1])-started).total_seconds() if hit[1] else None,
            prior_missing_sessions=[d for d in missing if d <= first_day],
            earlier_analysis_day_touch_unknown=origin_unknown and first_day!=day,
            intraday_order_unknown=first_day in bars,
            adverse_return_before_known_hit_pct=pct(min([base]+strict),base),
            adverse_return_including_hit_day_pct=pct(min([base]+including),base))
    return dict(status='COMPLETE' if finished else 'TRACKING', horizon_sessions=HORIZON,
        window_end_date=end_day, as_of=now.isoformat(), reference_price=plain(base),
        d1=mark(1), d5=mark(5), targets=targets, max_adverse_return_pct=pct(worst,base),
        missing_sessions=missing, calendar_gaps=gaps, session_count_basis='saved_calendar_and_completed_bar_dates',
        coverage='sampled_intraday_and_completed_daily_bars',
        same_day_before_first_capture_unknown=True)


def trade_cycles(fills):
    """Only confirmed experimental fills contribute to realized results."""
    cycles=[]; active={}
    for fill in sorted(fills,key=lambda f:(instant(f['observed_at']),f['id'])):
        key=fill['experiment_id']; qty=number(fill['quantity']); amount=number(fill['amount'])
        if qty<=0 or amount<=0:raise ValueError('invalid_fill')
        if fill['side']=='BUY':
            c=active.setdefault(key,dict(id=fill['id'],experiment_id=key,entry_recorded_at=fill['observed_at'],
                buy_quantity=Decimal(0),remaining=Decimal(0),buy_amount=Decimal(0),sell_amount=Decimal(0),fill_ids=[]))
            c['buy_quantity']+=qty;c['remaining']+=qty;c['buy_amount']+=amount;c['fill_ids'].append(fill['id'])
        elif fill['side']=='SELL':
            c=active.get(key)
            if not c or qty>c['remaining']:
                cycles.append(dict(id=fill['id'],status='UNMATCHED_SELL',experiment_id=key));continue
            c['remaining']-=qty;c['sell_amount']+=amount;c['fill_ids'].append(fill['id'])
            c['exit_recorded_at']=fill['observed_at']
            if c['remaining']==0:
                cycles.append(c);del active[key]
    cycles.extend(active.values())
    for c in cycles:
        if 'buy_amount' not in c:continue
        closed=c['remaining']==0
        c.update(status='CLOSED' if closed else 'OPEN',entry_price=plain(c['buy_amount']/c['buy_quantity']),
            gross_pnl=plain(c['sell_amount']-c['buy_amount']) if closed else None,
            gross_return_pct=pct(c['sell_amount'],c['buy_amount']) if closed else None,
            net_return_pct=None,cost_status='UNVERIFIED',time_basis='journal_observed_at_may_be_settlement_fallback')
        for key in ('buy_quantity','remaining','buy_amount','sell_amount'):c[key]=plain(c[key])
    return cycles


def read_json(path):
    if path.is_symlink():raise ValueError('symlink_source')
    return json.loads(path.read_text())


@contextmanager
def database(path):
    # Never use order-journal constructors (they initialize/write schemas).
    if not path.is_file() or path.is_symlink():raise ValueError('database_missing_or_unsafe')
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
    db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN');yield db
    finally:db.close()


def model_calls(report_path):
    calls=report_path.with_name(report_path.stem+'.model-calls.json')
    if calls.exists():raw=read_json(calls)
    else:raw=[read_json(p) for p in sorted((report_path.parent/'model-receipts').glob('*.json'))]
    fields=('call_id','model','resolved_model','reasoning_effort','status','requested_at','ended_at')
    return [{k:c.get(k) for k in fields} for c in raw]


def from_report(path, *, identity=None, decision=None):
    report=read_json(path)
    rows=[r for r in report['results'] if r.get('key')=='KR:005380' and r.get('scope')=='personal' and r.get('scenario')=='cash']
    if len(rows)!=1:raise ValueError('report_scope')
    row=rows[0];packet=row.get('packet') or {};analysis=row.get('analysis') or {};calls=model_calls(path)
    ends=[c['ended_at'] for c in calls if c.get('ended_at')]
    completed=max(ends,key=instant) if ends else None
    valid=row['status']=='ANALYZED' and row.get('analysis_source')=='model' and bool(completed)
    case=dict(id=identity or report['report_id'],kind='daily_quant' if decision else 'requested_quant_comparison',
        trade_date=day_of(report['as_of']),analysis_as_of=report['as_of'],completed_at=completed,
        status=decision['status'] if decision else row['status'],failure_code=(decision or {}).get('failure_code'),
        eligible=valid and (decision or {}).get('status')!='FAILED',action=analysis.get('action'),
        reason=analysis.get('summary'),reference_price=packet.get('price'),reference_price_at=packet.get('price_at'),
        price_proposal=analysis.get('price_proposal'),model_calls=calls,
        model_provenance='call_receipts' if calls else 'UNKNOWN',report_id=report['report_id'],report_path=str(path),
        analysis_contract=packet.get('analysis_contract'),request=packet.get('user_request'),
        decision=decision,scope_note='Different request versions are not controlled model comparisons.')
    return case


def upsert_case(history, case, now):
    prior=history['cases'].get(case['id'])
    # The evaluation archive outlives model receipt/report retention. A source
    # disappearing is a collection issue, not a new opinion or no model call.
    if prior and prior.get('model_calls') and not case.get('model_calls'):
        for key in ('model_calls','model_provenance','reference_price','reference_price_at',
                    'reason','action','completed_at','request','analysis_contract','report_id','report_path','price_proposal'):
            if key in prior:case[key]=prior[key]
        case['eligible']=prior.get('eligible',False) and case['status']!='FAILED'
        case['archived_source_reused']=True
    transitions=list(prior.get('status_history',[])) if prior else []
    if not transitions or transitions[-1]['status']!=case['status']:
        transitions.append(dict(status=case['status'],observed_at=now.isoformat()))
    case['status_history']=transitions
    history['cases'][case['id']]=case


def capture_cases(history,state,data_root,now,issues):
    for path in sorted((state/'decisions').glob('*.json')):
        if path.name.endswith('.delivery.json'):continue
        try:
            doc=read_json(path);identity=doc.get('analysis_id') or path.stem
            if instant(doc['produced_at'])>now:continue
            case=dict(id=identity,kind='daily_quant',trade_date=doc.get('trade_date',day_of(doc['started_at'])),
                completed_at=doc['produced_at'],status=doc['status'],failure_code=doc.get('failure_code'),
                eligible=False,action=None,reference_price=None,model_calls=[],model_provenance='NO_MODEL_RECEIPT',
                decision=doc,decision_path=str(path))
            folder=state/'analysis'/path.stem/'quant'
            reports=[p for p in folder.glob('qd-*.json') if re.fullmatch(r'qd-[a-f0-9]{16}\.json',p.name)]
            if doc.get('report_id'):
                reports=[p for p in reports if p.stem==doc['report_id']]
                if not reports:issues.append('report_missing:'+path.name)
            if len(reports)==1:
                case=from_report(reports[0],identity=identity,decision=doc);case['decision_path']=str(path)
            upsert_case(history,case,now)
        except (ValueError,KeyError,OSError,TypeError) as exc:issues.append('case:'+path.name+':'+type(exc).__name__)
    # Explicitly imported historical comparisons; do not treat arbitrary
    # conversational predictions or synthetic test reports as model decisions.
    seed=data_root/'private/quant-requests/20260916-hyundai-evaluation-baseline/case-index.json'
    if seed.exists():
        try:
            for row in read_json(seed)['cases']:
                if row.get('kind')=='requested_quant_comparison':
                    path=Path(row['report_path'])
                    if data_root.resolve() not in path.resolve().parents:raise ValueError('seed_path_scope')
                    upsert_case(history,from_report(path),now)
        except (ValueError,KeyError,OSError,TypeError) as exc:issues.append('comparison:'+type(exc).__name__)
    journal=state/'orders.sqlite3'
    if journal.exists():
        try:
            with database(journal) as db:
                for row in db.execute("SELECT * FROM rotation_decisions WHERE model_id!='astra' AND status='DECIDED'"):
                    data=json.loads(row['input_json']);out=json.loads(row['output_json'])
                    if data.get('instrument')!={'market':'KR','symbol':'005380'}:continue
                    if instant(row['updated_at'])>now:continue
                    q=data.get('evidence',{}).get('quote',{})
                    upsert_case(history,dict(id='legacy-'+row['decision_key'],kind='legacy_rotation',
                        experiment_id=row['experiment_id'],trade_date=day_of(row['created_at']),
                        completed_at=row['updated_at'],status=row['status'],action=out.get('action'),
                        reason=out.get('reason'),eligible=bool(q.get('last_price')),
                        reference_price=q.get('last_price'),reference_price_at=q.get('as_of'),
                        model_calls=[dict(model=row['model_id'],resolved_model=None,reasoning_effort=None)],
                        model_provenance='journal_declared_not_resolved',decision=out,request=data.get('policy'),
                        decision_key=row['decision_key']),now)
                for row in db.execute("SELECT * FROM experiment_fills WHERE mode='live'"):
                    fill=json.loads(row['fill_json'])
                    if fill.get('symbol')=='005380' and instant(fill['observed_at'])<=now:
                        fill.update(id=row['fill_id'],experiment_id=row['experiment_id'])
                        history['fills'][row['experiment_id']+':'+row['fill_id']]=fill
        except (sqlite3.Error,ValueError,KeyError,OSError,TypeError) as exc:issues.append('journal:'+type(exc).__name__)


def capture_market(history,data_root,now,issues):
    market=history['market'];calendar=[]
    path=data_root/'ops/calendar_lookahead.sqlite3'
    if path.exists():
        try:
            with database(path) as db:
                calendar=[json.loads(r[0]) for r in db.execute("SELECT payload_json FROM calendar_lookahead WHERE market='KR'")]
        except (sqlite3.Error,ValueError,KeyError,OSError) as exc:issues.append('calendar:'+type(exc).__name__)
    next_pairs=list(zip(sorted(d['trade_date'] for d in calendar),sorted(d['trade_date'] for d in calendar)[1:]))
    cache=data_root/'ops/calendar_cache.json'
    if cache.exists():
        try:
            kr=read_json(cache).get('kr')
            if kr:calendar.append(dict(trade_date=kr['trade_date'],sessions=[dict(kind=k,**v) for k,v in kr['sessions'].items()]))
        except (ValueError,KeyError,OSError,TypeError) as exc:issues.append('calendar_cache:'+type(exc).__name__)
    for day in calendar:
        regular=next((s for s in day['sessions'] if s['kind']=='regularMarket'),None)
        if regular:market['sessions'].setdefault(day['trade_date'],{}).update(start=regular['start'],end=regular['end'],source='saved_broker_calendar')
    for a,b in next_pairs:market['sessions'][a]['next_session']=b
    try:
        supplement=read_json(data_root/'ops/quant-inputs/latest.json')
        item=next(i for i in supplement['instruments'] if i.get('symbol')=='005380' and i.get('market')=='KR')
        for raw in item.get('bars',[]):
            b=dict(raw);day=b['date'];available=instant(b['available_at'])
            if available>now:continue
            high,low,close=(number(b[k]) for k in ('high','low','close'))
            if low<=0 or not low<=close<=high:raise ValueError('invalid_bar')
            b['source']='Toss completed daily candles via quant-inputs'
            prior=market['bars'].get(day)
            if prior and any(prior.get(k)!=b.get(k) for k in ('high','low','close','adjusted')):
                revision=dict(date=day,before=prior,after=b,observed_at=now.isoformat())
                history['bar_revisions'].append(revision)
            market['bars'][day]=b
            market['sessions'].setdefault(day,dict(end=b['available_at'],source='completed_daily_bar'))
    except (ValueError,KeyError,OSError,TypeError,StopIteration) as exc:issues.append('bars:'+type(exc).__name__)
    try:
        with database(data_root/'stores/latest_quotes.sqlite3') as db:
            rows=db.execute("SELECT * FROM latest_quotes WHERE symbol='005380' AND source='toss-current-price-v1'").fetchall()
        for row in rows:
            at=instant(row['provider_timestamp']);price=number(row['last_price'])
            if price<=0 or at>now:raise ValueError('invalid_quote_time_or_price')
            day=day_of(at);session=market['sessions'].get(day)
            # Sample only regular-session observations, never relabel a stale
            # previous close as a fresh current price.
            if not session or not session.get('start'):continue
            if not instant(session['start'])<=at<=instant(session['end']):continue
            if now-at>timedelta(minutes=15):
                if instant(session['start'])<=now<=instant(session['end']):issues.append('quote:stale')
                continue
            key=row['source']+':'+at.isoformat()
            market['quotes'].setdefault(key,dict(at=at.isoformat(),price=str(price),source=row['source'],
                fetched_at=row['fetched_at'],first_captured_at=now.isoformat(),venue='not_reported_by_quote_store'))
    except (sqlite3.Error,ValueError,KeyError,OSError,TypeError) as exc:issues.append('quote:'+type(exc).__name__)


def capture_executions(history,state,now,issues):
    for path in sorted((state/'runs').glob('*.json')):
        try:
            run=read_json(path)
            if run.get('live') is not True or instant(run['recorded_at'])>now:continue
            details=run.get('details',{})
            identity=details.get('analysis_id') or 'legacy-'+str(details.get('decision_key'))
            case=history['cases'].get(identity)
            if case is None:continue
            order_id=details.get('order_id')
            linked=[f['id'] for f in history['fills'].values() if f['id']==order_id]
            case['execution']=dict(status=run['status'],reason=run.get('reason'),recorded_at=run['recorded_at'],
                execution_status=details.get('execution_status'),order_id=order_id,linked_fill_ids=linked,
                fill_status='CONFIRMED' if linked else 'NOT_CONFIRMED',receipt_path=str(path))
        except (ValueError,KeyError,OSError,TypeError) as exc:issues.append('execution:'+path.name+':'+type(exc).__name__)


def atomic_text(path,text):
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.'+path.name)
    try:
        with os.fdopen(fd,'w') as f:f.write(text);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def summary_csv(history):
    output=io.StringIO();writer=csv.writer(output)
    writer.writerow(['date','case_id','kind','status','action','model','reasoning','reference_price','D1_date','D1_close','D1_pct',
        'D5_date','D5_close','D5_pct','plus2_status','plus2_first_day','plus2_observed_at','plus2_precision',
        'plus3_status','plus3_first_day','plus3_observed_at','plus3_precision','max_adverse_pct','evaluation_status'])
    for c in sorted(history['cases'].values(),key=lambda c:(c['trade_date'],c['id'])):
        e=c.get('evaluation',{});d1=e.get('d1',{});d5=e.get('d5',{});two=e.get('targets',{}).get('2',{});three=e.get('targets',{}).get('3',{})
        models=c.get('model_calls',[])
        writer.writerow([c['trade_date'],c['id'],c['kind'],c['status'],c.get('action'),
            '|'.join(dict.fromkeys(m.get('resolved_model') or m.get('model') or 'UNKNOWN' for m in models)),
            '|'.join(dict.fromkeys(m.get('reasoning_effort') or 'UNKNOWN' for m in models)),c.get('reference_price'),
            d1.get('date'),d1.get('price'),d1.get('return_pct'),d5.get('date'),d5.get('price'),d5.get('return_pct'),
            two.get('status'),two.get('trade_date'),two.get('at'),two.get('precision'),
            three.get('status'),three.get('trade_date'),three.get('at'),three.get('precision'),e.get('max_adverse_return_pct'),e.get('status')])
    return output.getvalue()


def collect(*,state,data_root,output,now):
    state,data_root,output=map(lambda p:Path(p).absolute(),(state,data_root,output))
    if output==state or output in state.parents or output==data_root or output in data_root.parents:
        raise ValueError('output_must_be_separate')
    if any(p.is_symlink() for p in (output,*output.parents)):raise ValueError('unsafe_output')
    output.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(output/'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'BUSY'}
        path=output/'history.json'
        history=read_json(path) if path.exists() else dict(schema_version=1,horizon_sessions=HORIZON,
            cases={},market=dict(bars={},sessions={},quotes={}),fills={},bar_revisions=[])
        if history['schema_version']!=1:raise ValueError('evaluation_schema')
        issues=[];capture_cases(history,state,data_root,now,issues);capture_market(history,data_root,now,issues)
        capture_executions(history,state,now,issues)
        for case in history['cases'].values():
            try:case['evaluation']=evaluate(case,history['market'],now=now)
            except (ValueError,KeyError,TypeError) as exc:
                case['evaluation']={'status':'INVALID_DATA'};issues.append('evaluate:'+case['id']+':'+type(exc).__name__)
        history['cycles']=trade_cycles(list(history['fills'].values()))
        for cycle in history['cycles']:
            if cycle.get('entry_price'):
                entry=dict(eligible=True,reference_price=cycle['entry_price'],completed_at=cycle['entry_recorded_at'],trade_date=day_of(cycle['entry_recorded_at']))
                cycle['price_followup']=evaluate(entry,history['market'],now=now)
                cycle['price_followup_basis']='market_path_from_fill_basis_including_after_exit'
                cycle['during_holding']=evaluate(entry,history['market'],
                    now=min(now,instant(cycle['exit_recorded_at'])) if cycle.get('exit_recorded_at') else now)
        history.update(updated_at=now.isoformat(),source_issues=issues)
        atomic_private(path,history);atomic_text(output/'summary.csv',summary_csv(history))
        result=dict(status='DEGRADED' if issues else 'OK',at=now.isoformat(),cases=len(history['cases']),
            cycles=len(history['cycles']),quotes=len(history['market']['quotes']),source_issues=issues,horizon_sessions=HORIZON)
        atomic_private(output/'latest-run.json',result)
        return result
    finally:os.close(fd)
