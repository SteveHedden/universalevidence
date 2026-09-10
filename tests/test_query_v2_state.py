from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Mapping

import httpx
from fastapi.testclient import TestClient

import api.main as api_main
from api.query_v2_cache import QueryV2Cache
import api.routes.query_v2 as query_v2_route
from api.routes.query_v2 import build_query_v2_request
import scripts.query_v2 as engine


STUNTING = "https://universalevidence.com/vocab/states/Stunting"
WASTING = "https://universalevidence.com/vocab/states/Wasting"
NUTRITION = (
    "https://universalevidence.com/vocab/interventions/NutritionIntervention"
)
BANGLADESH = "https://sws.geonames.org/1210997/"
AEA_OUTCOME_ONLY = (
    "https://universalevidence.com/ontology/Evidence_AEA_AEARCTR-0003248"
)


def result(source: str, study_id: str) -> dict[str, Any]:
    return {
        "source": source,
        "study_id": study_id,
        "intervention": None,
        "outcomes": [],
    }


@dataclass
class FakeAdapter:
    source_id: str
    rows_for_spec: Mapping[tuple[tuple[str, str], ...], list[dict[str, Any]]]
    calls: list[dict[str, str]]

    async def execute(self, spec, budget, regions):
        budget.claim_request()
        self.calls.append(dict(spec))
        return engine.BranchResult(
            rows=[
                dict(row)
                for row in self.rows_for_spec.get(tuple(sorted(spec.items())), [])
            ]
        )


def state_query(values: list[str], logic: str = "or") -> engine.CanonicalQuery:
    return engine.canonical_query({"state": values}, {"state": logic})


def test_state_is_first_class_in_canonical_identity_and_cache_keys():
    state = state_query([STUNTING, WASTING, STUNTING], "and")
    condition = engine.canonical_query(
        {"condition": [STUNTING, WASTING]}, {"condition": "and"}
    )

    assert state.values["state"] == tuple(sorted((STUNTING, WASTING)))
    assert state.logic["state"] == "and"
    assert state.identity()["values"]["state"] == list(
        sorted((STUNTING, WASTING))
    )
    assert QueryV2Cache.canonical_key(state) != QueryV2Cache.canonical_key(
        condition
    )


def test_route_contract_keeps_state_condition_and_outcome_distinct():
    query = build_query_v2_request(
        state=[STUNTING],
        condition=[WASTING],
        intervention=[],
        outcome=[STUNTING],
        region=[],
        state_logic="and",
        condition_logic="or",
        intervention_logic="or",
        outcome_logic="and",
        region_logic="or",
    )

    assert query.values["state"] == (STUNTING,)
    assert query.values["condition"] == (WASTING,)
    assert query.values["outcome"] == (STUNTING,)
    assert query.logic["state"] == query.logic["outcome"] == "and"


def test_http_route_accepts_repeated_state_and_state_logic(monkeypatch):
    observed: list[engine.CanonicalQuery] = []

    async def fake_get_or_execute(query, _execute):
        observed.append(query)
        return {
            "results": [],
            "meta": {
                "api_version": "query-v2",
                "returned_unique_studies": 0,
                "limit_per_source_branch": 100,
                "truncated": False,
                "approximate": False,
                "sources": {},
            },
        }

    monkeypatch.setattr(
        query_v2_route.query_v2_cache,
        "get_or_execute",
        fake_get_or_execute,
    )
    response = TestClient(api_main.app).get(
        "/query/v2",
        params=[
            ("state", STUNTING),
            ("state", WASTING),
            ("state_logic", "and"),
        ],
    )

    assert response.status_code == 200
    assert observed[0].values["state"] == tuple(sorted((STUNTING, WASTING)))
    assert observed[0].logic["state"] == "and"


def test_local_selector_unions_state_roles_before_limit_and_keeps_shared_filters():
    query = engine._local_selector_query(
        "aea",
        {
            "state": STUNTING,
            "intervention": NUTRITION,
            "region": BANGLADESH,
        },
        engine.default_region_index(),
    )

    condition_predicate = engine.v1._AXIS_MATCH_PRED["condition"]
    outcome_predicate = engine.v1._AXIS_MATCH_PRED["outcome"]
    intervention_predicate = engine.v1._AXIS_MATCH_PRED["intervention"]
    assert (
        f"?study (<{condition_predicate}>|<{outcome_predicate}>) <{STUNTING}>"
        in query
    )
    assert f"?study <{intervention_predicate}> <{NUTRITION}>" in query
    assert "?region_entry" in query
    assert "LIMIT 101" in query

    who_query = engine._local_selector_query(
        "who-ictrp",
        {
            "state": STUNTING,
            "intervention": NUTRITION,
            "region": BANGLADESH,
        },
        engine.default_region_index(),
    )
    assert (
        f"?study (<{condition_predicate}>|<{outcome_predicate}>) <{STUNTING}>"
        in who_query
    )
    assert f"?study <{intervention_predicate}> <{NUTRITION}>" in who_query
    assert "countryIsoAlpha2" in who_query
    assert "LIMIT 101" in who_query


def test_explicit_local_condition_and_outcome_remain_role_strict():
    regions = engine.default_region_index()
    condition_query = engine._local_selector_query(
        "aea", {"condition": STUNTING}, regions
    )
    outcome_query = engine._local_selector_query(
        "aea", {"outcome": STUNTING}, regions
    )

    condition_predicate = engine.v1._AXIS_MATCH_PRED["condition"]
    outcome_predicate = engine.v1._AXIS_MATCH_PRED["outcome"]
    assert f"?study <{condition_predicate}> <{STUNTING}>" in condition_query
    assert f"?study <{outcome_predicate}> <{STUNTING}>" not in condition_query
    assert f"?study <{outcome_predicate}> <{STUNTING}>" in outcome_query
    assert f"?study <{condition_predicate}> <{STUNTING}>" not in outcome_query


def test_outcome_only_aea_fixture_is_returned_by_unified_state():
    calls: list[str] = []

    async def select(query: str, budget: engine.SourceBudget):
        budget.claim_request()
        calls.append(query)
        if "SELECT DISTINCT ?study WHERE" in query:
            return [{"study": AEA_OUTCOME_ONLY}]
        return [
            {
                "study": AEA_OUTCOME_ONLY,
                "source": "AEA",
                "study_id": "AEARCTR-0003248",
                "title": "GroMoTo",
                "outcomeUri": STUNTING,
                "outcomeLabel": "Stunting",
            }
        ]

    adapter = engine.LocalSourceAdapter("aea", select=select)
    branch = asyncio.run(
        adapter.execute(
            {"state": STUNTING},
            engine.SourceBudget(),
            engine.default_region_index(),
        )
    )

    assert [row["study_id"] for row in branch.rows] == ["AEARCTR-0003248"]
    assert len(calls) == 2
    assert engine.v1._AXIS_MATCH_PRED["outcome"] in calls[0]


def test_local_state_cap_is_applied_after_role_union():
    study_uris = [f"https://example.org/aea/{index:03d}" for index in range(101)]

    async def select(query: str, budget: engine.SourceBudget):
        budget.claim_request()
        if "SELECT DISTINCT ?study WHERE" in query:
            return [{"study": uri} for uri in study_uris]
        return [
            {
                "study": uri,
                "source": "AEA",
                "study_id": uri.rsplit("/", 1)[-1],
                "title": uri,
            }
            for uri in study_uris[: engine.UNIQUE_STUDY_LIMIT]
        ]

    branch = asyncio.run(
        engine.LocalSourceAdapter("aea", select=select).execute(
            {"state": STUNTING},
            engine.SourceBudget(),
            engine.default_region_index(),
        )
    )

    assert branch.truncated is True
    assert len({row["study_id"] for row in branch.rows}) == 100


def test_multiple_states_apply_or_union_and_and_intersection_with_deduplication():
    rows = {
        (("state", STUNTING),): [
            result("AEA", "CONDITION-ONLY"),
            result("AEA", "BOTH-STATES"),
        ],
        (("state", WASTING),): [
            result("AEA", "OUTCOME-ONLY"),
            result("AEA", "BOTH-STATES"),
        ],
    }

    async def run(logic: str):
        adapter = FakeAdapter("aea", rows, [])
        selected, meta = await engine.execute_source(
            state_query([STUNTING, WASTING], logic),
            adapter,
            engine.default_region_index(),
        )
        return selected, meta, adapter.calls

    or_rows, _, or_calls = asyncio.run(run("or"))
    and_rows, _, and_calls = asyncio.run(run("and"))

    assert {row["study_id"] for row in or_rows} == {
        "CONDITION-ONLY",
        "OUTCOME-ONLY",
        "BOTH-STATES",
    }
    assert sum(row["study_id"] == "BOTH-STATES" for row in or_rows) == 1
    assert [row["study_id"] for row in and_rows] == ["BOTH-STATES"]
    assert or_calls == and_calls == [
        {"state": STUNTING},
        {"state": WASTING},
    ]


def test_state_planner_pushes_intervention_and_region_into_every_role_union_branch():
    query = engine.canonical_query(
        {
            "state": [STUNTING, WASTING],
            "intervention": [NUTRITION],
            "region": [BANGLADESH],
        },
        {"state": "or", "intervention": "or", "region": "or"},
    )
    clauses = engine.plan_query(query)

    assert [clause.specs for clause in clauses] == [
        (
            {
                "state": STUNTING,
                "intervention": NUTRITION,
                "region": BANGLADESH,
            },
        ),
        (
            {
                "state": WASTING,
                "intervention": NUTRITION,
                "region": BANGLADESH,
            },
        ),
    ]


def test_state_deduplication_uses_source_qualified_study_ids():
    spec = (("state", STUNTING),)
    adapters = {
        "ctgov": FakeAdapter(
            "ctgov", {spec: [result("CT.gov", "SHARED-ID")]}, []
        ),
        "aea": FakeAdapter("aea", {spec: [result("AEA", "SHARED-ID")]}, []),
        "isrctn": FakeAdapter("isrctn", {}, []),
        "who-ictrp": FakeAdapter("who-ictrp", {}, []),
    }

    payload = asyncio.run(
        engine.execute_query_v2(
            state_query([STUNTING]),
            adapters=adapters,
            regions=engine.default_region_index(),
        )
    ).payload

    assert len(payload["results"]) == 2
    assert {row["source"] for row in payload["results"]} == {"CT.gov", "AEA"}
    assert payload["meta"]["returned_unique_studies"] == 2


def test_ctgov_state_uses_one_ranked_condition_or_outcome_request(monkeypatch):
    requests: list[httpx.Request] = []

    async def resolve(uri: str):
        assert uri == STUNTING
        return engine.ConceptResolution(
            label="Stunting",
            terms=("Stunting", "Low height for age"),
            mesh_uris=("https://id.nlm.nih.gov/mesh/D013280",),
        )

    def map_study(raw, _iso, **_kwargs):
        study_id = raw["protocolSection"]["identificationModule"]["nctId"]
        return {
            "source": "CT.gov",
            "nctId": study_id,
            "title": study_id,
            "url": f"https://clinicaltrials.gov/study/{study_id}",
            "_intervention_names": [],
            "condition_mesh_uris": [],
            "intervention_mesh_uris": [],
            "_location_region_uris": [],
            "outcomes": ([{"measure": "Stunting"}] if study_id.endswith("OUTCOME") else []),
        }

    def respond(request: httpx.Request):
        requests.append(request)
        study_ids = ["NCT-CONDITION", "NCT-OUTCOME"] + [
            f"NCT-{index:03d}" for index in range(99)
        ]
        return httpx.Response(
            200,
            json={
                "studies": [
                    {
                        "protocolSection": {
                            "identificationModule": {"nctId": study_id}
                        }
                    }
                    for study_id in study_ids
                ]
            },
            request=request,
        )

    monkeypatch.setattr(engine.v1, "map_ctgov_study", map_study)
    monkeypatch.setattr(engine.v1, "_load_ctgov_intervention_concept_map", lambda: {})
    monkeypatch.setattr(engine, "_local_states_by_mesh", lambda _uris: {})
    adapter = engine.CtgovAdapter(
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        concept_resolver=resolve,
    )

    branch = asyncio.run(
        adapter.execute(
            {"state": STUNTING},
            engine.SourceBudget(),
            engine.default_region_index(),
        )
    )

    assert {"NCT-CONDITION", "NCT-OUTCOME"} <= {
        row["study_id"] for row in branch.rows
    }
    assert len({row["study_id"] for row in branch.rows}) == 100
    assert branch.truncated is True
    assert len(requests) == 1
    expression = requests[0].url.params["query.term"]
    assert "AREA[ConditionMeshId]D013280" in expression
    assert "AREA[OutcomeSearch]" in expression
    assert "query.cond" not in requests[0].url.params
    assert "query.outc" not in requests[0].url.params


def test_isrctn_state_closes_role_union_gap_in_one_request(monkeypatch):
    queries: list[str] = []

    async def resolve(uri: str):
        assert uri == STUNTING
        return engine.ConceptResolution(
            label="Stunting", terms=("Stunting",), mesh_uris=()
        )

    async def fetch(_client, _endpoint, query, _timeout):
        queries.append(query)
        return [
            {
                **result("ISRCTN", "ISRCTN-CONDITION"),
                "condition_descriptions": ["Stunting"],
                "countries": [],
                "country_iso_alpha2": [],
            },
            {
                **result("ISRCTN", "ISRCTN-OUTCOME"),
                "condition_descriptions": [],
                "outcomes": [{"measure": "Stunting"}],
                "countries": [],
                "country_iso_alpha2": [],
            },
        ]

    monkeypatch.setattr(engine.v1, "get_isrctn_conditions_xwalk", lambda: {})
    monkeypatch.setattr(engine, "_isrctn_direct_intervention_crosswalk", lambda: {})
    adapter = engine.IsrctnAdapter(
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, request=request)
            )
        ),
        fetch_batch=fetch,
        concept_resolver=resolve,
    )

    branch = asyncio.run(
        adapter.execute(
            {"state": STUNTING},
            engine.SourceBudget(),
            engine.default_region_index(),
        )
    )

    assert {row["study_id"] for row in branch.rows} == {
        "ISRCTN-CONDITION",
        "ISRCTN-OUTCOME",
    }
    assert queries == ["(condition: Stunting OR outcomeMeasures: Stunting)"]
