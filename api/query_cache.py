"""Bounded in-process query-result cache with single-flight protection."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import gzip
import hashlib
import json
import logging
import os
import random
import resource
import sys
import time
from typing import Awaitable, Callable, Protocol, cast


RESULT_CONTRACT_VERSION = "query-results-v2-gzip"
DEFAULT_MAX_ENTRIES = 2_000
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_POPULATED_TTL = 3_600.0
DEFAULT_EMPTY_TTL = 600.0
DEFAULT_TTL_JITTER = 0.10
DEFAULT_COMPRESSION_LEVEL = 1

logger = logging.getLogger(__name__)


class CacheableExecution(Protocol):
    results: list[dict]
    cacheable: bool


@dataclass
class CacheEntry:
    payload: bytes | list[dict]
    compressed: bool
    expires_at: float
    size_bytes: int
    raw_size_bytes: int
    row_count: int


def _current_rss_bytes() -> int:
    """Return current process RSS where available, with a portable fallback."""
    try:
        with open("/proc/self/statm", encoding="ascii") as statm:
            resident_pages = int(statm.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KiB; macOS reports bytes.
        return int(usage * 1024 if sys.platform.startswith("linux") else usage)


def _deep_size_bytes(value: object) -> int:
    """Estimate retained Python-object bytes, avoiding double-counting aliases."""
    seen: set[int] = set()

    def visit(item: object) -> int:
        item_id = id(item)
        if item_id in seen:
            return 0
        seen.add(item_id)
        size = sys.getsizeof(item)
        if isinstance(item, dict):
            size += sum(visit(key) + visit(child) for key, child in item.items())
        elif isinstance(item, (list, tuple, set, frozenset)):
            size += sum(visit(child) for child in item)
        return size

    return visit(value)


class QueryResultCache:
    """Process-local LRU cache.

    The production deployment intentionally runs one API worker. In-flight
    tasks are shielded so one disconnected caller cannot cancel work shared by
    other callers for the same key.
    """

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        populated_ttl: float = DEFAULT_POPULATED_TTL,
        empty_ttl: float = DEFAULT_EMPTY_TTL,
        ttl_jitter: float = DEFAULT_TTL_JITTER,
        compression_level: int = DEFAULT_COMPRESSION_LEVEL,
        compression_enabled: bool = True,
        measure_entries: bool = False,
        clock: Callable[[], float] = time.monotonic,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.populated_ttl = populated_ttl
        self.empty_ttl = empty_ttl
        self.ttl_jitter = ttl_jitter
        self.compression_level = compression_level
        self.compression_enabled = compression_enabled
        self.measure_entries = measure_entries
        self._clock = clock
        self._random_uniform = random_uniform
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self._size_bytes = 0
        self._inflight: dict[str, asyncio.Task] = {}
        self.reset_metrics()

    @staticmethod
    def canonical_key(axes: dict[str, str]) -> str:
        normalized = "&".join(
            f"{key.strip().casefold()}={value.strip()}"
            for key, value in sorted(axes.items(), key=lambda item: item[0].casefold())
        )
        return f"{RESULT_CONTRACT_VERSION}|{normalized}"

    def reset_metrics(self) -> None:
        self.hits = 0
        self.misses = 0
        self.coalesced_callers = 0
        self.backend_executions = 0
        self.lookup_count = 0
        self.lookup_latency_seconds = 0.0
        self.cold_query_count = 0
        self.cold_query_latency_seconds = 0.0
        self.uncacheable_results = 0
        self.evictions = 0
        self.store_candidates = 0
        self.raw_candidate_bytes = 0
        self.stored_candidate_bytes = 0
        self.serialization_seconds = 0.0
        self.compression_seconds = 0.0
        self.decompression_seconds = 0.0

    def clear(self) -> None:
        self._entries.clear()
        self._size_bytes = 0
        self._inflight.clear()
        self.reset_metrics()

    def _lookup(self, key: str) -> list[dict] | None:
        lookup_started = self._clock()
        entry = self._entries.get(key)
        self.lookup_count += 1
        try:
            if entry is None:
                return None
            if entry.expires_at <= self._clock():
                del self._entries[key]
                self._size_bytes -= entry.size_bytes
                return None
            self._entries.move_to_end(key)
            if not entry.compressed:
                return cast(list[dict], entry.payload)
            decode_started = self._clock()
            try:
                return json.loads(gzip.decompress(cast(bytes, entry.payload)))
            finally:
                self.decompression_seconds += self._clock() - decode_started
        finally:
            self.lookup_latency_seconds += self._clock() - lookup_started

    def _ttl_for(self, result: list[dict]) -> float:
        base = self.populated_ttl if result else self.empty_ttl
        jitter = self._random_uniform(-self.ttl_jitter, self.ttl_jitter)
        return base * (1.0 + jitter)

    def _store(self, key: str, result: list[dict]) -> None:
        started = self._clock()
        raw_payload = json.dumps(
            result, separators=(",", ":"), ensure_ascii=False, default=str
        ).encode()
        self.serialization_seconds += self._clock() - started

        started = self._clock()
        payload = (
            gzip.compress(raw_payload, compresslevel=self.compression_level)
            if self.compression_enabled
            else result
        )
        self.compression_seconds += self._clock() - started

        raw_size_bytes = len(raw_payload)
        size_bytes = len(payload) if self.compression_enabled else raw_size_bytes
        self.store_candidates += 1
        self.raw_candidate_bytes += raw_size_bytes
        self.stored_candidate_bytes += size_bytes

        if self.measure_entries:
            study_rows: dict[str, int] = {}
            for row in result:
                if str(row.get("source", "")).casefold() not in {
                    "ct.gov",
                    "clinicaltrials.gov",
                }:
                    continue
                study_id = str(row.get("study_id") or "")
                if study_id:
                    study_rows[study_id] = study_rows.get(study_id, 0) + 1
            logger.warning(
                "cache_store_measurement %s",
                json.dumps(
                    {
                        "key_hash": hashlib.sha256(key.encode()).hexdigest()[:16],
                        "rows": len(result),
                        "raw_json_bytes": raw_size_bytes,
                        "stored_bytes": size_bytes,
                        "storage_representation": (
                            "gzip-json" if self.compression_enabled else "python-object"
                        ),
                        "estimated_python_bytes": _deep_size_bytes(result),
                        "ctgov_duplicate_rows": sum(
                            count - 1 for count in study_rows.values() if count > 1
                        ),
                        "over_ceiling": size_bytes > self.max_bytes,
                    },
                    separators=(",", ":"),
                ),
            )

        if size_bytes > self.max_bytes:
            self.uncacheable_results += 1
            return
        replaced = self._entries.pop(key, None)
        if replaced is not None:
            self._size_bytes -= replaced.size_bytes
        self._entries[key] = CacheEntry(
            payload=payload,
            compressed=self.compression_enabled,
            expires_at=self._clock() + self._ttl_for(result),
            size_bytes=size_bytes,
            raw_size_bytes=raw_size_bytes,
            row_count=len(result),
        )
        self._size_bytes += size_bytes
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries or self._size_bytes > self.max_bytes:
            _, evicted = self._entries.popitem(last=False)
            self._size_bytes -= evicted.size_bytes
            self.evictions += 1

    async def _execute(
        self,
        key: str,
        axes: dict[str, str],
        fetcher: Callable[[dict[str, str]], Awaitable[CacheableExecution]],
    ) -> list[dict]:
        self.backend_executions += 1
        self.cold_query_count += 1
        started = self._clock()
        try:
            execution = await fetcher(axes)
        finally:
            self.cold_query_latency_seconds += self._clock() - started
        if execution.cacheable:
            self._store(key, execution.results)
        else:
            self.uncacheable_results += 1
        return execution.results

    async def get_or_execute(
        self,
        axes: dict[str, str],
        fetcher: Callable[[dict[str, str]], Awaitable[CacheableExecution]],
    ) -> list[dict]:
        key = self.canonical_key(axes)
        cached = self._lookup(key)
        if cached is not None:
            self.hits += 1
            return cached

        task = self._inflight.get(key)
        if task is not None:
            self.coalesced_callers += 1
            return await asyncio.shield(task)

        self.misses += 1
        task = asyncio.create_task(self._execute(key, dict(axes), fetcher))
        self._inflight[key] = task

        def retire(completed: asyncio.Task) -> None:
            if self._inflight.get(key) is completed:
                self._inflight.pop(key, None)

        task.add_done_callback(retire)
        return await asyncio.shield(task)

    def stats(self) -> dict[str, int | float | str]:
        process_usage = resource.getrusage(resource.RUSAGE_SELF)
        attempted = self.hits + self.misses + self.coalesced_callers
        hit_ratio = self.hits / attempted if attempted else 0.0
        avg_lookup_ms = (
            self.lookup_latency_seconds / self.lookup_count * 1_000
            if self.lookup_count
            else 0.0
        )
        avg_cold_ms = (
            self.cold_query_latency_seconds / self.cold_query_count * 1_000
            if self.cold_query_count
            else 0.0
        )
        avg_decompression_ms = (
            self.decompression_seconds / self.hits * 1_000 if self.hits else 0.0
        )
        return {
            "result_contract_version": RESULT_CONTRACT_VERSION,
            "entries": len(self._entries),
            "max_entries": self.max_entries,
            "approximate_bytes": self._size_bytes,
            "stored_bytes": self._size_bytes,
            "storage_representation": (
                "gzip-json" if self.compression_enabled else "python-object"
            ),
            "max_bytes": self.max_bytes,
            "process_rss_bytes": _current_rss_bytes(),
            "process_user_cpu_seconds": round(process_usage.ru_utime, 6),
            "process_system_cpu_seconds": round(process_usage.ru_stime, 6),
            "inflight": len(self._inflight),
            "hits": self.hits,
            "misses": self.misses,
            "hit_ratio": round(hit_ratio, 6),
            "coalesced_callers": self.coalesced_callers,
            "backend_executions": self.backend_executions,
            "backend_executions_avoided": self.hits + self.coalesced_callers,
            "uncacheable_results": self.uncacheable_results,
            "evictions": self.evictions,
            "store_candidates": self.store_candidates,
            "raw_candidate_bytes": self.raw_candidate_bytes,
            "stored_candidate_bytes": self.stored_candidate_bytes,
            "candidate_storage_ratio": round(
                self.stored_candidate_bytes / self.raw_candidate_bytes, 6
            )
            if self.raw_candidate_bytes
            else 0.0,
            "total_serialization_ms": round(self.serialization_seconds * 1_000, 3),
            "total_compression_ms": round(self.compression_seconds * 1_000, 3),
            "average_hit_decompression_ms": round(avg_decompression_ms, 6),
            "average_lookup_latency_ms": round(avg_lookup_ms, 6),
            "average_cold_query_latency_ms": round(avg_cold_ms, 3),
        }


query_result_cache = QueryResultCache(
    compression_enabled=os.getenv("CACHE_COMPRESSION_ENABLED", "true").casefold()
    in {"1", "true", "yes", "on"},
    measure_entries=os.getenv("CACHE_MEASURE_ENTRY_SIZES", "").casefold()
    in {"1", "true", "yes", "on"}
)
