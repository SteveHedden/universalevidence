from __future__ import annotations

import asyncio

import pytest
from rdflib import Dataset, Literal, URIRef
from rdflib.namespace import DCTERMS, RDF

import scripts.query_v2 as engine


STUDY = "https://universalevidence.com/ontology/Evidence_AEA_AEARCTR-0003248"
STUDY_ID = "AEARCTR-0003248"
REGISTRY_URL = "http://www.socialscienceregistry.org/trials/3248"
STUNTING = "https://universalevidence.com/vocab/states/Stunting"
NUTRITION_PARENT = (
    "https://universalevidence.com/vocab/interventions/NutritionIntervention"
)
DIRECT_CONCEPTS = {
    "https://universalevidence.com/vocab/interventions/NutritionEducation": (
        "Nutrition education"
    ),
    "https://universalevidence.com/vocab/interventions/CounselingIntervention": (
        "Counseling interventions"
    ),
    "https://universalevidence.com/vocab/interventions/CashTransfer": "Cash transfer",
    "https://universalevidence.com/vocab/interventions/HomeVisitingProgram": (
        "Home visiting programs"
    ),
}
INHERITED_PARENT = (
    "https://universalevidence.com/vocab/interventions/DirectServiceDelivery"
)


def _binding(**values: str) -> dict[str, str]:
    return values


def _raw_detail_rows() -> list[dict[str, str]]:
    return [
        _binding(
            study=STUDY,
            source="AEA",
            study_id=REGISTRY_URL,
            title="GroMoTo",
            intervention="registry intervention text",
        )
    ]


def _direct_crosswalk() -> dict[str, tuple[str, ...]]:
    return {
        engine.v1._normalize_xwalk_key("registry intervention text"): tuple(
            sorted(DIRECT_CONCEPTS)
        )
    }


def test_aea_detail_query_returns_raw_text_then_python_hydrates_direct_targets():
    dataset = Dataset()
    source_graph_uri = engine.v1._source_graph_uri("aea", engine.v1.AEA_GRAPH_URI)
    source = dataset.graph(URIRef(source_graph_uri))
    study = URIRef(STUDY)
    raw_predicate = URIRef(
        engine.v1._first_source_field(
            "aea", "intervention", "raw_text"
        )
    )
    source_text = Literal("  registry\r\nintervention   text  ")

    source.add((study, RDF.type, URIRef(f"{engine.v1.AEA}RCTStudy")))
    source.add((study, DCTERMS.identifier, URIRef(REGISTRY_URL)))
    source.add((study, DCTERMS.title, Literal("GroMoTo")))
    source.add((study, raw_predicate, source_text))
    source.add(
        (study, URIRef(f"{engine.v1.UE}matchesIntervention"), URIRef(INHERITED_PARENT))
    )

    query = engine._local_detail_query("aea", [STUDY], {})
    bindings = [
        {str(key): str(value) for key, value in row.asdict().items()}
        for row in dataset.query(query)
    ]
    presented = engine.dedupe_presentation(
        engine._local_rows(
            "aea",
            bindings,
            {},
            {
                engine.v1._normalize_xwalk_key(str(source_text)): tuple(
                    DIRECT_CONCEPTS
                )
            },
            DIRECT_CONCEPTS,
        )
    )

    assert {
        (row["intervention_concept_uri"], row["intervention_concept"])
        for row in presented
    } == set(DIRECT_CONCEPTS.items())
    assert INHERITED_PARENT not in {
        row["intervention_concept_uri"] for row in presented
    }
    assert len({engine.study_key(row) for row in presented}) == 1
    assert {row["study_id"] for row in presented} == {STUDY_ID}
    assert {row["url"] for row in presented} == {REGISTRY_URL}
    assert f"<{engine.v1.UE}rawText>" not in query
    assert "(skos:exactMatch|skos:closeMatch)" not in query
    assert f"<{engine.v1.UE}matchesIntervention>" not in query


@pytest.mark.parametrize("source_id", ["aea", "who-ictrp"])
def test_search_detail_fetches_source_text_without_crosswalk_join(
    source_id: str,
):
    query = engine._local_detail_query(
        source_id,
        [STUDY],
        {"intervention": NUTRITION_PARENT},
    )

    expected_predicate = (
        engine.v1._first_source_field("aea", "intervention", "raw_text")
        if source_id == "aea"
        else f"{engine.v1.ICTRP}intervention"
    )
    if source_id == "aea":
        assert expected_predicate == f"{engine.v1.AEA}intervention"
    assert engine.v1.sparql_iri(expected_predicate) in query
    assert f"<{engine.v1.UE}rawText>" not in query
    assert "(skos:exactMatch|skos:closeMatch)" not in query
    assert f"<{engine.v1.UE}matchesIntervention>" not in query
    assert f"BIND(<{NUTRITION_PARENT}> AS ?matchedInterventionUri)" not in query


def test_graph_detail_retains_existing_selected_intervention_attribution():
    query = engine._local_detail_query(
        "aea",
        [STUDY],
        {"intervention": NUTRITION_PARENT},
        hydrate_direct_interventions=False,
    )

    assert f"BIND(<{NUTRITION_PARENT}> AS ?matchedInterventionUri)" in query
    assert f"<{engine.v1.UE}rawText>" not in query


def test_source_scoped_fuseki_map_preserves_multi_targets_and_aea_normalization(
    monkeypatch: pytest.MonkeyPatch,
):
    queries: list[str] = []

    def select(query: str):
        queries.append(query)
        return [
            {"rawText": "  registry\r\nintervention   text  ", "concept": uri}
            for uri in DIRECT_CONCEPTS
        ] + [
            {
                "rawText": "registry intervention text",
                "concept": next(iter(DIRECT_CONCEPTS)),
            }
        ]

    monkeypatch.setattr(engine.v1, "sparql_select", select)
    engine._source_direct_intervention_crosswalk.cache_clear()
    try:
        crosswalk = engine._source_direct_intervention_crosswalk("aea")
    finally:
        engine._source_direct_intervention_crosswalk.cache_clear()

    assert crosswalk == _direct_crosswalk()
    assert len(queries) == 1
    assert "crosswalk/aea-interventions-freetext/" in queries[0]
    assert 'crosswalk/aea-interventions/"' not in queries[0]
    assert "?concept a ue:Intervention" in queries[0]


def test_who_direct_mapping_retains_exact_literal_semantics():
    uri = next(iter(DIRECT_CONCEPTS))
    crosswalk = {"registry  intervention text": (uri,)}

    assert engine._mapped_source_intervention_uris(
        "who-ictrp", crosswalk, "registry  intervention text"
    ) == (uri,)
    assert engine._mapped_source_intervention_uris(
        "who-ictrp", crosswalk, "registry intervention text"
    ) == ()
    assert engine._mapped_source_intervention_uris(
        "aea", {"registry intervention text": (uri,)}, "registry  intervention text"
    ) == (uri,)


def test_prewarm_loads_all_source_maps_before_requests(
    monkeypatch: pytest.MonkeyPatch,
):
    warmed: list[str] = []

    monkeypatch.setattr(engine, "_local_taxonomy_graph", lambda _name: object())
    monkeypatch.setattr(
        engine,
        "_source_direct_intervention_crosswalk",
        lambda source_id: warmed.append(source_id) or _direct_crosswalk(),
    )
    monkeypatch.setattr(
        engine,
        "_isrctn_direct_intervention_crosswalk",
        lambda: warmed.append("isrctn") or _direct_crosswalk(),
    )

    engine.prewarm_direct_intervention_crosswalks()

    assert warmed == ["ctgov", "aea", "who-ictrp", "isrctn"]


def test_four_source_direct_map_lru_retains_every_prewarmed_entry(
    monkeypatch: pytest.MonkeyPatch,
):
    loaded: list[str] = []

    def load(source_id: str):
        loaded.append(source_id)
        return {"raw": (f"https://example.org/{source_id}",)}

    engine._source_direct_intervention_crosswalk.cache_clear()
    engine._isrctn_direct_intervention_crosswalk.cache_clear()
    monkeypatch.setattr(engine, "_local_taxonomy_graph", lambda _name: object())
    monkeypatch.setattr(engine, "_load_source_direct_intervention_crosswalk", load)
    try:
        engine.prewarm_direct_intervention_crosswalks()
        assert loaded == ["ctgov", "aea", "who-ictrp", "isrctn"]
        assert engine._source_direct_intervention_crosswalk.cache_info().currsize == 4

        for source_id in ("ctgov", "aea", "who-ictrp", "isrctn"):
            engine._source_direct_intervention_crosswalk(source_id)
        engine._isrctn_direct_intervention_crosswalk()

        assert loaded == ["ctgov", "aea", "who-ictrp", "isrctn"]
    finally:
        engine._source_direct_intervention_crosswalk.cache_clear()
        engine._isrctn_direct_intervention_crosswalk.cache_clear()


@pytest.mark.parametrize("axis", ["state", "condition", "outcome", "intervention"])
def test_all_local_search_paths_return_every_direct_mapping_once(
    monkeypatch: pytest.MonkeyPatch, axis: str
):
    detail_queries: list[str] = []

    async def select(query: str, budget: engine.SourceBudget):
        budget.claim_request()
        if "SELECT DISTINCT ?study WHERE" in query:
            return [{"study": STUDY}]
        detail_queries.append(query)
        return _raw_detail_rows()

    monkeypatch.setattr(
        engine, "_source_direct_intervention_crosswalk", lambda _source: _direct_crosswalk()
    )
    monkeypatch.setattr(
        engine,
        "_local_intervention_labels",
        lambda uris: {uri: DIRECT_CONCEPTS[uri] for uri in uris},
    )

    selected_uri = NUTRITION_PARENT if axis == "intervention" else STUNTING
    query = engine.canonical_query({axis: [selected_uri]}, {axis: "or"})
    rows, meta = asyncio.run(
        engine.execute_source(
            query,
            engine.LocalSourceAdapter("aea", select=select),
            engine.default_region_index(),
        )
    )

    assert len(detail_queries) == 1
    assert f"<{engine.v1.UE}rawText>" not in detail_queries[0]
    assert "(skos:exactMatch|skos:closeMatch)" not in detail_queries[0]
    assert {
        (row["intervention_concept_uri"], row["intervention_concept"])
        for row in rows
    } == set(DIRECT_CONCEPTS.items())
    assert len(rows) == 4
    assert meta["returned_unique_studies"] == 1


def test_local_search_keeps_one_other_candidate_when_study_has_no_direct_mapping(
    monkeypatch: pytest.MonkeyPatch,
):
    async def select(query: str, budget: engine.SourceBudget):
        budget.claim_request()
        if "SELECT DISTINCT ?study WHERE" in query:
            return [{"study": STUDY}]
        return [
            _binding(
                study=STUDY,
                source="AEA",
                study_id=REGISTRY_URL,
                title="GroMoTo",
            )
        ]

    monkeypatch.setattr(
        engine, "_source_direct_intervention_crosswalk", lambda _source: {}
    )
    monkeypatch.setattr(engine, "_local_intervention_labels", lambda _uris: {})

    rows, meta = asyncio.run(
        engine.execute_source(
            engine.canonical_query(
                {"condition": [STUNTING]}, {"condition": "or"}
            ),
            engine.LocalSourceAdapter("aea", select=select),
            engine.default_region_index(),
        )
    )

    assert len(rows) == 1
    assert rows[0]["study_id"] == STUDY_ID
    assert rows[0]["intervention_concept_uri"] is None
    assert meta["returned_unique_studies"] == 1


def test_local_search_fails_closed_when_direct_map_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    async def select(query: str, budget: engine.SourceBudget):
        budget.claim_request()
        if "SELECT DISTINCT ?study WHERE" in query:
            return [{"study": STUDY}]
        return _raw_detail_rows()

    def unavailable(_source: str):
        raise RuntimeError("Fuseki unavailable")

    monkeypatch.setattr(engine, "_source_direct_intervention_crosswalk", unavailable)

    rows, meta = asyncio.run(
        engine.execute_source(
            engine.canonical_query(
                {"condition": [STUNTING]}, {"condition": "or"}
            ),
            engine.LocalSourceAdapter("aea", select=select),
            engine.default_region_index(),
        )
    )

    assert rows == []
    assert meta["status"] == "unavailable"
    assert meta["reason"] == "upstream_unavailable"
