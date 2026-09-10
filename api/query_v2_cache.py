"""Separate single-flight LRU cache for the query-v2 response envelope."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import gzip
import json
import os
import random
import time
from typing import Awaitable, Callable

try:
    from query_v2 import CanonicalQuery, QueryV2Execution, ExecutionProgress, ACTIVE_EXECUTION, RESPONSE_TIMEOUT_SECONDS
except ModuleNotFoundError:  # direct test import before route path setup
    from scripts.query_v2 import CanonicalQuery, QueryV2Execution, ExecutionProgress, ACTIVE_EXECUTION, RESPONSE_TIMEOUT_SECONDS

try:
    from query_runtime import REQUEST_DEADLINE, BACKGROUND_TASKS, bounded, observe, stage
except ModuleNotFoundError:
    from scripts.query_runtime import REQUEST_DEADLINE, BACKGROUND_TASKS, bounded, observe, stage


CACHE_NAMESPACE = "query-v2-envelope-1"


@dataclass
class _Entry:
    payload: bytes
    expires_at: float


class QueryV2Cache:
    def __init__(
        self,
        *,
        max_entries: int = 500,
        populated_ttl: float = 3600.0,
        empty_ttl: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_entries = max_entries
        self.populated_ttl = populated_ttl
        self.empty_ttl = empty_ttl
        self.clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[dict]] = {}
        self._progress: dict[str, ExecutionProgress] = {}
        self._storing: dict[str, asyncio.Task] = {}
        self.hits = 0
        self.misses = 0
        self.coalesced = 0
        self.executions = 0

    @staticmethod
    def canonical_key(query: CanonicalQuery) -> str:
        identity = {
            **query.identity(),
            # The cache is process-local and the deployment procedure restarts
            # the API after every Fuseki taxonomy/data reload. Explicit env
            # versions allow stronger invalidation without changing the HTTP
            # contract when versioned manifests are supplied.
            "taxonomy_version": os.getenv(
                "UE_TAXONOMY_VERSION", "process-local-fuseki"
            ),
            "dataset_version": os.getenv(
                "UE_DATASET_VERSION", "process-local-fuseki"
            ),
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return f"{CACHE_NAMESPACE}|{encoded}"

    def clear(self) -> None:
        self._entries.clear()
        self._inflight.clear()
        self._progress.clear()
        self._storing.clear()
        self.hits = self.misses = self.coalesced = self.executions = 0

    def _lookup(self, key: str) -> bytes | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self.clock():
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        return entry.payload

    async def _store(self, key: str, payload: dict) -> None:
        base = self.populated_ttl if payload.get("results") else self.empty_ttl
        ttl = base * (1.0 + random.uniform(-0.1, 0.1))
        def encode():
            return gzip.compress(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(), compresslevel=1)
        compressed = await asyncio.to_thread(encode)
        self._entries[key] = _Entry(
            payload=compressed,
            expires_at=self.clock() + ttl,
        )
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    async def get_or_execute(
        self,
        query: CanonicalQuery,
        execute: Callable[[CanonicalQuery], Awaitable[QueryV2Execution]],
    ) -> dict:
        deadline = REQUEST_DEADLINE.get()
        if deadline is None:
            deadline = time.monotonic() + RESPONSE_TIMEOUT_SECONDS
        key = self.canonical_key(query)
        stage("cache_lookup")
        cached = self._lookup(key)
        if cached is not None:
            self.hits += 1
            stage("cache_hit")
            try:
                return await bounded(asyncio.to_thread(lambda: json.loads(gzip.decompress(cached))),
                                     deadline - time.monotonic())
            except asyncio.TimeoutError:
                stage("cache_decode_deadline")
                return ExecutionProgress().snapshot()
        task = self._inflight.get(key)
        if task is not None:
            self.coalesced += 1
            progress = self._progress[key]
            stage("cache_coalesced")
        else:
            self.misses += 1
            progress = ExecutionProgress()

            async def run() -> dict:
                token = ACTIVE_EXECUTION.set(progress)
                deadline_token = REQUEST_DEADLINE.set(deadline)
                try:
                    self.executions += 1
                    execution = await execute(query)
                    progress.payload = execution.payload
                    if execution.cacheable:
                        async def store():
                            stage("cache_store_start")
                            await self._store(key, execution.payload)
                            stage("cache_store_end")
                        storing = asyncio.create_task(store(), name="query-v2-cache-store")
                        self._storing[key] = storing
                        observe(storing)
                        storing.add_done_callback(retire)
                    return execution.payload
                finally:
                    ACTIVE_EXECUTION.reset(token)
                    REQUEST_DEADLINE.reset(deadline_token)

            task = asyncio.create_task(run(), name="query-v2-shared")
            self._inflight[key] = task
            self._progress[key] = progress
            observe(task)

            def retire(completed: asyncio.Task) -> None:
                storing = self._storing.get(key)
                if self._inflight.get(key) is task and task.done() and (storing is None or storing.done()):
                    self._inflight.pop(key, None)
                    self._progress.pop(key, None)
                    self._storing.pop(key, None)

            task.add_done_callback(retire)
        # asyncio.wait never transfers an individual caller's cancellation to
        # the shared task. Its own execution deadline still bounds source work.
        stage("cache_wait_start")
        done, _ = await asyncio.wait({task}, timeout=max(0, deadline - time.monotonic()))
        stage("cache_wait_end", completed=bool(done))
        return task.result() if done else progress.snapshot()

    def stats(self) -> dict[str, int | str]:
        return {
            "cache_namespace": CACHE_NAMESPACE,
            "entries": len(self._entries),
            "inflight": len(self._inflight),
            "background_tasks": len(BACKGROUND_TASKS),
            "hits": self.hits,
            "misses": self.misses,
            "coalesced_callers": self.coalesced,
            "backend_executions": self.executions,
            "backend_executions_avoided": self.hits + self.coalesced,
        }


query_v2_cache = QueryV2Cache(
    max_entries=int(os.getenv("QUERY_V2_CACHE_MAX_ENTRIES", "500"))
)
