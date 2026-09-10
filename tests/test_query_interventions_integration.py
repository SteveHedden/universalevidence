import asyncio
import json
import os
import sys
import warnings
from pathlib import Path
from typing import Optional

import pytest

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import query_interventions as qi
import load_fuseki

FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "ctgov_child_malnutrition_kenya.json"
OUTCOMES_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "ctgov_child_malnutrition_kenya_outcomes.json"
CHALLENGE_URI = "https://universalevidence.com/vocab/conditions/ChildMalnutrition"
AEA_STUDY_URI = "https://universalevidence.com/ontology/Evidence_AEA_AEARCTR-0002019"


class MockResponse:
    def __init__(self, payload: dict, exc: Optional[Exception] = None):
        self.payload = payload
        self.exc = exc

    def json(self) -> dict:
        return self.payload

    def raise_for_status(self) -> None:
        if self.exc is not None:
            raise self.exc
        return None


def test_aea_external_url_prefers_registry_identifier_and_never_internal_uri():
    public_url = "http://www.socialscienceregistry.org/trials/11638"
    internal_uri = (
        "https://universalevidence.com/ontology/"
        "Evidence_AEA_AEARCTR-0011638"
    )

    assert qi._aea_external_url(public_url, internal_uri) == public_url
    assert qi._aea_external_url("AEARCTR-0011638") == (
        "https://www.socialscienceregistry.org/trials/11638"
    )
    assert qi._aea_external_url(None, internal_uri) == (
        "https://www.socialscienceregistry.org/trials/11638"
    )
    assert qi._aea_external_url(
        None, "https://universalevidence.com/ontology/Evidence_AEA_unknown"
    ) is None


@pytest.fixture
def ctgov_payload() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


@pytest.fixture
def ctgov_outcomes_payload() -> dict:
    return json.loads(OUTCOMES_FIXTURE_PATH.read_text())


def mock_ctgov_response(payload: dict):
    def _mock_get(*_args, **_kwargs):
        return MockResponse(payload)

    return _mock_get


def sparql_results(rows: list[dict[str, str]]) -> dict:
    return {
        "head": {"vars": sorted({key for row in rows for key in row})},
        "results": {
            "bindings": [
                {
                    key: {
                        "type": "uri" if value.startswith("http") else "literal",
                        "value": value,
                    }
                    for key, value in row.items()
                }
                for row in rows
            ]
        },
    }


def mock_sparql_response():
    def _mock_post(_url, data=None, **_kwargs):
        query = data["query"]
        if "SELECT ?source ?axis ?lookupProperty" in query:
            return MockResponse(
                sparql_results(
                    [
                        {
                            "source": "https://universalevidence.com/vocab/sources/ctgov",
                            "axis": f"{qi.UE}ChallengeAxis",
                            "lookupProperty": f"{qi.SKOS}exactMatch",
                            "namespaceFilter": qi.MESH_PREFIX,
                            "queryMechanism": f"{qi.UE}LiveQuery",
                            "apiParam": "query.cond",
                            "valueTransform": f"{qi.UE}ExtractMeSHLabel",
                        },
                        {
                            "source": "https://universalevidence.com/vocab/sources/ctgov",
                            "axis": f"{qi.UE}RegionAxis",
                            "lookupProperty": f"{qi.RDFS}label",
                            "queryMechanism": f"{qi.UE}LiveQuery",
                            "apiParam": "query.locn",
                            "valueTransform": f"{qi.UE}PassLiteral",
                        },
                        {
                            "source": "https://universalevidence.com/vocab/sources/aea",
                            "axis": f"{qi.UE}ChallengeAxis",
                            "lookupProperty": f"{qi.UE}aeaKeyword",
                            "queryMechanism": f"{qi.UE}LocalSparql",
                            "sparqlField": f"{qi.UE}aeaKeyword",
                            "valueTransform": f"{qi.UE}PassLiteral",
                        },
                        {
                            "source": "https://universalevidence.com/vocab/sources/aea",
                            "axis": f"{qi.UE}RegionAxis",
                            "lookupProperty": f"{qi.UE}iso3166Alpha2",
                            "queryMechanism": f"{qi.UE}LocalSparql",
                            "sparqlField": f"{qi.AEA}country",
                            "valueTransform": f"{qi.UE}ExpandToCountryLiterals",
                        },
                    ]
                )
            )
        if "SELECT ?challenge" in query:
            return MockResponse(sparql_results([{"condition": CHALLENGE_URI}]))
        if "SELECT ?value" in query and "core#exactMatch" in query:
            return MockResponse(
                sparql_results([{"value": "https://id.nlm.nih.gov/mesh/D044342"}])
            )
        if "SELECT ?value" in query and "ontology/aeaKeyword" in query:
            return MockResponse(sparql_results([{"value": "nutrition"}]))
        if "SELECT ?keyword ?sector" in query:
            return MockResponse(
                sparql_results(
                    [
                        {
                            "keyword": "nutrition",
                            "keyword": "https://socialscienceregistry.org/schema#Keyword_Health",
                        }
                    ]
                )
            )
        if "SELECT ?source ?study_id ?title ?url ?interventionUri ?interventionLabel ?intervention ?country ?status ?date ?outcomeText" in query:
            return MockResponse(
                sparql_results(
                    [
                        {
                            "source": "AEA",
                            "study_id": "AEARCTR-0002019",
                            "title": "Integrated Sanitation and Nutrition Behavior Change in Kenya",
                            "url": "http://www.socialscienceregistry.org/trials/2019",
                            "intervention": "Integrated sanitation and nutrition behavior change",
                            "country": "KE",
                            "status": "completed",
                            "date": "2019",
                            "outcomeText": "Child growth and household sanitation outcomes",
                        },
                    ]
                )
            )
        if "SELECT ?mesh" in query:
            return MockResponse(
                sparql_results([{"mesh": "https://id.nlm.nih.gov/mesh/D044342"}])
            )
        if "SELECT ?label" in query:
            return MockResponse(sparql_results([{"label": "Child Malnutrition"}]))
        raise AssertionError(f"Unexpected SPARQL query:\n{query}")

    return _mock_post


def test_cli_returns_merged_results_with_expected_schema(monkeypatch, capsys, ctgov_payload):
    monkeypatch.setattr(qi.requests, "post", mock_sparql_response())
    monkeypatch.setattr(qi.requests, "get", mock_ctgov_response(ctgov_payload))
    monkeypatch.setattr(qi.requests, "put", lambda *_args, **_kwargs: MockResponse({}))
    monkeypatch.setattr(qi.requests, "delete", lambda *_args, **_kwargs: MockResponse({}))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "query_interventions.py",
            "--challenge",
            "Child Malnutrition",
            "--country",
            "Kenya",
        ],
    )

    assert qi.main() == 0
    results = json.loads(capsys.readouterr().out)

    assert any(
        result["source"] == "AEA" and "AEARCTR-0002019" in result["study_id"]
        for result in results
    )
    assert any(
        result["source"] == "CT.gov"
        and result["url"].startswith("https://clinicaltrials.gov/study/")
        for result in results
    )
    assert all(tuple(result.keys()) == qi.RESULT_KEYS for result in results)


def test_country_normalization_ke_and_kenya_return_same_aea_set(monkeypatch, ctgov_payload):
    monkeypatch.setattr(qi.requests, "post", mock_sparql_response())
    monkeypatch.setattr(qi.requests, "get", mock_ctgov_response(ctgov_payload))
    monkeypatch.setattr(qi.requests, "put", lambda *_args, **_kwargs: MockResponse({}))
    monkeypatch.setattr(qi.requests, "delete", lambda *_args, **_kwargs: MockResponse({}))

    results_ke = qi.query_interventions("Child Malnutrition", "KE")
    results_kenya = qi.query_interventions("Child Malnutrition", "Kenya")

    aea_ke = {result["study_id"] for result in results_ke if result["source"] == "AEA"}
    aea_kenya = {
        result["study_id"] for result in results_kenya if result["source"] == "AEA"
    }

    assert aea_ke
    assert aea_ke == aea_kenya


def test_ctgov_query_uses_full_country_name(monkeypatch, ctgov_payload):
    captured = {}

    def _mock_get(_url, params=None, **_kwargs):
        captured.update(params)
        return MockResponse(ctgov_payload)

    monkeypatch.setattr(qi.requests, "post", mock_sparql_response())
    monkeypatch.setattr(qi.requests, "get", _mock_get)
    monkeypatch.setattr(qi.requests, "put", lambda *_args, **_kwargs: MockResponse({}))
    monkeypatch.setattr(qi.requests, "delete", lambda *_args, **_kwargs: MockResponse({}))

    qi.query_interventions("Child Malnutrition", "KE")

    assert captured["query.locn"] == "Kenya"


def test_ctgov_no_longer_uses_session_graph(monkeypatch, ctgov_payload):
    calls = {"put": [], "delete": []}

    def _mock_put(url, data=None, headers=None, **_kwargs):
        calls["put"].append((url, data, headers))
        return MockResponse({})

    def _mock_delete(url, **_kwargs):
        calls["delete"].append(url)
        return MockResponse({})

    monkeypatch.setattr(qi.requests, "post", mock_sparql_response())
    monkeypatch.setattr(qi.requests, "get", mock_ctgov_response(ctgov_payload))
    monkeypatch.setattr(qi.requests, "put", _mock_put)
    monkeypatch.setattr(qi.requests, "delete", _mock_delete)

    qi.query_interventions("Child Malnutrition", "Kenya")

    assert calls["put"] == []
    assert calls["delete"] == []


def test_ctgov_outcomes_are_extracted_without_direction_inference(ctgov_outcomes_payload):
    mapped = qi.map_ctgov_study(ctgov_outcomes_payload["studies"][0], "KE")

    assert mapped["outcomes"] == [
        {
            "type": "reported-result",
            "measure": "Weight-for-age z-score",
            "description": "Mean change in child weight-for-age z-score.",
            "time_frame": "6 months",
            "summary_statistic": "EG000 0.42 (0.11) z-score",
        },
        {
            "type": "pre-specified-measure",
            "measure": "Clinic visits",
            "description": "Number of clinic visits during follow-up.",
            "time_frame": "6 months",
        },
        {
            "type": "adverse-event",
            "measure": "Hospitalization",
            "description": "General disorders",
            "summary_statistic": "EG000 events=2 affected=2 at risk=120",
        },
    ]
    assert all("direction" not in outcome for outcome in mapped["outcomes"])


def test_query_results_include_aea_and_ctgov_outcomes(monkeypatch, ctgov_outcomes_payload):
    def _mock_post(_url, data=None, **_kwargs):
        query = data["query"]
        if "SELECT ?source ?study_id ?title ?url ?interventionUri ?interventionLabel ?intervention ?country ?status ?date ?outcomeText" in query:
            return MockResponse(
                sparql_results(
                    [
                        {
                            "source": "AEA",
                            "study_id": "AEARCTR-0002019",
                            "title": "Integrated Sanitation and Nutrition Behavior Change in Kenya",
                            "url": "http://www.socialscienceregistry.org/trials/2019",
                            "intervention": "Integrated sanitation and nutrition behavior change",
                            "country": "KE",
                            "status": "completed",
                            "date": "2019",
                            "outcomeText": "Child growth and household sanitation outcomes",
                        },
                    ]
                )
            )
        return mock_sparql_response()(_url, data=data, **_kwargs)

    monkeypatch.setattr(qi.requests, "post", _mock_post)
    monkeypatch.setattr(qi.requests, "get", mock_ctgov_response(ctgov_outcomes_payload))
    monkeypatch.setattr(qi.requests, "put", lambda *_args, **_kwargs: MockResponse({}))
    monkeypatch.setattr(qi.requests, "delete", lambda *_args, **_kwargs: MockResponse({}))

    results = qi.query_interventions("Child Malnutrition", "Kenya")

    assert all("outcomes" in result for result in results)
    aea = next(result for result in results if result["source"] == "AEA")
    ctgov = next(result for result in results if result["source"] == "CT.gov")
    assert aea["outcomes"] == [
        {
            "type": "pre-specified-measure",
            "measure": "Child growth and household sanitation outcomes",
        }
    ]
    assert {outcome["type"] for outcome in ctgov["outcomes"]} == {
        "reported-result",
        "pre-specified-measure",
        "adverse-event",
    }


def test_async_dispatcher_merges_source_adapters(monkeypatch):
    monkeypatch.setattr(qi, "resolve_condition_node", lambda _challenge: CHALLENGE_URI)
    monkeypatch.setattr(qi, "load_query_bindings", lambda: {"ctgov": {}, "aea": {}})
    monkeypatch.setattr(
        qi,
        "build_source_params",
        lambda _condition_node, _country, _bindings: {
            "ctgov": {"api": {"query.cond": ["Child Malnutrition"]}},
            "aea": {"sparql": {}},
            "isrctn": {"api": {}},
            "wholictrp": {"sparql": {}},
        },
    )
    monkeypatch.setattr(qi, "extract_condition_terms", lambda _node: (["nutrition"], set()))

    async def _aea(_country, _keywords, _sector_uris):
        return [
            {
                "source": "AEA",
                "study_id": "AEARCTR-0001",
                "title": "AEA",
                "year": 2019,
                "outcomes": [],
            }
        ]

    async def _ctgov(_params, _iso_country):
        return [
            {
                "source": "CT.gov",
                "study_id": "NCT1",
                "title": "CT",
                "year": 2021,
                "outcomes": [],
            }
        ]

    async def _isrctn(_params):
        return [
            {
                "source": "ISRCTN",
                "study_id": "ISRCTN1",
                "title": "ISRCTN",
                "year": 2020,
                "outcomes": [],
            }
        ]

    async def _who(_params):
        return [
            {
                "source": "WHO ICTRP",
                "study_id": "W1",
                "title": "WHO",
                "year": 2018,
                "outcomes": [],
            }
        ]

    monkeypatch.setattr(qi, "run_aea_adapter", _aea)
    monkeypatch.setattr(qi, "run_ctgov_adapter", _ctgov)
    monkeypatch.setattr(qi, "run_isrctn_adapter", _isrctn)
    monkeypatch.setattr(qi, "run_who_ictrp_adapter", _who)

    results = asyncio.run(qi.async_query_interventions("Child Malnutrition", "Kenya"))

    assert [result["source"] for result in results] == [
        "CT.gov",
        "ISRCTN",
        "AEA",
        "WHO ICTRP",
    ]
    assert all(tuple(result.keys()) == qi.RESULT_KEYS for result in results)


def test_async_query_interventions_delegates_to_axis_dispatcher(monkeypatch):
    captured = {}

    async def _mock_query_axes(axes):
        captured["axes"] = axes
        return [{"source": "AEA", "study_id": "AEARCTR-0001", "outcomes": []}]

    monkeypatch.setattr(qi, "async_query_axes", _mock_query_axes)

    results = asyncio.run(qi.async_query_interventions("Child Malnutrition", "Kenya"))

    assert captured["axes"] == {"condition": "Child Malnutrition", "country": "Kenya"}
    assert results == [{"source": "AEA", "study_id": "AEARCTR-0001", "outcomes": []}]


def test_async_query_axes_rejects_currently_unsupported_combinations():
    with pytest.raises(ValueError, match="Challenge axis is required"):
        asyncio.run(qi.async_query_axes({"intervention": "https://universalevidence.com/vocab/interventions/NutritionEducation"}))

    with pytest.raises(ValueError, match="Country or country-level region is required"):
        asyncio.run(qi.async_query_axes({"condition": CHALLENGE_URI}))


def test_resolve_region_country_uses_iso_alpha2(monkeypatch):
    def _mock_sparql_select(query):
        assert "ue:iso3166Alpha2" in query
        assert "https://sws.geonames.org/192950/" in query
        return [{"iso": "KE", "label": "Kenya"}]

    monkeypatch.setattr(qi, "sparql_select", _mock_sparql_select)

    assert qi.resolve_region_country("https://sws.geonames.org/192950/") == "KE"


def test_resolve_region_country_rejects_groupings(monkeypatch):
    monkeypatch.setattr(
        qi,
        "sparql_select",
        lambda _query: [{"label": "Sub-Saharan Africa"}],
    )

    with pytest.raises(ValueError, match="Region group queries are not supported"):
        qi.resolve_region_country("https://universalevidence.com/vocab/regions/SubSaharanAfrica")


def test_load_query_bindings_preserves_fallback_metadata(monkeypatch):
    def _mock_sparql_select(query):
        assert "ue:fallback" in query
        return [
            {
                "source": "https://universalevidence.com/vocab/sources/isrctn",
                "axis": f"{qi.UE}ChallengeAxis",
                "lookupProperty": f"{qi.SKOS}broadMatch",
                "namespaceFilter": "https://universalevidence.com/vocab/isrctn/condition-categories/",
                "queryMechanism": f"{qi.UE}LiveQuery",
                "apiParam": "conditionCategory",
                "valueTransform": f"{qi.UE}ExtractLabel",
                "fallbackLookupProperty": f"{qi.SKOS}altLabel",
                "fallbackApiParam": "condition",
                "fallbackValueTransform": f"{qi.UE}PassLiteral",
                "fallbackFilterQuality": "broad-text",
            },
            {
                "source": "https://universalevidence.com/vocab/sources/wholictrp",
                "axis": f"{qi.UE}RegionAxis",
                "lookupProperty": f"{qi.UE}iso3166Alpha2",
                "queryMechanism": f"{qi.UE}LocalSparql",
                "sparqlField": f"{qi.ICTRP}countryIsoAlpha2",
                "valueTransform": f"{qi.UE}PassLiteral",
            },
        ]

    monkeypatch.setattr(qi, "sparql_select", _mock_sparql_select)

    bindings = qi.load_query_bindings()

    isrctn_challenge = bindings["isrctn"]["ChallengeAxis"][0]
    assert isrctn_challenge["namespaceFilter"].endswith("/condition-categories/")
    assert isrctn_challenge["fallbackApiParam"] == "condition"
    assert isrctn_challenge["fallbackFilterQuality"] == "broad-text"
    assert bindings["wholictrp"]["RegionAxis"][0]["sparqlField"] == f"{qi.ICTRP}countryIsoAlpha2"


def test_build_source_params_applies_transforms_and_fallbacks(monkeypatch):
    calls = []

    bindings = {
        "ctgov": {
            "ChallengeAxis": [
                {
                    "lookupProperty": f"{qi.SKOS}exactMatch",
                    "namespaceFilter": qi.MESH_PREFIX,
                    "apiParam": "query.cond",
                    "valueTransform": f"{qi.UE}ExtractMeSHLabel",
                }
            ],
            "RegionAxis": [
                {
                    "lookupProperty": f"{qi.RDFS}label",
                    "apiParam": "query.locn",
                    "valueTransform": f"{qi.UE}PassLiteral",
                }
            ],
        },
        "isrctn": {
            "ChallengeAxis": [
                {
                    "lookupProperty": f"{qi.SKOS}broadMatch",
                    "namespaceFilter": "https://universalevidence.com/vocab/isrctn/condition-categories/",
                    "apiParam": "conditionCategory",
                    "valueTransform": f"{qi.UE}ExtractLabel",
                    "fallbackLookupProperty": f"{qi.SKOS}altLabel",
                    "fallbackApiParam": "condition",
                    "fallbackValueTransform": f"{qi.UE}PassLiteral",
                }
            ]
        },
        "aea": {
            "ChallengeAxis": [
                {
                    "lookupProperty": f"{qi.UE}aeaKeyword",
                    "sparqlField": f"{qi.UE}aeaKeyword",
                    "valueTransform": f"{qi.UE}PassLiteral",
                }
            ],
            "RegionAxis": [
                {
                    "lookupProperty": f"{qi.UE}iso3166Alpha2",
                    "sparqlField": f"{qi.AEA}country",
                    "valueTransform": f"{qi.UE}ExpandToCountryLiterals",
                }
            ],
        },
        "wholictrp": {
            "RegionAxis": [
                {
                    "lookupProperty": f"{qi.UE}iso3166Alpha2",
                    "sparqlField": f"{qi.ICTRP}countryIsoAlpha2",
                    "valueTransform": f"{qi.UE}PassLiteral",
                }
            ]
        },
    }

    def _taxonomy_lookup(subject_uri, property_uri, namespace_filter=None):
        calls.append((subject_uri, property_uri, namespace_filter))
        if property_uri == f"{qi.SKOS}exactMatch":
            return ["https://id.nlm.nih.gov/mesh/D044342"]
        if property_uri == f"{qi.SKOS}broadMatch":
            return []
        if property_uri == f"{qi.SKOS}altLabel":
            return ["child undernutrition"]
        if property_uri == f"{qi.UE}aeaKeyword":
            return ["nutrition"]
        return []

    monkeypatch.setattr(qi, "get_condition_label", lambda _node: "Child Malnutrition")
    monkeypatch.setattr(qi, "taxonomy_lookup", _taxonomy_lookup)

    params = qi.build_source_params(CHALLENGE_URI, "Kenya", bindings)

    assert params["ctgov"]["api"]["query.cond"] == ["Child Malnutrition"]
    assert params["ctgov"]["api"]["query.locn"] == ["Kenya"]
    assert params["isrctn"]["api"] == {"condition": ["child undernutrition"]}
    assert params["aea"]["sparql"][f"{qi.UE}aeaKeyword"] == ["nutrition"]
    assert "Kenya" in params["aea"]["sparql"][f"{qi.AEA}country"]
    assert params["wholictrp"]["sparql"][f"{qi.ICTRP}countryIsoAlpha2"] == ["KE"]
    assert (CHALLENGE_URI, f"{qi.SKOS}exactMatch", qi.MESH_PREFIX) in calls
    assert (
        CHALLENGE_URI,
        f"{qi.SKOS}broadMatch",
        "https://universalevidence.com/vocab/isrctn/condition-categories/",
    ) in calls


def test_dedupe_and_sort_results_keeps_latest_source_study_id_pair():
    results = qi.dedupe_and_sort_results(
        [
            {"source": "AEA", "study_id": "1", "title": "old", "year": 2018},
            {"source": "CT.gov", "study_id": "1", "title": "different source", "year": 2020},
            {"source": "AEA", "study_id": "1", "title": "replacement", "year": 2022},
        ]
    )

    assert [(result["source"], result["study_id"], result["title"]) for result in results] == [
        ("AEA", "1", "replacement"),
        ("CT.gov", "1", "different source"),
    ]
    assert all(result["outcomes"] == [] for result in results)


def test_local_fuseki_failure_propagates_without_session_graph(monkeypatch, ctgov_payload):
    def _mock_post(_url, data=None, **_kwargs):
        query = data["query"]
        if "SELECT ?source ?study_id ?title ?url ?interventionUri ?interventionLabel ?intervention ?country ?status ?date" in query:
            return MockResponse({}, exc=qi.requests.RequestException("final select failed"))
        return mock_sparql_response()(_url, data=data, **_kwargs)

    monkeypatch.setattr(qi.requests, "post", _mock_post)
    monkeypatch.setattr(qi.requests, "get", mock_ctgov_response(ctgov_payload))
    monkeypatch.setattr(qi.requests, "put", lambda *_args, **_kwargs: MockResponse({}))
    monkeypatch.setattr(qi.requests, "delete", lambda *_args, **_kwargs: MockResponse({}))

    with pytest.raises(qi.requests.RequestException):
        qi.query_interventions("Child Malnutrition", "Kenya")


def test_no_mesh_condition_skips_ctgov_session_graph(monkeypatch):
    def _mock_post(_url, data=None, **_kwargs):
        query = data["query"]
        if "SELECT ?value" in query and "core#exactMatch" in query:
            return MockResponse(sparql_results([]))
        if "SELECT ?source ?study_id ?title ?url ?interventionUri ?interventionLabel ?intervention ?country ?status ?date" in query:
            return MockResponse(sparql_results([]))
        return mock_sparql_response()(_url, data=data, **_kwargs)

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("CT.gov session graph should not be used")

    monkeypatch.setattr(qi.requests, "post", _mock_post)
    monkeypatch.setattr(qi.requests, "get", _unexpected)
    monkeypatch.setattr(qi.requests, "put", _unexpected)
    monkeypatch.setattr(qi.requests, "delete", _unexpected)

    assert qi.query_interventions("Child Malnutrition", "Kenya") == []


def test_query_module_has_no_rdflib_runtime_import():
    assert "rdflib" not in Path(qi.__file__).read_text()


def test_load_fuseki_puts_expected_named_graphs(monkeypatch):
    calls = []

    def _mock_put(url, data=None, headers=None, **_kwargs):
        calls.append((url, data, headers))
        return MockResponse({})

    monkeypatch.setattr(load_fuseki.requests, "put", _mock_put)

    load_fuseki.load_default_graphs("http://localhost:3030/ue/data")

    assert len(calls) == 2
    urls = [call[0] for call in calls]
    assert "graph=https%3A%2F%2Funiversalevidence.com%2Fgraph%2Faea" in urls[0]
    assert "graph=https%3A%2F%2Funiversalevidence.com%2Fgraph%2Fue-taxonomy" in urls[1]
    assert all(call[2] == {"Content-Type": "text/turtle"} for call in calls)

    taxonomy_payload = calls[1][1].decode("utf-8")
    assert "ChildMalnutrition" in taxonomy_payload
    assert "InsecticideTreatedNets" in taxonomy_payload
    assert "UnderFiveMortality" in taxonomy_payload
    assert "ISRCTN Registry" in taxonomy_payload
    assert "ConditionCategory_InfectionsAndInfestations" in taxonomy_payload
    assert "Keyword_health" in taxonomy_payload


def test_load_fuseki_conditionally_loads_who_ictrp(monkeypatch, tmp_path):
    who_path = tmp_path / "who-ictrp-raw.ttl"
    who_path.write_text(
        "@prefix ictrp: <https://universalevidence.com/source/who-ictrp/schema#> .\n"
        "<https://universalevidence.com/source/who-ictrp/trial/T1> "
        "a ictrp:TrialRegistration .\n",
        encoding="utf-8",
    )
    calls = []

    def _mock_put(url, data=None, headers=None, **_kwargs):
        calls.append((url, data, headers))
        return MockResponse({})

    monkeypatch.setattr(load_fuseki.requests, "put", _mock_put)
    monkeypatch.setattr(load_fuseki, "WHO_ICTRP_SOURCE", who_path)

    load_fuseki.load_default_graphs("http://localhost:3030/ue/data")

    assert len(calls) == 3
    assert "graph=https%3A%2F%2Funiversalevidence.com%2Fgraph%2Fwho-ictrp" in calls[2][0]


@pytest.mark.skipif(not os.environ.get("FUSEKI_URL"), reason="requires live Fuseki")
def test_live_fuseki_query_path_is_opt_in(monkeypatch, ctgov_payload):
    monkeypatch.setattr(qi.requests, "get", mock_ctgov_response(ctgov_payload))

    results = qi.query_interventions("Child Malnutrition", "Kenya")

    assert all(tuple(result.keys()) == qi.RESULT_KEYS for result in results)


@pytest.mark.parametrize("study_id", ["CTRI/2018/10/016193", "CTRI/2023/07/055739"])
def test_ctri_results_link_to_who_record_using_public_registration_id(study_id):
    from urllib.parse import parse_qs, urlparse

    result = qi._local_rows_to_results([{
        "graph": "https://universalevidence.com/graph/who-ictrp",
        "study_id": study_id,
        "title": "Example CTRI trial",
    }])[0]
    parsed = urlparse(result["url"])
    assert parsed.netloc == "trialsearch.who.int"
    assert parsed.path == "/Trial2.aspx"
    assert parse_qs(parsed.query) == {"TrialID": [study_id]}


def test_ctri_fix_preserves_direct_registry_links():
    assert qi._who_ictrp_external_url("NCT02761876") == "https://clinicaltrials.gov/study/NCT02761876"
    assert qi._who_ictrp_external_url("ISRCTN94319790") == "https://www.isrctn.com/ISRCTN94319790"
