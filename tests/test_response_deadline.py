"""End-to-end ASGI regressions: no upstream services or wall-clock sleeps in production."""
import asyncio
import importlib
import threading
import time

import httpx
import pytest
from fastapi import FastAPI
from rdflib import Graph

import api.routes.query_v2 as route
from api.query_v2_cache import QueryV2Cache

q = importlib.import_module(route.execute_query_v2.__module__)
runtime = importlib.import_module(route.stage.__module__)
qi = importlib.import_module(route.await_query_or_disconnect.__module__)
STUNTING = 'https://universalevidence.com/vocab/states/Stunting'


def payload(rows=None):
    rows = rows or []
    return q.annotate_completion({'results': rows, 'meta': {
        'api_version': 'query-v2', 'returned_unique_studies': len(rows),
        'limit_per_source_branch': 100, 'truncated': False, 'approximate': False,
        'sources': {source: q._source_meta(status='included', coverage='full',
                    count=sum(row['source'] == q.SOURCE_LABELS[source] for row in rows))
                    for source in q.SOURCE_IDS},
    }})


def setup(monkeypatch, seconds=.15):
    monkeypatch.setattr(route, 'RESPONSE_TIMEOUT_SECONDS', seconds)
    cache = QueryV2Cache()
    monkeypatch.setattr(route, 'query_v2_cache', cache)
    app = FastAPI()
    app.include_router(route.router)
    return app, cache


async def get(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        return await client.get('/query/v2', params={'state': STUNTING})


async def drain():
    for _ in range(30):
        await asyncio.sleep(.005)
        if not runtime.BACKGROUND_TASKS and not qi._DETACHED_QUERY_TASKS:
            return
    assert not runtime.BACKGROUND_TASKS
    assert not qi._DETACHED_QUERY_TASKS


def test_ready_asgi_response_never_awaits_cancellation_resistant_watcher(monkeypatch):
    app, cache = setup(monkeypatch)

    async def run():
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def check(_request):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return True

        async def execute(query):
            await entered.wait()
            return q.QueryV2Execution(payload([{'source': 'AEA', 'study_id': 'ready'}]))

        monkeypatch.setattr(route.Request, 'is_disconnected', check)
        monkeypatch.setattr(route, 'execute_query_v2', execute)
        task = asyncio.create_task(get(app))
        try:
            await asyncio.wait_for(cancelled.wait(), .5)
            done, _ = await asyncio.wait({task}, timeout=.1)
            assert done, 'Ready result is blocked by disconnect cleanup'
            assert task.result().json()['results'][0]['study_id'] == 'ready'
            assert runtime.BACKGROUND_TASKS  # cleanup still owned, not abandoned
        finally:
            release.set()
            await task
            await drain()

    asyncio.run(run())


def test_swallowed_cancel_does_not_restart_disconnect_polling(monkeypatch):
    async def run():
        entered, ready = asyncio.Event(), asyncio.Event()
        checks = 0
        class Request:
            async def is_disconnected(self):
                nonlocal checks
                checks += 1
                entered.set()
                try:
                    await ready.wait()
                except asyncio.CancelledError:
                    return False
                return False
        async def query():
            await entered.wait()
            return 'ready'
        assert await qi.await_query_or_disconnect(query(), Request(), poll_interval=0) == 'ready'
        await drain()
        assert checks == 1
    asyncio.run(run())


@pytest.mark.parametrize('planning_stage', ['region', 'concept'])
def test_deadline_includes_planning_and_returns_timeout_not_empty_success(monkeypatch, planning_stage):
    app, cache = setup(monkeypatch, .08)
    async def slow(*args):
        await asyncio.sleep(2)
        return q.RegionIndex(Graph())
    async def region(*args):
        return q.RegionIndex(Graph())
    monkeypatch.setattr(q, 'region_index_for_query', slow if planning_stage == 'region' else region)
    if planning_stage == 'concept': monkeypatch.setattr(q, 'resolve_concept', slow)
    async def run():
        started = time.monotonic()
        response = await get(app)
        assert time.monotonic() - started < .15
        data = response.json()
        assert response.status_code == 200
        assert data['results'] == []
        assert data['meta']['execution_status'] == 'timeout'
        assert set(data['meta']['timed_out_sources']) == set(q.SOURCE_IDS)
        assert data['meta']['completed_sources'] == []
        await drain()
        assert cache.stats()['inflight'] == 0
        assert cache.stats()['entries'] == 0
    asyncio.run(run())


def test_healthy_results_survive_stalled_source_and_cleanup_holds_permit(monkeypatch):
    app, cache = setup(monkeypatch, .10)
    async def run():
        release, cancelled = asyncio.Event(), asyncio.Event()
        semaphore = asyncio.Semaphore(1)
        class Adapter:
            def __init__(self, source): self.source_id = source
            async def execute(self, spec, budget, regions):
                if self.source_id == 'isrctn':
                    async with semaphore:
                        try: await asyncio.Future()
                        except asyncio.CancelledError:
                            cancelled.set()
                            await release.wait()
                return q.BranchResult(rows=[{'source': q.SOURCE_LABELS[self.source_id], 'study_id': self.source_id}])
        async def execute(query):
            return await q.execute_query_v2(query, adapters={s: Adapter(s) for s in q.SOURCE_IDS}, regions=q.RegionIndex(Graph()))
        monkeypatch.setattr(route, 'execute_query_v2', execute)
        try:
            started = time.monotonic()
            response = await get(app)
            assert time.monotonic() - started < .18
            data = response.json()
            assert {row['study_id'] for row in data['results']} == {'aea','ctgov','who-ictrp'}
            assert data['meta']['execution_status'] == 'partial'
            assert data['meta']['timed_out_sources'] == ['isrctn']
            await asyncio.wait_for(cancelled.wait(), .1)
            assert semaphore._value == 0
            assert cache.stats()['entries'] == 0
        finally:
            release.set()
            await drain()
        assert semaphore._value == 1
        assert cache.stats()['inflight'] == 0
    asyncio.run(run())


def test_finished_empty_search_is_complete(monkeypatch):
    app, cache = setup(monkeypatch)
    async def execute(query): return q.QueryV2Execution(payload())
    monkeypatch.setattr(route, 'execute_query_v2', execute)
    async def run():
        response = await get(app)
        assert response.json()['meta']['execution_status'] == 'complete'
        assert response.json()['meta']['timed_out_sources'] == []
        await drain()
    asyncio.run(run())


def test_cold_warm_and_simultaneous_stunting_share_one_execution(monkeypatch):
    app, cache = setup(monkeypatch, .3)
    calls = 0
    async def execute(query):
        nonlocal calls
        calls += 1
        await asyncio.sleep(.01)
        return q.QueryV2Execution(payload([{'source':'AEA','study_id':'stunting'}]))
    monkeypatch.setattr(route, 'execute_query_v2', execute)
    async def run():
        responses = await asyncio.gather(*(get(app) for _ in range(8)))
        await drain()
        warm = await get(app)
        assert calls == 1
        assert all(r.json() == warm.json() for r in responses)
        assert cache.stats()['coalesced_callers'] == 7
        assert cache.stats()['hits'] == 1
        await drain()
    asyncio.run(run())


def test_disconnecting_caller_does_not_cancel_shared_query(monkeypatch):
    app, cache = setup(monkeypatch, .3)
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def execute(query):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return q.QueryV2Execution(payload([{'source':'AEA','study_id':'shared'}]))
        monkeypatch.setattr(route, 'execute_query_v2', execute)
        async def disconnected(request):
            return request.headers.get('x-test-disconnect') == 'yes' and started.is_set()
        monkeypatch.setattr(route.Request, 'is_disconnected', disconnected)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            abandoned = asyncio.create_task(client.get('/query/v2',params={'state':STUNTING},headers={'x-test-disconnect':'yes'}))
            await started.wait()
            remaining = asyncio.create_task(get(app))
            response = await abandoned
            assert response.status_code == 499
            assert not remaining.done()
            release.set()
            assert (await remaining).json()['results'][0]['study_id'] == 'shared'
        assert calls == 1
        await drain()
        assert cache.stats()['inflight'] == 0
    asyncio.run(run())


def test_cache_store_cannot_hold_ready_response(monkeypatch):
    app, cache = setup(monkeypatch, .3)
    async def run():
        release = asyncio.Event()
        async def store(*args): await release.wait()
        async def execute(query): return q.QueryV2Execution(payload([{'source':'AEA','study_id':'ready'}]))
        monkeypatch.setattr(cache, '_store', store)
        monkeypatch.setattr(route, 'execute_query_v2', execute)
        try:
            started = time.monotonic()
            response = await get(app)
            assert time.monotonic()-started < .15
            assert response.json()['results'][0]['study_id'] == 'ready'
            assert response.json()['meta']['execution_status'] == 'complete'
            assert cache.stats()['inflight'] == 1  # coalesce while store finishes
        finally:
            release.set()
            await drain()
    asyncio.run(run())


def test_serialization_is_inside_total_response_deadline(monkeypatch):
    app, cache = setup(monkeypatch, .08)
    release = threading.Event()
    original = route.encode_response
    def slow_encode(data):
        release.wait(2)
        return original(data)
    async def execute(query): return q.QueryV2Execution(payload([{'source':'AEA','study_id':'ready'}]))
    monkeypatch.setattr(route, 'execute_query_v2', execute)
    monkeypatch.setattr(route, 'encode_response', slow_encode)
    async def run():
        try:
            started = time.monotonic()
            response = await get(app)
            assert time.monotonic()-started < .15
            assert response.json()['meta']['execution_status'] == 'timeout'
            assert response.json()['meta']['response_timeout_stage'] == 'serialization'
        finally:
            release.set()
            await drain()
    asyncio.run(run())


def test_timeout_config_rejects_nonfinite_or_nonpositive_values(monkeypatch):
    for value in ['0', '-1', 'NaN', 'inf', 'invalid']:
        monkeypatch.setenv('TEST_DEADLINE',value)
        assert q._positive_float_env('TEST_DEADLINE',10) == 10
    monkeypatch.setenv('TEST_DEADLINE','7.5')
    assert q._positive_float_env('TEST_DEADLINE',10) == 7.5


def test_shorter_joining_caller_deadline_does_not_cancel_owner(monkeypatch):
    app, cache = setup(monkeypatch, .3)
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def execute(query):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return q.QueryV2Execution(payload([{'source': 'AEA', 'study_id': 'owner'}]))
        monkeypatch.setattr(route, 'execute_query_v2', execute)
        owner = asyncio.create_task(get(app))
        await started.wait()
        monkeypatch.setattr(route, 'RESPONSE_TIMEOUT_SECONDS', .05)
        joined = await get(app)
        assert joined.json()['meta']['execution_status'] == 'timeout'
        assert not owner.done()
        release.set()
        assert (await owner).json()['results'][0]['study_id'] == 'owner'
        assert calls == 1
        await drain()
    asyncio.run(run())


def test_partial_source_preserves_finished_or_clause_but_not_unfinished_and(monkeypatch):
    async def run(logic):
        release = asyncio.Event()
        class Adapter:
            source_id = 'ctgov'
            calls = 0
            async def execute(self, spec, budget, regions):
                self.calls += 1
                if self.calls > 1: await release.wait()
                return q.BranchResult(rows=[{'source':'CT.gov','study_id':'safe-clause'}])
        query = q.canonical_query({'state':[STUNTING, STUNTING+'Other']},{'state':logic})
        progress = q.ExecutionProgress()
        token = q.ACTIVE_EXECUTION.set(progress)
        task = asyncio.create_task(q.execute_source(query, Adapter(), q.RegionIndex(Graph())))
        try:
            await asyncio.sleep(.03)
            snapshot = progress.snapshot()
            assert bool(snapshot['results']) is (logic == 'or')
            assert snapshot['meta']['timed_out_sources'] == list(q.SOURCE_IDS)
        finally:
            task.cancel()
            release.set()
            try: await task
            except asyncio.CancelledError: pass
            q.ACTIVE_EXECUTION.reset(token)
            await drain()
    asyncio.run(run('or'))
    asyncio.run(run('and'))


def test_actual_starlette_polling_does_not_hold_ready_asgi_responses(monkeypatch):
    app, cache = setup(monkeypatch, .3)
    async def execute(query):
        await asyncio.sleep(.002)
        return q.QueryV2Execution(payload([{'source':'AEA','study_id':'ready'}]))
    monkeypatch.setattr(route, 'execute_query_v2', execute)
    async def run():
        # Repeated actual Request polling complements the deterministic swallowed-
        # cancellation regression; every response must finish before disconnection.
        for _ in range(5):
            cache.clear()
            tasks = [asyncio.create_task(get(app)) for _ in range(20)]
            done, pending = await asyncio.wait(tasks, timeout=.2)
            assert not pending
            assert all(task.result().json()['results'] for task in done)
            await drain()
    asyncio.run(run())


def test_cancelled_planning_waiter_retires_shared_thread_result(monkeypatch):
    app, cache = setup(monkeypatch, .07)
    release = threading.Event()
    q._CONCEPT_CACHE.pop(STUNTING, None)
    async def region(*args): return q.RegionIndex(Graph())
    def slow_local(uri):
        release.wait(2)
        return q.ConceptResolution('Stunting', ('Stunting',), ())
    monkeypatch.setattr(q, 'region_index_for_query', region)
    monkeypatch.setattr(q, '_resolve_local_concept', slow_local)
    async def run():
        try:
            response = await get(app)
            assert response.json()['meta']['execution_status'] == 'timeout'
            assert STUNTING in q._CONCEPT_INFLIGHT
        finally:
            release.set()
            await drain()
        assert STUNTING not in q._CONCEPT_INFLIGHT
        assert q._CONCEPT_CACHE[STUNTING].label == 'Stunting'
    asyncio.run(run())
    q._CONCEPT_CACHE.pop(STUNTING, None)
