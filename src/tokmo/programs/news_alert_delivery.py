"""Injected-sender delivery adapter; no transport import and no blind retry."""

from tokmo.values.news_payload import contains_private_markers
from tokmo.values.ops_kernel import AlertOrigin
from tokmo.programs.news_source_buttons import source_markup


def transport_failure(error):
    """Keep diagnostic class/status, never raw URLs, tokens or provider bodies."""
    name=type(error).__name__
    name=''.join(c for c in name if c.isalnum() or c=='_')[:48]
    code=getattr(error,'code',None)
    status=str(code) if type(code) is int and 100<=code<=599 else None
    reason='provider_failure:'+name+((':'+status) if status else '')
    return reason, code in {429,500,502,503,504}


def news_live_body_guard(outbox):
    """Pre-transport privacy judgment for the live lane: every body part
    must be free of account, local-path, and credential markers."""
    return not any(contains_private_markers(part)
                   for part in outbox.body_evidence)


def run_alert_delivery(*, ops_kernel, role, telegram_send, keyboard_resolver, delivery_binder, now, body_guard=None, created_not_before=None):
    """Claim from the one canonical queue and settle exactly once by origin.
    ``created_not_before`` (aware) excludes pre-cutoff PENDING backlog from
    transport selection entirely (incident 2026-08-22)."""
    result={"claimed":0,"sent":0,"failed":0,"unknown":0}
    outbox=ops_kernel.claim_pending_telegram(role,now,origins=(
        AlertOrigin.NEWS_EVENT.value,AlertOrigin.NEWS_OPS.value),
        created_not_before=created_not_before)
    if outbox is None:return result
    result["claimed"]=1
    if body_guard is not None and not body_guard(outbox):
        # privacy/redaction fail-closed: settle FAILED without ever handing
        # the body to the transport; the claim is terminal, never retried.
        ops_kernel.mark_failed(role,outbox.alert_id,now,"redaction_rejected")
        result["rejected"]=1; return result
    markup=keyboard_resolver(outbox.alert_id) if outbox.origin is AlertOrigin.NEWS_EVENT else None
    if outbox.origin is AlertOrigin.NEWS_EVENT:
        markup=source_markup(outbox,markup)
    try:
        provider_message_id=telegram_send(outbox.body_evidence[0],reply_markup=markup)
    except TimeoutError:
        ops_kernel.mark_unknown(role,outbox.alert_id,now,"provider_timeout")
        result["unknown"]=1; return result
    except Exception as error:
        reason,transient=transport_failure(error)
        ops_kernel.mark_failed(role,outbox.alert_id,now,reason)
        result['failure_reason']=reason
        result['transient_failure']=transient
        result["failed"]=1; return result
    try:
        ops_kernel.mark_sent(role,outbox.alert_id,now,str(provider_message_id))
    except Exception:
        ops_kernel.mark_unknown(role,outbox.alert_id,now,"settle_failed_after_delivery")
        result["unknown"]=1; return result
    if outbox.origin is AlertOrigin.NEWS_EVENT:
        delivery_binder(outbox.alert_id,str(provider_message_id),now=now)
    result["sent"]=1
    return result


def run_canary_alert_delivery(*, ops_kernel, role, ledger, delivery,
                              telegram_send, keyboard_resolver,
                              delivery_binder, now):
    """Exactly-one Telegram canary drain (approval packet §2.2/§2.5).

    Same canonical outbox and claim/settle semantics as run_alert_delivery,
    plus a durable one-attempt reservation persisted BEFORE the transport
    start. SENT, FAILED, and UNKNOWN all keep the cap consumed; UNKNOWN is
    never retried or requeued; window expiry or a consumed cap claims
    nothing at all (automatic fail-closed OFF)."""
    result={"claimed":0,"sent":0,"failed":0,"unknown":0,"reserved":0,"off":None}
    if delivery.mode!="TELEGRAM_CANARY":
        result["off"]="mode"; return result
    if now>=delivery.window_end:
        result["off"]="window_expired"; return result
    if now<delivery.cutoff:
        result["off"]="before_cutoff"; return result
    if ledger.canary_attempt(delivery.activation_id) is not None:
        result["off"]="cap_consumed"; return result
    # §2.3 + incident 2026-08-22: only rows created at/after the delivery
    # cutoff are transport-eligible; an older PENDING backlog claims
    # nothing, so the one-attempt cap stays unconsumed.
    outbox=ops_kernel.claim_pending_telegram(role,now,origins=(
        AlertOrigin.NEWS_EVENT.value,),created_not_before=delivery.cutoff)
    if outbox is None:return result
    result["claimed"]=1
    if not ledger.reserve_canary_attempt(delivery.activation_id,
                                         outbox.alert_id,now=now):
        # a prior reservation already holds the cap: never start transport
        ops_kernel.mark_failed(role,outbox.alert_id,now,"canary_cap_consumed")
        result["failed"]=1; result["off"]="cap_consumed"; return result
    result["reserved"]=1
    markup=keyboard_resolver(outbox.alert_id)
    markup=source_markup(outbox,markup)
    try:
        provider_message_id=telegram_send(outbox.body_evidence[0],reply_markup=markup)
    except TimeoutError:
        ops_kernel.mark_unknown(role,outbox.alert_id,now,"provider_timeout")
        ledger.settle_canary_attempt(delivery.activation_id,"UNKNOWN",now=now)
        result["unknown"]=1; return result
    except Exception:
        ops_kernel.mark_failed(role,outbox.alert_id,now,"provider_failure")
        ledger.settle_canary_attempt(delivery.activation_id,"FAILED",now=now)
        result["failed"]=1; return result
    try:
        ops_kernel.mark_sent(role,outbox.alert_id,now,str(provider_message_id))
    except Exception:
        ops_kernel.mark_unknown(role,outbox.alert_id,now,"settle_failed_after_delivery")
        ledger.settle_canary_attempt(delivery.activation_id,"UNKNOWN",now=now)
        result["unknown"]=1; return result
    delivery_binder(outbox.alert_id,str(provider_message_id),now=now)
    ledger.settle_canary_attempt(delivery.activation_id,"SENT",now=now)
    result["sent"]=1
    return result


def live_delivery_authorized(ledger, delivery, now):
    """Recurring-lane judgment (user direction 2026-08-21): TELEGRAM_LIVE is
    ON only inside the activation window (or explicit continuous operation) AND with durable ledger
    proof that the referenced canary activation settled SENT and completed
    its two-slot duplicate-observation read-back, AND with no durable
    delivery trip. A gate-file edit alone can never satisfy this."""
    if delivery.mode != "TELEGRAM_LIVE":
        return False, "mode"
    if now < delivery.cutoff:
        return False, "before_cutoff"
    if delivery.window_end is None:
        if getattr(delivery,'continuous',False) is not True:
            return False, 'window_missing'
    elif now >= delivery.window_end:
        return False, "window_expired"
    predecessor = delivery.predecessor_activation_id
    attempt = ledger.canary_attempt(predecessor)
    if attempt is None or attempt["outcome"] != "SENT":
        return False, "predecessor_unproven"
    if ledger.canary_slot_proof(predecessor) is None:
        return False, "proof_missing"
    if ledger.live_trip(delivery.activation_id) is not None:
        return False, "live_tripped"
    return True, None


def run_live_alert_delivery(*, ops_kernel, role, ledger, delivery,
                            telegram_send, keyboard_resolver,
                            delivery_binder, now,
                            body_guard=news_live_body_guard):
    """Recurring live drain over the same canonical outbox and sender.

    Bounded to one claim per invocation (one pulse tick). A known temporary
    HTTP rejection settles that message but does not disable future news.
    Unknown delivery, unclassified errors, privacy and invariant failures
    still retain the durable trip. Failed/unknown messages are never resent.
    """
    result={"claimed":0,"sent":0,"failed":0,"unknown":0,"rejected":0,
            "off":None,"tripped":None}
    authorized,reason=live_delivery_authorized(ledger,delivery,now)
    if not authorized:
        result["off"]=reason; return result

    def _trip(trip_reason):
        ledger.record_live_trip(delivery.activation_id,trip_reason,now=now)
        result["tripped"]=trip_reason
    try:
        inner=run_alert_delivery(
            ops_kernel=ops_kernel,role=role,telegram_send=telegram_send,
            keyboard_resolver=keyboard_resolver,
            delivery_binder=delivery_binder,now=now,body_guard=body_guard,
            created_not_before=delivery.cutoff)
    except Exception:
        # duplicate transport start, unattributed write, or any other
        # claim/settle invariant break: delivery-only OFF, lane preserved.
        _trip("delivery_invariant_failure")
        return result
    for key in ("claimed","sent","failed","unknown"):
        result[key]=inner.get(key,0)
    result["rejected"]=inner.get("rejected",0)
    if inner.get('failure_reason'):result['failure_reason']=inner['failure_reason']
    if result["rejected"]:
        _trip("redaction_failure")
    elif result["unknown"]:
        _trip("transport_unknown")
    elif result["failed"] and inner.get('transient_failure'):
        result['degraded']='temporary_transport_rejection'
    elif result["failed"]:
        _trip("transport_failed")
    return result
