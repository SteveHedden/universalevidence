from __future__ import annotations

import os

import httpx
import pytest
from fastapi.testclient import TestClient

import api.main as api_main
import api.routes.taxonomy as taxonomy


client = TestClient(api_main.app)


def test_taxonomy_autocomplete_returns_stable_sorted_shape(monkeypatch):
    async def _mock_run_sparql(_query):
        return [
            {
                "uri": "https://universalevidence.com/vocab/conditions/SevereChildMalnutrition",
                "label": "Severe Child Malnutrition",
                "definition": "Severe nutrition challenge.",
            },
            {
                "uri": "https://universalevidence.com/vocab/conditions/ChildMalnutrition",
                "label": "Child Malnutrition",
                "definition": "Undernutrition in children.",
                "broader": "https://universalevidence.com/vocab/conditions/FoodInsecurity",
            },
            {
                "uri": "https://universalevidence.com/vocab/conditions/Malnutrition",
                "label": "Malnutrition",
            },
            {
                "uri": "https://universalevidence.com/vocab/conditions/ChildMalnutrition",
                "label": "Child Malnutrition",
                "broader": "https://universalevidence.com/vocab/conditions/Nutrition",
            },
            {
                "uri": "https://universalevidence.com/vocab/conditions/ChildDevelopment",
                "label": "Child Development",
            },
        ]

    monkeypatch.setattr(taxonomy, "run_sparql", _mock_run_sparql)

    response = client.get("/taxonomy/challenge", params={"q": "Child", "limit": 3})

    assert response.status_code == 200
    assert response.json() == [
        {
            "uri": "https://universalevidence.com/vocab/conditions/ChildDevelopment",
            "label": "Child Development",
            "broader": [],
            "altLabels": [],
        },
        {
            "uri": "https://universalevidence.com/vocab/conditions/ChildMalnutrition",
            "label": "Child Malnutrition",
            "broader": [
                "https://universalevidence.com/vocab/conditions/FoodInsecurity",
                "https://universalevidence.com/vocab/conditions/Nutrition",
            ],
            "altLabels": [],
            "definition": "Undernutrition in children.",
        },
        {
            "uri": "https://universalevidence.com/vocab/conditions/SevereChildMalnutrition",
            "label": "Severe Child Malnutrition",
            "broader": [],
            "altLabels": [],
            "definition": "Severe nutrition challenge.",
        },
    ]


def test_taxonomy_autocomplete_exact_label_match_sorts_first(monkeypatch):
    async def _mock_run_sparql(_query):
        return [
            {
                "uri": "https://universalevidence.com/vocab/conditions/MalnutritionProgram",
                "label": "Malnutrition Program",
            },
            {
                "uri": "https://universalevidence.com/vocab/conditions/Malnutrition",
                "label": "Malnutrition",
            },
            {
                "uri": "https://universalevidence.com/vocab/conditions/ChildMalnutrition",
                "label": "Child Malnutrition",
            },
        ]

    monkeypatch.setattr(taxonomy, "run_sparql", _mock_run_sparql)

    response = client.get("/taxonomy/challenge", params={"q": "Malnutrition"})

    assert response.status_code == 200
    assert [row["label"] for row in response.json()] == [
        "Malnutrition",
        "Malnutrition Program",
        "Child Malnutrition",
    ]


def test_taxonomy_class_mapping_uses_current_ue_classes(monkeypatch):
    captured_queries = []

    async def _mock_run_sparql(query):
        captured_queries.append(query)
        return []

    monkeypatch.setattr(taxonomy, "run_sparql", _mock_run_sparql)

    for cls, class_uri in {
        "challenge": "https://universalevidence.com/ontology/State",
        "condition": "https://universalevidence.com/ontology/State",
        "intervention": "https://universalevidence.com/ontology/Intervention",
        "outcome": "https://universalevidence.com/ontology/State",
        "region": "https://universalevidence.com/ontology/Region",
    }.items():
        response = client.get(f"/taxonomy/{cls}", params={"q": "x"})
        assert response.status_code == 200
        assert f"?uri rdf:type <{class_uri}> ." in captured_queries[-1]


def test_taxonomy_region_returns_empty_list_when_no_rows(monkeypatch):
    async def _mock_run_sparql(_query):
        return []

    monkeypatch.setattr(taxonomy, "run_sparql", _mock_run_sparql)

    response = client.get("/taxonomy/region", params={"q": "ken"})

    assert response.status_code == 200
    assert response.json() == []


def test_taxonomy_region_normalizes_country_rows(monkeypatch):
    async def _mock_run_sparql(_query):
        return [
            {
                "uri": "https://sws.geonames.org/192950/",
                "label": "Kenya",
                "definition": "Country concept.",
                "broader": "https://universalevidence.com/vocab/regions/SubSaharanAfrica",
            }
        ]

    monkeypatch.setattr(taxonomy, "run_sparql", _mock_run_sparql)

    response = client.get("/taxonomy/region", params={"q": "ken"})

    assert response.status_code == 200
    assert response.json() == [
        {
            "uri": "https://sws.geonames.org/192950/",
            "label": "Kenya",
            "broader": ["https://universalevidence.com/vocab/regions/SubSaharanAfrica"],
            "altLabels": [],
            "definition": "Country concept.",
        }
    ]


def test_flat_cache_search_matches_alt_labels(monkeypatch):
    # Regression test: production always hits the prewarmed _FLAT_CACHE path
    # (populated at startup for every CLASS_MAP entry), not the live-SPARQL
    # build_taxonomy_query path -- so altLabel matching must work through
    # _search_flat_cache, or altLabel search silently never fires in prod
    # even though build_taxonomy_query's SPARQL correctly includes altLabel.
    monkeypatch.setitem(
        taxonomy._FLAT_CACHE,
        "condition",
        [
            {
                "uri": "https://universalevidence.com/vocab/states/FoodInsecurity",
                "label": "Food insecurity",
                "broader": [],
                "altLabels": ["Hunger"],
            },
            {
                "uri": "https://universalevidence.com/vocab/states/ChildDevelopment",
                "label": "Child development",
                "broader": [],
                "altLabels": [],
            },
        ],
    )

    response = client.get("/taxonomy/condition", params={"q": "hunger"})

    assert response.status_code == 200
    uris = [r["uri"] for r in response.json()]
    assert uris == ["https://universalevidence.com/vocab/states/FoodInsecurity"]


def test_taxonomy_invalid_class_returns_400():
    response = client.get("/taxonomy/challenges", params={"q": "malnut"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown taxonomy class: challenges"


def test_taxonomy_q_validation_blocks_missing_empty_and_whitespace(monkeypatch):
    async def _mock_run_sparql(_query):
        raise AssertionError("Whitespace-only q should not query Fuseki")

    monkeypatch.setattr(taxonomy, "run_sparql", _mock_run_sparql)

    assert client.get("/taxonomy/challenge").status_code == 422
    assert client.get("/taxonomy/challenge", params={"q": ""}).status_code == 422

    whitespace = client.get("/taxonomy/challenge", params={"q": "   "})
    assert whitespace.status_code == 422
    assert whitespace.json()["detail"] == "q must contain non-whitespace text"


def test_taxonomy_fuseki_failure_returns_502(monkeypatch):
    async def _mock_run_sparql(_query):
        raise httpx.ConnectError("connection failed")

    monkeypatch.setattr(taxonomy, "run_sparql", _mock_run_sparql)

    response = client.get("/taxonomy/challenge", params={"q": "malnut"})

    assert response.status_code == 502
    assert response.json()["detail"] == "Taxonomy search service unavailable"


def test_build_taxonomy_query_matches_labels_alt_labels_and_definitions():
    query = taxonomy.build_taxonomy_query(
        "https://universalevidence.com/ontology/Challenge",
        "malnut",
    )

    assert "rdfs:label" in query
    assert "skos:prefLabel" in query
    assert "skos:altLabel" in query
    assert "skos:definition" in query
    assert "https://universalevidence.com/graph/ue-taxonomy" in query


@pytest.mark.skipif(os.environ.get("FUSEKI_LIVE_TEST") != "1", reason="requires local Fuseki")
def test_taxonomy_live_fuseki_smoke():
    response = client.get("/taxonomy/challenge", params={"q": "malnut"})

    assert response.status_code == 200
    assert any(row["label"] == "Child Malnutrition" for row in response.json())
