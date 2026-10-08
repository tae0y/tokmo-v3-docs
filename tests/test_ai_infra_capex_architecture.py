import ast
from pathlib import Path
import subprocess
import inspect


BASE = "be96b100eb3547eecdb2716cd59ca5408632fe1c"
IMPLEMENTATION_COMMIT = "beb0e6fe9bfbaac4f908ab2995ae2befda8b359e"
FILES = (Path("src/tokmo/values/ai_infra_capex.py"), Path("src/tokmo/records/external_news_query.py"), Path("src/tokmo/programs/ai_infra_capex_allocation.py"))

# Closed-SDS traceability map. Each target is a behavioral production-entry or
# exact adapter-seam test; architecture checks that none silently disappears.
SDS_ID_MAP = {
    "LC-C01":"test_public_fixture_vector_lifecycle_and_exact_pack", "LC-C02":"test_public_fixture_vector_lifecycle_and_exact_pack", "LC-C03":"test_public_fixture_vector_lifecycle_and_exact_pack", "LC-C04":"test_public_fixture_vector_lifecycle_and_exact_pack", "LC-C05":"test_lifecycle_renewal_and_wrong_period_reject_before_query", "LC-C06":"test_each_classifier_and_spec_component_is_digest_bound",
    "Q01":"test_exact_n5_schema_variants_reject", "Q02":"test_path_symlink_hardlink_and_public_mode_fail_closed", "Q03":"test_temporal_boundaries_revision_availability_and_snapshot_binding", "Q04":"test_temporal_boundaries_revision_availability_and_snapshot_binding", "Q05":"test_vacuum_rowid_permutation_is_semantically_invariant", "Q06":"test_content_hash_story_dedupe_unicode_boundary_and_no_text", "Q07":"test_public_fixture_vector_lifecycle_and_exact_pack", "Q08":"test_more_than_2000_unrelated_plus_five_relevant_returns_all_five", "Q09":"test_authorizer_allowlist_and_normative_sequence_are_pinned", "Q10":"test_public_fixture_vector_lifecycle_and_exact_pack", "Q11":"test_classifier_rules_exact", "Q12":"test_content_hash_story_dedupe_unicode_boundary_and_no_text", "Q13":"test_temporal_boundaries_revision_availability_and_snapshot_binding", "Q14":"test_busy_locked_is_prompt_and_sidecar_free", "Q15":"test_every_timestamp_column_malformed_fails", "Q16":"test_isolated_integrity_and_truthful_full_scan_plan", "Q17":"test_all_match_semantics_beyond_row_200_cannot_weaken_state", "Q18":"test_all_match_semantics_beyond_row_200_cannot_weaken_state", "Q19":"test_all_match_semantics_beyond_row_200_cannot_weaken_state", "Q20":"test_isolated_integrity_and_truthful_full_scan_plan",
    "S01":"test_state_coverage_freshness_and_typed_failure_semantics", "S02":"test_all_match_semantics_beyond_row_200_cannot_weaken_state", "S03":"test_classifier_rules_exact", "S04":"test_content_hash_story_dedupe_unicode_boundary_and_no_text", "S05":"test_state_coverage_freshness_and_typed_failure_semantics", "S06":"test_all_match_semantics_beyond_row_200_cannot_weaken_state", "S07":"test_public_pack_and_nested_values_reject_counterfactual_mutations", "S08":"test_public_pack_and_nested_values_reject_counterfactual_mutations", "S09":"test_all_match_semantics_beyond_row_200_cannot_weaken_state", "S10":"test_state_coverage_freshness_and_typed_failure_semantics", "S11":"test_public_pack_and_nested_values_reject_counterfactual_mutations", "S12":"test_public_fixture_vector_lifecycle_and_exact_pack", "S13":"test_classifier_rules_exact", "S14":"test_leaf_imports_exclude_network_runtime_and_raw_classifier_in_program", "S15":"test_each_classifier_and_spec_component_is_digest_bound", "S16":"test_evidence_40_41_and_missing_need_16_17_boundaries", "S17":"test_evidence_40_41_and_missing_need_16_17_boundaries", "S18":"test_evidence_40_41_and_missing_need_16_17_boundaries",
    "D-C1-01":"test_delta_polarity_member_matrix_outside_output","D-C1-02":"test_delta_multirow_story_union_max_weight_and_independent_polarities","D-C1-03":"test_delta_multirow_story_union_max_weight_and_independent_polarities","Q18-delta":"test_all_match_semantics_beyond_row_200_cannot_weaken_state","Q19-delta":"test_delta_physical_permutation_preserves_members_omissions_and_pack","S16-delta":"test_delta_member_projection_digest_mutations_and_contradiction_omission","S10-D01":"test_delta_output_truncation_matches_unlimited_production_aggregate_oracle","S10-D02":"test_delta_output_truncation_matches_unlimited_production_aggregate_oracle","S10-D03":"test_delta_raw_cap_is_insufficient_and_pack_construction_backstop_has_no_pack","S10-D04":"test_state_coverage_freshness_and_typed_failure_semantics","S10-D05":"test_delta_raw_cap_is_insufficient_and_pack_construction_backstop_has_no_pack","D-DIGEST-01":"test_delta_spec_digest_and_exact_supersession_fields","D-DIGEST-02":"test_delta_spec_digest_and_exact_supersession_fields","D-PRIV-01":"test_content_hash_story_dedupe_unicode_boundary_and_no_text",
    "M1-RED":"test_m1_exact_two_story_coverage_fallback_public_path","M1-EACH":"test_m1_each_insufficient_cause_zero_and_truthful_coexistence","M1-NONOK":"test_m1_every_non_ok_status_emits_query_failed_only","M1-ZERO":"test_m1_each_insufficient_cause_zero_and_truthful_coexistence","M1-ORDER":"test_m1_v02_spec_reason_order_and_nested_digest_binding","M1-STRICT":"test_m1_fallback_value_shape_and_pack_cross_links_are_strict","M1-DIGEST":"test_m1_v02_spec_reason_order_and_nested_digest_binding","M1-VECTOR":"test_m1_v02_public_vector_repeat_bytes","M1-CAPS":"test_m1_fallback_coexists_with_stable_16_cap_and_omitted_summary",
}


def test_leaf_imports_exclude_network_runtime_and_raw_classifier_in_program():
    forbidden = {"requests", "httpx", "urllib.request", "socket", "subprocess", "composition", "telegram", "event", "orders", "accounts", "providers", "presentation", "runtime"}
    for path in FILES:
        tree = ast.parse(path.read_text())
        imports = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)} | {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not any(any(x in item for x in forbidden) for item in imports)
    program = FILES[2].read_text()
    program_imports={n.module or "" for n in ast.walk(ast.parse(program)) if isinstance(n,ast.ImportFrom)}
    assert not any(".facts" in item or ".records" in item for item in program_imports)
    assert "CAPEX_TERM_GROUPS_V1" not in program and "raw_text" not in program
    query=FILES[1].read_text().casefold()
    assert "foreign_key_check" not in query and "quick_check" not in query and "integrity_check" not in query
    assert not any(token in query for token in ("insert into","update news_documents","delete from","create table","attach database","journal_mode="))


def test_exact_ten_file_change_seal_and_hot_files_equal_base():
    allowed = {
        "src/tokmo/values/ai_infra_capex.py", "src/tokmo/records/external_news_query.py",
        "src/tokmo/programs/ai_infra_capex_allocation.py", "tests/test_ai_infra_capex_values.py",
        "tests/test_external_news_query.py", "tests/test_ai_infra_capex_allocation.py",
        "tests/test_ai_infra_capex_architecture.py", "tests/fixtures/ai_infra_capex/term_groups_v1.json",
        "tests/fixtures/ai_infra_capex/news_n5_seed_v1.json", "tests/fixtures/ai_infra_capex/expected_packs_v1.json",
    }
    prefix = subprocess.run(["git", "rev-parse", "--show-prefix"], check=True, text=True, capture_output=True).stdout.strip()
    subprocess.run(["git", "merge-base", "--is-ancestor", IMPLEMENTATION_COMMIT, "HEAD"], check=True)
    tracked = subprocess.run(["git", "diff", "--name-only", BASE, IMPLEMENTATION_COMMIT, "--", "."], check=True, text=True, capture_output=True).stdout.splitlines()
    changed = {name.removeprefix(prefix) for name in tracked}
    assert changed == allowed
    hot = ["src/tokmo/records/external_news.py", "src/tokmo/values/signal_program_lifecycle.py"]
    for name in hot:
        # Seal this historical implementation, not all later approved work.
        closed_bytes = subprocess.run(
            ["git", "show", f"{IMPLEMENTATION_COMMIT}:{prefix}{name}"],
            check=True, capture_output=True,
        ).stdout
        assert closed_bytes == subprocess.run(["git", "show", f"{BASE}:{prefix}{name}"], check=True, capture_output=True).stdout

    composition = "src/tokmo/composition.py"
    baseline = subprocess.run(
        ["git", "show", f"{BASE}:{prefix}{composition}"], check=True,
        text=True, capture_output=True,
    ).stdout
    class AdmitSeamScrubber(ast.NodeTransformer):
        def visit_FunctionDef(self, node):
            # G1-C admits stock_candles_fetch; G2.5 admits calendar_fetch;
            # Quant G1A admits the reviewed effect-zero fundamentals seam.
            # The FRED on-demand → Macro caller delta admits
            # build_macro_runtime_deps, which gained ~8 lines of FRED wiring.
            if node.name in (
                "stock_candles_fetch",
                "calendar_fetch",
                "build_pulse_production_live_deps",
                "build_fundamental_materialization_deps",
                "build_macro_runtime_deps",
                "build_production_cadence_deps",
                "execute_official_membership_sic_capture",
                "execute_official_membership_canary",
                "membership_fetch",
                "sec_user_agent",
            ):
                return None
            return self.generic_visit(node)

        def visit_ImportFrom(self, node):
            if node.module == "tokmo.watchlists":
                node.names = [alias for alias in node.names if alias.name != "WatchlistError"]
                return node
            # The G3 telegram-activation delta admits the reviewed dialogue
            # sender transport import plus the shadow-lifecycle env-name
            # constants (activation SDS v0.2 §4); wiring lives entirely in
            # the already-admitted build_pulse_production_live_deps seam.
            if node.module in {
                "tokmo.facts.official_membership_live",
                "tokmo.facts.quant_membership",
                "tokmo.programs.quant_membership",
                "tokmo.values.official_membership_observation",
                "tokmo.facts.quant_recurring_membership",
                "tokmo.facts.telegram_dialogue_live",
                "tokmo.observability.shadow_lifecycle",
                "tokmo.records.ops_kernel",
            }:
                return None
            return self.generic_visit(node)

        def visit_ExceptHandler(self, node):
            if isinstance(node.type, ast.Name) and node.type.id == "WatchlistError":
                return None
            if isinstance(node.type, ast.Name) and node.type.id in {"OSError", "ValueError"}:
                reason = ast.dump(node, include_attributes=False)
                if "production_preflight_" in reason:
                    return None
            if isinstance(node.type, ast.Name) and node.type.id == "ConfigError":
                node.type = ast.Tuple(
                    elts=[ast.Name(id=name, ctx=ast.Load()) for name in (
                        "ConfigError", "OSError", "ValueError",
                    )],
                    ctx=ast.Load(),
                )
                return node
            return self.generic_visit(node)

        def visit_keyword(self, node):
            if node.arg in (
                "stock_candles_fetch", "calendar_fetch",
                "membership_fetch", "sec_user_agent", "sic_snapshot_loader",
            ):
                return None
            return self.generic_visit(node)

        def visit_Assign(self, node):
            if any(
                isinstance(target, ast.Name)
                and target.id == "membership_transport"
                for target in node.targets
            ):
                return None
            return self.generic_visit(node)

    def normalized(source):
        tree = AdmitSeamScrubber().visit(ast.parse(source))
        ast.fix_missing_locations(tree)
        return ast.dump(tree, include_attributes=False)

    # Preserve every historical top-level import/symbol/effect structurally;
    # admit only the reviewed nested fetch function and deps keyword seam.
    baseline_dump = normalized(baseline)
    current_source = Path(composition).read_text()
    assert normalized(current_source) == baseline_dump

    # Counterfactuals prove the normalization is targeted rather than broad.
    assert normalized(current_source.replace(
        "WatchlistError, load_watchlists", "RenamedWatchlistError, load_watchlists", 1,
    )) != baseline_dump
    assert normalized(current_source.replace(
        'refuse("once_required")', 'refuse("once_requirement_changed")', 1,
    )) != baseline_dump
    assert normalized(current_source + "\nUNRELATED_TOP_LEVEL_SENTINEL = True\n") != baseline_dump
    membership_imports = [
        node for node in ast.walk(ast.parse(current_source))
        if isinstance(node, ast.ImportFrom)
        and node.module == "tokmo.facts.official_membership_live"
    ]
    assert len(membership_imports) == 1
    assert len(membership_imports[0].names) == 3
    assert {item.name for item in membership_imports[0].names} == {
        "EndpointKindV1", "OfficialMembershipLiveTransport",
        "OfficialMembershipRequestV1",
    }
    exact_canary_imports = {
        node.module: {item.name for item in node.names}
        for node in ast.walk(ast.parse(current_source))
        if isinstance(node, ast.ImportFrom) and node.module in {
            "tokmo.facts.quant_membership",
            "tokmo.programs.quant_membership",
            "tokmo.values.official_membership_observation",
        }
    }
    assert exact_canary_imports == {
        "tokmo.facts.quant_membership": {
            "load_production_sic_snapshot",
            "parse_nasdaqlisted", "parse_otherlisted", "parse_sec_registry",
            "parse_sec_issuer",
        },
        "tokmo.programs.quant_membership": {
            "assemble_us_personal_membership_material",
            "describe_us_personal_membership_cohort", "proof_is_admissible",
        },
        "tokmo.values.official_membership_observation": {
            "MembershipPolicyV1", "ObservationSourceKindV1",
            "ObservedSourcePayloadV1", "OfficialMembershipAcquisitionV1",
            "RequestIdentityV1",
        },
    }
    assert current_source.count(
        "def execute_official_membership_sic_capture("
    ) == 1
    sic_seam = next(
        node for node in ast.walk(ast.parse(current_source))
        if isinstance(node, ast.FunctionDef)
        and node.name == "execute_official_membership_sic_capture"
    )
    sic_source = ast.get_source_segment(current_source, sic_seam)
    assert sic_source is not None
    assert sic_source.count('campaign_store.reserve("SEC_SIC")') == 1
    assert sic_source.count(
        "OfficialMembershipRequestV1(EndpointKindV1.SEC_SIC)"
    ) == 1
    assert sic_source.count("capture_store.append_success") == 1
    assert sic_source.count("capture_store.read(") == 1
    assert sic_source.count(
        'campaign_store.finish(reservation.ordinal, "FAILED")'
    ) == 2
    assert sic_source.count(
        'campaign_store.finish(reservation.ordinal, "SUCCEEDED")'
    ) == 1
    assert current_source.count(
        "def execute_official_membership_canary("
    ) == 1
    canary = next(
        node for node in ast.walk(ast.parse(current_source))
        if isinstance(node, ast.FunctionDef)
        and node.name == "execute_official_membership_canary"
    )
    canary_source = ast.get_source_segment(current_source, canary)
    assert canary_source is not None
    assert canary_source.count("campaign_store.reserve(") == 1
    reserve_calls = [
        node for node in ast.walk(canary)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "reserve_source"
    ]
    assert len(reserve_calls) == 4
    assert canary_source.count("for symbol in sorted(symbols):") == 1
    for endpoint in (
        "EndpointKindV1.NASDAQ_LISTED", "EndpointKindV1.OTHER_LISTED",
        "EndpointKindV1.SEC_REGISTRY", "EndpointKindV1.SEC_SUBMISSIONS",
    ):
        assert canary_source.count(endpoint) == 1
    assert canary_source.count("holdings_reader()") == 2
    assert canary_source.count("observation_store.append_success(") == 1
    # One preflight conflict read plus one post-write canonical read-back.
    assert canary_source.count("observation_store.read_acquisition(") == 2
    assert canary_source.count("observation_store.read_receipt(") == 1
    assert canary_source.count("observation_store.read_materials(") == 1
    assert current_source.count("def stock_candles_fetch(") == 1
    assert current_source.count("stock_candles_fetch=stock_candles_fetch") == 1
    # G2.5's scrubbed seam remains exactly one nested fetcher and one deps
    # binding. The scrubber must not admit deletion or duplication of either.
    assert current_source.count("def calendar_fetch(") == 1
    assert current_source.count("calendar_fetch=calendar_fetch,") == 1
    # The FRED on-demand → Macro caller delta admitted build_macro_runtime_deps
    # into the scrubber. Compensating assertions pin the admitted delta exactly:
    # the FRED wrapper, the pre-existing Toss start accounting, and the
    # fred_caller field.update so none can be silently removed or duplicated.
    assert current_source.count('closed.get("fred_caller")') == 1
    assert current_source.count("def _evaluate_with_fred(") == 1
    assert current_source.count("fields.update(fred_caller())") == 1
    macro_builder = next(
        node for node in ast.walk(ast.parse(current_source))
        if isinstance(node, ast.FunctionDef)
        and node.name == "build_macro_runtime_deps")
    macro_source = ast.get_source_segment(current_source, macro_builder)
    assert macro_source is not None
    assert macro_source.count("starts = 1") == 1
    assert macro_source.count("starts += 1") == 1


def test_sds_id_map_is_complete_and_targets_real_tests():
    delta={"D-C1-01","D-C1-02","D-C1-03","Q18-delta","Q19-delta","S16-delta",*(f"S10-D{i:02d}" for i in range(1,6)),"D-DIGEST-01","D-DIGEST-02","D-PRIV-01"}
    m1={"M1-RED","M1-EACH","M1-NONOK","M1-ZERO","M1-ORDER","M1-STRICT","M1-DIGEST","M1-VECTOR","M1-CAPS"}
    expected={*(f"LC-C{i:02d}" for i in range(1,7)),*(f"Q{i:02d}" for i in range(1,21)),*(f"S{i:02d}" for i in range(1,19)),*delta,*m1}
    assert set(SDS_ID_MAP)==expected
    names=set()
    for path in Path("tests").glob("test_ai_infra_capex*.py"):
        names|={node.name for node in ast.walk(ast.parse(path.read_text())) if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))}
    names|={node.name for node in ast.walk(ast.parse(Path("tests/test_external_news_query.py").read_text())) if isinstance(node,ast.FunctionDef)}
    assert set(SDS_ID_MAP.values())<=names


def test_delta_public_query_signature_and_production_default_are_exact():
    from tokmo.records.external_news_query import query_local_news_v1
    from tokmo.values.ai_infra_capex import PRODUCTION_QUERY_POLICY_V1
    signature=inspect.signature(query_local_news_v1);parameters=list(signature.parameters.values())
    assert [(p.name,p.kind) for p in parameters]==[("path",inspect.Parameter.POSITIONAL_OR_KEYWORD),("query",inspect.Parameter.POSITIONAL_OR_KEYWORD),("policy",inspect.Parameter.KEYWORD_ONLY)]
    assert parameters[2].default is PRODUCTION_QUERY_POLICY_V1
    values=FILES[0].read_text();program=FILES[2].read_text()
    assert "c458c6906e4def00d7f1127df0f3cdb8d45b6093b3d0a381adf9553f3dc9c63f" not in values+program
    assert "member_semantic_digest" in values and "story_key_membership_digest" in values and "semantic_member_digest" in values
    assert "story_membership_digest" not in values
