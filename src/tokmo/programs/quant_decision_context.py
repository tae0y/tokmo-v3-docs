"""Bounded source context for investment decisions, using existing local stores.

No provider calls, model calls, database initialization or NEWS mutations.
These are dated source excerpts, not an automatic verdict on an event's state.
"""
from __future__ import annotations

from datetime import timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time

from tokmo.programs.internal_evidence_query import EvidenceQueryV1, expand_query_terms, run_internal_evidence_query
from tokmo.programs.news_topic_discovery import _normalise, _tokens
from tokmo.programs.quant_local_context import collect_local_news_items
from tokmo.values.news_instrument_match import is_etf_name
from tokmo.values.quant_decisions import instant

NEWS_LIMIT = 12
DISCLOSURE_LIMIT = 6
TEXT_LIMIT = 1800
TOTAL_TEXT_LIMIT = 24000
# Legal names followed by Korean particles remain the same company. A company
# name followed by another business name (e.g. 중공업터보기계) does not.
_PARTICLES = {'은', '는', '이', '가', '을', '를', '의', '와', '과', '에', '에서', '에게', '에도', '으로', '로', '도', '만'}


def _query(query, family, **kwargs):
    result = run_internal_evidence_query(query, **kwargs)['families'][family]
    if result['store_health'] == 'STORE_BUSY_OR_LOCKED':
        # One short read retry handles a concurrent collector commit. No
        # provider/model retry and no lock-holder interruption.
        time.sleep(.05)
        result = run_internal_evidence_query(query, **kwargs)['families'][family]
    return result


def company_match(text, names):
    tokens = _tokens(_normalise(text))
    for name in names:
        parts = _tokens(_normalise(name))
        if not parts:
            continue
        for start in range(len(tokens) - len(parts) + 1):
            found = tokens[start:start + len(parts)]
            if found[:-1] != parts[:-1]:
                continue
            last, expected = found[-1], parts[-1]
            if last == expected or (last.startswith(expected) and last[len(expected):] in _PARTICLES):
                return True
    return False


def _open(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    return connection


def _excerpt(value, limit=TEXT_LIMIT):
    text = ' '.join(str(value or '').split())
    if len(text) <= limit:
        return text, False
    # Keep the original order and signal the omission. No generated paraphrase.
    head = text[:limit - 220].rsplit(' ', 1)[0]
    tail = text[-200:].split(' ', 1)[-1]
    # Do not turn a clipped amount into a different apparent number.
    if ' ' not in text[:limit - 220]:
        head = ''
    if ' ' not in text[-200:]:
        tail = ''
    return head + ' …[중간 생략]… ' + tail, True


def _time(value):
    try:
        return instant(value)
    except (ValueError, TypeError, AttributeError):
        return None


def _order(evidence):
    # Date-only filings have no publication clock. Sort those by their actual
    # collection time without presenting that time as publication time.
    return (_time(evidence['metadata'].get('published_at')) or instant(evidence['available_at']), evidence['id'])


def _filing_versions(path, receipts, now):
    """Read the actual listing revision available at this cutoff."""
    connection = _open(path)
    try:
        connection.execute('BEGIN')
        rows = connection.execute('SELECT * FROM disclosures WHERE rcept_no IN (' + ','.join('?' for _ in receipts) + ')', receipts).fetchall()
        revisions = connection.execute('SELECT * FROM dart_revisions WHERE rcept_no IN (' + ','.join('?' for _ in receipts) + ') ORDER BY revision_id', receipts).fetchall()
        changes = {}
        for revision in revisions:
            changes.setdefault(revision['rcept_no'], []).append(dict(revision))
        result = {}
        for original in map(dict, rows):
            history = changes.get(original['rcept_no'], [])
            first = history[0].get('previous_canonical_json') if history else original['canonical_json']
            versions = [(original['first_observed_at'], first)]
            versions.extend((r['observed_at'], r.get('observed_canonical_json')) for r in history)
            eligible = [(instant(at), payload) for at, payload in versions if payload and instant(at) <= now]
            if not eligible:
                continue
            at, payload = max(eligible, key=lambda pair: pair[0])
            value = json.loads(payload)
            result[original['rcept_no']] = dict(original, **{k: value[k] for k in ('corp_name', 'stock_code', 'report_nm', 'rcept_dt') if k in value},
                revision_available_at=at.isoformat())
        return result
    finally:
        connection.close()


def _news_evidence(row, now):
    first = _time(row.get('first_seen_at'))
    revised = (row.get('revision_no') or 1) > 1
    dates = [first]
    if revised:
        dates.extend(_time(row.get(k)) for k in ('last_seen_at', 'updated_at'))
    available = max((v for v in dates if v is not None), default=None)
    published = _time(row.get('published_at'))
    if first is None or available is None or available > now or (published and published > now):
        return None
    body = row.get('body_text') if row.get('content_state') == 'BODY_AVAILABLE' else None
    text, clipped = _excerpt(body or row.get('summary_text'))
    extent = 'body_excerpt' if body else 'source_summary' if text else 'title_only'
    identity = 'news:' + row['news_document_id'] + ':' + str(row.get('revision_no') or 1)
    return dict(id='local-' + sha256(identity.encode()).hexdigest()[:16], kind='news',
        text=row['title'] + ('\n' + text if text and text != row['title'] else ''),
        available_at=available.isoformat(), source=row.get('canonical_url') or '로컬 NEWS [' + row['news_document_id'] + ']',
        metadata=dict(title=row['title'], document_id=row['news_document_id'],
            published_at=row.get('published_at'), first_seen_at=row['first_seen_at'],
            content_extent=extent, excerpt_truncated=clipped,
            original_text_chars=len(body or row.get('summary_text') or ''),
            content_mode=row.get('content_mode'), publisher=row.get('publisher_name'),
            revision_no=row.get('revision_no') or 1,
            history_scope='current_revision_only' if revised else 'original_collection'))


def _cached_disclosures(path, now):
    """Source payloads only; NEWS model decisions never become official facts."""
    if not Path(path).is_file():
        return [], 'STORE_ABSENT'
    try:
        connection = _open(path)
        try:
            # Only already fetched official bodies. No full event/call history.
            rows = connection.execute("SELECT payload FROM news_decision_inputs WHERE json_extract(payload,'$.source_kind')='DISCLOSURE' AND json_extract(payload,'$.content_scope')='original_document'").fetchall()
        finally:
            connection.close()
        result = []
        for row in rows:
            value = json.loads(row[0])
            observed = _time(value.get('observed_at'))
            if observed is not None and observed <= now and value.get('body_verified') is True and value.get('text'):
                result.append(value)
        return result, 'OK'
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return [], 'UNAVAILABLE'


def collect_decision_context(root, item, *, as_of, universe, cached_documents=None):
    """Recent source timeline: 12 news + 6 filings, bounded text, explicit gaps.

    Exact as-of filtering happens before the final item cap. Revised source
    bodies are never backdated to their original collection timestamp.
    """
    root, now = Path(root), instant(as_of)
    day = now.astimezone(timezone.utc).date()
    names = universe[item['symbol']]
    expanded = expand_query_terms(terms=names)['expanded_terms']
    status, evidence = {}, []
    news_path = root / 'news/external_news_v4.sqlite3'
    if not names:
        ids = []
        status['news'] = 'IDENTITY_MISSING'
    elif is_etf_name(names[0]):
        # Retain the shared unique-brand ETF identity rule.
        items = collect_local_news_items(news_path, market=item['market'], symbol=item['symbol'],
            official_names=names, universe=universe, completed_session_date=day, max_items=100, status_out=status)
        ids = [c.source_label.rsplit('[', 1)[-1].rstrip(']') for c in items]
    else:
        query = EvidenceQueryV1(date_from=day-timedelta(days=30), date_to=day, terms=tuple(names),
            families=('news_raw',), limit=200)
        result = _query(query, 'news_raw', news_db=news_path,
            news_row_acceptor=lambda text: company_match(text, expanded))
        status['news'] = result['store_health']
        if result.get('counts', {}).get('truncated'):
            status['news'] = 'CANDIDATE_CAP_REACHED'
        ids = [d['document_id'] for d in result['documents']]
    if ids:
        try:
            connection = _open(news_path)
            try:
                rows = connection.execute('SELECT * FROM news_documents WHERE news_document_id IN (' + ','.join('?' for _ in ids) + ')', ids).fetchall()
            finally:
                connection.close()
            candidates = [e for row in rows if (e := _news_evidence(dict(row), now))]
            candidates.sort(key=_order, reverse=True)
            # Same title/text republication need not consume several slots.
            seen = set()
            for e in candidates:
                signature = ' '.join(e['text'].split())
                if signature not in seen:
                    evidence.append(e)
                    seen.add(signature)
                if len(evidence) == NEWS_LIMIT:
                    if len(candidates) > NEWS_LIMIT:
                        status['news_selection'] = 'RECENT_SUBSET'
                    break
        except (sqlite3.Error, OSError):
            status['news'] = 'UNAVAILABLE'
    if item['market'] == 'KR':
        query = EvidenceQueryV1(date_from=day-timedelta(days=30), date_to=day,
            symbols=(item['symbol'],), families=('opendart',), limit=200)
        family = _query(query, 'opendart', opendart_db=root / 'opendart-bulk.sqlite3')
        status['dart'] = family['store_health']
        versions = {}
        if family['disclosures']:
            try:
                versions = _filing_versions(root / 'opendart-bulk.sqlite3', [r['rcept_no'] for r in family['disclosures']], now)
            except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
                status['dart'] = 'REVISION_HISTORY_UNAVAILABLE'
        cached, cache_status = cached_documents if cached_documents is not None else _cached_disclosures(root / 'stores/news_episodes.sqlite3', now)
        status['disclosure_body_cache'] = cache_status
        count = 0
        for candidate in family['disclosures']:
            row = versions.get(candidate['rcept_no'])
            if not row or row.get('stock_code') != item['symbol']:
                continue
            available = _time(row.get('revision_available_at'))
            if available is None or available > now:
                continue
            receipt = row['rcept_no']
            bodies = [v for v in cached if v.get('stock_code') == item['symbol'] and
                str(v.get('id', '')).startswith('dart:' + receipt + ':') and v.get('source_system') != 'SEC'
                and v.get('body_verified') is True and _time(v.get('observed_at')) is not None
                and available <= instant(v['observed_at']) <= now]
            document = max(bodies, key=lambda v: instant(v['observed_at']), default=None)
            text, clipped = _excerpt(document['text'] if document else '')
            title = row['corp_name'] + ' ' + row['report_nm']
            evidence.append(dict(id='local-dart-' + receipt, kind='disclosure',
                text=title + ('\n' + text if text else '\n공시 목록만 확보. 본문 내용과 현재 진행 상태는 미확인.'),
                available_at=max(available, instant(document['observed_at'])).isoformat() if document else available.isoformat(),
                source='https://dart.fss.or.kr/dsaf001/main.do?rcpNo=' + receipt,
                metadata=dict(title=title, receipt=receipt, published_date=row['rcept_dt'][:4]+'-'+row['rcept_dt'][4:6]+'-'+row['rcept_dt'][6:8],
                    first_seen_at=row['first_observed_at'], content_extent='body_excerpt' if document else 'listing_only',
                    listing_revision_at=available.isoformat(),
                    excerpt_truncated=clipped, original_text_chars=len(document['text']) if document else 0,
                    body_observed_at=document['observed_at'] if document else None,
                    source_document_sha256=document.get('source_document_sha256') if document else None)))
            count += 1
            if count == DISCLOSURE_LIMIT:
                if len(family['disclosures']) > count:
                    status['dart_selection'] = 'RECENT_SUBSET'
                break
    # Text is budgeted newest-first but supplied oldest-first for followup
    # comparison. Older omitted material stays an explicit coverage limitation.
    evidence.sort(key=_order, reverse=True)
    selected, chars = [], 0
    for e in evidence:
        if chars + len(e['text']) > TOTAL_TEXT_LIMIT:
            status['context_text'] = 'RECENT_SUBSET'
            continue
        selected.append(e)
        chars += len(e['text'])
    return list(reversed(selected)), status


def replace_local_context(previous, context, status):
    """A temporary read failure does not discard an existing cached excerpt."""
    import re
    result = []
    available = {'OK', 'CANDIDATE_CAP_REACHED', 'CAP_EXCEEDED'}
    for e in previous:
        if not re.fullmatch(r'local-(?:[a-f0-9]{16}|dart-[0-9]{14})', e['id']):
            result.append(e)
        elif status.get('dart' if e['kind'] == 'disclosure' else 'news') not in available:
            result.append(dict(e, metadata=dict(e.get('metadata') or {},
                cached_fallback=True, refresh_status=status.get('dart' if e['kind'] == 'disclosure' else 'news', 'UNAVAILABLE'))))
    return result + context
