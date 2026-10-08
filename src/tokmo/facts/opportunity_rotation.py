"""Account-free Toss reads using the existing request builders and parsers.

The caller injects existing GET transports, an already acquired bearer and an
explicit cumulative call budget. No authentication, retries, or model calls.
"""
from datetime import date, timedelta

from tokmo.facts.toss_reads import TossReadOperation as Op, build_read_request, query_read
from tokmo.facts.toss_prices import query_current_prices
from tokmo.values import MISSING
from tokmo.values.opportunity_rotation import instant, number, symbol, validate_session, final_bar_at


def valid_daily(row, code, now):
    """Completed official history, including an explicitly terminal empty page."""
    try:
        bars = row['bars']
        completed = date.fromisoformat(row['completed_session'])
        dates = [date.fromisoformat(b['date']).isoformat() for b in bars]
        return (isinstance(bars, list) and row['symbol']==code and row['market']=='KR' and row['currency']=='KRW'
            and row['adjusted'] is True and row['source']=='Toss STOCK_CANDLES 1d adjusted'
            and (bool(bars) or completed<=instant(row['observed_at']).date())
            and instant(row['observed_at'])<=instant(now)
            and dates==sorted(set(dates)) and (not dates or dates[-1]==completed.isoformat())
            and (len(bars)>=200 or row['exhausted'] is True)
            and all(number(b['volume'])>=0 and
                0<number(b['low'])<=min(number(b['open']),number(b['close']))<=
                max(number(b['open']),number(b['close']))<=number(b['high']) for b in bars))
    except (KeyError,ValueError,TypeError,IndexError):
        return False


def daily_through(row, code, completed, now):
    """Reuse a verified cache without leaking later bars or changing observation time."""
    if not valid_daily(row,code,now) or row['completed_session']<completed.isoformat():
        return None
    bars = [b for b in row['bars'] if b['date']<=completed.isoformat()]
    if (bars and bars[-1]['date']==completed.isoformat()) or (not bars and row['exhausted'] is True):
        return dict(row,bars=bars,completed_session=completed.isoformat())
    return None


def unavailable_history(candidate, completed):
    """Session-scoped source evidence, never an inference from an empty bars field.

    Capture validates observation time against its wall clock. Discovery and
    quote freshness remain separate; old packets without provenance fail closed.
    """
    try:
        proof = candidate['history_provenance']
        return (candidate['bars']==[] and proof['completed_session']==completed
            and candidate['history_source']==proof['source']
            and candidate['history_observed_at']==proof['observed_at']
            and valid_daily(dict(proof,bars=[]),candidate['symbol'],proof['observed_at']))
    except (KeyError,TypeError):
        return False


def _session(day):
    integrated = day.integrated
    if integrated is MISSING:
        raise ValueError('calendar integrated sessions missing')
    if integrated is None:
        return None
    regular = getattr(integrated, 'regular_market', None)
    if regular is MISSING:
        raise ValueError('calendar regular session missing')
    if regular is None:
        return None
    result = dict(date=day.date.isoformat(), open=regular.start_time.isoformat(),
                  close=regular.end_time.isoformat(), source='Toss KR_MARKET_CALENDAR')
    validate_session(result)
    return result


def _bar(code, candle):
    return dict(symbol=code, at=instant(candle.timestamp).isoformat(), open=candle.open_price,
        high=candle.high_price, low=candle.low_price, close=candle.close_price, volume=candle.volume,
        currency=candle.currency, interval='1m', adjusted=False)


class SourceUnavailable(ValueError):
    """Carry only the shared transport's closed code and Retry-After."""
    def __init__(self, result):
        self.code, self.retry_after = result.code, result.retry_after
        super().__init__('Toss source unavailable: ' + self.code)


class TossOpportunitySource:
    def __init__(self, *, token, transport, price_transport, clock, max_calls, pace):
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError('explicit positive source budget required')
        self.token, self.transport, self.price_transport = token, transport, price_transport
        self.clock, self.pace, self.max_calls = clock, pace, max_calls
        self.calls, self.failed = [], False

    def _request(self, operation, **kwargs):
        if self.failed:
            raise ValueError('source stopped after failure; no retry')
        if len(self.calls) >= self.max_calls:
            self.failed = True
            raise ValueError('source budget exhausted; incomplete result not usable')
        self.pace()
        record = dict(operation=operation, at=self.clock().isoformat(),
                      params={k: v.isoformat() if hasattr(v,'isoformat') else v for k,v in kwargs.items()})
        self.calls.append(record)
        if operation == 'CURRENT_PRICES':
            result = query_current_prices(token=self.token, transport=self.price_transport, clock=self.clock, **kwargs)
        else:
            request = build_read_request(Op(operation), token=self.token, **kwargs)
            result = query_read(request, transport=self.transport, clock=self.clock)
        record['status'] = result.code
        if result.code != 'success':
            self.failed = True
            raise SourceUnavailable(result)
        return result.facts if operation == 'CURRENT_PRICES' else result.value

    def minutes(self, code, start, end):
        """Inclusive cursor is passed verbatim, deduplicated, and must progress.

        Read through start, not merely the latest quote. Even a short page may
        have a nextBefore; only a reached lower bound or explicit null ends it.
        """
        symbol(code); start, end = instant(start), instant(end)
        before, rows, cursors = end, {}, set()
        while True:
            page = self._request('STOCK_CANDLES', symbol=code, interval='1m', count=200, adjusted=False, before=before)
            timestamps = []
            for candle in page.candles:
                ts = instant(candle.timestamp)
                if ts > before:
                    raise ValueError('candle exceeds inclusive before')
                timestamps.append(ts)
                if start < ts <= end:
                    value = _bar(code, candle)
                    if ts in rows and rows[ts] != value:
                        raise ValueError('pagination conflicting candle')
                    rows[ts] = value
            if timestamps and min(timestamps) <= start or page.next_before is None:
                return [rows[t] for t in sorted(rows)]
            if page.next_before is MISSING:
                raise ValueError('pagination terminal metadata missing')
            cursor = instant(page.next_before)
            if cursor in cursors or cursor > before or not timestamps or cursor > min(timestamps):
                raise ValueError('pagination failed to progress')
            cursors.add(cursor); before = cursor

    def daily(self, code, completed, *, count=200):
        page = self._request('STOCK_CANDLES', symbol=code, interval='1d', count=count, adjusted=True,
            before=instant(completed.isoformat()+'T23:59:59+09:00'))
        bars = {}
        for c in page.candles:
            d = instant(c.timestamp).date()
            if d > completed or c.currency!='KRW':
                raise ValueError('daily candle after completed session or wrong currency')
            b = dict(date=d.isoformat(),open=c.open_price,high=c.high_price,low=c.low_price,
                     close=c.close_price,volume=c.volume)
            if d in bars and bars[d]!=b:
                raise ValueError('conflicting daily candle')
            bars[d]=b
        return dict(symbol=code,market='KR',currency='KRW',adjusted=True,
            completed_session=completed.isoformat(),bars=[bars[d] for d in sorted(bars)],
            exhausted=page.next_before is None,source='Toss STOCK_CANDLES 1d adjusted',
            observed_at=instant(self.clock()).isoformat())

    def discover(self, *, held_symbols):
        now = instant(self.clock())
        calendar = self._request('KR_MARKET_CALENDAR', date=now.date().isoformat())
        today = _session(calendar.today)
        selected = today or _session(calendar.next_business_day)
        if selected is None:
            raise ValueError('official upcoming regular session unavailable')
        completed = calendar.today.date if today and now >= instant(today['close']) else calendar.previous_business_day.date
        entries, ranked_at = {}, []
        for kind in ('MARKET_TRADING_AMOUNT','MARKET_TRADING_VOLUME'):
            ranking = self._request('RANKINGS', type=kind, marketCountry='KR', duration='1d', count=100,
                                    excludeInvestmentCaution=False)
            ranked_at.append(dict(type=kind, at=ranking.ranked_at.isoformat() if ranking.ranked_at not in (None,MISSING) else None))
            for item in ranking.rankings:
                symbol(item.symbol)
                if item.currency != 'KRW':
                    raise ValueError('KR ranking currency mismatch')
                entries.setdefault(item.symbol, []).append(dict(type=kind, rank=item.rank,
                    amount=item.trading_amount, volume=item.trading_volume))
        symbols = sorted(set(entries) | set(held_symbols))
        if not symbols:
            raise ValueError('Toss ranking empty; no discovery facts')
        stocks, quotes = {}, {}
        for offset in range(0,len(symbols),200):
            batch = tuple(symbols[offset:offset+200])
            stocks.update({s.symbol:s for s in self._request('STOCKS', symbols=batch)})
            for q in self._request('CURRENT_PRICES',symbols=batch):
                if q.currency_raw!='KRW':
                    raise ValueError('KR quote currency mismatch')
                quotes[q.symbol] = dict(price=q.last_price,at=q.timestamp.isoformat()) if q.timestamp else None
        candidates, exclusions = [], []
        for code in symbols:
            stock = stocks[code]
            detail = stock.korean_market_detail
            eligible = (stock.market in ('KOSPI','KOSDAQ') and stock.security_type=='STOCK' and stock.is_common_share
                and stock.status=='ACTIVE' and stock.currency=='KRW' and detail not in (MISSING,None)
                and not detail.liquidation_trading and not detail.krx_trading_suspended)
            if not eligible:
                exclusions.append(dict(symbol=code, reason='KR 활성 보통주/정상 정규장 거래 요건 미충족 또는 메타데이터 결측',
                                       market=stock.market,security_type=stock.security_type,status=stock.status))
                if code not in held_symbols:
                    continue
            daily = self.daily(code, completed)
            history, missing = daily['bars'], ['news','disclosure','fundamentals','sector_exposure']
            provenance = {k:daily[k] for k in ('symbol','market','currency','adjusted','completed_session',
                'exhausted','source','observed_at')} if valid_daily(daily,code,self.clock()) else None
            if not history or history[-1]['date']!=completed.isoformat():
                missing.append('completed_ohlcv'); history=[]
            if not history and provenance and daily['bars']==[]:
                missing.append('completed_ohlcv_source_exhausted')
            quote = quotes.get(code)
            if quote and (instant(quote['at'])>instant(self.clock()) or instant(quote['at']).date()<completed or number(quote['price'])<=0):
                missing.append('quote_timestamp_or_price'); quote=None
            if quote is None:
                missing.append('quote')
            candidates.append(dict(symbol=code,name=stock.name,eligible=eligible,ranking=entries.get(code,[]),
                quote=quote,bars=history,history_source=daily['source'],history_observed_at=daily['observed_at'],history_provenance=provenance,missing=missing,evidence=[],context_status=dict(news='NOT_CONNECTED',disclosure='NOT_CONNECTED'),
                metadata=dict(market=stock.market,security_type=stock.security_type,common_share=stock.is_common_share)))
        return dict(as_of=instant(self.clock()).isoformat(),session=selected,completed_session=completed.isoformat(),source='Toss read-only public market facts',
            universe='거래대금/거래량 1일 순위 각각 provider 최대 100의 합집합과 PAPER 보유; 로컬 후보 수 제한 없음; 전체 시장은 아님',
            rankings_as_of=ranked_at,candidates=candidates,exclusions=exclusions)

    def capture(self, *, symbols, start, end):
        start, end = instant(start), instant(end)
        sessions, closed, bars, coverage = [], [], [], []
        day = start.date()
        while day <= end.date():
            calendar = self._request('KR_MARKET_CALENDAR', date=day.isoformat())
            if calendar.today.date != day:
                raise ValueError('calendar date mismatch')
            session = _session(calendar.today)
            if session is None:
                closed.append(day.isoformat())
            else:
                sessions.append(session)
                opening, closing = validate_session(session)
                lo, hi = max(start.replace(second=0,microsecond=0),opening), min(end.replace(second=0,microsecond=0),final_bar_at(session))
                if lo < hi:
                    for code in sorted(set(symbols)):
                        rows = self.minutes(code,lo,hi)
                        bars.extend(rows)
                        expected = int((hi.replace(second=0,microsecond=0)-lo).total_seconds() // 60)
                        coverage.append(dict(symbol=code,start=lo.isoformat(),end=hi.isoformat(),
                                             complete=len(rows)==expected))
            day += timedelta(days=1)
        valuation = min(end.replace(second=0,microsecond=0), final_bar_at(sessions[-1])) if sessions else end.replace(second=0,microsecond=0)
        marks = {b['symbol']:dict(price=b['close'],at=b['at']) for b in bars if instant(b['at'])==valuation and number(b['volume'])>0}
        return dict(as_of=end.isoformat(),sessions=sessions,closed_dates=closed,bars=bars,coverage=coverage,
                    valuation_at=valuation.isoformat(),marks=marks,source='Toss STOCK_CANDLES 1m unadjusted; official regular sessions')
