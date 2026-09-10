import requests
from fastapi.testclient import TestClient

import api.main as api_main
import api.routes.query as query_route


client = TestClient(api_main.app)

CONDITION_URI = "https://universalevidence.com/vocab/conditions/ChildMalnutrition"
INTERVENTION_URI = "https://universalevidence.com/vocab/interventions/NutritionEducation"
REGION_URI = "https://sws.geonames.org/192950/"


def test_health_endpoint():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_query_endpoint_forwards_uri_axes_to_async_dispatcher(monkeypatch):
    expected = [
        {
            "source": "AEA",
            "study_id": "AEARCTR-0002019",
            "title": "Integrated Sanitation and Nutrition Behavior Change in Kenya",
            "year": 2019,
            "outcomes": [],
        }
    ]

    async def _mock_query_axes(axes):
        assert axes == {
            "condition": CONDITION_URI,
            "intervention": INTERVENTION_URI,
        }
        return expected

    monkeypatch.setattr(query_route, "async_query_axes", _mock_query_axes)

    response = client.get(
        "/query",
        params={"condition": CONDITION_URI, "intervention": INTERVENTION_URI},
    )

    assert response.status_code == 200
    assert response.json() == [
        {
            "source": "AEA",
            "study_id": "AEARCTR-0002019",
            "title": "Integrated Sanitation and Nutrition Behavior Change in Kenya",
            "url": None,
            "intervention": None,
            "country": None,
            "status": None,
            "year": 2019,
            "outcomes": [],
        }
    ]


def test_query_endpoint_forwards_region_uri_axis_to_async_dispatcher(monkeypatch):
    async def _mock_query_axes(axes):
        assert axes == {"condition": CONDITION_URI, "region": REGION_URI}
        return [{"source": "AEA", "study_id": "AEARCTR-0002019", "outcomes": []}]

    monkeypatch.setattr(query_route, "async_query_axes", _mock_query_axes)

    response = client.get("/query", params={"condition": CONDITION_URI, "region": REGION_URI})

    assert response.status_code == 200
    assert response.json()[0]["study_id"] == "AEARCTR-0002019"


def test_query_endpoint_forwards_state_axis_to_single_dispatch(monkeypatch):
    calls = 0

    async def _mock_query_axes(axes):
        nonlocal calls
        calls += 1
        assert axes == {"state": CONDITION_URI, "region": REGION_URI}
        return [{"source": "AEA", "study_id": "AEARCTR-0002019", "outcomes": []}]

    monkeypatch.setattr(query_route, "async_query_axes", _mock_query_axes)

    response = client.get("/query", params={"state": CONDITION_URI, "region": REGION_URI})

    assert response.status_code == 200
    assert calls == 1


def test_query_endpoint_country_forwarded_as_legacy_alias(monkeypatch):
    async def _mock_query_axes(axes):
        assert axes == {"condition": CONDITION_URI, "country": "Kenya"}
        return [{"source": "AEA", "study_id": "AEARCTR-0002019", "outcomes": []}]

    monkeypatch.setattr(query_route, "async_query_axes", _mock_query_axes)

    response = client.get("/query", params={"condition": CONDITION_URI, "country": "Kenya"})

    assert response.status_code == 200


def test_query_endpoint_rejects_missing_axes():
    response = client.get("/query")

    assert response.status_code == 400
    assert response.json()["detail"] == "Provide at least one query axis"


def test_query_endpoint_rejects_stale_compact_names():
    response = client.get("/query", params={"condition": "uec:ChildMalnutrition"})

    assert response.status_code == 400
    assert response.json()["detail"] == "condition must be a full universalevidence.com vocabulary URI"


def test_query_endpoint_rejects_axis_uri_under_wrong_vocabulary():
    response = client.get(
        "/query",
        params={"condition": "https://universalevidence.com/vocab/outcomes/UnderFiveMortality"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "condition must be a full URI under https://universalevidence.com/vocab/conditions/"
    )


def test_query_endpoint_intervention_only_is_valid(monkeypatch):
    async def _mock_query_axes(axes):
        assert axes == {"intervention": INTERVENTION_URI}
        return []

    monkeypatch.setattr(query_route, "async_query_axes", _mock_query_axes)

    response = client.get("/query", params={"intervention": INTERVENTION_URI})

    assert response.status_code == 200


def test_query_endpoint_region_only_is_valid(monkeypatch):
    async def _mock_query_axes(axes):
        assert axes == {"region": REGION_URI}
        return []

    monkeypatch.setattr(query_route, "async_query_axes", _mock_query_axes)

    response = client.get("/query", params={"region": REGION_URI})

    assert response.status_code == 200


def test_query_endpoint_dispatcher_value_error_returns_400(monkeypatch):
    async def _mock_query_axes(_axes):
        raise ValueError("some dispatcher error")

    monkeypatch.setattr(query_route, "async_query_axes", _mock_query_axes)

    response = client.get("/query", params={"condition": CONDITION_URI})

    assert response.status_code == 400
    assert response.json()["detail"] == "some dispatcher error"


def test_query_endpoint_request_error_returns_502(monkeypatch):
    async def _mock_query_axes(_axes):
        raise requests.RequestException("Fuseki unavailable")

    monkeypatch.setattr(query_route, "async_query_axes", _mock_query_axes)

    response = client.get("/query", params={"condition": CONDITION_URI})

    assert response.status_code == 502
    assert response.json()["detail"] == "Downstream service request failed"


def test_query_endpoint_unexpected_error_returns_500(monkeypatch):
    async def _mock_query_axes(_axes):
        raise RuntimeError("boom")

    monkeypatch.setattr(query_route, "async_query_axes", _mock_query_axes)

    response = client.get("/query", params={"condition": CONDITION_URI})

    assert response.status_code == 500
    assert response.json()["detail"] == "Internal server error"


def test_internal_progress_endpoints_are_not_exposed():
    paths = ("/progress", "/expansion-progress", "/hygiene-progress")
    schema_paths = client.get("/openapi.json").json()["paths"]
    for path in paths:
        assert client.get(path).status_code == 404
        assert path not in schema_paths
