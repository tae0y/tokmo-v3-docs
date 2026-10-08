import hashlib
import json
from pathlib import Path

from tokmo.values.ai_infra_capex import (
    CLASSIFIER_TABLE_BYTES_V1, CLASSIFIER_TABLE_DIGEST_V1,
    CLASSIFIER_TABLE_OBJECT_V1, SPEC_BYTES_V1, SPEC_DIGEST_V1,
    SPEC_OBJECT_V1, canonical_json_bytes,
    AllMatchPolarityStoryV1, AllMatchPolarityAggregateV1,
    validate_semantic_member_cap_v1, validate_pack_bytes_v1,
)
from tokmo.records.external_news_query import _classify
from tokmo.values.ai_infra_capex import ClaimantKindV1
import copy
import pytest
import unicodedata


def test_frozen_classifier_and_spec_digests_are_computed_from_constants():
    assert hashlib.sha256(CLASSIFIER_TABLE_BYTES_V1).hexdigest() == CLASSIFIER_TABLE_DIGEST_V1
    assert CLASSIFIER_TABLE_DIGEST_V1 == "efef7c0fb6ae79a4fa89920ad93daf4d82462c60291e2a07fd8434850d4c3b31"
    assert hashlib.sha256(SPEC_BYTES_V1).hexdigest() == SPEC_DIGEST_V1
    assert SPEC_DIGEST_V1 == "d403c8444bffc7e5ca958e5293ee511488fdc7743c03c863b7d8ce9353844c15"
    mirror = json.loads(Path("tests/fixtures/ai_infra_capex/term_groups_v1.json").read_text())
    assert mirror == CLASSIFIER_TABLE_OBJECT_V1


# S13/S15, Q11: normalization/classification rule combinations use the adapter seam.
@pytest.mark.parametrize("text,role,polarities,claimant,ambiguous,numeric", [
    ("GPU overview","FACT_DISCOVERY",{"CONTEXT"},"UNSPECIFIED",False,False),
    ("company GPU increase","FACT_DISCOVERY",{"SUPPORT"},"COMPANY",False,False),
    ("supplier GPU cut","FACT_DISCOVERY",{"COUNTER"},"SUPPLIER",False,False),
    ("analyst GPU increase cut","OPINION_DISCOVERY",{"SUPPORT","COUNTER"},"ANALYST_OR_OPINION",False,False),
    ("company supplier GPU overview","FACT_DISCOVERY",{"CONTEXT"},"UNSPECIFIED",True,False),
    ("company GPU capex overview","FACT_DISCOVERY",{"CONTEXT"},"COMPANY",False,False),
    ("company GPU 10 overview","FACT_DISCOVERY",{"CONTEXT"},"COMPANY",False,False),
    ("company GPU capex 10 billion","FACT_DISCOVERY",{"CONTEXT"},"COMPANY",False,True),
    ("회사 ＧＰＵ\t투자   축소 10 조원","FACT_DISCOVERY",{"SUPPORT","COUNTER"},"COMPANY",False,True),
])
def test_classifier_rules_exact(text,role,polarities,claimant,ambiguous,numeric):
    matches,kind,is_ambiguous,has_numeric=_classify(" ".join(unicodedata.normalize("NFKC",text).split()).casefold(),role)
    assert {m.polarity.value for m in matches}==polarities
    assert kind is ClaimantKindV1(claimant) and is_ambiguous is ambiguous and has_numeric is numeric


def _mutations(value):
    for key in value:
        changed=copy.deepcopy(value)
        item=changed[key]
        if isinstance(item,list): item.append("counterfactual")
        elif isinstance(item,dict): item["counterfactual"]="changed"
        elif isinstance(item,int): changed[key]=item+1
        else: changed[key]=str(item)+"-changed"
        yield key,changed


# LC-C06/S15: every top-level bound semantic component changes its canonical digest.
def test_each_classifier_and_spec_component_is_digest_bound():
    for key,changed in _mutations(CLASSIFIER_TABLE_OBJECT_V1):
        assert hashlib.sha256(canonical_json_bytes(changed)).hexdigest()!=CLASSIFIER_TABLE_DIGEST_V1,key
    for key,changed in _mutations(SPEC_OBJECT_V1):
        assert hashlib.sha256(canonical_json_bytes(changed)).hexdigest()!=SPEC_DIGEST_V1,key


# D-DIGEST-01/02: delta preimage and the seven superseding fields are authoritative.
def test_delta_spec_digest_and_exact_supersession_fields():
    assert SPEC_DIGEST_V1=="d403c8444bffc7e5ca958e5293ee511488fdc7743c03c863b7d8ce9353844c15"
    eligibility=SPEC_OBJECT_V1["eligibility"]
    for key in ("claimant_aggregate_rule","numeric_aggregate_rule","omitted_summary_rule","polarity_semantic_rule","semantic_member_cap_rule","matched_output_cap_state_rule"):
        changed=copy.deepcopy(SPEC_OBJECT_V1);changed["eligibility"][key]+="-mutated"
        assert hashlib.sha256(canonical_json_bytes(changed)).hexdigest()!=SPEC_DIGEST_V1
    changed=copy.deepcopy(SPEC_OBJECT_V1);changed["state_policy"]["construction_backstop"]+="-mutated"
    assert hashlib.sha256(canonical_json_bytes(changed)).hexdigest()!=SPEC_DIGEST_V1
    assert hashlib.sha256(CLASSIFIER_TABLE_BYTES_V1).hexdigest()=="efef7c0fb6ae79a4fa89920ad93daf4d82462c60291e2a07fd8434850d4c3b31"
    import tokmo.values.ai_infra_capex as values_module
    assert not hasattr(values_module,"_SPEC_JSON")


def test_delta_values_layer_member_and_pack_cap_plus_one_fail_construction():
    validate_semantic_member_cap_v1(8,temporal_rows=1)
    with pytest.raises(ValueError,match="semantic member cap"):validate_semantic_member_cap_v1(9,temporal_rows=1)
    validate_pack_bytes_v1(131072)
    with pytest.raises(ValueError,match="pack byte cap"):validate_pack_bytes_v1(131073)


# M1-DIGEST/M1-ORDER: complete v0.2 policy is canonical and mutation-bound.
def test_m1_v02_spec_reason_order_and_nested_digest_binding():
    from tokmo.values.ai_infra_capex import MissingReasonCodeV1
    assert SPEC_DIGEST_V1=="d403c8444bffc7e5ca958e5293ee511488fdc7743c03c863b7d8ce9353844c15"
    assert tuple(x.value for x in MissingReasonCodeV1)==("NO_LOCAL_MATCH","LOW_SOURCE_DIVERSITY","LOCAL_COVERAGE_MINIMUM_UNMET","STALE_LOCAL_NEWS","QUERY_FAILED","ATTRIBUTED_AMOUNT_UNVERIFIED","SUPPLIER_CLAIM_NEEDS_BUYER_CONFIRMATION","ANALYST_CLAIM_UNVERIFIED","CONTRADICTION_UNRESOLVED","ALLOCATION_PERIOD_OR_CURRENCY_MISSING")
    policy=SPEC_OBJECT_V1["missing_reason_policy"]
    for key,value in policy.items():
        changed=copy.deepcopy(SPEC_OBJECT_V1)
        if isinstance(value,list):changed["missing_reason_policy"][key]=list(reversed(value))
        elif isinstance(value,dict):changed["missing_reason_policy"][key][next(iter(value))]+="-mutated"
        else:changed["missing_reason_policy"][key]+="-mutated"
        assert hashlib.sha256(canonical_json_bytes(changed)).hexdigest()!=SPEC_DIGEST_V1
    assert hashlib.sha256(CLASSIFIER_TABLE_BYTES_V1).hexdigest()=="efef7c0fb6ae79a4fa89920ad93daf4d82462c60291e2a07fd8434850d4c3b31"


def test_seed_and_expected_vector_fixtures_are_nontrivial_and_digest_pinned():
    root=Path("tests/fixtures/ai_infra_capex");seed=json.loads((root/"news_n5_seed_v1.json").read_text());expected=json.loads((root/"expected_packs_v1.json").read_text())
    assert len(seed["rows"])==7 and any("revision" in row for row in seed["rows"])
    joined=" ".join(row["title"]+" "+row["summary"] for row in seed["rows"])
    assert "네트워크" in joined and "GPU" in joined and "Analyst" in joined and "increase" in joined and "축소" in joined
    assert expected["vector_count"]==len(expected["vectors"])==1
    for vector in expected["vectors"]:
        for name in ("query_digest","source_snapshot_digest","all_match_semantic_digest","pack_digest"):
            assert len(vector[name])==64 and set(vector[name])<={*"0123456789abcdef"} and vector[name]!="f"*64
