from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest
from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.namespace import RDF, SKOS

import api.main as api_main
from api.routes import graph_v2
import scripts.query_v2 as query_v2


STATE_BASE = "https://universalevidence.com/vocab/states/"
INTERVENTION_BASE = "https://universalevidence.com/vocab/interventions/"
CHILD_GROWTH = f"{STATE_BASE}ChildGrowth"
STUNTING = f"{STATE_BASE}Stunting"
WASTING = f"{STATE_BASE}Wasting"
DIRECT_INTERVENTIONS = {
    f"{INTERVENTION_BASE}NutritionEducation": "Nutrition education",
    f"{INTERVENTION_BASE}CounselingIntervention": "Counseling interventions",
    f"{INTERVENTION_BASE}CashTransfer": "Cash transfer",
    f"{INTERVENTION_BASE}HomeVisitingProgram": "Home visiting programs",
}
INHERITED_NUTRITION = f"{INTERVENTION_BASE}NutritionIntervention"
AEA_STUDY = (
    "https://universalevidence.com/ontology/"
    "Evidence_AEA_AEARCTR-0003248"
)


def _query(**values: list[str]):
    return graph_v2._graph_query(
        state=values.get("state", []),
        condition=values.get("condition", []),
        intervention=values.get("intervention", []),
        outcome=values.get("outcome", []),
        region=values.get("region", []),
        state_logic=values.get("state_logic", "or"),
        condition_logic=values.get("condition_logic", "or"),
        intervention_logic=values.get("intervention_logic", "or"),
        outcome_logic=values.get("outcome_logic", "or"),
        region_logic=values.get("region_logic", "or"),
    )


def _copy_subject(source: Graph, target: Graph, subject: URIRef) -> None:
    for triple in source.triples((subject, None, None)):
        target.add(triple)


async def _direct_maps_from_dataset(
    dataset: Dataset,
    source_id: str,
    roles: tuple[str, ...],
):
    taxonomy = dataset.graph(URIRef(graph_v2.v1.TAXONOMY_GRAPH_URI))

    def scoped_map(prefix: str, *, state: bool):
        values: dict[str, set[str]] = {}
        for entry, raw_text in taxonomy.subject_objects(
            URIRef(f"{graph_v2.v1.UE}rawText")
        ):
            if not str(entry).startswith(prefix):
                continue
            key = (
                query_v2._source_direct_state_key(source_id, raw_text)
                if state
                else query_v2._source_direct_intervention_key(
                    source_id, raw_text
                )
            )
            for predicate in (SKOS.exactMatch, SKOS.closeMatch):
                values.setdefault(key, set()).update(
                    str(concept)
                    for concept in taxonomy.objects(entry, predicate)
                )
        return {
            key: tuple(sorted(concepts))
            for key, concepts in values.items()
            if concepts
        }

    state_maps = {
        role: scoped_map(
            graph_v2._DIRECT_CROSSWALKS[source_id][role][1], state=True
        )
        for role in roles
    }
    intervention_map = scoped_map(
        graph_v2._DIRECT_CROSSWALKS[source_id]["intervention"][1],
        state=False,
    )
    return state_maps, intervention_map


@pytest.fixture
def frozen_aea_rows(monkeypatch) -> list[dict[str, str]]:
    """Run Graph's stored aggregation against the checked-in GroMoTo record."""
    root = Path(graph_v2.REPO_ROOT)
    raw = Graph().parse(root / "data/raw/aea-rct-raw.ttl", format="turtle")
    outcome_crosswalk = Graph().parse(
        root / "vocabularies/crosswalks/aea-outcomes-crosswalk.ttl",
        format="turtle",
    )
    intervention_crosswalk = Graph().parse(
        root / "vocabularies/crosswalks/aea-interventions-freetext-crosswalk.ttl",
        format="turtle",
    )
    states = Graph().parse(root / "vocabularies/states.ttl", format="turtle")
    interventions = Graph().parse(
        root / "vocabularies/interventions.ttl", format="turtle"
    )

    dataset = Dataset()
    source = dataset.graph(URIRef(graph_v2._source_graph("aea")))
    taxonomy = dataset.graph(URIRef(graph_v2.v1.TAXONOMY_GRAPH_URI))
    study = URIRef(AEA_STUDY)
    _copy_subject(raw, source, study)

    outcome_raw = next(
        raw.objects(study, URIRef(f"{graph_v2.v1.AEA}primaryOutcome"))
    )
    state_uris: set[str] = set()
    for entry in outcome_crosswalk.subjects(
        URIRef(f"{graph_v2.v1.UE}rawText"), outcome_raw
    ):
        _copy_subject(outcome_crosswalk, taxonomy, entry)
        for predicate in (SKOS.exactMatch, SKOS.closeMatch):
            for concept in outcome_crosswalk.objects(entry, predicate):
                state_uris.add(str(concept))
                source.add(
                    (
                        study,
                        URIRef(f"{graph_v2.v1.UE}matchesOutcome"),
                        concept,
                    )
                )

    intervention_raw = next(
        raw.objects(study, URIRef(f"{graph_v2.v1.AEA}intervention"))
    )
    intervention_uris: set[str] = set()
    for entry in intervention_crosswalk.subjects(
        URIRef(f"{graph_v2.v1.UE}rawText"), intervention_raw
    ):
        _copy_subject(intervention_crosswalk, taxonomy, entry)
        for predicate in (SKOS.exactMatch, SKOS.closeMatch):
            intervention_uris.update(
                str(concept)
                for concept in intervention_crosswalk.objects(entry, predicate)
            )

    for uri in state_uris:
        _copy_subject(states, taxonomy, URIRef(uri))
    for uri in intervention_uris:
        _copy_subject(interventions, taxonomy, URIRef(uri))

    assert state_uris == {CHILD_GROWTH, STUNTING, WASTING}
    assert intervention_uris == set(DIRECT_INTERVENTIONS)

    async def select(sparql, _budget):
        return [
            {str(key): str(value) for key, value in row.asdict().items()}
            for row in dataset.query(sparql)
        ]

    monkeypatch.setattr(graph_v2, "_sparql_select", select)
    monkeypatch.setattr(
        graph_v2,
        "_stored_direct_maps",
        lambda source_id, roles: _direct_maps_from_dataset(
            dataset, source_id, tuple(roles)
        ),
    )
    rows, meta, _duration = asyncio.run(
        graph_v2.load_local_groups(
            "aea",
            _query(state=sorted(state_uris)),
            sorted(state_uris),
            [],
        )
    )
    assert meta["returned_unique_studies"] == 1
    return rows


def test_frozen_aea_gromoto_is_exact_three_by_four(frozen_aea_rows):
    assert len(frozen_aea_rows) == 12
    assert {
        (row["state"], row["intervention"], row["weight"])
        for row in frozen_aea_rows
    } == {
        (state, intervention, "1")
        for state in (CHILD_GROWTH, STUNTING, WASTING)
        for intervention in DIRECT_INTERVENTIONS
    }
    assert INHERITED_NUTRITION not in {
        row["intervention"] for row in frozen_aea_rows
    }


def test_state_payload_wire_aliases_and_frozen_aea_counts(frozen_aea_rows):
    state_taxonomy = {
        "nodes": [
            {
                "id": uri,
                "label": uri.rsplit("/", 1)[-1],
                "class": "Condition",
                "studyCount": 0,
                "selected": True,
            }
            for uri in (CHILD_GROWTH, STUNTING, WASTING)
        ],
        "pairs": [],
        "omitted": 0,
    }
    payload = graph_v2.build_graph_v2_payload(
        query=_query(state=[CHILD_GROWTH, STUNTING, WASTING]),
        condition_taxonomy=state_taxonomy,
        intervention_taxonomy={"nodes": [], "pairs": [], "omitted": 0},
        local_rows={"aea": frozen_aea_rows, "who-ictrp": []},
        live_rows={"ctgov": [], "isrctn": []},
        source_meta={
            source: {
                "status": "included",
                "coverage": "full",
                "returned_unique_studies": 1 if source == "aea" else 0,
                "truncated": False,
                "approximate": False,
                "reason": None,
            }
            for source in graph_v2.SOURCE_IDS
        },
    )
    assert {node["class"] for node in payload["nodes"]} <= {
        "State",
        "Intervention",
    }
    assert payload["metadata"]["stateNodes"] == 3
    assert payload["metadata"]["conditionNodes"] == 3
    assert payload["metadata"]["stateNodesOmitted"] == 0
    assert payload["metadata"]["conditionNodesOmitted"] == 0
    assert payload["metadata"]["caps"]["stateNodes"] == graph_v2.MAX_STATE_NODES
    assert payload["metadata"]["caps"]["conditionNodes"] == graph_v2.MAX_STATE_NODES
    assert len([edge for edge in payload["edges"] if edge["kind"] == "evidence"]) == 12
    assert {node["studyCount"] for node in payload["nodes"]} == {1}
    assert payload["meta"]["returned_unique_studies"] == 1


def test_ctgov_graph_projection_unifies_roles_and_keeps_direct_multimap():
    cash = f"{INTERVENTION_BASE}CashTransfer"
    rutf = f"{INTERVENTION_BASE}ReadyToUseTherapeuticFoodIntervention"
    rows = query_v2._ctgov_presentation(
        [
            {
                "source": "CT.gov",
                "nctId": "NCT-OUTCOME",
                "title": "Outcome only",
                "_intervention_names": ["Combo"],
                "condition_mesh_uris": [],
                "intervention_mesh_uris": [],
                "outcomes": [
                    {
                        "measure": "Height-for-age",
                        "state_concept_uri": STUNTING,
                        "state_concept": "Stunting",
                    }
                ],
            },
            {
                "source": "CT.gov",
                "nctId": "NCT-BOTH",
                "title": "Both roles",
                "_intervention_names": ["Combo"],
                "condition_mesh_uris": ["mesh:stunting"],
                "intervention_mesh_uris": [],
                "outcomes": [
                    {
                        "measure": "Stunting",
                        "state_concept_uri": STUNTING,
                        "state_concept": "Stunting",
                    }
                ],
            },
            {
                "source": "CT.gov",
                "nctId": "NCT-CONDITION",
                "title": "Condition only",
                "_intervention_names": ["Combo"],
                "condition_mesh_uris": ["mesh:stunting"],
                "intervention_mesh_uris": [],
                "outcomes": [],
            },
        ],
        {"state": STUNTING},
        {"state": "Stunting"},
        {"mesh:stunting": (STUNTING, "Stunting")},
        {"Combo": ((rutf, "RUTF"), (cash, "Cash transfer"))},
        {},
        {},
        specific_attribution=True,
    )
    assert {
        (row["study_id"], row["state_concept_uri"], row["intervention_concept_uri"])
        for row in rows
    } == {
        (study, STUNTING, intervention)
        for study in ("NCT-OUTCOME", "NCT-BOTH", "NCT-CONDITION")
        for intervention in (rutf, cash)
    }


def test_who_stored_direct_pairs_union_roles_and_dedupe_both_role_study():
    dataset = Dataset()
    source = dataset.graph(URIRef(graph_v2._source_graph("who-ictrp")))
    taxonomy = dataset.graph(URIRef(graph_v2.v1.TAXONOMY_GRAPH_URI))
    study = URIRef("https://example.org/who/1")
    condition_raw = Literal("Stunting condition")
    outcome_raw = Literal("Stunting outcome")
    intervention_raw = Literal("Program A")
    source.add((study, RDF.type, URIRef(f"{graph_v2.v1.UE}Evidence")))
    source.add((study, URIRef(f"{graph_v2.v1.UE}matchesCondition"), URIRef(STUNTING)))
    source.add((study, URIRef(f"{graph_v2.v1.UE}matchesOutcome"), URIRef(STUNTING)))
    source.add((study, URIRef(f"{graph_v2.v1.ICTRP}condition"), condition_raw))
    source.add((study, URIRef(f"{graph_v2.v1.ICTRP}primaryOutcome"), outcome_raw))
    source.add((study, URIRef(f"{graph_v2.v1.ICTRP}intervention"), intervention_raw))

    taxonomy.add((URIRef(STUNTING), RDF.type, URIRef(f"{graph_v2.v1.UE}State")))
    taxonomy.add((URIRef(STUNTING), SKOS.prefLabel, Literal("Stunting")))
    for role, raw in (("conditions", condition_raw), ("outcomes", outcome_raw)):
        entry = URIRef(f"https://universalevidence.com/crosswalk/who-ictrp-{role}/fixture")
        taxonomy.add((entry, RDF.type, URIRef(f"{graph_v2.v1.UE}CrosswalkEntry")))
        taxonomy.add((entry, URIRef(f"{graph_v2.v1.UE}rawText"), raw))
        taxonomy.add((entry, SKOS.exactMatch, URIRef(STUNTING)))
    direct = list(DIRECT_INTERVENTIONS)[:2]
    for index, intervention in enumerate(direct):
        entry = URIRef(
            f"https://universalevidence.com/crosswalk/who-ictrp-interventions/{index}"
        )
        taxonomy.add((entry, URIRef(f"{graph_v2.v1.UE}rawText"), intervention_raw))
        taxonomy.add((entry, SKOS.exactMatch, URIRef(intervention)))
        taxonomy.add((URIRef(intervention), RDF.type, URIRef(f"{graph_v2.v1.UE}Intervention")))
        taxonomy.add((URIRef(intervention), SKOS.prefLabel, Literal(DIRECT_INTERVENTIONS[intervention])))

    rows = dataset.query(
        graph_v2.local_group_query(
            "who-ictrp", _query(state=[STUNTING]), [STUNTING], []
        )
    )
    bindings = [
        {str(key): str(value) for key, value in row.asdict().items()}
        for row in rows
    ]
    pair_bindings = [
        row
        for row in bindings
        if row.get("rowKind") == graph_v2._STORED_PAIR_ROW
    ]
    assert {(row["intervention"], row["weight"]) for row in pair_bindings} == {
        (intervention, "1") for intervention in direct
    }
    assert len(
        [
            row
            for row in bindings
            if row.get("rowKind") == graph_v2._STORED_SELECTION_ROW
        ]
    ) == 1

    detail_bindings = [
        {str(key): str(value) for key, value in row.asdict().items()}
        for row in dataset.query(
            graph_v2.local_detail_query(
                "who-ictrp",
                _query(state=[STUNTING]),
                STUNTING,
                direct[0],
                26,
                [STUNTING],
                direct,
            )
        )
    ]
    assert {
        (row.get("rowKind"), row.get("study")) for row in detail_bindings
    } == {
        (graph_v2._STORED_SELECTION_ROW, None),
        (graph_v2._STORED_DETAIL_ROW, str(study)),
    }
    assert next(
        row["selectedStudyUris"]
        for row in detail_bindings
        if row.get("rowKind") == graph_v2._STORED_SELECTION_ROW
    ) == str(study)


def test_who_stored_projection_keeps_explicit_roles_strict_and_mixed_roots():
    dataset = Dataset()
    source = dataset.graph(URIRef(graph_v2._source_graph("who-ictrp")))
    taxonomy = dataset.graph(URIRef(graph_v2.v1.TAXONOMY_GRAPH_URI))
    study = URIRef("https://example.org/who/role-strict")
    condition_raw = Literal("Direct stunting condition")
    outcome_raw = Literal("Direct wasting outcome")
    intervention_raw = Literal("Direct program")
    intervention = next(iter(DIRECT_INTERVENTIONS))
    source.add((study, RDF.type, URIRef(f"{graph_v2.v1.UE}Evidence")))
    source.add(
        (study, URIRef(f"{graph_v2.v1.UE}matchesCondition"), URIRef(STUNTING))
    )
    source.add(
        (study, URIRef(f"{graph_v2.v1.UE}matchesOutcome"), URIRef(WASTING))
    )
    source.add((study, URIRef(f"{graph_v2.v1.ICTRP}condition"), condition_raw))
    source.add(
        (study, URIRef(f"{graph_v2.v1.ICTRP}primaryOutcome"), outcome_raw)
    )
    source.add(
        (study, URIRef(f"{graph_v2.v1.ICTRP}intervention"), intervention_raw)
    )
    for state in (STUNTING, WASTING):
        taxonomy.add((URIRef(state), RDF.type, URIRef(f"{graph_v2.v1.UE}State")))
        taxonomy.add((URIRef(state), SKOS.prefLabel, Literal(state.rsplit("/", 1)[-1])))
    for role, raw, state in (
        ("conditions", condition_raw, STUNTING),
        ("outcomes", outcome_raw, WASTING),
    ):
        entry = URIRef(
            f"https://universalevidence.com/crosswalk/who-ictrp-{role}/strict"
        )
        taxonomy.add((entry, URIRef(f"{graph_v2.v1.UE}rawText"), raw))
        taxonomy.add((entry, SKOS.exactMatch, URIRef(state)))
    intervention_entry = URIRef(
        "https://universalevidence.com/crosswalk/who-ictrp-interventions/strict"
    )
    taxonomy.add(
        (
            intervention_entry,
            URIRef(f"{graph_v2.v1.UE}rawText"),
            intervention_raw,
        )
    )
    taxonomy.add((intervention_entry, SKOS.exactMatch, URIRef(intervention)))
    taxonomy.add(
        (URIRef(intervention), RDF.type, URIRef(f"{graph_v2.v1.UE}Intervention"))
    )
    taxonomy.add(
        (
            URIRef(intervention),
            SKOS.prefLabel,
            Literal(DIRECT_INTERVENTIONS[intervention]),
        )
    )

    def projected_states(query, retained):
        return {
            str(row.asdict()["state"])
            for row in dataset.query(
                graph_v2.local_group_query(
                    "who-ictrp", query, retained, [intervention]
                )
            )
            if str(row.asdict().get("rowKind")) == graph_v2._STORED_PAIR_ROW
        }

    condition_query = _query(condition=[STUNTING])
    outcome_query = _query(outcome=[WASTING])
    mixed_query = _query(state=[STUNTING], outcome=[WASTING])
    assert projected_states(condition_query, [STUNTING]) == {STUNTING}
    assert projected_states(outcome_query, [WASTING]) == {WASTING}
    assert graph_v2._graph_state_roots(mixed_query) == tuple(
        sorted((STUNTING, WASTING))
    )
    assert projected_states(mixed_query, [STUNTING, WASTING]) == {
        STUNTING,
        WASTING,
    }


def test_stored_region_and_state_any_all_are_in_the_single_capped_selector():
    ssa = "https://universalevidence.com/vocab/regions/SubSaharanAfrica"
    regions = query_v2.default_region_index()
    sparql = graph_v2.local_group_query(
        "aea",
        _query(
            state=[STUNTING, WASTING],
            region=[ssa],
            state_logic="and",
        ),
        [STUNTING, WASTING],
        [],
        regions,
    )
    assert f"(ue:matchesCondition|ue:matchesOutcome) <{STUNTING}>" in sparql
    assert f"(ue:matchesCondition|ue:matchesOutcome) <{WASTING}>" in sparql
    assert "crosswalk/aea-regions/" in sparql
    assert f"LIMIT {graph_v2.UNIQUE_STUDY_LIMIT}" in sparql
    assert f"LIMIT {graph_v2.UNIQUE_STUDY_LIMIT + 1}" in sparql
    assert sparql.count("SELECT DISTINCT ?selectionStudy WHERE") == 1
    assert sparql.count("SELECT DISTINCT ?study WHERE") == 1


def test_stored_detail_uses_the_hydrated_region_index_from_execution(monkeypatch):
    group = URIRef(
        "https://universalevidence.com/vocab/regions/HydratedGroup"
    )
    country = URIRef("https://example.org/regions/HydratedCountry")
    region_graph = Graph()
    region_graph.add((group, SKOS.narrower, country))
    region_graph.add(
        (country, URIRef(f"{graph_v2.v1.UE}iso3166Alpha2"), Literal("ZZ"))
    )
    hydrated_regions = query_v2.RegionIndex(region_graph)
    study = "https://example.org/who/hydrated-region"
    captured = {}
    original_selection_query = graph_v2.stored_selection_query

    async def hydrate(_query):
        return hydrated_regions

    def selection_query(*args, **kwargs):
        regions = kwargs.get("regions") if kwargs else args[2]
        captured["regions"] = regions
        sparql = original_selection_query(*args, **kwargs)
        captured["sparql"] = sparql
        return sparql

    async def select(sparql, _budget):
        if "?rowKind ?role ?rawText" in sparql:
            return [
                {
                    "study": study,
                    "rowKind": graph_v2._STORED_STATE_RAW_ROW,
                    "role": "condition",
                    "rawText": "hydrated state",
                },
                {
                    "study": study,
                    "rowKind": graph_v2._STORED_INTERVENTION_RAW_ROW,
                    "role": "intervention",
                    "rawText": "hydrated intervention",
                },
            ]
        if "?study_id ?title ?status ?date ?country" in sparql:
            return [
                {
                    "study": study,
                    "study_id": "WHO-HYDRATED",
                    "title": "Hydrated region detail",
                }
            ]
        return [{"study": study}]

    async def direct_maps(_source_id, _roles):
        return (
            {"condition": {"hydrated state": (STUNTING,)}},
            {"hydrated intervention": (INHERITED_NUTRITION,)},
        )

    monkeypatch.setattr(graph_v2, "region_index_for_query", hydrate)
    monkeypatch.setattr(graph_v2, "stored_selection_query", selection_query)
    monkeypatch.setattr(graph_v2, "_sparql_select", select)
    monkeypatch.setattr(graph_v2, "_stored_direct_maps", direct_maps)
    payload = asyncio.run(
        graph_v2.execute_edge_details(
            _query(
                state=[STUNTING],
                intervention=[INHERITED_NUTRITION],
                region=[str(group)],
            ),
            STUNTING,
            INHERITED_NUTRITION,
            0,
            25,
            expected_source_counts={"who-ictrp": 1},
            expected_source_membership_digests={
                "who-ictrp": graph_v2._study_membership_digest(
                    "who-ictrp", [study]
                )
            },
            cursor_context="hydrated-region",
        )
    )

    assert captured["regions"] is hydrated_regions
    assert 'VALUES ?regionIso { "ZZ" }' in captured["sparql"]
    assert payload["meta"]["source_counts_reconciled"] is True
    assert payload["meta"]["source_membership_reconciled"] is True
    assert graph_v2._DETAIL_SUPPORT_ID not in payload["results"][0]
    assert payload["meta"]["sound"] is True


def test_stored_detail_propagates_101st_source_limit_uncertainty(monkeypatch):
    selected = [
        f"https://example.org/who/{index:03d}"
        for index in range(graph_v2.UNIQUE_STUDY_LIMIT + 1)
    ]

    async def select(sparql, _budget):
        if "?rowKind ?role ?rawText" in sparql:
            return [
                {
                    "study": selected[0],
                    "rowKind": graph_v2._STORED_STATE_RAW_ROW,
                    "role": "condition",
                    "rawText": "limited state",
                },
                {
                    "study": selected[0],
                    "rowKind": graph_v2._STORED_INTERVENTION_RAW_ROW,
                    "role": "intervention",
                    "rawText": "limited intervention",
                },
            ]
        if "?study_id ?title ?status ?date ?country" in sparql:
            return [
                {
                    "study": selected[0],
                    "study_id": "WHO-LIMITED",
                    "title": "Retained capped detail",
                }
            ]
        return [{"study": study} for study in selected]

    async def direct_maps(_source_id, _roles):
        return (
            {"condition": {"limited state": (STUNTING,)}},
            {"limited intervention": (INHERITED_NUTRITION,)},
        )

    monkeypatch.setattr(graph_v2, "_sparql_select", select)
    monkeypatch.setattr(graph_v2, "_stored_direct_maps", direct_maps)
    payload = asyncio.run(
        graph_v2.execute_edge_details(
            _query(state=[STUNTING], intervention=[INHERITED_NUTRITION]),
            STUNTING,
            INHERITED_NUTRITION,
            0,
            25,
            expected_source_counts={"who-ictrp": 1},
            expected_source_membership_digests={
                "who-ictrp": graph_v2._study_membership_digest(
                    "who-ictrp", [selected[0]]
                )
            },
            cursor_context="source-limit",
        )
    )

    source = payload["meta"]["sources"]["who-ictrp"]
    assert [row["study_id"] for row in payload["results"]] == ["WHO-LIMITED"]
    assert source["reason"] == "source_limit"
    assert source["source_limit_reached"] is True
    assert source["source_limit_omitted_lower_bound"] == 1
    assert payload["meta"]["source_counts_reconciled"] is True
    assert payload["meta"]["source_membership_reconciled"] is True
    assert payload["meta"]["truncated"] is True
    assert payload["meta"]["approximate"] is True
    assert payload["meta"]["sound"] is True


def test_edge_state_is_canonical_and_legacy_alias_is_bounded(monkeypatch):
    edge_id = graph_v2.stable_evidence_edge_id(STUNTING, INHERITED_NUTRITION)

    async def aggregate(_query):
        return {
            "nodes": [],
            "edges": [
                {
                    "id": edge_id,
                    "kind": "evidence",
                    "source": INHERITED_NUTRITION,
                    "target": STUNTING,
                    "source_counts": {"aea": 1},
                    graph_v2._EDGE_MEMBERSHIP_DIGESTS: {
                        "aea": graph_v2._study_membership_digest(
                            "aea", ["aea-1"]
                        )
                    },
                }
            ],
            "metadata": {},
            "meta": {},
        }

    async def details(*_args, **_kwargs):
        return {"results": [], "meta": {"edgeId": edge_id}}

    graph_v2.graph_v2_cache.clear()
    graph_v2.graph_v2_detail_cache.clear()
    monkeypatch.setattr(graph_v2, "execute_graph_v2", aggregate)
    monkeypatch.setattr(graph_v2, "execute_edge_details", details)
    monkeypatch.setattr(graph_v2, "graph_versions", lambda: (None, None, "test"))
    client = TestClient(api_main.app)
    common = {
        "state": STUNTING,
        "edge_intervention": INHERITED_NUTRITION,
    }
    canonical = client.get(
        f"/graph/v2/edges/{edge_id}/studies",
        params={**common, "edge_state": STUNTING},
    )
    legacy = client.get(
        f"/graph/v2/edges/{edge_id}/studies",
        params={**common, "edge_condition": STUNTING},
    )
    conflict = client.get(
        f"/graph/v2/edges/{edge_id}/studies",
        params={
            **common,
            "edge_state": STUNTING,
            "edge_condition": WASTING,
        },
    )
    assert canonical.status_code == 200
    assert legacy.status_code == 200
    assert conflict.status_code == 422


def test_graph_page_emits_only_canonical_state_and_visible_region_wire():
    page = (
        Path(graph_v2.REPO_ROOT)
        / "site/public/labs/malnutrition-evidence-graph.html"
    ).read_text(encoding="utf-8")
    assert "requestedStates" in page
    assert "requestedRegions" in page
    assert "graphParams.append('state'" in page
    assert "graphParams.append('region'" in page
    assert "edge_state: state.id" in page
    assert "Live evidence across" not in page
    assert "n.class === 'State'" in page
    assert 'id="region-query"' in page
    assert "regionInput.addEventListener('keydown'" in page
    assert "addRegionTerm(regionSearchState.suggestions[0])" in page
    assert "if (regionInput.value.trim())" in page
    assert "Select a Region from the suggestions" in page
    assert "edge_condition:" not in page
    assert "graphParams.append('condition'" not in page


class _ProjectedLiveFixtureAdapter:
    """Deterministic transport double that retains the real live projections."""

    def __init__(self, source_id: str, calls: list[tuple[str, dict[str, str]]]):
        self.source_id = source_id
        self.calls = calls

    async def execute(self, spec, budget, _regions):
        budget.claim_request()
        self.calls.append((self.source_id, dict(spec)))
        state_label = "Stunting"
        intervention_label = DIRECT_INTERVENTIONS[
            f"{INTERVENTION_BASE}CashTransfer"
        ]
        cash = f"{INTERVENTION_BASE}CashTransfer"
        if self.source_id == "ctgov":
            rows = query_v2._ctgov_presentation(
                [
                    {
                        "source": "CT.gov",
                        "nctId": "SHARED-REGISTRY-ID",
                        "title": "CT.gov outcome-only fixture",
                        "_intervention_names": ["Direct cash fixture"],
                        "condition_mesh_uris": [],
                        "intervention_mesh_uris": [],
                        "outcomes": [{"measure": "Direct growth outcome"}],
                    }
                ],
                spec,
                {"state": state_label, "intervention": intervention_label},
                {},
                {"Direct cash fixture": ((cash, intervention_label),)},
                {},
                {"Direct growth outcome": ((STUNTING, state_label),)},
                specific_attribution=True,
            )
        else:
            rows = query_v2._stamp_isrctn(
                [
                    {
                        "source": "ISRCTN",
                        "study_id": "SHARED-REGISTRY-ID",
                        "title": "ISRCTN condition-only fixture",
                        "condition_descriptions": ["Direct growth condition"],
                        "drug_names_list": [],
                        "intervention_descriptions": ["Direct cash fixture"],
                        "outcomes": [],
                    }
                ],
                spec,
                {"state": state_label, "intervention": intervention_label},
                condition_crosswalk={
                    query_v2.v1._normalize_xwalk_key("Direct growth condition"): (
                        STUNTING,
                    )
                },
                condition_labels={STUNTING: state_label},
                intervention_crosswalk={
                    query_v2.v1._normalize_xwalk_key("Direct cash fixture"): (
                        cash,
                    )
                },
                intervention_labels={cash: intervention_label},
                outcome_crosswalk={},
                state_labels={STUNTING: state_label},
                specific_attribution=True,
            )
        return query_v2.BranchResult(rows=rows)


def _add_reconciliation_stored_fixture(
    dataset: Dataset,
    *,
    source_id: str,
    study_uri: str,
    study_id: str,
    role: str,
) -> None:
    source = dataset.graph(URIRef(graph_v2._source_graph(source_id)))
    taxonomy = dataset.graph(URIRef(graph_v2.v1.TAXONOMY_GRAPH_URI))
    study = URIRef(study_uri)
    cash = f"{INTERVENTION_BASE}CashTransfer"
    state_raw = Literal(f"{source_id} direct {role} fixture")
    intervention_raw = Literal(f"{source_id} direct cash fixture")
    if source_id == "aea":
        source.add((study, RDF.type, URIRef(f"{graph_v2.v1.AEA}RCTStudy")))
        role_predicate = URIRef(
            f"{graph_v2.v1.AEA}{'primaryOutcome' if role == 'outcome' else 'keyword'}"
        )
    else:
        source.add((study, RDF.type, URIRef(f"{graph_v2.v1.UE}Evidence")))
        role_predicate = URIRef(
            f"{graph_v2.v1.ICTRP}{'primaryOutcome' if role == 'outcome' else 'condition'}"
        )
    source.add((study, role_predicate, state_raw))
    source.add(
        (
            study,
            URIRef(
                f"{graph_v2.v1.AEA if source_id == 'aea' else graph_v2.v1.ICTRP}intervention"
            ),
            intervention_raw,
        )
    )
    source.add(
        (
            study,
            URIRef(f"{graph_v2.v1.UE}matches{role.title()}"),
            URIRef(STUNTING),
        )
    )
    source.add(
        (study, URIRef(f"{graph_v2.v1.UE}matchesIntervention"), URIRef(cash))
    )
    source.add(
        (study, URIRef(f"{graph_v2.v1.DCTERMS}identifier"), Literal(study_id))
    )
    source.add(
        (
            study,
            URIRef(f"{graph_v2.v1.DCTERMS}title"),
            Literal(f"{source_id} stored reconciliation fixture"),
        )
    )

    taxonomy.add((URIRef(STUNTING), RDF.type, URIRef(f"{graph_v2.v1.UE}State")))
    taxonomy.add((URIRef(STUNTING), SKOS.prefLabel, Literal("Stunting")))
    taxonomy.add((URIRef(cash), RDF.type, URIRef(f"{graph_v2.v1.UE}Intervention")))
    taxonomy.add(
        (URIRef(cash), SKOS.prefLabel, Literal(DIRECT_INTERVENTIONS[cash]))
    )
    state_prefix = graph_v2._DIRECT_CROSSWALKS[source_id][role][1]
    intervention_prefix = graph_v2._DIRECT_CROSSWALKS[source_id]["intervention"][1]
    state_entry = URIRef(f"{state_prefix}reconciliation-fixture")
    intervention_entry = URIRef(f"{intervention_prefix}reconciliation-fixture")
    taxonomy.add(
        (state_entry, URIRef(f"{graph_v2.v1.UE}rawText"), state_raw)
    )
    taxonomy.add((state_entry, SKOS.exactMatch, URIRef(STUNTING)))
    taxonomy.add(
        (
            intervention_entry,
            URIRef(f"{graph_v2.v1.UE}rawText"),
            intervention_raw,
        )
    )
    taxonomy.add((intervention_entry, SKOS.exactMatch, URIRef(cash)))


async def _run_complete_reconciliation_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], dict[str, Any], list[tuple[str, dict[str, str]]]]:
    cash = f"{INTERVENTION_BASE}CashTransfer"
    dataset = Dataset()
    _add_reconciliation_stored_fixture(
        dataset,
        source_id="aea",
        study_uri="https://example.org/aea/reconciliation",
        study_id="AEARCTR-RECONCILIATION",
        role="outcome",
    )
    _add_reconciliation_stored_fixture(
        dataset,
        source_id="who-ictrp",
        study_uri="https://example.org/who/reconciliation",
        study_id="WHO-RECONCILIATION",
        role="condition",
    )
    live_calls: list[tuple[str, dict[str, str]]] = []
    adapters = {
        source_id: _ProjectedLiveFixtureAdapter(source_id, live_calls)
        for source_id in ("ctgov", "isrctn")
    }
    stored_shapes: list[tuple[str, str]] = []

    async def select(sparql: str, _budget) -> list[dict[str, str]]:
        source_id = next(
            source
            for source in ("aea", "who-ictrp")
            if f"GRAPH <{graph_v2._source_graph(source)}>" in sparql
        )
        shape = (
            "detail"
            if "?study_id ?title ?status ?date ?country" in sparql
            else "attribution"
            if "?rowKind ?role ?rawText" in sparql
            else "selection"
        )
        stored_shapes.append((source_id, shape))
        return [
            {str(key): str(value) for key, value in row.asdict().items()}
            for row in dataset.query(sparql)
        ]

    async def regions(_query):
        return query_v2.default_region_index()

    monkeypatch.setattr(graph_v2, "_sparql_select", select)
    monkeypatch.setattr(
        graph_v2,
        "_stored_direct_maps",
        lambda source_id, roles: _direct_maps_from_dataset(
            dataset, source_id, tuple(roles)
        ),
    )
    monkeypatch.setattr(graph_v2, "region_index_for_query", regions)
    monkeypatch.setattr(graph_v2, "default_adapters", lambda: adapters)
    query = _query(state=[STUNTING], intervention=[cash])
    aggregate = await graph_v2.execute_graph_v2(query)
    edge = next(
        item
        for item in aggregate["edges"]
        if item["kind"] == "evidence"
        and item["target"] == STUNTING
        and item["source"] == cash
    )
    details = await graph_v2.execute_edge_details(
        query,
        STUNTING,
        cash,
        0,
        25,
        expected_source_counts=edge["source_counts"],
        expected_source_membership_digests=edge[
            graph_v2._EDGE_MEMBERSHIP_DIGESTS
        ],
        cursor_context="complete-reconciliation",
    )
    assert sorted(stored_shapes) == sorted(
        [
            ("aea", "selection"),
            ("aea", "attribution"),
            ("who-ictrp", "selection"),
            ("who-ictrp", "attribution"),
            ("aea", "selection"),
            ("aea", "attribution"),
            ("aea", "detail"),
            ("who-ictrp", "selection"),
            ("who-ictrp", "attribution"),
            ("who-ictrp", "detail"),
        ]
    )
    return aggregate, details, live_calls


def test_complete_aggregate_to_detail_reconciliation_for_all_four_sources(
    monkeypatch,
):
    aggregate, details, live_calls = asyncio.run(
        _run_complete_reconciliation_fixture(monkeypatch)
    )
    cash = f"{INTERVENTION_BASE}CashTransfer"
    edge = next(
        item
        for item in aggregate["edges"]
        if item["kind"] == "evidence"
        and item["target"] == STUNTING
        and item["source"] == cash
    )
    expected_counts = {source: 1 for source in graph_v2.SOURCE_IDS}

    assert edge["weight"] == 4
    assert edge["source_counts"] == expected_counts
    assert aggregate["meta"]["returned_unique_studies"] == 4
    assert {
        source: {
            key: aggregate["meta"]["sources"][source][key]
            for key in (
                "status",
                "selected_unique_studies",
                "attributed_unique_studies",
                "returned_unique_studies",
                "truncated",
                "approximate",
            )
        }
        for source in graph_v2.SOURCE_IDS
    } == {
        source: {
            "status": "included",
            "selected_unique_studies": 1,
            "attributed_unique_studies": 1,
            "returned_unique_studies": 1,
            "truncated": False,
            "approximate": False,
        }
        for source in graph_v2.SOURCE_IDS
    }
    assert {
        node["studyCount"]
        for node in aggregate["nodes"]
        if node["id"] in {STUNTING, cash}
    } == {4}
    assert details["meta"]["source_counts"] == expected_counts
    assert details["meta"]["detail_population_source_counts"] == expected_counts
    assert details["meta"]["source_counts_reconciled"] is True
    assert details["meta"]["nextCursor"] is None
    assert details["meta"]["truncated"] is False
    assert details["meta"]["approximate"] is False
    assert details["meta"]["sound"] is True
    assert details["meta"]["returned"] == edge["weight"]
    assert len(live_calls) == 4
    assert [source for source, _spec in live_calls].count("ctgov") == 2
    assert [source for source, _spec in live_calls].count("isrctn") == 2
    assert all(
        spec == {"state": STUNTING, "intervention": cash}
        for _source, spec in live_calls
    )


def test_same_study_id_in_two_sources_survives_aggregate_and_detail_merge(
    monkeypatch,
):
    aggregate, details, _live_calls = asyncio.run(
        _run_complete_reconciliation_fixture(monkeypatch)
    )
    cash = f"{INTERVENTION_BASE}CashTransfer"
    edge = next(
        item
        for item in aggregate["edges"]
        if item["kind"] == "evidence"
        and item["target"] == STUNTING
        and item["source"] == cash
    )
    shared = [
        row
        for row in details["results"]
        if row["study_id"] == "SHARED-REGISTRY-ID"
    ]

    assert edge["source_counts"]["ctgov"] == 1
    assert edge["source_counts"]["isrctn"] == 1
    assert {(row["source"], row["study_id"]) for row in shared} == {
        ("CT.gov", "SHARED-REGISTRY-ID"),
        ("ISRCTN", "SHARED-REGISTRY-ID"),
    }
    assert len(shared) == 2
