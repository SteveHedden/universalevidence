from __future__ import annotations

import asyncio
from dataclasses import dataclass

import requests

from api.cache_warming import build_warm_list, top_level_axes, ui_default_axes
from api.query_cache import QueryResultCache
import scripts.query_interventions as qi


@dataclass
class Execution:
    results: list[dict]
    cacheable: bool = True


def test_single_flight_coalesces_100_identical_cold_requests():
    async def run():
        cache = QueryResultCache(ttl_jitter=0)
        calls = 0

        async def fetch(_axes):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return Execution([{"study_id": "one"}])

        responses = await asyncio.gather(
            *(cache.get_or_execute({"state": "urn:test"}, fetch) for _ in range(100))
        )

        assert calls == 1
        assert all(response == [{"study_id": "one"}] for response in responses)
        assert cache.stats()["coalesced_callers"] == 99
        assert cache.stats()["backend_executions_avoided"] == 99

    asyncio.run(run())


def test_canonical_key_ignores_axis_order_and_contract_version_is_present():
    cache = QueryResultCache()
    first = cache.canonical_key({"region": " R ", "state": "S"})
    second = cache.canonical_key({"state": "S", "region": "R"})

    assert first == second
    assert first.startswith("query-results-v2-gzip|")


def test_populated_and_empty_ttl_expire_independently():
    now = [0.0]
    cache = QueryResultCache(
        populated_ttl=100,
        empty_ttl=10,
        ttl_jitter=0,
        clock=lambda: now[0],
    )
    calls = {"full": 0, "empty": 0}

    async def full(_axes):
        calls["full"] += 1
        return Execution([{"study_id": calls["full"]}])

    async def empty(_axes):
        calls["empty"] += 1
        return Execution([])

    async def run():
        await cache.get_or_execute({"state": "full"}, full)
        await cache.get_or_execute({"state": "empty"}, empty)
        now[0] = 11
        await cache.get_or_execute({"state": "full"}, full)
        await cache.get_or_execute({"state": "empty"}, empty)

    asyncio.run(run())
    assert calls == {"full": 1, "empty": 2}


def test_lru_is_bounded_by_entry_count_and_approximate_bytes():
    cache = QueryResultCache(max_entries=2, max_bytes=100, ttl_jitter=0)

    async def fetch(axes):
        return Execution([{"value": axes["state"]}])

    async def run():
        await cache.get_or_execute({"state": "one"}, fetch)
        await cache.get_or_execute({"state": "two"}, fetch)
        await cache.get_or_execute({"state": "three"}, fetch)

    asyncio.run(run())
    stats = cache.stats()
    assert stats["entries"] <= 2
    assert stats["approximate_bytes"] <= 100


def test_cache_stores_only_compressed_payload_and_reconstructs_results():
    cache = QueryResultCache(ttl_jitter=0)
    original = [{"study_id": "one", "title": "repeated " * 1_000}]

    async def fetch(_axes):
        return Execution(original)

    async def run():
        first = await cache.get_or_execute({"state": "compressed"}, fetch)
        second = await cache.get_or_execute({"state": "compressed"}, fetch)
        return first, second

    first, second = asyncio.run(run())
    entry = next(iter(cache._entries.values()))

    assert first == second == original
    assert isinstance(entry.payload, bytes)
    assert not hasattr(entry, "result")
    assert entry.size_bytes < entry.raw_size_bytes
    assert cache.stats()["storage_representation"] == "gzip-json"


def test_cached_result_is_isolated_from_caller_mutation():
    cache = QueryResultCache(ttl_jitter=0)
    calls = 0

    async def fetch(_axes):
        nonlocal calls
        calls += 1
        return Execution([{"study_id": "one", "outcomes": ["original"]}])

    async def run():
        first = await cache.get_or_execute({"state": "mutation"}, fetch)
        first[0]["outcomes"].append("changed")
        return await cache.get_or_execute({"state": "mutation"}, fetch)

    second = asyncio.run(run())
    assert second == [{"study_id": "one", "outcomes": ["original"]}]
    assert calls == 1


def test_cache_stats_report_compression_and_process_memory():
    cache = QueryResultCache(ttl_jitter=0)

    async def fetch(_axes):
        return Execution([{"title": "compressible " * 1_000}])

    asyncio.run(cache.get_or_execute({"state": "stats"}, fetch))
    stats = cache.stats()

    assert stats["store_candidates"] == 1
    assert stats["raw_candidate_bytes"] > stats["stored_candidate_bytes"]
    assert 0 < stats["candidate_storage_ratio"] < 1
    assert stats["process_rss_bytes"] > 0


def test_compression_can_be_disabled_for_matched_baseline_measurement():
    cache = QueryResultCache(compression_enabled=False, ttl_jitter=0)

    async def fetch(_axes):
        return Execution([{"title": "baseline " * 1_000}])

    asyncio.run(cache.get_or_execute({"state": "baseline"}, fetch))
    entry = next(iter(cache._entries.values()))
    stats = cache.stats()

    assert entry.compressed is False
    assert entry.payload == [{"title": "baseline " * 1_000}]
    assert stats["storage_representation"] == "python-object"
    assert stats["stored_candidate_bytes"] == stats["raw_candidate_bytes"]
    assert stats["candidate_storage_ratio"] == 1.0


def test_exceptions_and_degraded_results_are_not_cached():
    cache = QueryResultCache(ttl_jitter=0)
    attempts = 0

    async def raises(_axes):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("temporary")

    async def degraded(_axes):
        nonlocal attempts
        attempts += 1
        return Execution([{"source": "AEA"}], cacheable=False)

    async def run():
        for _ in range(2):
            try:
                await cache.get_or_execute({"state": "error"}, raises)
            except RuntimeError:
                pass
        first = await cache.get_or_execute({"state": "degraded"}, degraded)
        second = await cache.get_or_execute({"state": "degraded"}, degraded)
        assert first == second == [{"source": "AEA"}]

    asyncio.run(run())
    assert attempts == 4
    assert cache.stats()["entries"] == 0
    assert cache.stats()["uncacheable_results"] == 2


def test_swallowed_ctgov_timeout_marks_execution_uncacheable(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise requests.Timeout("temporary CT.gov outage")

    monkeypatch.setattr(qi.requests, "get", timeout)

    async def only_ctgov(_axes):
        return await qi._run_ctgov_live({}, "", None, None, None)

    execution = asyncio.run(
        qi.async_query_axes_with_metadata({"condition": "urn:test"}, only_ctgov)
    )

    assert execution.results == []
    assert execution.source_status["ctgov"] == "error"
    assert execution.cacheable is False


def test_swallowed_ctgov_timeout_is_returned_but_retried_not_cached(monkeypatch):
    cache = QueryResultCache(ttl_jitter=0)
    calls = 0

    def timeout(*_args, **_kwargs):
        raise requests.Timeout("temporary CT.gov outage")

    monkeypatch.setattr(qi.requests, "get", timeout)

    async def fetch(axes):
        nonlocal calls
        calls += 1

        async def degraded(_axes):
            local = [{"source": "AEA", "study_id": "still-returned"}]
            await qi._run_ctgov_live({}, "", None, None, None)
            return local

        return await qi.async_query_axes_with_metadata(axes, degraded)

    async def run():
        first = await cache.get_or_execute({"state": "urn:test"}, fetch)
        second = await cache.get_or_execute({"state": "urn:test"}, fetch)
        return first, second

    first, second = asyncio.run(run())
    assert first == second == [{"source": "AEA", "study_id": "still-returned"}]
    assert calls == 2
    assert cache.stats()["entries"] == 0


def test_warm_list_uses_defaults_match_counts_and_real_top_levels():
    defaults = ui_default_axes(
        'state: { uri: "https://universalevidence.com/vocab/states/Malaria"'
    )
    trees = {
        "condition": [
            {
                "collection": True,
                "children": [{"uri": "urn:state-root", "children": []}],
            }
        ],
        "intervention": [{"uri": "urn:intervention-root", "children": []}],
    }
    top_levels = top_level_axes(trees)
    rows = [
        {"axis": "state", "concept": f"urn:popular-{index}", "matches": str(100 - index)}
        for index in range(75)
    ]

    warm_list = build_warm_list(rows, defaults, top_levels, limit=100)

    assert warm_list[0] == {
        "state": "https://universalevidence.com/vocab/states/Malaria"
    }
    assert {"state": "urn:popular-0"} in warm_list
    assert {"state": "urn:state-root"} in warm_list
    assert {"intervention": "urn:intervention-root"} in warm_list
    assert 50 <= len(warm_list) <= 100
