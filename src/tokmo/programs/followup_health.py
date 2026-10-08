"""Small, read-only diagnostics for the existing followup worker."""
import json
from pathlib import Path
import re
import sqlite3


def error_info(error):
    """Only known local validation details; never copy raw provider messages."""
    text=str(error)
    category=getattr(error,'category',None)
    if category in {'TRANSIENT','EXECUTOR_UNKNOWN','AUTH','QUOTA','REQUEST'}:
        return {'cause':'model_execution','model_error_category':category,'exit_code':error.exit_code,
                'diagnostic_path':error.diagnostic_path}
    if text.startswith('subscription CLI failed (exit '):
        match=re.fullmatch(r'subscription CLI failed \(exit (-?\d+)\)',text)
        return {'cause':'model_execution','exit_code':int(match[1]) if match else None}
    if text=='subscription model returned an error':return {'cause':'model_response'}
    if 'unsupported monetary/share claim:' in text:
        match=re.search(r'unsupported monetary/share claim: ([+\-\d,.조억만천백십\s]+(?:원|달러|주|%))',text)
        return {'cause':'number_validation','value':match[1] if match else None}
    if '근거 id' in text or 'unknown source' in text:
        return {'cause':'reference_validation'}
    if isinstance(error,PermissionError):
        return {'cause':'runtime_scope' if 'PRODUCT_SCOPE' in text else 'model_access'}
    return {}


def enrich_errors(root, errors):
    """Resolve local choice labels and persisted failures, not conversation text."""
    rows=[dict(e) for e in errors]
    path=Path(root)/'private/investment-followup.sqlite3'
    if not path.is_file():return rows
    connection=None
    try:
        connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
        for row in rows:
            cid=row.get('choice_id')
            if not cid:continue
            found=connection.execute('SELECT payload FROM investment_choices WHERE id=?',(cid,)).fetchone()
            if found:
                choice=json.loads(found[0]);i=choice['interpretation']
                row['subject']=i['summary'][:100]
                plan=choice.get('plan',{})
                state_row=connection.execute('SELECT value FROM investment_watermarks WHERE source=?',('plan:'+cid,)).fetchone()
                state=(json.loads(state_row[0]) or {}) if state_row else {}
                if row.get('stage')=='plan' and 'TIME' not in state.get('done',[]):
                    if plan.get('review_at'):row['scheduled_at']=plan['review_at']
                    row.setdefault('review_kind','SCHEDULED')
                elif row.get('stage')!='price_checkpoint':
                    row.pop('scheduled_at',None)
                    row.setdefault('review_kind','FOLLOWUP')
                    if 'TIME' in state.get('done',[]):row['previous_schedule_completed']=plan.get('review_at')
            if not row.get('cause'):
                found=connection.execute('SELECT value FROM investment_watermarks WHERE source=?',('failed_review:'+cid,)).fetchone()
                failure=json.loads(found[0]) if found else None
                if isinstance(failure,dict):row.update({k:failure[k] for k in ('cause','value','exit_code','model_error_category','diagnostic_path','review_kind') if k in failure})
            if row.get('error')=='REVIEW_PENDING_RETRY':
                found=connection.execute('SELECT value FROM investment_watermarks WHERE source=?',('plan:'+cid,)).fetchone()
                work=(json.loads(found[0]) or {}).get('work',{}) if found else {}
                row.update({k:work[k] for k in ('cause','value','exit_code','failed_at','model_error_category','diagnostic_path','review_kind') if k in work})
                failure=work.get('failure') or {}
                row.update({k:failure[k] for k in ('tries','retry_at','retryable') if k in failure})
                row.setdefault('retryable',False)
    except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):
        # Failure to add a label must not hide the original worker error.
        pass
    finally:
        if connection is not None:connection.close()
    return rows


def issue_key(error):
    stage=error.get('stage') or ('review' if error.get('choice_id') else 'worker')
    subject=error.get('choice_id') or ''
    if error.get('http_status') in (401,403):return stage+':authentication:'+subject
    if stage in {'plan','review'}:return 'analysis:'+subject
    if stage=='price_checkpoint':return 'price:'+subject
    if stage in {'match','delivery','snapshot'}:return stage
    return stage+':'+str(error.get('error','unknown'))


def issue_text(error):
    code=error.get('error','unknown');stage=error.get('stage');cause=error.get('cause')
    subject=error.get('subject') or error.get('choice_id') or '전체 점검'
    if cause=='number_validation':
        reason='분석에 쓴 '+(error.get('value') or '수치')+'를 입력 근거와 맞추지 못했어.'
    elif cause=='reference_validation':reason='분석이 가리킨 근거 번호가 제공한 자료와 맞지 않아.'
    elif cause=='model_execution':
        reason={'TRANSIENT':'모델 서비스의 일시적인 연결·처리 오류로 분석이 중단됐어.',
                'AUTH':'모델 서비스 인증을 확인해야 해.',
                'QUOTA':'모델 구독 사용 한도 때문에 분석을 진행하지 못했어.',
                'REQUEST':'모델에 전달한 요청이나 실행 설정을 확인해야 해.',
                'EXECUTOR_UNKNOWN':'모델 실행기가 종료됐어. 원인 상세는 비공개 실행 기록에 남겼어.'}.get(error.get('model_error_category'),
                    '모델 실행기가 비정상 종료되어 분석을 끝내지 못했어.')
        if error.get('exit_code') is not None:reason+=' 종료 코드 '+str(error['exit_code'])+'.'
        if error.get('model_error_category')=='EXECUTOR_UNKNOWN' and not error.get('diagnostic_path'):
            reason='모델 실행기가 종료됐어. 상세 원인은 실행 기록으로 확인해야 해.'
    elif cause=='model_response':reason='모델 서비스가 정상 분석 대신 오류 응답을 반환했어.'
    elif cause=='runtime_scope':reason='운영 파일과 설치 검증 기록이 맞지 않아 모델 호출이 차단됐어.'
    elif code in {'AnalysisValidationError','ValueError','REVIEW_PENDING_RETRY'} and (error.get('choice_id') or stage=='review'):
        reason='분석 결과가 가격·수량 또는 근거 참조 검사를 통과하지 못했어.'
    elif stage=='price_checkpoint':
        reason={
            'QUOTE_OUTSIDE_SESSION':'아직 이번 정규장 시세가 들어오지 않았어.',
            'QUOTE_STALE':'시세의 기준 시각이 허용 범위보다 오래됐어.',
            'QUOTE_MISSING':'이 종목의 저장 시세가 없어.',
            'WAITING_SESSION':'정규장 시작을 기다리고 있어.',
        }.get(code,'가격 감시에 필요한 시세·장 운영 정보를 확인하지 못했어.')
    elif error.get('http_status') in (401,403) or isinstance(code,str) and code=='PermissionError':
        reason='모델 또는 서비스 인증·접근 권한을 확인해야 해.'
    elif stage=='match':
        count=len(set(error.get('event_ids',[])))
        reason='뉴스와 투자 선택 연결 보류'+(f' {count}건' if count else '')+'. 판단을 완료하지 못했어.'
    elif stage=='delivery':
        count=len(error.get('deliveries',[]))
        reason='알림 발송 확인 필요'+(f' {count}건' if count else '')+'. 완료 여부를 확인하지 못했어.'
    elif stage=='snapshot':reason='분석 입력자료를 읽지 못했어.'
    elif code in {'TimeoutError','ConnectionError','URLError'}:reason='모델 응답이나 서비스 연결이 제한 시간 안에 끝나지 않았어.'
    else:reason='점검 처리 중 오류가 발생했어 ('+str(code)[:60]+').'
    impact=('가격 조건 도달 판단이 보류돼.' if stage=='price_checkpoint' else
            '해당 분석 결과의 전달이 보류돼.' if error.get('choice_id') else
            '뉴스 연결과 후속 분석에 지연이 있을 수 있어.' if stage=='match' else
            '해당 처리 단계의 결과를 확인해야 해.')
    retry=('다음 수집 후 자동으로 다시 확인해.' if stage=='price_checkpoint' else
           '예정된 제한 횟수 안에서 자동 재시도해.' if error.get('retry_at') else
           '원인 수리 후 재검증이 필요해. 같은 입력을 계속 재호출하지 않아.')
    return subject,reason,impact,retry
