"""Reuse prepared Quant packets in the current conversation, without a second AI."""
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
import json

from tokmo.programs.quant_decision_analysis import MODEL_INSTRUCTION, ALARM_WRITING_STYLE, validate_analysis, enforce_shared_budget, render_report


def fingerprint(value):
    return sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def prepare_dialogue(report):
    return {'report_id':report['report_id'],'instructions':MODEL_INSTRUCTION+ALARM_WRITING_STYLE,
            'rows':[{'identity':r['identity'],'packet_sha256':fingerprint(r['packet']),'packet':r['packet']}
                    for r in report['results']],
            'response_shape':{'report_id':report['report_id'],'author_model':'actual current model',
                'responses':{'row identity':{'packet_sha256':'copy from row','analysis':'same Quant JSON contract'}}}}


def complete_dialogue(prepared, answer, *, author_model, completed_at):
    if any(r.get('status')!='CALCULATED' or r.get('analysis') is not None for r in prepared['results']):
        raise ValueError('dialogue requires an unanalysed prepared report')
    if answer.get('report_id')!=prepared['report_id'] or answer.get('author_model')!=author_model:
        raise ValueError('dialogue report/model identity mismatch')
    if not author_model or author_model in {'off','recorded','current'}:raise ValueError('actual author model required')
    rows=answer.get('responses',{})
    if not isinstance(rows,dict) or set(rows)!={r['identity'] for r in prepared['results']}:
        raise ValueError('dialogue requires the exact prepared rows')
    result=deepcopy(prepared)
    for row in result['results']:
        response=rows[row['identity']]
        if response.get('packet_sha256')!=fingerprint(row['packet']):raise ValueError('dialogue packet mismatch')
        row.update(analysis=None,selected_option=None,analysis_source='dialogue',errors=[],status='FAILED',
            analysis_origin={'model':author_model,'source':'current_conversation_declared',
                'prepared_report_id':prepared['report_id'],'completed_at':completed_at})
        try:
            row['analysis']=validate_analysis(response['analysis'],row['packet'])
            row['selected_option']=next((o for o in row['packet']['options'] if o['id']==row['analysis']['option_id']),None)
            row['status']='ANALYZED'
        except (ValueError,KeyError,TypeError) as error:row['errors']=['invalid_dialogue:'+str(error)]
    balances={(r['account'],r['currency'],r['scenario']):Decimal(r['remaining_after_buys']) for r in prepared['allocation']}
    remaining=enforce_shared_budget(result['results'],balances=balances)
    result['allocation']=[dict(account=k[0],currency=k[1],scenario=k[2],remaining_after_buys=str(v))
                          for k,v in sorted(remaining.items())]
    result['dialogue']={'author_model':author_model,'source':'current_conversation_declared',
        'prepared_report_id':prepared['report_id'],'completed_at':completed_at,'additional_model_calls':0}
    result.pop('report_id',None)
    result['report_id']='qd-'+fingerprint(result)[:16]
    return result


def report_pages(report, *, limit=3800):
    """Two plain-text pages, splitting complete paragraphs/lines without loss.

    The caller can shorten commentary if it exceeds two pages; original results
    remain saved. No analysis source or condition is silently truncated.
    """
    text=render_report(report)
    # Plain-text export uses Telegram's UTF-16 limit and keeps complete lines.
    length=lambda s:len(s.encode('utf-16-le'))//2
    pages=[];part=[]
    for line in text.splitlines():
        if length(line)>limit:raise ValueError('QUANT_DISPLAY_TOO_LONG: one line exceeds page')
        if part and length('\n'.join([*part,line]))>limit:
            pages.append('\n'.join(part));part=[]
        part.append(line)
    if part:pages.append('\n'.join(part))
    if len(pages)>2:raise ValueError('QUANT_DISPLAY_TOO_LONG: shorten commentary; original saved')
    return pages
