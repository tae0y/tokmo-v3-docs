"""Compact model views over existing event records; original sources stay intact.

Memory is a dated model interpretation, never an independent official source.
Delivery snapshots belong to proposals/receipts, not to the latest internal state.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from tokmo.records.news_decision_store import timestamp, kst


MEMORY_FIELDS = {'topic': 100, 'phase': 180, 'summary': 600, 'watch': 300}


def compact_portfolio(context):
    result = deepcopy(context)
    fields = ('key', 'name', 'kind')
    if isinstance(result.get('instruments'), list):
        result['instruments'] = [{k: row[k] for k in fields if k in row}
                                 for row in result['instruments']]
    if isinstance(result.get('holdings'), list):
        # Keep every held identity/account, including small and retirement
        # positions. Display quantities/prices use the original context.
        fields = ('key', 'name', 'kind', 'scope', 'account', 'currency', 'observed_at', 'quantity_basis')
        result['holdings'] = [{k: row[k] for k in fields if k in row} |
            ({} if row.get('key') else {'symbol': row.get('symbol'), 'market_raw': row.get('market_raw')})
            for row in result['holdings']]
    return result


def memory_view(event):
    memory = event.get('context_memory')
    if memory:
        return deepcopy(memory)
    decision = event.get('decision') or {}
    return {'status': 'LEGACY_DECISION_ONLY', 'topic': decision.get('title', ''),
            'summary': decision.get('facts', '')[:600], 'watch': decision.get('next_check', '')[:300],
            'phase': 'UNKNOWN', 'source_ids': decision.get('evidence_ids', [])[:8],
            'reviewed_at': event.get('updated_at')}


def update_memory(update, previous, evidence, *, at):
    """Invalid auxiliary memory must not veto an otherwise useful first warning."""
    if update is None:
        return deepcopy((previous or {}).get('context_memory')), 'NOT_PROVIDED'
    if not isinstance(update, dict):
        return deepcopy((previous or {}).get('context_memory')), 'INVALID_UPDATE'
    for key, limit in MEMORY_FIELDS.items():
        if not isinstance(update.get(key), str) or not update[key].strip() or len(update[key]) > limit:
            return deepcopy((previous or {}).get('context_memory')), 'INVALID_' + key.upper()
    known = {row['id']: row for row in evidence}
    ids = update.get('source_ids')
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 8
            or any(not isinstance(key, str) or key not in known for key in ids)):
        return deepcopy((previous or {}).get('context_memory')), 'INVALID_SOURCE_IDS'
    rows = [known[key] for key in ids]
    memory = {key: update[key].strip() for key in MEMORY_FIELDS}
    memory.update(source_ids=list(dict.fromkeys(ids)), status='MODEL_INTERPRETATION',
                  reviewed_at=kst(at),
                  source_observed_through=kst(max(timestamp(r['observed_at']) for r in rows)))
    prior = (previous or {}).get('context_memory') or {}
    memory['first_observed_at'] = prior.get('first_observed_at') or kst(min(timestamp(r['observed_at']) for r in rows))
    return memory, None


def related_context(events, ids, *, exclude, at):
    rows = []
    for event in events:
        if event['id'] not in ids or event['id'] == exclude or timestamp(event['updated_at']) > timestamp(at):
            continue
        rows.append({'event_id': event['id'], 'memory': memory_view(event),
                     'last_sent': deepcopy(event.get('last_sent'))})
    return rows[:8]


def compact_prior(prior):
    result = deepcopy(prior)
    if isinstance(result.get('context_snapshot'), dict):
        result['context_snapshot'] = compact_portfolio(result['context_snapshot'])
    last_id = (result.get('last_sent') or {}).get('alert_id')
    history = []
    for row in result.get('delivered_history', []):
        if row['alert_id'] == last_id:
            continue  # Exact same receipt/body already supplied in last_sent.
        if row.get('context_memory'):
            history.append({k: row[k] for k in ('alert_id','delivered_at','context_memory','evidence_ids') if k in row})
        else:
            history.append(row)  # No retrospective AI summary of legacy sends.
    if 'delivered_history' in result:
        result['delivered_history'] = history
    return result


def observation_view(event):
    evidence = event.get('evidence') or []
    if not evidence:
        return None
    at = max(timestamp(row['observed_at']) for row in evidence)
    return {'observed_through':kst(at),'observations':len(evidence),
            'numeric_progress':numeric_progress(evidence,
                baseline_ids=set((event.get('last_sent') or {}).get('evidence_ids') or []),at=at)}


def numeric_progress(evidence, *, baseline_ids, at):
    """Only source-supplied structured measurements, never numbers guessed from prose.

    Optional measurements: {metric, value, unit, measured_at}. The pair
    metric/unit must identify one comparable series. No currency conversion.
    """
    series = {}
    for row in evidence:
        if timestamp(row['observed_at']) > timestamp(at):
            continue
        measurements = row.get('measurements', [])
        if not isinstance(measurements, list):
            continue
        for point in measurements:
            try:
                metric, unit = point['metric'], point['unit']
                when = timestamp(point['measured_at'])
                value = Decimal(str(point['value']))
                if (not isinstance(metric,str) or not isinstance(unit,str) or not metric or not unit
                        or not value.is_finite() or when > timestamp(row['observed_at'])):
                    continue
            except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
                continue
            series.setdefault((metric,unit), []).append((when,row['id'],value))
    output = []
    for (metric,unit), points in sorted(series.items()):
        points.sort()
        # Conflicting values at one measurement time are not a reliable series.
        if any(len({v for t,_,v in points if t==when})>1 for when,_,_ in points):
            continue
        baselines = [p for p in points if p[1] in baseline_ids]
        if not baselines:
            continue
        base, latest = baselines[-1], points[-1]
        if latest[0] <= base[0]:
            continue
        output.append({'metric':metric,'unit':unit,'baseline_source_id':base[1],'latest_source_id':latest[1],
                       'baseline_value':str(base[2]),'latest_value':str(latest[2]),
                       'change':str(latest[2]-base[2]),
                       'change_percent':str((latest[2]/base[2]-1)*100) if base[2] != 0 else None,
                       'elapsed_hours':(latest[0]-base[0]).total_seconds()/3600})
    return output[:8]
