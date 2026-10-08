"""Hyundai analysis handoff using existing Quant requests and validated reports.

No broker adapter, approval writer, alternate model or band-clamping path
lives here. Proposed prices are aligned using the runner's existing tick rule.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from tokmo.records.rotation_analysis_inputs import ConfirmedMarketClosure
from tokmo.values.quant_decisions import parse_snapshot, instant
from tokmo.values.rotation_decision_file import parse_decision_file, DecisionFileError
from tokmo.values.rotation_policy import align_price

KST=ZoneInfo('Asia/Seoul')
PRICE_ONLY='지정가 도달 외 추가 조건 없음'
PURPOSE='hyundai-short-term-rotation'
# Owner approval stays open for 30 minutes after the proposal notice was
# delivered (2026-10-02 agreement), with one reminder 5 minutes before the end.
# The 10:40 analysis close in window() still bounds analysis completion.
APPROVAL_WINDOW=timedelta(minutes=30)
APPROVAL_WARNING=timedelta(minutes=5)


class RotationAnalysisError(ValueError):
    pass


def policy_digest(policy):
    return sha256(json.dumps(asdict(policy),sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()


def window(now):
    day=instant(now).astimezone(KST).date()
    return day,datetime.combine(day,time(10,5),KST),datetime.combine(day,time(10,40),KST)


def _number(value):
    try:
        result=Decimal(str(value))
        if isinstance(value,bool) or not result.is_finite():raise ValueError()
        return result
    except Exception:raise RotationAnalysisError('invalid_number') from None


def check_context(policy, context, now):
    # Capacity is configuration, like account identity and protected holdings,
    # not a constant inferred from the initial account snapshot. A lot fills and
    # empties over several days, so inventory may sit anywhere inside it.
    if policy.quantity<Decimal(1) or policy.quantity!=policy.quantity.to_integral_value():
        raise RotationAnalysisError('experiment_scope')
    if now>=policy.valid_until:raise RotationAnalysisError('experiment_expired')
    qty=_number(context['quantity'])
    if not 0<=qty<=policy.quantity:raise RotationAnalysisError('inventory_invalid')
    if context.get('frozen') is not False:raise RotationAnalysisError('inventory_frozen')
    if context.get('open_commands')!=[]:raise RotationAnalysisError('orders_unresolved')
    if _number(context['cash'])<0:raise RotationAnalysisError('inventory_cash_invalid')
    settled=instant(context['settled_at'])
    closed=instant(context['last_closed_at']) if context.get('last_closed_at') else None
    morning=(context.get('settled_before_today_open') is True
             and settled.astimezone(KST).date()==now.astimezone(KST).date()
             and settled<instant(context['regular_open_at']))
    if settled>now or not (morning or closed is not None and closed<settled):
        raise RotationAnalysisError('settlement_stale')
    basis=context.get('cost_basis')
    if qty>0 and (basis is None or _number(basis)<=0):raise RotationAnalysisError('cost_basis_unknown')
    if qty==0 and basis is not None:raise RotationAnalysisError('inventory_basis_mismatch')
    return qty


def build_rotation_request(data, policy, context, *, now):
    qty=check_context(policy,context,now)
    result=deepcopy(data)
    snapshot=parse_snapshot(result)
    if not timedelta(0)<=now-snapshot.as_of<=timedelta(hours=2):raise RotationAnalysisError('snapshot_stale')
    key=f'{policy.market}:{policy.symbol}'
    positions=[p for p in snapshot.positions if p.key==key and p.account==policy.account_scope]
    if len(positions)!=1 or positions[0].scope!='personal' or positions[0].quantity!=policy.protected_quantity+qty:
        raise RotationAnalysisError('account_inventory_mismatch')
    captured=positions[0].observed_at
    if captured is None or not instant(context['settled_at'])<=captured<=now or now-captured>timedelta(hours=2):
        raise RotationAnalysisError('holdings_stale')
    item=next((i for i in snapshot.instruments if i.key==key),None)
    if item is None or item.price is None or item.price_at is None:
        raise RotationAnalysisError('quote_missing')
    if not timedelta(0)<=now-item.price_at<=timedelta(hours=2) or item.price_at.astimezone(KST).date()!=now.astimezone(KST).date():
        raise RotationAnalysisError('quote_stale')
    if context.get('regular_open_at') and item.price_at<instant(context['regular_open_at']):
        raise RotationAnalysisError('quote_before_open')
    if not item.bars:raise RotationAnalysisError('daily_bars_missing')
    last_bar=date.fromisoformat(item.bars[-1]['date'])
    if context.get('last_closed_at') and last_bar<instant(context['last_closed_at']).astimezone(KST).date():
        raise RotationAnalysisError('daily_bars_stale')
    funds=[c for c in snapshot.cash if c.account==policy.account_scope and c.currency==item.currency]
    if len(funds)!=1:raise RotationAnalysisError('cash_missing')
    if not timedelta(0)<=now-funds[0].observed_at<=timedelta(hours=2):raise RotationAnalysisError('cash_stale')
    # What the lot still needs today: the empty part when it has room, the held
    # part when it is full. One share is the special case where both are 1.
    remaining=policy.quantity-qty
    wanted=remaining if remaining>0 else qty
    rotation=dict(experiment_id=policy.experiment_id,inventory={'quantity':str(qty)},
        capacity={'quantity':str(policy.quantity),'remaining':str(remaining),'today':str(wanted)},
        protected_position={'quantity':str(policy.protected_quantity)},
        cost_basis={'price':context.get('cost_basis')},
        buy_band={'lower':str(policy.buy_floor),'upper':str(policy.buy_ceiling)},
        sell_min_pct=str(policy.sell_min_pct),sell_target_pct=str(policy.sell_target_pct),
        fee_reserve={'amount':str(policy.fee_buffer)},settled_at=context['settled_at'],
        max_holding_days=None,loss_review_threshold=None)
    if qty:
        rotation['sell_reference']={'lower':str(policy.sell_floor_for(_number(context['cost_basis']))),
                                    'upper':str(policy.sell_target_for(_number(context['cost_basis']))),
                                    'upper_is_target_not_cap':True}
    question=(
        f'{item.name} 회전용 {wanted}주만 판단한다. 회전 정원은 {policy.quantity}주이고 현재 회전 재고는 {qty}주다. 실제 전체 보유와 현금은 그대로 읽고 보호 {policy.protected_quantity}주는 매도하지 않는다. '
        f'장기 보유 추가가 아닌 저가 진입·단기 반등 회수 목적이다. 비용 전 기본 목표 {policy.sell_target_pct}%, 상승 여력과 저항대를 보고 {policy.sell_min_pct}% 이상에서도 매도 허용. '
        '20일선 회복을 모든 진입의 필수조건으로 고정하지 말되, 실험 소액이라는 이유로 근거를 완화하지 않는다. '
        '매수·회수 가격의 시장 근거와 반대로 움직일 때 재검토 조건을 기존 자료로 설명한다. 목표 수익률은 도달 가능성의 근거가 아니다. '
        '회전 정원에 빈 자리가 있으면 그 빈 만큼 NEW_BUY, 정원이 다 찼으면 보유 전량 SELL 가정이다. '
        '정원이 2주 이상일 때 이미 일부를 보유한 매수는 평단을 낮추는 추가 매수이며 당일 되팔지 않는다. '
        '매도 기준가는 회전 보유분의 실제 체결가(2주 이상이면 그 가중평균)이고 계좌 전체 평단이나 주문 지정가가 아니다. '
        f'명시적 매수/매도 의견이면 price_proposal에 side, quantity={wanted}, entry_price와 reason을 반드시 적는다. '
        'NEW_BUY의 entry_price는 매수가, exit_price는 회수가로 둘 다 필요하다. SELL의 entry_price는 이번 매도가이며 추가 재매수는 제안하지 않는다. '
        'WATCH/HOLD는 가격 가정이 있어도 회전 보류로 해석한다. 실제 계좌 전체의 매도 의견을 회전 1주의 매도로 바꾸지 않는다. '
        f'제안 시 지정가 도달만 남았다면 condition을 정확히 "{PRICE_ONLY}"으로 적고, '
        '다른 반등·수급 확인이 더 필요하면 그 조건을 쓰고 보류한다. 미확인 조건을 이미 충족했다고 하지 않는다. '
        '현재가가 제안 매수가 이하이면 같은 가격·수량의 당일 지정가 매수로 전환되며 당일 후속 매도 예약은 없고 다음 거래일 새로 판단한다. '
        '사전 OTO는 지정 매수가 기준 예상 수익률이며 실제 체결가 자동 재연동으로 설명하지 않는다. '
        'fee_reserve는 현금 여유이며 실제 왕복 수수료·세금이 아니다. 최대 보유 기간과 손실 재검토 비율은 아직 미정이다. '
        '허용 범위 밖 가격도 근거가 있으면 제안하며 밴드에 맞춰 가격을 바꾸지 않는다. '
        '인계 시 실제 가격별 호가 단위로 매수는 내림·매도는 올림 정렬하고, 정렬 전후 가격을 함께 기록한다.')
    request=dict(key=key,account=policy.account_scope,scope='personal',currency=item.currency,quantity=str(wanted),
        side='NEW_BUY' if remaining>0 else 'SELL',purpose=PURPOSE,question=question,rotation=rotation,
        budget=str(min(_number(context['cash']),funds[0].available,policy.initial_budget)))
    result['analysis_requests']=[request]
    return result


def base_document(policy, *, started_at, now):
    day,_,deadline=window(started_at)
    return dict(schema='rotation-decision-file-v1',status='RUNNING',experiment_id=policy.experiment_id,
        account_scope=policy.account_scope,trade_date=day.isoformat(),analysis_id=f'astra-{day:%Y%m%d}-{policy.experiment_id}',
        producer='astra',started_at=started_at.isoformat(),produced_at=now.isoformat(),data_as_of=started_at.isoformat(),
        valid_until=min(deadline,policy.valid_until).isoformat(),decision=None,band_proposal=None,
        analysis_summary='현대차 회전 1주 분석 중',failure_reason=None,policy_digest=policy_digest(policy),revision=1)


def project_report(report, *, policy, context, started_at, now):
    qty=check_context(policy,context,started_at)
    expected=(policy.account_scope,'personal',f'{policy.market}:{policy.symbol}','cash')
    rows=[r for r in report.get('results',[]) if tuple(r.get(k) for k in ('account','scope','key','scenario'))==expected]
    if len(rows)!=1:raise RotationAnalysisError('report_scope')
    row=rows[0];packet=row.get('packet') or {}
    if tuple(packet.get(k) for k in ('account','scope','key','scenario'))!=expected:
        raise RotationAnalysisError('report_scope')
    request=packet.get('user_request') or {}
    if request.get('purpose')!=PURPOSE or request.get('rotation',{}).get('experiment_id')!=policy.experiment_id:
        raise RotationAnalysisError('report_purpose')
    if row.get('status')!='ANALYZED' or not isinstance(row.get('analysis'),dict):raise RotationAnalysisError('quant_failed')
    if row.get('analysis_source')=='reused':raise RotationAnalysisError('report_reused')
    if instant(report['as_of']).astimezone(KST).date()!=started_at.astimezone(KST).date():raise RotationAnalysisError('report_date')
    analysis=row['analysis'];action=analysis.get('action')
    if action not in {'BUY','ADD','TRIM','EXIT','HOLD','WATCH'}:raise RotationAnalysisError('quant_action')
    reason=analysis.get('summary')
    if not isinstance(reason,str) or not reason.strip():raise RotationAnalysisError('quant_summary')
    ids=list(dict.fromkeys(s for r in analysis.get('reasons',[]) for s in r.get('sources',[])))
    if not ids or any(not isinstance(s,str) or not s.strip() for s in ids):raise RotationAnalysisError('quant_evidence')
    remaining=policy.quantity-qty
    wanted=remaining if remaining>0 else qty
    decision=dict(symbol=policy.symbol,action='HOLD',buy_price=None,sell_price=None,expires_on=None,
                  quantity=str(wanted),total_amount=None,reason=reason[:1200],evidence_ids=ids)
    # A proposal with an extra condition still carries real prices. The owner
    # reads the condition on the notice and decides; it is never consumed
    # unapproved and never silently downgraded to HOLD.
    pending_condition=None
    if action not in {'HOLD','WATCH'}:
        buying=action in {'BUY','ADD'}
        if buying!=(remaining>0):raise RotationAnalysisError('inventory_action_mismatch')
        proposal=analysis.get('price_proposal')
        if not isinstance(proposal,dict):raise RotationAnalysisError('price_proposal_missing')
        if _number(proposal.get('quantity'))!=wanted:raise RotationAnalysisError('proposal_quantity')
        if proposal.get('side')!=('NEW_BUY' if buying else 'SELL'):raise RotationAnalysisError('proposal_side')
        option=row.get('selected_option')
        if option and _number(option.get('quantity'))!=wanted:raise RotationAnalysisError('protected_position')
        if proposal.get('price_basis')!='PROPOSED' or proposal.get('entry_price') is None:
            raise RotationAnalysisError('price_proposal_missing')
        if buying and proposal.get('exit_price') is None:raise RotationAnalysisError('exit_price_missing')
        if not buying and proposal.get('exit_price') is not None:raise RotationAnalysisError('unexpected_reentry')
        condition=proposal.get('condition')
        if condition!=PRICE_ONLY:
            pending_condition=condition.strip() if isinstance(condition,str) and condition.strip() else '조건 내용 미기재'
        adjustments=[]
        def price(raw,side):
            original=_number(raw)
            if original<=0:raise RotationAnalysisError('price_invalid')
            aligned=align_price(original,policy.market,side)
            text=format(aligned,'f')
            if aligned!=original:
                adjustments.append(f'{"매수" if side=="BUY" else "매도"} {raw} → {text}')
            return text
        entry=price(proposal['entry_price'],'BUY' if buying else 'SELL')
        exit_price=price(proposal['exit_price'],'SELL') if buying else entry
        decision.update(action='ENTER' if buying else 'EXIT',
            buy_price=entry if buying else None,
            sell_price=exit_price,
            expires_on=started_at.astimezone(KST).date().isoformat() if buying else None,
            total_amount=format(_number(entry)*wanted,'f'))
        notes=[]
        if pending_condition is not None:
            notes.append('모델 조건: '+pending_condition[:400])
        if adjustments:
            notes.append('호가 정렬: '+', '.join(adjustments))
        if notes:
            note='\n'.join(notes)
            decision['reason']=reason[:max(0,1199-len(note))]+'\n'+note
    result=base_document(policy,started_at=started_at,now=now)
    result.update(status='COMPLETE',decision=decision,analysis_summary=decision['reason'],report_id=report['report_id'],
                  data_as_of=report['as_of'])
    try:
        parsed=parse_decision_file(json.dumps(result),policy=policy,now=now,trade_date=started_at.astimezone(KST).date(),
                                   cost_basis=_number(context['cost_basis']) if qty else None)
    except DecisionFileError as error:raise RotationAnalysisError(error.code) from error
    if parsed.in_band is False or pending_condition is not None:
        result.update(status='WAITING_APPROVAL',proposal_id=f'{started_at.astimezone(KST):%Y%m%d}-today-{policy.experiment_id}')
        head='승인 범위 밖 제안이야. ' if parsed.in_band is False else ''
        if pending_condition is not None:
            head+='모델이 추가 조건을 달았어. 조건은 사용자가 직접 확인하고 승인해 줘. '
        result['analysis_summary']=head+'호가 정렬한 제안 가격으로 알림 발송 후 30분 동안 승인을 기다려. 정확한 마감 시각은 알림에 표시해. '+decision['reason']
        # A today-only exact-price exception need not change the standing band.
        # The owner creates its proposal from the preserved decision.
    return result


def _notify(store, policy, day, document, notify, now):
    try:
        receipt=notify(document)
        if not isinstance(receipt,dict) or not receipt.get('message_id'):raise ValueError('delivery_receipt_missing')
        state=dict(status='SENT',receipt=receipt)
    except Exception as error:state=dict(status='FAILED',error=type(error).__name__)
    state.update(decision_status=document['status'],at=now.isoformat())
    store.record_delivery(policy,day,state)


def approval_deadline(document, delivery):
    """When the owner can still approve: the file's own valid_until, bound at
    the moment the proposal notice was delivered. An undelivered notice has
    no owner-facing window, so the caller falls back to the analysis close."""
    if (document or {}).get('status')=='WAITING_APPROVAL' and (delivery or {}).get('status')=='SENT':
        return instant(document['valid_until'])
    return None


def _warn(store, policy, day, document, delivery, warn, now, closes):
    """One reminder, recorded whether or not it reached the group. Never retried,
    never restarts the window."""
    try:
        receipt=warn(document,closes)
        state=dict(status='SENT',at=now.isoformat(),receipt=receipt)
    except Exception as error:state=dict(status='FAILED',at=now.isoformat(),error=type(error).__name__)
    store.record_delivery(policy,day,dict(delivery,warning=state))


def run_rotation_analysis(*, policy, store, load_inputs, analyze, clock, notify, check_market=None, warn=None):
    now=instant(clock());day,start,deadline=window(now)
    if now<start:return {'status':'NOT_DUE','trade_date':day.isoformat()}
    with store.lock(policy,day) as acquired:
        if not acquired:return {'status':'BUSY','trade_date':day.isoformat()}
        existing=store.read(policy,day)
        if existing:
            if existing['status'] not in {'RUNNING','WAITING_APPROVAL'}:return existing
            delivery=store.delivery_receipt(policy,day) or {}
            owner_until=approval_deadline(existing,delivery)
            closes=owner_until or deadline
            if now<closes:
                if warn is not None and owner_until is not None and now>=closes-APPROVAL_WARNING and 'warning' not in delivery:
                    _warn(store,policy,day,existing,delivery,warn,now,closes)
                return existing
            reason='approval_timeout' if existing['status']=='WAITING_APPROVAL' else 'analysis_timeout'
            existing.update(status='EXPIRED',produced_at=now.isoformat(),failure_code=reason,failure_reason=reason,
                            analysis_summary=f'{closes.astimezone(KST):%H:%M} 마감으로 오늘 회전을 종료했어.')
            store.write(policy,day,existing);_notify(store,policy,day,existing,notify,now)
            return existing
        result=base_document(policy,started_at=now,now=now)
        calendar_checked=check_market is None
        try:
            # Calendar-only preflight also runs for a first close-only invocation.
            # Existing terminal documents above always win; never rewrite history.
            if check_market is not None:check_market()
            calendar_checked=True
            if now>=deadline:
                result.update(status='EXPIRED',failure_code='analysis_timeout',failure_reason='start_after_deadline',
                              analysis_summary='분석 시작 전에 10:40 마감이 지나 오늘 회전을 종료했어.')
            else:
                store.write(policy,day,result)
                data,context=load_inputs()
                # Input capture happens after the run started.
                inputs_ready_at=instant(clock())
                if inputs_ready_at>=deadline:raise RotationAnalysisError('analysis_timeout')
                data=build_rotation_request(data,policy,context,now=inputs_ready_at)
                report=analyze(data,deadline)
                completed=instant(clock())
                if completed>=deadline:raise RotationAnalysisError('analysis_timeout')
                result=project_report(report,policy=policy,context=context,started_at=now,now=completed)
        except ConfirmedMarketClosure as closed:
            if (closed.market,closed.trade_date)!=(policy.market,day):
                raise RotationAnalysisError('calendar_closure_identity') from closed
            result.update(status='SKIPPED',produced_at=instant(clock()).isoformat(),
                analysis_summary='공식 캘린더에서 종일 휴장이 확인돼 오늘 회전 분석을 실행하지 않았어.')
        except Exception as error:
            code=str(error) if isinstance(error,RotationAnalysisError) else 'model_timeout' if isinstance(error,TimeoutError) else type(error).__name__
            expired=(calendar_checked and instant(clock())>=deadline) or code=='analysis_timeout'
            result.update(status='EXPIRED' if expired else 'FAILED',produced_at=instant(clock()).isoformat(),
                failure_code='analysis_timeout' if expired else code,failure_reason=code,
                analysis_summary='현대차 회전 분석을 완료하지 못해 오늘은 거래를 보류해.')
        sent_at=instant(clock())
        if result['status']=='WAITING_APPROVAL':
            # The notice and the file bind the same deadline: 30 minutes from
            # this delivery attempt. A failed delivery leaves the file waiting,
            # and the analysis close (not this window) ends the day.
            result['valid_until']=min(sent_at+APPROVAL_WINDOW,policy.valid_until).isoformat()
        store.write(policy,day,result)
        _notify(store,policy,day,result,notify,sent_at)
        return result
