from datetime import datetime, timedelta, timezone
import json

from tokmo.programs.news_investigation import classify_event_guarded, run_news_investigation
from tokmo.records.news_episode_store import NewsEpisodeStore
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.news_classifier import (DEFAULT_NEWS_CLASSIFIER_MODEL_ID,NewsClassifierFailure,
    ResolvedRoute,validate_classifier_route)
from tokmo.records.news_episode_store import NewsEpisodeStoreError
import pytest
from tokmo.values.ops_kernel import ProducerRole

NOW=datetime(2026,8,21,tzinfo=timezone.utc); HEX="a"*64

def _kwargs(store,episode,call,ops=None):
    return dict(store=store,episode_id=episode.episode_id,purpose="EVENT_CLASS",
        evidence={"ev1":"public"},prompt_policy_digest=HEX,model_contract_digest=HEX,
        configured_model_id=DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
        admitted_catalog={DEFAULT_NEWS_CLASSIFIER_MODEL_ID:"openai-codex"},classifier_call=call,
        now=NOW,ops_kernel=ops,role=ProducerRole.HERMES,activation_enabled=False)

def test_quota_is_typed_deferred_new_attempt_rows_and_one_ops_outbox(tmp_path):
    store=NewsEpisodeStore(tmp_path/"news.sqlite"); episode=store.upsert("quota",(),NOW)
    ops=OpsKernelStore(tmp_path/"ops"/"kernel.sqlite",stable_phase_ids=frozenset()); calls=[]
    def quota(request): calls.append(request.model_id); raise NewsClassifierFailure("QUOTA_EXCEEDED")
    results=[run_news_investigation(**_kwargs(store,episode,quota,ops)) for _ in range(10)]
    assert {result["status"] for result in results}=={"CLASSIFICATION_DEFERRED"}
    assert calls==[DEFAULT_NEWS_CLASSIFIER_MODEL_ID]*10
    assert store.connection.execute("SELECT count(*) FROM classifier_calls").fetchone()==(10,)
    assert set(r[0] for r in store.connection.execute("SELECT exit FROM classifier_calls"))=={"QUOTA_EXCEEDED"}
    assert tuple(ops._connect().execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_OPS'").fetchone())==(1,)
    assert store.connection.execute("SELECT ops_alert_id FROM classifier_outages WHERE status='OPEN'").fetchone()[0]

def test_parse_fail_retries_are_bounded_and_each_call_is_a_row(tmp_path):
    store=NewsEpisodeStore(tmp_path/"news.sqlite"); episode=store.upsert("parse",(),NOW); calls=[]
    def invalid(request): calls.append(1); return b"not-json"
    result=run_news_investigation(**_kwargs(store,episode,invalid),retry_max=2)
    assert result["status"]=="CLASSIFICATION_DEFERRED" and len(calls)==3
    assert store.connection.execute("SELECT count(*) FROM classifier_calls").fetchone()==(3,)
    assert set(r[0] for r in store.connection.execute("SELECT exit FROM classifier_calls"))=={"PARSE_FAIL"}

def test_event_class_deterministic_guards_have_both_classes():
    assert classify_event_guarded(direct_holding=True,official_primary=True,irreversible=True,
        single_source_claim=False)=="CORE_BREAKING"
    assert classify_event_guarded(direct_holding=False,official_primary=False,irreversible=False,
        single_source_claim=True)=="ORDINARY"
    assert classify_event_guarded(direct_holding=False,official_primary=False,irreversible=False,
        single_source_claim=False,systemic_transition=True,propagation_high=True)=="CORE_BREAKING"
    assert classify_event_guarded(direct_holding=False,official_primary=True,irreversible=False,
        single_source_claim=False,classifier_event_class="UNDECIDED")=="ORDINARY"
    assert classify_event_guarded(direct_holding=False,official_primary=True,irreversible=True,
        single_source_claim=False,classification_status="DEFERRED")=="CLASSIFICATION_DEFERRED"

def test_success_clears_outage_with_call_provenance_and_no_resolution_alert(tmp_path):
    store=NewsEpisodeStore(tmp_path/"news.sqlite");episode=store.upsert("recover",(),NOW)
    ops=OpsKernelStore(tmp_path/"ops"/"kernel.sqlite",stable_phase_ids=frozenset())
    store.record_outage(DEFAULT_NEWS_CLASSIFIER_MODEL_ID,"QUOTA_EXCEEDED",NOW,HEX)
    from tokmo.programs.news_investigation import _enqueue_outages
    _enqueue_outages(store,ops,ProducerRole.HERMES,activation_enabled=False,now=NOW)
    def success(request):return json.dumps({"provider":request.provider,"model_id":request.model_id,"event_class":"ORDINARY","citations":["ev1"]}).encode()
    result=run_news_investigation(**_kwargs(store,episode,success,ops))
    assert result["status"]=="OK" and store.open_outages()==()
    cleared=store.connection.execute("SELECT cleared_by_call_id FROM classifier_outages").fetchone()[0]
    assert cleared and store.connection.execute("SELECT exit FROM classifier_calls WHERE call_id=?",(cleared,)).fetchone()==("OK",)
    assert tuple(ops._connect().execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_OPS'").fetchone())==(1,)
    def quota(request):raise NewsClassifierFailure("QUOTA_EXCEEDED")
    retry=_kwargs(store,episode,quota,ops);retry["evidence"]={"ev2":"new public evidence"};retry["now"]=NOW+timedelta(seconds=1)
    assert run_news_investigation(**retry)["exit"]=="QUOTA_EXCEEDED"
    assert store.connection.execute("SELECT count(*) FROM classifier_outages").fetchone()==(2,)
    assert tuple(ops._connect().execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_OPS'").fetchone())==(2,)

def test_run_start_config_readback_invalid_is_fail_closed_outage(tmp_path):
    store=NewsEpisodeStore(tmp_path/"news.sqlite");episode=store.upsert("config",(),NOW)
    ops=OpsKernelStore(tmp_path/"ops"/"kernel.sqlite",stable_phase_ids=frozenset())
    config=tmp_path/"news_classifier_model.json";config.write_text('{"model_id":"BAD MODEL"}')
    called=[]
    result=run_news_investigation(**_kwargs(store,episode,lambda request:called.append(request),ops),config_path=config)
    assert result["status"]=="CLASSIFICATION_DEFERRED" and result["exit"]=="CONFIG_INVALID" and called==[]
    assert store.connection.execute("SELECT failure_class FROM classifier_outages").fetchone()==("CONFIG_INVALID",)

def test_route_always_resolved_and_clear_paths_are_typed(tmp_path):
    model=DEFAULT_NEWS_CLASSIFIER_MODEL_ID
    assert isinstance(validate_classifier_route(model,{model:"openai-codex"}),ResolvedRoute)
    store=NewsEpisodeStore(tmp_path/"news.sqlite")
    store.record_outage(model,"CONFIG_INVALID",NOW)
    assert store.clear_config_invalid_outages(now=NOW)==1
    store.record_outage(model,"QUOTA_EXCEEDED",NOW)
    with pytest.raises(NewsEpisodeStoreError):store.clear_open_outages("ncl-missing",now=NOW)

def test_route_requires_mapping_catalog_and_never_fabricates_provider(tmp_path):
    model=DEFAULT_NEWS_CLASSIFIER_MODEL_ID
    for catalog in ({model},model):
        with pytest.raises(Exception):validate_classifier_route(model,catalog)
    store=NewsEpisodeStore(tmp_path/"news.sqlite");episode=store.upsert("route-provider",(),NOW)
    def success(request):return json.dumps({"provider":request.provider,"model_id":request.model_id,"citations":["ev1"]})
    result=run_news_investigation(**(_kwargs(store,episode,success)|{"admitted_catalog":{model:"actual-provider"}}))
    assert result["status"]=="OK"
    assert store.connection.execute("SELECT provider FROM classifier_calls").fetchone()==("actual-provider",)

def test_input_digest_mismatch_and_durable_daily_cap_call_provider_zero(tmp_path):
    store=NewsEpisodeStore(tmp_path/"news.sqlite");episode=store.upsert("budget",(),NOW);called=[]
    mismatch=run_news_investigation(**_kwargs(store,episode,lambda request:called.append(request)),
        expected_input_evidence_digest="0"*64)
    assert mismatch["exit"]=="INPUT_DIGEST_MISMATCH" and called==[]
    for attempt in range(1,97):
        call={"call_id":f"ncl-budget-{attempt}","classification_identity":f"{attempt:064x}",
            "attempt_no":1,"purpose":"EVENT_CLASS","episode_id":episode.episode_id,"provider":"openai-codex",
            "model_id":DEFAULT_NEWS_CLASSIFIER_MODEL_ID,"model_contract_digest":HEX,
            "prompt_policy_digest":HEX,"input_evidence_digest":HEX,"output_schema_id":"news-classifier-output-v1"}
        store.plan_classifier_call(call,now=NOW)
    capped=run_news_investigation(**_kwargs(store,episode,lambda request:called.append(request)))
    assert capped["exit"]=="MODEL_BUDGET_EXCEEDED" and called==[]

def test_single_config_model_swap_creates_identity_and_preserves_spark_history(tmp_path):
    store=NewsEpisodeStore(tmp_path/"news.sqlite");episode=store.upsert("swap",(),NOW)
    config=tmp_path/"news_classifier_model.json"
    catalog={DEFAULT_NEWS_CLASSIFIER_MODEL_ID:"openai-codex","approved-v2":"provider-v2"}
    def invoke(request):return json.dumps({"provider":request.provider,"model_id":request.model_id,"citations":["ev1"]})
    config.write_text(json.dumps({"model_id":DEFAULT_NEWS_CLASSIFIER_MODEL_ID}))
    first=run_news_investigation(**(_kwargs(store,episode,invoke)|{"admitted_catalog":catalog}),config_path=config)
    before=tuple(store.connection.execute("SELECT classification_identity,provider,model_id,model_contract_digest FROM classifier_calls"))
    config.write_text(json.dumps({"model_id":"approved-v2"}))
    second=run_news_investigation(**(_kwargs(store,episode,invoke)|{"admitted_catalog":catalog,"model_contract_digest":"c"*64}),config_path=config)
    after=tuple(store.connection.execute("SELECT classification_identity,provider,model_id,model_contract_digest FROM classifier_calls"))
    assert first["classification_identity"]!=second["classification_identity"]
    assert before[0] in after and {row[2] for row in after}=={DEFAULT_NEWS_CLASSIFIER_MODEL_ID,"approved-v2"}

def test_w6_outage_alert_converges_across_different_clocks(tmp_path):
    from datetime import timedelta
    from tokmo.programs.news_investigation import _enqueue_outages
    store=NewsEpisodeStore(tmp_path/"news.sqlite")
    ops=OpsKernelStore(tmp_path/"ops"/"kernel.sqlite",stable_phase_ids=frozenset())
    store.record_outage(DEFAULT_NEWS_CLASSIFIER_MODEL_ID,"QUOTA_EXCEEDED",NOW,HEX)
    original=store.bind_outage_alert
    store.bind_outage_alert=lambda *args,**kwargs:None
    assert _enqueue_outages(store,ops,ProducerRole.HERMES,activation_enabled=False,now=NOW)==1
    store.bind_outage_alert=original
    assert _enqueue_outages(store,ops,ProducerRole.HERMES,activation_enabled=False,
        now=NOW+timedelta(minutes=5))==1
    assert tuple(ops._connect().execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_OPS'").fetchone())==(1,)
