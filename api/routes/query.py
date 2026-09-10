"""Query API routes backed by the federated dispatcher."""

from __future__ import annotations

import logging
from pathlib import Path
import sys
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from query_interventions import (  # noqa: E402
    ClientDisconnectedError,
    LocalQueryBusyError,
    async_query_axes,
    async_query_axes_with_metadata,
    await_query_or_disconnect,
    sparql_select,
)
from api.routes.taxonomy import _FLAT_CACHE
from api.vocabulary_compat import canonical_vocabulary_uri
from api.graph_v2_cache import graph_v2_cache
from api.query_cache import query_result_cache

logger = logging.getLogger(__name__)

VOCAB_PREFIX = "https://universalevidence.com/vocab/"
# condition and outcome both resolve to ue:State concepts which may live in
# conditions/, outcomes/, or states/ namespaces after the STATES migration.
# Accept any URI under the shared vocab/ prefix for state-type axes.
AXIS_PREFIXES = {
    "condition": VOCAB_PREFIX,
    "intervention": f"{VOCAB_PREFIX}interventions/",
    "outcome": VOCAB_PREFIX,
    "state": VOCAB_PREFIX,
    "region": (f"{VOCAB_PREFIX}regions/", "https://sws.geonames.org/"),
}
PUBLIC_RESULT_KEYS = (
    "source",
    "study_id",
    "title",
    "url",
    "intervention",
    "intervention_concept",
    "intervention_concept_uri",
    "condition_concept",
    "condition_concept_uri",
    "country",
    "status",
    "year",
    "outcomes",
)

router = APIRouter()

async def _cached_query_axes(axes: dict[str, str], query_fn=None) -> list[dict]:
    async def execute(query_axes: dict[str, str]):
        return await async_query_axes_with_metadata(
            query_axes,
            query_fn=query_fn or async_query_axes,
        )

    return await query_result_cache.get_or_execute(axes, execute)


@router.get("/cache/stats")
def cache_stats() -> dict[str, Any]:
    """Expose process-local cache effectiveness and latency counters."""
    return {
        **query_result_cache.stats(),
        "graph_v2": graph_v2_cache.stats(),
    }


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _looks_like_uri(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _normalize_slug(s: str) -> str:
    """Lowercase and strip punctuation/spaces for fuzzy slug matching."""
    return "".join(c for c in s.lower() if c.isalnum())


def _local_name(uri: str) -> str:
    return uri.rstrip("/").rsplit("/", 1)[-1]


def _resolve_slug(axis: str, slug: str) -> str:
    """Resolve a human-readable slug to a full taxonomy URI using the in-memory cache.

    Matches against prefLabel and URI local name (case-insensitive, punctuation-stripped).
    Raises HTTPException if no match or ambiguous.
    """
    concepts = _FLAT_CACHE.get(axis, [])
    normalized = _normalize_slug(slug)
    matches = [
        c for c in concepts
        if _normalize_slug(c.get("label", "")) == normalized
        or _normalize_slug(_local_name(c["uri"])) == normalized
    ]
    if len(matches) == 1:
        return matches[0]["uri"]
    if len(matches) > 1:
        uris = ", ".join(c["uri"] for c in matches[:3])
        raise HTTPException(
            status_code=400,
            detail=f"Ambiguous slug '{slug}' for {axis} — use a full URI. Candidates: {uris}",
        )
    raise HTTPException(
        status_code=400,
        detail=f"Unknown {axis} '{slug}'. Use /taxonomy/{axis}?q={slug} to find the right URI.",
    )


def _validate_axis_uri(axis: str, value: str) -> str:
    expected_prefix = AXIS_PREFIXES[axis]
    prefixes = (expected_prefix,) if isinstance(expected_prefix, str) else expected_prefix
    if not value.startswith(prefixes):
        raise HTTPException(
            status_code=400,
            detail=f"{axis} must be a full URI under one of: {', '.join(prefixes)}",
        )
    return canonical_vocabulary_uri(value)


def build_axes_dict(
    condition: Optional[str] = None,
    intervention: Optional[str] = None,
    outcome: Optional[str] = None,
    region: Optional[str] = None,
    country: Optional[str] = None,
    state: Optional[str] = None,
) -> dict[str, str]:
    """Build and validate the dispatcher axes dictionary.

    Any single URI axis is sufficient. Country is accepted as a legacy alias
    for region and is resolved to ISO at dispatch time. Non-URI values for any
    taxonomy axis raise 400.
    """
    raw_axes = {
        "condition": _clean(condition),
        "intervention": _clean(intervention),
        "outcome": _clean(outcome),
        "state": _clean(state),
        "region": _clean(region),
    }
    country_text = _clean(country)
    axes: dict[str, str] = {}

    for axis, value in raw_axes.items():
        if value is None:
            continue
        if _looks_like_uri(value):
            axes[axis] = _validate_axis_uri(axis, value)
        else:
            axes[axis] = _resolve_slug(axis, value)

    if country_text:
        axes["country"] = country_text

    if not axes:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one query axis",
        )

    return axes


def normalize_query_results(results: list[dict]) -> list[dict]:
    """Constrain query results to the public schema and guarantee outcomes."""
    normalized = []
    for result in results:
        item = {key: result.get(key) for key in PUBLIC_RESULT_KEYS}
        item["outcomes"] = result.get("outcomes") or []
        normalized.append(item)
    return normalized


_STATS_QUERY = """
PREFIX ue:  <https://universalevidence.com/ontology/>
PREFIX aea: <https://socialscienceregistry.org/schema#>
SELECT
  (COUNT(DISTINCT ?e) AS ?studies)
  (COUNT(DISTINCT ?g) AS ?sources)
  (COUNT(DISTINCT ?place) AS ?countries)
WHERE {
  GRAPH ?g {
    ?e a ue:Evidence .
    OPTIONAL { ?e ue:location ?loc }
    OPTIONAL { ?e aea:country ?country }
    BIND(COALESCE(?loc, ?country) AS ?place)
  }
  FILTER(?g != <https://universalevidence.com/graph/ue-taxonomy>)
}
"""

_REACHABLE_QUERY = """
PREFIX aea:   <https://socialscienceregistry.org/schema#>
PREFIX ictrp: <https://universalevidence.com/source/who-ictrp/schema#>
PREFIX ue:    <https://universalevidence.com/ontology/>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
SELECT (COUNT(DISTINCT ?study) AS ?reachable) WHERE {
  GRAPH <https://universalevidence.com/graph/ue-taxonomy> {
    ?entry ue:rawText ?kw ;
           (skos:exactMatch|skos:closeMatch) ?concept .
  }
  {
    GRAPH <https://universalevidence.com/graph/aea> {
      ?study a aea:RCTStudy ;
             aea:keyword ?kw .
    }
  } UNION {
    GRAPH <https://universalevidence.com/graph/who-ictrp> {
      ?study (ictrp:condition|ictrp:intervention|ictrp:primaryOutcome) ?kw .
    }
  }
}
"""


@router.get("/stats")
def stats() -> dict:
    """Return aggregate counts: total evidence records, taxonomy-reachable studies, sources, countries."""
    try:
        rows = sparql_select(_STATS_QUERY)
        row = rows[0] if rows else {}
        reachable_rows = sparql_select(_REACHABLE_QUERY)
        reachable = int((reachable_rows[0] if reachable_rows else {}).get("reachable", 0))
        return {
            "studies": int(row.get("studies", 0)),
            "reachable": reachable,
            "sources": int(row.get("sources", 0)),
            "countries": int(row.get("countries", 0)),
        }
    except Exception:
        logger.warning("Stats query failed", exc_info=True)
        return {"studies": 0, "reachable": 0, "sources": 0, "countries": 0}


@router.get("/query")
async def query(
    request: Request,
    condition: Optional[str] = Query(None),
    intervention: Optional[str] = Query(None),
    outcome: Optional[str] = Query(None),
    region: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    state: Optional[str] = None,
) -> list[dict]:
    """Return normalized studies, truncated to 100 local results per source."""
    axes = build_axes_dict(condition, intervention, outcome, region, country, state)
    try:
        results = await await_query_or_disconnect(_cached_query_axes(axes), request)
        return normalize_query_results(results)
    except LocalQueryBusyError as exc:
        raise HTTPException(
            status_code=503, detail="Search is busy, try again shortly"
        ) from exc
    except ClientDisconnectedError as exc:
        raise HTTPException(status_code=499, detail="Client disconnected") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except requests.RequestException as exc:
        logger.warning("Downstream request failed: %s", exc)
        raise HTTPException(status_code=502, detail="Downstream service request failed") from exc
    except Exception as exc:
        logger.exception("Unhandled query failure")
        raise HTTPException(status_code=500, detail="Internal server error") from exc
