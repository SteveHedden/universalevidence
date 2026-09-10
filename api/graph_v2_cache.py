"""Bounded single-flight cache for hierarchy-aware graph projections."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import gzip
import json
import random
import time
from typing import Awaitable, Callable, Literal


CACHE_NAMESPACE = "graph-v2-projection-1"
CacheStatus = Literal["hit", "miss", "coalesced", "bypass"]


@dataclass
class _Entry:
    payload: bytes
    expires_at: float


class GraphV2Cache:
    def __init__(
        self,
        *,
        namespace: str = CACHE_NAMESPACE,
        max_entries: int = 200,
        populated_ttl: float = 3600.0,
        empty_ttl: float = 600.0,
        partial_ttl: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.namespace = namespace
        self.max_entries = max_entries
        self.populated_ttl = populated_ttl
        self.empty_ttl = empty_ttl
        self.partial_ttl = partial_ttl
        self.clock = clock
        self.random_uniform = random_uniform
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[dict]] = {}
        self.hits = 0
        self.misses = 0
        self.coalesced = 0
        self.executions = 0
        self.bypasses = 0
        self.uncacheable = 0
        self.evictions = 0

    @staticmethod
    def canonical_key(identity: dict, namespace: str = CACHE_NAMESPACE) -> str:
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return f"{namespace}|{encoded}"

    def clear(self) -> None:
        self._entries.clear()
        self._inflight.clear()
        self.hits = self.misses = self.coalesced = self.executions = 0
        self.bypasses = self.uncacheable = self.evictions = 0

    def _lookup(self, key: str) -> dict | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self.clock():
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        return json.loads(gzip.decompress(entry.payload))

    def _store(self, key: str, payload: dict) -> None:
        meta = payload.get("meta") or {}
        partial = bool(meta.get("truncated") or meta.get("approximate"))
        if partial:
            base = self.partial_ttl
        elif payload.get("results") or any(
            edge.get("kind") == "evidence"
            for edge in (payload.get("edges") or ())
        ):
            base = self.populated_ttl
        else:
            base = self.empty_ttl
        jittered = base * (1.0 + self.random_uniform(-0.1, 0.1))
        # The contract's partial lifetime is an upper bound, not a nominal
        # value that positive jitter may extend beyond 60 seconds.
        ttl = min(base, jittered) if partial else jittered
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        self._entries[key] = _Entry(
            payload=gzip.compress(encoded, compresslevel=1),
            expires_at=self.clock() + ttl,
        )
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
            self.evictions += 1

    @staticmethod
    def cacheable(payload: dict) -> bool:
        """Return whether a response is a sound, contract-approved cache value.

        Deterministic graph/node caps and explicit source budget exhaustion are
        sound lower bounds. Transient source failures are intentionally retried
        instead of being retained for even the partial TTL.
        """
        meta = payload.get("meta") or {}
        if meta.get("sound") is False:
            return False
        sources = meta.get("sources") or {}
        return all(
            source.get("status") in {"included", "excluded"}
            and source.get("reason")
            in {
                None,
                "budget_exhausted",
                "source_limit",
                "unsupported_admin_level",
            }
            for source in sources.values()
        )

    async def get_or_execute_with_status(
        self,
        identity: dict,
        execute: Callable[[], Awaitable[dict]],
        *,
        enabled: bool = True,
    ) -> tuple[dict, CacheStatus]:
        if not enabled:
            self.bypasses += 1
            self.executions += 1
            return await execute(), "bypass"

        key = self.canonical_key(identity, self.namespace)
        cached = self._lookup(key)
        if cached is not None:
            self.hits += 1
            return cached, "hit"
        task = self._inflight.get(key)
        if task is not None:
            self.coalesced += 1
            return await asyncio.shield(task), "coalesced"

        self.misses += 1

        async def run() -> dict:
            self.executions += 1
            payload = await execute()
            if self.cacheable(payload):
                self._store(key, payload)
            else:
                self.uncacheable += 1
            return payload

        task = asyncio.create_task(run())
        self._inflight[key] = task

        def retire(completed: asyncio.Task) -> None:
            if self._inflight.get(key) is completed:
                self._inflight.pop(key, None)

        task.add_done_callback(retire)
        return await asyncio.shield(task), "miss"

    async def get_or_execute(
        self,
        identity: dict,
        execute: Callable[[], Awaitable[dict]],
    ) -> dict:
        payload, _status = await self.get_or_execute_with_status(identity, execute)
        return payload

    def stats(self) -> dict[str, int | str]:
        return {
            "cache_namespace": self.namespace,
            "entries": len(self._entries),
            "inflight": len(self._inflight),
            "hits": self.hits,
            "misses": self.misses,
            "coalesced_callers": self.coalesced,
            "backend_executions": self.executions,
            "backend_executions_avoided": self.hits + self.coalesced,
            "bypasses": self.bypasses,
            "uncacheable_responses": self.uncacheable,
            "evictions": self.evictions,
        }


graph_v2_cache = GraphV2Cache()
