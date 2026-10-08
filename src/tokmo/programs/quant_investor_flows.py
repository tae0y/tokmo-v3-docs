"""Small dated investor-flow context, collected by the existing Pulse reader.

Market KRW (KRX) and stock shares (KRX+NXT) remain separate. Missing
intraday personal flows are unknown, never a residual or zero estimate.
"""
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from tokmo.facts.toss_reads import TossReadOperation as Op, SPEC_TABLE
from tokmo.values import MISSING
from tokmo.values.quant_decisions import instant

KST = ZoneInfo('Asia/Seoul')
GROUPS = ('individual', 'foreigner', 'institution')


def _net(value, *, volume=False):
    if value is None or value is MISSING: return None
    buy = Decimal(value.buy_volume if volume else value.buy_amount)
    sell = Decimal(value.sell_volume if volume else value.sell_amount)
    if any(not v.is_finite() or v < 0 or v != v.to_integral() for v in (buy, sell)):
        raise ValueError('invalid investor amount/volume')
    if volume and buy - sell != Decimal(value.net_buy_volume):
        raise ValueError('investor net does not match buy minus sell')
    return str(buy - sell)


def capture_flows(fetch, *, targets, now):
    result = {'investor_flows': {}, 'failures': []}
    for key in targets:
        market = key in {'KOSPI', 'KOSDAQ'}
        op = Op.INDICATOR_INVESTOR_TRADING if market else Op.STOCK_INVESTOR_TRADING
        try:
            response = fetch(op, symbol=key if market else key.removeprefix('KR:'), count=21,
                             **({'interval': '1d'} if market else {}))
            # The provider may update the current row while this GET is in
            # flight. Compare to receipt time, not the tick's start time.
            received_at = fetch.clock() if callable(getattr(fetch, 'clock', None)) else now
            received_at = max(instant(received_at), now)
            records = []
            for row in response.records:
                if row.updated_at > received_at or row.date > received_at.astimezone(KST).date():
                    raise ValueError('future investor flow: source='+row.updated_at.isoformat()+
                                     ' received='+received_at.isoformat())
                if row.date > row.updated_at.astimezone(KST).date():
                    raise ValueError('flow before its trading date')
                if market:
                    from tokmo.values.toss_investor_trading import InvestorTradingAmount
                    institution = InvestorTradingAmount(row.institution_buy_amount, row.institution_sell_amount)
                else:
                    institution = row.institution
                records.append(dict(date=row.date.isoformat(), updated_at=row.updated_at.isoformat(),
                    net={group: _net(value, volume=not market) for group, value in
                         zip(GROUPS, (row.individual, row.foreigner, institution))}))
            if not records: raise ValueError('no investor trading records')
            if len({r['date'] for r in records}) != len(records): raise ValueError('duplicate investor date')
            result['investor_flows'][key] = dict(
                records=sorted(records, key=lambda r: r['date'], reverse=True)[:21],
                fetched_at=received_at.isoformat(), error=None,
                unit='KRW' if market else 'shares', exchange='KRX' if market else 'KRX+NXT',
                foreigner_basis='registered_and_unregistered' if market else 'registered',
                source=SPEC_TABLE[op].path)
        except Exception as error:
            # Finite retry and provider cooldown belong to the existing refresh worker.
            reason = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
            result['failures'].append(dict(operation=op.value, key=key, reason=reason[:160]))
    return result


def read_flows(root):
    path = Path(root) / 'ops/quant-inputs/latest.json'
    try: return json.loads(path.read_text()).get('investor_flows', {}) if path.is_file() else {}
    except (ValueError, OSError): return {}


def compact_flows(flows, *, key, now):
    """Related KR series only; no raw articles, account data, or fetch timestamps."""
    if not key.startswith('KR:'): return {}
    today = now.astimezone(KST).date().isoformat()
    result = {}
    for identity in ('KOSPI', 'KOSDAQ', key):
        series = flows.get(identity, {})
        try:
            rows = [r for r in series.get('records', []) if r['date'] <= today and instant(r['updated_at']) <= now]
            rows.sort(key=lambda r: r['date'], reverse=True)
            if not rows:
                result[identity] = dict(status='UNAVAILABLE')
                continue
            latest = rows[0]
            fetched = instant(series['fetched_at'])
            if fetched > now: raise ValueError('future capture')
            market = identity in {'KOSPI', 'KOSDAQ'}
            # Exclude today's changing aggregates from completed-day windows.
            history = [r for r in rows if r['date'] < today][:20]
            windows = []
            for count in (5, 20):
                selected = history[:count]
                net = {}
                for group in GROUPS:
                    values = [r['net'].get(group) for r in selected]
                    net[group] = str(sum(Decimal(v) for v in values)) if values and all(v is not None for v in values) else None
                windows.append(dict(requested_sessions=count, observed_sessions=len(selected),
                    complete=len(selected) == count and all(v is not None for v in net.values()),
                    start=selected[-1]['date'] if selected else None, end=selected[0]['date'] if selected else None,
                    net=net))
            delayed = bool(series.get('error')) or now - fetched > timedelta(hours=1)
            # Do not claim freshness just because the endpoint answered now.
            if (latest['date'] == today and 9 <= now.astimezone(KST).hour < 16
                    and now - instant(latest['updated_at']) > timedelta(hours=1)): delayed = True
            if (now.astimezone(KST).weekday() < 5 and now.astimezone(KST).hour >= 10
                    and latest['date'] < today):
                source_status = 'LATEST_REPORTED_DAY_ONLY'  # Not a holiday guess or today's zero flow.
            else:
                source_status = 'LATEST_REPORTED'
            result[identity] = dict(status='DELAYED' if delayed else source_status,
                unit='KRW' if market else 'shares', exchange='KRX' if market else 'KRX+NXT',
                foreigner_basis=series['foreigner_basis'], source=series['source'],
                latest=dict(latest, basis='PROVISIONAL' if latest['date'] == today or
                            latest['net'].get('individual') is None else 'DAILY_REPORTED'),
                windows=windows)
        except (ValueError, KeyError, TypeError, ArithmeticError):
            result[identity] = dict(status='UNAVAILABLE')
    return result
