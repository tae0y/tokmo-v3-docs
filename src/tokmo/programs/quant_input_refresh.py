"""Recurring input maintenance inside the existing authenticated Pulse.

One small job per tick, no new scheduler, authentication, AI, or delivery.
Account inputs expire after 15 minutes; completed daily bars follow the
broker calendar. Retry state is separate from manually queued capture jobs.
"""
from datetime import datetime, time, timedelta
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from tokmo.programs.price_reference import instant, read_calendar, read_cache
from tokmo.records.quant_decision_reports import atomic_private


def read_json(path):
    return json.loads(path.read_text()) if path.is_file() else {}


def completed_session(calendar, market, now):
    """A close becomes due five minutes after the actual regular session."""
    zone = ZoneInfo('Asia/Seoul' if market == 'KR' else 'America/New_York')
    local = now.astimezone(zone)
    days = {d['trade_date']: d for d in calendar['days'] if d['market'] == market}
    history = calendar['history'].get(market, {})
    for offset in range(30):
        day = (local.date() - timedelta(days=offset)).isoformat()
        opened = history.get(day)
        if opened is None:
            return None  # A missing calendar day is not a confirmed holiday.
        if not opened:
            continue
        sessions = [s for s in days.get(day, {}).get('sessions', []) if s['kind'] == 'regularMarket']
        close = max((instant(s['end']) for s in sessions), default=None)
        if close is None:
            close = datetime.combine(datetime.fromisoformat(day).date(),
                time(15, 30) if market == 'KR' else time(16), tzinfo=zone)
        if close + timedelta(minutes=5) <= now:
            return day
    return None


def session_closes(calendar):
    return {d['market']+':'+d['trade_date']: max(s['end'] for s in d['sessions'] if s['kind']=='regularMarket')
            for d in calendar['days'] if any(s['kind']=='regularMarket' for s in d['sessions'])}


def inventory_jobs(data, calendar, now, *, investor_flows=False):
    personal = sorted((p for p in data['positions'] if p['scope'] == 'personal'),
                      key=lambda p: (p['account'], p['key']))
    accounts = sorted({p['account'] for p in personal} |
                      {a['account'] for a in data.get('accounts', []) if a['scope'] == 'personal'})
    jobs = []
    def add(kind, subset, identity, target):
        key = kind + ':' + sha256(identity.encode()).hexdigest()[:16]
        jobs.append(dict(id=key, kind=kind, data=subset, target=target))
    missing_prices = sorted((i for i in data['instruments'] if i.get('price') is None or
        not i.get('price_at') or now-instant(i['price_at'])>timedelta(minutes=30)),
        key=lambda i:(i['market'],i['symbol']))
    for start in range(0,len(missing_prices),200):
        group=missing_prices[start:start+200]
        identity=','.join(i['market']+':'+i['symbol'] for i in group)
        add('quotes',dict(data,instruments=group),identity,identity)
    for account in accounts:
        positions = [p for p in personal if p['account'] == account]
        subset = dict(data, positions=positions, accounts=[dict(account=account, scope='personal')])
        # Holdings changes make the account/quantity inputs immediately due.
        signature = json.dumps([(p['key'], str(p['quantity'])) for p in positions])
        for kind in ('funds', 'orders'):
            add(kind, subset, account, signature)
    for start in range(0, len(personal), 3):
        positions = personal[start:start + 3]
        identity = json.dumps([(p['account'], p['key']) for p in positions])
        add('sellable', dict(data, positions=positions), identity,
            json.dumps([str(p['quantity']) for p in positions]))
    expected = {m: completed_session(calendar, m, now) for m in ('KR', 'US')}
    for market in ('KR', 'US'):
        if not expected[market]:
            continue
        items = sorted((i for i in data['instruments'] if i['market'] == market), key=lambda i: i['symbol'])
        # One symbol leaves up to three candle pages after metadata, without
        # raising the existing four-call/deadline ceiling for a Pulse tick.
        for item in items:
            group = [item]
            add('candles', dict(data, instruments=group, completed_session=expected[market],
                               session_closes=session_closes(calendar)),
                market + ':' + ','.join(i['symbol'] for i in group), expected[market])
    if investor_flows:
        kr = sorted({i['market']+':'+i['symbol'] for i in data['instruments'] if i['market']=='KR'})
        if kr:
            add('investor_flows',dict(data,flow_targets=['KOSPI','KOSDAQ']), 'markets', 'markets')
        for start in range(0,len(kr),3):
            group=kr[start:start+3]
            add('investor_flows',dict(data,flow_targets=group), ','.join(group), ','.join(group))
    return jobs, expected


def select_due(jobs, state, cache, now):
    cached = {(i['market'], i['symbol']): i for i in cache.get('instruments', [])}
    due = []
    for job in jobs:
        previous = state.get('jobs', {}).get(job['id'], {})
        if previous.get('target') == job['target'] and previous.get('next_at'):
            if instant(previous['next_at']) > now:
                continue
        if job['kind'] == 'candles' and not previous.get('error'):
            end = datetime.fromisoformat(job['target']).date()
            try:
                start = end.replace(year=end.year-1)
            except ValueError:
                start = end.replace(year=end.year-1, day=28)
            def reusable(item):
                from tokmo.programs.quant_decision_inputs import verified_price_history
                row = cached.get((item['market'], item['symbol']), {})
                history = verified_price_history(row) or {}
                try:
                    if history.get('available_at') and instant(history['available_at']) > now:
                        return False
                except (ValueError, TypeError):
                    return False
                # SOURCE_EXHAUSTED is a known short listing, NOT full-year
                # coverage. Keep its PARTIAL label and avoid same-day loops.
                return (isinstance(history.get('requested_start'), str)
                    and history['requested_start'] <= start.isoformat()
                    and history.get('requested_end') == job['target']
                    and history.get('requested_sessions') is None
                    and history.get('stop_reason') in {'TARGET_REACHED', 'SOURCE_EXHAUSTED'})
            if all(reusable(i) for i in job['data']['instruments']):
                continue
        due.append(job)
    # Finish uncaptured groups before repeatedly refreshing the first ones.
    due.sort(key=lambda j: state.get('jobs', {}).get(j['id'], {}).get('last_success_at', ''))
    return due


def refresh_due_inputs(*, project_root, data_root, existing_token, clock, now, deadline, wire_recorder=None):
    from tokmo.facts.toss_reads_live import TossReadsLiveTransport
    from tokmo.programs.quant_input_capture import ExistingTokenReader, capture_job, read_existing_account_refs
    from tokmo.programs.quant_decision_inputs import load_local_snapshot

    root, now = Path(data_root), instant(now)
    directory = root / 'ops/quant-inputs'
    state_path = directory / 'refresh.json'
    state = read_json(state_path)
    cooldowns = [d.get('rate_limit_until') for d in (state, read_cache(root))]
    if any(value and instant(value) > now for value in cooldowns):
        return dict(job='automatic_quant_inputs', skipped=True, calls=[], failures=[], authentication_issued=0)
    config = read_json(root / 'investment_followup.json')
    data = load_local_snapshot(project_root, root, as_of=now, include_context=False)
    calendar = read_calendar(root, at=now)
    jobs, expected = inventory_jobs(data, calendar, now,
                                   investor_flows=config.get('investor_flow_enabled') is True)
    state['jobs'] = {j['id']: state.get('jobs', {}).get(j['id'], {}) for j in jobs}
    cache = read_json(directory / 'latest.json')
    due = select_due(jobs, state, cache, now)
    state.update(checked_at=now.isoformat(), expected_sessions=expected,
                 inventory_count=len(data['instruments']), due_jobs=len(due))
    if not due:
        state['status'] = 'DEGRADED' if (any(v.get('error') for v in state['jobs'].values()) or
            any(not expected.get(i['market']) for i in data['instruments'])) else 'CURRENT'
        atomic_private(state_path, state)
        return dict(job='automatic_quant_inputs', skipped=True, calls=[], failures=[], authentication_issued=0)
    job = due[0]
    previous = state['jobs'][job['id']]
    if previous.get('target') != job['target']:
        previous = {}
    refs = (read_existing_account_refs(root / 'ops/account_native_refs.sqlite3', clock=clock)
            if job['kind'] not in {'candles','quotes','investor_flows'} else None)
    reader = ExistingTokenReader(token=existing_token, refs=refs, transport=TossReadsLiveTransport(),
                                 clock=clock, max_calls=4, deadline=deadline, wire_recorder=wire_recorder)
    result = capture_job(reader, data=job['data'], path=directory / 'latest.json',
                         job=job['kind'], now=now, batch=1 if job['kind']=='candles' else 3)
    if job['kind'] == 'candles' and not result['failures']:
        captured = read_json(directory / 'latest.json')
        by_key = {(i['market'], i['symbol']): i for i in captured.get('instruments', [])}
        for item in job['data']['instruments']:
            if not any(b['date'] == job['target'] for b in by_key.get((item['market'], item['symbol']), {}).get('bars', [])):
                result['failures'].append(dict(operation='STOCK_CANDLES', key=item['market']+':'+item['symbol'],
                                               reason='completed_session_not_returned'))
    if not reader.calls:
        # A busy normal Pulse defers work without consuming a retry attempt.
        state['status'] = 'WAITING'
    else:
        attempts = previous.get('attempts', 0) + 1
        # A new scheduled probe after a one-hour cooldown starts a bounded cycle.
        if attempts > 3:
            attempts = 1
        failed = bool(result['failures'])
        delay = (5 if attempts == 1 else 15 if attempts == 2 else 60) if failed else 15
        record = dict(previous, target=job['target'], at=now.isoformat(),
                      attempts=attempts if failed else 0,
                      next_at=(now + timedelta(minutes=delay)).isoformat(),
                      error=result['failures'] if failed else None)
        if not failed:
            record['last_success_at'] = now.isoformat()
        state['jobs'][job['id']] = record
        state['status'] = 'DEGRADED' if failed else 'REFRESHING'
        if reader.rate_limited:
            seconds = max(60, next((c.get('retry_after', 60) for c in reversed(reader.calls) if c['status'] == 'http_429'), 60))
            state['rate_limit_until'] = (now + timedelta(seconds=seconds)).isoformat()
        state['last_job'] = dict(id=job['id'], kind=job['kind'], at=now.isoformat(),
            failures=result['failures'], calls=reader.calls, authentication_issued=0)
    atomic_private(state_path, state)
    return dict(result, automatic=True, refresh_status=state['status'], skipped=not reader.calls)
