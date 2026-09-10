from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import query_interventions as qi


CROSSWALK_URI = "https://universalevidence.com/vocab/interventions/NutritionEducation"
MESH_URI = "https://universalevidence.com/vocab/interventions/MeshFallback"
LABEL_URI = "https://universalevidence.com/vocab/interventions/LabelFallback"
EXPLICIT_URI = "https://universalevidence.com/vocab/interventions/ExplicitSearch"


@pytest.fixture(autouse=True)
def clear_intervention_crosswalk_cache():
    qi._load_ctgov_intervention_concept_map.cache_clear()
    yield
    qi._load_ctgov_intervention_concept_map.cache_clear()


class Response:
    def __init__(self, studies):
        self._studies = studies

    def raise_for_status(self):
        return None

    def json(self):
        return {"studies": self._studies}


def _study(nct_id: str, intervention: str, mesh_id: str | None = None) -> dict:
    study = {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id, "briefTitle": nct_id},
            "armsInterventionsModule": {"interventions": [{"name": intervention}]},
            "statusModule": {"overallStatus": "COMPLETED"},
        },
        "derivedSection": {},
    }
    if mesh_id:
        study["derivedSection"]["interventionBrowseModule"] = {
            "meshes": [{"id": mesh_id}]
        }
    return study


def test_lookup_resolves_known_crosswalk_text_and_uses_intervention_namespace(monkeypatch):
    captured = {}

    def select(query):
        captured["query"] = query
        return [
            {
                "rawText": "Herbal Formulation",
                "conceptUri": CROSSWALK_URI,
                "conceptLabel": "Nutrition education",
                "matchType": "exact",
            }
        ]

    qi._load_ctgov_intervention_concept_map.cache_clear()
    monkeypatch.setattr(qi, "sparql_select", select)

    concept_map = qi._load_ctgov_intervention_concept_map()

    assert concept_map["Herbal Formulation"] == (
        CROSSWALK_URI,
        "Nutrition education",
    )
    assert "a ue:Intervention" in captured["query"]
    assert "crosswalk/ctgov-interventions/" in captured["query"]


def test_live_mapping_prefers_crosswalk_over_mesh(monkeypatch):
    study = _study("NCT-CROSSWALK", "Herbal Formulation", "D000001")
    monkeypatch.setattr(qi.requests, "get", lambda *_args, **_kwargs: Response([study]))
    monkeypatch.setattr(
        qi,
        "_load_ctgov_intervention_concept_map",
        lambda: {"Herbal Formulation": (CROSSWALK_URI, "Nutrition education")},
    )
    monkeypatch.setattr(
        qi,
        "lookup_interventions_by_mesh",
        lambda _uris: {"http://id.nlm.nih.gov/mesh/D000001": (MESH_URI, "Mesh")},
    )
    monkeypatch.setattr(qi, "lookup_intervention_concepts_by_label", lambda _texts: {})

    results = asyncio.run(qi._run_ctgov_live({}, "", None, None, None))

    assert results[0]["intervention_concept_uri"] == CROSSWALK_URI


def test_crosswalk_failure_preserves_mesh_and_label_fallbacks(monkeypatch):
    studies = [
        _study("NCT-MESH", "Uncovered mesh intervention", "D000002"),
        _study("NCT-LABEL", "Known taxonomy label"),
    ]
    monkeypatch.setattr(qi.requests, "get", lambda *_args, **_kwargs: Response(studies))
    monkeypatch.setattr(
        qi,
        "_load_ctgov_intervention_concept_map",
        lambda: (_ for _ in ()).throw(RuntimeError("Fuseki unavailable")),
    )
    monkeypatch.setattr(
        qi,
        "lookup_interventions_by_mesh",
        lambda _uris: {
            "http://id.nlm.nih.gov/mesh/D000002": (MESH_URI, "Mesh fallback")
        },
    )
    monkeypatch.setattr(
        qi,
        "lookup_intervention_concepts_by_label",
        lambda texts: (
            {"Known taxonomy label": (LABEL_URI, "Label fallback")}
            if "Known taxonomy label" in texts
            else {}
        ),
    )

    results = asyncio.run(qi._run_ctgov_live({}, "", None, None, None))
    by_id = {result["study_id"]: result for result in results}

    assert by_id["NCT-MESH"]["intervention_concept_uri"] == MESH_URI
    assert by_id["NCT-LABEL"]["intervention_concept_uri"] == LABEL_URI


def test_explicit_intervention_search_assignment_is_not_overwritten(monkeypatch):
    study = _study("NCT-EXPLICIT", "Herbal Formulation", "D000001")
    monkeypatch.setattr(qi.requests, "get", lambda *_args, **_kwargs: Response([study]))
    monkeypatch.setattr(
        qi,
        "_load_ctgov_intervention_concept_map",
        lambda: {"Herbal Formulation": (CROSSWALK_URI, "Crosswalk")},
    )

    results = asyncio.run(
        qi._run_ctgov_live({}, "", "Explicit search", EXPLICIT_URI, None)
    )

    assert results[0]["intervention_concept_uri"] == EXPLICIT_URI
    assert results[0]["intervention_concept"] == "Explicit search"


def test_ctgov_study_matches_intervention_terms_by_substring():
    assert qi._ctgov_study_matches_intervention_terms(
        ["Wave energy"], ["Marine wave energy converter"]
    )
    assert not qi._ctgov_study_matches_intervention_terms(
        ["Wave energy"], ["Extracorporeal Shock Wave Therapy"]
    )


def test_ctgov_study_matches_intervention_terms_passes_through_when_unverifiable():
    # No named interventions to check against -- nothing to disprove the match with.
    assert qi._ctgov_study_matches_intervention_terms(["Wave energy"], [])


def test_word_collision_false_positive_dropped_when_intr_terms_dont_match(monkeypatch):
    # Regression test for the "Wave energy" -> "shock wave therapy" false positive:
    # CT.gov's query.intr does word-level matching, not phrase matching, so a study
    # sharing only individual words with a short altLabel must be dropped rather
    # than stamped with that concept.
    real_match = _study("NCT-REAL", "Marine wave energy converter")
    false_positive = _study("NCT-FALSE-POSITIVE", "Extracorporeal Shock Wave Therapy")
    monkeypatch.setattr(
        qi.requests, "get", lambda *_args, **_kwargs: Response([real_match, false_positive])
    )

    results = asyncio.run(
        qi._run_ctgov_live({}, "", "Wave energy", EXPLICIT_URI, None, ["Wave energy"])
    )

    ids = {r["study_id"] for r in results}
    assert ids == {"NCT-REAL"}


def test_intervention_axis_search_without_terms_keeps_prior_unconditional_stamping(monkeypatch):
    # When intr_terms isn't supplied (e.g. an older/other caller), preserve the
    # original unconditional-trust behavior rather than dropping everything.
    study = _study("NCT-UNVERIFIED", "Something unrelated in text")
    monkeypatch.setattr(qi.requests, "get", lambda *_args, **_kwargs: Response([study]))

    results = asyncio.run(qi._run_ctgov_live({}, "", "Wave energy", EXPLICIT_URI, None))

    assert results[0]["intervention_concept_uri"] == EXPLICIT_URI


def test_mapping_retains_all_distinct_interventions_in_source_order():
    study = _study("NCT-MULTI", "Drug A")
    study["protocolSection"]["armsInterventionsModule"]["interventions"] = [
        {"name": "Drug A"},
        {"name": "Placebo"},
        {"name": "Drug A"},
        {"name": "Nutrition counselling"},
    ]

    mapped = qi.map_ctgov_study(study, "")

    assert mapped["intervention"] == "Drug A"
    assert mapped["_intervention_names"] == [
        "Drug A",
        "Placebo",
        "Nutrition counselling",
    ]


def test_multi_intervention_expansion_dedupes_concepts_and_preserves_unmatched(monkeypatch):
    study = _study("NCT-MULTI", "Drug A", "D000009")
    study["protocolSection"]["armsInterventionsModule"]["interventions"] = [
        {"name": "Drug A"},
        {"name": "Drug A alias"},
        {"name": "Nutrition counselling"},
        {"name": "Unclassified placebo"},
    ]
    monkeypatch.setattr(qi.requests, "get", lambda *_args, **_kwargs: Response([study]))
    monkeypatch.setattr(
        qi,
        "_load_ctgov_intervention_concept_map",
        lambda: {
            "Drug A": (CROSSWALK_URI, "Drug concept"),
            "Drug A alias": (CROSSWALK_URI, "Drug concept"),
        },
    )
    monkeypatch.setattr(
        qi,
        "lookup_interventions_by_mesh",
        lambda _uris: (_ for _ in ()).throw(
            AssertionError("study-level MeSH must not classify multi-intervention rows")
        ),
    )
    monkeypatch.setattr(
        qi,
        "lookup_intervention_concepts_by_label",
        lambda _texts: {
            "Nutrition counselling": (LABEL_URI, "Nutrition counselling")
        },
    )

    expanded = asyncio.run(qi._run_ctgov_live({}, "", None, None, None))
    results = qi.dedupe_and_sort_results(expanded)

    assert len(results) == 3
    by_key = {
        result.get("intervention_concept_uri") or result["intervention"]: result
        for result in results
    }
    assert by_key[CROSSWALK_URI]["intervention"] == "Drug A"
    assert by_key[LABEL_URI]["intervention"] == "Nutrition counselling"
    assert by_key["Unclassified placebo"]["intervention_concept_uri"] is None


def test_fetch_cap_counts_studies_before_row_expansion(monkeypatch):
    studies = []
    crosswalk = {}
    for index in range(100):
        study = _study(f"NCT-{index:03d}", f"Intervention {index} A")
        second = f"Intervention {index} B"
        study["protocolSection"]["armsInterventionsModule"]["interventions"].append(
            {"name": second}
        )
        studies.append(study)
        crosswalk[f"Intervention {index} A"] = (
            f"{CROSSWALK_URI}/a/{index}",
            f"A {index}",
        )
        crosswalk[second] = (f"{CROSSWALK_URI}/b/{index}", f"B {index}")

    monkeypatch.setattr(qi.requests, "get", lambda *_args, **_kwargs: Response(studies))
    monkeypatch.setattr(qi, "_load_ctgov_intervention_concept_map", lambda: crosswalk)
    monkeypatch.setattr(qi, "lookup_interventions_by_mesh", lambda _uris: {})
    monkeypatch.setattr(qi, "lookup_intervention_concepts_by_label", lambda _texts: {})

    results = asyncio.run(qi._run_ctgov_live({}, "", None, None, None))

    assert len({result["study_id"] for result in results}) == 100
    assert len(results) == 200


def test_unmatched_rows_dedupe_by_raw_intervention_not_only_study_id():
    results = qi.dedupe_and_sort_results(
        [
            {"source": "CT.gov", "study_id": "NCT-RAW", "intervention": "Placebo"},
            {"source": "CT.gov", "study_id": "NCT-RAW", "intervention": "Usual care"},
        ]
    )

    assert {result["intervention"] for result in results} == {"Placebo", "Usual care"}
