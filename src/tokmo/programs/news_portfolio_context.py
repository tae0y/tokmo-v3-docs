"""Small read-only NEWS view: local identities and dated holdings/quote facts.

No broker authentication, model, new store or automatic pension refresh. Prices
are optional display facts; their absence never decides whether news matters.
"""
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

from tokmo.records.news_decision_inputs import read_portfolio_context
from tokmo.records.news_decision_store import timestamp
from tokmo.records.playbook_identity import _front_matter
from tokmo.values.instrument_display import display_or_symbol
from tokmo.programs.price_reference import read_cache, read_calendar, comparison_reference, quote_session


def number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _json(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def read_catalog(project, supplement, *, at):
    """Use existing captured identities and playbook identity fields only."""
    catalog = {}
    for scope in ('personal', 'retirement'):
        for market in ('KR', 'US'):
            for path in sorted((Path(project) / 'playbooks' / scope / market).glob('*.md')):
                try:
                    front = _front_matter(path.read_text())
                    code = re.search(r'^instrument_code:\s*"([^"\n]+)"', front, re.M)
                    name = re.search(r'^instrument_name:\s*"([^"\n]+)"', front, re.M)
                    if not code or not name or code[1] != path.stem:
                        continue
                    catalog[market + ':' + code[1]] = dict(key=market + ':' + code[1], symbol=code[1],
                        market=market, name=name[1], kind='unknown', identity_source='playbook_identity')
                except (OSError, UnicodeError):
                    continue
    # The capture's names/types come from the broker. Do not replace them
    # with thematic guesses or load a capture from after the replay instant.
    try:
        captured = timestamp(supplement['captured_at'])
    except (KeyError, TypeError, ValueError):
        captured = None
    if captured is not None and captured <= timestamp(at):
        for row in supplement.get('instruments', []):
            if row.get('market') not in ('KR', 'US') or not row.get('symbol'):
                continue
            key = row['market'] + ':' + row['symbol']
            if row.get('name') and row['name'] != row['symbol']:
                catalog[key] = dict(key=key, symbol=row['symbol'], market=row['market'], name=row['name'],
                    kind=row.get('kind', 'unknown'), identity_source='captured_instrument',
                    identity_at=supplement['captured_at'])
    return catalog


def read_quotes(path, *, at):
    if not Path(path).is_file():
        return {}
    try:
        connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute("SELECT symbol,last_price,currency_raw,provider_timestamp,fetched_at "
                "FROM latest_quotes WHERE source='toss-current-price-v1'").fetchall()
        finally:
            connection.close()
        result = {}
        for row in rows:
            value = dict(row)
            observed = value['provider_timestamp'] or value['fetched_at']
            if timestamp(value['fetched_at']) > timestamp(at) or timestamp(observed) > timestamp(at):
                continue
            price = number(value['last_price'])
            if price is None or price <= 0:
                continue
            result[value['symbol']] = dict(price=str(price), currency=value['currency_raw'],
                price_at=observed, fetched_at=value['fetched_at'],
                age_seconds=max(0, int((timestamp(at) - timestamp(observed)).total_seconds())))
        return result
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return {}


def read_news_portfolio(root, project, *, at, followup=True, market_observations=False):
    root = Path(root)
    result = read_portfolio_context(root / 'stores/current_holdings.json', at,
        followup_path=root / 'private/investment-followup.sqlite3' if followup else None)
    supplement = _json(root / 'ops/quant-inputs/latest.json')
    catalog = read_catalog(project, supplement, at=at)
    result['instruments'] = list(catalog.values())
    holdings = result.get('holdings', [])
    for row in holdings:
        key = str(row.get('market_raw')) + ':' + str(row.get('symbol'))
        identity = catalog.get(key, {})
        row.update(key=key, name=identity.get('name'), kind=identity.get('kind', 'unknown'))
    # A pension statement does not expire every five minutes. Preserve the
    # user's file-based quantities and disclose its own date independently.
    registry = root / 'stores/pension_registry.sqlite3'
    pension_at = None
    if registry.is_file():
        try:
            c = sqlite3.connect(registry.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
            try:
                row = c.execute('SELECT r.source_as_of FROM pension_revisions r JOIN pension_active_pointer p '
                    'ON p.active_revision_id=r.revision_id').fetchone()
                pension_at = row[0] if row and timestamp(row[0]) <= timestamp(at) else None
            finally:
                c.close()
        except (sqlite3.Error, ValueError, OSError):
            pass
    for row in holdings:
        row['observed_at'] = pension_at if row.get('scope') == 'retirement' else result.get('observed_at')
        row['quantity_basis'] = 'USER_FILE' if row.get('scope') == 'retirement' else 'BROKER_CAPTURE'
    from tokmo.programs.etf_context import attach_news_etf_context
    attach_news_etf_context(root, result, at=at)
    quotes = read_quotes(root / 'stores/latest_quotes.sqlite3', at=at)
    # Rendering uses this optional, deterministic data. Model prompts receive
    # identity/holdings only, so refreshed prices cannot become a news delta.
    references = read_cache(root)
    result['_market_context'] = dict(quotes=quotes, instruments=supplement.get('instruments', []),
        captured_at=supplement.get('captured_at'), references=references.get('instruments', {}),
        calendar=read_calendar(root, at=at, cached=references))
    if market_observations:
        from tokmo.records.news_market_observations import read_market_observations
        result['_review_market_observations'] = read_market_observations(
            root / 'ops/market_regime_shadow.sqlite3', catalog, at=at)
    return result


def price_lines(keys, context, *, at):
    """Compact prices and holding P&L; never total-account or intraday P&L guesses."""
    market = context.get('_market_context') or {}
    quotes = market.get('quotes', {})
    catalog = {row['key']: row for row in context.get('instruments', [])}
    histories = {row['market'] + ':' + row['symbol']: row for row in market.get('instruments', [])}
    lines = []
    def money(value, currency):
        sign = '-' if value < 0 else ''
        value = abs(value)
        return sign + (f'{value:,.0f}원' if currency == 'KRW' else f'${value:,.2f}' if currency == 'USD' else f'{value:,.2f} {currency}')
    for key in list(dict.fromkeys(keys))[:2]:
        identity = catalog.get(key)
        if not identity:
            continue
        quote = quotes.get(identity['symbol'])
        if not quote or quote.get('age_seconds', 999999) > 4 * 86400:
            continue
        price, currency = number(quote['price']), quote['currency']
        if price is None or price <= 0 or currency not in ('KRW', 'USD'):
            continue
        when = timestamp(quote['price_at']).astimezone(ZoneInfo('Asia/Seoul'))
        label = display_or_symbol(identity['market'], identity['symbol'], identity['name'])
        line = money(price, currency)
        history = histories.get(key, {})
        bars = []
        if market.get('captured_at') and timestamp(market['captured_at']) <= timestamp(at):
            bars = history.get('bars', [])
        reference = market.get('references', {}).get(key, {})
        if reference.get('captured_at') and timestamp(reference['captured_at']) <= timestamp(at):
            by_date = {b['date']: b for b in bars}
            by_date.update({b['date']: b for b in reference.get('bars', [])})
            bars = list(by_date.values())
        calendar = market.get('calendar', {})
        basis = comparison_reference(market=identity['market'], price_at=quote['price_at'],
            currency=currency, bars=bars, calendar=calendar, at=at)
        change_basis = ''
        if basis['bar']:
            previous = basis['bar']
            close = number(previous.get('close'))
            if close and close > 0:
                line += f" / {(price / close - 1) * 100:+.1f}%"
                if basis['status'] != 'OK':
                    change_basis = f"등락은 {previous['date'][5:]} 종가 대비 · " + (
                        f"직전 거래일 {basis['previous_date'][5:]} 자료 미갱신" if basis['previous_date']
                        else '직전 거래일 확인 필요')
        elif basis['previous_date']:
            change_basis = f"등락률 미확인 · 직전 거래일 {basis['previous_date'][5:]} 종가 미갱신"
        else:
            change_basis = '등락률 미확인 · 거래일 자료 확인 필요'
        if lines:
            lines.append('')
        lines.append(label)
        lines.append(line)
        lines.append(f'{when:%m-%d %H:%M} 기준')
        for holding in context.get('holdings', []):
            if holding.get('key') != key or holding.get('currency') != currency:
                continue
            qty, avg = number(holding.get('quantity')), number(holding.get('avg_price'))
            if qty is None or qty <= 0:
                continue
            detail = f"보유 {qty:g}주"
            if avg is not None and avg > 0:
                detail += f' · 평단가 {money(avg, currency)}'
            lines.append(detail)
            if avg is not None and avg > 0:
                lines.append(f'수익 {money(qty * (price - avg), currency)} ({(price / avg - 1) * 100:+.1f}%)')
            peers = [h for h in context.get('holdings', []) if h.get('account') == holding.get('account')
                and h.get('currency') == currency and number(h.get('quantity')) is not None and number(h['quantity']) > 0]
            total = Decimal(0)
            for peer in peers:
                q = quotes.get(peer['symbol'])
                if not q or q.get('currency') != currency or q.get('age_seconds', 999999) > 4 * 86400:
                    total = None
                    break
                total += number(peer['quantity']) * number(q['price'])
            if total and total > 0:
                scope = '퇴직연금' if holding.get('scope') == 'retirement' else '국내' if currency == 'KRW' else '미국'
                lines.append(f'{scope} 보유종목 중 {qty * price / total * 100:.1f}%')
            if holding.get('scope') == 'retirement' and holding.get('observed_at'):
                lines.append('보유 기준 ' + timestamp(holding['observed_at']).astimezone(ZoneInfo('Asia/Seoul')).strftime('%m-%d'))
        if change_basis:
            lines.append(change_basis)
        current_session = quote_session(calendar, identity['market'], at)
        if (current_session['trade_date'] and basis['trade_date']
                and current_session['trade_date'] > basis['trade_date']):
            lines.append('현재가 갱신 지연 · 이전 거래일 시세')
    return lines
