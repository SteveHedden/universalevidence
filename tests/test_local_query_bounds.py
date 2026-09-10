import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest
import requests
from fastapi import HTTPException


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import query_interventions as qi


OUTCOME = "https://universalevidence.com/vocab/states/AdverseEvents"


class MockResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"results": {"bindings": []}}


def test_local_query_peak_concurrency_is_bounded(monkeypatch):
    monkeypatch.setattr(qi, "_LOCAL_QUERY_SEMAPHORE", asyncio.Semaphore(2))
    active = 0
    peak = 0

    def slow_query(_axes):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            time.sleep(0.03)
            return []
        finally:
            active -= 1

    monkeypatch.setattr(qi, "_run_unified_local_query", slow_query)

    async def run():
        await asyncio.gather(*(qi._run_local_query_limited({}) for _ in range(8)))

    asyncio.run(run())
    assert peak == 2


def test_local_sources_run_concurrently_and_keep_result_order(monkeypatch):
    both_started = threading.Barrier(2, timeout=1)

    def aea(_axes):
        both_started.wait()
        return [{"source": "aea"}]

    def who(_axes):
        both_started.wait()
        return [{"source": "who"}]

    monkeypatch.setattr(qi, "_run_aea_crosswalk_query", aea)
    monkeypatch.setattr(qi, "_run_who_ictrp_crosswalk_query", who)

    assert qi._run_unified_local_query({}) == [
        {"source": "aea"},
        {"source": "who"},
    ]


def test_state_local_query_preserves_separate_axis_budgets(monkeypatch):
    calls = []

    def aea(axes):
        calls.append(("aea", axes))
        return [{"source": "aea", "axis": next(iter(axes))}]

    def who(axes):
        calls.append(("who", axes))
        return [{"source": "who", "axis": next(iter(axes))}]

    monkeypatch.setattr(qi, "_run_aea_crosswalk_query", aea)
    monkeypatch.setattr(qi, "_run_who_ictrp_crosswalk_query", who)

    results = qi._run_unified_local_query({"state": OUTCOME})

    assert {tuple(sorted(axes.items())) for _, axes in calls} == {
        (("condition", OUTCOME),),
        (("outcome", OUTCOME),),
    }
    assert len(calls) == 4
    assert results == [
        {"source": "aea", "axis": "condition"},
        {"source": "who", "axis": "condition"},
        {"source": "aea", "axis": "outcome"},
        {"source": "who", "axis": "outcome"},
    ]


def test_aea_enrichment_lookups_run_concurrently(monkeypatch):
    all_started = threading.Barrier(4, timeout=1)
    study_uri = "https://example.test/study/1"

    monkeypatch.setattr(qi, "build_aea_crosswalk_sparql_query", lambda _axes: "query")
    monkeypatch.setattr(qi, "sparql_select", lambda _query: [{"study": study_uri}])
    monkeypatch.setattr(qi, "_source_graph_uri", lambda *_args: qi.AEA_GRAPH_URI)
    monkeypatch.setattr(qi, "_first_source_field", lambda *_args: "keyword")
    monkeypatch.setattr(qi, "_local_rows_to_results", lambda rows: rows)

    def direct(*_args):
        all_started.wait()
        return {}

    def stamped(*_args):
        all_started.wait()
        return {}

    def conditions(*_args):
        all_started.wait()
        return {}

    def outcomes(*_args):
        all_started.wait()
        return {}

    monkeypatch.setattr(qi, "lookup_all_direct_intervention_concepts", direct)
    monkeypatch.setattr(qi, "lookup_all_intervention_concepts", stamped)
    monkeypatch.setattr(qi, "lookup_condition_concepts", conditions)
    monkeypatch.setattr(qi, "lookup_matched_outcome_concepts", outcomes)

    results = qi._run_aea_crosswalk_query({"outcome": OUTCOME})

    assert results == [{"study": study_uri, "allInterventionConcepts": []}]


def test_who_enrichment_lookups_run_concurrently(monkeypatch):
    all_started = threading.Barrier(3, timeout=1)
    study_uri = "https://example.test/study/1"

    monkeypatch.setattr(qi, "build_who_ictrp_crosswalk_query", lambda _axes: "query")
    monkeypatch.setattr(qi, "sparql_select", lambda _query: [{"study": study_uri}])
    monkeypatch.setattr(qi, "_local_rows_to_results", lambda rows: rows)

    def lookup(*_args):
        all_started.wait()
        return {}

    monkeypatch.setattr(qi, "lookup_all_direct_intervention_concepts", lookup)
    monkeypatch.setattr(qi, "lookup_all_intervention_concepts", lookup)
    monkeypatch.setattr(qi, "lookup_matched_outcome_concepts", lookup)

    results = qi._run_who_ictrp_crosswalk_query({"outcome": OUTCOME})

    assert results == [{"study": study_uri, "allInterventionConcepts": []}]


def test_local_query_busy_raises_instead_of_returning_empty(monkeypatch):
    monkeypatch.setattr(qi, "_LOCAL_QUERY_SEMAPHORE", asyncio.Semaphore(0))
    monkeypatch.setattr(qi, "LOCAL_QUERY_SEMAPHORE_TIMEOUT", 0.01)

    with pytest.raises(qi.LocalQueryBusyError, match="Search is busy"):
        asyncio.run(qi._run_local_query_limited({}))


def test_local_query_permit_released_after_exception(monkeypatch):
    semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(qi, "_LOCAL_QUERY_SEMAPHORE", semaphore)

    def fail(_axes):
        raise RuntimeError("simulated Fuseki failure")

    monkeypatch.setattr(qi, "_run_unified_local_query", fail)

    with pytest.raises(RuntimeError, match="simulated Fuseki failure"):
        asyncio.run(qi._run_local_query_limited({}))
    assert semaphore._value == 1


def test_disconnect_keeps_permit_until_detached_work_finishes(monkeypatch):
    semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(qi, "_LOCAL_QUERY_SEMAPHORE", semaphore)
    monkeypatch.setattr(qi, "_DETACHED_QUERY_TASKS", set())

    def slow_query(_axes):
        time.sleep(0.08)
        return []

    monkeypatch.setattr(qi, "_run_unified_local_query", slow_query)

    class DisconnectedRequest:
        async def is_disconnected(self):
            return True

    async def run():
        with pytest.raises(qi.ClientDisconnectedError):
            await qi.await_query_or_disconnect(
                qi._run_local_query_limited({}),
                DisconnectedRequest(),
                poll_interval=0,
            )
        await asyncio.sleep(0.01)
        assert semaphore._value == 0
        assert len(qi._DETACHED_QUERY_TASKS) == 1
        await asyncio.sleep(0.1)
        assert semaphore._value == 1
        assert not qi._DETACHED_QUERY_TASKS

    asyncio.run(run())


def test_crosswalk_queries_are_limited(monkeypatch):
    monkeypatch.setattr(qi, "_source_graph_uri", lambda *_args: qi.AEA_GRAPH_URI)
    monkeypatch.setattr(
        qi,
        "_first_source_field",
        lambda _source, axis, _role: f"https://example.test/{axis}",
    )

    who = qi.build_who_ictrp_crosswalk_query({"outcome": OUTCOME})
    aea = qi.build_aea_crosswalk_sparql_query({"outcome": OUTCOME})

    assert who and who.rstrip().endswith("LIMIT 100")
    assert aea and aea.rstrip().endswith("LIMIT 100")


def test_state_crosswalk_queries_match_condition_or_outcome(monkeypatch):
    monkeypatch.setattr(qi, "_source_graph_uri", lambda *_args: qi.AEA_GRAPH_URI)
    monkeypatch.setattr(
        qi,
        "_first_source_field",
        lambda _source, axis, _role: f"https://example.test/{axis}",
    )

    who = qi.build_who_ictrp_crosswalk_query({"state": OUTCOME})
    aea = qi.build_aea_crosswalk_sparql_query({"state": OUTCOME})

    expected = (
        f"(<{qi.UE}matchesCondition>|<{qi.UE}matchesOutcome>) <{OUTCOME}>"
    )
    assert who and expected in who
    assert aea and expected in aea


def test_sparql_select_sends_fuseki_server_timeout(monkeypatch):
    captured = {}

    def post(_url, **kwargs):
        captured.update(kwargs)
        return MockResponse()

    monkeypatch.setattr(requests, "post", post)

    assert qi.sparql_select("SELECT * WHERE {}", endpoint="http://fuseki/query") == []
    assert captured["data"] == {
        "query": "SELECT * WHERE {}",
        "timeout": "30000,30000",
    }


def test_query_route_maps_local_busy_to_503(monkeypatch):
    import api.routes.query as query_route

    async def busy(_axes):
        raise qi.LocalQueryBusyError("busy")

    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    monkeypatch.setattr(query_route, "_cached_query_axes", busy)

    async def run():
        with pytest.raises(HTTPException) as raised:
            await query_route.query(
                ConnectedRequest(),
                condition=OUTCOME,
                intervention=None,
                outcome=None,
                region=None,
                country=None,
            )
        assert raised.value.status_code == 503
        assert raised.value.detail == "Search is busy, try again shortly"

    asyncio.run(run())


def test_graph_route_maps_local_busy_to_503(monkeypatch):
    import api.routes.graph as graph_route

    async def busy(_axes):
        raise qi.LocalQueryBusyError("busy")

    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    monkeypatch.setattr(graph_route, "async_query_axes", busy)
    monkeypatch.setattr(graph_route, "load_taxonomy_labels", lambda: {})
    monkeypatch.setattr(graph_route, "load_intervention_taxonomy", lambda: [])

    async def run():
        with pytest.raises(HTTPException) as raised:
            await graph_route.graph(
                ConnectedRequest(),
                condition=OUTCOME,
                region=None,
                country=None,
                source=None,
            )
        assert raised.value.status_code == 503
        assert raised.value.detail == "Search is busy, try again shortly"

    asyncio.run(run())
