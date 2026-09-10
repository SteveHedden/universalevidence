#!/usr/bin/env python3
"""Read-only sweep of the local Fuseki admission path."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import random
import statistics
import subprocess
import sys
import time

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import query_interventions as qi  # noqa: E402

LEVELS = (2, 4, 8, 15)
WINDOW_SECONDS = 180
CALL_TIMEOUT_SECONDS = 30
HEALTH_URL = os.getenv("FUSEKI_HEALTH_URL", "http://fuseki:3030/ue/query")
CORPUS_QUERY = """
PREFIX ue: <https://universalevidence.com/ontology/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?kind ?concept (COUNT(DISTINCT ?study) AS ?matches) WHERE {
  VALUES (?kind ?predicate) {
    ("condition" ue:matchesCondition)
    ("intervention" ue:matchesIntervention)
    ("outcome" ue:matchesOutcome)
    ("region" ue:location)
  }
  GRAPH ?graph { ?study ?predicate ?concept }
  FILTER(?graph IN (
    <https://universalevidence.com/graph/aea>,
    <https://universalevidence.com/graph/who-ictrp>
  ))
  GRAPH <https://universalevidence.com/graph/ue-taxonomy> {
    ?concept skos:prefLabel ?label .
  }
}
GROUP BY ?kind ?concept
HAVING(COUNT(DISTINCT ?study) > 0)
ORDER BY ?kind DESC(?matches) STR(?concept)
"""


def p99(values: list[float]) -> float | None:
    if not values:
        return None
    return sorted(values)[max(0, min(len(values) - 1, int(len(values) * 0.99) - 1))]


def docker_snapshot() -> dict:
    try:
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", "universalevidence-fuseki-1"],
            check=False, capture_output=True, text=True,
        )
    except FileNotFoundError:
        return {"unavailable": "docker CLI is outside the disposable harness container"}
    try:
        return json.loads(result.stdout.strip())
    except (json.JSONDecodeError, AttributeError):
        return {"raw": result.stdout.strip(), "error": result.stderr.strip()}


def restarts() -> int | None:
    try:
        result = subprocess.run(
            ["docker", "inspect", "universalevidence-fuseki-1", "--format", "{{.RestartCount}}"],
            check=False, capture_output=True, text=True,
        )
    except FileNotFoundError:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def healthy() -> bool:
    try:
        response = requests.get(
            HEALTH_URL,
            params={"query": "ASK{}"},
            headers={"Accept": "application/sparql-results+json"},
            timeout=5,
        )
        return response.ok and response.json().get("boolean") is True
    except (OSError, requests.RequestException, ValueError):
        return False


def build_corpus() -> list[dict[str, str]]:
    rows = qi.sparql_select(CORPUS_QUERY)
    by_kind: dict[str, list[str]] = {"condition": [], "intervention": [], "outcome": [], "region": []}
    for row in rows:
        kind, concept = row.get("kind"), row.get("concept")
        if kind in by_kind and concept and concept not in by_kind[kind]:
            by_kind[kind].append(concept)

    candidates: list[dict[str, str]] = []
    candidates.extend({"condition": uri} for uri in by_kind["condition"][:150])
    candidates.extend({"outcome": uri} for uri in by_kind["outcome"][:150])
    candidates.extend({"intervention": uri} for uri in by_kind["intervention"][:150])
    candidates.extend({"region": uri} for uri in by_kind["region"][:50])
    candidates.extend(
        {"condition": state, "region": region}
        for state, region in zip(by_kind["condition"][:50], by_kind["region"][:50])
    )
    candidates.extend(
        {"intervention": intervention, "region": region}
        for intervention, region in zip(by_kind["intervention"][:50], by_kind["region"][:50])
    )
    candidates.extend(
        {"condition": state, "intervention": intervention}
        for state, intervention in zip(by_kind["condition"][:100], by_kind["intervention"][:100])
    )

    # The count query above is the resolvability check: every selected concept
    # has at least one local study match in the production graphs. Do not issue
    # hundreds of unmeasured validation queries before the timed sweep.
    if len(candidates) < 100:
        raise RuntimeError(f"only {len(candidates)} resolvable corpus axes found")
    return candidates[:100]


async def run_level(level: int, corpus: list[dict[str, str]]) -> dict:
    qi._LOCAL_QUERY_SEMAPHORE = asyncio.Semaphore(level)
    latencies: list[float] = []
    errors: Counter[str] = Counter()
    started = time.monotonic()
    stop_at = started + WINDOW_SECONDS
    completed = 0
    health_failures = 0
    max_mem_percent = 0.0
    samples: list[dict] = []
    next_index = 0
    lock = asyncio.Lock()

    async def monitor() -> None:
        nonlocal health_failures, max_mem_percent
        while time.monotonic() < stop_at:
            if not healthy():
                health_failures += 1
            snapshot = docker_snapshot()
            raw = str(snapshot.get("MemPerc", "0")).rstrip("%")
            try:
                max_mem_percent = max(max_mem_percent, float(raw))
            except ValueError:
                pass
            samples.append({"ts": time.time(), "healthy": healthy(), "docker": snapshot})
            await asyncio.sleep(1)

    async def worker() -> None:
        nonlocal completed, next_index
        while time.monotonic() < stop_at:
            async with lock:
                axes = corpus[next_index % len(corpus)]
                next_index += 1
            call_started = time.monotonic()
            try:
                await asyncio.wait_for(qi._run_local_query_limited(axes), CALL_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                errors["timeout"] += 1
            except Exception as exc:
                errors[type(exc).__name__] += 1
            else:
                completed += 1
            latencies.append(time.monotonic() - call_started)

    monitor_task = asyncio.create_task(monitor())
    workers = [asyncio.create_task(worker()) for _ in range(level)]
    await asyncio.gather(*workers)
    await monitor_task
    wall = time.monotonic() - started
    restart_count = restarts()
    result = {
        "level": level,
        "window_seconds": round(wall, 3),
        "client_concurrency": level,
        "attempted": len(latencies),
        "completed": completed,
        "errors": dict(errors),
        "latency_seconds": {
            "p50": round(statistics.median(latencies), 4) if latencies else None,
            "p95": round(sorted(latencies)[max(0, int(len(latencies) * .95) - 1)], 4) if latencies else None,
            "p99": round(p99(latencies), 4) if latencies else None,
            "max": round(max(latencies), 4) if latencies else None,
        },
        "throughput_per_second": round(completed / wall, 4) if wall else 0,
        "health_failures": health_failures,
        "max_fuseki_mem_percent": round(max_mem_percent, 3),
        "fuseki_restart_count": restart_count,
        "health_samples": samples,
    }
    result["passes"] = (
        not errors
        and health_failures == 0
        and restart_count in (0, None)
        and (result["latency_seconds"]["p99"] is not None and result["latency_seconds"]["p99"] < 5)
    )
    return result


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    corpus = build_corpus()
    results = {"flags": {name: os.getenv(name) for name in ("MATERIALIZED_SOURCES_ENABLED", "FEDERATED_QUERY_ENABLED")}, "corpus": corpus, "levels": []}
    for level in LEVELS:
        result = await run_level(level, corpus)
        results["levels"].append(result)
        args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
        if not result["passes"]:
            break


if __name__ == "__main__":
    asyncio.run(main())
