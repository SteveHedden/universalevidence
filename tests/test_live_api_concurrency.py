import asyncio
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import query_interventions as qi


INTERVENTION = "https://universalevidence.com/vocab/interventions/TestIntervention"
STATE = "https://universalevidence.com/vocab/states/TestState"


@pytest.mark.parametrize("raw, expected", [
    ("COMPLETED", "completed"),
    ("NOT_YET_RECRUITING", "in_development"),
    ("RECRUITING", "on_going"),
    ("ACTIVE_NOT_RECRUITING", "on_going"),
    ("ENROLLING_BY_INVITATION", "on_going"),
    ("TERMINATED", "TERMINATED"),
])
def test_ctgov_statuses_use_shared_vocabulary(raw, expected):
    study = {"protocolSection": {
        "identificationModule": {"nctId": "NCT12345678", "briefTitle": "Status test"},
        "statusModule": {"overallStatus": raw},
    }}
    assert qi.map_ctgov_study(study, "CR")["status"] == expected


def _prepare_resolvable_intervention(monkeypatch):
    monkeypatch.setattr(qi, "get_concept_search_terms", lambda _uri: ["Test intervention"])
    monkeypatch.setattr(qi, "get_concept_label", lambda _uri: "Test intervention")


def test_live_sources_share_combined_concurrency_limit(monkeypatch):
    _prepare_resolvable_intervention(monkeypatch)
    monkeypatch.setattr(qi, "_LIVE_API_SEMAPHORE", asyncio.Semaphore(2))
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def delayed_call(*args):
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        try:
            await asyncio.sleep(0.02)
            return []
        finally:
            async with lock:
                active -= 1

    async def ctgov(params, *_args):
        assert params["pageSize"] == "100"
        return await delayed_call()

    async def isrctn(params, *_args):
        assert params["limit"] == 100
        return await delayed_call()

    monkeypatch.setattr(qi, "_run_ctgov_live", ctgov)
    monkeypatch.setattr(qi, "_run_isrctn_live", isrctn)

    async def run_burst():
        axes = {"intervention": INTERVENTION}
        calls = [qi._maybe_run_ctgov(axes) for _ in range(4)]
        calls += [qi._maybe_run_isrctn(axes) for _ in range(4)]
        await asyncio.gather(*calls)

    asyncio.run(run_burst())
    assert peak == 2


def test_state_search_preserves_both_param_sets_under_one_live_slot(monkeypatch):
    monkeypatch.setattr(qi, "_LIVE_API_SEMAPHORE", asyncio.Semaphore(2))
    monkeypatch.setattr(qi, "extract_mesh_exact_matches", lambda _uri: ["http://id.nlm.nih.gov/mesh/D012345"])
    monkeypatch.setattr(
        qi,
        "get_concept_search_terms",
        lambda _uri: ["Test state", "Alternate state"],
    )
    captured = []
    acquisitions = 0

    async def acquire(_source):
        nonlocal acquisitions
        acquisitions += 1
        await qi._LIVE_API_SEMAPHORE.acquire()
        return True

    async def ctgov(params, *_args):
        captured.append(params)
        return []

    monkeypatch.setattr(qi, "_run_ctgov_live", ctgov)
    monkeypatch.setattr(qi, "_acquire_live_api_slot", acquire)

    assert asyncio.run(qi._maybe_run_ctgov({"state": STATE})) == []
    assert len(captured) == 2
    assert acquisitions == 2
    assert qi._LIVE_API_SEMAPHORE._value == 2
    condition_params, outcome_params = captured
    assert qi.ctgov_condition_mesh_filter(
        ["http://id.nlm.nih.gov/mesh/D012345"]
    ) == [condition_params["filter.advanced"]]
    assert condition_params.get("query.outc") is None
    assert outcome_params["query.outc"] == '"Test state" OR "Alternate state"'
    assert outcome_params.get("filter.advanced") is None


def test_ctgov_condition_mesh_filter_chunks_large_id_lists():
    mesh_uris = [f"http://id.nlm.nih.gov/mesh/D{i:06d}" for i in range(45)]
    filters = qi.ctgov_condition_mesh_filter(mesh_uris, chunk_size=20)
    assert len(filters) == 3
    assert filters[0].count(" OR ") == 19
    assert filters[1].count(" OR ") == 19
    assert filters[2].count(" OR ") == 4
    rejoined = " OR ".join(filters)
    for i in range(45):
        assert f"AREA[ConditionMeshId]D{i:06d}" in rejoined


def test_ctgov_condition_mesh_filter_preserves_all_chunks(caplog):
    mesh_uris = [f"http://id.nlm.nih.gov/mesh/D{i:06d}" for i in range(4284)]
    with caplog.at_level("INFO"):
        filters = qi.ctgov_condition_mesh_filter(mesh_uris, chunk_size=20)
    assert len(filters) == 215
    assert "coverage" in caplog.text
    assert all("AREA[ConditionMeshId]D" in chunk for chunk in filters)


def test_condition_axis_splits_into_chunked_requests_and_merges_results(monkeypatch):
    monkeypatch.setattr(qi, "_LIVE_API_SEMAPHORE", asyncio.Semaphore(2))
    mesh_uris = [f"http://id.nlm.nih.gov/mesh/D{i:06d}" for i in range(25)]
    monkeypatch.setattr(qi, "extract_mesh_exact_matches", lambda _uri: mesh_uris)
    monkeypatch.setattr(qi, "ctgov_condition_mesh_filter", lambda uris, chunk_size=20: ["chunk-a", "chunk-b"])

    captured = []

    async def ctgov(params, *_args):
        captured.append(params["filter.advanced"])
        return [{"source": "CT.gov", "study_id": params["filter.advanced"]}]

    monkeypatch.setattr(qi, "_run_ctgov_live", ctgov)

    condition = "https://universalevidence.com/vocab/states/TestCondition"
    results = asyncio.run(qi._maybe_run_ctgov({"condition": condition}))

    assert captured == ["chunk-a", "chunk-b"]
    assert {r["study_id"] for r in results} == {"chunk-a", "chunk-b"}


def test_isrctn_intervention_query_chunks_splits_large_term_lists():
    terms = [f"Intervention term {i}" for i in range(35)]
    chunks = qi.isrctn_intervention_query_chunks(terms, chunk_size=15, max_chunks=None)
    assert len(chunks) == 3
    assert chunks[0].count(" OR ") == 14
    assert chunks[1].count(" OR ") == 14
    assert chunks[2].count(" OR ") == 4
    rejoined = " OR ".join(chunks)
    for i in range(35):
        assert f'"Intervention term {i}"' in rejoined


def test_isrctn_intervention_query_chunks_caps_and_warns(caplog):
    terms = [f"Term{i}" for i in range(500)]
    with caplog.at_level("WARNING"):
        chunks = qi.isrctn_intervention_query_chunks(terms, chunk_size=15, max_chunks=15)
    assert len(chunks) == 15
    assert "truncated" in caplog.text


def test_isrctn_axis_splits_broad_intervention_into_chunked_requests_and_merges(monkeypatch):
    monkeypatch.setattr(qi, "_LIVE_API_SEMAPHORE", asyncio.Semaphore(2))
    terms = [f"Term{i}" for i in range(30)]
    monkeypatch.setattr(qi, "get_concept_search_terms", lambda _uri: terms)
    monkeypatch.setattr(qi, "get_concept_label", lambda _uri: "Test intervention")
    monkeypatch.setattr(
        qi, "isrctn_intervention_query_chunks", lambda intr_terms: ["chunk-a", "chunk-b"]
    )

    captured = []

    async def isrctn(params, *_args):
        captured.append(params["q"])
        return [{"source": "ISRCTN", "study_id": params["q"]}]

    monkeypatch.setattr(qi, "_run_isrctn_live", isrctn)

    results = asyncio.run(qi._maybe_run_isrctn({"intervention": INTERVENTION}))

    assert captured == ["chunk-a", "chunk-b"]
    assert {r["study_id"] for r in results} == {"chunk-a", "chunk-b"}


def test_live_api_slot_released_after_exception(monkeypatch):
    _prepare_resolvable_intervention(monkeypatch)
    semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(qi, "_LIVE_API_SEMAPHORE", semaphore)

    async def fail(*_args):
        raise RuntimeError("simulated mapping failure")

    async def succeed(*_args):
        return []

    monkeypatch.setattr(qi, "_run_ctgov_live", fail)
    monkeypatch.setattr(qi, "_run_isrctn_live", succeed)

    async def run():
        with pytest.raises(RuntimeError, match="simulated mapping failure"):
            await qi._maybe_run_ctgov({"intervention": INTERVENTION})
        assert semaphore._value == 1
        assert await qi._maybe_run_isrctn({"intervention": INTERVENTION}) == []

    asyncio.run(run())


def test_live_api_slot_released_after_cancellation(monkeypatch):
    _prepare_resolvable_intervention(monkeypatch)
    semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(qi, "_LIVE_API_SEMAPHORE", semaphore)
    started = asyncio.Event()

    async def wait_forever(*_args):
        started.set()
        await asyncio.Event().wait()

    async def succeed(*_args):
        return []

    monkeypatch.setattr(qi, "_run_ctgov_live", wait_forever)
    monkeypatch.setattr(qi, "_run_isrctn_live", succeed)

    async def run():
        task = asyncio.create_task(qi._maybe_run_ctgov({"intervention": INTERVENTION}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert semaphore._value == 1
        assert await qi._maybe_run_isrctn({"intervention": INTERVENTION}) == []

    asyncio.run(run())


def test_live_api_slot_timeout_skips_only_that_source(monkeypatch, caplog):
    _prepare_resolvable_intervention(monkeypatch)
    monkeypatch.setattr(qi, "_LIVE_API_SEMAPHORE", asyncio.Semaphore(0))
    monkeypatch.setattr(qi, "LIVE_API_SEMAPHORE_TIMEOUT", 0.01)

    with caplog.at_level("INFO"):
        result = asyncio.run(qi._maybe_run_ctgov({"intervention": INTERVENTION}))

    assert result == []
    assert "live API concurrency slot unavailable" in caplog.text


def test_local_query_is_not_gated_by_live_api_semaphore(monkeypatch):
    monkeypatch.setattr(qi, "_LIVE_API_SEMAPHORE", asyncio.Semaphore(0))
    monkeypatch.setattr(qi, "LIVE_API_SEMAPHORE_TIMEOUT", 0.01)
    monkeypatch.setattr(qi, "get_concept_search_terms", lambda _uri: ["Test outcome"])
    monkeypatch.setattr(qi, "_run_unified_local_query", lambda _axes: [{"source": "local"}])
    monkeypatch.setattr(qi, "dedupe_and_sort_results", lambda results: results)

    results = asyncio.run(
        qi.async_query_axes(
            {"outcome": "https://universalevidence.com/vocab/states/TestOutcome"}
        )
    )

    assert results == [{"source": "local"}]
