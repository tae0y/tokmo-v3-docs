from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tokmo.programs.news_event_admission import AdmissionInput, admit_early_factual, admit_news_event
from tokmo.values.news_payload import NewsPayloadError, validate_news_payload
from tokmo.records.news_episode_store import NewsEpisodeStore

UTC=timezone.utc

def test_night_axis_boundaries_and_prior_reaction_acknowledgement():
    base=AdmissionInput("nep-1",2,"ORDINARY","NEW_TRANSMISSION_PATH",
        datetime(2026,8,21,12,tzinfo=UTC),datetime(2026,8,20,tzinfo=UTC),False,True,
        admitted_at=datetime(2026,8,21,13,tzinfo=UTC),prior_reaction="NOT_HELPFUL") # 22:00 KST
    held=admit_news_event(base)
    assert held["status"]=="HELD_MORNING"
    assert held["acknowledges_prior_reaction"] is True
    assert "이전 알림" in held["body"]
    assert admit_news_event(replace(base,admitted_at=datetime(2026,8,21,12,59,tzinfo=UTC)))["status"]=="PENDING"
    assert admit_news_event(replace(base,admitted_at=datetime(2026,8,21,23,tzinfo=UTC)))["status"]=="PENDING" # 08:00

def test_payload_sections_privacy_links_early_and_action_contract():
    sections={str(i):f"section {i}" for i in range(1,11)}
    validate_news_payload(sections=sections,links=("https://example.com/a",),callback_data="n1:u:"+"a"*32,
                          revision_kind="INITIAL",provisional=False)
    with pytest.raises(NewsPayloadError):
        validate_news_payload(sections=sections|{"4":"account_id=KR-123 /Users/me secret_token=abc"},
            links=("https://example.com",),callback_data="n1:u:"+"a"*32,
            revision_kind="INITIAL",provisional=False)
    with pytest.raises(NewsPayloadError):
        validate_news_payload(sections=sections,links=("https://example.com",),callback_data="n1:u:"+"a"*32,
            revision_kind="EARLY_FACTUAL",provisional=False)
    action=sections|{"6":"material action condition MET; 매도 범위 10-20%; 사용자 확인 요청"}
    validate_news_payload(sections=action,links=("https://example.com",),callback_data="n1:d:"+"a"*32,
        revision_kind="ACTION_DRAFT",provisional=False)

def test_three_night_holds_release_as_one_morning_coalesced(tmp_path):
    store=NewsEpisodeStore(tmp_path/"news.sqlite"); episode=store.upsert("night",(),datetime(2026,8,21,tzinfo=UTC))
    for revision in range(1,4):
        store.hold_morning_revision(episode.episode_id,revision,f"사건: 야간 상태 {revision}",now=datetime(2026,8,21,18+revision,tzinfo=UTC),release_not_before=datetime(2026,8,21,23,tzinfo=UTC))
    released=store.release_morning_holds(now_kst=datetime(2026,8,22,8,tzinfo=timezone.utc).astimezone())
    assert len(released)==1 and released[0]["revision_kind"]=="MORNING_COALESCED"
    assert store.connection.execute("SELECT count(*) FROM alert_revisions WHERE delivery_state='SUPPRESSED'").fetchone()==(3,)

def test_early_factual_requires_trusted_source_and_checkpoint():
    early=admit_early_factual(source_id="bbc",event_class="CORE_BREAKING",unknowns=("official confirmation",),checkpoint="independent source")
    assert early["revision_kind"]=="EARLY_FACTUAL" and early["provisional"] is True
    assert admit_early_factual(source_id="unknown-blog",event_class="CORE_BREAKING",unknowns=("x",),checkpoint="y") is None
