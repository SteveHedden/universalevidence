#!/usr/bin/env python3
"""Test-only live-source concurrency sweep.

Runs directly against the live adapters, bypassing the public query route,
local-query admission gate, and result cache. It changes only this process's
copy of the live semaphore; the deployed API process is untouched.
"""

from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
from pathlib import Path
import statistics
import sys
import threading
import time
from typing import Any, Awaitable, Callable

import httpx


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import fetch_isrctn  # noqa: E402
import query_interventions as qi  # noqa: E402


LEVELS = (2, 4, 8, 15)
LOGICAL_TIMEOUT_SECONDS = 30.0
DEFAULT_DURATION_SECONDS = 180.0
CURRENT_CALL_ID: ContextVar[int | None] = ContextVar("live_sweep_call_id", default=None)

CORPUS_QUERY = """
PREFIX ue: <https://universalevidence.com/ontology/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT DISTINCT ?kind ?concept WHERE {
  GRAPH <https://universalevidence.com/graph/ue-taxonomy> {
    {
      ?concept a ue:State ; skos:prefLabel ?label .
      BIND("condition" AS ?kind)
    }
    UNION
    {
      ?concept a ue:Intervention ; skos:prefLabel ?label .
      BIND("intervention" AS ?kind)
    }
  }
}
ORDER BY ?kind STR(?concept)
"""


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction) - 1))
    return round(ordered[index], 4)


@dataclass
class RawRecord:
    call_id: int | None
    status: int | None
    elapsed: float
    error: str | None


@dataclass
class RawCollector:
    records: list[RawRecord] = field(default_factory=list)
    active: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def begin(self) -> None:
        with self._lock:
            self.active += 1

    def finish(
        self,
        *,
        call_id: int | None,
        status: int | None,
        elapsed: float,
        error: str | None,
    ) -> None:
        with self._lock:
            self.records.append(RawRecord(call_id, status, elapsed, error))
            self.active -= 1

    def active_count(self) -> int:
        with self._lock:
            return self.active


class InstrumentedAsyncClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        collector: RawCollector,
        throttle_seen: asyncio.Event,
    ):
        self.client = client
        self.collector = collector
        self.throttle_seen = throttle_seen

    async def get(self, *args: Any, **kwargs: Any) -> httpx.Response:
        started = time.monotonic()
        self.collector.begin()
        try:
            response = await self.client.get(*args, **kwargs)
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            self.collector.finish(
                call_id=CURRENT_CALL_ID.get(),
                status=status,
                elapsed=time.monotonic() - started,
                error=type(exc).__name__,
            )
            if status == 429:
                self.throttle_seen.set()
            raise
        self.collector.finish(
            call_id=CURRENT_CALL_ID.get(),
            status=response.status_code,
            elapsed=time.monotonic() - started,
            error=None,
        )
        if response.status_code == 429:
            self.throttle_seen.set()
        return response


def build_corpus() -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Select exactly 50 conditions and 50 interventions usable by both sources."""
    rows = qi.sparql_select(CORPUS_QUERY)
    conditions: list[dict[str, str]] = []
    interventions: list[dict[str, str]] = []
    mesh_map: dict[str, list[str]] = {}
    terms_map: dict[str, list[str]] = {}
    label_map: dict[str, str] = {}

    for row in rows:
        kind = row.get("kind")
        uri = row.get("concept")
        if not uri:
            continue
        if kind == "condition" and len(conditions) < 50:
            meshes = qi.extract_mesh_exact_matches(uri)
            label = qi.get_concept_label(uri)
            if meshes and label:
                conditions.append({"condition": uri})
                mesh_map[uri] = meshes
                label_map[uri] = label
        elif kind == "intervention" and len(interventions) < 50:
            terms = qi.get_concept_search_terms(uri)
            label = qi.get_concept_label(uri)
            if terms and label:
                interventions.append({"intervention": uri})
                terms_map[uri] = terms
                label_map[uri] = label
        if len(conditions) == 50 and len(interventions) == 50:
            break

    if len(conditions) != 50 or len(interventions) != 50:
        raise RuntimeError(
            f"Could not build exact corpus: conditions={len(conditions)}, "
            f"interventions={len(interventions)}"
        )
    return conditions + interventions, {
        "condition_meshes": mesh_map,
        "intervention_terms": terms_map,
        "labels": label_map,
    }


def install_resolver_snapshot(snapshot: dict[str, Any]) -> None:
    """Remove repeated local Fuseki resolution work from the timed live-source sweep."""
    mesh_map = snapshot["condition_meshes"]
    terms_map = snapshot["intervention_terms"]
    label_map = snapshot["labels"]
    qi.extract_mesh_exact_matches = lambda uri: list(mesh_map.get(uri, []))
    qi.get_concept_search_terms = lambda uri: list(terms_map.get(uri, []))
    qi.get_concept_label = lambda uri: label_map.get(uri)


async def wait_for_raw_drain(collector: RawCollector) -> None:
    deadline = time.monotonic() + LOGICAL_TIMEOUT_SECONDS + 5
    while collector.active_count() and time.monotonic() < deadline:
        await asyncio.sleep(0.1)


def summarize_raw(records: list[RawRecord], wall: float) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    for record in records:
        key = str(record.status) if record.status is not None else "transport_error"
        statuses[key] = statuses.get(key, 0) + 1
    latencies = [record.elapsed for record in records]
    throttled = sum(1 for record in records if record.status == 429)
    errors = sum(
        1
        for record in records
        if record.error is not None
        or record.status is None
        or record.status >= 400
    )
    return {
        "attempted": len(records),
        "completed": len(records) - errors,
        "errors": errors,
        "throttled_429": throttled,
        "status_counts": statuses,
        "throughput_http_per_second": round(len(records) / wall, 4),
        "p50_seconds": percentile(latencies, 0.50),
        "p95_seconds": percentile(latencies, 0.95),
        "p99_seconds": percentile(latencies, 0.99),
        "max_seconds": round(max(latencies), 4) if latencies else None,
    }


async def run_level(
    *,
    source: str,
    level: int,
    duration: float,
    corpus: list[dict[str, str]],
) -> dict[str, Any]:
    qi._LIVE_API_SEMAPHORE = asyncio.Semaphore(level)
    collector = RawCollector()
    logical: list[dict[str, Any]] = []
    logical_lock = asyncio.Lock()
    next_call_id = 0
    stop_at = time.monotonic() + duration
    throttle_seen = asyncio.Event()

    original_requests_get = qi.requests.get
    original_fetch_isrctn = fetch_isrctn.fetch_isrctn
    async_client: httpx.AsyncClient | None = None

    if source == "ctgov":
        def instrumented_get(*args: Any, **kwargs: Any):
            started = time.monotonic()
            collector.begin()
            try:
                response = original_requests_get(*args, **kwargs)
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                collector.finish(
                    call_id=CURRENT_CALL_ID.get(),
                    status=status,
                    elapsed=time.monotonic() - started,
                    error=type(exc).__name__,
                )
                if status == 429:
                    throttle_seen.set()
                raise
            collector.finish(
                call_id=CURRENT_CALL_ID.get(),
                status=response.status_code,
                elapsed=time.monotonic() - started,
                error=None,
            )
            if response.status_code == 429:
                throttle_seen.set()
            return response

        qi.requests.get = instrumented_get
        source_call = qi._maybe_run_ctgov
    else:
        async_client = httpx.AsyncClient(timeout=30)
        instrumented_client = InstrumentedAsyncClient(
            async_client,
            collector,
            throttle_seen,
        )

        async def instrumented_fetch(params: dict[str, Any], **kwargs: Any):
            result = await original_fetch_isrctn(
                params,
                client=instrumented_client,
                **kwargs,
            )
            return result

        fetch_isrctn.fetch_isrctn = instrumented_fetch
        source_call = qi._maybe_run_isrctn

    async def worker(worker_id: int) -> None:
        nonlocal next_call_id
        index = worker_id
        while time.monotonic() < stop_at and not throttle_seen.is_set():
            axes = corpus[index % len(corpus)]
            index += level
            async with logical_lock:
                call_id = next_call_id
                next_call_id += 1
            token = CURRENT_CALL_ID.set(call_id)
            started = time.monotonic()
            error: str | None = None
            try:
                await asyncio.wait_for(
                    source_call(dict(axes)),
                    timeout=LOGICAL_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                error = "logical_timeout"
            except Exception as exc:
                error = type(exc).__name__
            finally:
                CURRENT_CALL_ID.reset(token)
            logical.append(
                {
                    "call_id": call_id,
                    "elapsed": time.monotonic() - started,
                    "error": error,
                }
            )

    started = time.monotonic()
    try:
        await asyncio.gather(*(worker(worker_id) for worker_id in range(level)))
        await wait_for_raw_drain(collector)
    finally:
        qi.requests.get = original_requests_get
        fetch_isrctn.fetch_isrctn = original_fetch_isrctn
        if async_client is not None:
            await async_client.aclose()
    wall = time.monotonic() - started

    completed = [item for item in logical if item["error"] is None]
    logical_latencies = [item["elapsed"] for item in completed]
    return {
        "source": source,
        "live_api_max_concurrency": level,
        "duration_target_seconds": duration,
        "wall_seconds": round(wall, 4),
        "logical": {
            "attempted": len(logical),
            "completed": len(completed),
            "errors": len(logical) - len(completed),
            "error_counts": {
                error: sum(1 for item in logical if item["error"] == error)
                for error in sorted(
                    {item["error"] for item in logical if item["error"]}
                )
            },
            "throughput_attempted_per_second": round(len(logical) / wall, 4),
            "throughput_completed_per_second": round(len(completed) / wall, 4),
            "p50_seconds": percentile(logical_latencies, 0.50),
            "p95_seconds": percentile(logical_latencies, 0.95),
            "p99_seconds": percentile(logical_latencies, 0.99),
            "max_seconds": (
                round(max(logical_latencies), 4) if logical_latencies else None
            ),
        },
        "http": summarize_raw(collector.records, wall),
        "stop_sweep": any(record.status == 429 for record in collector.records),
    }


async def async_main(args: argparse.Namespace) -> int:
    corpus, snapshot = build_corpus()
    install_resolver_snapshot(snapshot)
    corpus_evidence = {
        "count": len(corpus),
        "conditions": sum("condition" in axes for axes in corpus),
        "interventions": sum("intervention" in axes for axes in corpus),
        "axes": corpus,
    }
    if args.prepare_only:
        print(json.dumps(corpus_evidence, indent=2))
        return 0

    all_results: dict[str, Any] = {
        "method": {
            "levels": list(LEVELS),
            "duration_seconds": args.duration,
            "logical_timeout_seconds": LOGICAL_TIMEOUT_SECONDS,
            "corpus": corpus_evidence,
        },
        "sources": {},
    }
    for source in args.sources:
        source_results = []
        for level in LEVELS:
            result = await run_level(
                source=source,
                level=level,
                duration=args.duration,
                corpus=corpus,
            )
            source_results.append(result)
            print(json.dumps(result), flush=True)
            if result["stop_sweep"]:
                break
        all_results["sources"][source] = source_results

    args.output.write_text(json.dumps(all_results, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_DURATION_SECONDS,
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        choices=("ctgov", "isrctn"),
        default=("ctgov", "isrctn"),
    )
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("live-api-concurrency-results.json"),
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main(build_parser().parse_args())))
