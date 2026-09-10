"""Fuseki-backed taxonomy autocomplete routes."""

from __future__ import annotations

import json
import logging
import os

import httpx
from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

UE = "https://universalevidence.com/ontology/"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
SKOS = "http://www.w3.org/2004/02/skos/core#"
OWL = "http://www.w3.org/2002/07/owl#"

DEFAULT_FUSEKI_QUERY_URL = "http://localhost:3030/ue/query"
TAXONOMY_GRAPH_URI = "https://universalevidence.com/graph/ue-taxonomy"
MAX_LIMIT = 50
SPARQL_FETCH_LIMIT = 200

CLASS_MAP = {
    "challenge": f"{UE}State",
    "state": f"{UE}State",
    "condition": f"{UE}State",
    "intervention": f"{UE}Intervention",
    "outcome": f"{UE}State",
    "region": f"{UE}Region",
}

router = APIRouter()

# Process-level tree cache keyed by class name. Pre-populated at startup.
_TREE_CACHE: dict[str, list] = {}
# Flat concept list for in-memory autocomplete, keyed by class name.
_FLAT_CACHE: dict[str, list[dict]] = {}


def get_sparql_endpoint() -> str:
    """Return the Fuseki SPARQL SELECT endpoint."""
    return os.environ.get("FUSEKI_URL", DEFAULT_FUSEKI_QUERY_URL)


def sparql_string(value: str) -> str:
    """Return a SPARQL-safe string literal."""
    return json.dumps(value)


def build_tree_query(class_uri: str) -> str:
    """Return all non-deprecated concepts with labels and skos:broader for tree building."""
    return f"""
PREFIX rdf: <{RDF}>
PREFIX rdfs: <{RDFS}>
PREFIX skos: <{SKOS}>
PREFIX owl: <{OWL}>

SELECT ?uri ?label ?broader ?altLabel WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    ?uri rdf:type <{class_uri}> .
    FILTER NOT EXISTS {{ ?uri owl:deprecated true . }}
    OPTIONAL {{ ?uri skos:prefLabel ?prefLabel . }}
    OPTIONAL {{ ?uri rdfs:label ?rdfsLabel . }}
    OPTIONAL {{ ?uri skos:broader ?broader . }}
    OPTIONAL {{ ?uri skos:altLabel ?altLabel . }}
    BIND(COALESCE(?prefLabel, ?rdfsLabel, STR(?uri)) AS ?label)
  }}
}}
"""


def build_collection_query() -> str:
    """Return all skos:Collection instances with their labels and members."""
    return f"""
PREFIX rdfs: <{RDFS}>
PREFIX skos: <{SKOS}>

SELECT ?collection ?label ?member WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    ?collection a skos:Collection .
    OPTIONAL {{ ?collection skos:prefLabel ?label . }}
    OPTIONAL {{ ?collection rdfs:label ?label . }}
    OPTIONAL {{ ?collection skos:member ?member . }}
  }}
}}
"""


def inject_collections(roots: list[dict], collection_rows: list[dict]) -> list[dict]:
    """Wrap root nodes under their skos:Collection headers.

    Collections that have no members present in the root list are omitted.
    Root nodes not in any collection remain at the top level after collections.
    """
    if not collection_rows:
        return roots

    collections: dict[str, dict] = {}
    member_to_collections: dict[str, list[str]] = {}

    for row in collection_rows:
        coll_uri = row.get("collection")
        if not coll_uri:
            continue
        if coll_uri not in collections:
            collections[coll_uri] = {
                "uri": coll_uri,
                "label": row.get("label", coll_uri.rsplit("/", 1)[-1]),
                "collection": True,
                "children": [],
            }
        member_uri = row.get("member")
        if member_uri:
            member_to_collections.setdefault(member_uri, []).append(coll_uri)

    root_map = {node["uri"]: node for node in roots}
    collected: set[str] = set()

    for member_uri, coll_uris in member_to_collections.items():
        if member_uri in root_map:
            for coll_uri in coll_uris:
                if coll_uri in collections:
                    collections[coll_uri]["children"].append(root_map[member_uri])
                    collected.add(member_uri)

    for coll in collections.values():
        coll["children"].sort(key=lambda n: n.get("label", ""))

    result: list[dict] = sorted(
        [c for c in collections.values() if c["children"]],
        key=lambda c: c.get("label", ""),
    )
    uncollected = [n for n in roots if n["uri"] not in collected]
    result.extend(sorted(uncollected, key=lambda n: n.get("label", "")))
    return result


def build_tree(concepts: dict[str, dict]) -> list[dict]:
    """Assemble a nested tree from flat concept records keyed by URI."""
    children_map: dict[str, list[str]] = {uri: [] for uri in concepts}
    root_uris: set[str] = set(concepts.keys())

    for uri, concept in concepts.items():
        for broader_uri in concept.get("broader", []):
            if broader_uri in concepts:
                children_map[broader_uri].append(uri)
                root_uris.discard(uri)

    def make_node(uri: str) -> dict:
        concept = concepts[uri]
        children = sorted(
            children_map.get(uri, []),
            key=lambda u: concepts[u].get("label", ""),
        )
        node: dict = {
            "uri": uri,
            "label": concept.get("label", uri.rsplit("/", 1)[-1]),
            "children": [make_node(c) for c in children],
        }
        if concept.get("definition"):
            node["definition"] = concept["definition"]
        if concept.get("altLabels"):
            node["altLabels"] = concept["altLabels"]
        return node

    return [make_node(u) for u in sorted(root_uris, key=lambda u: concepts[u].get("label", ""))]


def build_taxonomy_query(class_uri: str, q: str) -> str:
    """Build the taxonomy autocomplete SPARQL query."""
    search = sparql_string(q.lower())
    return f"""
PREFIX rdf: <{RDF}>
PREFIX rdfs: <{RDFS}>
PREFIX skos: <{SKOS}>
PREFIX owl: <{OWL}>

SELECT DISTINCT ?uri ?label ?broader WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    ?uri rdf:type <{class_uri}> .
    FILTER NOT EXISTS {{ ?uri owl:deprecated true . }}
    OPTIONAL {{ ?uri skos:prefLabel ?prefLabel . }}
    OPTIONAL {{ ?uri rdfs:label ?rdfsLabel . }}
    OPTIONAL {{ ?uri skos:altLabel ?altLabel . }}
    OPTIONAL {{ ?uri skos:broader ?broader . }}
    BIND(COALESCE(?prefLabel, ?rdfsLabel, STR(?uri)) AS ?label)
    FILTER(
      CONTAINS(LCASE(STR(?label)), {search}) ||
      CONTAINS(LCASE(STR(COALESCE(?altLabel, ""))), {search})
    )
  }}
}}
LIMIT {SPARQL_FETCH_LIMIT}
"""


def _flatten_tree(nodes: list[dict], out: list[dict] | None = None) -> list[dict]:
    """Flatten a nested tree into a list of {uri, label, broader} dicts."""
    if out is None:
        out = []
    for node in nodes:
        out.append({"uri": node["uri"], "label": node["label"], "broader": []})
        _flatten_tree(node.get("children", []), out)
    return out


async def prewarm_trees() -> None:
    """Load all taxonomy trees and flat concept lists into memory at startup."""
    try:
        collection_rows = await run_sparql(build_collection_query(), timeout=60)
    except Exception:
        logger.warning("Failed to fetch skos:Collection data; tree will have no groupings", exc_info=True)
        collection_rows = []

    for cls, class_uri in CLASS_MAP.items():
        if cls in _TREE_CACHE:
            continue
        try:
            sparql = build_tree_query(class_uri)
            rows = await run_sparql(sparql, timeout=120)
            concepts = {r["uri"]: r for r in normalize_rows(rows) if r.get("uri")}
            roots = build_tree(concepts)
            _TREE_CACHE[cls] = inject_collections(roots, collection_rows)
            _FLAT_CACHE[cls] = list(concepts.values())
            logger.info("Pre-warmed %s tree (%d concepts)", cls, len(concepts))
        except Exception:
            logger.warning("Failed to pre-warm %s tree", cls, exc_info=True)


async def run_sparql(query: str, endpoint: str | None = None, timeout: int = 30) -> list[dict[str, str]]:
    """Run a SPARQL SELECT query and return simple string bindings."""
    url = endpoint or get_sparql_endpoint()
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            url,
            data={"query": query},
            headers={"Accept": "application/sparql-results+json"},
        )
        response.raise_for_status()
        payload = response.json()

    rows: list[dict[str, str]] = []
    for binding in payload.get("results", {}).get("bindings", []):
        rows.append(
            {
                key: value.get("value")
                for key, value in binding.items()
                if value.get("value") is not None
            }
        )
    return rows


def normalize_rows(rows: list[dict[str, str]]) -> list[dict]:
    """Aggregate SPARQL rows into stable autocomplete records."""
    records: dict[str, dict] = {}
    for row in rows:
        uri = row.get("uri")
        if not uri:
            continue

        record = records.setdefault(
            uri,
            {
                "uri": uri,
                "label": row.get("label") or uri.rsplit("/", 1)[-1].rsplit("#", 1)[-1],
                "broader": [],
                "altLabels": [],
            },
        )
        if row.get("label") and not record.get("label"):
            record["label"] = row["label"]
        if row.get("definition") and not record.get("definition"):
            record["definition"] = row["definition"]
        if row.get("broader") and row["broader"] not in record["broader"]:
            record["broader"].append(row["broader"])
        if row.get("altLabel") and row["altLabel"] not in record["altLabels"]:
            record["altLabels"].append(row["altLabel"])

    return list(records.values())


def relevance_key(record: dict, q: str) -> tuple[int, str]:
    """Sort exact label matches before prefix, substring, and fallback matches."""
    label = record.get("label", "")
    label_lower = label.lower()
    query_lower = q.lower()
    if label_lower == query_lower:
        rank = 0
    elif label_lower.startswith(query_lower):
        rank = 1
    elif query_lower in label_lower:
        rank = 2
    else:
        rank = 3
    return rank, label_lower


def sort_and_limit(records: list[dict], q: str, limit: int) -> list[dict]:
    """Sort records by simple relevance and apply the requested limit."""
    return sorted(records, key=lambda record: relevance_key(record, q))[:limit]


@router.get("/taxonomy/{cls}/tree")
async def tree(cls: str) -> list[dict]:
    """Return the full taxonomy hierarchy as a nested tree of nodes."""
    class_uri = CLASS_MAP.get(cls)
    if class_uri is None:
        raise HTTPException(status_code=400, detail=f"Unknown taxonomy class: {cls}")

    if cls in _TREE_CACHE:
        return _TREE_CACHE[cls]

    sparql = build_tree_query(class_uri)
    try:
        rows = await run_sparql(sparql, timeout=120)
        collection_rows = await run_sparql(build_collection_query(), timeout=60)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Taxonomy search service unavailable") from exc

    concepts = {r["uri"]: r for r in normalize_rows(rows) if r.get("uri")}
    roots = build_tree(concepts)
    result = inject_collections(roots, collection_rows)
    _TREE_CACHE[cls] = result
    _FLAT_CACHE[cls] = list(concepts.values())
    return result


def _search_flat_cache(cls: str, q: str) -> list[dict]:
    """Filter the flat concept cache by label or altLabel substring match."""
    q_lower = q.lower()
    results = []
    for concept in _FLAT_CACHE[cls]:
        label = concept.get("label", "")
        alt_labels = concept.get("altLabels", [])
        if q_lower in label.lower() or any(q_lower in alt.lower() for alt in alt_labels):
            results.append(concept)
    return results


@router.get("/taxonomy/{cls}")
async def autocomplete(
    cls: str,
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=MAX_LIMIT),
) -> list[dict]:
    """Return taxonomy autocomplete results."""
    class_uri = CLASS_MAP.get(cls)
    if class_uri is None:
        raise HTTPException(status_code=400, detail=f"Unknown taxonomy class: {cls}")

    query_text = q.strip()
    if not query_text:
        raise HTTPException(status_code=422, detail="q must contain non-whitespace text")

    if cls in _FLAT_CACHE:
        return sort_and_limit(_search_flat_cache(cls, query_text), query_text, limit)

    sparql = build_taxonomy_query(class_uri, query_text)
    try:
        rows = await run_sparql(sparql)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Taxonomy search service unavailable") from exc

    return sort_and_limit(normalize_rows(rows), query_text, limit)
