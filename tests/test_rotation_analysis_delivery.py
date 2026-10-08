from copy import deepcopy
from datetime import timedelta
import json

import pytest

from test_rotation_analysis import policy, report, project, NOW, DEADLINE
from tokmo.records.rotation_analysis import RotationAnalysisStore
from tokmo.programs.rotation_analysis_delivery import (
    callback_token, make_notifier, resolve_callback, ApprovalError,
)


def waiting(tmp_path):
    store=RotationAnalysisStore(tmp_path/'decisions')
    doc=project(report(price='379500'))
    store.write(policy(),NOW.date(),doc)
    receipt={'message_id':'42','chat_id':'-99','callback_token':callback_token(doc)}
    store.record_delivery(policy(),NOW.date(),{'status':'SENT','receipt':receipt})
    return store,doc


def resolve(tmp_path,store,doc,**changes):
    args=dict(store=store,proposal_root=tmp_path/'proposals',policy=policy(),
              user_id='7',owner_id='7',chat_id='-99',message_id='42',
              data='rot1:a:'+callback_token(doc),now=NOW+timedelta(minutes=3))
    args.update(changes)
    return resolve_callback(**args)


def test_notification_preserves_price_and_has_bound_short_buttons():
    doc=project(report(price='379500'));calls=[]
    send=lambda text,reply_markup=None: calls.append((text,reply_markup)) or 'telegram-group:-99:42'
    receipt=make_notifier(send)(doc)
    assert '379,500' in calls[0][0] and '10:40' in calls[0][0]
    buttons=calls[0][1]['inline_keyboard'][0]
    assert len(buttons)==2 and all(len(b['callback_data'].encode())<=64 for b in buttons)
    assert receipt=={'message_id':'42','chat_id':'-99','callback_token':callback_token(doc)}


def test_owner_approval_is_canonical_today_only_and_preserves_price(tmp_path):
    store,doc=waiting(tmp_path)
    assert resolve(tmp_path,store,doc)=='APPROVED'
    approval=json.loads((tmp_path/'proposals'/f"{doc['proposal_id']}.json").read_text())
    assert approval['scope']=='today' and approval['approved_by']=='7'
    assert approval['policy_digest']==doc['policy_digest']
    assert approval['decision']['sell_price']=='379500'
    final=store.read(policy(),NOW.date())
    assert final['status']=='COMPLETE' and final['decision']==doc['decision']
    from tokmo.values.rotation_decision_file import today_approval_matches
    assert today_approval_matches(approval,decision=final['decision'],policy=policy(),trade_date=NOW.date())
    assert resolve(tmp_path,store,doc)=='ALREADY_RESOLVED'


@pytest.mark.parametrize('changes',[
    {'user_id':'8'}, {'owner_id':''}, {'chat_id':'-98'}, {'message_id':'43'},
    {'data':'rot1:a:wrong'}, {'now':DEADLINE}, {'now':NOW+timedelta(days=1)},
])
def test_wrong_or_expired_callback_cannot_approve(tmp_path,changes):
    store,doc=waiting(tmp_path)
    with pytest.raises(ApprovalError):resolve(tmp_path,store,doc,**changes)
    assert not (tmp_path/'proposals').exists()
    assert store.read(policy(),NOW.date())['status']=='WAITING_APPROVAL'


@pytest.mark.parametrize('field,value',[
    ('policy_digest','changed'), ('revision',2), ('decision',{'action':'EXIT','sell_price':'379000'}),
])
def test_document_changed_after_notice_cannot_be_approved(tmp_path,field,value):
    store,doc=waiting(tmp_path);changed=deepcopy(doc);changed[field]=value
    store.write(policy(),NOW.date(),changed)
    with pytest.raises(ApprovalError):resolve(tmp_path,store,doc)


def test_existing_owner_proposal_must_match_exact_decision(tmp_path):
    store,doc=waiting(tmp_path)
    (tmp_path/'proposals').mkdir()
    (tmp_path/'proposals'/f"{doc['proposal_id']}.json").write_text(json.dumps({
        'scope':'today','status':'PENDING','experiment_id':policy().experiment_id,
        'trade_date':doc['trade_date'],'analysis_id':doc['analysis_id'],
        'revision':1,'decision':{'action':'EXIT','sell_price':'379000'},
    }))
    with pytest.raises(ApprovalError):resolve(tmp_path,store,doc)


def test_rejection_closes_day_without_modifying_policy(tmp_path):
    store,doc=waiting(tmp_path)
    assert resolve(tmp_path,store,doc,data='rot1:r:'+callback_token(doc))=='REJECTED'
    final=store.read(policy(),NOW.date())
    assert final['status']=='FAILED' and final['failure_code']=='owner_rejected'


def test_failure_notice_has_no_approval_buttons():
    doc=project(report());doc.update(status='FAILED',failure_reason='quant_failed',decision=None)
    calls=[]
    make_notifier(lambda text,reply_markup=None: calls.append((text,reply_markup)) or 'telegram-group:-99:1')(doc)
    assert calls[0][1] is None and 'quant_failed' in calls[0][0]


def test_integer_tick_written_as_decimal_still_sends_notice():
    doc=project(report(price='380000.0'));calls=[]
    make_notifier(lambda text,reply_markup=None:calls.append(text) or 'telegram-group:-99:1')(doc)
    assert '380,000' in calls[0]


# --- approval window bound to the delivered notice (2026-10-02) ---

def later_window(doc, minute=47):
    changed=deepcopy(doc);changed['valid_until']=(NOW.replace(minute=minute)).isoformat()
    return changed


def test_notice_shows_the_exact_deadline_from_the_document():
    doc=later_window(project(report(price='379500')));calls=[]
    make_notifier(lambda text,reply_markup=None: calls.append(text) or 'telegram-group:-99:42')(doc)
    assert '10:47' in calls[0] and '10:40' not in calls[0]
    assert '5분 전' in calls[0]


def test_expiry_notice_names_the_real_deadline():
    doc=later_window(project(report(price='379500')))
    doc.update(status='EXPIRED',failure_code='approval_timeout',failure_reason='approval_timeout');calls=[]
    make_notifier(lambda text,reply_markup=None: calls.append(text) or 'telegram-group:-99:43')(doc)
    assert '10:47' in calls[0] and '10:40' not in calls[0]


def test_warning_notice_is_plain_text_with_deadline_and_no_buttons():
    from tokmo.programs.rotation_analysis_delivery import make_warning
    doc=later_window(project(report(price='379500')));calls=[]
    receipt=make_warning(lambda text,reply_markup=None: calls.append((text,reply_markup)) or 'telegram-group:-99:44')(doc,NOW.replace(minute=47))
    assert '10:47' in calls[0][0] and '379,500' in calls[0][0] and calls[0][1] is None
    assert receipt=={'message_id':'44','chat_id':'-99'}


def test_callback_honours_the_document_deadline_not_the_analysis_close(tmp_path):
    store=RotationAnalysisStore(tmp_path/'decisions')
    doc=later_window(project(report(price='379500')))
    store.write(policy(),NOW.date(),doc)
    store.record_delivery(policy(),NOW.date(),{'status':'SENT','receipt':{'message_id':'42','chat_id':'-99','callback_token':callback_token(doc)}})
    with pytest.raises(ApprovalError,match='expired'):resolve(tmp_path,store,doc,now=NOW.replace(minute=47))
    assert resolve(tmp_path,store,doc,now=NOW.replace(minute=45))=='APPROVED'
