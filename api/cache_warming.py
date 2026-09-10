"""Build and asynchronously populate the bounded Phase 1 query warm list."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import re

from api.routes.query import _cached_query_axes
from api.routes.taxonomy import _TREE_CACHE


logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = REPO_ROOT / "site" / "src" / "App.tsx"
WARM_LIST_MIN = 50
WARM_LIST_MAX = 100
WARM_INTERVAL_SECONDS = 0.25

MATCH_COUNT_QUERY = """
PREFIX ue: <https://universalevidence.com/ontology/>
SELECT ?axis ?concept (COUNT(DISTINCT ?study) AS ?matches) WHERE {
  {
    GRAPH ?graph { ?study ue:matchesCondition ?concept . }
    BIND("state" AS ?axis)
  } UNION {
    GRAPH ?graph { ?study ue:matchesOutcome ?concept . }
    BIND("state" AS ?axis)
  } UNION {
    GRAPH ?graph { ?study ue:matchesIntervention ?concept . }
    BIND("intervention" AS ?axis)
  }
}
GROUP BY ?axis ?concept
ORDER BY DESC(?matches)
LIMIT 75
"""


def _real_roots(nodes: list[dict]) -> list[dict]:
    roots: list[dict] = []
    for node in nodes:
        if node.get("collection"):
            roots.extend(node.get("children", []))
        else:
            roots.append(node)
    return roots


def ui_default_axes(source: str | None = None) -> list[dict[str, str]]:
    """Extract full vocabulary URIs from the frontend's default search object."""
    if source is None:
        try:
            source = APP_SOURCE.read_text()
        except OSError:
            return []
    defaults: list[dict[str, str]] = []
    for axis, uri in re.findall(
        r"(state|condition|intervention|outcome|region)\s*:\s*\{\s*uri:\s*[\"']([^\"']+)",
        source,
    ):
        defaults.append({("state" if axis == "condition" else axis): uri})
    return defaults


def top_level_axes(tree_cache: dict[str, list] | None = None) -> list[dict[str, str]]:
    """Return state/intervention searches for real top-level taxonomy concepts."""
    trees = tree_cache if tree_cache is not None else _TREE_CACHE
    axes: list[dict[str, str]] = []
    for taxonomy_axis, query_axis in (
        ("condition", "state"),
        ("intervention", "intervention"),
    ):
        for node in _real_roots(trees.get(taxonomy_axis, [])):
            uri = node.get("uri")
            if uri:
                axes.append({query_axis: uri})
    return axes


def build_warm_list(
    match_rows: list[dict],
    defaults: list[dict[str, str]],
    top_levels: list[dict[str, str]],
    *,
    limit: int = WARM_LIST_MAX,
) -> list[dict[str, str]]:
    """Combine three required signals, preserving priority and removing duplicates."""
    candidates = [
        *defaults,
        *(
            {row["axis"]: row["concept"]}
            for row in match_rows
            if row.get("axis") in {"state", "intervention"} and row.get("concept")
        ),
        *top_levels,
    ]
    seen: set[tuple[tuple[str, str], ...]] = set()
    result: list[dict[str, str]] = []
    for axes in candidates:
        key = tuple(sorted(axes.items()))
        if key in seen:
            continue
        seen.add(key)
        result.append(axes)
        if len(result) >= limit:
            break
    return result


async def warm_common_queries() -> None:
    """Populate common entries in the background without delaying readiness."""
    try:
        from query_interventions import sparql_select

        match_rows = await asyncio.to_thread(sparql_select, MATCH_COUNT_QUERY)
        warm_list = build_warm_list(
            match_rows,
            ui_default_axes(),
            top_level_axes(),
        )
        logger.info("Starting bounded cache warm-up for %d queries", len(warm_list))
        for axes in warm_list:
            try:
                await _cached_query_axes(axes)
            except Exception:
                logger.warning("Cache warm-up failed for %s", axes, exc_info=True)
            await asyncio.sleep(WARM_INTERVAL_SECONDS)
        logger.info("Cache warm-up complete")
    except Exception:
        logger.warning("Unable to build query cache warm list", exc_info=True)
