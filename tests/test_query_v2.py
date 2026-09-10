from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from fastapi.testclient import TestClient
import httpx
import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, SKOS

import api.main as api_main
import api.routes.query_v2 as query_v2_route
from api.query_v2_cache import QueryV2Cache
import scripts.query_v2 as query_v2_engine
from scripts.query_v2 import (
    AXES,
    BranchResult,
    CanonicalQuery,
    CtgovAdapter,
    CTGOV_V2_FIELDS,
    IsrctnAdapter,
    QueryV2Execution,
    RegionIndex,
    SOURCE_IDS,
    SOURCE_LABELS,
    SourceBudget,
    canonical_query,
    default_region_index,
    execute_graph_source,
    execute_query_v2,
    execute_source,
    plan_query,
    region_index_for_query,
    _local_detail_query,
    _local_selector_query,
    _limit_selected_studies,
    _resolve_local_concept,
)


MALARIA = "https://universalevidence.com/vocab/states/Malaria"
MALNUTRITION = "https://universalevidence.com/vocab/states/Malnutrition"
CHILD_MALNUTRITION = "https://universalevidence.com/vocab/states/ChildMalnutrition"
STUNTING = "https://universalevidence.com/vocab/states/Stunting"
WASTING = "https://universalevidence.com/vocab/states/Wasting"
NUTRITION = "https://universalevidence.com/vocab/interventions/NutritionIntervention"
NUTRITIONAL_SUPPORT = "https://universalevidence.com/vocab/interventions/NutritionalSupport"
RUTF = "https://universalevidence.com/vocab/interventions/ReadyToUseTherapeuticFoodIntervention"
CASH = "https://universalevidence.com/vocab/interventions/CashTransfer"
CHITTAGONG = "https://sws.geonames.org/1337200/"
DHAKA = "https://sws.geonames.org/1337179/"
BANGLADESH = "https://sws.geonames.org/1210997/"
KENYA = "https://sws.geonames.org/192950/"
UGANDA = "https://sws.geonames.org/226074/"
WORLD = "https://universalevidence.com/vocab/regions/World"


def make_query(**values: list[str]) -> CanonicalQuery:
    return canonical_query(values, {axis: "or" for axis in AXES})


@dataclass
class FakeAdapter:
    source_id: str
    rows_for_spec: Mapping[tuple[tuple[str, str], ...], list[dict[str, Any]]]
    calls: list[dict[str, str]]
    truncated: bool = False
    exhaust_after: int | None = None

    async def execute(self, spec, budget, regions):
        budget.claim_request()
        self.calls.append(dict(spec))
        rows = self.rows_for_spec.get(tuple(sorted(spec.items())), [])
        exhausted = self.exhaust_after is not None and len(self.calls) >= self.exhaust_after
        return BranchResult(
            rows=[dict(row) for row in rows],
            truncated=self.truncated or exhausted,
            approximate=exhausted,
            budget_exhausted=exhausted,
        )


def result(source: str, study_id: str, intervention: str | None = None) -> dict:
    return {
        "source": source,
        "study_id": study_id,
        "intervention": intervention,
        "outcomes": [],
    }


def test_aea_local_rows_never_expose_internal_evidence_uri_as_url():
    internal_uri = (
        "https://universalevidence.com/ontology/"
        "Evidence_AEA_AEARCTR-0011638"
    )
    public_url = "http://www.socialscienceregistry.org/trials/11638"

    minimal = query_v2_engine._minimal_local_rows("aea", [internal_uri])
    hydrated = query_v2_engine._local_rows(
        "aea",
        [
            {
                "study": internal_uri,
                "study_id": public_url,
                "title": "Decreasing abandonment of calls to the 988 Suicide and Crisis Lifeline",
            }
        ],
        {},
    )

    assert minimal[0]["url"] == "https://www.socialscienceregistry.org/trials/11638"
    assert hydrated[0]["url"] == public_url
    assert all("/ontology/Evidence_AEA_" not in row["url"] for row in minimal + hydrated)


def test_canonical_request_sorts_deduplicates_and_cache_key_includes_logic():
    first = canonical_query(
        {"condition": [MALARIA, MALNUTRITION, MALARIA]},
        {"condition": "or"},
    )
    second = canonical_query(
        {"condition": [MALNUTRITION, MALARIA]},
        {"condition": "or"},
    )
    changed_logic = canonical_query(
        {"condition": [MALNUTRITION, MALARIA]},
        {"condition": "and"},
    )

    assert first == second
    assert first.values["condition"] == tuple(sorted((MALARIA, MALNUTRITION)))
    assert QueryV2Cache.canonical_key(first) == QueryV2Cache.canonical_key(second)
    assert QueryV2Cache.canonical_key(first) != QueryV2Cache.canonical_key(changed_logic)
    assert QueryV2Cache.canonical_key(first).startswith("query-v2-envelope-1|")
    assert '"taxonomy_version":"process-local-fuseki"' in QueryV2Cache.canonical_key(first)
    assert '"dataset_version":"process-local-fuseki"' in QueryV2Cache.canonical_key(first)


def test_planner_pushes_every_cross_axis_predicate_into_each_branch():
    query = canonical_query(
        {"condition": [MALARIA], "region": [KENYA, UGANDA]},
        {"condition": "or", "region": "and"},
    )

    clauses = plan_query(query)

    assert len(clauses) == 1
    assert clauses[0].specs == (
        {"condition": MALARIA, "region": KENYA},
        {"condition": MALARIA, "region": UGANDA},
    )


def test_graph_applies_one_post_union_source_cap_and_keeps_complete_rows():
    calls: list[dict[str, str]] = []

    def branch_rows(state: str, start: int) -> list[dict[str, Any]]:
        return [
            {
                "source": "CT.gov",
                "study_id": f"NCT{study_index:04d}",
                "state_concept_uri": state,
                "state_concept": state.rsplit("/", 1)[-1],
                "intervention_concept_uri": intervention,
                "intervention_concept": intervention.rsplit("/", 1)[-1],
                "outcomes": [],
            }
            for study_index in range(start, start + 75)
            for intervention in (RUTF, CASH)
        ]

    adapter = FakeAdapter(
        "ctgov",
        {
            (("state", STUNTING),): branch_rows(STUNTING, 0),
            (("state", WASTING),): branch_rows(WASTING, 75),
        },
        calls,
    )
    rows, meta = asyncio.run(
        execute_graph_source(
            make_query(state=[STUNTING, WASTING]),
            adapter,
            default_region_index(),
        )
    )

    retained_ids = {row["study_id"] for row in rows}
    assert len(retained_ids) == query_v2_engine.UNIQUE_STUDY_LIMIT
    assert len(rows) == 2 * query_v2_engine.UNIQUE_STUDY_LIMIT
    assert all(
        {
            row["intervention_concept_uri"]
            for row in rows
            if row["study_id"] == study_id
        }
        == {RUTF, CASH}
        for study_id in retained_ids
    )
    assert meta["returned_unique_studies"] == query_v2_engine.UNIQUE_STUDY_LIMIT
    assert meta["truncated"] is True
    assert meta["approximate"] is True
    assert meta["reason"] == "source_limit"
    assert meta["source_limit_reached"] is True
    assert meta["source_limit_omitted_lower_bound"] == 50


def test_graph_exact_truncated_window_does_not_invent_an_omitted_study():
    calls: list[dict[str, str]] = []
    rows = [
        {
            "source": "CT.gov",
            "study_id": f"NCT{index:04d}",
            "state_concept_uri": STUNTING,
            "intervention_concept_uri": RUTF,
            "outcomes": [],
        }
        for index in range(query_v2_engine.UNIQUE_STUDY_LIMIT)
    ]
    adapter = FakeAdapter(
        "isrctn",
        {(("state", STUNTING),): rows},
        calls,
        truncated=True,
    )

    returned, meta = asyncio.run(
        execute_graph_source(
            make_query(state=[STUNTING]),
            adapter,
            default_region_index(),
        )
    )

    assert len({row["study_id"] for row in returned}) == 100
    assert meta["returned_unique_studies"] == 100
    assert meta["truncated"] is True
    assert meta["approximate"] is True
    assert meta["reason"] == "source_limit"
    assert meta["source_limit_reached"] is True
    assert meta["source_limit_omitted_lower_bound"] == 0


@pytest.mark.parametrize("source_id", ["ctgov", "isrctn"])
@pytest.mark.parametrize("state_logic", ["or", "and"])
def test_graph_never_stamps_state_branches_without_direct_attribution(
    source_id,
    state_logic,
):
    calls: list[dict[str, str]] = []
    blank_state_row = {
        "source": "CT.gov" if source_id == "ctgov" else "ISRCTN",
        "study_id": (
            "NCT-BRANCH-PROVENANCE"
            if source_id == "ctgov"
            else "ISRCTN-BRANCH-PROVENANCE"
        ),
        "intervention_concept_uri": RUTF,
        "intervention_concept": "RUTF",
        "outcomes": [],
    }
    adapter = FakeAdapter(
        source_id,
        {
            (("state", STUNTING),): [blank_state_row],
            (("state", WASTING),): [blank_state_row],
        },
        calls,
    )
    rows, meta = asyncio.run(
        execute_graph_source(
            canonical_query(
                {"state": [STUNTING, WASTING]},
                {"state": state_logic},
            ),
            adapter,
            default_region_index(),
        )
    )

    if state_logic == "and":
        assert rows == []
        assert meta["returned_unique_studies"] == 0
    else:
        assert len(rows) == 1
        assert all(not row.get("state_concept_uri") for row in rows)
        assert all(not row.get("condition_concept_uri") for row in rows)
        assert {row["intervention_concept_uri"] for row in rows} == {RUTF}
        assert meta["returned_unique_studies"] == 1
    assert meta["source_limit_reached"] is False


@pytest.mark.parametrize("source_id", ["ctgov", "isrctn"])
@pytest.mark.parametrize(
    ("state_logic", "expected_studies"),
    [
        ("or", {"SHARED", "STUNTING-ONLY", "WASTING-ONLY"}),
        ("and", {"SHARED"}),
    ],
)
def test_graph_state_any_all_preserves_only_direct_branch_coordinates(
    source_id,
    state_logic,
    expected_studies,
):
    source = "CT.gov" if source_id == "ctgov" else "ISRCTN"

    def row(study_id: str, state: str) -> dict[str, Any]:
        return {
            "source": source,
            "study_id": study_id,
            "state_concept_uri": state,
            "state_concept": state.rsplit("/", 1)[-1],
            "intervention_concept_uri": RUTF,
            "intervention_concept": "RUTF",
            "outcomes": [],
        }

    adapter = FakeAdapter(
        source_id,
        {
            (("state", STUNTING),): [
                row("SHARED", STUNTING),
                row("STUNTING-ONLY", STUNTING),
            ],
            (("state", WASTING),): [
                row("SHARED", WASTING),
                row("WASTING-ONLY", WASTING),
            ],
        },
        [],
    )

    rows, meta = asyncio.run(
        execute_graph_source(
            canonical_query(
                {"state": [STUNTING, WASTING]},
                {"state": state_logic},
            ),
            adapter,
            default_region_index(),
        )
    )

    assert {row["study_id"] for row in rows} == expected_studies
    assert {row["state_concept_uri"] for row in rows} == {STUNTING, WASTING}
    assert all(row["intervention_concept_uri"] == RUTF for row in rows)
    assert meta["returned_unique_studies"] == len(expected_studies)


@pytest.mark.parametrize("source_id", ["ctgov", "isrctn"])
@pytest.mark.parametrize(
    ("unsupported_state", "case_id"),
    [
        (None, "unmapped"),
        (MALARIA, "outside-subtree"),
    ],
)
def test_graph_state_all_rejects_unsupported_ctgov_and_isrctn_branches(
    source_id,
    unsupported_state,
    case_id,
):
    source = "CT.gov" if source_id == "ctgov" else "ISRCTN"
    study_id = f"{source_id.upper()}-{case_id}"
    direct_row = {
        "source": source,
        "study_id": study_id,
        "state_concept_uri": STUNTING,
        "condition_concept_uri": STUNTING,
        "intervention_concept_uri": RUTF,
        "outcomes": [],
    }
    unsupported_row = {
        "source": source,
        "study_id": study_id,
        "intervention_concept_uri": RUTF,
        "outcomes": [],
    }
    if unsupported_state:
        unsupported_row["state_concept_uri"] = unsupported_state
        unsupported_row["condition_concept_uri"] = unsupported_state
    adapter = FakeAdapter(
        source_id,
        {
            (("state", STUNTING),): [direct_row],
            (("state", WASTING),): [unsupported_row],
        },
        [],
    )

    rows, meta = asyncio.run(
        execute_graph_source(
            canonical_query(
                {"state": [STUNTING, WASTING]},
                {"state": "and"},
            ),
            adapter,
            default_region_index(),
        )
    )

    assert rows == []
    assert meta["status"] == "included"
    assert meta["returned_unique_studies"] == 0
    assert meta["truncated"] is False
    assert meta["approximate"] is False


@pytest.mark.parametrize("source_id", ["ctgov", "isrctn"])
def test_graph_state_any_keeps_only_the_directly_supported_coordinate(source_id):
    source = "CT.gov" if source_id == "ctgov" else "ISRCTN"
    study_id = f"{source_id.upper()}-ONE-DIRECT"
    adapter = FakeAdapter(
        source_id,
        {
            (("state", STUNTING),): [
                {
                    "source": source,
                    "study_id": study_id,
                    "state_concept_uri": STUNTING,
                    "condition_concept_uri": STUNTING,
                    "intervention_concept_uri": RUTF,
                    "outcomes": [],
                }
            ],
            (("state", WASTING),): [
                {
                    "source": source,
                    "study_id": study_id,
                    "intervention_concept_uri": RUTF,
                    "outcomes": [],
                }
            ],
        },
        [],
    )

    rows, meta = asyncio.run(
        execute_graph_source(
            canonical_query(
                {"state": [STUNTING, WASTING]},
                {"state": "or"},
            ),
            adapter,
            default_region_index(),
        )
    )

    assert len(rows) == 1
    assert rows[0]["state_concept_uri"] == STUNTING
    assert rows[0]["intervention_concept_uri"] == RUTF
    assert meta["returned_unique_studies"] == 1


def test_concept_resolution_uses_tracked_taxonomy_without_fuseki():
    resolution = _resolve_local_concept(MALNUTRITION)

    assert resolution is not None
    assert resolution.label == "Malnutrition"
    assert "Undernutrition" in resolution.terms
    assert resolution.mesh_uris


def test_region_and_intersects_at_study_level_while_region_or_unions():
    rows = {
        tuple(sorted({"region": KENYA}.items())): [
            result("CT.gov", "NCT-MULTISITE"),
            result("CT.gov", "NCT-KENYA"),
        ],
        tuple(sorted({"region": UGANDA}.items())): [
            result("CT.gov", "NCT-MULTISITE"),
            result("CT.gov", "NCT-UGANDA"),
        ],
    }

    async def run(logic: str):
        adapter = FakeAdapter("ctgov", rows, [])
        query = canonical_query(
            {"region": [KENYA, UGANDA]}, {"region": logic}
        )
        return await execute_source(query, adapter, default_region_index())

    and_rows, _ = asyncio.run(run("and"))
    or_rows, _ = asyncio.run(run("or"))

    assert {row["study_id"] for row in and_rows} == {"NCT-MULTISITE"}
    assert {row["study_id"] for row in or_rows} == {
        "NCT-MULTISITE",
        "NCT-KENYA",
        "NCT-UGANDA",
    }


def test_contextual_region_evidence_does_not_broaden_chittagong():
    regions = default_region_index()

    assert regions.verified_match([CHITTAGONG, BANGLADESH], CHITTAGONG)
    assert not regions.verified_match([DHAKA, BANGLADESH], CHITTAGONG)
    assert regions.verified_match([CHITTAGONG], BANGLADESH)


def test_ctgov_contextual_crosswalk_falls_back_to_loaded_fuseki(monkeypatch, tmp_path):
    expected = {"ctgov-location:v1|country=Bangladesh|state=|city=Chittagong": CHITTAGONG}
    monkeypatch.setattr(query_v2_engine.v1, "VOCABULARIES_DIR", tmp_path)
    monkeypatch.setattr(query_v2_engine.v1, "_CTGOV_REGIONS_XWALK", None)
    monkeypatch.setattr(query_v2_engine.v1, "_load_fuseki_xwalk", lambda: expected)

    assert query_v2_engine.v1.get_ctgov_regions_xwalk() is expected


def test_ctgov_contextual_crosswalk_fails_closed_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(query_v2_engine.v1, "VOCABULARIES_DIR", tmp_path)
    monkeypatch.setattr(query_v2_engine.v1, "_CTGOV_REGIONS_XWALK", None)
    monkeypatch.setattr(
        query_v2_engine.v1,
        "_load_fuseki_xwalk",
        lambda: {"Bangladesh": "https://sws.geonames.org/1210997/"},
    )

    with pytest.raises(RuntimeError, match="Contextual"):
        query_v2_engine.v1.get_ctgov_regions_xwalk()


def test_ctgov_country_filter_uses_hydrated_region_labels(monkeypatch):
    graph = Graph()
    country = URIRef(BANGLADESH)
    graph.add((country, query_v2_engine.UE.iso3166Alpha2, Literal("BD")))
    graph.add((country, SKOS.prefLabel, Literal("Bangladesh")))
    regions = RegionIndex(graph)
    study = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT-COUNTRY"},
            "contactsLocationsModule": {
                "locations": [{"country": "Bangladesh"}]
            }
        }
    }
    monkeypatch.setattr(
        query_v2_engine.v1,
        "normalize_country_to_iso",
        lambda *_args, **_kwargs: pytest.fail("legacy local mirror fallback used"),
    )

    assert query_v2_engine._raw_ctgov_country_isos(study, regions) == {"BD"}


def test_region_hierarchy_hydrates_from_fuseki_when_ignored_mirrors_are_absent(
    monkeypatch,
):
    graph = Graph().parse(
        Path(__file__).parents[1] / "vocabularies" / "regions.ttl",
        format="turtle",
    )
    index = RegionIndex(graph)
    assert index.kind(CHITTAGONG) == "unknown"
    assert index.country_isos_for_labels(["Bangladesh"]) == set()

    bindings = [
        {
            "region": {"type": "uri", "value": CHITTAGONG},
            "label": {"type": "literal", "value": "Chittagong"},
            "featureCode": {
                "type": "uri",
                "value": "http://www.geonames.org/ontology#A.ADM1",
            },
            "parent": {"type": "uri", "value": BANGLADESH},
            "parentIso": {"type": "literal", "value": "BD"},
            "parentName": {"type": "literal", "value": "Bangladesh"},
        }
    ]
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"results": {"bindings": bindings}},
            request=request,
        )
    )
    real_client = httpx.AsyncClient
    monkeypatch.setattr(query_v2_engine, "default_region_index", lambda: index)
    monkeypatch.setattr(
        query_v2_engine.httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_client(transport=transport, timeout=kwargs.get("timeout")),
    )

    hydrated = asyncio.run(region_index_for_query(make_query(region=[CHITTAGONG])))

    descriptor = hydrated.describe(CHITTAGONG)
    assert descriptor.kind == "adm1"
    assert descriptor.country_uris == (BANGLADESH,)
    assert descriptor.country_isos == ("BD",)
    assert hydrated.country_isos_for_labels(["Bangladesh"]) == {"BD"}


def test_region_hydration_has_a_total_wall_clock_deadline(monkeypatch):
    graph = Graph().parse(
        Path(__file__).parents[1] / "vocabularies" / "regions.ttl",
        format="turtle",
    )
    index = RegionIndex(graph)

    async def slow_response(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"results": {"bindings": []}}, request=request)

    transport = httpx.MockTransport(slow_response)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(query_v2_engine, "TIME_BUDGET_SECONDS", 0.01)
    monkeypatch.setattr(query_v2_engine, "default_region_index", lambda: index)
    monkeypatch.setattr(
        query_v2_engine.httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_client(
            transport=transport,
            timeout=kwargs.get("timeout"),
        ),
    )

    with pytest.raises(query_v2_engine.SourceUnavailable):
        asyncio.run(region_index_for_query(make_query(region=[CHITTAGONG])))


def test_source_stage_deadline_returns_sound_partial_metadata():
    class SlowAdapter(FakeAdapter):
        async def execute(self, spec, budget, regions):
            await asyncio.sleep(0.1)
            return BranchResult(rows=[result(SOURCE_LABELS[self.source_id], "TOO-LATE")])

    spec_key = (("condition", MALARIA),)
    adapters = {
        source: (
            SlowAdapter(source, {}, [])
            if source == "ctgov"
            else FakeAdapter(
                source,
                {spec_key: [result(SOURCE_LABELS[source], source.upper())]},
                [],
            )
        )
        for source in SOURCE_IDS
    }

    execution = asyncio.run(
        execute_query_v2(
            make_query(condition=[MALARIA]),
            adapters=adapters,
            regions=default_region_index(),
            source_stage_timeout=0.01,
        )
    )

    assert execution.payload["meta"]["sources"]["ctgov"] == {
        "status": "included",
        "coverage": "full",
        "returned_unique_studies": 0,
        "truncated": True,
        "approximate": True,
        "reason": "budget_exhausted",
    }
    assert {row["source"] for row in execution.payload["results"]} == {
        SOURCE_LABELS[source] for source in SOURCE_IDS if source != "ctgov"
    }


def test_ctgov_adapter_applies_adm1_verification_before_presentation(monkeypatch):
    raw_studies = [
        {"protocolSection": {"identificationModule": {"nctId": "NCT-CHITTAGONG"}}},
        {"protocolSection": {"identificationModule": {"nctId": "NCT-DHAKA"}}},
    ]

    def map_study(raw, _iso, **_kwargs):
        study_id = raw["protocolSection"]["identificationModule"]["nctId"]
        return {
            "source": "CT.gov",
            "nctId": study_id,
            "title": study_id,
            "url": f"https://clinicaltrials.gov/study/{study_id}",
            "intervention": "A",
            "_intervention_names": ["A"],
            "condition_mesh_uris": [],
            "intervention_mesh_uris": [],
            "country": "BD",
            "_location_region_uris": (
                [CHITTAGONG, BANGLADESH]
                if study_id.endswith("CHITTAGONG")
                else [DHAKA, BANGLADESH]
            ),
            "status": "completed",
            "year": 2026,
            "outcomes": [],
        }

    monkeypatch.setattr(query_v2_engine.v1, "map_ctgov_study", map_study)
    monkeypatch.setattr(query_v2_engine.v1, "get_ctgov_regions_xwalk", lambda: {})
    monkeypatch.setattr(query_v2_engine.v1, "_load_ctgov_intervention_concept_map", lambda: {})
    monkeypatch.setattr(query_v2_engine.v1, "_load_ctgov_outcome_concept_map", lambda: {})
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"studies": raw_studies}, request=request)
    )
    adapter = CtgovAdapter(lambda: httpx.AsyncClient(transport=transport))

    branch = asyncio.run(
        adapter.execute(
            {"region": CHITTAGONG}, SourceBudget(), default_region_index()
        )
    )

    assert {row["study_id"] for row in branch.rows} == {"NCT-CHITTAGONG"}
    assert "_location_region_uris" not in branch.rows[0]
    assert "_locations" not in branch.rows[0]


def test_ctgov_region_only_enriches_selected_studies_with_condition_concepts(monkeypatch):
    mesh_uri = "https://id.nlm.nih.gov/mesh/D008288"
    raw_studies = [
        {"protocolSection": {"identificationModule": {"nctId": "NCT-MALARIA"}}}
    ]

    def map_study(raw, _iso, **_kwargs):
        study_id = raw["protocolSection"]["identificationModule"]["nctId"]
        return {
            "source": "CT.gov",
            "nctId": study_id,
            "title": study_id,
            "url": f"https://clinicaltrials.gov/study/{study_id}",
            "intervention": "A",
            "_intervention_names": ["A", "B"],
            "condition_mesh_uris": [mesh_uri],
            "intervention_mesh_uris": [],
            "country": "",
            "_location_region_uris": [],
            "status": "completed",
            "year": 2026,
            "outcomes": [],
        }

    looked_up = []

    def lookup_states(mesh_uris):
        looked_up.extend(mesh_uris)
        return {mesh_uri: (MALARIA, "Malaria")}

    monkeypatch.setattr(query_v2_engine.v1, "map_ctgov_study", map_study)
    monkeypatch.setattr(query_v2_engine, "_local_states_by_mesh", lookup_states)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"studies": raw_studies}, request=request)
    )
    adapter = CtgovAdapter(lambda: httpx.AsyncClient(transport=transport))

    branch = asyncio.run(
        adapter.execute({"region": WORLD}, SourceBudget(), default_region_index())
    )

    assert looked_up == [mesh_uri]
    assert len(branch.rows) == 2
    assert {row["condition_concept_uri"] for row in branch.rows} == {MALARIA}
    assert {row["condition_concept"] for row in branch.rows} == {"Malaria"}
    assert all("condition_mesh_uris" not in row for row in branch.rows)


def test_ctgov_condition_only_uses_intervention_crosswalk_for_rutf(monkeypatch):
    raw_studies = [
        {"protocolSection": {"identificationModule": {"nctId": "NCT-RUTF"}}}
    ]

    def map_study(raw, _iso, **_kwargs):
        study_id = raw["protocolSection"]["identificationModule"]["nctId"]
        return {
            "source": "CT.gov",
            "nctId": study_id,
            "title": "RUTF for severe acute malnutrition",
            "url": f"https://clinicaltrials.gov/study/{study_id}",
            "intervention": "Ready to Use Therapeutic Food (RUTF)",
            "_intervention_names": ["Ready to Use Therapeutic Food (RUTF)"],
            "condition_mesh_uris": [],
            "intervention_mesh_uris": [],
            "country": "",
            "_location_region_uris": [],
            "status": "completed",
            "year": 2025,
            "outcomes": [],
        }

    monkeypatch.setattr(query_v2_engine.v1, "map_ctgov_study", map_study)
    monkeypatch.setattr(
        query_v2_engine.v1,
        "_load_ctgov_intervention_concept_map",
        lambda: {
            "Ready to Use Therapeutic Food (RUTF)": (
                RUTF,
                "Ready-to-use therapeutic/supplementary food",
            )
        },
    )
    monkeypatch.setattr(
        query_v2_engine.v1,
        "lookup_intervention_concepts_by_label",
        lambda _texts: {},
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"studies": raw_studies}, request=request
        )
    )
    adapter = CtgovAdapter(lambda: httpx.AsyncClient(transport=transport))

    branch = asyncio.run(
        adapter.execute(
            {"condition": MALNUTRITION},
            SourceBudget(),
            default_region_index(),
        )
    )

    assert branch.rows[0]["intervention_concept_uri"] == RUTF
    assert (
        branch.rows[0]["intervention_concept"]
        == "Ready-to-use therapeutic/supplementary food"
    )


def test_ctgov_presentation_prefers_specific_graph_coordinates_under_selected_roots():
    rows = query_v2_engine._ctgov_presentation(
        [
            {
                "source": "CT.gov",
                "nctId": "NCT-SPECIFIC",
                "title": "Specific nutrition trial",
                "url": "https://clinicaltrials.gov/study/NCT-SPECIFIC",
                "_intervention_names": ["RUTF"],
                "condition_mesh_uris": ["mesh:child-malnutrition"],
                "intervention_mesh_uris": [],
                "outcomes": [],
            }
        ],
        {"condition": MALNUTRITION, "intervention": NUTRITIONAL_SUPPORT},
        {"condition": "Malnutrition", "intervention": "Nutritional support"},
        {"mesh:child-malnutrition": (CHILD_MALNUTRITION, "Child malnutrition")},
        {"RUTF": (RUTF, "Ready-to-use therapeutic/supplementary food")},
        {},
        {},
        specific_attribution=True,
    )

    assert [(row["condition_concept_uri"], row["intervention_concept_uri"]) for row in rows] == [
        (CHILD_MALNUTRITION, RUTF)
    ]


def test_ctgov_specific_coordinate_enrichment_is_graph_only():
    rows = query_v2_engine._ctgov_presentation(
        [
            {
                "source": "CT.gov",
                "nctId": "NCT-SEARCH",
                "_intervention_names": ["RUTF"],
                "condition_mesh_uris": ["mesh:child-malnutrition"],
                "intervention_mesh_uris": [],
                "outcomes": [],
            }
        ],
        {"condition": MALNUTRITION, "intervention": NUTRITION},
        {"condition": "Malnutrition", "intervention": "Nutrition intervention"},
        {"mesh:child-malnutrition": (CHILD_MALNUTRITION, "Child malnutrition")},
        {"RUTF": (RUTF, "Ready-to-use therapeutic/supplementary food")},
        {},
        {},
    )

    assert [(row["condition_concept_uri"], row["intervention_concept_uri"]) for row in rows] == [
        (MALNUTRITION, NUTRITION)
    ]


def test_ctgov_graph_preserves_interventions_without_inventing_state():
    rows = query_v2_engine._ctgov_presentation(
        [
            {
                "source": "CT.gov",
                "nctId": "NCT-FALLBACK",
                "_intervention_names": ["Combination"],
                "condition_mesh_uris": [],
                "intervention_mesh_uris": [],
                "outcomes": [{"measure": "Unmapped measure"}],
            }
        ],
        {"state": STUNTING},
        {"state": "Stunting"},
        {},
        {"Combination": ((RUTF, "RUTF"), (CASH, "Cash transfer"))},
        {},
        {},
        specific_attribution=True,
    )

    assert {row["intervention_concept_uri"] for row in rows} == {RUTF, CASH}
    assert all(not row.get("state_concept_uri") for row in rows)
    assert len(rows) == 2


def test_ctgov_graph_excludes_direct_condition_outside_selected_state_subtree():
    rows = query_v2_engine._ctgov_presentation(
        [
            {
                "source": "CT.gov",
                "nctId": "NCT-OUTSIDE-STATE",
                "_intervention_names": ["Combination"],
                "condition_mesh_uris": ["mesh:malaria"],
                "intervention_mesh_uris": [],
                "outcomes": [],
            }
        ],
        {"state": STUNTING},
        {"state": "Stunting"},
        {"mesh:malaria": (MALARIA, "Malaria")},
        {"Combination": ((RUTF, "RUTF"), (CASH, "Cash transfer"))},
        {},
        {},
        specific_attribution=True,
    )

    assert {row["intervention_concept_uri"] for row in rows} == {RUTF, CASH}
    assert all(not row.get("state_concept_uri") for row in rows)
    assert all(not row.get("condition_concept_uri") for row in rows)


def test_ctgov_graph_attributes_explicit_condition_and_outcome_roles_independently():
    rows = query_v2_engine._ctgov_presentation(
        [
            {
                "source": "CT.gov",
                "nctId": "NCT-TWO-ROLES",
                "_intervention_names": ["RUTF"],
                "condition_mesh_uris": ["mesh:stunting"],
                "intervention_mesh_uris": [],
                "outcomes": [
                    {
                        "measure": "Weight-for-height",
                        "state_concept_uri": WASTING,
                        "state_concept": "Wasting",
                    }
                ],
            }
        ],
        {"condition": STUNTING, "outcome": WASTING},
        {"condition": "Stunting", "outcome": "Wasting"},
        {"mesh:stunting": (STUNTING, "Stunting")},
        {"RUTF": (RUTF, "RUTF")},
        {},
        {},
        specific_attribution=True,
    )

    assert {row["state_concept_uri"] for row in rows} == {STUNTING, WASTING}
    assert {
        row.get("condition_concept_uri") for row in rows
    } == {None, STUNTING}


def test_ctgov_graph_mixed_state_and_explicit_outcome_preserves_both_roots():
    rows = query_v2_engine._ctgov_presentation(
        [
            {
                "source": "CT.gov",
                "nctId": "NCT-MIXED-ROOTS",
                "_intervention_names": ["RUTF"],
                "condition_mesh_uris": ["mesh:stunting"],
                "intervention_mesh_uris": [],
                "outcomes": [
                    {
                        "measure": "Weight-for-height",
                        "state_concept_uri": WASTING,
                        "state_concept": "Wasting",
                    }
                ],
            }
        ],
        {"state": STUNTING, "outcome": WASTING},
        {"state": "Stunting", "outcome": "Wasting"},
        {"mesh:stunting": (STUNTING, "Stunting")},
        {"RUTF": (RUTF, "RUTF")},
        {},
        {},
        specific_attribution=True,
    )

    assert {row["state_concept_uri"] for row in rows} == {STUNTING, WASTING}


@pytest.mark.parametrize(
    ("spec", "expected_state"),
    [
        ({"condition": MALNUTRITION}, CHILD_MALNUTRITION),
        ({"outcome": MALNUTRITION}, STUNTING),
    ],
)
def test_ctgov_graph_single_explicit_role_does_not_project_the_other_role(
    spec,
    expected_state,
):
    rows = query_v2_engine._ctgov_presentation(
        [
            {
                "source": "CT.gov",
                "nctId": "NCT-ONE-ROLE",
                "_intervention_names": ["RUTF"],
                "condition_mesh_uris": ["mesh:child-malnutrition"],
                "intervention_mesh_uris": [],
                "outcomes": [
                    {
                        "measure": "Stunting prevalence",
                        "state_concept_uri": STUNTING,
                        "state_concept": "Stunting",
                    }
                ],
            }
        ],
        spec,
        {},
        {
            "mesh:child-malnutrition": (
                CHILD_MALNUTRITION,
                "Child malnutrition",
            )
        },
        {"RUTF": (RUTF, "RUTF")},
        {},
        {},
        specific_attribution=True,
    )

    assert {row["state_concept_uri"] for row in rows} == {expected_state}


def test_ctgov_graph_mesh_fallback_preserves_every_direct_target(monkeypatch):
    state_mesh = URIRef("http://id.nlm.nih.gov/mesh/DSTATE")
    intervention_mesh = URIRef("http://id.nlm.nih.gov/mesh/DINTERVENTION")
    state_graph = Graph()
    intervention_graph = Graph()
    state_targets = {
        "https://example.org/state/a": "State A",
        "https://example.org/state/b": "State B",
    }
    intervention_targets = {
        "https://example.org/intervention/a": "Intervention A",
        "https://example.org/intervention/b": "Intervention B",
    }
    for uri, label in state_targets.items():
        state_graph.add((URIRef(uri), SKOS.exactMatch, state_mesh))
        state_graph.add((URIRef(uri), SKOS.prefLabel, Literal(label)))
    for uri, label in intervention_targets.items():
        intervention_graph.add((URIRef(uri), SKOS.closeMatch, intervention_mesh))
        intervention_graph.add((URIRef(uri), SKOS.prefLabel, Literal(label)))
    monkeypatch.setattr(
        query_v2_engine,
        "_local_taxonomy_graph",
        lambda filename: state_graph
        if filename == "states.ttl"
        else intervention_graph,
    )

    state_map = query_v2_engine._local_states_by_mesh_multi([str(state_mesh)])
    intervention_map = query_v2_engine._local_interventions_by_mesh_multi(
        [str(intervention_mesh)]
    )
    rows = query_v2_engine._ctgov_presentation(
        [
            {
                "source": "CT.gov",
                "nctId": "NCT-MESH-MULTI",
                "_intervention_names": ["Unmapped raw name"],
                "condition_mesh_uris": [str(state_mesh)],
                "intervention_mesh_uris": [str(intervention_mesh)],
                "outcomes": [],
            }
        ],
        {},
        {},
        state_map,
        {},
        intervention_map,
        {},
        specific_attribution=True,
    )

    assert {
        (row["state_concept_uri"], row["intervention_concept_uri"])
        for row in rows
    } == {
        (state_uri, intervention_uri)
        for state_uri in state_targets
        for intervention_uri in intervention_targets
    }


def test_local_intervention_label_lookup_preserves_every_exact_target(
    monkeypatch,
):
    shared_label = "Shared exact intervention label"
    first = URIRef("https://example.org/intervention/a")
    second = URIRef("https://example.org/intervention/b")
    not_intervention = URIRef("https://example.org/state/not-intervention")
    graph = Graph()
    graph.add((first, RDF.type, query_v2_engine.UE.Intervention))
    graph.add((first, SKOS.prefLabel, Literal("Intervention A")))
    graph.add((first, SKOS.altLabel, Literal(shared_label)))
    graph.add((second, RDF.type, query_v2_engine.UE.Intervention))
    graph.add((second, SKOS.prefLabel, Literal(shared_label)))
    graph.add((not_intervention, RDF.type, query_v2_engine.UE.State))
    graph.add((not_intervention, SKOS.prefLabel, Literal(shared_label)))
    monkeypatch.setattr(
        query_v2_engine,
        "_local_taxonomy_graph",
        lambda filename: graph,
    )

    result = query_v2_engine._local_interventions_by_label_multi(
        [shared_label, shared_label, shared_label.lower()]
    )

    assert result == {
        shared_label: (
            (str(first), "Intervention A"),
            (str(second), shared_label),
        )
    }


def test_ctgov_outcome_crosswalk_lookup_filters_prewarmed_map(monkeypatch):
    calls: list[tuple[str, str]] = []

    def source_map(source_id: str, role: str):
        calls.append((source_id, role))
        return {
            "Measure 0": (STUNTING,),
            "Measure 1": (WASTING,),
            "Not selected": (MALNUTRITION,),
        }

    monkeypatch.setattr(
        query_v2_engine,
        "_source_direct_state_crosswalk",
        source_map,
    )
    monkeypatch.setattr(
        query_v2_engine.v1,
        "sparql_select",
        lambda _query: (_ for _ in ()).throw(
            AssertionError("request-time Fuseki lookup must not run")
        ),
    )

    result = query_v2_engine._bounded_ctgov_direct_outcome_crosswalk(
        ["Measure 0", "Measure  1", "Unknown", "Measure 0"]
    )

    assert result == {
        "Measure 0": (STUNTING,),
    }
    assert calls == [("ctgov", "outcome")]


def test_ctgov_graph_outcomes_keep_exact_raw_text_within_same_batch():
    rows = query_v2_engine._ctgov_presentation(
        [
            {
                "source": "CT.gov",
                "nctId": "NCT-EXACT-OUTCOME",
                "_intervention_names": [],
                "condition_mesh_uris": [],
                "intervention_mesh_uris": [],
                "outcomes": [{"measure": "Weight gain"}],
            },
            {
                "source": "CT.gov",
                "nctId": "NCT-WHITESPACE-OUTCOME",
                "_intervention_names": [],
                "condition_mesh_uris": [],
                "intervention_mesh_uris": [],
                "outcomes": [{"measure": "Weight  gain"}],
            },
        ],
        {},
        {},
        {},
        {},
        {},
        {"Weight gain": ((STUNTING, "Stunting"),)},
        specific_attribution=True,
    )

    by_study = {row["study_id"]: row for row in rows}
    exact = by_study["NCT-EXACT-OUTCOME"]
    whitespace_variant = by_study["NCT-WHITESPACE-OUTCOME"]
    assert exact["state_concept_uri"] == STUNTING
    assert exact["outcomes"][0]["state_concept_uri"] == STUNTING
    assert "state_concept_uri" not in whitespace_variant
    assert "state_concept_uri" not in whitespace_variant["outcomes"][0]


@pytest.mark.parametrize(
    ("source_id", "raw_value", "lookup_value", "matches"),
    [
        ("who-ictrp", "registry  outcome", "registry outcome", False),
        ("ctgov", "registry  outcome", "registry outcome", False),
        ("aea", "registry  outcome", "registry outcome", True),
        ("isrctn", "registry  outcome", "registry outcome", True),
    ],
)
def test_direct_state_keys_are_source_aware(
    source_id,
    raw_value,
    lookup_value,
    matches,
):
    uri = f"https://example.org/state/{source_id}"
    crosswalk = {
        query_v2_engine._source_direct_state_key(source_id, raw_value): (uri,)
    }

    result = query_v2_engine._mapped_source_state_uris(
        source_id,
        crosswalk,
        lookup_value,
    )

    assert result == ((uri,) if matches else ())


@pytest.mark.parametrize(
    ("source_id", "role", "prefix"),
    [
        ("ctgov", "outcome", "crosswalk/ctgov-outcomes/"),
        ("aea", "condition", "crosswalk/aea-conditions/"),
        ("aea", "outcome", "crosswalk/aea-outcomes/"),
        ("isrctn", "condition", "crosswalk/isrctn-states/"),
        ("isrctn", "outcome", "crosswalk/isrctn-outcomes/"),
        ("who-ictrp", "condition", "crosswalk/who-ictrp-conditions/"),
        ("who-ictrp", "outcome", "crosswalk/who-ictrp-outcomes/"),
    ],
)
def test_direct_state_map_queries_are_source_and_role_scoped(
    monkeypatch,
    source_id,
    role,
    prefix,
):
    observed: list[str] = []

    def select(query: str):
        observed.append(query)
        return [{"rawText": "Raw  value", "concept": STUNTING}]

    monkeypatch.setattr(query_v2_engine.v1, "sparql_select", select)

    result = query_v2_engine._load_source_direct_state_crosswalk(
        source_id,
        role,
    )

    expected_key = (
        "Raw  value"
        if source_id in {"ctgov", "who-ictrp"}
        else "Raw value"
    )
    assert result == {expected_key: (STUNTING,)}
    assert len(observed) == 1
    assert prefix in observed[0]
    assert "?concept a ue:State" in observed[0]


def test_graph_direct_state_prewarm_uses_one_query_and_caches_all_maps(
    monkeypatch,
):
    observed: list[str] = []

    def select(query: str):
        observed.append(query)
        rows = [
            {
                "entry": f"{prefix}fixture",
                "rawText": f"{source_id}  {role}",
                "concept": f"https://example.org/state/{source_id}/{role}",
            }
            for source_id, roles in (
                query_v2_engine._DIRECT_STATE_CROSSWALK_PREFIXES.items()
            )
            for role, prefixes in roles.items()
            for prefix in prefixes[:1]
        ]
        ctgov_row = next(
            row
            for row in rows
            if row["entry"].startswith(
                query_v2_engine.CTGOV_OUTCOME_CROSSWALK_PREFIX
            )
        )
        rows.extend(
            [
                dict(ctgov_row),
                {
                    **ctgov_row,
                    "concept": "https://example.org/state/ctgov/outcome-alt",
                },
                {
                    "entry": (
                        "https://universalevidence.com/crosswalk/"
                        "future-state/fixture"
                    ),
                    "rawText": "future raw value",
                    "concept": "https://example.org/state/future",
                },
            ]
        )
        return rows

    monkeypatch.setattr(query_v2_engine.v1, "sparql_select", select)
    monkeypatch.setattr(
        query_v2_engine,
        "_local_taxonomy_graph",
        lambda filename: filename,
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_PREWARMED_GRAPH_DIRECT_STATE_CROSSWALKS",
        {},
    )
    query_v2_engine._source_direct_state_crosswalk.cache_clear()
    query_v2_engine._isrctn_direct_state_crosswalk.cache_clear()
    try:
        query_v2_engine.prewarm_graph_direct_state_crosswalks()
        assert len(observed) == 1
        query = observed[0]
        graph_body = query.split(
            f"GRAPH <{query_v2_engine.v1.TAXONOMY_GRAPH_URI}> {{",
            1,
        )[1].rsplit("\n  }", 1)[0]
        assert "SELECT ?entry ?rawText ?concept" in query
        assert "VALUES" not in graph_body
        assert "STRSTARTS" not in graph_body
        assert "?entryPrefix" not in query
        assert "{ ?entry skos:exactMatch ?concept . }" in graph_body
        assert "{ ?entry skos:closeMatch ?concept . }" in graph_body
        assert all(
            prefix not in query
            for roles in query_v2_engine._DIRECT_STATE_CROSSWALK_PREFIXES.values()
            for prefixes in roles.values()
            for prefix in prefixes
        )
        assert (
            query_v2_engine._source_direct_state_crosswalk.cache_info().currsize
            == 7
        )

        cached = {}
        for source_id, roles in (
            query_v2_engine._DIRECT_STATE_CROSSWALK_PREFIXES.items()
        ):
            for role in roles:
                cached[(source_id, role)] = (
                    query_v2_engine._source_direct_state_crosswalk(
                        source_id,
                        role,
                    )
                )
        query_v2_engine._isrctn_direct_state_crosswalk("condition")
        query_v2_engine._isrctn_direct_state_crosswalk("outcome")

        assert len(observed) == 1
        assert cached[("ctgov", "outcome")] == {
            "ctgov  outcome": (
                "https://example.org/state/ctgov/outcome",
                "https://example.org/state/ctgov/outcome-alt",
            )
        }
        assert cached[("aea", "condition")] == {
            "aea condition": (
                "https://example.org/state/aea/condition",
            )
        }
        assert all(
            "future raw value" not in crosswalk
            for crosswalk in cached.values()
        )
    finally:
        query_v2_engine._source_direct_state_crosswalk.cache_clear()
        query_v2_engine._isrctn_direct_state_crosswalk.cache_clear()


def test_graph_direct_state_prewarm_rejects_ambiguous_prefix_routes(
    monkeypatch,
):
    monkeypatch.setattr(
        query_v2_engine,
        "_DIRECT_STATE_CROSSWALK_PREFIXES",
        {
            "ctgov": {
                "outcome": (
                    "https://universalevidence.com/crosswalk/",
                ),
            },
            "aea": {
                "condition": (
                    "https://universalevidence.com/crosswalk/aea-",
                ),
            },
        },
    )
    monkeypatch.setattr(
        query_v2_engine.v1,
        "sparql_select",
        lambda _query: [
            {
                "entry": (
                    "https://universalevidence.com/crosswalk/aea-fixture"
                ),
                "rawText": "ambiguous",
                "concept": STUNTING,
            }
        ],
    )

    with pytest.raises(
        query_v2_engine.SourceUnavailable,
        match="ambiguous source/role",
    ):
        query_v2_engine._load_graph_direct_state_crosswalks()


def test_graph_direct_state_prewarm_allows_alias_prefixes_for_same_route(
    monkeypatch,
):
    monkeypatch.setattr(
        query_v2_engine,
        "_DIRECT_STATE_CROSSWALK_PREFIXES",
        {
            "ctgov": {
                "outcome": (
                    "https://universalevidence.com/crosswalk/",
                    query_v2_engine.CTGOV_OUTCOME_CROSSWALK_PREFIX,
                ),
            },
        },
    )
    monkeypatch.setattr(
        query_v2_engine.v1,
        "sparql_select",
        lambda _query: [
            {
                "entry": (
                    f"{query_v2_engine.CTGOV_OUTCOME_CROSSWALK_PREFIX}fixture"
                ),
                "rawText": "same route",
                "concept": STUNTING,
            }
        ],
    )

    assert query_v2_engine._load_graph_direct_state_crosswalks() == {
        ("ctgov", "outcome"): {"same route": (STUNTING,)}
    }


def test_graph_direct_state_prewarm_validates_before_replacing_cache(
    monkeypatch,
):
    existing = {("ctgov", "outcome"): {"old": (STUNTING,)}}
    monkeypatch.setattr(
        query_v2_engine,
        "_PREWARMED_GRAPH_DIRECT_STATE_CROSSWALKS",
        existing,
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_local_taxonomy_graph",
        lambda filename: filename,
    )
    monkeypatch.setattr(
        query_v2_engine.v1,
        "sparql_select",
        lambda _query: [
            {
                "entry": (
                    f"{query_v2_engine.CTGOV_OUTCOME_CROSSWALK_PREFIX}fixture"
                ),
                "rawText": "new",
                "concept": WASTING,
            }
        ],
    )

    with pytest.raises(
        query_v2_engine.SourceUnavailable,
        match="aea/condition.*who-ictrp/outcome",
    ):
        query_v2_engine.prewarm_graph_direct_state_crosswalks()

    assert query_v2_engine._PREWARMED_GRAPH_DIRECT_STATE_CROSSWALKS is existing


def test_direct_state_map_safely_falls_back_before_bulk_prewarm(monkeypatch):
    loaded: list[tuple[str, str]] = []

    def load(source_id: str, role: str):
        loaded.append((source_id, role))
        return {"raw": (STUNTING,)}

    monkeypatch.setattr(
        query_v2_engine,
        "_PREWARMED_GRAPH_DIRECT_STATE_CROSSWALKS",
        {},
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_load_source_direct_state_crosswalk",
        load,
    )
    query_v2_engine._source_direct_state_crosswalk.cache_clear()
    try:
        assert query_v2_engine._source_direct_state_crosswalk(
            "ctgov", "outcome"
        ) == {"raw": (STUNTING,)}
        assert query_v2_engine._source_direct_state_crosswalk(
            "ctgov", "outcome"
        ) == {"raw": (STUNTING,)}
        assert loaded == [("ctgov", "outcome")]
    finally:
        query_v2_engine._source_direct_state_crosswalk.cache_clear()


def test_ctgov_graph_uses_bounded_direct_outcomes_without_legacy_loader(
    monkeypatch,
):
    observed_maps: list[tuple[str, str]] = []
    map_options: list[bool] = []

    def map_study(_raw, _iso, **kwargs):
        map_options.append(kwargs["include_outcome_concepts"])
        return {
            "source": "CT.gov",
            "nctId": "NCT-DIRECT-OUTCOME",
            "_intervention_names": ["Combination"],
            "condition_mesh_uris": [],
            "intervention_mesh_uris": ["mesh:must-not-run"],
            "outcomes": [{"measure": "Weight-for-height"}],
        }

    def legacy_loader_must_not_run():
        raise AssertionError("Graph must not synchronously load every outcome")

    def direct_outcomes(source_id, role):
        observed_maps.append((source_id, role))
        return {"Weight-for-height": (WASTING,)}

    def mesh_must_not_run(_mesh_uris):
        raise AssertionError("direct raw mappings must suppress MeSH fallback")

    async def resolve(_uri):
        return query_v2_engine.ConceptResolution(
            label="Malnutrition",
            terms=("Malnutrition",),
            mesh_uris=(),
        )

    monkeypatch.setattr(query_v2_engine.v1, "map_ctgov_study", map_study)
    monkeypatch.setattr(
        query_v2_engine.v1,
        "_load_ctgov_outcome_concept_map",
        legacy_loader_must_not_run,
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_source_direct_state_crosswalk",
        direct_outcomes,
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_bounded_ctgov_direct_outcome_crosswalk",
        lambda _measures: (_ for _ in ()).throw(
            AssertionError("legacy request-time lookup must not run")
        ),
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_source_direct_intervention_crosswalk",
        lambda source_id: {
            "Combination": (RUTF, CASH)
        }
        if source_id == "ctgov"
        else {},
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_local_state_labels",
        lambda _uris: {WASTING: "Wasting"},
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_local_intervention_labels",
        lambda _uris: {RUTF: "RUTF", CASH: "Cash transfer"},
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_local_interventions_by_mesh_multi",
        mesh_must_not_run,
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"studies": [{"fixture": True}]},
            request=request,
        )
    )
    adapter = CtgovAdapter(
        lambda: httpx.AsyncClient(transport=transport),
        concept_resolver=resolve,
    )

    rows, meta = asyncio.run(
        execute_graph_source(
            make_query(state=[MALNUTRITION]),
            adapter,
            default_region_index(),
        )
    )

    assert map_options == [False]
    assert observed_maps == [("ctgov", "outcome")]
    assert {
        (row["state_concept_uri"], row["intervention_concept_uri"])
        for row in rows
    } == {(WASTING, RUTF), (WASTING, CASH)}
    assert meta["status"] == "included"


def test_ctgov_graph_unmatched_label_uses_local_taxonomy_not_fuseki(
    monkeypatch,
):
    local_calls: list[tuple[str, ...]] = []

    def map_study(_raw, _iso, **_kwargs):
        return {
            "source": "CT.gov",
            "nctId": "NCT-LOCAL-LABEL",
            "_intervention_names": ["Shared local label"],
            "condition_mesh_uris": [],
            "intervention_mesh_uris": [],
            "outcomes": [],
        }

    def local_labels(names):
        local_calls.append(tuple(names))
        return {
            "Shared local label": (
                (RUTF, "Ready-to-use food"),
                (CASH, "Cash transfer"),
            )
        }

    async def resolve(_uri):
        return query_v2_engine.ConceptResolution(
            label="Stunting",
            terms=("Stunting",),
            mesh_uris=(),
        )

    monkeypatch.setattr(query_v2_engine.v1, "map_ctgov_study", map_study)
    monkeypatch.setattr(
        query_v2_engine,
        "_source_direct_intervention_crosswalk",
        lambda _source_id: {"Different label": (NUTRITION,)},
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_local_intervention_labels",
        lambda _uris: {NUTRITION: "Nutrition intervention"},
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_local_interventions_by_label_multi",
        local_labels,
    )
    monkeypatch.setattr(
        query_v2_engine.v1,
        "lookup_intervention_concepts_by_label",
        lambda _names: (_ for _ in ()).throw(
            AssertionError("Graph label fallback must not query Fuseki")
        ),
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"studies": [{"fixture": True}]},
            request=request,
        )
    )

    rows, meta = asyncio.run(
        execute_graph_source(
            make_query(state=[STUNTING]),
            CtgovAdapter(
                lambda: httpx.AsyncClient(transport=transport),
                concept_resolver=resolve,
            ),
            default_region_index(),
        )
    )

    assert local_calls == [("Shared local label",)]
    assert {row["intervention_concept_uri"] for row in rows} == {RUTF, CASH}
    assert all(not row.get("state_concept_uri") for row in rows)
    assert all(not row.get("condition_concept_uri") for row in rows)
    assert meta["status"] == "included"


def test_ctgov_graph_local_label_fallback_keeps_late_multi_name_target(
    monkeypatch,
):
    late_label = "zzzz tracked exact label"
    intervention_names = [
        f"Unmapped intervention {index:03d}"
        for index in range(query_v2_engine.UNIQUE_STUDY_LIMIT + 1)
    ] + [late_label]
    observed_names: list[tuple[str, ...]] = []

    def map_study(_raw, _iso, **_kwargs):
        return {
            "source": "CT.gov",
            "nctId": "NCT-MANY-NAMES",
            "_intervention_names": intervention_names,
            "condition_mesh_uris": [],
            "intervention_mesh_uris": [],
            "outcomes": [],
        }

    def local_labels(names):
        observed_names.append(tuple(names))
        return {late_label: ((RUTF, "Ready-to-use food"),)}

    async def resolve(_uri):
        return query_v2_engine.ConceptResolution(
            label="Stunting",
            terms=("Stunting",),
            mesh_uris=(),
        )

    monkeypatch.setattr(query_v2_engine.v1, "map_ctgov_study", map_study)
    monkeypatch.setattr(
        query_v2_engine,
        "_source_direct_intervention_crosswalk",
        lambda _source_id: {"Different label": (NUTRITION,)},
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_local_intervention_labels",
        lambda _uris: {NUTRITION: "Nutrition intervention"},
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_local_interventions_by_label_multi",
        local_labels,
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"studies": [{"fixture": True}]},
            request=request,
        )
    )

    rows, meta = asyncio.run(
        execute_graph_source(
            make_query(state=[STUNTING]),
            CtgovAdapter(
                lambda: httpx.AsyncClient(transport=transport),
                concept_resolver=resolve,
            ),
            default_region_index(),
        )
    )

    assert len(observed_names) == 1
    assert len(observed_names[0]) == query_v2_engine.UNIQUE_STUDY_LIMIT + 2
    assert observed_names[0][-1] == late_label
    assert [row["intervention_concept_uri"] for row in rows] == [RUTF]
    assert rows[0]["intervention"] == late_label
    assert meta["status"] == "included"


def test_ctgov_ordinary_search_keeps_legacy_label_lookup(monkeypatch):
    legacy_calls: list[tuple[str, ...]] = []

    def map_study(_raw, _iso, **_kwargs):
        return {
            "source": "CT.gov",
            "nctId": "NCT-SEARCH-LABEL",
            "_intervention_names": ["Legacy search label"],
            "condition_mesh_uris": [],
            "intervention_mesh_uris": [],
            "outcomes": [],
        }

    def legacy_lookup(names):
        legacy_calls.append(tuple(names))
        return {"Legacy search label": (RUTF, "Ready-to-use food")}

    monkeypatch.setattr(query_v2_engine.v1, "map_ctgov_study", map_study)
    monkeypatch.setattr(
        query_v2_engine.v1,
        "_load_ctgov_intervention_concept_map",
        lambda: {},
    )
    monkeypatch.setattr(
        query_v2_engine.v1,
        "lookup_intervention_concepts_by_label",
        legacy_lookup,
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_local_interventions_by_label_multi",
        lambda _names: (_ for _ in ()).throw(
            AssertionError("ordinary Search must retain its legacy lookup")
        ),
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"studies": [{"fixture": True}]},
            request=request,
        )
    )

    branch = asyncio.run(
        CtgovAdapter(
            lambda: httpx.AsyncClient(transport=transport)
        ).execute({}, SourceBudget(), default_region_index())
    )

    assert legacy_calls == [("Legacy search label",)]
    assert branch.rows[0]["intervention_concept_uri"] == RUTF


def test_ctgov_graph_fails_closed_when_direct_outcome_map_fails(monkeypatch):
    def map_study(_raw, _iso, **_kwargs):
        return {
            "source": "CT.gov",
            "nctId": "NCT-OUTCOME-FAILURE",
            "_intervention_names": [],
            "condition_mesh_uris": [],
            "intervention_mesh_uris": [],
            "outcomes": [{"measure": "Weight-for-height"}],
        }

    def unavailable(_source_id, _role):
        raise RuntimeError("Fuseki unavailable")

    async def resolve(_uri):
        return query_v2_engine.ConceptResolution(
            label="Malnutrition",
            terms=("Malnutrition",),
            mesh_uris=(),
        )

    monkeypatch.setattr(query_v2_engine.v1, "map_ctgov_study", map_study)
    monkeypatch.setattr(
        query_v2_engine,
        "_source_direct_state_crosswalk",
        unavailable,
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"studies": [{"fixture": True}]},
            request=request,
        )
    )
    rows, meta = asyncio.run(
        execute_graph_source(
            make_query(state=[MALNUTRITION]),
            CtgovAdapter(
                lambda: httpx.AsyncClient(transport=transport),
                concept_resolver=resolve,
            ),
            default_region_index(),
        )
    )

    assert rows == []
    assert meta["status"] == "unavailable"
    assert meta["reason"] == "upstream_unavailable"


def test_ctgov_graph_fails_closed_when_direct_intervention_map_fails(monkeypatch):
    def map_study(_raw, _iso, **_kwargs):
        return {
            "source": "CT.gov",
            "nctId": "NCT-INTERVENTION-FAILURE",
            "_intervention_names": ["Combination"],
            "condition_mesh_uris": [],
            "intervention_mesh_uris": [],
            "outcomes": [],
        }

    def unavailable(_source_id):
        raise RuntimeError("Fuseki unavailable")

    async def resolve(_uri):
        return query_v2_engine.ConceptResolution(
            label="Stunting",
            terms=("Stunting",),
            mesh_uris=(),
        )

    monkeypatch.setattr(query_v2_engine.v1, "map_ctgov_study", map_study)
    monkeypatch.setattr(
        query_v2_engine,
        "_source_direct_intervention_crosswalk",
        unavailable,
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"studies": [{"fixture": True}]},
            request=request,
        )
    )
    rows, meta = asyncio.run(
        execute_graph_source(
            make_query(state=[STUNTING]),
            CtgovAdapter(
                lambda: httpx.AsyncClient(transport=transport),
                concept_resolver=resolve,
            ),
            default_region_index(),
        )
    )

    assert rows == []
    assert meta["status"] == "unavailable"
    assert meta["reason"] == "upstream_unavailable"


def test_ctgov_graph_fails_closed_when_required_mesh_fallback_fails(monkeypatch):
    def map_study(_raw, _iso, **_kwargs):
        return {
            "source": "CT.gov",
            "nctId": "NCT-MESH-FAILURE",
            "_intervention_names": ["Unmapped program"],
            "condition_mesh_uris": [],
            "intervention_mesh_uris": ["http://id.nlm.nih.gov/mesh/D000001"],
            "outcomes": [],
        }

    def unavailable(_mesh_uris):
        raise RuntimeError("intervention taxonomy unavailable")

    async def resolve(_uri):
        return query_v2_engine.ConceptResolution(
            label="Stunting",
            terms=("Stunting",),
            mesh_uris=(),
        )

    monkeypatch.setattr(query_v2_engine.v1, "map_ctgov_study", map_study)
    monkeypatch.setattr(
        query_v2_engine,
        "_source_direct_intervention_crosswalk",
        lambda _source_id: {"Different program": (RUTF,)},
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_local_interventions_by_label_multi",
        lambda _names: {},
    )
    monkeypatch.setattr(
        query_v2_engine.v1,
        "lookup_intervention_concepts_by_label",
        lambda _names: (_ for _ in ()).throw(
            AssertionError("Graph label fallback must stay local")
        ),
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_local_interventions_by_mesh_multi",
        unavailable,
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"studies": [{"fixture": True}]},
            request=request,
        )
    )
    rows, meta = asyncio.run(
        execute_graph_source(
            make_query(state=[STUNTING]),
            CtgovAdapter(
                lambda: httpx.AsyncClient(transport=transport),
                concept_resolver=resolve,
            ),
            default_region_index(),
        )
    )

    assert rows == []
    assert meta["status"] == "unavailable"
    assert meta["reason"] == "upstream_unavailable"


def test_graph_attribution_does_not_add_work_to_ordinary_ctgov_search(monkeypatch):
    raw_study = {"protocolSection": {"identificationModule": {"nctId": "NCT-GRAPH"}}}
    calls = {"upstream": 0, "condition_enrichment": 0, "intervention_enrichment": 0}

    def map_study(_raw, _iso, **_kwargs):
        return {
            "source": "CT.gov",
            "nctId": "NCT-GRAPH",
            "_intervention_names": ["RUTF"],
            "condition_mesh_uris": ["mesh:child-malnutrition"],
            "intervention_mesh_uris": [],
            "outcomes": [],
        }

    def condition_enrichment(_mesh_uris):
        calls["condition_enrichment"] += 1
        return {"mesh:child-malnutrition": (CHILD_MALNUTRITION, "Child malnutrition")}

    def intervention_enrichment(source_id):
        calls["intervention_enrichment"] += 1
        assert source_id == "ctgov"
        return {"RUTF": (RUTF,)}

    async def resolve(uri):
        label = "Malnutrition" if uri == MALNUTRITION else "Nutrition intervention"
        terms = (label,) if uri == MALNUTRITION else ("RUTF",)
        return query_v2_engine.ConceptResolution(label=label, terms=terms, mesh_uris=())

    def response(request):
        calls["upstream"] += 1
        return httpx.Response(200, json={"studies": [raw_study]}, request=request)

    monkeypatch.setattr(query_v2_engine.v1, "map_ctgov_study", map_study)
    monkeypatch.setattr(
        query_v2_engine,
        "_local_states_by_mesh_multi",
        condition_enrichment,
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_source_direct_intervention_crosswalk",
        intervention_enrichment,
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_local_intervention_labels",
        lambda _uris: {
            RUTF: "Ready-to-use therapeutic/supplementary food"
        },
    )
    adapter = CtgovAdapter(
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(response)),
        concept_resolver=resolve,
    )
    spec = {"condition": MALNUTRITION, "intervention": NUTRITIONAL_SUPPORT}

    ordinary = asyncio.run(
        adapter.execute(spec, SourceBudget(), default_region_index())
    )
    assert calls == {
        "upstream": 1,
        "condition_enrichment": 0,
        "intervention_enrichment": 0,
    }
    assert ordinary.rows[0]["condition_concept_uri"] == MALNUTRITION
    assert ordinary.rows[0]["intervention_concept_uri"] == NUTRITIONAL_SUPPORT

    graph_rows, _meta = asyncio.run(
        execute_graph_source(
            make_query(condition=[MALNUTRITION], intervention=[NUTRITIONAL_SUPPORT]),
            adapter,
            default_region_index(),
        )
    )
    assert calls == {
        "upstream": 2,
        "condition_enrichment": 1,
        "intervention_enrichment": 1,
    }
    assert [
        (row["condition_concept_uri"], row["intervention_concept_uri"])
        for row in graph_rows
    ] == [(CHILD_MALNUTRITION, RUTF)]


def test_ctgov_condition_enrichment_uses_tracked_states_taxonomy():
    assert query_v2_engine._local_states_by_mesh(
        ["http://id.nlm.nih.gov/mesh/D008288"]
    ) == {
        "http://id.nlm.nih.gov/mesh/D008288": (MALARIA, "Malaria")
    }


def test_ctgov_adapter_caps_unique_studies_then_expands_interventions(monkeypatch):
    raw_studies = [
        {"protocolSection": {"identificationModule": {"nctId": f"NCT{index:04d}"}}}
        for index in range(101)
    ]

    def map_study(raw, _iso, **_kwargs):
        study_id = raw["protocolSection"]["identificationModule"]["nctId"]
        index = int(study_id.removeprefix("NCT"))
        return {
            "source": "CT.gov",
            "nctId": study_id,
            "title": study_id,
            "url": f"https://clinicaltrials.gov/study/{study_id}",
            "intervention": "A",
            "_intervention_names": ["A", "B", "C"],
            "condition_mesh_uris": [
                f"http://id.nlm.nih.gov/mesh/D{index:06d}"
            ],
            "intervention_mesh_uris": [],
            "country": "",
            "_location_region_uris": [],
            "status": "completed",
            "year": 2026,
            "outcomes": [],
        }

    monkeypatch.setattr(query_v2_engine.v1, "map_ctgov_study", map_study)
    monkeypatch.setattr(query_v2_engine.v1, "get_ctgov_regions_xwalk", lambda: {})
    monkeypatch.setattr(query_v2_engine.v1, "_load_ctgov_intervention_concept_map", lambda: {})
    monkeypatch.setattr(query_v2_engine.v1, "_load_ctgov_outcome_concept_map", lambda: {})
    looked_up = []
    monkeypatch.setattr(
        query_v2_engine,
        "_local_states_by_mesh",
        lambda mesh_uris: looked_up.extend(mesh_uris) or {},
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"studies": raw_studies}, request=request)
    )
    adapter = CtgovAdapter(lambda: httpx.AsyncClient(transport=transport))

    branch = asyncio.run(
        adapter.execute({"region": WORLD}, SourceBudget(), default_region_index())
    )

    assert branch.truncated is True
    assert len({row["study_id"] for row in branch.rows}) == 100
    assert len(branch.rows) == 300
    assert len(looked_up) == 100
    assert "http://id.nlm.nih.gov/mesh/D000100" not in looked_up
    assert "LocationGeoPoint" in CTGOV_V2_FIELDS
    assert "PrimaryOutcomeMeasure" in CTGOV_V2_FIELDS
    assert "Results" not in CTGOV_V2_FIELDS


def test_ctgov_v2_mapping_skips_unbounded_outcome_crosswalk_enrichment(monkeypatch):
    def fail_if_loaded():
        raise AssertionError("v2 must not load the legacy whole-outcome crosswalk")

    monkeypatch.setattr(
        query_v2_engine.v1,
        "_load_ctgov_outcome_concept_map",
        fail_if_loaded,
    )
    study = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT-OUTCOME"},
            "outcomesModule": {
                "primaryOutcomes": [{"measure": "Weight-for-age"}]
            },
        }
    }

    mapped = query_v2_engine.v1.map_ctgov_study(
        study,
        "",
        include_region_evidence=False,
        include_outcome_concepts=False,
    )

    assert mapped is not None
    assert mapped["outcomes"][0]["measure"] == "Weight-for-age"
    assert "state_concept_uri" not in mapped["outcomes"][0]


def test_isrctn_adm1_requires_location_country_crosswalk_confirmation(monkeypatch):
    async def fake_fetch(*_args):
        return [
            {
                **result("ISRCTN", "ISRCTN-CONFIRMED"),
                "countries": ["Bangladesh"],
                "country_iso_alpha2": ["BD"],
                "study_locations": ["Chittagong"],
            },
            {
                **result("ISRCTN", "ISRCTN-COUNTRY-ONLY"),
                "countries": ["Bangladesh"],
                "country_iso_alpha2": ["BD"],
                "study_locations": [],
            },
        ]

    monkeypatch.setattr(
        query_v2_engine,
        "_isrctn_region_uris",
        lambda row: [CHITTAGONG, BANGLADESH]
        if row["study_id"].endswith("CONFIRMED")
        else [BANGLADESH],
    )
    monkeypatch.setattr(query_v2_engine.v1, "get_isrctn_conditions_xwalk", lambda: {})
    monkeypatch.setattr(query_v2_engine, "_isrctn_direct_intervention_crosswalk", lambda: {})
    monkeypatch.setattr(query_v2_engine.v1, "get_isrctn_regions_xwalk", lambda: {})
    adapter = IsrctnAdapter(
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, request=request)
            )
        ),
        fetch_batch=fake_fetch,
    )

    branch = asyncio.run(
        adapter.execute(
            {"region": CHITTAGONG}, SourceBudget(), default_region_index()
        )
    )

    assert {row["study_id"] for row in branch.rows} == {"ISRCTN-CONFIRMED"}


def test_isrctn_region_only_enriches_condition_descriptions(monkeypatch):
    async def fake_fetch(*_args):
        return [
            {
                **result("ISRCTN", "ISRCTN-MALARIA"),
                "condition_descriptions": ["Malaria"],
                "countries": [],
                "country_iso_alpha2": [],
            }
        ]

    monkeypatch.setattr(
        query_v2_engine.v1,
        "get_isrctn_conditions_xwalk",
        lambda: {"Malaria": MALARIA},
    )
    adapter = IsrctnAdapter(
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, request=request)
            )
        ),
        fetch_batch=fake_fetch,
    )

    branch = asyncio.run(
        adapter.execute({"region": WORLD}, SourceBudget(), default_region_index())
    )

    assert branch.rows[0]["condition_concept_uri"] == MALARIA
    assert branch.rows[0]["condition_concept"] == "Malaria"


def test_isrctn_condition_only_uses_intervention_crosswalk(monkeypatch):
    async def fake_fetch(*_args):
        return [
            {
                **result("ISRCTN", "ISRCTN-RUTF", "RUTF"),
                "condition_descriptions": ["Severe acute malnutrition"],
                "intervention_descriptions": ["RUTF"],
                "drug_names_list": [],
                "countries": [],
                "country_iso_alpha2": [],
            }
        ]

    monkeypatch.setattr(
        query_v2_engine,
        "_isrctn_direct_intervention_crosswalk",
        lambda: {"RUTF": (RUTF,)},
    )
    monkeypatch.setattr(
        query_v2_engine,
        "_local_intervention_labels",
        lambda uris: {
            RUTF: "Ready-to-use therapeutic/supplementary food"
        }
        if RUTF in uris
        else {},
    )
    adapter = IsrctnAdapter(
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, request=request)
            )
        ),
        fetch_batch=fake_fetch,
    )

    branch = asyncio.run(
        adapter.execute(
            {"condition": MALNUTRITION},
            SourceBudget(),
            default_region_index(),
        )
    )

    assert branch.rows[0]["intervention_concept_uri"] == RUTF
    assert (
        branch.rows[0]["intervention_concept"]
        == "Ready-to-use therapeutic/supplementary food"
    )


def test_isrctn_stamping_prefers_specific_graph_coordinates_under_selected_roots():
    rows = query_v2_engine._stamp_isrctn(
        [
            {
                **result("ISRCTN", "ISRCTN-SPECIFIC", "RUTF"),
                "condition_descriptions": ["Child malnutrition"],
                "intervention_descriptions": ["RUTF"],
                "drug_names_list": [],
            }
        ],
        {"condition": MALNUTRITION, "intervention": NUTRITIONAL_SUPPORT},
        {"condition": "Malnutrition", "intervention": "Nutritional support"},
        {
            query_v2_engine.v1._normalize_xwalk_key("Child malnutrition"): CHILD_MALNUTRITION
        },
        {CHILD_MALNUTRITION: "Child malnutrition"},
        {query_v2_engine.v1._normalize_xwalk_key("RUTF"): RUTF},
        {RUTF: "Ready-to-use therapeutic/supplementary food"},
        specific_attribution=True,
    )

    assert [(row["condition_concept_uri"], row["intervention_concept_uri"]) for row in rows] == [
        (CHILD_MALNUTRITION, RUTF)
    ]


def test_isrctn_search_uses_direct_intervention_mapping_under_selected_root():
    rows = query_v2_engine._stamp_isrctn(
        [
            {
                **result("ISRCTN", "ISRCTN-SEARCH", "RUTF"),
                "condition_descriptions": ["Child malnutrition"],
                "intervention_descriptions": ["RUTF"],
                "drug_names_list": [],
            }
        ],
        {"condition": MALNUTRITION, "intervention": NUTRITION},
        {"condition": "Malnutrition", "intervention": "Nutrition intervention"},
        {
            query_v2_engine.v1._normalize_xwalk_key("Child malnutrition"): CHILD_MALNUTRITION
        },
        {CHILD_MALNUTRITION: "Child malnutrition"},
        {query_v2_engine.v1._normalize_xwalk_key("RUTF"): RUTF},
        {RUTF: "Ready-to-use therapeutic/supplementary food"},
    )

    assert [(row["condition_concept_uri"], row["intervention_concept_uri"]) for row in rows] == [
        (MALNUTRITION, RUTF)
    ]


def test_isrctn_uses_recruitment_country_query_constraint():
    calls = []

    async def fake_fetch(*args):
        calls.append(args)
        return []

    adapter = IsrctnAdapter(
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, request=request)
            )
        ),
        fetch_batch=fake_fetch,
    )

    asyncio.run(
        adapter.execute(
            {"region": BANGLADESH}, SourceBudget(), default_region_index()
        )
    )

    assert len(calls) == 1
    assert calls[0][2] == "(recruitmentCountry: Bangladesh)"
    assert len(calls[0]) == 4


def test_isrctn_country_verification_normalizes_raw_source_label():
    async def fake_fetch(*_args):
        return [
            {
                **result("ISRCTN", "ISRCTN-BD"),
                "countries": ["Bangladesh"],
                "country_iso_alpha2": [],
            }
        ]

    adapter = IsrctnAdapter(
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, request=request)
            )
        ),
        fetch_batch=fake_fetch,
    )

    branch = asyncio.run(
        adapter.execute(
            {"region": BANGLADESH}, SourceBudget(), default_region_index()
        )
    )

    assert [row["study_id"] for row in branch.rows] == ["ISRCTN-BD"]


def test_local_adapters_select_101_study_ids_before_outer_row_expansion():
    regions = default_region_index()
    for source in ("aea", "who-ictrp"):
        selector = _local_selector_query(
            source,
            {"condition": MALARIA, "region": BANGLADESH},
            regions,
        )
        detail = _local_detail_query(
            source,
            ["https://example.org/study/one"],
            {"condition": MALARIA},
        )
        assert "LIMIT 101" in selector
        assert "?study a <" in selector
        assert f"<{query_v2_engine.v1._AXIS_MATCH_PRED['condition']}> <{MALARIA}>" in selector
        assert "VALUES ?study" in detail
        assert "LIMIT 101" not in detail


def test_country_only_sources_are_explicitly_excluded_for_adm1():
    calls: dict[str, list[dict[str, str]]] = {source: [] for source in ("ctgov", "aea", "isrctn", "who-ictrp")}
    adapters = {
        source: FakeAdapter(source, {}, calls[source])
        for source in calls
    }

    execution = asyncio.run(
        execute_query_v2(make_query(region=[CHITTAGONG]), adapters=adapters)
    )
    sources = execution.payload["meta"]["sources"]

    assert sources["aea"] == {
        "status": "excluded",
        "coverage": "country_only",
        "returned_unique_studies": 0,
        "truncated": False,
        "approximate": False,
        "reason": "unsupported_admin_level",
    }
    assert sources["who-ictrp"] == sources["aea"]
    assert calls["aea"] == []
    assert calls["who-ictrp"] == []
    assert len(calls["ctgov"]) == len(calls["isrctn"]) == 1


def test_country_only_source_keeps_supported_disjunct_of_mixed_region_or():
    kenya_spec = tuple(sorted({"region": KENYA}.items()))
    adapter = FakeAdapter(
        "aea", {kenya_spec: [result("AEA", "AEA-KENYA")]}, []
    )
    query = canonical_query(
        {"region": [CHITTAGONG, KENYA]}, {"region": "or"}
    )

    rows, meta = asyncio.run(
        execute_source(query, adapter, default_region_index())
    )

    assert [row["study_id"] for row in rows] == ["AEA-KENYA"]
    assert adapter.calls == [{"region": KENYA}]
    assert meta["status"] == "included"
    assert meta["coverage"] == "country_only"
    assert meta["reason"] == "unsupported_admin_level"
    assert meta["truncated"] is meta["approximate"] is True


def test_study_limit_precedes_presentation_expansion_and_has_no_global_cap():
    raw_rows = [
        {"study": f"urn:study:{index:03d}", "intervention": intervention}
        for index in range(101)
        for intervention in ("A", "B", "C")
    ]

    selected, truncated = _limit_selected_studies(raw_rows)

    assert truncated is True
    assert len({row["study"] for row in selected}) == 100
    assert len(selected) == 300

    adapters = {}
    for source, label in (
        ("ctgov", "CT.gov"),
        ("aea", "AEA"),
        ("isrctn", "ISRCTN"),
        ("who-ictrp", "WHO ICTRP"),
    ):
        rows = [result(label, f"{source}-{index}") for index in range(100)]
        spec_key = tuple(sorted({"condition": MALARIA}.items()))
        adapters[source] = FakeAdapter(source, {spec_key: rows}, [])
    execution = asyncio.run(execute_query_v2(make_query(condition=[MALARIA]), adapters=adapters))

    assert execution.payload["meta"]["returned_unique_studies"] == 400
    assert len(execution.payload["results"]) == 400


def test_budget_exhaustion_preserves_sound_single_branch_subset_and_metadata():
    spec_key = tuple(sorted({"condition": MALARIA}.items()))
    adapter = FakeAdapter(
        "ctgov",
        {spec_key: [result("CT.gov", "NCT-SOUND")]},
        [],
        exhaust_after=1,
    )

    rows, meta = asyncio.run(
        execute_source(make_query(condition=[MALARIA]), adapter, default_region_index())
    )

    assert [row["study_id"] for row in rows] == ["NCT-SOUND"]
    assert meta["truncated"] is True
    assert meta["approximate"] is True
    assert meta["reason"] == "budget_exhausted"


def test_planner_never_starts_more_than_eight_source_branches():
    conditions = [
        f"https://universalevidence.com/vocab/states/Condition{index}"
        for index in range(9)
    ]
    rows = {
        tuple(sorted({"condition": uri}.items())): [
            result("CT.gov", f"NCT-{index}")
        ]
        for index, uri in enumerate(sorted(conditions))
    }
    adapter = FakeAdapter("ctgov", rows, [])
    query = canonical_query({"condition": conditions}, {"condition": "or"})

    selected, meta = asyncio.run(
        execute_source(query, adapter, default_region_index())
    )

    assert len(adapter.calls) == 8
    assert len(selected) == 8
    assert meta["truncated"] is True
    assert meta["approximate"] is True
    assert meta["reason"] == "budget_exhausted"


def test_missing_region_assets_return_source_metadata_not_http_500():
    empty_regions = RegionIndex(Graph())
    adapters = {
        source: FakeAdapter(source, {}, [])
        for source in ("ctgov", "aea", "isrctn", "who-ictrp")
    }

    execution = asyncio.run(
        execute_query_v2(
            make_query(region=[CHITTAGONG]),
            adapters=adapters,
            regions=empty_regions,
        )
    )

    assert execution.payload["results"] == []
    assert {
        meta["status"] for meta in execution.payload["meta"]["sources"].values()
    } == {"unavailable"}
    assert {
        meta["reason"] for meta in execution.payload["meta"]["sources"].values()
    } == {"upstream_unavailable"}


def test_query_v2_http_contract_uses_repeated_params_and_keeps_v1_shape(monkeypatch):
    payload = {
        "results": [result("CT.gov", "NCT1")],
        "meta": {
            "api_version": "query-v2",
            "returned_unique_studies": 1,
            "limit_per_source_branch": 100,
            "truncated": False,
            "approximate": False,
            "sources": {},
        },
    }
    observed: list[CanonicalQuery] = []

    async def fake_get_or_execute(query, execute):
        observed.append(query)
        return payload

    monkeypatch.setattr(query_v2_route.query_v2_cache, "get_or_execute", fake_get_or_execute)
    client = TestClient(api_main.app)
    response = client.get(
        "/query/v2",
        params=[
            ("condition", MALARIA),
            ("condition", MALNUTRITION),
            ("condition", MALARIA),
            ("condition_logic", "and"),
        ],
    )

    assert response.status_code == 200
    assert response.json() == payload
    assert observed[0].values["condition"] == tuple(sorted((MALARIA, MALNUTRITION)))
    assert observed[0].logic["condition"] == "and"

    invalid = client.get("/query/v2", params={"condition": "Malaria"})
    missing = client.get("/query/v2")
    assert invalid.status_code == 422
    assert missing.status_code == 422


def test_v2_cache_is_single_flight_and_does_not_use_v1_namespace():
    cache = QueryV2Cache(populated_ttl=60)
    query = make_query(condition=[MALARIA])
    calls = 0

    async def execute(_query):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return QueryV2Execution(
            {
                "results": [result("CT.gov", "NCT1")],
                "meta": {
                    "api_version": "query-v2",
                    "returned_unique_studies": 1,
                    "limit_per_source_branch": 100,
                    "truncated": False,
                    "approximate": False,
                    "sources": {
                        source: {
                            "status": "included",
                            "coverage": "full",
                            "returned_unique_studies": 0,
                            "truncated": False,
                            "approximate": False,
                            "reason": None,
                        }
                        for source in ("ctgov", "aea", "isrctn", "who-ictrp")
                    },
                },
            }
        )

    async def run():
        return await asyncio.gather(
            *(cache.get_or_execute(query, execute) for _ in range(20))
        )

    responses = asyncio.run(run())
    assert calls == 1
    assert all(response == responses[0] for response in responses)
    assert cache.stats()["coalesced_callers"] == 19
    assert cache.stats()["backend_executions_avoided"] == 19
    assert "query-results-v2-gzip" not in cache.canonical_key(query)


def test_stable_approximation_is_cacheable_but_transient_partial_is_not():
    def execution(reason=None, status="included"):
        return QueryV2Execution(
            {
                "results": [result("CT.gov", "NCT1")],
                "meta": {
                    "approximate": True,
                    "sources": {
                        source: {
                            "status": status if source == "ctgov" else "included",
                            "reason": reason if source == "ctgov" else None,
                        }
                        for source in SOURCE_IDS
                    },
                },
            }
        )

    assert execution().cacheable is True
    assert execution("unsupported_admin_level", "excluded").cacheable is True
    assert execution("budget_exhausted").cacheable is False
    assert execution("upstream_unavailable", "unavailable").cacheable is False


def test_runtime_response_shape_matches_frozen_required_metadata():
    response_schema = json.loads(
        (Path(__file__).parents[1] / "contracts" / "query-v2" / "response.schema.json").read_text()
    )
    required_meta = set(response_schema["$defs"]["queryMeta"]["required"])
    required_source = set(response_schema["$defs"]["sourceMeta"]["required"])
    adapters = {
        source: FakeAdapter(source, {}, [])
        for source in ("ctgov", "aea", "isrctn", "who-ictrp")
    }
    payload = asyncio.run(
        execute_query_v2(make_query(region=[WORLD]), adapters=adapters)
    ).payload

    assert set(payload) == {"results", "meta"}
    assert required_meta <= set(payload["meta"])
    assert set(payload["meta"]["sources"]) == {
        "ctgov", "aea", "isrctn", "who-ictrp"
    }
    assert all(
        required_source <= set(meta)
        for meta in payload["meta"]["sources"].values()
    )
