from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import query_interventions as v1  # noqa: E402
import query_v2 as v2  # noqa: E402


RAW = "Nasal continuous positive airway pressure"
CONCEPT = "https://universalevidence.com/vocab/interventions/ContinuousPositiveAirwayPressure"
LABEL = "Continuous positive airway pressure"
EXPLICIT = "https://universalevidence.com/vocab/interventions/ExplicitSelection"


def test_materialized_crosswalk_lookup_requires_ctgov_namespace_and_intervention_type(monkeypatch):
    captured = {}

    def select(query):
        captured["query"] = query
        return [{"rawText": RAW, "conceptUri": CONCEPT, "conceptLabel": LABEL, "matchType": "close"}]

    v1._load_ctgov_intervention_concept_map.cache_clear()
    monkeypatch.setattr(v1, "sparql_select", select)

    assert v1._load_ctgov_intervention_concept_map()[RAW] == (CONCEPT, LABEL)
    assert "a ue:Intervention" in captured["query"]
    assert "crosswalk/ctgov-interventions/" in captured["query"]


def test_v1_live_fallback_attaches_raw_name_crosswalk_without_overwriting_explicit():
    mapped = v1._attach_ctgov_intervention_concept(
        {"intervention": RAW, "intervention_concept_uri": None},
        {RAW: (CONCEPT, LABEL)},
    )
    explicit = v1._attach_ctgov_intervention_concept(
        {
            "intervention": RAW,
            "intervention_concept_uri": EXPLICIT,
            "intervention_concept": "Explicit",
        },
        {RAW: (CONCEPT, LABEL)},
    )

    assert mapped["intervention_concept_uri"] == CONCEPT
    assert mapped["intervention_concept"] == LABEL
    assert explicit["intervention_concept_uri"] == EXPLICIT


def test_query_v2_presentation_uses_same_crosswalk_and_preserves_explicit_precedence():
    study = {
        "source": "CT.gov",
        "nctId": "NCT-SYNTHETIC-001",
        "title": "Pilot",
        "url": "https://clinicaltrials.gov/study/NCT-SYNTHETIC-001",
        "intervention": RAW,
        "_intervention_names": [RAW],
        "condition_mesh_uris": [],
        "intervention_mesh_uris": [],
        "country": "",
        "status": "completed",
        "year": 2026,
        "outcomes": [],
    }

    fallback_rows = v2._ctgov_presentation(
        [study],
        {},
        {},
        {},
        {RAW: (CONCEPT, LABEL)},
        {},
        {},
    )
    explicit_rows = v2._ctgov_presentation(
        [study],
        {"intervention": EXPLICIT},
        {"intervention": "Explicit"},
        {},
        {RAW: (CONCEPT, LABEL)},
        {},
        {},
    )

    assert fallback_rows[0]["intervention_concept_uri"] == CONCEPT
    assert fallback_rows[0]["intervention_concept"] == LABEL
    assert explicit_rows[0]["intervention_concept_uri"] == EXPLICIT
