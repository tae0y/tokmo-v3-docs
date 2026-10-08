"""Price observations with explicit clocks and comparison periods."""
from decimal import Decimal


def price_context(instrument):
    bars = instrument.bars
    if not bars or not all(b['adjusted'] for b in bars):
        return {'status': 'ADJUSTED_BARS_UNAVAILABLE'}
    last = bars[-1]
    previous = bars[-2] if len(bars) > 1 else None
    def change(value, prior):
        return value / prior - 1 if prior and prior > 0 else None
    session = {k: last[k] for k in ('date', 'available_at', 'close', 'volume')}
    session.update(previous_date=previous['date'] if previous else None,
        previous_close=previous['close'] if previous else None,
        previous_volume=previous['volume'] if previous else None,
        return_1=change(last['close'], previous['close']) if previous else None,
        volume_return_1=change(last['volume'], previous['volume']) if previous else None)
    averages = {}
    periods = {}
    for window in (5, 20, 60, 120):
        if len(bars) > window:
            periods['ret_' + str(window)] = dict(from_date=bars[-window-1]['date'],
                to_date=last['date'], basis='완료 수정 일봉 종가 수익률')
    for window in (20, 60, 120):
        if len(bars) >= window:
            mean = sum(b['close'] for b in bars[-window:]) / Decimal(window)
            averages[str(window)] = dict(value=mean, from_date=bars[-window]['date'], to_date=last['date'],
                completed_close_gap=change(last['close'], mean),
                latest_quote_gap=change(instrument.price, mean) if instrument.price is not None else None)
    if len(bars) >= 40:
        periods['volume_change_20'] = dict(previous_from=bars[-40]['date'], previous_to=bars[-21]['date'],
            recent_from=bars[-20]['date'], recent_to=last['date'], basis='최근 20거래일 평균 / 그 이전 20거래일 평균 - 1. 전일 대비가 아님.')
    periods['max_drawdown'] = dict(from_date=bars[-200:][0]['date'], to_date=last['date'],
        basis='기존 지표의 최근 최대 200개 완료 일봉 종가 기준 최대 낙폭. 현재 고점 대비 하락률과 다름.')
    # Q5: summarize every selected bar without expanding the prompt into raw bars.
    peak, drawdown = bars[0]['close'], Decimal(0)
    for bar in bars:
        peak = max(peak, bar['close'])
        drawdown = min(drawdown, bar['close'] / peak - 1)
    history = dict(count=len(bars), from_date=bars[0]['date'], to_date=last['date'],
        return_total=change(last['close'], bars[0]['close']), max_drawdown=drawdown,
        low=min(b['low'] for b in bars), high=max(b['high'] for b in bars),
        basis='선택한 전체 완료 수정 일봉 구간. 개별 지표의 계산 기간과 별개.')
    return dict(status='AVAILABLE', completed_session=session, moving_averages=averages, feature_periods=periods,
        history=history,
        latest_quote=dict(price=instrument.price, at=instrument.price_at),
        interpretation='완료 종가로 계산한 추세와 이후 현재가를 구분. 단기 가격 반등, 중기 추세, 조건부 진입안 충족 여부는 각각 판단.')


def evidence_coverage(evidence, metrics):
    """Report missing inputs rather than silently filling them with inference."""
    news = [e for e in evidence if e['kind'] == 'news']
    filings = [e for e in evidence if e['kind'] == 'disclosure']
    financial = {key: value for key, value in metrics.items() if key in {
        'revenue', 'operating_profit', 'net_income', 'equity', 'assets', 'liabilities',
        'revenue_growth', 'operating_profit_growth', 'net_income_growth'}}
    return dict(news_items=len(news), disclosure_items=len(filings),
        body_excerpt_ids=[e['id'] for e in news + filings if e.get('metadata', {}).get('content_extent') == 'body_excerpt'],
        shallow_source_ids=[e['id'] for e in news + filings if e.get('metadata', {}).get('content_extent') in {'title_only', 'listing_only', 'source_summary'}],
        financial_metrics='AVAILABLE' if financial else 'NOT_PROVIDED',
        financial_periods=sorted({str(m.get('period')) for m in financial.values()}),
        other_metric_keys=sorted(set(metrics) - set(financial)),
        current_event_status='SOURCE_COMPARISON_REQUIRED',
        note='수집된 일부 자료의 시간순 목록. 예고의 실제 실행·종결, 기사에 없는 실적 영향은 후속 원문 없이는 확정할 수 없음.')
