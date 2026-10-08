"""Read existing stores and playbooks into the investment decision contract."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import json
import re
import sqlite3

from tokmo.programs.quant_decision_calculations import plain
from tokmo.records.current_holdings_store import CurrentHoldingsStore
from tokmo.records.latest_quotes import LatestQuoteStore, LatestQuoteStoreReadError
from tokmo.values.current_holdings import classify_market
from tokmo.values.quant_decisions import instant


def pension_source_date(root, *, at):
    """The composite refresh is not a new pension statement."""
    path=Path(root)/'stores/pension_registry.sqlite3'
    if not path.is_file():return None
    try:
        connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            row=connection.execute("SELECT r.source_as_of FROM pension_revisions r JOIN pension_active_pointer p ON p.active_revision_id=r.revision_id WHERE r.state='ACTIVE'").fetchone()
        finally:connection.close()
        if row:
            from tokmo.values.quant_decisions import instant
            source=instant(row[0])
            if source<=at:return source.isoformat()
    except (sqlite3.Error,ValueError,TypeError):pass
    return None


def read_user_choices(root, *, at):
    path=Path(root)/'private/investment-followup.sqlite3'
    if not path.is_file():return []
    connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        from tokmo.values.quant_decisions import instant
        choices=[json.loads(r[0]) for r in connection.execute('SELECT payload FROM investment_choices ORDER BY rowid')]
        choices=[c for c in choices if instant(c['recorded_at'])<=at]
        replaced={c.get('supersedes') for c in choices}
        from tokmo.records.investment_followup import plan_active, plan_state
        active=[c for c in choices if c['id'] not in replaced and plan_active(connection,c,at=at)]
        return [dict(c,plan_state=plan_state(connection,c,at=at)) if c.get('plan') else c for c in active]
    finally:connection.close()


def playbook_evidence(path, *, as_of):
    """Preserve voices and dates; a migration note is metadata, not a thesis."""
    path = Path(path)
    if not path.is_file():
        return []
    text = path.read_text()
    migrated = "마이그레이션 메모" in text or "V2 플레이북 이관" in text
    evidence = []
    # Current documents are known NOW. Historical replay supplies dated
    # evidence in its fixture, never reads today's file as yesterday's fact.
    def content(marker):
        match = re.search(r"<!-- " + marker + r"_START -->(.*?)<!-- " + marker + r"_END -->", text, re.S)
        if not match:
            return ""
        value = match.group(1)
        value = re.sub(r"\*\*마이그레이션 메모:.*?(?:\n\s*\n|$)", "", value, flags=re.S)
        return "\n".join(line for line in value.splitlines()
                          if line.strip() and not line.lstrip().startswith(("#", ">", "- `source", "- `source_snapshot")))[:5000]
    for marker, kind, label in (("USER_THESIS", "legacy_thesis" if migrated else "user_thesis",
                                "이관 플레이북(화자 혼재 가능)" if migrated else "사용자 보유 가설"),
                               ("HERMES_CURRENT", "ai_interpretation", "플레이북 AI 해석")):
        value = content(marker)
        if value:
            evidence.append({"id": marker.lower(), "kind": kind, "text": value,
                "available_at": as_of.isoformat(), "source": label,
                "metadata": {"time_basis": "current_document_read", "published_at": None}})
    matches = list(re.finditer(r'<a id="entry-([^\"]+)"></a>(.*?)(?=<a id="entry-|\Z)', text, re.S))
    for match in matches[:5]:
        block = match.group(2)
        raw = re.search(r'<!-- USER_RAW_START[^>]+-->(.*?)<!-- USER_RAW_END', block, re.S)
        if not raw:
            continue
        event_date = re.search(r'`event_date`: `([^`]+)`', block)
        recorded = re.search(r'`recorded_date`: `([^`]+)`', block)
        value = raw.group(1).strip()
        when = event_date.group(1) if event_date else "거래일 미확인"
        provenance = "기록시점 미확인" if not recorded or recorded.group(1) == "unknown" else "기록일 " + recorded.group(1)
        evidence.append({"id": "trade-" + match.group(1), "kind": "trade_reason", "text": f"{when} · {value}"[:1500],
            "available_at": as_of.isoformat(), "source": "플레이북 거래 사유 · " + provenance,
            "metadata": {"time_basis": "current_document_read", "event_date": when,
                         "recorded_date": recorded.group(1) if recorded else None}})
    return evidence


def load_local_snapshot(project_root, data_root, *, as_of, supplement=None, include_context=True, pension_baseline=None, pension_analysis=None, historical_pension=False):
    """Read-only; supplement is an explicit capture or user analysis input.

    No DB constructor that initializes/migrates is used. Missing cash remains
    missing; a workbook is never silently treated as a whole account.
    Legacy pension paths cannot override current holdings. Applying those
    views requires an explicit historical comparison, never runtime config.
    """
    project, root = Path(project_root), Path(data_root)
    now = instant(as_of)
    state = CurrentHoldingsStore(root / "stores/current_holdings.json", clock=lambda: now).read()
    if state is None:
        raise ValueError("현재 보유 스냅샷이 없습니다")
    instruments, positions, limitations = {}, [], []
    if (pension_analysis or pension_baseline) and not historical_pension:
        limitations.append('과거 연금 분석 자료 대신 저장된 현재 보유를 사용합니다. 과거 자료 적용은 명시적 비교 분석에서만 가능합니다.')
    pension_at=pension_source_date(root,at=now)
    try:user_choices=read_user_choices(root,at=now)
    except (sqlite3.Error,ValueError,KeyError,TypeError):
        user_choices=[]
        limitations.append('선택 기록을 읽지 못했습니다. 선택 변화 없음으로 해석하지 않습니다.')
    # Watchlists and explicit comparison/fundamental supplements are routes
    # into discovery independent of turnover ranking.
    for market in ('KR', 'US'):
        path = project / 'watchlists' / (market + '.json')
        if path.is_file():
            for row in json.loads(path.read_text()).get('entries', []):
                if row.get('status') == 'active':
                    instruments[f"{market}:{row['symbol']}"] = dict(symbol=row['symbol'], market=market,
                        name=row.get('name', row['symbol']), currency='KRW' if market == 'KR' else 'USD',
                        kind='stock', sources=['관심 종목'])
    if include_context:
        from tokmo.records.quant_report_locator import locate_latest_verified_reports
        located = locate_latest_verified_reports(root / "ops/quant-g2-preview")
    else:
        located = {}
    for market, item in located.items():
        if not item:
            continue
        for candidate in item.report.cards:
            key = f"{market}:{candidate.symbol}"
            instruments[key] = {"symbol": candidate.symbol, "market": market,
                "name": candidate.name, "currency": "KRW" if market == "KR" else "USD",
                "kind": "stock", "sources": ["기존 후보 보고서:" + item.completed_session_date]}
    for account in state.accounts:
        scope = "retirement" if account.account_scope.startswith("pension-excel-") else "personal"
        for holding in account.holdings:
            market_value = classify_market(holding.market_raw, holding.market_field)
            if market_value is None:
                limitations.append("분류하지 못한 보유자산이 있어 계좌 평가 범위가 불완전합니다.")
                continue
            market = market_value.value
            key = f"{market}:{holding.symbol}"
            instruments.setdefault(key, {"symbol": holding.symbol, "market": market, "currency": holding.currency,
                "name": holding.symbol, "kind": "etf" if scope == "retirement" else "stock", "sources": ["현재 보유 스냅샷"]})
            positions.append({"account": account.account_scope, "scope": scope, "key": key,
                "quantity": holding.quantity, "average_cost": holding.avg_price,
                "observed_at": pension_at if scope=='retirement' and pension_at else state.captured_at.isoformat(),
                "captured_at":state.captured_at.isoformat(),
                "source_status":('PENSION_SOURCE_DATED' if pension_at else 'PENSION_SOURCE_UNKNOWN') if scope=='retirement' else 'BROKER_CAPTURE',
                "sellable": None})
    # A sale removes a holding, not its reentry watch. Retain the instrument
    # as an input route even when it disappears from holdings and watchlists.
    for choice in user_choices:
        for key in choice['interpretation']['keys']:
            market,symbol=key.split(':',1)
            instruments.setdefault(key,dict(symbol=symbol,market=market,currency='KRW' if market=='KR' else 'USD',
                name=symbol,kind='etf' if choice['interpretation']['scope']=='retirement' else 'stock',sources=['사용자 선택 후속 추적']))
    pension_view=None
    account_context=[dict(account=a.account_scope,scope='retirement' if a.account_scope.startswith('pension-excel-') else 'personal') for a in state.accounts]
    if historical_pension and pension_analysis and pension_baseline:
        raise ValueError('연금 분석 자료와 단일 과거 기준을 동시에 지정할 수 없습니다')
    if historical_pension and pension_analysis:
        from tokmo.programs.pension_analysis_sources import load_analysis
        from tokmo.programs.pension_analysis_view import apply_view
        pension_view=load_analysis(pension_analysis,as_of=now)
        adjusted=apply_view(dict(positions=positions,accounts=account_context,instruments=list(instruments.values()),as_of=now.isoformat(),limitations=limitations),pension_view)
        positions=adjusted['positions']
        instruments={i['market']+':'+i['symbol']:i for i in adjusted['instruments']}
    if historical_pension and pension_baseline:
        from tokmo.programs.pension_analysis_view import load_baseline,apply_view
        pension_view=load_baseline(pension_baseline,as_of=now)
        adjusted=apply_view(dict(positions=positions,accounts=account_context,instruments=list(instruments.values()),as_of=now.isoformat(),limitations=limitations),pension_view)
        positions=adjusted['positions']
        instruments={i['market']+':'+i['symbol']:i for i in adjusted['instruments']}
    from tokmo.programs.etf_context import connected_etf_symbols, read_etf_context
    for symbol in connected_etf_symbols(root):
        context = read_etf_context(root, symbol, at=now)
        if not any(context[k]['status'] in {'REFERENCE_ONLY','STALE','NO_DATA'} for k in ('components','nav')):
            continue
        instruments.setdefault('KR:'+symbol, dict(symbol=symbol, market='KR', currency='KRW',
            name=symbol, kind='etf', sources=['KIS ETF 조회 참고 대상 · 보유 여부 미확인']))
    quotes = LatestQuoteStore(root / "stores/latest_quotes.sqlite3", clock=lambda: now)
    for key, item in instruments.items():
        try:
            quote = quotes.read_latest(source="toss-current-price-v1", symbols=(item["symbol"],))[0]
            item.update(price=quote.last_price, price_at=(quote.timestamp or quote.fetched_at).isoformat())
        except LatestQuoteStoreReadError:
            item.update(price=None, price_at=None)
        item["evidence"] = []
        for scope in ("personal", "retirement"):
            path = project / "playbooks" / scope / item["market"] / (item["symbol"] + ".md")
            if path.is_file():
                name = re.search(r'^instrument_name:\s*"?([^"\n]+)', path.read_text(), re.M)
                if name:
                    item["name"] = name.group(1)
            item["evidence"].extend(dict(e, id=scope + ':' + e['id'], scope=scope) for e in playbook_evidence(path, as_of=now))
    data = {"schema_version": 1, "as_of": now.isoformat(), "synthetic": False, "positions": positions,
            "instruments": list(instruments.values()), "cash": [], "fx": None, "limitations": limitations,
            "accounts": [{"account": a.account_scope,
                          "scope": "retirement" if a.account_scope.startswith("pension-excel-") else "personal"}
                         for a in state.accounts]}
    data['user_choices']=user_choices
    from tokmo.programs.quant_investor_flows import read_flows
    data['investor_flows']=read_flows(root)
    if pension_view:data['pension_analysis_view']={k:v for k,v in pension_view.items() if k!='positions'}
    supplements = supplement if isinstance(supplement, (list, tuple)) else [supplement] if supplement else []
    for supplement in supplements:
        # Merge by identity; never replace the whole holdings inventory with
        # a short list of queried symbols.
        supplement = json.loads(Path(supplement).read_text()) if isinstance(supplement, (str, Path)) else supplement
        for updated in supplement.get("instruments", []):
            key = f"{updated['market']}:{updated['symbol']}"
            old = instruments.get(key, {})
            updated = dict(updated)
            # Dated pension supplements are fallbacks, never a reason to
            # roll a current quote or refreshed price history backwards.
            if (old.get('price') is not None and old.get('price_at') and updated.get('price_at')
                    and instant(updated['price_at']) < instant(old['price_at'])):
                updated.pop('price', None)
                updated.pop('price_at', None)
                updated.pop('sources', None)
            old_bar = max((b['date'] for b in old.get('bars', [])), default='')
            new_bar = max((b['date'] for b in updated.get('bars', [])), default='')
            if 'bars' in updated and old_bar > new_bar:
                updated.pop('bars')
            original_evidence = old.get("evidence", [])
            instruments[key] = dict(old, **updated)
            instruments[key]["metrics"] = dict(old.get("metrics", {}), **updated.get("metrics", {}))
            instruments[key]["evidence"] = list({e["id"]: e for e in original_evidence + updated.get("evidence", [])}.values())
        data["instruments"] = list(instruments.values())
        for name in ("cash", "fx", "extra_capital", "account_summaries", "fees", "orders", "investor_flows"):
            if name in supplement:
                data[name] = supplement[name]
        sellable = {(p["account"], p["key"]): p for p in supplement.get("positions", [])}
        for position in positions:
            updated = sellable.get((position["account"], position["key"]))
            if updated and updated.get("sellable") is not None:
                position["sellable"] = updated["sellable"]
                position['sellable_at'] = updated.get('observed_at', supplement.get('captured_at'))
    # A newly added pension ETF may have completed candles before it enters
    # the ordinary quote universe. Use that dated close for comparison only.
    for item in instruments.values():
        completed=[b for b in item.get('bars',[]) if b.get('adjusted') is True and
                   b.get('available_at') and instant(b['available_at'])<=now]
        if completed:
            bar=max(completed,key=lambda b:instant(b['available_at']))
            if (item.get('price') is not None and item.get('price_at')
                    and instant(item['price_at']) >= instant(bar['available_at'])):
                continue
            item.update(price=bar['close'],price_at=bar['available_at'])
            item.setdefault('sources',[]).append('실시간 시세 미확인 · 완료 수정 일봉 종가를 비교 기준으로 사용')
            limitations.append(item.get('name',item['symbol'])+': '+str(bar['date'])+' 종가 기준 비교입니다.')
    if include_context:
        from tokmo.programs.quant_decision_context import collect_decision_context, _cached_disclosures, replace_local_context
        cached_documents = _cached_disclosures(root / 'stores/news_episodes.sqlite3', now)
    # Identity stays market+symbol keyed until the existing per-market matcher.
    # Only stored official names are used; the matcher owns verified aliases.
    universes = {market: {} for market in ('KR', 'US')}
    for item in instruments.values():
        english = item.get('english_name')
        names = [item['name']] if item.get('name') and item['name'] != item['symbol'] else []
        if isinstance(english, str) and english.strip(): names.append(english.strip())
        universes[item['market']][item['symbol']] = tuple(dict.fromkeys(names))
    for item in (instruments.values() if include_context else ()):
        context, status = collect_decision_context(root, item, as_of=now, universe=universes[item['market']],
                                                   cached_documents=cached_documents)
        status['identity'] = 'OFFICIAL_ENGLISH_NAME' if isinstance(item.get('english_name'), str) and item['english_name'].strip() else 'ENGLISH_NAME_MISSING'
        # Captures may carry the previous projection. Replace only our generated
        # local ids, retaining separately supplied business/interview evidence.
        item['evidence'] = replace_local_context(item.get('evidence', []), context, status)
        item['sources'] = list(dict.fromkeys(item.get('sources', []) + [f'{k}:{v}' for k,v in status.items()]))
    if include_context:
        from tokmo.programs.etf_context import attach_quant_etf_context
        attach_quant_etf_context(root, list(instruments.values()), at=now,
            held_keys={p['key'] for p in positions if Decimal(str(p['quantity'])) > 0})
    if not data["cash"]:
        limitations.append("가용 자금 미조회: 종목·가격 분석은 가능하며 매수 수량은 자금 자료 연결 후 계산합니다.")
    if any(p["scope"] == "retirement" for p in positions) and not data.get("account_summaries"):
        limitations.append("퇴직연금은 연결된 ETF 기준입니다. 현금·기타자산을 포함한 전체 계좌 비중은 별도 자료가 필요합니다.")
    if not pension_view and any(p['scope']=='retirement' for p in positions):
        limitations.append('퇴직연금 보유 원천 기준: '+(pension_at or '미확인')+' · 합성 보유파일 생성 시각과 다릅니다. 이후 거래 확인 전에는 현행 잔고로 확정하지 않습니다.')
    refresh_path = root / 'ops/quant-inputs/refresh.json'
    if refresh_path.is_file():
        refresh = json.loads(refresh_path.read_text())
        expected = refresh.get('expected_sessions', {})
        if any(not expected.get(i['market']) for i in instruments.values()):
            limitations.append('거래 달력 확인 지연: 일부 시장의 최신 완료 거래일을 확인하지 못했습니다. 일봉 기준일을 확인해 해석합니다.')
        missing = [i.get('name', i['symbol']) for i in instruments.values()
                   if expected.get(i['market']) and
                   max((b['date'] for b in i.get('bars', [])), default='') < expected[i['market']]]
        if missing:
            limitations.append('분석 일봉 갱신 대기·지연: '+', '.join(missing)+'. 마지막 완료 일봉 이후의 추세가 반영되지 않았을 수 있습니다.')
    from datetime import timedelta
    if any(now-instant(c['observed_at']) > timedelta(minutes=30) for c in data['cash']):
        limitations.append('매수 가능액 갱신 지연: 30분 이상 지난 자료가 있습니다. 표시된 기준 시각 이후 입출금·주문은 반영되지 않았을 수 있습니다.')
    return data


def capture_order_info(fetch, *, accounts, positions, now, sections=('cash','fees','orders','sellable','fx')):
    """Inject a token-sharing GET reader; never creates a fresh token itself."""
    from tokmo.facts.toss_reads import TossReadOperation as Op
    result = {"cash": [], "positions": [], "fees": {}, "fx": None, "orders": [], "failures": []}
    for account in accounts:
        for currency in (("KRW", "USD") if 'cash' in sections else ()):
            try:
                value = fetch(Op.BUYING_POWER, account=account, currency=currency)
                if value.currency != currency:
                    raise ValueError("currency_mismatch")
                result["cash"].append({"account": account, "scope": "personal", "currency": currency,
                    "available": value.cash_buying_power, "observed_at": now.isoformat(),
                    "source": "Toss GET /api/v1/buying-power", "basis": "cash_buying_power"})
            except Exception as error:
                result["failures"].append({"operation": "BUYING_POWER", "currency": currency, "reason": type(error).__name__})
        try:
            for item in (fetch(Op.COMMISSIONS, account=account) if 'fees' in sections else ()):
                if item.start_date and item.start_date > now.date() or item.end_date and item.end_date < now.date():
                    continue
                result["fees"][f"{account}:{item.market_country}"] = {"rate": item.commission_rate, "observed_at": now.isoformat()}
        except Exception as error:
            result["failures"].append({"operation": "COMMISSIONS", "reason": type(error).__name__})
        from tokmo.facts.toss_reads import resolve_conditional_order_side
        for operation in ((Op.ORDERS,Op.CONDITIONAL_ORDERS) if 'orders' in sections else ()):
            try:
                cursor=None;seen=set()
                for page_number in range(10):
                    kwargs={'status':'OPEN','limit':100}
                    if cursor:kwargs['cursor']=cursor
                    page=fetch(operation,account=account,**kwargs)
                    if operation==Op.ORDERS:
                        for o in page.orders:
                            result['orders'].append({'account':account,'key':('KR' if o.currency=='KRW' else 'US')+':'+o.symbol,
                                'id':o.order_id,'kind':'regular','side':o.side,'quantity':o.quantity,
                                'filled_quantity':o.execution.filled_quantity,'status':o.status,
                                'reservation_basis':'이미 주문가능금액/매도가능수량에 반영된 일반 주문을 중복 차감하지 않음'})
                    else:
                        for o in page.conditional_orders:
                            legs=[o.first]+([o.second] if hasattr(o.second,'status') else [])
                            result['orders'].append({'account':account,'key':o.market+':'+o.symbol,'id':o.conditional_order_id,
                                'kind':'conditional','type':o.type,'status':o.status,'quantity':o.quantity,
                                'legs':[{'side':resolve_conditional_order_side(o.type,n,getattr(leg,'order_side',None)),
                                         'status':leg.status,'price':leg.order_price if isinstance(leg.order_price,str) else None}
                                        for n,leg in enumerate(legs)],
                                'relationship':'OCO 두 매도는 택일, OTO 후속 매도는 선행 매수 후 실행. 각 다리를 독립 동시 주문으로 합산하지 않음'})
                    if not page.has_next:break
                    cursor=page.next_cursor
                    if not isinstance(cursor,str) or cursor in seen:raise ValueError('pagination_stalled')
                    seen.add(cursor)
                else:raise ValueError('order_pages_exceeded')
            except Exception as error:
                result['orders'].append({'account':account,'key':'*','kind':'unavailable','operation':operation.value})
                result['failures'].append({'operation':operation.value,'reason':type(error).__name__})
    for position in (positions if 'sellable' in sections else ()):
        if position["scope"] != "personal":
            continue
        try:
            value = fetch(Op.SELLABLE_QUANTITY, account=position["account"], symbol=position["key"].split(":")[1])
            result["positions"].append({"account": position["account"], "key": position["key"], "sellable": value.sellable_quantity,
                                        'observed_at':now.isoformat()})
        except Exception as error:
            result["failures"].append({"operation": "SELLABLE_QUANTITY", "key": position["key"], "reason": type(error).__name__})
    if 'fx' not in sections:return result
    try:
        value = fetch(Op.EXCHANGE_RATE, baseCurrency="USD", quoteCurrency="KRW")
        result["fx"] = {"base_currency": "USD", "quote_currency": "KRW", "rate": value.mid_rate,
            "observed_at": value.valid_from.isoformat(), "source": "Toss 참고용 midRate", "acquired_at": now.isoformat()}
    except Exception as error:
        result["failures"].append({"operation": "EXCHANGE_RATE", "reason": type(error).__name__})
    return result


def verified_price_history(row):
    """Return coherent retained coverage; the analysis request remains separate."""
    from datetime import date
    history = row.get('metrics', {}).get('price_history', {})
    if not isinstance(history, dict):
        return None
    history = history.get('cache_coverage', history)
    bars = row.get('bars', [])
    if not isinstance(history, dict) or not bars:
        return None
    dates = [b.get('date') for b in bars]
    try:
        if any(date.fromisoformat(d).isoformat() != d for d in dates):
            return None
        end = date.fromisoformat(history['requested_end']).isoformat()
        start = history.get('requested_start')
        if start is not None and date.fromisoformat(start).isoformat() != start:
            return None
        sessions = history.get('requested_sessions')
        stop = history.get('stop_reason')
        if (dates != sorted(set(dates)) or type(history.get('actual_count')) is not int or history['actual_count'] != len(bars)
                or history.get('actual_start') != dates[0] or history.get('actual_end') != dates[-1]
                or end != dates[-1] or not all(b.get('adjusted') is True for b in bars)
                or stop not in {'TARGET_REACHED', 'SOURCE_EXHAUSTED'}
                or history.get('status') != ('COVERED' if stop == 'TARGET_REACHED' else 'PARTIAL')
                or (start is not None and (start > dates[0] or sessions is not None))
                or (start is None and (type(sessions) is not int or sessions < len(bars)))
                or (sessions is not None and stop == 'TARGET_REACHED' and sessions != len(bars))):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    return history


class HistoryRequestError(ValueError):
    """A known caller error, distinct from storage/provider/internal failures."""


def validate_history_request(*, history_start=None, history_sessions=None, instruments=(), now,
                             completed_session=None):
    """Validate the requested span before any wire call; errors are bounded identifiers."""
    from datetime import date, time, timedelta
    from zoneinfo import ZoneInfo
    if history_start is not None and history_sessions is not None:
        raise HistoryRequestError('invalid_history_exclusive')
    if history_sessions is not None and (type(history_sessions) is not int or history_sessions < 1):
        raise HistoryRequestError('invalid_history_sessions')
    if history_start is None:
        return
    try:
        start = date.fromisoformat(history_start)
        if start.isoformat() != history_start:
            raise ValueError()
    except (ValueError, TypeError):
        raise HistoryRequestError('invalid_history_start') from None
    for item in instruments:
        local = now.astimezone(ZoneInfo('Asia/Seoul' if item['market'] == 'KR' else 'America/New_York'))
        close = time(15, 30) if item['market'] == 'KR' else time(16)
        end = (date.fromisoformat(completed_session) if completed_session else
               local.date() - timedelta(days=int(local.time() < close)))
        if start > end:
            raise HistoryRequestError('invalid_history_after_completed_session')


def capture_market_data(fetch, *, instruments, now, completed_session=None, session_closes=None,
                        history_start=None, history_sessions=None, history_reason=None):
    """Reuse official stock metadata and adjusted candles through a GET seam.

    Last complete local dates only: today's in-progress candle is not a
    completed bar. The daily close timestamp uses the existing market day
    convention; early-close precision requires the calendar-supplied time.
    """
    from datetime import time, timedelta
    from zoneinfo import ZoneInfo
    from tokmo.facts.toss_reads import TossReadOperation as Op
    from tokmo.values._primitives import MISSING
    validate_history_request(history_start=history_start, history_sessions=history_sessions,
                             instruments=instruments, now=now, completed_session=completed_session)
    result, failures = [], []
    symbols = tuple(dict.fromkeys(i['symbol'] for i in instruments))
    metadata = {}
    for offset in range(0, len(symbols), 200):
        try:
            for item in fetch(Op.STOCKS, symbols=symbols[offset:offset + 200]):
                market = ('KR' if item.market in {'KR','KOSPI','KOSDAQ','KONEX'} else
                          'US' if item.market in {'US','NASDAQ','NYSE','AMEX'} else None)
                if market and item.currency == ('KRW' if market=='KR' else 'USD'):
                    metadata[market, item.symbol] = item
        except Exception as error:
            failures.append({'operation': 'STOCKS', 'reason': type(error).__name__})
    for instrument in instruments:
        symbol, market = instrument['symbol'], instrument['market']
        zone = ZoneInfo('Asia/Seoul' if market == 'KR' else 'America/New_York')
        local = now.astimezone(zone)
        # Before midnight is explicit: never treat an incomplete daily bar
        # as today's completed session merely because it is already returned.
        before = datetime.combine(local.date(), time(), tzinfo=zone)
        normal_close=time(15,30) if market=='KR' else time(16)
        if local.time()>=normal_close:before+=timedelta(days=1)
        if completed_session:
            before=datetime.combine(datetime.fromisoformat(completed_session).date()+timedelta(days=1),time(),tzinfo=zone)
        end = before.date() - timedelta(days=1)
        try:
            start = end.replace(year=end.year-1)
        except ValueError:  # February 29 -> February 28 in the previous year.
            start = end.replace(year=end.year-1, day=28)
        if history_start is not None:
            from datetime import date
            start = date.fromisoformat(history_start)
        if history_sessions is not None: start = None
        candles, cursor, pages, stop = {}, before, 0, 'PAGE_BUDGET'
        # Calendar days bound possible daily bars; one extra page permits
        # overlap. The supplied reader independently enforces its call budget.
        page_limit = ((history_sessions+199)//200+1 if history_sessions is not None
                      else ((end-start).days+200)//200+1)
        try:
            while pages < page_limit:
                count = min(200, history_sessions-len(candles)) if history_sessions else 200
                pages += 1
                page = fetch(Op.STOCK_CANDLES, symbol=symbol, interval='1d', count=count, adjusted=True, before=cursor)
                oldest = None
                for candle in page.candles:
                    day = candle.timestamp.astimezone(zone).date()
                    close_time = datetime.combine(day, normal_close, tzinfo=zone)
                    if (session_closes or {}).get(market+':'+day.isoformat()):
                        close_time = instant(session_closes[market+':'+day.isoformat()])
                    if day > end or close_time > now: continue
                    oldest = min(day, oldest) if oldest else day
                    if start is not None and day < start: continue
                    bar = dict(date=day.isoformat(), available_at=close_time.isoformat(),
                        open=candle.open_price, high=candle.high_price, low=candle.low_price,
                        close=candle.close_price, volume=candle.volume, adjusted=True)
                    candles[day] = bar
                if ((history_sessions is not None and len(candles) >= history_sessions) or
                        (start is not None and oldest is not None and oldest <= start)):
                    stop = 'TARGET_REACHED'
                    break
                next_before = getattr(page, 'next_before', None)
                if not page.candles or next_before is None or next_before is MISSING:
                    stop = 'SOURCE_EXHAUSTED'
                    break
                if (not isinstance(next_before, datetime)
                        or next_before.utcoffset() is None or next_before >= cursor):
                    raise ValueError('candle_cursor_not_advancing')
                cursor = next_before
            if stop == 'PAGE_BUDGET': raise ValueError('candle_page_budget')
        except Exception as error:
            known = {'candle_cursor_not_advancing', 'candle_page_budget'}
            bounded = {'capture job call budget reached':'capture_call_budget',
                'not enough time for optional capture before next tick':'capture_deadline',
                'expired existing token; no renewal':'capture_auth_blocked',
                'http_429':'http_429', 'auth_rejection':'auth_rejection', 'forbidden':'forbidden'}
            reason = str(error) if str(error) in known else bounded.get(str(error), type(error).__name__)
            stop = 'FETCH_FAILED'
            failures.append({'operation':'STOCK_CANDLES','key':f'{market}:{symbol}','reason':reason})
        bars = [candles[day] for day in sorted(candles)]
        if history_sessions is not None: bars = bars[-history_sessions:]
        row: dict = dict(symbol=symbol, market=market, bars=bars)
        item = metadata.get((market, symbol))
        if item:
            row.update(name=item.name, english_name=item.english_name, currency=item.currency,
                       kind='etf' if item.security_type.upper() == 'ETF' else 'stock')
            row['metrics'] = {'shares_outstanding': {'value':item.shares_outstanding, 'unit':'shares',
                'period':local.date().isoformat(), 'available_at':now.isoformat(), 'source':'Toss GET /api/v1/stocks'}}
        row.setdefault('metrics', {})['price_history'] = {
            'available_at': now.isoformat(),
            'requested_start': start.isoformat() if start else None, 'requested_end': end.isoformat(),
            'requested_sessions': history_sessions,
            'actual_start': bars[0]['date'] if bars else None,
            'actual_end': bars[-1]['date'] if bars else None, 'actual_count': len(bars),
            'pages': pages, 'stop_reason': stop, 'status': 'COVERED' if stop == 'TARGET_REACHED' else 'PARTIAL',
            'failure_reason': reason if stop == 'FETCH_FAILED' else None,
            'reason': history_reason or ('explicit_sessions' if history_sessions else
                'explicit_start' if history_start else 'default_calendar_year')}
        result.append(row)
    return {'instruments':result,'failures':failures}
