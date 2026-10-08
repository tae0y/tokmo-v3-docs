"""Bridge durable decision proposals to the existing Telegram outbox."""
import re
import json

from tokmo.programs.news_decision_engine import identity, night
from tokmo.programs.news_source_buttons import MARKER, source_buttons, numbered_buttons
from tokmo.records.news_decision_store import kst, timestamp
from tokmo.values.ops_kernel import AlertChannel, AlertOrigin, AlertOutboxV1, AlertStatus


def _parts(body, limit=3500):
    # Telegram's bound is UTF-16 units; use a conservative codepoint bound
    # and account for astral characters rather than dropping evidence.
    parts, current, units = [], "", 0
    for char in body:
        size = 2 if ord(char) > 65535 else 1
        if units + size > limit:
            parts.append(current)
            current, units = "", 0
        current += char
        units += size
    if current:
        parts.append(current)
    return parts


def _digest_parts(body, limit=3500):
    """Keep a numbered title and its summary together within the UTF-16 bound."""
    result, current = [], ''
    blocks = re.split(r'(?m)(?=^[0-9]+\. )', body)
    for block in blocks:
        if current and len((current+block).encode('utf-16-le'))//2 > limit:
            result.append(current); current = ''
        pieces = _parts(block, limit)
        if len(pieces) > 1:
            result.extend(pieces[:-1]); current = pieces[-1]
        elif pieces:
            current += pieces[0]
    if current:
        result.append(current)
    return result


def presentation_parts(alert, body):
    """Pack citation-bearing blocks, keeping each split piece with its own links."""
    if alert.get('kind') == 'EVENING_DIGEST':
        entries = {entry['number']: numbered_buttons(entry.get('source_buttons', []), entry['number'])
                   for entry in alert.get('entries', [])}
        blocks = []
        for block in re.split(r'(?m)(?=^[0-9]+\. )', body):
            number = re.match(r'^(\d+)\. ', block)
            blocks.append((block, entries.get(int(number[1]), []) if number else []))
        if len(blocks) > 1:
            header, _ = blocks.pop(0)
            blocks[0] = (header + blocks[0][0], blocks[0][1])
    elif alert.get('kind') == 'MORNING_DIGEST':
        separator = '\n\n────────\n\n'
        blocks = [source_buttons((separator if index else '') + block, number=index+1)
                  for index, block in enumerate(body.split(separator))]
    else:
        blocks = [source_buttons(body)]
    result, current, buttons = [], '', []
    for block, links in blocks:
        for piece in _parts(block):
            if current and (len((current+piece).encode('utf-16-le'))//2 > 3500 or len(buttons+links) > 100):
                result.append((current, buttons)); current, buttons = '', []
            current += piece
            buttons += [button for button in links if button not in buttons]
    if current:
        result.append((current, buttons))
    return result


def queue_proposals(engine, ops_kernel, role, *, cutoff, activation_enabled=False, test=False, only=None):
    """``only`` restricts queueing to that one proposal (urgent publish); other events' ordinary
    proposals keep their state until the run's own publish."""
    if not activation_enabled:
        return {"queued": 0, "reason": "delivery_disabled"}
    queued = 0
    for alert in engine.pending_alerts():
        if timestamp(alert["created_at"]) < timestamp(cutoff) or (only is not None and alert["id"] != only):
            continue
        body = ("[재현테스트]\n" if test else "") + alert["body"]
        refs = []
        for index, (part, part_buttons) in enumerate(presentation_parts(alert, body)):
            outbox_id = "nda-" + identity([alert["id"], index])
            refs.append(outbox_id)
            prior = ops_kernel.read_outbox(outbox_id)
            body_evidence = (part, "news_proposal_id:" + alert["id"])
            if part_buttons:
                body_evidence += (MARKER + json.dumps(part_buttons, ensure_ascii=False, separators=(',', ':')),)
            if alert.get("source_observed_at"):
                body_evidence += ("news_observed_at:" + alert["source_observed_at"],)
            if prior is not None:
                if prior.body_evidence != body_evidence:
                    raise ValueError("decision outbox identity conflict")
                continue
            outbox = AlertOutboxV1(outbox_id, AlertOrigin.NEWS_EVENT, None, None, None, None, 0,
                alert.get("event_id") or alert["id"], "news:decision:" + outbox_id,
                AlertChannel.TELEGRAM, ('NEWS_DIGEST_' + str(alert['digest_hour'])) if alert.get('kind') == 'EVENING_DIGEST'
                    else "NEWS_IMMEDIATE" if alert.get("urgency") == "IMMEDIATE" and alert.get("importance") == "HIGH" else "ORDINARY",
                body_evidence, timestamp(alert["created_at"]),
                status=AlertStatus.PENDING)
            ops_kernel.record_news_alert_outbox(role, outbox, activation_enabled=True)
            queued += 1
        for event in engine.store.events():
            if any(item["id"] in {alert["id"], *alert.get("constituent_ids", [])} for item in event.get("pending_alerts", [])):
                event.setdefault("delivery_refs", {})[alert["id"]] = refs
                # The proposal's own evidence ids, kept at queue time so a later send baseline
                # can cite them after a newer proposal has replaced last_alert.
                evidence_ids = alert.get('evidence_ids') or alert.get('evidence_snapshots', {}).get(event['id'])
                if evidence_ids:
                    event.setdefault("queued_evidence", {})[alert["id"]] = list(evidence_ids)
                memory = alert.get('context_memory') or alert.get('context_snapshots', {}).get(event['id'])
                if memory:
                    event.setdefault('queued_context_memory', {})[alert['id']] = memory
                # Same for a digest's constituent proposals, so a digest that completes after a newer
                # proposal replaced last_alert still reports what it actually bundled.
                if alert.get("constituent_ids"):
                    event.setdefault("queued_constituents", {})[alert["id"]] = list(alert["constituent_ids"])
                if alert.get('kind') == 'EVENING_DIGEST':
                    # One immutable item per event, not a replicated archive of the entire digest.
                    entry = next((item for item in alert.get('entries', []) if item['event_id'] == event['id']), None)
                    if entry is not None:
                        details = event.get('pending_digest_details', {}).pop(entry['proposal_id'], {})
                        prior_entry = event.get('queued_digest_entries', {}).get(alert['id'], {})
                        event.setdefault('queued_digest_entries', {})[alert['id']] = dict(prior_entry, **entry, **details)
                engine.store.save_event(event)
        engine.acknowledge(alert["id"], "QUEUED")
    reconcile_deliveries(engine, ops_kernel)
    return {"queued": queued}


# Exactly the shapes the production senders produce. Group chat ids use the same grammar the
# route itself enforces (news_delivery_route.build_news_sender: fullmatch ``-[1-9][0-9]{0,19}``);
# a message_id is the Telegram integer (``int`` → ``str``), so ASCII decimal digits only.
_GROUP_CHAT_ID = re.compile(r"-[1-9][0-9]{0,19}")
_MESSAGE_ID = re.compile(r"[0-9]+")


def _message_receipt(value):
    """True only for a receipt the production senders actually produce: the bare Telegram
    message_id (dialogue sender) or exactly ``telegram-group:<chat_id>:<message_id>`` from the
    group route. Anything else — None, empty, ``'None'``, ``MOCK-…``, replay ``SIMULATED-…``,
    an empty/nonnumeric/positive chat id or extra components — is not proof of a send. This is a
    local shape check, never authentication of an external delivery."""
    if value is None:
        return False
    parts = str(value).split(":")
    if len(parts) == 1:
        return _MESSAGE_ID.fullmatch(parts[0]) is not None
    return (len(parts) == 3 and parts[0] == "telegram-group"
            and _GROUP_CHAT_ID.fullmatch(parts[1]) is not None and _MESSAGE_ID.fullmatch(parts[2]) is not None)


def _complete_send(rows):
    """Every part actually SENT with a receipt time and a real provider message receipt; else None."""
    if not rows or any(row is None or row.status is None or row.status.value != "SENT" or row.delivered_at is None for row in rows):
        return None
    if not all(_message_receipt(row.provider_message_id) for row in rows):
        return None
    return max(timestamp(row.delivered_at) for row in rows)


def delivery_issues(store, ops_kernel=None):
    """Outstanding delivery failures, including older proposals replaced by new ones."""
    issues = {}
    for event in store.events():
        for alert_id, refs in event.get('delivery_refs', {}).items():
            if alert_id in event.get('delivery_resolutions', {}):
                continue
            if ops_kernel is not None:
                rows = [ops_kernel.read_outbox(key) for key in refs]
                statuses = sorted({row.status.value if row and row.status else 'MISSING' for row in rows})
                if _complete_send(rows) is not None:
                    continue
                if not statuses or not set(statuses) <= {'PENDING', 'ATTEMPTING'}:
                    issues[alert_id] = {'alert_id': alert_id, 'event_id': event['id'],
                                        'statuses': statuses or ['MISSING'], 'outbox_refs': refs}
            else:
                states = {p['id']: p.get('state') for p in event.get('comparison_proposals', [])}
                if event.get('last_alert', {}).get('id') == alert_id:
                    states[alert_id] = event.get('last_delivery')
                if states.get(alert_id) in {'FAILED', 'UNKNOWN'}:
                    issues[alert_id] = {'alert_id': alert_id, 'event_id': event['id'],
                                        'statuses': [states[alert_id]], 'outbox_refs': refs}
    return list(issues.values())


def reconsider_failed_proposals(engine, ops_kernel, alert_ids, *, at, reason, confirmed_not_sent=False):
    """Prepare a fresh value assessment, retaining the old outbox and evidence.

    Explicit investigation is required: FAILED historically included transport
    exceptions after a request. Mixed/unknown/partly delivered messages are not
    eligible. This function neither sends nor invokes a model; use worker locks.
    """
    if not reason or not reason.strip():raise ValueError('recovery reason required')
    if not confirmed_not_sent:raise ValueError('receipt investigation: confirm not sent first')
    targets = set(alert_ids)
    known = set(); changes = []
    for event in engine.store.events():
        for alert_id in targets & event.get('delivery_refs', {}).keys():
            known.add(alert_id)
            if alert_id in event.get('delivery_resolutions', {}):continue
            rows = [ops_kernel.read_outbox(key) for key in event['delivery_refs'][alert_id]]
            if not rows or any(row is None or row.status != AlertStatus.FAILED or row.provider_message_id or row.delivered_at for row in rows):
                raise ValueError('only wholly failed, unreceipted proposals can be reconsidered')
            changes.append((event, alert_id))
    if not targets or targets - known:raise ValueError('proposal changed or absent; review current state')
    for event, alert_id in changes:
        removed = {alert_id, *event.get('queued_constituents', {}).get(alert_id, [])}
        old = [p for p in event.get('comparison_proposals', []) if p['id'] in removed]
        if event.get('last_alert', {}).get('id') in removed:
            old.append(event.pop('last_alert'))
            event.pop('last_transition', None);event['last_delivery'] = 'NOT_SENT'
        event.setdefault('retired_pending', []).extend(dict(p, retired_at=kst(at), retired_reason=reason,
            state='FAILED') for p in {p['id']: p for p in old}.values())
        event['comparison_proposals'] = [p for p in event.get('comparison_proposals', []) if p['id'] not in removed]
        event['pending_alerts'] = [p for p in event.get('pending_alerts', []) if p['id'] not in removed]
        event.setdefault('delivery_resolutions', {})[alert_id] = {'action': 'reconsider', 'at': kst(at),
            'reason': reason, 'confirmed_not_sent': True, 'outbox_refs': event['delivery_refs'][alert_id]}
        event['recovery_review_at'] = kst(at);event['next_check_at'] = kst(at)
        event['recovery_generation'] = identity([event.get('recovery_generation'), alert_id])
        engine.store.save_event(event)
    return {'reconsidered': len({key for _, key in changes}), 'events': sorted({e['id'] for e, _ in changes})}


def _sent_snapshot(event, alert_id, rows, refs, delivered):
    proposal = event.get('last_alert') if event.get('last_alert', {}).get('id') == alert_id else {}
    evidence_ids = proposal.get('evidence_ids') or event.get('queued_evidence', {}).get(alert_id)
    value = dict(alert_id=alert_id, body=''.join(row.body_evidence[0] for row in rows),
                 parts=len(rows), delivered_at=kst(delivered), outbox_refs=list(refs))
    if evidence_ids:
        value['evidence_ids'] = list(evidence_ids)
    else:
        value['evidence_snapshot'] = 'UNVERIFIED'
    constituents = proposal.get('constituent_ids') or event.get('queued_constituents', {}).get(alert_id)
    if constituents:
        value['constituent_ids'] = list(constituents)
    memory = event.get('queued_context_memory', {}).get(alert_id)
    if memory:
        value['context_memory'] = memory
    return value


def reconcile_deliveries(engine, ops_kernel):
    # 1) State of the last proposal, from its own outbox parts (unchanged contract).
    for event in engine.store.events():
        alert_id = event.get("last_alert", {}).get("id")
        refs = event.get("delivery_refs", {}).get(alert_id, [])
        rows = [ops_kernel.read_outbox(key) for key in refs]
        if not rows or any(row is None for row in rows):
            continue
        statuses = {row.status.value for row in rows}
        if statuses == {"SENT"}:
            engine.acknowledge(alert_id, "SENT")
        elif "UNKNOWN" in statuses:
            engine.acknowledge(alert_id, "UNKNOWN")
        elif "FAILED" in statuses:
            engine.acknowledge(alert_id, "FAILED")
    # 2) last_sent: the latest complete actual send among every proposal this event ever queued
    #    (delayed receipts included), advancing only to a later delivered_at. Body is the delivered
    #    text in part order; evidence ids only from the stored proposal itself, never rebuilt.
    for event in engine.store.events():
        current = event.get("last_sent")
        history = {row['alert_id']: row for row in event.get('delivered_history', [])}
        if current:
            history[current['alert_id']] = current
        latest = None
        completed = set()
        snapshots = event.get("comparison_proposals", [])
        unsent = event.get("unsent_evidence") or {}
        remaining = set(unsent.get("ids", []))
        changed = False
        for alert_id, refs in event.get("delivery_refs", {}).items():
            rows = [ops_kernel.read_outbox(key) for key in refs]
            delivered = _complete_send(rows)
            if delivered is not None:
                completed.add(alert_id)
                # Delayed older receipts matter to duplicate comparison too.
                history[alert_id] = _sent_snapshot(event, alert_id, rows, refs, delivered)
                # Creation, not receipt arrival, establishes which unsent decision this body could cover.
                # Legacy coverage/time unknown: keep the evidence rather than infer it was delivered.
                if unsent.get("observed_at") and all(timestamp(row.created_at) >= timestamp(unsent["observed_at"]) for row in rows):
                    remaining.difference_update(event.get("queued_evidence", {}).get(alert_id, []))
            else:
                statuses = {row.status.value for row in rows if row is not None and row.status is not None}
                state = "FAILED" if "FAILED" in statuses else "UNKNOWN" if "UNKNOWN" in statuses or "SENT" in statuses else "QUEUED"
                for item in snapshots:
                    if item["id"] == alert_id and item.get("state") != state:
                        item["state"] = state
                        changed = True
            if delivered is None or (current and timestamp(current["delivered_at"]) > delivered):
                continue
            # A JSON object's key order is not chronology. Break exact receipt
            # time ties by creation time, then the latest/current proposal.
            rank = (delivered, max(timestamp(row.created_at) for row in rows),
                    alert_id == event.get('last_alert', {}).get('id'),
                    alert_id == (current or {}).get('alert_id'))
            if latest and latest[0] >= rank:
                continue
            latest = (rank, delivered, alert_id, rows, list(refs))
        unresolved = [item for item in snapshots if item["id"] not in completed]
        recent = sorted(history.values(), key=lambda row: timestamp(row['delivered_at']))[-8:]
        if recent != event.get('delivered_history', []):
            event['delivered_history'] = recent
            changed = True
        # The latest proposal can have completed before a delayed older proposal became last_sent.
        # Its actual completion must not be forgotten merely because it is no longer the latest receipt.
        if event.get("last_alert", {}).get("id") in completed and event.get("completed_proposal_id") != event["last_alert"]["id"]:
            event["completed_proposal_id"] = event["last_alert"]["id"]
            changed = True
        if len(unresolved) != len(snapshots):
            event["comparison_proposals"] = unresolved
            changed = True
        if remaining != set(unsent.get("ids", [])):
            event["unsent_evidence"] = dict(unsent, ids=[key for key in unsent["ids"] if key in remaining])
            changed = True
        if latest is None:
            if changed:
                engine.store.save_event(event)
            continue
        _, delivered, alert_id, rows, refs = latest
        if event.get('last_sent') != history[alert_id]:
            event['last_sent'] = history[alert_id]
            changed = True
        if changed:
            engine.store.save_event(event)


def queue_health_notice(ops_kernel, role, report, now):
    """At most one analysis-health notice a day; the caller checks activation."""
    if night(now):
        return
    subject = "news-decision-health-" + timestamp(now).date().isoformat()
    alert_id = "ndo-" + identity(subject)
    if ops_kernel.read_outbox(alert_id) is not None:
        return
    errors = report.get("input_states", {}).get("ERROR", 0)
    unavailable = [key for key, row in report.get("sources", {}).items() if row["status"] == "UNAVAILABLE"]
    body = f"[뉴스 분석 점검 필요] 분석 보류 {errors}건."
    if unavailable:
        body += " 읽지 못한 수집 자료: " + ", ".join(unavailable) + "."
    body += "\n중요 뉴스가 없다는 뜻이 아닙니다. 분석 실패 기록을 확인해야 합니다. 자동 무한 재시도는 하지 않습니다."
    outbox = AlertOutboxV1(alert_id, AlertOrigin.NEWS_OPS, None, None, None, None, 0,
        subject, "newsops:" + subject, AlertChannel.TELEGRAM, "OPS_FAILURE", (body,), timestamp(now), status=AlertStatus.PENDING)
    ops_kernel.record_news_ops_failure_outbox(role, outbox, activation_enabled=True)
