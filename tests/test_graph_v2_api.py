from __future__ import annotations

import asyncio
import json
import time

from fastapi.testclient import TestClient
import pytest

import api.main as api_main
from api.graph_v2_cache import GraphV2Cache
import api.routes.graph_v2 as graph_v2


ROOT = "https://universalevidence.com/vocab/states/Malnutrition"
CHILD = "https://universalevidence.com/vocab/states/ChildMalnutrition"
WASTING = "https://universalevidence.com/vocab/states/Wasting"
MALARIA = "https://universalevidence.com/vocab/states/Malaria"
INTERVENTION = "https://universalevidence.com/vocab/interventions/NutritionIntervention"
NUTRITIONAL_SUPPORT = "https://universalevidence.com/vocab/interventions/NutritionalSupport"
RUTF = "https://universalevidence.com/vocab/interventions/ReadyToUseTherapeuticFoodIntervention"


def _meta(count: int = 0):
    return {
        "status": "included",
        "coverage": "full",
        "returned_unique_studies": count,
        "truncated": False,
        "approximate": False,
        "reason": None,
    }


def _query(**values):
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


def _direct_state_live_rows():
    def condition(source, study_id, intervention=INTERVENTION):
        return {
            "source": source,
            "study_id": study_id,
            "state_concept_uri": CHILD,
            "state_concept": "Child malnutrition",
            "condition_concept_uri": CHILD,
            "condition_concept": "Child malnutrition",
            "intervention_concept_uri": intervention,
            "intervention_concept": intervention.rsplit("/", 1)[-1],
            "outcomes": [],
        }

    def outcome(source, study_id, intervention=INTERVENTION):
        return {
            "source": source,
            "study_id": study_id,
            "state_concept_uri": CHILD,
            "state_concept": "Child malnutrition",
            "intervention_concept_uri": intervention,
            "intervention_concept": intervention.rsplit("/", 1)[-1],
            "outcomes": [
                {
                    "measure": "Direct child outcome",
                    "state_concept_uri": CHILD,
                    "state_concept": "Child malnutrition",
                }
            ],
        }

    duplicate_roles = condition("CT.gov", "C-DUPLICATE-ROLES")
    duplicate_roles["outcomes"] = outcome(
        "CT.gov", "C-DUPLICATE-ROLES"
    )["outcomes"]
    return {
        "ctgov": [
            condition("CT.gov", "C-CONDITION"),
            outcome("CT.gov", "C-OUTCOME"),
            duplicate_roles,
            condition("CT.gov", "SHARED-ID"),
            condition("CT.gov", "C-MULTI-INTERVENTION"),
            condition("CT.gov", "C-MULTI-INTERVENTION", RUTF),
            {
                "source": "CT.gov",
                "study_id": "C-FALLBACK-ONLY",
                "intervention_concept_uri": INTERVENTION,
                "outcomes": [],
            },
            {
                "source": "CT.gov",
                "study_id": "C-OUTSIDE-SUBTREE",
                "state_concept_uri": MALARIA,
                "condition_concept_uri": MALARIA,
                "intervention_concept_uri": INTERVENTION,
                "outcomes": [],
            },
        ],
        "isrctn": [
            outcome("ISRCTN", "I-OUTCOME"),
            outcome("ISRCTN", "SHARED-ID"),
            outcome("ISRCTN", "I-MULTI-INTERVENTION", RUTF),
        ],
    }


def _direct_state_state_taxonomy():
    return {
        "nodes": [
            {
                "id": ROOT,
                "label": "Malnutrition",
                "class": "State",
                "studyCount": 0,
                "depth": 0,
                "selected": True,
            },
            {
                "id": CHILD,
                "label": "Child malnutrition",
                "class": "State",
                "studyCount": 0,
                "depth": 1,
                "selected": False,
            },
        ],
        "pairs": [(ROOT, CHILD)],
        "roots": [ROOT],
        "omitted": 0,
    }


def _direct_state_intervention_taxonomy():
    return {
        "nodes": [
            {
                "id": INTERVENTION,
                "label": "Nutrition intervention",
                "class": "Intervention",
                "studyCount": 0,
                "depth": 0,
                "selected": True,
            },
            {
                "id": RUTF,
                "label": "Ready-to-use therapeutic food",
                "class": "Intervention",
                "studyCount": 0,
                "depth": 1,
                "selected": False,
            },
        ],
        "pairs": [(INTERVENTION, RUTF)],
        "roots": [INTERVENTION],
        "omitted": 0,
    }


def _direct_state_aggregate_payload():
    live_rows = _direct_state_live_rows()
    return graph_v2.build_graph_v2_payload(
        query=_query(state=[ROOT], intervention=[INTERVENTION]),
        condition_taxonomy=_direct_state_state_taxonomy(),
        intervention_taxonomy=_direct_state_intervention_taxonomy(),
        local_rows={"aea": [], "who-ictrp": []},
        live_rows=live_rows,
        source_meta={
            "aea": _meta(),
            "ctgov": _meta(7),
            "isrctn": _meta(3),
            "who-ictrp": _meta(),
        },
    )


def test_tracked_malnutrition_and_malaria_subtrees_are_deterministic():
    malnutrition = graph_v2.condition_subtree(ROOT)
    malnutrition_again = graph_v2.condition_subtree(ROOT)
    malaria = graph_v2.condition_subtree(
        "https://universalevidence.com/vocab/states/Malaria"
    )

    assert malnutrition == malnutrition_again
    assert malnutrition["nodes"][0]["id"] == ROOT
    assert len(malnutrition["pairs"]) == len(malnutrition["nodes"]) - 1
    assert len(malaria["nodes"]) == 6
    assert malaria["nodes"][0]["label"] == "Malaria"
    assert malaria["omitted"] == 0


def test_multi_root_condition_and_intervention_forests_retain_selected_roots():
    malaria = "https://universalevidence.com/vocab/states/Malaria"
    cash = "https://universalevidence.com/vocab/interventions/CashTransfer"
    conditions = graph_v2.condition_forest([ROOT, malaria])
    interventions = graph_v2.intervention_forest([INTERVENTION, cash])

    assert {node["id"] for node in conditions["nodes"] if node["selected"]} == {
        ROOT,
        malaria,
    }
    assert {node["id"] for node in interventions["nodes"] if node["selected"]} == {
        INTERVENTION,
        cash,
    }
    assert conditions == graph_v2.condition_forest([ROOT, malaria])
    assert interventions == graph_v2.intervention_forest([INTERVENTION, cash])
    assert len(conditions["nodes"]) <= graph_v2.MAX_CONDITION_NODES
    assert len(interventions["nodes"]) <= graph_v2.MAX_INTERVENTION_NODES


def test_combined_selection_retains_both_complete_descendant_forests_and_their_evidence():
    conditions = graph_v2.condition_forest([ROOT])
    interventions = graph_v2.intervention_forest([NUTRITIONAL_SUPPORT])
    query = graph_v2._graph_query(
        condition=[ROOT],
        intervention=[NUTRITIONAL_SUPPORT],
        outcome=[],
        region=[],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )

    payload = graph_v2.build_graph_v2_payload(
        query=query,
        condition_taxonomy=conditions,
        intervention_taxonomy=interventions,
        local_rows={
            "aea": [
                {
                    "condition": CHILD,
                    "intervention": RUTF,
                    "interventionLabel": "Ready-to-use therapeutic food",
                    "studyUris": "https://example.org/rutf-child-malnutrition",
                }
            ],
            "who-ictrp": [],
        },
        live_rows={"ctgov": [], "isrctn": []},
        source_meta={
            source: _meta(1 if source == "aea" else 0)
            for source in graph_v2.SOURCE_IDS
        },
    )

    expected_node_ids = {
        node["id"] for node in conditions["nodes"] + interventions["nodes"]
    }
    assert {node["id"] for node in payload["nodes"]} == expected_node_ids

    hierarchy_pairs = {
        (edge["source"], edge["target"])
        for edge in payload["edges"]
        if edge["kind"] == "hierarchy"
    }
    assert hierarchy_pairs == set(conditions["pairs"] + interventions["pairs"])

    evidence = next(
        edge
        for edge in payload["edges"]
        if edge["kind"] == "evidence"
        and edge["source"] == RUTF
        and edge["target"] == CHILD
    )
    assert evidence["weight"] == 1
    assert evidence["source_counts"] == {"aea": 1}
    assert payload["metadata"]["conditionNodesOmitted"] == 0
    assert payload["metadata"]["interventionNodesOmitted"] == 0


def test_local_group_query_pushes_boolean_axes_before_grouping():
    malaria = "https://universalevidence.com/vocab/states/Malaria"
    cash = "https://universalevidence.com/vocab/interventions/CashTransfer"
    query = graph_v2._graph_query(
        condition=[ROOT, malaria],
        intervention=[INTERVENTION, cash],
        outcome=[],
        region=[],
        condition_logic="and",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )
    sparql = graph_v2.local_group_query(
        "aea", query, [ROOT, malaria], [INTERVENTION, cash]
    )

    assert f"?selectionStudy ue:matchesCondition <{ROOT}>" in sparql
    assert f"?selectionStudy ue:matchesCondition <{malaria}>" in sparql
    assert f"?study ue:matchesIntervention <{INTERVENTION}>" in sparql
    assert f"?study ue:matchesIntervention <{cash}>" in sparql
    assert f"LIMIT {graph_v2.UNIQUE_STUDY_LIMIT}" in sparql
    assert f"LIMIT {graph_v2.UNIQUE_STUDY_LIMIT + 1}" in sparql
    assert "ORDER BY ?rowOrder DESC(?weight)" in sparql
    assert "ORDER BY DESC(?rowOrder)" not in sparql
    assert "?state ?stateLabel ?intervention ?interventionLabel" in sparql


def test_stored_runtime_queries_bound_studies_before_raw_attribution():
    query = graph_v2._graph_query(
        state=[ROOT],
        condition=[],
        intervention=[],
        outcome=[],
        region=[],
        state_logic="or",
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )
    selector = graph_v2.stored_selection_query("who-ictrp", query)
    raw = graph_v2.stored_raw_attribution_query(
        "who-ictrp", ("https://example.org/study/1",)
    )

    assert "(ue:matchesCondition|ue:matchesOutcome)" in selector
    assert f"LIMIT {graph_v2.UNIQUE_STUDY_LIMIT + 1}" in selector
    assert "ue:rawText" not in selector
    assert "VALUES ?study { <https://example.org/study/1> }" in raw
    assert 'BIND("condition" AS ?role)' in raw
    assert 'BIND("outcome" AS ?role)' in raw
    assert 'BIND("intervention" AS ?role)' in raw
    assert graph_v2.v1.TAXONOMY_GRAPH_URI not in raw
    assert "sameTerm(" not in raw
    assert "REPLACE(" not in raw


@pytest.mark.parametrize(
    ("population_size", "source_limited", "pair_weight"),
    [(graph_v2.UNIQUE_STUDY_LIMIT, False, 2), (graph_v2.UNIQUE_STUDY_LIMIT + 1, True, 1)],
)
def test_stored_selection_cap_uses_101st_sentinel_independent_of_attribution(
    monkeypatch, population_size, source_limited, pair_weight
):
    selected = [
        f"https://example.org/study/{index:03d}"
        for index in range(population_size)
    ]
    pair_studies = selected[-2:]
    captured = []

    async def select(sparql, _budget):
        captured.append(sparql)
        if "?rowKind ?role ?rawText" not in sparql:
            return [{"study": study} for study in selected]
        return [
            row
            for study in pair_studies
            for row in (
                {
                    "study": study,
                    "rowKind": graph_v2._STORED_STATE_RAW_ROW,
                    "role": "condition",
                    "rawText": "direct state",
                },
                {
                    "study": study,
                    "rowKind": graph_v2._STORED_INTERVENTION_RAW_ROW,
                    "role": "intervention",
                    "rawText": "direct intervention",
                },
            )
        ]

    async def direct_maps(_source_id, _roles):
        return (
            {"condition": {"direct state": (ROOT,)}},
            {"direct intervention": (INTERVENTION,)},
        )

    monkeypatch.setattr(graph_v2, "_sparql_select", select)
    monkeypatch.setattr(graph_v2, "_stored_direct_maps", direct_maps)
    query = graph_v2._graph_query(
        condition=[ROOT],
        intervention=[INTERVENTION],
        outcome=[],
        region=[],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )

    rows, meta, _duration = asyncio.run(
        graph_v2.load_local_groups(
            "aea", query, [ROOT], [INTERVENTION]
        )
    )

    assert len(captured) == 2
    assert f"LIMIT {graph_v2.UNIQUE_STUDY_LIMIT + 1}" in captured[0]
    assert "ue:rawText" not in captured[0]
    assert "VALUES ?study" in captured[1]
    assert graph_v2.v1.TAXONOMY_GRAPH_URI not in captured[1]
    assert meta["returned_unique_studies"] == graph_v2.UNIQUE_STUDY_LIMIT
    assert meta["source_limit_reached"] is source_limited
    assert meta["source_limit_omitted_lower_bound"] == int(source_limited)
    assert meta["reason"] == ("source_limit" if source_limited else None)
    assert rows[0]["weight"] == str(pair_weight)
    assert len(rows[0]["studyUris"].split(graph_v2.GROUP_SEPARATOR)) == pair_weight


def test_stored_python_attribution_preserves_direct_multimaps_and_role_dedupe(
    monkeypatch,
):
    study = "https://example.org/study/direct"

    async def select(sparql, _budget):
        if "?rowKind ?role ?rawText" not in sparql:
            return [{"study": study}]
        return [
            {
                "study": study,
                "rowKind": graph_v2._STORED_STATE_RAW_ROW,
                "role": role,
                "rawText": "  direct   state  ",
            }
            for role in ("condition", "outcome")
        ] + [
            {
                "study": study,
                "rowKind": graph_v2._STORED_INTERVENTION_RAW_ROW,
                "role": "intervention",
                "rawText": " direct  intervention ",
            }
        ]

    async def direct_maps(_source_id, _roles):
        return (
            {
                "condition": {"direct state": (CHILD,)},
                "outcome": {"direct state": (CHILD,)},
            },
            {"direct intervention": (INTERVENTION, RUTF)},
        )

    monkeypatch.setattr(graph_v2, "_sparql_select", select)
    monkeypatch.setattr(graph_v2, "_stored_direct_maps", direct_maps)
    query = graph_v2._graph_query(
        state=[ROOT],
        condition=[],
        intervention=[],
        outcome=[],
        region=[],
        state_logic="or",
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )

    selected, support, limited = asyncio.run(
        graph_v2._load_stored_support(
            "aea", query, [ROOT, CHILD], []
        )
    )

    assert selected == (study,)
    assert limited is False
    assert support == {
        (CHILD, INTERVENTION): (study,),
        (CHILD, RUTF): (study,),
    }
    assert all(state != ROOT for state, _intervention in support)


def test_stored_dynamic_state_projection_treats_empty_retained_forest_as_unrestricted(
    monkeypatch,
):
    study = "https://example.org/study/dynamic"

    async def select(sparql, _budget):
        if "?rowKind ?role ?rawText" not in sparql:
            return [{"study": study}]
        return [
            {
                "study": study,
                "rowKind": graph_v2._STORED_STATE_RAW_ROW,
                "role": "condition",
                "rawText": "dynamic state",
            },
            {
                "study": study,
                "rowKind": graph_v2._STORED_INTERVENTION_RAW_ROW,
                "role": "intervention",
                "rawText": "selected intervention",
            },
        ]

    async def direct_maps(_source_id, _roles):
        return (
            {"condition": {"dynamic state": (CHILD,)}, "outcome": {}},
            {"selected intervention": (INTERVENTION,)},
        )

    monkeypatch.setattr(graph_v2, "_sparql_select", select)
    monkeypatch.setattr(graph_v2, "_stored_direct_maps", direct_maps)
    query = graph_v2._graph_query(
        state=[],
        condition=[],
        intervention=[INTERVENTION],
        outcome=[],
        region=[],
        state_logic="or",
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )

    _selected, support, _limited = asyncio.run(
        graph_v2._load_stored_support(
            "aea", query, [], [INTERVENTION]
        )
    )

    assert support == {(CHILD, INTERVENTION): (study,)}


def test_stored_transport_timeouts_are_reported_as_cacheable_budget_exhaustion(
    monkeypatch,
):
    async def regions(_query):
        return graph_v2.RegionIndex(graph_v2.Graph())

    async def stored_timeout(*_args, **_kwargs):
        raise graph_v2.httpx.ReadTimeout("stored source deadline")

    async def live_empty(_query, adapter, _regions):
        return [], _meta()

    monkeypatch.setattr(graph_v2, "region_index_for_query", regions)
    monkeypatch.setattr(graph_v2, "load_local_groups", stored_timeout)
    monkeypatch.setattr(graph_v2, "execute_graph_source", live_empty)
    monkeypatch.setattr(
        graph_v2,
        "default_adapters",
        lambda: {"ctgov": object(), "isrctn": object()},
    )
    query = graph_v2._graph_query(
        state=[ROOT],
        condition=[],
        intervention=[],
        outcome=[],
        region=[],
        state_logic="or",
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )

    payload = asyncio.run(graph_v2.execute_graph_v2(query))

    for source_id in ("aea", "who-ictrp"):
        source = payload["meta"]["sources"][source_id]
        assert source["status"] == "included"
        assert source["reason"] == "budget_exhausted"
        assert source["budget_exhausted"] is True
    assert graph_v2.GraphV2Cache.cacheable(payload) is True


def test_graph_deadline_cancels_and_reaps_all_pending_source_tasks(monkeypatch):
    cancelled: list[str] = []

    async def regions(_query):
        return graph_v2.RegionIndex(graph_v2.Graph())

    async def wait_until_cancelled(source_id):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(source_id)

    async def stored(source_id, *_args, **_kwargs):
        await wait_until_cancelled(source_id)

    async def live(_query, adapter, _regions):
        await wait_until_cancelled(adapter)

    monkeypatch.setattr(graph_v2, "GRAPH_DEADLINE_SECONDS", 0.01)
    monkeypatch.setattr(graph_v2, "region_index_for_query", regions)
    monkeypatch.setattr(graph_v2, "load_local_groups", stored)
    monkeypatch.setattr(graph_v2, "execute_graph_source", live)
    monkeypatch.setattr(
        graph_v2,
        "default_adapters",
        lambda: {"ctgov": "ctgov", "isrctn": "isrctn"},
    )
    query = graph_v2._graph_query(
        state=[ROOT],
        condition=[],
        intervention=[],
        outcome=[],
        region=[],
        state_logic="or",
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )

    started = time.monotonic()
    payload = asyncio.run(graph_v2.execute_graph_v2(query))
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert sorted(cancelled) == sorted(graph_v2.SOURCE_IDS)
    assert all(
        source["reason"] == "budget_exhausted"
        for source in payload["meta"]["sources"].values()
    )


def test_graph_v2_payload_uses_specific_children_and_distinct_source_counts():
    subtree = {
        "nodes": [
            {"id": ROOT, "label": "Malnutrition", "class": "Condition", "studyCount": 0, "depth": 0},
            {"id": CHILD, "label": "Child malnutrition", "class": "Condition", "studyCount": 0, "depth": 1},
        ],
        "pairs": [(ROOT, CHILD)],
        "total": 2,
        "omitted": 0,
    }
    query = graph_v2._graph_query(
        condition=[ROOT],
        intervention=[],
        outcome=[],
        region=[],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )
    payload = graph_v2.build_graph_v2_payload(
        query=query,
        condition_taxonomy=subtree,
        intervention_taxonomy={"nodes": [], "pairs": [], "roots": [], "omitted": 0},
        local_rows={
            "aea": [
                {
                    "condition": CHILD,
                    "intervention": INTERVENTION,
                    "interventionLabel": "Nutrition intervention",
                    "studyUris": "https://example.org/a1|||https://example.org/a2",
                }
            ],
            "who-ictrp": [],
        },
        live_rows={
            "ctgov": [
                {
                    "source": "CT.gov",
                    "study_id": "NCT1",
                    "condition_concept_uri": ROOT,
                    "intervention_concept_uri": INTERVENTION,
                    "intervention_concept": "Nutrition intervention",
                },
                {
                    "source": "CT.gov",
                    "study_id": "NCT1",
                    "condition_concept_uri": ROOT,
                    "intervention_concept_uri": INTERVENTION,
                    "intervention_concept": "Nutrition intervention",
                },
                {
                    "source": "CT.gov",
                    "study_id": "NCT2",
                    "condition_concept_uri": ROOT,
                    "intervention_concept_uri": "",
                },
            ],
            "isrctn": [],
        },
        source_meta={
            source: _meta(2 if source in {"aea", "ctgov"} else 0)
            for source in graph_v2.SOURCE_IDS
        },
    )

    hierarchy = next(edge for edge in payload["edges"] if edge["kind"] == "hierarchy")
    assert (hierarchy["source"], hierarchy["target"]) == (ROOT, CHILD)

    child_edge = next(
        edge
        for edge in payload["edges"]
        if edge["kind"] == "evidence" and edge["target"] == CHILD
    )
    root_edge = next(
        edge
        for edge in payload["edges"]
        if edge["kind"] == "evidence" and edge["target"] == ROOT
    )
    assert child_edge["weight"] == 2
    assert child_edge["source_counts"] == {"aea": 2}
    assert root_edge["weight"] == 1
    assert root_edge["source_counts"] == {"ctgov": 1}
    assert payload["meta"]["sources"]["ctgov"]["selected_unique_studies"] == 2
    assert payload["meta"]["sources"]["ctgov"]["attributed_unique_studies"] == 1
    assert payload["meta"]["sources"]["ctgov"]["returned_unique_studies"] == 1
    assert payload["meta"]["sources"]["ctgov"]["omitted_unattributed_unique_studies"] == 1
    assert "studies" not in child_edge
    assert "studyIds" not in child_edge
    assert payload["meta"]["schemaVersion"] == "graph-v2"


def test_live_aggregate_requires_direct_coordinates_and_source_qualified_identity():
    payload = _direct_state_aggregate_payload()
    evidence = {
        (edge["target"], edge["source"]): edge
        for edge in payload["edges"]
        if edge["kind"] == "evidence"
    }

    assert set(evidence) == {(CHILD, INTERVENTION), (CHILD, RUTF)}
    intervention_edge = evidence[(CHILD, INTERVENTION)]
    assert intervention_edge["weight"] == 7
    assert intervention_edge["source_counts"] == {"ctgov": 5, "isrctn": 2}
    assert intervention_edge[graph_v2._EDGE_MEMBERSHIP_DIGESTS] == {
        "ctgov": graph_v2._study_membership_digest(
            "ctgov",
            [
                "C-CONDITION",
                "C-OUTCOME",
                "C-DUPLICATE-ROLES",
                "SHARED-ID",
                "C-MULTI-INTERVENTION",
            ],
        ),
        "isrctn": graph_v2._study_membership_digest(
            "isrctn", ["I-OUTCOME", "SHARED-ID"]
        ),
    }

    multimap_edge = evidence[(CHILD, RUTF)]
    assert multimap_edge["weight"] == 2
    assert multimap_edge["source_counts"] == {"ctgov": 1, "isrctn": 1}
    assert payload["meta"]["returned_unique_studies"] == 8
    assert {
        key: payload["meta"]["sources"]["ctgov"][key]
        for key in (
            "selected_unique_studies",
            "attributed_unique_studies",
            "returned_unique_studies",
            "omitted_unattributed_unique_studies",
        )
    } == {
        "selected_unique_studies": 7,
        "attributed_unique_studies": 5,
        "returned_unique_studies": 5,
        "omitted_unattributed_unique_studies": 2,
    }
    assert {
        key: payload["meta"]["sources"]["isrctn"][key]
        for key in (
            "selected_unique_studies",
            "attributed_unique_studies",
            "returned_unique_studies",
            "omitted_unattributed_unique_studies",
        )
    } == {
        "selected_unique_studies": 3,
        "attributed_unique_studies": 3,
        "returned_unique_studies": 3,
        "omitted_unattributed_unique_studies": 0,
    }
    assert all(
        edge["target"] != ROOT
        for edge in evidence.values()
    )


def test_intervention_only_dynamic_condition_cap_is_disclosed():
    query = graph_v2._graph_query(
        condition=[],
        intervention=[INTERVENTION],
        outcome=[],
        region=[],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )
    rows = [
        {
            "condition": f"https://universalevidence.com/vocab/states/Test{i}",
            "conditionLabel": f"Test {i}",
            "intervention": INTERVENTION,
            "interventionLabel": "Nutrition intervention",
            "studyUris": f"https://example.org/study/{i}",
        }
        for i in range(graph_v2.MAX_CONDITION_NODES + 3)
    ]
    payload = graph_v2.build_graph_v2_payload(
        query=query,
        condition_taxonomy={"nodes": [], "pairs": [], "roots": [], "omitted": 0},
        intervention_taxonomy={
            "nodes": [{"id": INTERVENTION, "label": "Nutrition intervention", "class": "Intervention", "studyCount": 0, "depth": 0, "selected": True}],
            "pairs": [],
            "roots": [INTERVENTION],
            "omitted": 0,
        },
        local_rows={"aea": rows, "who-ictrp": []},
        live_rows={"ctgov": [], "isrctn": []},
        source_meta={source: _meta() for source in graph_v2.SOURCE_IDS},
    )

    assert payload["metadata"]["conditionNodes"] == graph_v2.MAX_CONDITION_NODES
    assert payload["metadata"]["conditionNodesOmitted"] == 3
    assert payload["meta"]["truncated"] is True


def test_intervention_only_live_rows_without_conditions_support_no_edge():
    query = graph_v2._graph_query(
        condition=[],
        intervention=[INTERVENTION],
        outcome=[],
        region=[],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )
    condition_ids: set[str] = set()
    intervention_ids = {INTERVENTION}

    assert graph_v2._live_edge_coordinates(
        {"condition_concept_uri": "", "intervention_concept_uri": INTERVENTION},
        query,
        condition_ids,
        intervention_ids,
    ) == ()
    assert graph_v2._live_edge_coordinates(
        {"condition_concept_uri": ROOT, "intervention_concept_uri": INTERVENTION},
        query,
        condition_ids,
        intervention_ids,
    ) == ((ROOT, INTERVENTION),)


@pytest.mark.parametrize(
    "query",
    [
        _query(state=[ROOT]),
        _query(state=[ROOT, MALARIA]),
        _query(condition=[ROOT]),
        _query(outcome=[ROOT]),
    ],
    ids=["one-state", "multiple-states", "strict-condition", "strict-outcome"],
)
def test_selected_roots_never_supply_missing_live_state_coordinates(query):
    assert graph_v2._live_edge_coordinates(
        {"intervention_concept_uri": INTERVENTION, "outcomes": []},
        query,
        {ROOT, CHILD, WASTING, MALARIA},
        {INTERVENTION},
    ) == ()


def test_live_edge_coordinates_are_in_scope_deduplicated_and_role_strict():
    row = {
        "state_concept_uri": WASTING,
        "condition_concept_uri": CHILD,
        "intervention_concept_uri": INTERVENTION,
        "outcomes": [
            {"state_concept_uri": WASTING},
            {"state_concept_uri": WASTING},
        ],
    }
    state_ids = {ROOT, CHILD, WASTING}
    intervention_ids = {INTERVENTION}

    assert graph_v2._live_edge_coordinates(
        row,
        _query(state=[ROOT]),
        state_ids,
        intervention_ids,
    ) == ((WASTING, INTERVENTION), (CHILD, INTERVENTION))
    assert graph_v2._live_edge_coordinates(
        row,
        _query(condition=[ROOT]),
        state_ids,
        intervention_ids,
    ) == ((CHILD, INTERVENTION),)
    assert graph_v2._live_edge_coordinates(
        row,
        _query(outcome=[ROOT]),
        state_ids,
        intervention_ids,
    ) == ((WASTING, INTERVENTION),)

    assert graph_v2._live_edge_coordinates(
        {
            "state_concept_uri": MALARIA,
            "condition_concept_uri": MALARIA,
            "intervention_concept_uri": INTERVENTION,
            "outcomes": [],
        },
        _query(state=[ROOT]),
        state_ids,
        intervention_ids,
    ) == ()
    assert graph_v2._live_edge_coordinates(
        {
            "state_concept_uri": MALARIA,
            "condition_concept_uri": MALARIA,
            "intervention_concept_uri": INTERVENTION,
            "outcomes": [],
        },
        _query(state=[ROOT, MALARIA]),
        {ROOT, CHILD, WASTING, MALARIA},
        intervention_ids,
    ) == ((MALARIA, INTERVENTION),)


def test_strict_outcome_accepts_direct_top_level_multimap_projection():
    assert graph_v2._live_edge_coordinates(
        {
            "state_concept_uri": WASTING,
            "intervention_concept_uri": INTERVENTION,
            "outcomes": [{"measure": "Direct multi-mapped outcome"}],
        },
        _query(outcome=[ROOT]),
        {ROOT, CHILD, WASTING},
        {INTERVENTION},
    ) == ((WASTING, INTERVENTION),)


def test_evidence_edge_ids_are_deterministic_and_pair_specific():
    first = graph_v2.stable_evidence_edge_id(CHILD, INTERVENTION)
    assert first == graph_v2.stable_evidence_edge_id(CHILD, INTERVENTION)
    assert first != graph_v2.stable_evidence_edge_id(ROOT, INTERVENTION)


def test_edge_detail_query_limits_unique_studies_before_hydration():
    query_request = graph_v2._graph_query(
        condition=[ROOT],
        intervention=[],
        outcome=[],
        region=[],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )
    query = graph_v2.local_detail_query(
        "who-ictrp",
        query_request,
        CHILD,
        INTERVENTION,
        26,
        [ROOT, CHILD],
        [INTERVENTION],
    )

    assert "SELECT DISTINCT ?selectionStudy WHERE" in query
    assert "ORDER BY STR(?selectionStudy)" in query
    assert "SELECT DISTINCT ?study WHERE" in query
    assert "ORDER BY STR(?study)" in query
    assert f"LIMIT {graph_v2.UNIQUE_STUDY_LIMIT}" in query
    assert f"LIMIT {graph_v2.UNIQUE_STUDY_LIMIT + 1}" in query
    assert "LIMIT 27" in query
    assert query.index(f"LIMIT {graph_v2.UNIQUE_STUDY_LIMIT + 1}") < query.rindex("OPTIONAL { ?study <http://purl.org/dc/terms/identifier>")
    assert query.rstrip().endswith("LIMIT 27")


def test_aea_edge_detail_uses_public_registry_identifier(monkeypatch):
    internal_uri = (
        "https://universalevidence.com/ontology/"
        "Evidence_AEA_AEARCTR-0011638"
    )
    public_url = "http://www.socialscienceregistry.org/trials/11638"

    async def select(sparql, _budget):
        if "?rowKind ?role ?rawText" in sparql:
            return [
                {
                    "study": internal_uri,
                    "rowKind": graph_v2._STORED_STATE_RAW_ROW,
                    "role": "condition",
                    "rawText": "direct child state",
                },
                {
                    "study": internal_uri,
                    "rowKind": graph_v2._STORED_INTERVENTION_RAW_ROW,
                    "role": "intervention",
                    "rawText": "direct intervention",
                },
            ]
        if "?study_id ?title ?status ?date ?country" in sparql:
            return [
                {
                    "study": internal_uri,
                    "study_id": public_url,
                    "title": "Decreasing abandonment of calls to the 988 Suicide and Crisis Lifeline",
                }
            ]
        return [{"study": internal_uri}]

    async def direct_maps(_source_id, _roles):
        return (
            {"condition": {"direct child state": (CHILD,)}},
            {"direct intervention": (INTERVENTION,)},
        )

    monkeypatch.setattr(graph_v2, "_sparql_select", select)
    monkeypatch.setattr(graph_v2, "_stored_direct_maps", direct_maps)
    query = graph_v2._graph_query(
        condition=[ROOT],
        intervention=[INTERVENTION],
        outcome=[],
        region=[],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )
    rows, _meta = asyncio.run(
        graph_v2._local_detail_rows(
            "aea",
            query,
            CHILD,
            INTERVENTION,
            25,
            [ROOT, CHILD],
            [INTERVENTION],
        )
    )

    assert rows[0]["study_id"] == public_url
    assert rows[0]["url"] == public_url
    assert "/ontology/Evidence_AEA_" not in rows[0]["url"]


def test_aggregate_and_detail_share_the_retained_capped_specificity_population():
    excluded = "https://universalevidence.com/vocab/states/OutOfCapDescendant"
    query_request = graph_v2._graph_query(
        condition=[ROOT],
        intervention=[INTERVENTION],
        outcome=[],
        region=[],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )
    retained_conditions = [ROOT, CHILD]
    retained_interventions = [INTERVENTION]

    aggregate = graph_v2.local_group_query(
        "aea", query_request, retained_conditions, retained_interventions
    )
    detail = graph_v2.local_detail_query(
        "aea",
        query_request,
        ROOT,
        INTERVENTION,
        26,
        retained_conditions,
        retained_interventions,
    )

    retained_condition_values = f"VALUES ?state {{ <{ROOT}> <{CHILD}> }}"
    retained_intervention_values = f"VALUES ?intervention {{ <{INTERVENTION}> }}"
    assert retained_condition_values in aggregate
    assert retained_intervention_values in aggregate
    assert retained_intervention_values in detail
    assert f"VALUES ?state {{ <{ROOT}> }}" in detail
    assert excluded not in aggregate
    assert excluded not in detail
    assert "skos:broader+" not in aggregate
    assert "skos:broader+" not in detail


def test_detail_source_reconciliation_requires_exact_source_set_and_counts():
    expected = {"aea": 2}

    assert graph_v2._source_counts_reconciled(
        expected, {"aea": 2}, next_cursor=None
    ) is True
    assert graph_v2._source_counts_reconciled(
        expected, {"aea": 2, "ctgov": 1}, next_cursor=None
    ) is False
    assert graph_v2._source_counts_reconciled(
        expected, {"aea": 1}, next_cursor=None
    ) is False
    assert graph_v2._source_counts_reconciled(
        expected, {"aea": 2}, next_cursor="more"
    ) is True
    assert graph_v2._source_counts_reconciled(
        expected, {"aea": 1}, next_cursor="more"
    ) is False


def test_graph_v2_accepts_multiple_condition_and_intervention_terms(monkeypatch):
    graph_v2.graph_v2_cache.clear()
    captured = None

    async def execute(query):
        nonlocal captured
        captured = query
        return {
            "nodes": [],
            "edges": [],
            "metadata": {},
            "meta": {
                "api_version": "query-v2",
                "schemaVersion": "graph-v2",
                "returned_unique_studies": 0,
                "limit_per_source_branch": 100,
                "truncated": False,
                "approximate": False,
                "sources": {},
            },
        }

    monkeypatch.setattr(graph_v2, "execute_graph_v2", execute)
    client = TestClient(api_main.app)
    second_condition = "https://universalevidence.com/vocab/states/Malaria"
    second_intervention = "https://universalevidence.com/vocab/interventions/CashTransfer"
    response = client.get(
        "/graph/v2",
        params=[
            ("condition", ROOT),
            ("condition", second_condition),
            ("intervention", INTERVENTION),
            ("intervention", second_intervention),
            ("condition_logic", "and"),
            ("intervention_logic", "or"),
        ],
    )

    assert response.status_code == 200
    assert captured.values["condition"] == tuple(sorted((ROOT, second_condition)))
    assert captured.values["intervention"] == tuple(
        sorted((INTERVENTION, second_intervention))
    )
    assert captured.logic["condition"] == "and"
    assert captured.logic["intervention"] == "or"


def test_graph_v2_accepts_state_outcome_region_and_intervention_axes(monkeypatch):
    graph_v2.graph_v2_cache.clear()

    async def execute(_query):
        return {"nodes": [], "edges": [], "metadata": {}, "meta": {}}

    monkeypatch.setattr(graph_v2, "execute_graph_v2", execute)
    client = TestClient(api_main.app)

    assert client.get("/graph/v2", params={"intervention": INTERVENTION}).status_code == 200
    assert client.get(
        "/graph/v2",
        params={"outcome": "https://universalevidence.com/vocab/states/BodyWeight"},
    ).status_code == 200
    assert client.get(
        "/graph/v2",
        params={"region": "https://universalevidence.com/vocab/regions/SubSaharanAfrica"},
    ).status_code == 200


def test_graph_v2_route_is_additive_and_cached(monkeypatch):
    graph_v2.graph_v2_cache.clear()
    calls = 0

    async def execute(query):
        nonlocal calls
        calls += 1
        return {
            "nodes": [{"id": ROOT, "label": "Malnutrition", "class": "Condition", "studyCount": 0}],
            "edges": [],
            "metadata": {},
            "meta": {
                "api_version": "query-v2",
                "schemaVersion": "graph-v2",
                "returned_unique_studies": 0,
                "limit_per_source_branch": 100,
                "truncated": False,
                "approximate": False,
                "sources": {},
            },
        }

    monkeypatch.setattr(graph_v2, "execute_graph_v2", execute)
    monkeypatch.setattr(
        graph_v2,
        "graph_versions",
        lambda: ("a" * 64, "b" * 64, None),
    )
    client = TestClient(api_main.app)
    params = [("condition", ROOT)]
    first = client.get("/graph/v2", params=params)
    second = client.get("/graph/v2", params=params)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["meta"]["schemaVersion"] == "graph-v2"
    assert calls == 1
    assert first.json()["meta"]["cache_status"] == "miss"
    assert second.json()["meta"]["cache_status"] == "hit"


def test_graph_v2_cache_coalesces_identical_inflight_calls():
    cache = GraphV2Cache(populated_ttl=60, empty_ttl=60)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def execute():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"nodes": [], "edges": [], "meta": {}}

    async def scenario():
        first = asyncio.create_task(cache.get_or_execute({"condition": ROOT}, execute))
        await started.wait()
        second = asyncio.create_task(cache.get_or_execute({"condition": ROOT}, execute))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    results = asyncio.run(scenario())
    assert results[0] == results[1]
    assert calls == 1
    assert cache.coalesced == 1


def test_graph_v2_bypasses_cache_without_both_loader_versions(monkeypatch):
    graph_v2.graph_v2_cache.clear()
    calls = 0

    async def execute(_query):
        nonlocal calls
        calls += 1
        return {"nodes": [], "edges": [], "metadata": {}, "meta": {"sources": {}}}

    monkeypatch.setattr(graph_v2, "execute_graph_v2", execute)
    monkeypatch.setattr(
        graph_v2,
        "graph_versions",
        lambda: (None, "b" * 64, "taxonomy_version_unavailable"),
    )
    client = TestClient(api_main.app)

    first = client.get("/graph/v2", params={"condition": ROOT})
    second = client.get("/graph/v2", params={"condition": ROOT})

    assert first.status_code == second.status_code == 200
    assert calls == 2
    assert first.json()["meta"]["cache_status"] == "bypass"
    assert graph_v2.graph_v2_cache.stats()["bypasses"] == 2


def test_condition_only_graph_enforces_declared_intervention_cap():
    query = graph_v2._graph_query(
        condition=[ROOT],
        intervention=[],
        outcome=[],
        region=[],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )
    intervention_count = graph_v2.MAX_INTERVENTION_NODES + 3
    rows = [
        {
            "condition": ROOT,
            "conditionLabel": "Malnutrition",
            "intervention": f"https://universalevidence.com/vocab/interventions/Test{i}",
            "interventionLabel": f"Test {i}",
            "studyUris": f"https://example.org/study/{i}",
        }
        for i in range(intervention_count)
    ]
    payload = graph_v2.build_graph_v2_payload(
        query=query,
        condition_taxonomy={
            "nodes": [{"id": ROOT, "label": "Malnutrition", "class": "Condition", "studyCount": 0, "depth": 0, "selected": True}],
            "pairs": [],
            "roots": [ROOT],
            "omitted": 0,
        },
        intervention_taxonomy={"nodes": [], "pairs": [], "roots": [], "omitted": 0},
        local_rows={"aea": rows, "who-ictrp": []},
        live_rows={"ctgov": [], "isrctn": []},
        source_meta={source: _meta(intervention_count if source == "aea" else 0) for source in graph_v2.SOURCE_IDS},
    )

    assert payload["metadata"]["interventionNodes"] == graph_v2.MAX_INTERVENTION_NODES
    assert payload["metadata"]["interventionNodesOmitted"] == 3
    assert payload["metadata"]["totalNodes"] <= graph_v2.MAX_TOTAL_NODES
    assert payload["meta"]["truncated"] is True


def test_node_counts_are_rebuilt_from_the_100_retained_edges():
    query = graph_v2._graph_query(
        condition=[ROOT],
        intervention=[],
        outcome=[],
        region=[],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )
    rows = [
        {
            "state": ROOT,
            "stateLabel": "Malnutrition",
            "intervention": f"https://universalevidence.com/vocab/interventions/Cap{i:03d}",
            "interventionLabel": f"Cap {i:03d}",
            "studyUris": f"https://example.org/study/{i:03d}",
        }
        for i in range(graph_v2.MAX_INTERVENTION_NODES + 3)
    ]
    selected = len(rows)
    payload = graph_v2.build_graph_v2_payload(
        query=query,
        condition_taxonomy={
            "nodes": [
                {
                    "id": ROOT,
                    "label": "Malnutrition",
                    "class": "State",
                    "studyCount": 0,
                    "selected": True,
                }
            ],
            "pairs": [],
            "roots": [ROOT],
            "omitted": 0,
        },
        intervention_taxonomy={"nodes": [], "pairs": [], "roots": [], "omitted": 0},
        local_rows={"aea": rows, "who-ictrp": []},
        live_rows={"ctgov": [], "isrctn": []},
        source_meta={
            source: _meta(selected if source == "aea" else 0)
            for source in graph_v2.SOURCE_IDS
        },
    )

    evidence = [edge for edge in payload["edges"] if edge["kind"] == "evidence"]
    state_node = next(node for node in payload["nodes"] if node["id"] == ROOT)
    assert len(evidence) == graph_v2.MAX_INTERVENTION_NODES
    assert sum(edge["weight"] for edge in evidence) == graph_v2.MAX_INTERVENTION_NODES
    assert state_node["studyCount"] == graph_v2.MAX_INTERVENTION_NODES
    assert payload["meta"]["returned_unique_studies"] == graph_v2.MAX_INTERVENTION_NODES
    assert payload["meta"]["sources"]["aea"]["selected_unique_studies"] == selected
    assert payload["meta"]["sources"]["aea"]["omitted_by_graph_caps_unique_studies"] == 3


def test_node_omissions_include_interventions_only_on_state_capped_edges():
    query = graph_v2._graph_query(
        condition=[],
        intervention=[],
        outcome=[],
        region=["https://universalevidence.com/vocab/regions/World"],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )
    candidate_count = graph_v2.MAX_STATE_NODES + 1
    rows = [
        {
            "state": f"https://universalevidence.com/vocab/states/Cap{i:03d}",
            "stateLabel": f"State cap {i:03d}",
            "intervention": f"https://universalevidence.com/vocab/interventions/StateCap{i:03d}",
            "interventionLabel": f"Intervention cap {i:03d}",
            "studyUris": f"https://example.org/state-cap/{i:03d}",
        }
        for i in range(candidate_count)
    ]
    payload = graph_v2.build_graph_v2_payload(
        query=query,
        condition_taxonomy={"nodes": [], "pairs": [], "roots": [], "omitted": 0},
        intervention_taxonomy={"nodes": [], "pairs": [], "roots": [], "omitted": 0},
        local_rows={"aea": rows, "who-ictrp": []},
        live_rows={"ctgov": [], "isrctn": []},
        source_meta={source: _meta() for source in graph_v2.SOURCE_IDS},
    )

    assert payload["metadata"]["stateNodesOmitted"] == 1
    assert payload["metadata"]["interventionNodesOmitted"] == 1
    assert payload["metadata"]["totalNodesOmitted"] == 2


def test_grouped_row_cap_is_global_and_disclosed():
    local_rows = {
        "aea": [{"weight": "2", "condition": ROOT, "intervention": f"a-{i}"} for i in range(1_500)],
        "who-ictrp": [{"weight": "1", "condition": ROOT, "intervention": f"w-{i}"} for i in range(1_000)],
    }
    source_meta = {source: _meta() for source in graph_v2.SOURCE_IDS}

    retained = graph_v2.cap_grouped_rows(local_rows, source_meta)

    assert sum(len(rows) for rows in retained.values()) == graph_v2.MAX_GROUPED_ROWS
    assert len(retained["aea"]) == 1_500
    assert len(retained["who-ictrp"]) == 500
    assert source_meta["who-ictrp"]["grouped_rows_omitted_lower_bound"] == 500
    assert source_meta["who-ictrp"]["budget_exhausted"] is True


def test_detail_cursor_is_bound_to_edge_and_filters():
    query = graph_v2._graph_query(
        condition=[ROOT],
        intervention=[],
        outcome=[],
        region=[],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )
    edge_id = graph_v2.stable_evidence_edge_id(CHILD, INTERVENTION)
    membership_a = {
        "ctgov": graph_v2._study_membership_digest("ctgov", ["C1", "C2"])
    }
    membership_b = {
        "ctgov": graph_v2._study_membership_digest("ctgov", ["C1", "C3"])
    }
    context = graph_v2._detail_cursor_context(
        query, edge_id, "a" * 64, "b" * 64, membership_a
    )
    changed_context = graph_v2._detail_cursor_context(
        query, edge_id, "a" * 64, "b" * 64, membership_b
    )
    identity_a = graph_v2.detail_cache_identity(
        query,
        edge_id=edge_id,
        condition_uri=CHILD,
        intervention_uri=INTERVENTION,
        expected_source_counts={"ctgov": 2},
        offset=0,
        limit=25,
        taxonomy_version="a" * 64,
        dataset_version="b" * 64,
        expected_source_membership_digests=membership_a,
    )
    identity_b = graph_v2.detail_cache_identity(
        query,
        edge_id=edge_id,
        condition_uri=CHILD,
        intervention_uri=INTERVENTION,
        expected_source_counts={"ctgov": 2},
        offset=0,
        limit=25,
        taxonomy_version="a" * 64,
        dataset_version="b" * 64,
        expected_source_membership_digests=membership_b,
    )
    cursor = graph_v2._encode_cursor(25, context)

    assert graph_v2._decode_cursor(cursor, context) == 25
    assert changed_context != context
    assert graph_v2.GraphV2Cache.canonical_key(identity_a) != (
        graph_v2.GraphV2Cache.canonical_key(identity_b)
    )
    with pytest.raises(graph_v2.HTTPException) as mismatch:
        graph_v2._decode_cursor(cursor, changed_context)
    assert mismatch.value.status_code == 422


def test_detail_route_rejects_hash_valid_pair_missing_from_aggregate(monkeypatch):
    graph_v2.graph_v2_cache.clear()
    monkeypatch.setattr(
        graph_v2,
        "graph_versions",
        lambda: ("a" * 64, "b" * 64, None),
    )

    async def aggregate(_query):
        return {"nodes": [], "edges": [], "metadata": {}, "meta": {"sources": {}}}

    monkeypatch.setattr(graph_v2, "execute_graph_v2", aggregate)
    edge_id = graph_v2.stable_evidence_edge_id(CHILD, INTERVENTION)
    response = TestClient(api_main.app).get(
        f"/graph/v2/edges/{edge_id}/studies",
        params={
            "edge_condition": CHILD,
            "edge_intervention": INTERVENTION,
            "condition": ROOT,
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "stale or invalid graph evidence edge"


def test_graph_to_lazy_detail_interaction_uses_aggregate_source_counts(monkeypatch):
    graph_v2.graph_v2_cache.clear()
    graph_v2.graph_v2_detail_cache.clear()
    edge_id = graph_v2.stable_evidence_edge_id(CHILD, INTERVENTION)
    membership_digests = {
        "aea": graph_v2._study_membership_digest("aea", ["aea-1", "aea-2"])
    }
    monkeypatch.setattr(
        graph_v2,
        "graph_versions",
        lambda: ("a" * 64, "b" * 64, None),
    )

    async def aggregate(_query):
        return {
            "nodes": [
                {"id": CHILD, "label": "Child malnutrition", "class": "Condition", "studyCount": 2},
                {"id": INTERVENTION, "label": "Nutrition intervention", "class": "Intervention", "studyCount": 2},
            ],
            "edges": [
                {
                    "id": edge_id,
                    "kind": "evidence",
                    "source": INTERVENTION,
                    "target": CHILD,
                    "weight": 2,
                    "source_counts": {"aea": 2},
                    graph_v2._EDGE_MEMBERSHIP_DIGESTS: membership_digests,
                }
            ],
            "metadata": {},
            "meta": {"sources": {}},
        }

    captured = {}

    async def details(
        _query,
        condition_uri,
        intervention_uri,
        offset,
        limit,
        *,
        expected_source_counts,
        expected_source_membership_digests,
        cursor_context,
    ):
        captured["calls"] = captured.get("calls", 0) + 1
        captured.update(
            {
                "condition": condition_uri,
                "intervention": intervention_uri,
                "offset": offset,
                "limit": limit,
                "source_counts": expected_source_counts,
                "source_membership_digests": (
                    expected_source_membership_digests
                ),
                "cursor_context": cursor_context,
            }
        )
        return {
            "results": [{"source": "AEA", "study_id": "A1"}],
            "meta": {"nextCursor": None, "source_counts_reconciled": True},
        }

    monkeypatch.setattr(graph_v2, "execute_graph_v2", aggregate)
    monkeypatch.setattr(graph_v2, "execute_edge_details", details)
    client = TestClient(api_main.app)
    graph_response = client.get("/graph/v2", params={"condition": ROOT})
    edge = graph_response.json()["edges"][0]
    detail_response = client.get(
        f"/graph/v2/edges/{edge['id']}/studies",
        params={
            "edge_condition": edge["target"],
            "edge_intervention": edge["source"],
            "condition": ROOT,
            "limit": 50,
        },
    )
    cached_detail_response = client.get(
        f"/graph/v2/edges/{edge['id']}/studies",
        params={
            "edge_condition": edge["target"],
            "edge_intervention": edge["source"],
            "condition": ROOT,
            "limit": 50,
        },
    )

    assert (
        graph_response.status_code
        == detail_response.status_code
        == cached_detail_response.status_code
        == 200
    )
    assert detail_response.json()["results"][0]["study_id"] == "A1"
    assert captured["source_counts"] == {"aea": 2}
    assert captured["source_membership_digests"] == membership_digests
    assert captured["limit"] == 50
    assert captured["calls"] == 1
    assert detail_response.json()["meta"]["detailCacheStatus"] == "miss"
    assert cached_detail_response.json()["meta"]["detailCacheStatus"] == "hit"
    assert client.get(
        f"/graph/v2/edges/{edge['id']}/studies",
        params={
            "edge_condition": edge["target"],
            "edge_intervention": edge["source"],
            "condition": ROOT,
            "limit": 51,
        },
    ).status_code == 422


def test_edge_detail_token_survives_aggregate_cache_eviction(monkeypatch):
    graph_v2.graph_v2_cache.clear()
    graph_v2.graph_v2_detail_cache.clear()
    monkeypatch.setenv("GRAPH_EDGE_TOKEN_SECRET", "test-edge-token-secret")
    edge_id = graph_v2.stable_evidence_edge_id(CHILD, INTERVENTION)
    membership_digests = {
        "who-ictrp": graph_v2._study_membership_digest(
            "who-ictrp", ["W1", "W2"]
        )
    }
    monkeypatch.setattr(
        graph_v2,
        "graph_versions",
        lambda: ("a" * 64, "b" * 64, None),
    )
    aggregate_calls = 0

    async def aggregate(_query):
        nonlocal aggregate_calls
        aggregate_calls += 1
        return {
            "nodes": [],
            "edges": [
                {
                    "id": edge_id,
                    "kind": "evidence",
                    "source": INTERVENTION,
                    "target": CHILD,
                    "weight": 2,
                    "source_counts": {"who-ictrp": 2},
                    graph_v2._EDGE_MEMBERSHIP_DIGESTS: membership_digests,
                }
            ],
            "metadata": {},
            "meta": {"sources": {}},
        }

    captured = {}

    async def details(
        _query,
        _condition_uri,
        _intervention_uri,
        _offset,
        _limit,
        *,
        expected_source_counts,
        expected_source_membership_digests,
        cursor_context,
    ):
        captured["source_counts"] = expected_source_counts
        captured["source_membership_digests"] = (
            expected_source_membership_digests
        )
        captured["cursor_context"] = cursor_context
        return {
            "results": [{"source": "WHO ICTRP", "study_id": "W1"}],
            "meta": {"nextCursor": None, "source_counts_reconciled": True},
        }

    monkeypatch.setattr(graph_v2, "execute_graph_v2", aggregate)
    monkeypatch.setattr(graph_v2, "execute_edge_details", details)
    client = TestClient(api_main.app)
    graph_response = client.get("/graph/v2", params={"condition": ROOT})
    edge = graph_response.json()["edges"][0]
    graph_v2.graph_v2_cache.clear()

    async def unexpected_aggregate(_query):
        raise AssertionError("edge token must avoid rebuilding the aggregate graph")

    monkeypatch.setattr(graph_v2, "execute_graph_v2", unexpected_aggregate)
    detail_response = client.get(
        f"/graph/v2/edges/{edge_id}/studies",
        params={
            "edge_condition": CHILD,
            "edge_intervention": INTERVENTION,
            "condition": ROOT,
            "edge_token": edge["detailToken"],
        },
    )

    assert graph_response.status_code == detail_response.status_code == 200
    assert aggregate_calls == 1
    assert captured["source_counts"] == {"who-ictrp": 2}
    assert captured["source_membership_digests"] == membership_digests
    assert graph_v2._EDGE_MEMBERSHIP_DIGESTS not in edge
    assert detail_response.json()["meta"]["aggregateCacheStatus"] == "edge-token"


def test_direct_live_edge_token_and_paginated_details_reconcile(monkeypatch):
    graph_v2.graph_v2_cache.clear()
    graph_v2.graph_v2_detail_cache.clear()
    monkeypatch.setenv("GRAPH_EDGE_TOKEN_SECRET", "test-direct-state-secret")
    taxonomy_version = "a" * 64
    dataset_version = "b" * 64
    aggregate = _direct_state_aggregate_payload()
    live_rows = _direct_state_live_rows()
    query = _query(state=[ROOT], intervention=[INTERVENTION])
    expected_edge = next(
        edge
        for edge in aggregate["edges"]
        if edge["kind"] == "evidence"
        and edge["target"] == CHILD
        and edge["source"] == INTERVENTION
    )

    monkeypatch.setattr(
        graph_v2,
        "graph_versions",
        lambda: (taxonomy_version, dataset_version, None),
    )
    monkeypatch.setattr(
        graph_v2, "state_forest", lambda _uris: _direct_state_state_taxonomy()
    )
    monkeypatch.setattr(
        graph_v2,
        "intervention_forest",
        lambda _uris: _direct_state_intervention_taxonomy(),
    )

    async def regions(_query):
        return graph_v2.RegionIndex(graph_v2.Graph())

    async def graph_payload(_query):
        return aggregate

    async def source_rows(_query, adapter, _regions):
        rows = live_rows[adapter]
        return rows, _meta(len({row["study_id"] for row in rows}))

    monkeypatch.setattr(graph_v2, "region_index_for_query", regions)
    monkeypatch.setattr(graph_v2, "execute_graph_v2", graph_payload)
    monkeypatch.setattr(graph_v2, "execute_graph_source", source_rows)
    monkeypatch.setattr(
        graph_v2,
        "default_adapters",
        lambda: {"ctgov": "ctgov", "isrctn": "isrctn"},
    )

    client = TestClient(api_main.app)
    query_params = {"state": ROOT, "intervention": INTERVENTION}
    graph_response = client.get("/graph/v2", params=query_params)
    edge = next(
        item
        for item in graph_response.json()["edges"]
        if item["kind"] == "evidence"
        and item["target"] == CHILD
        and item["source"] == INTERVENTION
    )
    token_manifest = graph_v2._decode_edge_detail_token(
        edge["detailToken"],
        query=query,
        edge_id=edge["id"],
        condition_uri=CHILD,
        intervention_uri=INTERVENTION,
        taxonomy_version=taxonomy_version,
        dataset_version=dataset_version,
    )

    assert graph_response.status_code == 200
    assert edge["weight"] == expected_edge["weight"] == 7
    assert edge["source_counts"] == expected_edge["source_counts"]
    assert token_manifest == (
        expected_edge["source_counts"],
        expected_edge[graph_v2._EDGE_MEMBERSHIP_DIGESTS],
    )

    detail_params = {
        **query_params,
        "edge_state": CHILD,
        "edge_intervention": INTERVENTION,
        "edge_token": edge["detailToken"],
        "limit": 4,
    }
    first_response = client.get(
        f"/graph/v2/edges/{edge['id']}/studies",
        params=detail_params,
    )
    first = first_response.json()
    second_response = client.get(
        f"/graph/v2/edges/{edge['id']}/studies",
        params={**detail_params, "cursor": first["meta"]["nextCursor"]},
    )
    second = second_response.json()

    assert first_response.status_code == second_response.status_code == 200
    assert first["meta"]["returned"] == 4
    assert second["meta"]["returned"] == 3
    assert first["meta"]["nextCursor"] is not None
    assert second["meta"]["nextCursor"] is None
    assert {
        (row["source"], row["study_id"])
        for row in first["results"] + second["results"]
    } == {
        ("CT.gov", "C-CONDITION"),
        ("CT.gov", "C-OUTCOME"),
        ("CT.gov", "C-DUPLICATE-ROLES"),
        ("CT.gov", "SHARED-ID"),
        ("CT.gov", "C-MULTI-INTERVENTION"),
        ("ISRCTN", "I-OUTCOME"),
        ("ISRCTN", "SHARED-ID"),
    }
    for page in (first, second):
        assert page["meta"]["source_counts"] == {"ctgov": 5, "isrctn": 2}
        assert page["meta"]["detail_population_source_counts"] == {
            "ctgov": 5,
            "isrctn": 2,
        }
        assert page["meta"]["source_counts_reconciled"] is True
        assert page["meta"]["source_membership_reconciled"] is True
        assert page["meta"]["sound"] is True


@pytest.mark.parametrize(
    "row",
    [
        {
            "source": "CT.gov",
            "study_id": "C-FALLBACK-ONLY",
            "intervention_concept_uri": INTERVENTION,
            "outcomes": [],
        },
        {
            "source": "CT.gov",
            "study_id": "C-FALLBACK-ONLY",
            "state_concept_uri": MALARIA,
            "condition_concept_uri": MALARIA,
            "intervention_concept_uri": INTERVENTION,
            "outcomes": [],
        },
    ],
    ids=["missing-state", "outside-subtree"],
)
def test_stale_fallback_only_edge_token_never_reveals_live_studies(
    monkeypatch,
    row,
):
    graph_v2.graph_v2_cache.clear()
    graph_v2.graph_v2_detail_cache.clear()
    monkeypatch.setenv("GRAPH_EDGE_TOKEN_SECRET", "test-stale-edge-secret")
    taxonomy_version = "a" * 64
    dataset_version = "b" * 64
    query = _query(state=[ROOT], intervention=[INTERVENTION])
    edge_id = graph_v2.stable_evidence_edge_id(ROOT, INTERVENTION)
    stale_edge = {
        "id": edge_id,
        "kind": "evidence",
        "source": INTERVENTION,
        "target": ROOT,
        "weight": 1,
        "source_counts": {"ctgov": 1},
        graph_v2._EDGE_MEMBERSHIP_DIGESTS: {
            "ctgov": graph_v2._study_membership_digest(
                "ctgov", ["C-FALLBACK-ONLY"]
            )
        },
    }
    token = graph_v2._encode_edge_detail_token(
        query,
        stale_edge,
        taxonomy_version,
        dataset_version,
    )

    monkeypatch.setattr(
        graph_v2,
        "graph_versions",
        lambda: (taxonomy_version, dataset_version, None),
    )
    monkeypatch.setattr(
        graph_v2, "state_forest", lambda _uris: _direct_state_state_taxonomy()
    )
    monkeypatch.setattr(
        graph_v2,
        "intervention_forest",
        lambda _uris: _direct_state_intervention_taxonomy(),
    )

    async def regions(_query):
        return graph_v2.RegionIndex(graph_v2.Graph())

    async def source_rows(_query, adapter, _regions):
        assert adapter == "ctgov"
        return [row], _meta(1)

    monkeypatch.setattr(graph_v2, "region_index_for_query", regions)
    monkeypatch.setattr(graph_v2, "execute_graph_source", source_rows)
    monkeypatch.setattr(
        graph_v2,
        "default_adapters",
        lambda: {"ctgov": "ctgov"},
    )

    response = TestClient(api_main.app).get(
        f"/graph/v2/edges/{edge_id}/studies",
        params={
            "state": ROOT,
            "intervention": INTERVENTION,
            "edge_state": ROOT,
            "edge_intervention": INTERVENTION,
            "edge_token": token,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["results"] == []
    assert payload["meta"]["detail_population_source_counts"] == {"ctgov": 0}
    assert payload["meta"]["source_counts_reconciled"] is False
    assert payload["meta"]["source_membership_reconciled"] is False
    assert payload["meta"]["sound"] is False


def test_edge_detail_rejects_same_count_membership_drift_between_pages(
    monkeypatch,
):
    graph_v2.graph_v2_cache.clear()
    graph_v2.graph_v2_detail_cache.clear()
    monkeypatch.setenv("GRAPH_EDGE_TOKEN_SECRET", "test-edge-token-secret")
    monkeypatch.setattr(
        graph_v2,
        "graph_versions",
        lambda: ("a" * 64, "b" * 64, None),
    )
    monkeypatch.setattr(
        graph_v2,
        "state_forest",
        lambda _uris: {
            "nodes": [{"id": ROOT}],
            "pairs": [],
            "roots": [ROOT],
            "omitted": 0,
        },
    )
    monkeypatch.setattr(
        graph_v2,
        "intervention_forest",
        lambda _uris: {
            "nodes": [{"id": INTERVENTION}],
            "pairs": [],
            "roots": [INTERVENTION],
            "omitted": 0,
        },
    )

    first_membership = [f"C{index:03d}" for index in range(30)]
    changed_membership = ["A000", *first_membership[:-1]]
    membership_digests = {
        "ctgov": graph_v2._study_membership_digest(
            "ctgov", first_membership
        )
    }
    edge_id = graph_v2.stable_evidence_edge_id(ROOT, INTERVENTION)

    async def aggregate(_query):
        return {
            "nodes": [],
            "edges": [
                {
                    "id": edge_id,
                    "kind": "evidence",
                    "source": INTERVENTION,
                    "target": ROOT,
                    "weight": 30,
                    "source_counts": {"ctgov": 30},
                    graph_v2._EDGE_MEMBERSHIP_DIGESTS: membership_digests,
                }
            ],
            "metadata": {},
            "meta": {"sources": {}},
        }

    async def regions(_query):
        return graph_v2.RegionIndex(graph_v2.Graph())

    detail_calls = 0

    async def live_rows(_query, adapter, _regions):
        nonlocal detail_calls
        assert adapter == "ctgov"
        population = (
            first_membership if detail_calls == 0 else changed_membership
        )
        detail_calls += 1
        return (
            [
                {
                    "source": "CT.gov",
                    "study_id": study_id,
                    "title": f"Study {study_id}",
                }
                for study_id in population
            ],
            _meta(30),
        )

    monkeypatch.setattr(graph_v2, "execute_graph_v2", aggregate)
    monkeypatch.setattr(graph_v2, "region_index_for_query", regions)
    monkeypatch.setattr(graph_v2, "execute_graph_source", live_rows)
    monkeypatch.setattr(
        graph_v2,
        "_live_edge_coordinates",
        lambda *_args, **_kwargs: {(ROOT, INTERVENTION)},
    )
    monkeypatch.setattr(
        graph_v2,
        "default_adapters",
        lambda: {"ctgov": "ctgov"},
    )

    client = TestClient(api_main.app)
    query_params = {"state": ROOT, "intervention": INTERVENTION}
    graph_response = client.get("/graph/v2", params=query_params)
    edge = graph_response.json()["edges"][0]
    token = edge["detailToken"]
    encoded = token.split(".", 1)[0]
    padded = encoded + "=" * (-len(encoded) % 4)
    token_payload = json.loads(
        graph_v2.base64.urlsafe_b64decode(padded.encode()).decode()
    )

    assert graph_response.status_code == 200
    assert graph_v2._EDGE_MEMBERSHIP_DIGESTS not in edge
    assert token_payload["v"] == 2
    assert token_payload["sourceMembershipDigests"] == membership_digests
    assert "studies" not in token_payload

    legacy_payload = dict(token_payload)
    legacy_payload["v"] = 1
    legacy_payload.pop("sourceMembershipDigests")
    legacy_encoded = graph_v2.base64.urlsafe_b64encode(
        json.dumps(
            legacy_payload, sort_keys=True, separators=(",", ":")
        ).encode()
    ).decode().rstrip("=")
    legacy_signature = graph_v2.hmac.new(
        graph_v2._edge_detail_token_key("a" * 64, "b" * 64),
        legacy_encoded.encode(),
        graph_v2.hashlib.sha256,
    ).hexdigest()
    legacy_response = client.get(
        f"/graph/v2/edges/{edge_id}/studies",
        params={
            **query_params,
            "edge_state": ROOT,
            "edge_intervention": INTERVENTION,
            "edge_token": f"{legacy_encoded}.{legacy_signature}",
        },
    )
    assert legacy_response.status_code == 422
    assert legacy_response.json()["detail"] == "invalid graph edge detail token"

    detail_params = {
        **query_params,
        "edge_state": ROOT,
        "edge_intervention": INTERVENTION,
        "edge_token": token,
        "limit": 25,
    }
    first_page = client.get(
        f"/graph/v2/edges/{edge_id}/studies",
        params=detail_params,
    )

    assert first_page.status_code == 200
    first_payload = first_page.json()
    assert [row["study_id"] for row in first_payload["results"]] == (
        first_membership[:25]
    )
    assert first_payload["meta"]["source_membership_reconciled"] is True
    assert first_payload["meta"]["sound"] is True
    assert graph_v2.GraphV2Cache.cacheable(first_payload) is True
    assert first_payload["meta"]["nextCursor"] is not None

    # Without membership validation the changed population's offset 25 page
    # would repeat C024 and omit the former C029 while retaining the same count.
    assert sorted(changed_membership)[25:] == [
        "C024",
        "C025",
        "C026",
        "C027",
        "C028",
    ]
    second_page = client.get(
        f"/graph/v2/edges/{edge_id}/studies",
        params={
            **detail_params,
            "cursor": first_payload["meta"]["nextCursor"],
        },
    )

    assert detail_calls == 2
    assert second_page.status_code == 409
    assert second_page.json()["detail"] == (
        "graph edge evidence changed; refresh the graph"
    )


def test_edge_detail_token_rejects_changed_query_context(monkeypatch):
    graph_v2.graph_v2_cache.clear()
    monkeypatch.setenv("GRAPH_EDGE_TOKEN_SECRET", "test-edge-token-secret")
    monkeypatch.setattr(
        graph_v2,
        "graph_versions",
        lambda: ("a" * 64, "b" * 64, None),
    )
    query = graph_v2._graph_query(
        condition=[ROOT],
        intervention=[],
        outcome=[],
        region=[],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )
    edge_id = graph_v2.stable_evidence_edge_id(CHILD, INTERVENTION)
    token = graph_v2._encode_edge_detail_token(
        query,
        {
            "id": edge_id,
            "kind": "evidence",
            "source": INTERVENTION,
            "target": CHILD,
            "source_counts": {"aea": 1},
            graph_v2._EDGE_MEMBERSHIP_DIGESTS: {
                "aea": graph_v2._study_membership_digest("aea", ["aea-1"])
            },
        },
        "a" * 64,
        "b" * 64,
    )

    response = TestClient(api_main.app).get(
        f"/graph/v2/edges/{edge_id}/studies",
        params={
            "edge_condition": CHILD,
            "edge_intervention": INTERVENTION,
            "condition": CHILD,
            "edge_token": token,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid graph edge detail token"

    tampered = f"{token[:-1]}{'0' if token[-1] != '0' else '1'}"
    tampered_response = TestClient(api_main.app).get(
        f"/graph/v2/edges/{edge_id}/studies",
        params={
            "edge_condition": CHILD,
            "edge_intervention": INTERVENTION,
            "condition": ROOT,
            "edge_token": tampered,
        },
    )
    assert tampered_response.status_code == 422
    assert tampered_response.json()["detail"] == "invalid graph edge detail token"


def test_graph_omits_edge_detail_tokens_without_versioned_secret(monkeypatch):
    graph_v2.graph_v2_cache.clear()
    edge_id = graph_v2.stable_evidence_edge_id(CHILD, INTERVENTION)
    monkeypatch.setenv("GRAPH_EDGE_TOKEN_SECRET", "test-edge-token-secret")
    monkeypatch.setattr(
        graph_v2,
        "graph_versions",
        lambda: (None, None, "missing_graph_versions"),
    )

    async def aggregate(_query):
        return {
            "nodes": [],
            "edges": [
                {
                    "id": edge_id,
                    "kind": "evidence",
                    "source": INTERVENTION,
                    "target": CHILD,
                    "weight": 1,
                    "source_counts": {"aea": 1},
                }
            ],
            "metadata": {},
            "meta": {"sources": {}},
        }

    monkeypatch.setattr(graph_v2, "execute_graph_v2", aggregate)
    response = TestClient(api_main.app).get("/graph/v2", params={"condition": ROOT})

    assert response.status_code == 200
    assert "detailToken" not in response.json()["edges"][0]


def test_edge_details_query_only_sources_that_contributed(monkeypatch):
    query = graph_v2._graph_query(
        condition=[ROOT],
        intervention=[INTERVENTION],
        outcome=[],
        region=[],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )
    monkeypatch.setattr(
        graph_v2,
        "condition_forest",
        lambda _uris: {"nodes": [{"id": ROOT}], "pairs": [], "roots": [ROOT]},
    )
    monkeypatch.setattr(
        graph_v2,
        "intervention_forest",
        lambda _uris: {
            "nodes": [{"id": INTERVENTION}],
            "pairs": [],
            "roots": [INTERVENTION],
        },
    )

    async def local_rows(source_id, *_args, **_kwargs):
        assert source_id == "who-ictrp"
        return (
            [{"source": "WHO ICTRP", "study_id": "W1", "title": "Study"}],
            _meta(1),
        )

    async def unexpected_regions(_query):
        raise AssertionError("WHO-only details must not build a live region index")

    def unexpected_adapters():
        raise AssertionError("WHO-only details must not initialize live adapters")

    monkeypatch.setattr(graph_v2, "_local_detail_rows", local_rows)
    monkeypatch.setattr(graph_v2, "region_index_for_query", unexpected_regions)
    monkeypatch.setattr(graph_v2, "default_adapters", unexpected_adapters)

    payload = asyncio.run(
        graph_v2.execute_edge_details(
            query,
            ROOT,
            INTERVENTION,
            0,
            25,
            expected_source_counts={"who-ictrp": 1},
            expected_source_membership_digests={
                "who-ictrp": graph_v2._study_membership_digest(
                    "who-ictrp", ["W1"]
                )
            },
            cursor_context="context",
        )
    )

    assert [row["study_id"] for row in payload["results"]] == ["W1"]
    assert set(payload["meta"]["sources"]) == {"who-ictrp"}
    assert payload["meta"]["source_counts_reconciled"] is True
    assert payload["meta"]["source_membership_reconciled"] is True
    assert payload["meta"]["sound"] is True


def test_edge_detail_deadline_reaps_pending_sources_and_is_not_cacheable(
    monkeypatch,
):
    pending_tasks: dict[str, asyncio.Task] = {}
    reaped: set[str] = set()

    monkeypatch.setattr(graph_v2, "GRAPH_DEADLINE_SECONDS", 0.01)
    monkeypatch.setattr(
        graph_v2,
        "state_forest",
        lambda _uris: {
            "nodes": [{"id": ROOT}],
            "pairs": [],
            "roots": [ROOT],
            "omitted": 0,
        },
    )
    monkeypatch.setattr(
        graph_v2,
        "intervention_forest",
        lambda _uris: {
            "nodes": [{"id": INTERVENTION}],
            "pairs": [],
            "roots": [INTERVENTION],
            "omitted": 0,
        },
    )

    async def regions(_query):
        return graph_v2.RegionIndex(graph_v2.Graph())

    async def wait_for_deadline(source_id):
        task = asyncio.current_task()
        assert task is not None
        pending_tasks[source_id] = task
        try:
            await asyncio.Event().wait()
        finally:
            reaped.add(source_id)

    async def local_rows(source_id, *_args, **_kwargs):
        if source_id == "aea":
            await wait_for_deadline(source_id)
        assert source_id == "who-ictrp"
        return (
            [
                {
                    "source": "WHO ICTRP",
                    "study_id": f"W{index:03d}",
                    "title": f"Study {index}",
                }
                for index in range(30)
            ],
            _meta(30),
        )

    async def live_rows(_query, adapter, _regions):
        assert adapter == "ctgov"
        await wait_for_deadline(adapter)

    monkeypatch.setattr(graph_v2, "region_index_for_query", regions)
    monkeypatch.setattr(graph_v2, "_local_detail_rows", local_rows)
    monkeypatch.setattr(graph_v2, "execute_graph_source", live_rows)
    monkeypatch.setattr(
        graph_v2,
        "default_adapters",
        lambda: {"ctgov": "ctgov"},
    )
    query = graph_v2._graph_query(
        state=[ROOT],
        condition=[],
        intervention=[INTERVENTION],
        outcome=[],
        region=[],
        state_logic="or",
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )

    async def scenario():
        payload = await graph_v2.execute_edge_details(
            query,
            ROOT,
            INTERVENTION,
            0,
            25,
            expected_source_counts={
                "aea": 1,
                "ctgov": 1,
                "who-ictrp": 30,
            },
            expected_source_membership_digests={
                "aea": graph_v2._study_membership_digest(
                    "aea", ["expected-aea"]
                ),
                "ctgov": graph_v2._study_membership_digest(
                    "ctgov", ["expected-ctgov"]
                ),
                "who-ictrp": graph_v2._study_membership_digest(
                    "who-ictrp", [f"W{index:03d}" for index in range(30)]
                ),
            },
            cursor_context="detail-deadline",
        )
        assert all(task.done() for task in pending_tasks.values())
        return payload

    payload = asyncio.run(scenario())

    assert set(pending_tasks) == {"aea", "ctgov"}
    assert reaped == {"aea", "ctgov"}
    assert all(task.cancelled() for task in pending_tasks.values())
    for source_id in ("aea", "ctgov"):
        source = payload["meta"]["sources"][source_id]
        assert source["status"] == "included"
        assert source["reason"] == "budget_exhausted"
        assert source["budget_exhausted"] is True
    assert payload["meta"]["nextCursor"] is not None
    assert payload["meta"]["source_counts_reconciled"] is False
    assert payload["meta"]["source_membership_reconciled"] is False
    assert payload["meta"]["truncated"] is True
    assert payload["meta"]["approximate"] is True
    assert payload["meta"]["sound"] is False
    assert graph_v2.GraphV2Cache.cacheable(payload) is False


def test_edge_detail_cursor_does_not_mask_population_count_mismatch(
    monkeypatch,
):
    monkeypatch.setattr(
        graph_v2,
        "state_forest",
        lambda _uris: {
            "nodes": [{"id": ROOT}],
            "pairs": [],
            "roots": [ROOT],
            "omitted": 0,
        },
    )
    monkeypatch.setattr(
        graph_v2,
        "intervention_forest",
        lambda _uris: {
            "nodes": [{"id": INTERVENTION}],
            "pairs": [],
            "roots": [INTERVENTION],
            "omitted": 0,
        },
    )

    async def regions(_query):
        return graph_v2.RegionIndex(graph_v2.Graph())

    async def local_rows(source_id, *_args, **_kwargs):
        assert source_id == "aea"
        return (
            [
                {
                    "source": "AEA",
                    "study_id": f"A{index:03d}",
                    "title": f"AEA study {index}",
                }
                for index in range(20)
            ],
            _meta(20),
        )

    async def live_rows(_query, adapter, _regions):
        assert adapter == "ctgov"
        return (
            [
                {
                    "source": "ClinicalTrials.gov",
                    "study_id": f"C{index:03d}",
                    "title": f"CT.gov study {index}",
                }
                for index in range(20)
            ],
            _meta(20),
        )

    monkeypatch.setattr(graph_v2, "region_index_for_query", regions)
    monkeypatch.setattr(graph_v2, "_local_detail_rows", local_rows)
    monkeypatch.setattr(graph_v2, "execute_graph_source", live_rows)
    monkeypatch.setattr(
        graph_v2,
        "_live_edge_coordinates",
        lambda *_args, **_kwargs: {(ROOT, INTERVENTION)},
    )
    monkeypatch.setattr(
        graph_v2,
        "default_adapters",
        lambda: {"ctgov": "ctgov"},
    )
    query = graph_v2._graph_query(
        state=[ROOT],
        condition=[],
        intervention=[INTERVENTION],
        outcome=[],
        region=[],
        state_logic="or",
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )

    payload = asyncio.run(
        graph_v2.execute_edge_details(
            query,
            ROOT,
            INTERVENTION,
            0,
            25,
            expected_source_counts={"aea": 30, "ctgov": 20},
            expected_source_membership_digests={
                "aea": graph_v2._study_membership_digest(
                    "aea", [f"A{index:03d}" for index in range(30)]
                ),
                "ctgov": graph_v2._study_membership_digest(
                    "ctgov", [f"C{index:03d}" for index in range(20)]
                ),
            },
            cursor_context="count-mismatch",
        )
    )

    assert len(payload["results"]) == 25
    assert payload["meta"]["nextCursor"] is not None
    assert payload["meta"]["detail_population_source_counts"] == {
        "aea": 20,
        "ctgov": 20,
    }
    assert payload["meta"]["source_counts_reconciled"] is False
    assert payload["meta"]["source_membership_reconciled"] is False
    assert payload["meta"]["truncated"] is True
    assert payload["meta"]["approximate"] is True
    assert payload["meta"]["sound"] is False
    assert graph_v2.GraphV2Cache.cacheable(payload) is False


def test_edge_details_fail_closed_for_unknown_contributing_source():
    query = graph_v2._graph_query(
        condition=[ROOT],
        intervention=[INTERVENTION],
        outcome=[],
        region=[],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )

    with pytest.raises(graph_v2.HTTPException) as invalid:
        asyncio.run(
            graph_v2.execute_edge_details(
                query,
                ROOT,
                INTERVENTION,
                0,
                25,
                expected_source_counts={"mystery-registry": 1},
                expected_source_membership_digests={},
                cursor_context="context",
            )
        )

    assert invalid.value.status_code == 500
    assert invalid.value.detail == "graph evidence edge has invalid source counts"


def test_edge_details_suppress_cursor_beyond_pagination_ceiling(monkeypatch):
    query = graph_v2._graph_query(
        condition=[ROOT],
        intervention=[INTERVENTION],
        outcome=[],
        region=[],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )
    monkeypatch.setattr(
        graph_v2,
        "condition_forest",
        lambda _uris: {"nodes": [{"id": ROOT}], "pairs": [], "roots": [ROOT]},
    )
    monkeypatch.setattr(
        graph_v2,
        "intervention_forest",
        lambda _uris: {
            "nodes": [{"id": INTERVENTION}],
            "pairs": [],
            "roots": [INTERVENTION],
        },
    )

    async def local_rows(source_id, *_args, **_kwargs):
        assert source_id == "who-ictrp"
        return (
            [
                {
                    "source": "WHO ICTRP",
                    "study_id": f"W{index:04d}",
                    "title": f"Study {index}",
                }
                for index in range(
                    graph_v2.DETAIL_MAX_OFFSET + graph_v2.DETAIL_MAX_LIMIT + 1
                )
            ],
            _meta(
                graph_v2.DETAIL_MAX_OFFSET + graph_v2.DETAIL_MAX_LIMIT + 1
            ),
        )

    monkeypatch.setattr(graph_v2, "_local_detail_rows", local_rows)

    payload = asyncio.run(
        graph_v2.execute_edge_details(
            query,
            ROOT,
            INTERVENTION,
            graph_v2.DETAIL_MAX_OFFSET,
            graph_v2.DETAIL_MAX_LIMIT,
            expected_source_counts={
                "who-ictrp": (
                    graph_v2.DETAIL_MAX_OFFSET
                    + graph_v2.DETAIL_MAX_LIMIT
                    + 1
                )
            },
            expected_source_membership_digests={
                "who-ictrp": graph_v2._study_membership_digest(
                    "who-ictrp",
                    [
                        f"W{index:04d}"
                        for index in range(
                            graph_v2.DETAIL_MAX_OFFSET
                            + graph_v2.DETAIL_MAX_LIMIT
                            + 1
                        )
                    ],
                )
            },
            cursor_context="context",
        )
    )

    assert len(payload["results"]) == graph_v2.DETAIL_MAX_LIMIT
    assert payload["meta"]["nextCursor"] is None
    assert payload["meta"]["truncated"] is True


def test_graph_cache_retries_transient_source_failures():
    cache = GraphV2Cache(random_uniform=lambda _low, _high: 0.0)
    calls = 0

    async def execute():
        nonlocal calls
        calls += 1
        return {
            "nodes": [],
            "edges": [],
            "meta": {
                "sources": {
                    "ctgov": {"status": "error", "reason": "upstream_error"}
                }
            },
        }

    asyncio.run(cache.get_or_execute({"condition": ROOT}, execute))
    asyncio.run(cache.get_or_execute({"condition": ROOT}, execute))

    assert calls == 2
    assert cache.stats()["uncacheable_responses"] == 2


def test_graph_cache_uses_short_ttl_for_sound_budget_limited_partials():
    now = [0.0]
    cache = GraphV2Cache(
        populated_ttl=3_600,
        empty_ttl=600,
        partial_ttl=60,
        clock=lambda: now[0],
        # Positive jitter must never extend a partial beyond the 60-second cap.
        random_uniform=lambda _low, high: high,
    )
    calls = 0

    async def execute():
        nonlocal calls
        calls += 1
        return {
            "nodes": [{"id": ROOT}],
            "edges": [{"id": "edge"}],
            "meta": {
                "truncated": True,
                "approximate": True,
                "sound": True,
                "sources": {
                    "ctgov": {
                        "status": "included",
                        "reason": "budget_exhausted",
                    }
                },
            },
        }

    asyncio.run(cache.get_or_execute({"condition": ROOT}, execute))
    now[0] = 59.9
    asyncio.run(cache.get_or_execute({"condition": ROOT}, execute))
    now[0] = 60.1
    asyncio.run(cache.get_or_execute({"condition": ROOT}, execute))

    assert calls == 2
    assert cache.hits == 1


def test_graph_cache_reuses_deterministic_source_limit_partials():
    cache = GraphV2Cache(random_uniform=lambda _low, _high: 0.0)
    calls = 0

    async def execute():
        nonlocal calls
        calls += 1
        return {
            "nodes": [{"id": ROOT}],
            "edges": [{"id": "edge", "kind": "evidence"}],
            "meta": {
                "truncated": True,
                "approximate": True,
                "sound": True,
                "sources": {
                    "aea": {
                        "status": "included",
                        "reason": "source_limit",
                    }
                },
            },
        }

    asyncio.run(cache.get_or_execute({"condition": ROOT}, execute))
    asyncio.run(cache.get_or_execute({"condition": ROOT}, execute))

    assert calls == 1
    assert cache.hits == 1
    assert cache.stats()["uncacheable_responses"] == 0


def test_graph_cache_treats_edge_detail_results_as_populated():
    now = [0.0]
    cache = GraphV2Cache(
        populated_ttl=60,
        empty_ttl=1,
        clock=lambda: now[0],
        random_uniform=lambda _low, _high: 0.0,
    )
    calls = 0

    async def execute():
        nonlocal calls
        calls += 1
        return {
            "results": [{"source": "WHO ICTRP", "study_id": "W1"}],
            "meta": {"sound": True, "sources": {}},
        }

    asyncio.run(cache.get_or_execute({"edge": "one"}, execute))
    now[0] = 2.0
    asyncio.run(cache.get_or_execute({"edge": "one"}, execute))

    assert calls == 1
    assert cache.hits == 1


def test_graph_cache_uses_empty_ttl_when_only_hierarchy_edges_are_present():
    now = [0.0]
    cache = GraphV2Cache(
        populated_ttl=100,
        empty_ttl=10,
        clock=lambda: now[0],
        random_uniform=lambda _low, _high: 0.0,
    )
    calls = 0

    async def execute():
        nonlocal calls
        calls += 1
        return {
            "nodes": [{"id": ROOT}],
            "edges": [{"id": "hierarchy", "kind": "hierarchy"}],
            "meta": {"sources": {}},
        }

    asyncio.run(cache.get_or_execute({"condition": ROOT}, execute))
    now[0] = 9.9
    asyncio.run(cache.get_or_execute({"condition": ROOT}, execute))
    now[0] = 10.1
    asyncio.run(cache.get_or_execute({"condition": ROOT}, execute))

    assert calls == 2
    assert cache.hits == 1


def test_graph_cache_identity_changes_with_loader_versions():
    query = graph_v2._graph_query(
        condition=[ROOT],
        intervention=[],
        outcome=[],
        region=[],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )

    first = graph_v2.graph_cache_identity(
        query, taxonomy_version="a" * 64, dataset_version="b" * 64
    )
    taxonomy_changed = graph_v2.graph_cache_identity(
        query, taxonomy_version="c" * 64, dataset_version="b" * 64
    )
    dataset_changed = graph_v2.graph_cache_identity(
        query, taxonomy_version="a" * 64, dataset_version="d" * 64
    )

    assert GraphV2Cache.canonical_key(first) != GraphV2Cache.canonical_key(taxonomy_changed)
    assert GraphV2Cache.canonical_key(first) != GraphV2Cache.canonical_key(dataset_changed)


def test_graph_cache_identity_includes_grouped_row_cap(monkeypatch):
    query = graph_v2._graph_query(
        condition=[ROOT],
        intervention=[],
        outcome=[],
        region=[],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )
    first = graph_v2.graph_cache_identity(
        query, taxonomy_version="a" * 64, dataset_version="b" * 64
    )

    monkeypatch.setattr(graph_v2, "MAX_GROUPED_ROWS", graph_v2.MAX_GROUPED_ROWS + 1)
    changed = graph_v2.graph_cache_identity(
        query, taxonomy_version="a" * 64, dataset_version="b" * 64
    )

    assert first["caps"]["groupedRows"] == 2_000
    assert GraphV2Cache.canonical_key(first) != GraphV2Cache.canonical_key(changed)


def test_graph_lab_keeps_safe_edge_hits_without_internal_coverage_notice():
    page = (
        graph_v2.REPO_ROOT
        / "site"
        / "public"
        / "labs"
        / "malnutrition-evidence-graph.html"
    ).read_text(encoding="utf-8")

    assert "graphCoverageNotice" not in page
    assert "Bounded result" not in page
    assert "studies without graphable intervention attribution" not in page
    assert "function edgeHitCoordinates(edge)" in page
    assert "function nearestEvidenceEdge(event)" in page
    assert ".attr('data-edge-id', d => d.id)" in page
    assert "stroke-width: 10px" in page
    assert "const selectedSubtreeIds = new Set();" in page
    assert "selectedNodes.forEach(node => includeSelectedSubtree(node.id));" in page
    assert "return selectedSubtreeContains(d)" in page
    assert ".filter(edge => edge.kind === 'evidence')" in page
    assert "function fitGraphToViewport" in page
    assert "window.requestAnimationFrame(() => fitGraphToViewport({ animate: true }));" in page
    assert "Retry loading studies" in page
    assert "Details unavailable" in page
    assert "The linked-study count is still valid." in page
    assert "id=\"retry-edge-studies\"" in page
    assert "detailParams.set('edge_token', edge.detailToken)" in page
    assert "role=\"alert\"" in page
    assert "aria-busy" in page
    assert "edge._detailState === 'loaded' && hasLiveQuery && edge._nextCursor" in page


def test_graph_v2_cache_stats_include_edge_details():
    response = TestClient(api_main.app).get("/graph/v2/cache/stats")

    assert response.status_code == 200
    assert response.json()["cache_namespace"].startswith("graph-v2-projection")
    assert response.json()["detail_cache"]["cache_namespace"].startswith(
        "graph-v2-edge-details"
    )


def test_shared_cache_stats_expose_graph_v2_counters():
    response = TestClient(api_main.app).get("/cache/stats")

    assert response.status_code == 200
    assert response.json()["graph_v2"]["cache_namespace"].startswith("graph-v2")


def test_graph_observability_emits_one_structured_summary_and_per_source_records(caplog):
    query = graph_v2._graph_query(
        condition=[ROOT],
        intervention=[],
        outcome=[],
        region=[],
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="or",
        region_logic="or",
    )
    payload = {
        "nodes": [{"id": ROOT}],
        "edges": [{"id": "edge"}],
        "meta": {
            "truncated": True,
            "approximate": True,
            "budget_exhausted": True,
            "source_timings_ms": {source: 1.25 for source in graph_v2.SOURCE_IDS},
            "sources": {
                source: {
                    "grouped_rows": 3,
                    "truncated": True,
                    "approximate": True,
                    "budget_exhausted": True,
                }
                for source in graph_v2.SOURCE_IDS
            },
        },
    }

    with caplog.at_level("INFO", logger=graph_v2.logger.name):
        graph_v2._log_graph_observability(
            query,
            payload,
            duration_ms=12.5,
            cache_status="miss",
            cache_bypass_reason=None,
        )

    request_records = [
        json.loads(record.message.removeprefix("graph_request "))
        for record in caplog.records
        if record.message.startswith("graph_request ")
    ]
    source_records = [
        json.loads(record.message.removeprefix("graph_source "))
        for record in caplog.records
        if record.message.startswith("graph_source ")
    ]
    assert len(request_records) == 1
    assert len(source_records) == len(graph_v2.SOURCE_IDS)
    assert {
        "duration_ms",
        "cache_status",
        "grouped_rows",
        "returned_nodes",
        "returned_edges",
        "truncated",
        "approximate",
        "budget_exhausted",
    } <= request_records[0].keys()
    assert all(
        {
            "source_duration_ms",
            "cache_status",
            "grouped_rows",
            "returned_nodes",
            "returned_edges",
            "truncated",
            "approximate",
            "budget_exhausted",
        }
        <= record.keys()
        for record in source_records
    )


def test_multi_state_row_labels_resolve_from_each_concept_identity(monkeypatch):
    from rdflib import Graph, Literal, URIRef
    from rdflib.namespace import SKOS

    states = Graph()
    for uri, label in [(ROOT, "Malnutrition"), (CHILD, "Child malnutrition")]:
        states.add((URIRef(uri), SKOS.prefLabel, Literal(label)))
    interventions = Graph()
    interventions.add((URIRef(INTERVENTION), SKOS.prefLabel, Literal("Nutrition intervention")))
    monkeypatch.setattr(graph_v2, "_state_graph", lambda: states)
    monkeypatch.setattr(graph_v2, "_intervention_graph", lambda: interventions)
    empty = {"nodes": [], "pairs": [], "roots": [], "omitted": 0}
    payload = graph_v2.build_graph_v2_payload(
        query=_query(intervention=[INTERVENTION]),
        condition_taxonomy=empty,
        intervention_taxonomy=_direct_state_intervention_taxonomy(),
        local_rows={"aea": [], "who-ictrp": []},
        live_rows={"ctgov": [{
            "study_id": "NCT1",
            "condition_concept_uri": ROOT,
            "condition_concept": "Malnutrition",
            "outcomes": [{"state_concept_uri": CHILD}],
            "intervention_concept_uri": INTERVENTION,
            "intervention_concept": "Incorrect row label",
        }], "isrctn": []},
        source_meta={source: _meta(1 if source == "ctgov" else 0)
                     for source in graph_v2.SOURCE_IDS},
    )
    nodes = {node["id"]: node for node in payload["nodes"]}
    assert nodes[ROOT]["label"] == "Malnutrition"
    assert nodes[CHILD]["label"] == "Child malnutrition"
    assert nodes[ROOT]["studyCount"] == nodes[CHILD]["studyCount"] == 1
    evidence = [edge for edge in payload["edges"] if edge["kind"] == "evidence"]
    assert {edge["target"] for edge in evidence} == {ROOT, CHILD}
    assert all(edge["weight"] == 1 for edge in evidence)
    assert nodes[INTERVENTION]["label"] == "Nutrition intervention"
