"""Deterministic NEWS admission; activation defaults off at composition."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
from zoneinfo import ZoneInfo
from tokmo.values.news_config import (MORNING_RELEASE_KST,NIGHT_HOLD_START_KST,
    TRUSTED_BREAKING_SOURCE_IDS)
from tokmo.values.ops_kernel import AlertChannel, AlertOrigin, AlertOutboxV1, AlertStatus

class NewsAdmissionRejected(ValueError):pass


@dataclass(frozen=True)
class AdmissionInput:
    episode_id: str
    revision: int
    event_class: str
    material_change: str | None
    observed_at: datetime
    cutoff: datetime
    muted: bool
    activation_enabled: bool
    admitted_at: datetime | None = None
    prior_reaction: str | None = None
    prior_material_change: str | None = None
    parent_episode_id: str | None = None
    # A classifier label alone is insufficient for overnight interruption.
    # The producer must separately prove user/portfolio materiality.
    immediate_eligible: bool = True


def admit_early_factual(*, source_id, event_class, unknowns, checkpoint):
    if (source_id not in TRUSTED_BREAKING_SOURCE_IDS or event_class != "CORE_BREAKING"
            or not unknowns or not str(checkpoint).strip()): return None
    return {"revision_kind":"EARLY_FACTUAL","provisional":True,
            "label":"잠정/미확인","unknowns":tuple(unknowns),"checkpoint":checkpoint}

def record_early_factual_revision(store,episode_id,*,source_id,unknowns,checkpoint,body,
        portfolio_context_digest,links,confidence,now):
    if admit_early_factual(source_id=source_id,event_class="CORE_BREAKING",unknowns=unknowns,checkpoint=checkpoint) is None:return None
    store.mark_early_alerted(episode_id,now=now)
    return store.admit_alert_revision({"episode_id":episode_id,"revision_no":1,"revision_kind":"EARLY_FACTUAL",
        "body_text":body,"portfolio_context_digest":portfolio_context_digest,"confidence":confidence,
        "material_change_kind":"FIRST_ADMISSION","material_change_observed_at":now,
        "links":links},now=now)

def record_correction_revision(store,episode_id,*,body,portfolio_context_digest,links,confidence,now):
    revision_no=store.next_alert_revision_no(episode_id)
    store.mark_disconfirmed(episode_id,now=now)
    return store.admit_alert_revision({"episode_id":episode_id,"revision_no":revision_no,"revision_kind":"CORRECTION",
        "body_text":body,"portfolio_context_digest":portfolio_context_digest,"confidence":confidence,
        "material_change_kind":"RETRACTION_OR_CORRECTION","material_change_observed_at":now,
        "links":links},now=now)


def admit_news_event(item: AdmissionInput) -> dict:
    identity = f"{item.episode_id}:{item.revision}:{item.material_change}"
    digest = hashlib.sha256(identity.encode()).hexdigest()
    base={"alert_id":"nal-"+digest[:24],"dedupe_key":"news:"+digest,"channel":"SHADOW",
          "status":"REJECTED","body":identity,"acknowledges_prior_reaction":False}
    if item.parent_episode_id is not None:
        base=base|{"body":"기존 mute와 별개인 systemic 신규 사건; 신규 정체성 근거: "+identity}
    if item.observed_at < item.cutoff:
        return base|{"status":"REJECTED_CUTOFF"}
    if item.muted:
        return base|{"status":"REJECTED_MUTED"}
    if item.material_change is None:
        return base|{"status":"REJECTED_NO_CHANGE"}
    if item.material_change == item.prior_material_change:
        return base|{"status":"REJECTED_NO_CHANGE"}
    if item.prior_reaction == "NOT_HELPFUL":
        base=base|{"acknowledges_prior_reaction":True,
            "body":"이전 알림이 도움되지 않았다는 반응을 반영함; 새 변화: "+identity}
    admission_time=item.admitted_at or item.observed_at
    hour = admission_time.astimezone(ZoneInfo("Asia/Seoul")).hour
    night = hour >= NIGHT_HOLD_START_KST or hour < MORNING_RELEASE_KST
    held = night and (item.event_class == "ORDINARY" or not item.immediate_eligible)
    if not item.activation_enabled:
        return base|{"status":"SHADOW_HELD_MORNING" if held else "SHADOW"}
    return base|{"channel":"TELEGRAM","status":"HELD_MORNING" if held else "PENDING"}


def _outbox_from_revision(row, *, activation_enabled, now):
    channel=AlertChannel.TELEGRAM if activation_enabled else AlertChannel.SHADOW
    return AlertOutboxV1("nal-"+row["alert_revision_id"][:24],AlertOrigin.NEWS_EVENT,
        None,None,None,None,0,row["episode_id"],f'news:{row["episode_id"]}:{row["revision_no"]}',
        channel,row["event_class"],(row["body_text"],"cb:"+row["callback_ref"]),
        datetime.fromisoformat(row["created_at"]),
        status=AlertStatus.PENDING if channel is AlertChannel.TELEGRAM else None)


def _telegram_eligible(row, telegram_cutoff):
    """Approval packet §2.3: pre-cutoff evidence/backlog never becomes
    Telegram. Rows with a missing or unparseable observed timestamp fail
    closed to SHADOW."""
    if telegram_cutoff is None:
        return True
    raw=row.get("material_change_observed_at")
    if type(raw) is not str:
        return False
    try:
        observed=datetime.fromisoformat(raw.replace("Z","+00:00"))
    except ValueError:
        return False
    if observed.tzinfo is None or observed.utcoffset() is None:
        return False
    return observed >= telegram_cutoff


def reconcile_news_outbox(*,store,ops_kernel,role,activation_enabled,now,telegram_cutoff=None):
    changed=0
    for row in store.list_pending_outbox_revisions():
        row_enabled=activation_enabled and _telegram_eligible(row,telegram_cutoff)
        outbox=_outbox_from_revision(row,activation_enabled=row_enabled,now=now)
        ops_kernel.record_news_alert_outbox(role,outbox,activation_enabled=row_enabled)
        store.mark_revision_outboxed(row["alert_revision_id"],outbox.alert_id,now=now)
        changed+=1
    return changed


def run_news_event_admission(*,store,ops_kernel,role,draft,activation_enabled,now,telegram_cutoff=None,inject_crash_at=None):
    if store.episode_state(draft["episode_id"])=="MUTED":raise NewsAdmissionRejected("muted episode")
    revision_id=store.admit_alert_revision(draft,now=now)
    if inject_crash_at=="after_tx_c":raise RuntimeError("injected after TX-C")
    reconcile_news_outbox(store=store,ops_kernel=ops_kernel,role=role,
        activation_enabled=activation_enabled,telegram_cutoff=telegram_cutoff,now=now)
    return revision_id
