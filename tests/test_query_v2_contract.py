from __future__ import annotations

import json
from pathlib import Path

from scripts.query_v2_canary import (
    baseline_observations,
    cache_stats_delta,
    canonical_source,
    evaluate_case,
    parameter_pairs,
    summarize_payload,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = REPO_ROOT / "contracts" / "query-v2"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "query_v2"
DOC = REPO_ROOT / "contracts" / "query-v2" / "README.md"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_contract_document_freezes_v2_without_mutating_v1():
    text = DOC.read_text()

    assert "GET /query/v2" in text
    assert "existing `GET /query`" in text
    assert "`GET /graph`" in text
    assert "remain unchanged" in text
    assert "100 **unique studies per upstream source request/branch**" in text
    assert "There is no global" in text
    assert "graph-v2 projection" in text
    assert "explicit decision" in text


def test_request_schema_uses_repeated_axes_and_explicit_logic():
    schema = load_json(CONTRACT_DIR / "request.schema.json")
    properties = schema["properties"]

    assert {"state", "condition", "intervention", "outcome", "region"} <= set(properties)
    assert "country" not in properties
    assert schema["$defs"]["logic"]["enum"] == ["or", "and"]
    assert schema["$defs"]["logic"]["default"] == "or"
    assert schema["$defs"]["axisValues"]["uniqueItems"] is True
    assert {tuple(option["required"]) for option in schema["anyOf"]} == {
        ("state",),
        ("condition",),
        ("intervention",),
        ("outcome",),
        ("region",),
    }


def test_response_schema_requires_all_source_metadata_and_per_branch_limit():
    schema = load_json(CONTRACT_DIR / "response.schema.json")
    meta = schema["$defs"]["queryMeta"]
    sources = meta["properties"]["sources"]
    source_meta = schema["$defs"]["sourceMeta"]

    assert meta["properties"]["api_version"]["const"] == "query-v2"
    assert meta["properties"]["limit_per_source_branch"]["const"] == 100
    assert set(sources["required"]) == {"ctgov", "aea", "isrctn", "who-ictrp"}
    assert source_meta["properties"]["status"]["enum"] == [
        "included",
        "excluded",
        "unavailable",
        "error",
    ]
    assert "unsupported_admin_level" in source_meta["properties"]["reason"]["enum"]


def test_capability_matrix_matches_frozen_source_semantics():
    matrix = load_json(CONTRACT_DIR / "source-capabilities.json")
    sources = matrix["sources"]

    assert matrix["canonical_sources"] == ["ctgov", "aea", "isrctn", "who-ictrp"]
    assert matrix["budgets"] == {
        "limit_unique_studies_per_source_branch": 100,
        "max_requests_per_source": 8,
        "max_pages_per_branch": 3,
        "time_budget_seconds": 5,
    }
    assert sources["ctgov"]["region_levels"]["adm1"] == "partial"
    assert "contextual_city_country_crosswalk" in sources["ctgov"][
        "adm1_verification"
    ]
    assert sources["isrctn"]["region_levels"]["adm1"] == "partial"
    assert sources["aea"]["region_levels"]["adm1"] == "none"
    assert sources["who-ictrp"]["region_levels"]["adm1"] == "none"
    assert all("state" in source["axes"] for source in sources.values())


def test_deterministic_adapter_fixtures_cover_incident_and_limits():
    corpus = load_json(FIXTURE_DIR / "source_adapter_cases.json")
    cases = {case["id"]: case for case in corpus["cases"]}

    assert len(cases) == len(corpus["cases"])
    assert {
        "ctgov_chittagong_does_not_broaden_to_bangladesh",
        "ctgov_context_is_scoped_to_one_location",
        "region_and_uses_multisite_study_semantics",
        "source_capability_adm1_exclusions",
        "unique_study_limit_precedes_presentation_expansion",
        "state_unions_condition_and_outcome_before_deduplication",
        "state_role_union_precedes_source_branch_cap",
        "budget_exhaustion_never_broadens_predicate",
        "missing_optional_geo_asset_is_not_http_500",
    } <= set(cases)

    chittagong = cases["ctgov_chittagong_does_not_broaden_to_bangladesh"]
    assert chittagong["expected_study_ids"] == ["NCT-CHITTAGONG"]
    assert "NCT-DHAKA-ONLY" in chittagong["forbidden_study_ids"]

    cap = cases["unique_study_limit_precedes_presentation_expansion"]
    assert cap["generator"] == {
        "unique_studies": 101,
        "intervention_rows_per_study": 3,
    }
    assert cap["expected"]["selected_unique_studies"] == 100
    assert cap["expected"]["presentation_rows_after_selection"] == 300
    assert cap["expected"]["global_merged_cap"] is None

    budget = cases["budget_exhaustion_never_broadens_predicate"]
    assert budget["expected"]["requested_predicates_preserved"] is True
    assert budget["expected"]["truncated"] is True
    assert budget["expected"]["approximate"] is True


def test_canary_corpus_contains_every_required_live_comparison():
    corpus = load_json(FIXTURE_DIR / "canary_queries.json")
    cases = {case["id"]: case for case in corpus["cases"]}

    assert {
        "malnutrition_anywhere",
        "malaria_anywhere",
        "malaria_chittagong",
        "bangladesh_country",
        "south_asia_group",
        "kenya_or_uganda",
        "kenya_and_uganda",
    } == set(cases)
    assert cases["malnutrition_anywhere"]["gates"][
        "postmortem_reference_is_exact_gate"
    ] is False
    assert "state" in cases["malnutrition_anywhere"]["v2_params"]
    assert "condition" not in cases["malnutrition_anywhere"]["v2_params"]
    assert cases["kenya_or_uganda"]["v1_params"] is None
    assert cases["kenya_and_uganda"]["v2_params"]["region_logic"] == ["and"]


def test_canary_parameter_pairs_preserve_repeated_values():
    assert parameter_pairs(
        {
            "region_logic": ["or"],
            "region": ["https://example.org/Kenya", "https://example.org/Uganda"],
        }
    ) == [
        ("region", "https://example.org/Kenya"),
        ("region", "https://example.org/Uganda"),
        ("region_logic", "or"),
    ]


def test_recorded_v1_baseline_is_reusable_for_local_v2_canaries(tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"cases": [{"id": "synthetic_case", "observations": [{
        "endpoint": "v1", "http_status": 200,
        "source_counts": {"aea": 2, "ctgov": 3},
    }]}]}))
    observations = baseline_observations(baseline)
    assert observations["synthetic_case"][0]["http_status"] == 200
    assert observations["synthetic_case"][0]["source_counts"] == {"aea": 2, "ctgov": 3}


def test_canary_summary_deduplicates_before_counting_sources():
    payload = [
        {"source": "CT.gov", "study_id": "NCT1", "intervention": "A"},
        {"source": "ClinicalTrials.gov", "study_id": "NCT1", "intervention": "B"},
        {"source": "AEA RCT Registry", "study_id": "AEA1"},
    ]

    assert summarize_payload(payload, "v1") == {
        "presentation_rows": 3,
        "unique_studies": 2,
        "duplicate_presentation_rows": 1,
        "source_counts": {"aea": 1, "ctgov": 1},
    }


def test_canary_evaluation_detects_source_starvation():
    case = {
        "compare_with_v1": True,
        "gates": {
            "preserve_same_run_v1_sources": True,
            "no_expanded_row_starvation": True,
        },
    }
    observations = [
        {
            "endpoint": "v1",
            "http_status": 200,
            "unique_studies": 409,
            "source_counts": {"aea": 100, "ctgov": 100, "isrctn": 100, "who-ictrp": 109},
        },
        {
            "endpoint": "v2",
            "http_status": 200,
            "unique_studies": 47,
            "source_counts": {"ctgov": 47},
        },
    ]

    result = evaluate_case(case, observations)

    assert result["status"] == "fail"
    assert result["checks"]["preserve_same_run_v1_sources"] is False
    assert result["checks"]["no_expanded_row_starvation"] is False


def test_canary_starvation_gate_respects_the_per_source_branch_cap():
    case = {
        "compare_with_v1": True,
        "gates": {"no_expanded_row_starvation": True},
    }
    observations = [
        {
            "endpoint": "v1",
            "http_status": 200,
            "unique_studies": 306,
            "source_counts": {
                "aea": 37,
                "ctgov": 174,
                "isrctn": 93,
                "who-ictrp": 2,
            },
        },
        {
            "endpoint": "v2",
            "http_status": 200,
            "unique_studies": 305,
            "source_counts": {
                "aea": 37,
                "ctgov": 100,
                "isrctn": 93,
                "who-ictrp": 75,
            },
        },
    ]

    result = evaluate_case(case, observations)

    assert result["status"] == "pass"
    assert result["checks"]["no_expanded_row_starvation"] is True


def test_source_aliases_normalize_to_contract_ids():
    assert canonical_source("CT.gov") == "ctgov"
    assert canonical_source("ClinicalTrials.gov") == "ctgov"
    assert canonical_source("AEA RCT Registry") == "aea"
    assert canonical_source("ISRCTN Registry") == "isrctn"
    assert canonical_source("WHO ICTRP") == "who-ictrp"


def test_cache_stats_delta_exposes_cold_and_repeat_counter_changes():
    assert cache_stats_delta(
        {"hits": 3, "misses": 102, "backend_executions": 102},
        {"hits": 8, "misses": 107, "backend_executions": 107},
    ) == {"hits": 5, "misses": 5, "backend_executions": 5}
    assert cache_stats_delta(None, {"hits": 1}) is None
