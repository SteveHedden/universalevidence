#!/usr/bin/env python3
"""Record repeatable v1 baselines and compare them with /query/v2 canaries.

The script intentionally does not mutate or warm application state through a
private API. It records the process cache counters exposed by /cache/stats when
available so a reviewer can distinguish a first observation from a confirmed
cache miss or hit.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Iterable
from urllib.parse import urljoin

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = REPO_ROOT / "tests" / "fixtures" / "query_v2" / "canary_queries.json"
DEFAULT_TIMEOUT_SECONDS = 90.0

SOURCE_ALIASES = {
    "aea": "aea",
    "aea rct registry": "aea",
    "clinicaltrials.gov": "ctgov",
    "ct.gov": "ctgov",
    "ctgov": "ctgov",
    "isrctn": "isrctn",
    "isrctn registry": "isrctn",
    "who ictrp": "who-ictrp",
    "who-ictrp": "who-ictrp",
}


def canonical_source(value: object) -> str:
    """Return a stable source ID for v1 display labels or v2 IDs."""
    text = str(value or "").strip()
    lowered = text.casefold()
    if lowered in SOURCE_ALIASES:
        return SOURCE_ALIASES[lowered]
    if "/sources/" in lowered:
        lowered = lowered.rstrip("/").rsplit("/", 1)[-1]
    return SOURCE_ALIASES.get(lowered, lowered or "unknown")


def parameter_pairs(params: dict[str, list[str]] | None) -> list[tuple[str, str]]:
    """Expand JSON array values into repeated HTTP query parameters."""
    if not params:
        return []
    pairs: list[tuple[str, str]] = []
    for key in sorted(params):
        values = params[key]
        if not isinstance(values, list) or not values:
            raise ValueError(f"{key} must be a non-empty JSON array")
        pairs.extend((key, str(value)) for value in values)
    return pairs


def _study_rows(payload: object, endpoint: str) -> list[dict[str, Any]]:
    if endpoint == "v1":
        return payload if isinstance(payload, list) else []
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return payload["results"]
    return []


def summarize_payload(payload: object, endpoint: str) -> dict[str, Any]:
    """Summarize rows without persisting mutable live study payloads."""
    rows = _study_rows(payload, endpoint)
    keys: set[tuple[str, str]] = set()
    source_counts: Counter[str] = Counter()
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = canonical_source(row.get("source"))
        study_id = str(row.get("study_id") or "").strip()
        if not study_id:
            continue
        key = (source, study_id)
        if key in keys:
            continue
        keys.add(key)
        source_counts[source] += 1

    summary: dict[str, Any] = {
        "presentation_rows": len(rows),
        "unique_studies": len(keys),
        "duplicate_presentation_rows": max(0, len(rows) - len(keys)),
        "source_counts": dict(sorted(source_counts.items())),
    }
    if endpoint == "v2" and isinstance(payload, dict):
        meta = payload.get("meta")
        if isinstance(meta, dict):
            summary["meta"] = meta
    return summary


def _json_payload(response: requests.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None


def observe(
    session: requests.Session,
    *,
    base_url: str,
    endpoint: str,
    params: dict[str, list[str]] | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Make one timed request and retain only bounded diagnostic output."""
    path = "/query" if endpoint == "v1" else "/query/v2"
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    started = time.perf_counter()
    try:
        response = session.get(
            url,
            params=parameter_pairs(params),
            timeout=timeout_seconds,
            headers={"Accept": "application/json"},
        )
    except requests.RequestException as exc:
        return {
            "endpoint": endpoint,
            "url": url,
            "http_status": None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }

    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    payload = _json_payload(response)
    observation: dict[str, Any] = {
        "endpoint": endpoint,
        "url": response.url,
        "http_status": response.status_code,
        "elapsed_ms": elapsed_ms,
        "payload_sha256": hashlib.sha256(response.content).hexdigest(),
        "content_bytes": len(response.content),
    }
    if response.status_code == 200:
        observation.update(summarize_payload(payload, endpoint))
    else:
        observation["error_body"] = (
            payload if payload is not None else response.text[:1000]
        )
    return observation


def fetch_cache_stats(
    session: requests.Session,
    base_url: str,
    timeout_seconds: float,
    path: str = "cache/stats",
) -> dict[str, Any] | None:
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    try:
        response = session.get(url, timeout=min(timeout_seconds, 10.0))
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None
    return payload if response.status_code == 200 and isinstance(payload, dict) else None


def cache_stats_delta(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> dict[str, int | float] | None:
    """Return numeric counter deltas when both cache snapshots are available."""
    if before is None or after is None:
        return None
    delta: dict[str, int | float] = {}
    for key in (
        "hits",
        "misses",
        "backend_executions",
        "backend_executions_avoided",
        "uncacheable_results",
    ):
        old = before.get(key)
        new = after.get(key)
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            delta[key] = new - old
    return delta


def baseline_observations(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Load successful v1 observations from a previously recorded report."""
    payload = json.loads(path.read_text())
    result: dict[str, list[dict[str, Any]]] = {}
    for case in payload.get("cases", []):
        case_id = case.get("id")
        observations = [
            dict(item)
            for item in case.get("observations", [])
            if item.get("endpoint") == "v1" and item.get("http_status") == 200
        ]
        if case_id and observations:
            result[str(case_id)] = observations
    return result


def _latest_success(observations: Iterable[dict[str, Any]], endpoint: str) -> dict[str, Any] | None:
    for observation in reversed(list(observations)):
        if observation.get("endpoint") == endpoint and observation.get("http_status") == 200:
            return observation
    return None


def evaluate_case(case: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate stable same-run gates; geographic truth remains reviewer-audited."""
    if not case.get("compare_with_v1"):
        v2 = _latest_success(observations, "v2")
        return {
            "status": "pass" if v2 is not None else "not_evaluated",
            "checks": {"v2_available": v2 is not None},
        }

    v1 = _latest_success(observations, "v1")
    v2 = _latest_success(observations, "v2")
    if v1 is None or v2 is None:
        return {
            "status": "not_evaluated",
            "checks": {
                "v1_available": v1 is not None,
                "v2_available": v2 is not None,
            },
        }

    gates = case.get("gates", {})
    checks: dict[str, bool] = {}
    if gates.get("preserve_same_run_v1_sources"):
        v1_sources = set(v1.get("source_counts", {}))
        v2_sources = set(v2.get("source_counts", {}))
        checks["preserve_same_run_v1_sources"] = v1_sources <= v2_sources
    if gates.get("no_expanded_row_starvation"):
        v1_counts = v1.get("source_counts", {})
        v2_counts = v2.get("source_counts", {})
        checks["no_expanded_row_starvation"] = all(
            int(v2_counts.get(source, 0)) >= min(int(count), 100)
            for source, count in v1_counts.items()
        )
    checks["v1_available"] = True
    checks["v2_available"] = True
    return {
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "review_required": [
            key
            for key in (
                "ctgov_locations_verified_in_requested_adm1",
                "forbid_parent_country_broadening",
                "country_behavior_preserved",
                "descendant_expansion_preserves_predicate",
            )
            if key in gates
        ],
    }


def run_canary(
    *,
    base_url: str,
    v1_base_url: str | None,
    v1_baseline_path: Path | None,
    cases_path: Path,
    baseline_only: bool,
    passes: int,
    timeout_seconds: float,
    selected_cases: set[str] | None = None,
) -> dict[str, Any]:
    corpus_bytes = cases_path.read_bytes()
    corpus = json.loads(corpus_bytes)
    cases = corpus["cases"]
    if selected_cases:
        cases = [case for case in cases if case["id"] in selected_cases]
        missing = selected_cases - {case["id"] for case in cases}
        if missing:
            raise ValueError(f"Unknown case IDs: {', '.join(sorted(missing))}")

    session = requests.Session()
    effective_v1_base_url = v1_base_url or base_url
    cache_stats_path = "cache/stats" if baseline_only else "query/v2/cache/stats"
    recorded_v1 = (
        baseline_observations(v1_baseline_path) if v1_baseline_path else {}
    )
    report: dict[str, Any] = {
        "schema_version": "query-v2-canary-report-1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url.rstrip("/"),
        "v1_base_url": effective_v1_base_url.rstrip("/"),
        "v1_baseline": str(v1_baseline_path) if v1_baseline_path else None,
        "v1_baseline_sha256": (
            hashlib.sha256(v1_baseline_path.read_bytes()).hexdigest()
            if v1_baseline_path
            else None
        ),
        "mode": "v1-baseline" if baseline_only else "v1-v2-canary",
        "passes": passes,
        "timeout_seconds": timeout_seconds,
        "case_corpus": str(cases_path),
        "case_corpus_sha256": hashlib.sha256(corpus_bytes).hexdigest(),
        "cache_note": (
            "first/repeat are observation order, not assertions of cold/warm state; "
            "use cache_stats deltas when the server exposes them"
        ),
        "cache_stats_endpoint": cache_stats_path,
        "cache_stats_before": fetch_cache_stats(
            session, base_url, timeout_seconds, cache_stats_path
        ),
        "cases": [],
    }

    for case in cases:
        observations: list[dict[str, Any]] = []
        for pass_index in range(passes):
            label = "first" if pass_index == 0 else f"repeat_{pass_index}"
            if case.get("v1_params") is not None:
                baseline_items = recorded_v1.get(case["id"], [])
                if v1_baseline_path:
                    if not baseline_items:
                        raise ValueError(
                            f"No successful v1 baseline for case: {case['id']}"
                        )
                    item = dict(
                        baseline_items[min(pass_index, len(baseline_items) - 1)]
                    )
                    item["baseline_reused"] = True
                else:
                    item = observe(
                        session,
                        base_url=effective_v1_base_url,
                        endpoint="v1",
                        params=case["v1_params"],
                        timeout_seconds=timeout_seconds,
                    )
                item["observation"] = label
                observations.append(item)
            if not baseline_only:
                item = observe(
                    session,
                    base_url=base_url,
                    endpoint="v2",
                    params=case["v2_params"],
                    timeout_seconds=timeout_seconds,
                )
                item["observation"] = label
                observations.append(item)
        report["cases"].append(
            {
                "id": case["id"],
                "gates": case.get("gates", {}),
                "observations": observations,
                "evaluation": evaluate_case(case, observations),
            }
        )

    report["cache_stats_after"] = fetch_cache_stats(
        session, base_url, timeout_seconds, cache_stats_path
    )
    report["cache_stats_delta"] = cache_stats_delta(
        report["cache_stats_before"], report["cache_stats_after"]
    )
    statuses = Counter(case["evaluation"]["status"] for case in report["cases"])
    report["evaluation_summary"] = dict(sorted(statuses.items()))
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="API origin, without /query (default: %(default)s)",
    )
    parser.add_argument(
        "--v1-base-url",
        help=(
            "Optional origin for unchanged v1 observations; defaults to "
            "--base-url so existing behavior is preserved"
        ),
    )
    parser.add_argument(
        "--v1-baseline",
        type=Path,
        help=(
            "Reuse successful v1 observations from a recorded canary report "
            "instead of issuing live v1 requests"
        ),
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--case", action="append", dest="selected_cases")
    parser.add_argument(
        "--baseline-only",
        action="store_true",
        help="Call the legacy /query endpoint only",
    )
    parser.add_argument("--passes", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.passes < 1:
        raise SystemExit("--passes must be at least 1")
    report = run_canary(
        base_url=args.base_url,
        v1_base_url=args.v1_base_url,
        v1_baseline_path=args.v1_baseline,
        cases_path=args.cases,
        baseline_only=args.baseline_only,
        passes=args.passes,
        timeout_seconds=args.timeout,
        selected_cases=set(args.selected_cases) if args.selected_cases else None,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    else:
        print(rendered, end="")

    failed_http = any(
        observation.get("http_status") != 200
        for case in report["cases"]
        for observation in case["observations"]
    )
    failed_gate = any(
        case["evaluation"]["status"] == "fail" for case in report["cases"]
    )
    return 1 if failed_http or failed_gate else 0


if __name__ == "__main__":
    raise SystemExit(main())
