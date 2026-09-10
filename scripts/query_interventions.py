#!/usr/bin/env python3
"""Federated intervention query CLI backed by Fuseki."""

import argparse
import asyncio
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Optional
from urllib.parse import urlencode, urlparse
from uuid import uuid4

import requests

try:
    from scripts.ctgov_intervention_identity import lookup as ctgov_intervention_lookup
except ModuleNotFoundError:
    from ctgov_intervention_identity import lookup as ctgov_intervention_lookup

logger = logging.getLogger(__name__)


def _positive_int_env(name: str, default: int) -> int:
    """Read a positive integer environment setting, falling back safely."""
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
        if value < 1:
            raise ValueError
        return value
    except ValueError:
        logger.warning("Invalid %s=%r; using %d", name, raw, default)
        return default


LIVE_API_MAX_CONCURRENCY = _positive_int_env("LIVE_API_MAX_CONCURRENCY", 2)
LIVE_API_SEMAPHORE_TIMEOUT = 8
# This limiter is process-wide, not host-wide. The current deployment uses one
# uvicorn worker. A multi-worker deployment must retain one worker or introduce
# a cross-process limiter to preserve the combined host-wide cap.
_LIVE_API_SEMAPHORE = asyncio.Semaphore(LIVE_API_MAX_CONCURRENCY)

LOCAL_QUERY_MAX_CONCURRENCY = _positive_int_env("LOCAL_QUERY_MAX_CONCURRENCY", 2)
LOCAL_QUERY_SEMAPHORE_TIMEOUT = 8
# Optional performance mode: omit the expensive global WHO-ICTRP sort while preserving
# the existing bounded result size. Disabled by default pending validation.
LOCAL_QUERY_FAST_WHO_ORDER = os.getenv("LOCAL_QUERY_FAST_WHO_ORDER", "false").lower() == "true"
# Like the live-API limiter, this is process-wide rather than host-wide and
# relies on the current single-worker uvicorn deployment. Each permit follows
# the actual background Fuseki work, even after its HTTP client disconnects.
_LOCAL_QUERY_SEMAPHORE = asyncio.Semaphore(LOCAL_QUERY_MAX_CONCURRENCY)
_DETACHED_QUERY_TASKS: set[asyncio.Task] = set()


class LocalQueryBusyError(RuntimeError):
    """Raised when primary local search capacity is unavailable."""


class ClientDisconnectedError(RuntimeError):
    """Raised after detaching work for a client that has disconnected."""


@dataclass(frozen=True)
class QueryExecution:
    """Internal query result plus source-completion state for cache decisions."""

    results: list[dict]
    source_status: dict[str, str]

    @property
    def cacheable(self) -> bool:
        return all(
            status in {"success", "not_applicable"}
            for status in self.source_status.values()
        )


_SOURCE_COMPLETION: ContextVar[Optional[dict[str, str]]] = ContextVar(
    "source_completion", default=None
)


def _mark_source_status(source: str, status: str) -> None:
    tracker = _SOURCE_COMPLETION.get()
    if tracker is not None:
        tracker[source] = status


async def _acquire_live_api_slot(source: str) -> bool:
    """Acquire the shared live-API slot, returning False on bounded queue wait."""
    try:
        await asyncio.wait_for(
            _LIVE_API_SEMAPHORE.acquire(), timeout=LIVE_API_SEMAPHORE_TIMEOUT
        )
    except asyncio.TimeoutError:
        _mark_source_status(
            "ctgov" if source == "CT.gov" else "isrctn",
            "skipped_due_to_capacity",
        )
        logger.info(
            "Skipping %s: live API concurrency slot unavailable after %ss",
            source,
            LIVE_API_SEMAPHORE_TIMEOUT,
        )
        return False
    return True


def _consume_detached_query(task: asyncio.Task) -> None:
    """Retire a detached query and consume/log any terminal exception."""
    _DETACHED_QUERY_TASKS.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:
        logger.info("Detached query task was cancelled")
    except Exception:
        logger.exception("Detached query task failed")


async def await_query_or_disconnect(awaitable, request, poll_interval: float = 0.1):
    """Return ready work without awaiting disconnect-watcher cancellation.

    Starlette's cancelled AnyIO scope can swallow Task.cancel() during a poll.
    An explicit stop condition ends that poller even when cancellation is lost.
    The callback owns/observes cleanup; no cleanup await can hold the response.
    Disconnected/cancelled callers detach work without releasing its permits.
    """
    try:
        from query_runtime import observe, stage
    except ModuleNotFoundError:
        from scripts.query_runtime import observe, stage
    query_task = asyncio.ensure_future(awaitable)
    stopped = False

    async def watch_disconnect() -> None:
        while not stopped and not query_task.done():
            if await request.is_disconnected():
                return
            if stopped or query_task.done():
                return
            await asyncio.sleep(poll_interval)

    disconnect_task = asyncio.create_task(watch_disconnect(), name="query-disconnect-watcher")
    try:
        done, _ = await asyncio.wait(
            {query_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if query_task in done or query_task.done():
            return query_task.result()
        # Propagate a watcher failure instead of silently treating it as disconnect.
        disconnect_task.result()
        raise ClientDisconnectedError("Client disconnected")
    finally:
        stopped = True
        stage("watcher_cleanup_start")
        observe(disconnect_task, cancel=True)
        if not query_task.done():
            _DETACHED_QUERY_TASKS.add(query_task)
            query_task.add_done_callback(_consume_detached_query)
        stage("watcher_cleanup_detached")

UE = "https://universalevidence.com/ontology/"
AEA = "https://socialscienceregistry.org/schema#"
DCTERMS = "http://purl.org/dc/terms/"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
SKOS = "http://www.w3.org/2004/02/skos/core#"

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
ENGINE_DIR = REPO_ROOT / "engine"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from source_registry import (  # noqa: E402
    get_source_config,
    load_region_label_mapping,
    load_region_uri_mapping,
    load_source_configs,
)
from ctgov_location_context import canonical_location_key, study_locations  # noqa: E402

VOCABULARIES_DIR = REPO_ROOT / "vocabularies"
REGIONS_PATH = VOCABULARIES_DIR / "regions.ttl"

DEFAULT_FUSEKI_QUERY_URL = "http://localhost:3030/ue/query"
AEA_GRAPH_URI = "https://universalevidence.com/graph/aea"
WHO_ICTRP_GRAPH_URI = "https://universalevidence.com/graph/who-ictrp"
TAXONOMY_GRAPH_URI = "https://universalevidence.com/graph/ue-taxonomy"

CONDITION_PREFIX = "https://universalevidence.com/vocab/conditions/"
AEA_KEYWORD_PREFIX = "https://socialscienceregistry.org/schema#Keyword_"
ISRCTN_NS = "https://www.isrctn.com/schema#"
MESH_PREFIX = "https://id.nlm.nih.gov/mesh/"
MESH_PREFIXES = ("https://id.nlm.nih.gov/mesh/", "http://id.nlm.nih.gov/mesh/")
CTGOV_API_BASE = "https://clinicaltrials.gov/api/v2/studies"
ICTRP = "https://universalevidence.com/source/who-ictrp/schema#"
MESH_DESCRIPTOR_PATTERN = "D"
RESULT_KEYS = (
    "source",
    "study_id",
    "title",
    "url",
    "intervention",
    "intervention_concept",
    "intervention_concept_uri",
    "all_intervention_concepts",
    "condition_concept",
    "condition_concept_uri",
    "country",
    "status",
    "year",
    "outcomes",
)

_COUNTRY_NORM: Optional[dict[str, str]] = None
_COUNTRY_NORM_LOWER: Optional[dict[str, str]] = None
_COUNTRY_NORM_REVERSE: Optional[dict[str, set[str]]] = None
_COUNTRY_URI_NORM: Optional[dict[str, str]] = None
_COUNTRY_URI_NORM_LOWER: Optional[dict[str, str]] = None
_WARNED_COUNTRY_KEYS: set[str] = set()

# Crosswalk caches: rawText → (concept_uri, match_type)
_ISRCTN_CONDITIONS_XWALK: Optional[dict[str, str]] = None
_ISRCTN_INTERVENTIONS_XWALK: Optional[dict[str, str]] = None
_ISRCTN_REGIONS_XWALK: Optional[dict[str, str]] = None
_CTGOV_REGIONS_XWALK: Optional[dict[str, str]] = None

# Flat cache of all CrosswalkEntry triples from Fuseki (used when local TTL files absent)
_FUSEKI_XWALK_CACHE: Optional[dict[str, str]] = None


import re as _re

def _normalize_xwalk_key(text: str) -> str:
    """Collapse whitespace for crosswalk lookup — handles tab/CRLF/multi-space drift."""
    return _re.sub(r"\s+", " ", text).strip()


def _load_crosswalk(ttl_path: Path) -> dict[str, str]:
    """Load a crosswalk TTL and return {normalized_rawText: concept_uri}."""
    from rdflib import Graph, Namespace, URIRef
    from rdflib.namespace import SKOS as _SKOS
    _UE = Namespace(UE)
    g = Graph()
    g.parse(ttl_path, format="turtle")
    result: dict[str, str] = {}
    for entry in g.subjects(_UE.rawText, None):
        raw = str(g.value(entry, _UE.rawText) or "").strip()
        if not raw:
            continue
        concept = (
            g.value(entry, _SKOS.exactMatch)
            or g.value(entry, _SKOS.closeMatch)
        )
        if concept and isinstance(concept, URIRef):
            result[_normalize_xwalk_key(raw)] = str(concept)
    return result


def _load_fuseki_xwalk() -> dict[str, str]:
    """Load all CrosswalkEntry triples from Fuseki taxonomy graph into a flat dict."""
    global _FUSEKI_XWALK_CACHE
    if _FUSEKI_XWALK_CACHE is not None:
        return _FUSEKI_XWALK_CACHE
    query = f"""
    PREFIX ue: <{UE}>
    PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
    SELECT ?rawText ?concept WHERE {{
        GRAPH <{TAXONOMY_GRAPH_URI}> {{
            ?entry a ue:CrosswalkEntry ;
                   ue:rawText ?rawText .
            {{ ?entry skos:exactMatch ?concept }} UNION {{ ?entry skos:closeMatch ?concept }}
        }}
    }}
    """
    rows = sparql_select(query)
    _FUSEKI_XWALK_CACHE = {
        _normalize_xwalk_key(r["rawText"]): r["concept"]
        for r in rows
        if r.get("rawText") and r.get("concept")
    }
    logger.info("Loaded %d crosswalk entries from Fuseki", len(_FUSEKI_XWALK_CACHE))
    return _FUSEKI_XWALK_CACHE


@lru_cache(maxsize=1)
def _load_state_concept_uris() -> set[str]:
    """Return every concept URI typed ue:State, for filtering crosswalk matches
    that should only ever resolve to a state (not an indicator or intervention)."""
    query = f"""
PREFIX ue: <{UE}>
SELECT ?uri WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{ ?uri a ue:State . }}
}}
"""
    rows = sparql_select(query)
    return {row["uri"] for row in rows if row.get("uri")}


def get_isrctn_conditions_xwalk() -> dict[str, str]:
    global _ISRCTN_CONDITIONS_XWALK
    if _ISRCTN_CONDITIONS_XWALK is None:
        path = VOCABULARIES_DIR / "crosswalks" / "isrctn-states-crosswalk.ttl"
        _ISRCTN_CONDITIONS_XWALK = _load_crosswalk(path) if path.exists() else _load_fuseki_xwalk()
    return _ISRCTN_CONDITIONS_XWALK


def get_isrctn_interventions_xwalk() -> dict[str, str]:
    global _ISRCTN_INTERVENTIONS_XWALK
    if _ISRCTN_INTERVENTIONS_XWALK is None:
        path = VOCABULARIES_DIR / "crosswalks" / "isrctn-interventions-crosswalk.ttl"
        _ISRCTN_INTERVENTIONS_XWALK = _load_crosswalk(path) if path.exists() else _load_fuseki_xwalk()
    return _ISRCTN_INTERVENTIONS_XWALK


def get_isrctn_regions_xwalk() -> dict[str, str]:
    global _ISRCTN_REGIONS_XWALK
    if _ISRCTN_REGIONS_XWALK is None:
        path = VOCABULARIES_DIR / "crosswalks" / "isrctn-regions-crosswalk.ttl"
        _ISRCTN_REGIONS_XWALK = _load_crosswalk(path) if path.exists() else _load_fuseki_xwalk()
    return _ISRCTN_REGIONS_XWALK


def get_ctgov_regions_xwalk() -> dict[str, str]:
    """Load the contextual CT.gov country/ADM1 crosswalk.

    ADM1 keys are generated by ``canonical_location_key`` from one intact API
    location object.  Country fallback entries retain CT.gov's exact country
    literal.  Never fall back to a bare city lookup.
    """
    global _CTGOV_REGIONS_XWALK
    if _CTGOV_REGIONS_XWALK is None:
        path = VOCABULARIES_DIR / "crosswalks" / "ctgov-regions-crosswalk.ttl"
        # Mirrors/crosswalks are intentionally gitignored and may be absent
        # from the production API image even though the loader has placed them
        # in the Fuseki taxonomy graph. Use that active graph as the deployment
        # fallback; never read quarantine or a bare-city queue.
        loaded = (
            _load_crosswalk(path) if path.exists() else _load_fuseki_xwalk()
        )
        if not any(key.startswith("ctgov-location:v1|") for key in loaded):
            raise RuntimeError(
                "Contextual CT.gov region crosswalk is not loaded"
            )
        _CTGOV_REGIONS_XWALK = loaded
    return _CTGOV_REGIONS_XWALK


def ctgov_location_region_uris(study: dict) -> list[str]:
    """Resolve every verified country/ADM1 URI on a CT.gov study.

    Each lookup is scoped to its own ``locations[]`` record; context is never
    borrowed from an adjacent site in the same study.
    """
    crosswalk = get_ctgov_regions_xwalk()
    resolved: list[str] = []
    for context in study_locations(study):
        for raw_text in (canonical_location_key(context), context.country):
            if not raw_text:
                continue
            concept_uri = crosswalk.get(_normalize_xwalk_key(raw_text))
            if concept_uri and concept_uri not in resolved:
                resolved.append(concept_uri)
    return resolved


def source_key(source_uri: str) -> str:
    """Return the short source id from a source URI."""
    return source_uri.rstrip("/").rsplit("/", 1)[-1]


def local_name(uri: str) -> str:
    """Return the trailing local name for a URI."""
    return uri.rstrip("/#").rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def _source_config(key: str):
    """Return a runtime source config from sources.ttl."""
    return get_source_config(key)


def _source_graph_uri(key: str, fallback: str) -> str:
    return _source_config(key).graph_uri or fallback


def _sparql_predicate(uri: str) -> str:
    return sparql_iri(uri)


def _first_source_field(source_key_name: str, axis: str, role: str) -> str:
    field = _source_config(source_key_name).first_field(axis, role)
    if field is None:
        raise ValueError(f"{source_key_name} has no {role} field for {axis} in sources.ttl")
    return field


def _first_api_param(source_key_name: str, axis: str, role: str) -> Optional[str]:
    mappings = _source_config(source_key_name).mappings(axis, role)
    return mappings[0].api_param if mappings else None


def _source_endpoint(source_key_name: str, fallback: str) -> str:
    return _source_config(source_key_name).endpoint_url or fallback


def _source_label_for_graph(graph_uri: str) -> str:
    for config in load_source_configs().values():
        if config.graph_uri == graph_uri:
            if config.key == "aea":
                return "AEA"
            if config.key == "wholictrp":
                return "WHO ICTRP"
            return config.title or local_name(config.uri)
    return (
        "AEA" if "graph/aea" in graph_uri else
        "WHO ICTRP" if "who-ictrp" in graph_uri else
        local_name(graph_uri)
    )


def get_sparql_endpoint() -> str:
    """Return the Fuseki SPARQL SELECT endpoint."""
    return os.environ.get("FUSEKI_URL", DEFAULT_FUSEKI_QUERY_URL)


def get_fuseki_base() -> str:
    """Return the Fuseki dataset base URL from the configured query endpoint."""
    endpoint = get_sparql_endpoint().rstrip("/")
    if endpoint.endswith("/query"):
        return endpoint[: -len("/query")]
    return endpoint


def get_fuseki_update_url() -> str:
    """Return the Fuseki SPARQL UPDATE endpoint."""
    return f"{get_fuseki_base()}/update"


def get_fuseki_graph_store_url() -> str:
    """Return the Fuseki Graph Store endpoint."""
    return f"{get_fuseki_base()}/data"


def graph_store_url(graph_uri: str) -> str:
    """Return a Graph Store URL for a specific named graph."""
    return f"{get_fuseki_graph_store_url()}?{urlencode({'graph': graph_uri})}"


def sparql_string(value: str) -> str:
    """Return a SPARQL-safe string literal."""
    return json.dumps(value)


def sparql_iri(value: str) -> str:
    """Return a SPARQL IRI token."""
    if not (value.startswith("http://") or value.startswith("https://")):
        raise ValueError(f"Expected absolute IRI, got: {value}")
    return f"<{value}>"


def sparql_select(query: str, endpoint: Optional[str] = None) -> list[dict[str, str]]:
    """Run a SPARQL SELECT query and return simple string bindings."""
    url = endpoint or get_sparql_endpoint()
    response = requests.post(
        url,
        data={"query": query, "timeout": "30000,30000"},
        headers={"Accept": "application/sparql-results+json"},
        timeout=60,
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


def put_graph(graph_uri: str, turtle_payload: bytes) -> None:
    """Replace a named graph with Turtle content."""
    response = requests.put(
        graph_store_url(graph_uri),
        data=turtle_payload,
        headers={"Content-Type": "text/turtle"},
        timeout=30,
    )
    response.raise_for_status()


def delete_graph(graph_uri: str) -> None:
    """Delete a named graph from Fuseki."""
    response = requests.delete(graph_store_url(graph_uri), timeout=30)
    response.raise_for_status()


def load_country_norm_mapping(refresh: bool = False) -> dict[str, str]:
    """Load the country normalization lookup from the region vocabulary."""
    global _COUNTRY_NORM, _COUNTRY_NORM_LOWER

    if _COUNTRY_NORM is not None and not refresh:
        return _COUNTRY_NORM

    mapping = load_region_label_mapping(REGIONS_PATH)
    _COUNTRY_NORM = mapping
    _COUNTRY_NORM_LOWER = {k.lower(): v for k, v in mapping.items()}
    return _COUNTRY_NORM


def load_country_uri_mapping(refresh: bool = False) -> dict[str, str]:
    """Load country label aliases mapped to canonical GeoNames URIs."""
    global _COUNTRY_URI_NORM, _COUNTRY_URI_NORM_LOWER

    if _COUNTRY_URI_NORM is not None and not refresh:
        return _COUNTRY_URI_NORM

    mapping = load_region_uri_mapping(REGIONS_PATH)
    _COUNTRY_URI_NORM = mapping
    _COUNTRY_URI_NORM_LOWER = {k.lower(): v for k, v in mapping.items()}
    return _COUNTRY_URI_NORM


def warn_unmapped_country(country: str, context: str) -> None:
    """Log a warning once for each unmapped country value and context."""
    warning_key = f"{context}:{country}"
    if warning_key in _WARNED_COUNTRY_KEYS:
        return

    _WARNED_COUNTRY_KEYS.add(warning_key)
    logger.warning("Country %r not found in regions.ttl; using raw %s", country, context)


def load_country_reverse_index(refresh: bool = False) -> dict[str, set[str]]:
    """Invert the region label mapping to ISO -> set of raw literals."""
    global _COUNTRY_NORM_REVERSE

    if _COUNTRY_NORM_REVERSE is not None and not refresh:
        return _COUNTRY_NORM_REVERSE

    mapping = load_country_norm_mapping(refresh=refresh)
    reverse_index: defaultdict[str, set[str]] = defaultdict(set)
    for raw_literal, iso_code in mapping.items():
        reverse_index[iso_code].add(raw_literal)

    _COUNTRY_NORM_REVERSE = dict(reverse_index)
    return _COUNTRY_NORM_REVERSE


def normalize_country_to_iso(country: str, context: str = "input") -> str:
    """Normalize a country value to ISO alpha-2 where possible."""
    load_country_norm_mapping()
    normalized = _COUNTRY_NORM.get(country) or _COUNTRY_NORM_LOWER.get(country.lower())
    if normalized:
        return normalized

    warn_unmapped_country(country, context)
    return country


def normalize_country_to_region_uri(country: str, context: str = "input") -> str:
    """Normalize a country value to its canonical GeoNames region URI."""
    load_country_uri_mapping()
    normalized = _COUNTRY_URI_NORM.get(country) or _COUNTRY_URI_NORM_LOWER.get(country.lower())
    if normalized:
        return normalized

    warn_unmapped_country(country, context)
    return country


def normalize_country_output(country: str) -> str:
    """Normalize a result country value, falling back to the raw literal."""
    return normalize_country_to_iso(country, context="output")


def iso_to_country_name(iso: str) -> str:
    """Return a plain country name suitable for CT.gov query.locn.

    CT.gov query.locn is a text search; ISO codes return nothing.
    Picks the shortest key in the region label mapping that maps to the ISO code
    and contains no parentheses.
    """
    mapping = load_country_norm_mapping()
    candidates = [
        k for k, v in mapping.items()
        if v == iso and "(" not in k and len(k) > 2 and not k.isupper()
    ]
    return min(candidates, key=len) if candidates else iso


def expand_country_literals(country: str) -> tuple[str, set[str]]:
    """Normalize a user country input and expand it to matching raw literals."""
    iso_country = normalize_country_to_iso(country, context="input")
    reverse_index = load_country_reverse_index()
    country_literals = set(reverse_index.get(iso_country, set()))
    if country_literals:
        return iso_country, country_literals

    return iso_country, {country, iso_country}


def resolve_condition_node(challenge: str) -> str:
    """Resolve a challenge by full URI, uec: prefix, label, or altLabel."""
    challenge_text = challenge.strip()
    candidate_uris = []
    if challenge_text.startswith("uec:"):
        candidate_uris.append(f"{CONDITION_PREFIX}{challenge_text.split(':', 1)[1]}")
    elif challenge_text.startswith("http://") or challenge_text.startswith("https://"):
        candidate_uris.append(challenge_text)

    if candidate_uris:
        values = " ".join(sparql_iri(uri) for uri in candidate_uris)
        query = f"""
PREFIX ue: <{UE}>
PREFIX rdf: <{RDF}>
SELECT ?challenge WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    VALUES ?challenge {{ {values} }}
    ?challenge rdf:type ue:State .
  }}
}}
LIMIT 1
"""
        rows = sparql_select(query)
        if rows:
            return rows[0]["challenge"]

    normalized = challenge_text.casefold()
    query = f"""
PREFIX ue: <{UE}>
PREFIX rdf: <{RDF}>
PREFIX rdfs: <{RDFS}>
PREFIX skos: <{SKOS}>
SELECT ?challenge WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    ?challenge rdf:type ue:State ;
      ?labelPredicate ?label .
    VALUES ?labelPredicate {{ rdfs:label skos:prefLabel skos:altLabel }}
    FILTER(STRSTARTS(STR(?challenge), {sparql_string(CONDITION_PREFIX)}))
    FILTER(LCASE(STR(?label)) = {sparql_string(normalized)})
  }}
}}
LIMIT 1
"""
    rows = sparql_select(query)
    if rows:
        return rows[0]["challenge"]

    raise ValueError(f"Unknown challenge: {challenge}")

def resolve_region_country(region: str) -> str:
    """Resolve a region URI (country or admin1) to an ISO alpha-2 country code.

    Handles both legacy uer: concepts (ue:iso3166Alpha2) and GeoNames URIs
    (gn:countryCode on the concept itself, or via skos:broader for admin1).
    Regional group concepts (World Bank regions, World) have no country code
    and raise ValueError.
    """
    rows = sparql_select(
        f"""
PREFIX ue:   <{UE}>
PREFIX gn:   <http://www.geonames.org/ontology#>
PREFIX rdfs: <{RDFS}>
PREFIX skos: <{SKOS}>

SELECT ?iso ?label WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    {sparql_iri(region)} a ue:Region .
    OPTIONAL {{ {sparql_iri(region)} ue:iso3166Alpha2 ?legacyIso . }}
    OPTIONAL {{ {sparql_iri(region)} gn:countryCode ?directCode . }}
    OPTIONAL {{
      {sparql_iri(region)} skos:broader ?parent .
      ?parent gn:countryCode ?parentCode .
    }}
    BIND(COALESCE(?legacyIso, ?directCode, ?parentCode) AS ?iso)
    OPTIONAL {{ {sparql_iri(region)} rdfs:label ?rdfsLabel . }}
    OPTIONAL {{ {sparql_iri(region)} skos:prefLabel ?prefLabel . }}
    BIND(COALESCE(?rdfsLabel, ?prefLabel) AS ?label)
  }}
}}
LIMIT 1
"""
    )
    if not rows:
        raise ValueError(f"Unknown region: {region}")
    if rows[0].get("iso"):
        return rows[0]["iso"]
    label = rows[0].get("label") or local_name(region)
    raise ValueError(
        f"Region group queries are not supported by the current dispatcher: {label}"
    )


MAX_REGION_GROUP_COUNTRIES = 75


def resolve_region_countries(region: str) -> list[dict[str, str]]:
    """Return country URI/ISO/name records for a country or region group."""
    rows = sparql_select(f"""
PREFIX ue: <{UE}>
PREFIX skos: <{SKOS}>
SELECT DISTINCT ?country ?iso ?name WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    {sparql_iri(region)} skos:narrowerTransitive? ?country .
    ?country a ue:Region ; ue:iso3166Alpha2 ?iso ; skos:prefLabel ?name .
  }}
}}
ORDER BY ?iso
""")
    if not rows:
        # Preserve the existing useful error for unknown/non-country values.
        return [{"country": region, "iso": resolve_region_country(region), "name": country_name_from_iso(resolve_region_country(region))}]
    countries = [{"country": r["country"], "iso": r["iso"], "name": r.get("name") or r["iso"]} for r in rows]
    if len(countries) > MAX_REGION_GROUP_COUNTRIES:
        raise ValueError(f"Region group expands to {len(countries)} countries; maximum is {MAX_REGION_GROUP_COUNTRIES}")
    return countries


def _region_is_admin1(region_uri: str) -> bool:
    """Return True if region_uri is a GeoNames admin1 (state/province) concept."""
    rows = sparql_select(f"""
PREFIX gn:   <http://www.geonames.org/ontology#>
PREFIX ue:   <{UE}>
SELECT ?code WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    {sparql_iri(region_uri)} gn:featureCode ?code .
  }}
}} LIMIT 1
""")
    if not rows:
        return False
    code = rows[0].get("code", "")
    return "ADM1" in str(code)


def country_name_from_iso(iso: str) -> str:
    """Return a display name for an ISO alpha-2 code using the taxonomy.

    Country-level concepts use ue:iso3166Alpha2 (not gn:countryCode, which is
    only on admin1 regions). Falls back to the ISO code if not found.
    """
    rows = sparql_select(f"""
PREFIX ue:   <{UE}>
PREFIX skos: <{SKOS}>
SELECT ?name WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    ?country ue:iso3166Alpha2 "{iso}" ;
             skos:prefLabel ?name .
  }}
}} LIMIT 1
""")
    return rows[0]["name"] if rows and rows[0].get("name") else iso


def extract_mesh_exact_matches(challenge_node: str) -> list[str]:
    """Return MeSH exact-match URIs for a challenge and all its descendants."""
    mesh_filters = " || ".join(
        f"STRSTARTS(STR(?mesh), {sparql_string(prefix)})"
        for prefix in MESH_PREFIXES
    )
    query = f"""
PREFIX skos: <{SKOS}>
SELECT DISTINCT ?mesh WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    {sparql_iri(challenge_node)} skos:narrowerTransitive? ?concept .
    ?concept skos:exactMatch ?mesh .
    FILTER({mesh_filters})
  }}
}}
ORDER BY ?mesh
"""
    return [row["mesh"] for row in sparql_select(query) if row.get("mesh")]


def get_challenge_label(challenge_node: str) -> str:
    """Return the display label for a challenge."""
    query = f"""
PREFIX rdfs: <{RDFS}>
PREFIX skos: <{SKOS}>
SELECT ?label WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    {sparql_iri(challenge_node)} ?labelPredicate ?label .
    VALUES ?labelPredicate {{ rdfs:label skos:prefLabel }}
  }}
}}
LIMIT 1
"""
    rows = sparql_select(query)
    if not rows or not rows[0].get("label"):
        raise ValueError(f"Challenge is missing rdfs:label or skos:prefLabel: {challenge_node}")
    return rows[0]["label"]


def get_concept_label(concept_uri: str) -> Optional[str]:
    """Return the display label for any taxonomy concept URI, or None if not found."""
    query = f"""
PREFIX rdfs: <{RDFS}>
PREFIX skos: <{SKOS}>
SELECT ?label WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    {sparql_iri(concept_uri)} ?labelPredicate ?label .
    VALUES ?labelPredicate {{ rdfs:label skos:prefLabel }}
  }}
}}
LIMIT 1
"""
    rows = sparql_select(query)
    return rows[0]["label"] if rows and rows[0].get("label") else None


def get_concept_search_terms(concept_uri: str) -> list[str]:
    """Return all search terms for a concept: its own labels plus narrower concept labels.

    Collects prefLabel, altLabels, and rdfs:label for the concept itself, then
    prefLabel and altLabels for any directly narrower concepts (skos:broader inverse).
    Used to build OR queries for live APIs like CT.gov query.intr / query.outc.
    """
    query = f"""
PREFIX rdfs: <{RDFS}>
PREFIX skos: <{SKOS}>
SELECT DISTINCT ?term WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    {{
      {sparql_iri(concept_uri)} skos:prefLabel|skos:altLabel|rdfs:label ?term .
    }}
    UNION
    {{
      ?narrower skos:broader {sparql_iri(concept_uri)} ;
               skos:prefLabel|skos:altLabel ?term .
    }}
  }}
}}
"""
    seen: set[str] = set()
    terms: list[str] = []
    for row in sparql_select(query):
        t = row.get("term", "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            terms.append(t)
    return terms


def lookup_intervention_concepts_by_label(texts: list[str]) -> dict[str, tuple[str, str]]:
    """Map raw intervention text strings to (concept_uri, concept_label) via prefLabel/altLabel.

    Returns a dict keyed by the original text (case-insensitive matched).
    """
    if not texts:
        return {}
    values_block = " ".join(f'"{t.replace(chr(34), chr(92)+chr(34))}"' for t in texts)
    query = f"""
PREFIX rdfs: <{RDFS}>
PREFIX skos: <{SKOS}>
PREFIX ue: <{UE}>
SELECT ?text ?concept ?prefLabel WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    ?concept a ue:Intervention ;
             skos:prefLabel ?prefLabel ;
             ?labelPred ?text .
    VALUES ?labelPred {{ skos:prefLabel skos:altLabel rdfs:label }}
    VALUES ?text {{ {values_block} }}
  }}
}}
"""
    result: dict[str, tuple[str, str]] = {}
    for row in sparql_select(query):
        text = row.get("text", "")
        concept = row.get("concept", "")
        label = row.get("prefLabel", "")
        if text and concept and label and text not in result:
            result[text] = (concept, label)
    return result


def extract_study_id(study: str) -> str:
    """Return the trailing AEA study identifier from the subject URI."""
    return study.rsplit("_", 1)[-1]


def extract_study_year(raw_year: Optional[str]) -> Optional[int]:
    """Extract a four-digit study year from a raw string."""
    if raw_year and raw_year[:4].isdigit():
        return int(raw_year[:4])
    return None


def prespecified_outcome(measure: str, **extra: Optional[str]) -> dict:
    """Return a pre-specified outcome object."""
    outcome = {
        "type": "pre-specified-measure",
        "measure": measure,
    }
    for key, value in extra.items():
        if value:
            outcome[key] = value
    return outcome


def reported_outcome(measure: str, **extra: Optional[str]) -> dict:
    """Return a reported-result outcome object without inferring direction."""
    outcome = {
        "type": "reported-result",
        "measure": measure,
    }
    for key, value in extra.items():
        if value:
            outcome[key] = value
    return outcome


def adverse_event_outcome(measure: str, **extra: Optional[str]) -> dict:
    """Return an adverse-event outcome object without inferring direction."""
    outcome = {
        "type": "adverse-event",
        "measure": measure,
    }
    for key, value in extra.items():
        if value:
            outcome[key] = value
    return outcome


def aea_outcomes(primary_outcome: Optional[str]) -> list[dict]:
    """Map AEA primaryOutcome free text to a pre-specified outcome, enriched with concept when matched."""
    if not primary_outcome or not primary_outcome.strip():
        return []
    raw = primary_outcome.strip()
    outcome = prespecified_outcome(raw)
    try:
        match = _load_aea_outcome_concept_map().get(raw)
        if match:
            outcome["state_concept"] = match[1]
            outcome["state_concept_uri"] = match[0]
    except Exception:
        pass
    return [outcome]



@lru_cache(maxsize=1)
def _load_aea_outcome_concept_map() -> dict[str, tuple[str, str]]:
    query = f"""
PREFIX skos: <{SKOS}>
PREFIX ue:   <{UE}>
SELECT ?rawText ?conceptUri ?conceptLabel ?matchType WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    {{
      ?entry ue:rawText ?rawText ; skos:exactMatch ?conceptUri .
      BIND("exact" AS ?matchType)
    }} UNION {{
      ?entry ue:rawText ?rawText ; skos:closeMatch ?conceptUri .
      BIND("close" AS ?matchType)
    }}
    ?conceptUri a ue:State ;
               skos:prefLabel ?conceptLabel .
  }}
}}
ORDER BY ?rawText DESC(?matchType)
"""
    rows = sparql_select(query)
    result: dict[str, tuple[str, str]] = {}
    for row in rows:
        raw = row.get("rawText", "")
        uri = row.get("conceptUri", "")
        label = row.get("conceptLabel", "")
        if raw and uri and label and raw not in result:
            result[raw] = (uri, label)
    return result


@lru_cache(maxsize=1)
def _load_ctgov_outcome_concept_map() -> dict[str, tuple[str, str]]:
    query = f"""
PREFIX skos: <{SKOS}>
PREFIX ue:   <{UE}>
SELECT ?rawText ?conceptUri ?conceptLabel ?matchType WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    {{
      ?entry ue:rawText ?rawText ; skos:exactMatch ?conceptUri .
      BIND("exact" AS ?matchType)
    }} UNION {{
      ?entry ue:rawText ?rawText ; skos:closeMatch ?conceptUri .
      BIND("close" AS ?matchType)
    }}
    ?conceptUri a ue:State ;
               skos:prefLabel ?conceptLabel .
    FILTER(STRSTARTS(STR(?entry), "https://universalevidence.com/crosswalk/ctgov-outcomes/"))
  }}
}}
ORDER BY ?rawText DESC(?matchType)
"""
    rows = sparql_select(query)
    result: dict[str, tuple[str, str]] = {}
    for row in rows:
        raw = row.get("rawText", "")
        uri = row.get("conceptUri", "")
        label = row.get("conceptLabel", "")
        if raw and uri and label and raw not in result:
            result[raw] = (uri, label)
    return result


@lru_cache(maxsize=1)
def _load_ctgov_intervention_concept_map() -> dict[str, tuple[str, str]]:
    """Map CT.gov intervention text through its purpose-built crosswalk."""
    query = f"""
PREFIX skos: <{SKOS}>
PREFIX ue:   <{UE}>
SELECT ?rawText ?conceptUri ?conceptLabel ?matchType WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    {{
      ?entry ue:rawText ?rawText ; skos:exactMatch ?conceptUri .
      BIND("exact" AS ?matchType)
    }} UNION {{
      ?entry ue:rawText ?rawText ; skos:closeMatch ?conceptUri .
      BIND("close" AS ?matchType)
    }}
    ?conceptUri a ue:Intervention ;
                skos:prefLabel ?conceptLabel .
    FILTER(STRSTARTS(STR(?entry), "https://universalevidence.com/crosswalk/ctgov-interventions/"))
  }}
}}
ORDER BY ?rawText DESC(?matchType)
"""
    rows = sparql_select(query)
    result: dict[str, tuple[str, str]] = {}
    for row in rows:
        raw = row.get("rawText", "")
        uri = row.get("conceptUri", "")
        label = row.get("conceptLabel", "")
        if raw and uri and label and raw not in result:
            result[raw] = (uri, label)
    return result


def _attach_ctgov_intervention_concept(
    result: dict,
    concept_map: Optional[dict[str, tuple[str, str]]] = None,
) -> dict:
    """Attach a crosswalk concept without replacing an existing classification."""
    if result.get("intervention_concept_uri") or not result.get("intervention"):
        return result
    if concept_map is None:
        try:
            concept_map = _load_ctgov_intervention_concept_map()
        except Exception:
            logger.warning("CT.gov intervention crosswalk lookup failed", exc_info=True)
            return result
    match = ctgov_intervention_lookup(
        concept_map, result.get("study_id") or result.get("nctId"), result["intervention"]
    )
    if match:
        result["intervention_concept_uri"], result["intervention_concept"] = match
    return result


def _attach_ctgov_outcome_concept(outcome: dict) -> dict:
    """Attach a states.ttl concept to a CT.gov outcome when its measure has an exact crosswalk row."""
    measure = outcome.get("measure")
    if not measure:
        return outcome
    try:
        match = _load_ctgov_outcome_concept_map().get(measure)
    except Exception:
        match = None
    if match:
        outcome["state_concept_uri"] = match[0]
        outcome["state_concept"] = match[1]
    return outcome

def mesh_descriptor_id(mesh_uri: str) -> Optional[str]:
    """Extract a MeSH descriptor D-number from a MeSH URI."""
    descriptor = local_name(mesh_uri)
    if descriptor.startswith(MESH_DESCRIPTOR_PATTERN) and descriptor[1:].isdigit():
        return descriptor
    return None


CTGOV_MESH_FILTER_CHUNK_SIZE = 20
def ctgov_condition_mesh_filter(
    mesh_uris: list[str],
    chunk_size: int = CTGOV_MESH_FILTER_CHUNK_SIZE,
    max_chunks: Optional[int] = None,
) -> list[str]:
    """Build CT.gov advanced condition filters from MeSH descriptor URIs.

    CT.gov rejects filter.advanced values with too many OR'd terms (413
    Request Entity Too Large), so descriptor IDs are split into chunks. Each
    chunk must be queried separately and the results merged by the caller.
    All descriptor chunks are returned; callers must bound request concurrency
    rather than dropping descriptors.
    """
    descriptor_ids = []
    seen = set()
    for mesh_uri in mesh_uris:
        descriptor_id = mesh_descriptor_id(mesh_uri)
        if descriptor_id and descriptor_id not in seen:
            seen.add(descriptor_id)
            descriptor_ids.append(descriptor_id)
    chunks = [
        " OR ".join(
            f"AREA[ConditionMeshId]{descriptor_id}"
            for descriptor_id in descriptor_ids[i : i + chunk_size]
        )
        for i in range(0, len(descriptor_ids), chunk_size)
    ]
    logger.info(
        "CT.gov condition filter coverage: %d unique MeSH descriptors across %d chunks",
        len(descriptor_ids), len(chunks),
    )
    if max_chunks is not None and len(chunks) > max_chunks:
        logger.warning(
            "CT.gov condition filter explicitly truncated: %d MeSH descriptors need %d chunks, capping to %d",
            len(descriptor_ids), len(chunks), max_chunks,
        )
        chunks = chunks[:max_chunks]
    return chunks


ISRCTN_INTR_TERM_CHUNK_SIZE = 15
ISRCTN_MAX_INTR_TERM_CHUNKS = 15
def isrctn_intervention_query_chunks(
    intr_terms: list[str],
    chunk_size: int = ISRCTN_INTR_TERM_CHUNK_SIZE,
    max_chunks: Optional[int] = ISRCTN_MAX_INTR_TERM_CHUNKS,
) -> list[str]:
    """Split intervention search terms into ISRCTN q= OR-clause chunks.

    ISRCTN rejects long q= values with 414 Request-URI Too Large once enough
    terms are OR'd together (observed failure: 65 terms / ~3.2KB URL). Terms
    are split into fixed-size chunks; callers query each chunk separately and
    merge results, mirroring ctgov_condition_mesh_filter's chunking for the
    same class of bug on CT.gov.
    """
    quoted = [f'"{t}"' if " " in t else t for t in intr_terms]
    chunks = [
        " OR ".join(f"intervention: {t}" for t in quoted[i : i + chunk_size])
        for i in range(0, len(quoted), chunk_size)
    ]
    if max_chunks is not None and len(chunks) > max_chunks:
        logger.warning(
            "ISRCTN intervention filter truncated: %d terms need %d chunks, capping to %d",
            len(quoted), len(chunks), max_chunks,
        )
        chunks = chunks[:max_chunks]
    return chunks


def fetch_ctgov_studies(mesh_uris: list[str], country_name: str) -> list[dict]:
    """Fetch CT.gov studies for MeSH condition descriptors and country name."""
    condition_filters = ctgov_condition_mesh_filter(mesh_uris)
    if not condition_filters:
        return []
    region_param = _first_api_param("ctgov", "region", "live_api_param") or "query.locn"
    studies: list[dict] = []
    for condition_filter in condition_filters:
        params = {
            "filter.advanced": condition_filter,
            region_param: country_name,
        }
        response = requests.get(_source_endpoint("ctgov", CTGOV_API_BASE), params=params, timeout=30)
        response.raise_for_status()
        studies.extend(response.json().get("studies", []))
    return studies


def extract_ctgov_year(raw_date: Optional[str]) -> Optional[int]:
    """Extract the leading year from a CT.gov date string."""
    if raw_date and raw_date[:4].isdigit():
        return int(raw_date[:4])
    return None


def summarize_ctgov_measurements(outcome: dict) -> Optional[str]:
    """Return a compact summary of CT.gov measurement values when present."""
    values: list[str] = []
    for group in outcome.get("groups", []) or []:
        group_id = group.get("id")
        for measurement in group.get("measurements", []) or []:
            value = measurement.get("value")
            if value is None:
                continue
            spread = measurement.get("spread")
            unit = measurement.get("unitOfMeasure") or outcome.get("unitOfMeasure")
            pieces = []
            if group_id:
                pieces.append(str(group_id))
            pieces.append(str(value))
            if spread:
                pieces.append(f"({spread})")
            if unit:
                pieces.append(str(unit))
            values.append(" ".join(pieces))

    for class_item in outcome.get("classes", []) or []:
        for category in class_item.get("categories", []) or []:
            for measurement in category.get("measurements", []) or []:
                value = measurement.get("value")
                if value is None:
                    continue
                title = category.get("title") or class_item.get("title")
                unit = measurement.get("unitOfMeasure") or outcome.get("unitOfMeasure")
                pieces = []
                if title:
                    pieces.append(str(title))
                pieces.append(str(value))
                if unit:
                    pieces.append(str(unit))
                values.append(" ".join(pieces))

    return "; ".join(values) if values else None


def ctgov_outcome_item(
    outcome: dict,
    fallback_type: str,
    *,
    include_concept: bool = True,
) -> Optional[dict]:
    """Map one CT.gov outcome entry without interpreting effect direction."""
    measure = outcome.get("measure")
    if not measure:
        return None

    extra = {
        "description": outcome.get("description"),
        "time_frame": outcome.get("timeFrame"),
        "summary_statistic": summarize_ctgov_measurements(outcome),
    }
    if extra["summary_statistic"]:
        mapped = reported_outcome(measure, **extra)
        return _attach_ctgov_outcome_concept(mapped) if include_concept else mapped
    outcome = prespecified_outcome(measure, **extra) if fallback_type == "pre-specified-measure" else reported_outcome(measure, **extra)
    return _attach_ctgov_outcome_concept(outcome) if include_concept else outcome


def summarize_ctgov_adverse_event_stats(stats: Optional[object]) -> Optional[str]:
    """Return a compact string for CT.gov adverse event stats."""
    if not stats:
        return None
    if isinstance(stats, str):
        return stats
    if isinstance(stats, dict):
        stats = [stats]
    if not isinstance(stats, list):
        return str(stats)

    values: list[str] = []
    for stat in stats:
        if not isinstance(stat, dict):
            values.append(str(stat))
            continue
        pieces = []
        group_id = stat.get("groupId")
        if group_id:
            pieces.append(str(group_id))
        for label, key in (
            ("events", "numEvents"),
            ("affected", "numAffected"),
            ("at risk", "numAtRisk"),
        ):
            value = stat.get(key)
            if value is not None:
                pieces.append(f"{label}={value}")
        if pieces:
            values.append(" ".join(pieces))
    return "; ".join(values) if values else None


def extract_ctgov_outcomes(
    study: dict,
    *,
    include_concepts: bool = True,
) -> list[dict]:
    """Extract CT.gov outcome and adverse event data as nested result objects."""
    results_section = study.get("resultsSection", {})
    protocol = study.get("protocolSection", {})
    outcomes_module = results_section.get("outcomesModule") or protocol.get("outcomesModule") or {}
    outcomes: list[dict] = []

    for key in ("primaryOutcomes", "secondaryOutcomes", "otherOutcomes"):
        for item in outcomes_module.get(key, []) or []:
            mapped = ctgov_outcome_item(
                item,
                "pre-specified-measure",
                include_concept=include_concepts,
            )
            if mapped:
                outcomes.append(mapped)

    adverse_events = results_section.get("adverseEventsModule") or protocol.get("adverseEventsModule") or {}
    for event_group in ("seriousEvents", "otherEvents"):
        for event in adverse_events.get(event_group, []) or []:
            term = event.get("term") or event.get("organSystem")
            if not term:
                continue
            extra = {
                "description": event.get("organSystem"),
                "summary_statistic": (
                    summarize_ctgov_adverse_event_stats(event.get("stats"))
                    or event.get("notes")
                ),
            }
            outcomes.append(adverse_event_outcome(term, **extra))

    return outcomes


def map_ctgov_study(
    study: dict,
    iso_country: str,
    *,
    include_region_evidence: bool = False,
    include_outcome_concepts: bool = True,
) -> Optional[dict]:
    """Map a CT.gov study item into the UE result shape."""
    protocol = study.get("protocolSection", {})
    identification = protocol.get("identificationModule", {})
    nct_id = identification.get("nctId")
    if not nct_id:
        return None

    arms = protocol.get("armsInterventionsModule", {})
    interventions = arms.get("interventions", [])
    intervention_names = list(
        dict.fromkeys(
            name
            for item in interventions
            if (name := (item.get("name") or "").strip())
        )
    )
    intervention_name = intervention_names[0] if intervention_names else None

    status_module = protocol.get("statusModule", {})
    start_date = status_module.get("startDateStruct", {}).get("date")

    conditions_module = protocol.get("conditionsModule", {})
    derived = study.get("derivedSection", {})
    condition_meshes = [
        f"http://id.nlm.nih.gov/mesh/{m['id']}"
        for m in derived.get("conditionBrowseModule", {}).get("meshes", [])
        if m.get("id", "").startswith("D")
    ]
    intervention_meshes = [
        f"http://id.nlm.nih.gov/mesh/{m['id']}"
        for m in derived.get("interventionBrowseModule", {}).get("meshes", [])
        if m.get("id", "").startswith("D")
    ]

    raw_status = status_module.get("overallStatus")
    status_map = {
        "COMPLETED": "completed",
        "NOT_YET_RECRUITING": "in_development",
        "RECRUITING": "on_going",
        "ACTIVE_NOT_RECRUITING": "on_going",
        "ENROLLING_BY_INVITATION": "on_going",
    }

    return {
        "source": "CT.gov",
        "nctId": nct_id,
        "title": identification.get("briefTitle"),
        "url": f"https://clinicaltrials.gov/study/{nct_id}",
        "intervention": intervention_name,
        "_intervention_names": intervention_names,
        "condition_mesh_uris": condition_meshes,
        "intervention_mesh_uris": intervention_meshes,
        "country": iso_country,
        # Internal source-aware region evidence.  The public result formatter
        # currently omits this field; hierarchical filtering can consume it
        # without recomputing or using unsafe bare-city keys.
        "_location_region_uris": (
            ctgov_location_region_uris(study) if include_region_evidence else []
        ),
        # Stopped/unclear CT.gov enums intentionally remain raw so the UI
        # fallback displays them and excludes them from completed counts.
        "status": status_map.get(raw_status, raw_status),
        "year": extract_ctgov_year(start_date),
        "outcomes": extract_ctgov_outcomes(
            study,
            include_concepts=include_outcome_concepts,
        ),
    }


def dedupe_and_sort_results(results: list[dict]) -> list[dict]:
    """Deduplicate and sort by year descending.

    Key: (source, study_id, intervention_concept_uri) so a study with multiple matched
    intervention concepts produces one row per concept (enabling multi-group list display).
    Falls back to (source, study_id) for results without a concept URI.
    Within the same key, prefer rows with condition_concept_uri resolved.
    """
    seen: dict[tuple, dict] = {}
    for result in results:
        formatted = format_result(result)
        source = formatted.get("source")
        study_id = formatted.get("study_id")
        if source is None or study_id is None:
            logger.warning("Skipping result with missing source or study_id: %r", formatted)
            continue
        concept_uri = formatted.get("intervention_concept_uri")
        intervention_key = concept_uri or f"raw:{formatted.get('intervention') or ''}"
        key = (source, study_id, intervention_key)
        existing = seen.get(key)
        if existing is None or (
            not existing.get("condition_concept_uri") and formatted.get("condition_concept_uri")
        ):
            seen[key] = formatted
    return sorted(seen.values(), key=lambda result: result.get("year") or 0, reverse=True)


def format_result(result: dict) -> dict:
    """Return a result dict constrained to the public output schema."""
    formatted = {key: result.get(key) for key in RESULT_KEYS}
    formatted["outcomes"] = result.get("outcomes") or []
    return formatted


_ICTRP_AXIS_PREDICATES = {
    "condition":    f"{ICTRP}condition",
    "intervention": f"{ICTRP}intervention",
    "outcome":      f"{ICTRP}primaryOutcome",
}


def build_who_ictrp_crosswalk_query(axes: dict[str, str]) -> Optional[str]:
    """Build a WHO ICTRP query using crosswalk joins on raw string literals.

    Matches ictrp:condition / ictrp:intervention / ictrp:primaryOutcome literals
    against ue:CrosswalkEntry.ue:rawText in the taxonomy graph.
    """
    crosswalk_axes = [a for a in ("condition", "intervention", "outcome", "state") if axes.get(a)]
    if not crosswalk_axes and not axes.get("region"):
        return None

    # Inline stamp patterns (no GRAPH wrapper) — placed first inside the ICTRP GRAPH block
    stamp_lines = []
    for axis in crosswalk_axes:
        if axis == "state":
            stamp_lines.append(
                f"    ?study (<{_AXIS_MATCH_PRED['condition']}>|"
                f"<{_AXIS_MATCH_PRED['outcome']}>) <{axes[axis]}> ."
            )
        else:
            if axis not in _AXIS_MATCH_PRED:
                continue
            match_pred = _AXIS_MATCH_PRED[axis]
            stamp_lines.append(f"    ?study <{match_pred}> <{axes[axis]}> .")

    region_line = ""
    matched_country_name: Optional[str] = None
    if axes.get("region"):
        # WHO-ICTRP only has country-level location data (ictrp:countryIsoAlpha2).
        # Filtering by an admin1 URI would collapse to the parent country and return
        # all studies from that country, not admin1-specific results. Skip region
        # filter for admin1 concepts; country-level and regional groups still work.
        region_uri = axes["region"]
        is_admin1 = _region_is_admin1(region_uri)
        if is_admin1:
            logger.info("WHO-ICTRP region filter skipped for admin1: %s", region_uri)
        else:
            try:
                countries = resolve_region_countries(region_uri)
                isos = " ".join(sparql_string(c["iso"]) for c in countries)
                region_line = f"    VALUES ?regionIso {{ {isos} }}\n    ?study <{ICTRP}countryIsoAlpha2> ?regionIso ."
                matched_country_name = countries[0]["name"] if len(countries) == 1 else None
            except ValueError:
                logger.warning("WHO-ICTRP region filter skipped: %s", region_uri)

    if not stamp_lines and not region_line:
        return None

    inline_filter = "\n".join(stamp_lines) + ("\n" + region_line if region_line else "")

    ictrp_intervention_clause = ""
    if not axes.get("intervention"):
        ictrp_intervention_clause = f"""
  OPTIONAL {{
    GRAPH <{WHO_ICTRP_GRAPH_URI}> {{ ?study ue:matchesIntervention ?interventionUri . }}
    GRAPH <{TAXONOMY_GRAPH_URI}> {{ ?interventionUri skos:prefLabel ?interventionLabel . }}
  }}
"""

    country_clause = (
        f'    BIND("{matched_country_name}" AS ?country)'
        if matched_country_name
        else "    OPTIONAL { ?study ictrp:country ?country . }"
    )

    order_clause = "" if LOCAL_QUERY_FAST_WHO_ORDER else "ORDER BY DESC(?date) ?study_id"
    return f"""
PREFIX ue:    <{UE}>
PREFIX ictrp: <{ICTRP}>
PREFIX dcterms: <{DCTERMS}>
PREFIX skos:  <http://www.w3.org/2004/02/skos/core#>
SELECT DISTINCT ?graph ?study ?source ?study_id ?title
                ?interventionUri ?interventionLabel
                ?country ?status ?date ?outcomeText WHERE {{
  BIND(<{WHO_ICTRP_GRAPH_URI}> AS ?graph)
  GRAPH <{WHO_ICTRP_GRAPH_URI}> {{
{inline_filter}
    OPTIONAL {{ ?study dcterms:identifier ?study_id . }}
    OPTIONAL {{ ?study dcterms:title ?title . }}
{country_clause}
    OPTIONAL {{ ?study ictrp:dateOfRegistration ?date . }}
    OPTIONAL {{ ?study ictrp:primaryOutcome|ictrp:secondaryOutcome ?outcomeText . }}
  }}
{ictrp_intervention_clause}}}
{order_clause}
LIMIT 100
"""


_AXIS_MATCH_PRED = {
    "condition":    f"{UE}matchesCondition",
    "intervention": f"{UE}matchesIntervention",
    "outcome":      f"{UE}matchesOutcome",
}


def _aea_axis_clause(axis: str, axis_uri: str) -> str:
    """Return an inline stamp triple for one AEA axis (no GRAPH wrapper).

    Returned string is meant to be placed INSIDE the AEA GRAPH block as the
    first required pattern so Fuseki uses it as the leading filter.
    """
    if axis not in _AXIS_MATCH_PRED:
        raise ValueError(f"Unsupported AEA crosswalk axis: {axis}")
    match_pred = _AXIS_MATCH_PRED[axis]
    return f"    ?study <{match_pred}> <{axis_uri}> ."


def build_aea_crosswalk_sparql_query(axes: dict[str, str]) -> Optional[str]:
    """Build an AEA-only query using keyword mirror taxonomy crosswalks."""
    aea_graph_uri = _source_graph_uri("aea", AEA_GRAPH_URI)
    intervention_field = _first_source_field("aea", "intervention", "raw_text")
    outcome_field = _first_source_field("aea", "outcome", "raw_text")
    crosswalk_axes = [
        axis for axis in ("condition", "intervention", "outcome", "state")
        if axes.get(axis)
    ]
    if not crosswalk_axes and not axes.get("region"):
        return None

    axis_clauses = "\n".join(
        (
            f"    ?study (<{_AXIS_MATCH_PRED['condition']}>|"
            f"<{_AXIS_MATCH_PRED['outcome']}>) <{axes[axis]}> ."
            if axis == "state"
            else _aea_axis_clause(axis, axes[axis])
        )
        for axis in crosswalk_axes
    )
    location_clause = ""
    if axes.get("region"):
        region_field = _first_source_field("aea", "region", "region_literal")
        region_uris = " ".join(f"<{c['country']}>" for c in resolve_region_countries(axes["region"]))
        location_clause = f"""
    ?study {_sparql_predicate(region_field)} ?region_raw .
  }}
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    ?region_entry ue:rawText ?region_raw ;
                  (skos:exactMatch|skos:closeMatch) ?region_concept .
    VALUES ?region_concept {{ {region_uris} }}
  }}
  GRAPH <{aea_graph_uri}> {{
"""
    intervention_label_clause = ""
    if axes.get("intervention"):
        intervention_label_clause = f"""
  BIND(<{axes['intervention']}> AS ?interventionUri)
  OPTIONAL {{
    GRAPH <{TAXONOMY_GRAPH_URI}> {{
      ?interventionUri rdfs:label|skos:prefLabel ?interventionLabel .
    }}
  }}
"""

    return f"""
PREFIX ue:      <{UE}>
PREFIX aea:     <{AEA}>
PREFIX dcterms: <{DCTERMS}>
PREFIX rdfs:    <{RDFS}>
PREFIX skos:    <{SKOS}>
SELECT DISTINCT ?graph ?study ?source ?study_id ?title ?url
                ?interventionUri ?interventionLabel ?intervention
                ?country ?status ?date ?outcomeText WHERE {{
  BIND(<{aea_graph_uri}> AS ?graph)
  GRAPH <{aea_graph_uri}> {{
{axis_clauses}
{location_clause}
    OPTIONAL {{ ?study dcterms:publisher ?source . }}
    OPTIONAL {{ ?study dcterms:identifier ?study_id . }}
    OPTIONAL {{ ?study dcterms:title ?title . }}
    OPTIONAL {{ ?study dcterms:identifier ?url . }}
    OPTIONAL {{ ?study ue:studyStatus ?status . }}
    OPTIONAL {{ ?study aea:studyStatus ?status . }}
    OPTIONAL {{ ?study dcterms:date ?date . }}
    OPTIONAL {{ ?study aea:country ?country . }}
    OPTIONAL {{ ?study {_sparql_predicate(intervention_field)} ?intervention . }}
    OPTIONAL {{ ?study {_sparql_predicate(outcome_field)} ?outcomeText . }}
  }}
{intervention_label_clause}
}}
ORDER BY DESC(?date) ?study_id
LIMIT 100
"""


_WHO_ICTRP_URL_PREFIXES: dict[str, str] = {
    "ISRCTN": "https://www.isrctn.com/",
    "NCT": "https://clinicaltrials.gov/study/",
    "ACTRN": "https://www.anzctr.org.au/Trial/Registration/TrialReview.aspx?ACTRN=",
    "PACTR": "https://pactr.samrc.ac.za/TrialDisplay.aspx?TrialID=",
    "DRKS": "https://drks.de/search/de/trial/",
    "NL-OMON": "https://onderzoekmetmensen.nl/en/trial/",
    "SLCTR": "https://slctr.lk/trials/",
    "ChiCTR": "https://www.chictr.org.cn/showproj.html?proj=",
    "IRCT": "https://en.irct.ir/trial/",
    "TCTR": "https://www.thaiclinicaltrials.org/show/",
    "RBR-": "https://ensaiosclinicos.gov.br/rg/",
    "LBCTR": "https://www.lbctr.org.lb/en/search?trial_id=",
    "RPCEC": "https://rpcec.sld.cu/en/trials/",
}


_AEA_STUDY_ID_PATTERN = re.compile(r"AEARCTR-(\d+)")
_AEA_REGISTRY_HOSTS = {
    "socialscienceregistry.org",
    "www.socialscienceregistry.org",
}


def _aea_external_url(
    identifier: Optional[str], study_uri: Optional[str] = None
) -> Optional[str]:
    """Return an AEA registry page, never an internal UE evidence URI."""
    candidates = [identifier, study_uri]
    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value:
            continue
        parsed = urlparse(value)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in _AEA_REGISTRY_HOSTS
            and parsed.path.startswith("/trials/")
        ):
            return value
        match = _AEA_STUDY_ID_PATTERN.search(value)
        if match:
            trial_id = str(int(match.group(1)))
            return f"https://www.socialscienceregistry.org/trials/{trial_id}"
    return None


def _who_ictrp_external_url(study_id: str) -> Optional[str]:
    # CTRI mid1 is an internal numeric key, not the public CTRI registration ID.
    # WHO ICTRP accepts the public ID and preserves the source record identity.
    if study_id.startswith("CTRI/"):
        return "https://trialsearch.who.int/Trial2.aspx?" + urlencode({"TrialID": study_id})
    # EUCTR format: EUCTR{year}-{number}-{country_code}
    # URL: https://www.clinicaltrialsregister.eu/ctr-search/trial/{year}-{number}/{country_code}
    if study_id.startswith("EUCTR"):
        tail = study_id[5:]
        last_hyphen = tail.rfind("-")
        if last_hyphen > 0:
            return (
                f"https://www.clinicaltrialsregister.eu/ctr-search/trial/"
                f"{tail[:last_hyphen]}/{tail[last_hyphen + 1:]}"
            )
        return None
    for prefix, base in _WHO_ICTRP_URL_PREFIXES.items():
        if study_id.startswith(prefix):
            return f"{base}{study_id}"
    return None


def _build_aea_outcomes(primary_outcome, matched_concepts=None):
    if not primary_outcome or not primary_outcome.strip():
        return []
    outcome = prespecified_outcome(primary_outcome.strip())
    if matched_concepts:
        uri, lbl = matched_concepts[0]
        outcome["state_concept"] = lbl
        outcome["state_concept_uri"] = uri
    return [outcome]


def _build_matched_outcomes(outcome_text, matched_concepts=None):
    if not outcome_text or not str(outcome_text).strip():
        return []
    outcome = prespecified_outcome(str(outcome_text).strip())
    if matched_concepts:
        uri, lbl = matched_concepts[0]
        outcome["state_concept"] = lbl
        outcome["state_concept_uri"] = uri
    return [outcome]


def lookup_matched_outcome_concepts(study_uris, graph_uri):
    if not study_uris:
        return {}
    unique_uris = list(dict.fromkeys(study_uris))[:500]
    values = " ".join(sparql_iri(uri) for uri in unique_uris)
    query = (
        "PREFIX ue: <" + UE + "> "
        "PREFIX skos: <http://www.w3.org/2004/02/skos/core#> "
        "SELECT DISTINCT ?study ?concept ?label WHERE { "
        "VALUES ?study { " + values + " } "
        "GRAPH <" + graph_uri + "> { ?study ue:matchesOutcome ?concept . } "
        "GRAPH <" + TAXONOMY_GRAPH_URI + "> { "
        "?concept a ue:State ; "
        "skos:prefLabel ?label . "
        "FILTER NOT EXISTS { "
        "?study ue:matchesOutcome ?narrower . "
        "?concept skos:narrowerTransitive ?narrower . } } }"
    )
    rows = sparql_select(query)
    result = {}
    for row in rows:
        study = row.get("study")
        uri = row.get("concept", "")
        lbl = row.get("label", "")
        if study and uri and lbl:
            result.setdefault(study, []).append((uri, lbl))
    return result


def _local_rows_to_results(rows: list[dict[str, str]]) -> list[dict]:
    """Map unified SPARQL rows to the public result schema."""
    results = []
    for row in rows:
        graph = row.get("graph", "")
        source = row.get("source") or _source_label_for_graph(graph)
        study_id = row.get("study_id")
        if not study_id:
            study = row.get("study", "")
            study_id = study.rsplit("_", 1)[-1] if "_" in study else study
        url = row.get("url")
        if source == "AEA" or "aea" in graph:
            url = _aea_external_url(url or study_id, row.get("study"))
        elif not url and "who-ictrp" in graph and study_id:
            url = _who_ictrp_external_url(study_id)
        intervention_label = row.get("interventionLabel")
        intervention = intervention_label or row.get("intervention")
        results.append(format_result({
            "source": source,
            "study_id": study_id,
            "title": row.get("title"),
            "url": url,
            "intervention": intervention,
            "intervention_concept": intervention_label,
            "intervention_concept_uri": row.get("interventionUri"),
            "all_intervention_concepts": row.get("allInterventionConcepts") or [],
            "condition_concept": row.get("conditionLabel"),
            "condition_concept_uri": row.get("conditionUri"),
            "country": row.get("country"),
            "status": row.get("status"),
            "year": extract_study_year(row.get("date")),
            "outcomes": _build_matched_outcomes(row.get("outcomeText"), row.get("matchedOutcomeConcepts")),
        }))
    return results


def lookup_intervention_concepts(study_uris: list[str]) -> dict[str, tuple[str, str]]:
    """Return {study_uri: (concept_uri, concept_label)} — single concept per study (min URI).

    Kept for backward compatibility; use lookup_all_intervention_concepts for graph building.
    """
    all_map = lookup_all_intervention_concepts(
        study_uris, _source_graph_uri("aea", AEA_GRAPH_URI)
    )
    return {study: concepts[0] for study, concepts in all_map.items() if concepts}


def lookup_all_direct_intervention_concepts(
    study_uris: list[str], graph_uri: str, raw_text_predicate: str
) -> dict[str, list[tuple[str, str]]]:
    """Return {study_uri: [(concept_uri, label), ...]} via crosswalk rawText join, no ancestor expansion.

    Each concept is the direct (most specific) match from a keyword, not an ancestor.
    """
    if not study_uris:
        return {}
    unique_uris = list(dict.fromkeys(study_uris))[:500]
    values = " ".join(sparql_iri(uri) for uri in unique_uris)
    pred = sparql_iri(raw_text_predicate)
    query = f"""
PREFIX ue:   <{UE}>
PREFIX skos: <{SKOS}>
SELECT DISTINCT ?study ?concept ?label WHERE {{
  VALUES ?study {{ {values} }}
  GRAPH <{graph_uri}> {{ ?study {pred} ?kw . }}
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    ?entry ue:rawText ?kw ;
           (skos:exactMatch|skos:closeMatch) ?concept .
    ?concept a ue:Intervention ;
             skos:prefLabel ?label .
  }}
}}
"""
    rows = sparql_select(query)
    result: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        study = row.get("study")
        uri = row.get("concept", "")
        label = row.get("label", "")
        if study and uri and label:
            result.setdefault(study, []).append((uri, label))
    return result


def lookup_all_intervention_concepts(
    study_uris: list[str], graph_uri: str
) -> dict[str, list[tuple[str, str]]]:
    """Return {study_uri: [(concept_uri, label), ...]} for ALL matched intervention stamps."""
    if not study_uris:
        return {}
    unique_uris = list(dict.fromkeys(study_uris))[:500]
    values = " ".join(sparql_iri(uri) for uri in unique_uris)
    query = f"""
PREFIX ue:   <{UE}>
PREFIX skos: <{SKOS}>
SELECT ?study ?iUri ?iLabel WHERE {{
  VALUES ?study {{ {values} }}
  GRAPH <{graph_uri}> {{
    ?study ue:matchesIntervention ?iUri .
  }}
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    ?iUri skos:prefLabel ?iLabel .
  }}
}}
"""
    rows = sparql_select(query)
    result: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        study = row.get("study")
        uri = row.get("iUri", "")
        label = row.get("iLabel", "")
        if study and uri and label:
            result.setdefault(study, []).append((uri, label))
    return result


def lookup_condition_concepts(study_uris: list[str]) -> dict[str, tuple[str, str]]:
    """Return {study_uri: (condition_uri, label)} using pre-stamped matchesCondition triples."""
    if not study_uris:
        return {}
    values = " ".join(sparql_iri(uri) for uri in study_uris[:300])
    aea_graph_uri = _source_graph_uri("aea", AEA_GRAPH_URI)
    query = f"""
PREFIX ue:   <{UE}>
PREFIX skos: <{SKOS}>
SELECT ?study (MIN(?cUri) AS ?conditionUri) (SAMPLE(?cLabel) AS ?conditionLabel) WHERE {{
  VALUES ?study {{ {values} }}
  GRAPH <{aea_graph_uri}> {{
    ?study ue:matchesCondition ?cUri .
  }}
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    ?cUri skos:prefLabel ?cLabel .
  }}
}} GROUP BY ?study
"""
    rows = sparql_select(query)
    result: dict[str, tuple[str, str]] = {}
    for row in rows:
        study = row.get("study")
        uri = row.get("conditionUri", "")
        label = row.get("conditionLabel", "")
        if study and uri and label:
            result[study] = (uri, label)
    return result


@lru_cache(maxsize=1)
def _load_mesh_state_map() -> dict[str, tuple[str, str]]:
    """Load the complete MeSH→State mapping from the taxonomy graph into memory.

    Runs once and is cached for the process lifetime. Uses the mesh-diseases-states
    crosswalk already loaded into Fuseki — no per-request VALUES clause needed.
    """
    query = f"""
PREFIX skos: <{SKOS}>
PREFIX ue:   <{UE}>
SELECT ?meshUri ?stateUri ?stateLabel WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    ?stateUri a ue:State ;
             skos:prefLabel ?stateLabel ;
             (skos:exactMatch|skos:closeMatch) ?meshUri .
    FILTER(STRSTARTS(STR(?meshUri), "http://id.nlm.nih.gov/mesh/") ||
           STRSTARTS(STR(?meshUri), "https://id.nlm.nih.gov/mesh/"))
  }}
}}
"""
    result: dict[str, tuple[str, str]] = {}
    for row in sparql_select(query):
        mesh = row.get("meshUri", "")
        uri = row.get("stateUri", "")
        label = row.get("stateLabel", "")
        if mesh and uri and label and mesh not in result:
            result[mesh] = (uri, label)
    logger.info("Loaded %d MeSH→State mappings into memory", len(result))
    return result


def lookup_states_by_mesh(mesh_uris: list[str]) -> dict[str, tuple[str, str]]:
    """Return {mesh_uri: (state_uri, state_label)} via in-memory crosswalk cache."""
    if not mesh_uris:
        return {}
    mesh_map = _load_mesh_state_map()
    return {uri: mesh_map[uri] for uri in mesh_uris if uri in mesh_map}


@lru_cache(maxsize=1)
def _load_mesh_intervention_map() -> dict[str, tuple[str, str]]:
    """Load the complete MeSH→Intervention mapping from the taxonomy graph into memory."""
    query = f"""
PREFIX skos: <{SKOS}>
PREFIX ue:   <{UE}>
SELECT ?meshUri ?interventionUri ?interventionLabel WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    ?interventionUri a ue:Intervention ;
             skos:prefLabel ?interventionLabel ;
             (skos:exactMatch|skos:closeMatch) ?meshUri .
    FILTER(STRSTARTS(STR(?meshUri), "http://id.nlm.nih.gov/mesh/") ||
           STRSTARTS(STR(?meshUri), "https://id.nlm.nih.gov/mesh/"))
  }}
}}
"""
    result: dict[str, tuple[str, str]] = {}
    for row in sparql_select(query):
        mesh = row.get("meshUri", "")
        uri = row.get("interventionUri", "")
        label = row.get("interventionLabel", "")
        if mesh and uri and label and mesh not in result:
            result[mesh] = (uri, label)
    logger.info("Loaded %d MeSH\u2192Intervention mappings into memory", len(result))
    return result


def lookup_interventions_by_mesh(mesh_uris: list[str]) -> dict[str, tuple[str, str]]:
    """Return {mesh_uri: (intervention_uri, intervention_label)} via in-memory cache."""
    if not mesh_uris:
        return {}
    mesh_map = _load_mesh_intervention_map()
    return {uri: mesh_map[uri] for uri in mesh_uris if uri in mesh_map}


def _run_concurrently_ordered(*calls):
    """Run zero-argument callables concurrently and return results in input order.

    Exiting the executor waits for every submitted call, including when one call
    raises. That is important for keeping the outer local-query permit attached
    to all Fuseki work spawned by the operation.
    """
    if not calls:
        return []
    if len(calls) == 1:
        return [calls[0]()]
    with ThreadPoolExecutor(max_workers=len(calls)) as executor:
        futures = [executor.submit(call) for call in calls]
        return [future.result() for future in futures]


def _run_aea_crosswalk_query(axes: dict[str, str]) -> list[dict]:
    """Execute the AEA keyword crosswalk query when an AEA-supported axis is present."""
    query = build_aea_crosswalk_sparql_query(axes)
    if query is None:
        return []
    rows = sparql_select(query)

    # Deduplicate to one canonical row per study (multiple rows exist due to keyword repetition).
    study_canonical: dict[str, dict] = {}
    for row in rows:
        study = row.get("study", "")
        if study and study not in study_canonical:
            study_canonical[study] = row

    unique_study_uris = list(study_canonical.keys())

    aea_graph_uri = _source_graph_uri("aea", AEA_GRAPH_URI)
    enrichment_calls = []
    enrichment_names = []

    if not axes.get("intervention"):
        kw_field = _first_source_field("aea", "intervention", "keyword_crosswalk")
        enrichment_names.extend(("direct", "stamped"))
        enrichment_calls.extend(
            (
                lambda: lookup_all_direct_intervention_concepts(
                    unique_study_uris, aea_graph_uri, kw_field
                ),
                lambda: lookup_all_intervention_concepts(
                    unique_study_uris, aea_graph_uri
                ),
            )
        )

    if not axes.get("condition"):
        enrichment_names.append("condition")
        enrichment_calls.append(lambda: lookup_condition_concepts(unique_study_uris))

    enrichment_names.append("outcome")
    enrichment_calls.append(
        lambda: lookup_matched_outcome_concepts(unique_study_uris, aea_graph_uri)
    )
    enrichment = dict(
        zip(enrichment_names, _run_concurrently_ordered(*enrichment_calls))
    )

    if not axes.get("intervention"):
        # Direct concepts: one row per (study, directly-matched concept).
        direct_all = enrichment["direct"]
        # All stamped concepts (direct + ancestors): stored for graph view hierarchy counting.
        all_stamped = enrichment["stamped"]

        # Expand: one canonical row per (study, direct concept) for list grouping.
        expanded_rows: list[dict] = []
        for study_uri, base_row in study_canonical.items():
            concepts = direct_all.get(study_uri, [])
            all_c = all_stamped.get(study_uri, [])
            if concepts:
                for uri, label in concepts:
                    r = dict(base_row)
                    r["interventionUri"] = uri
                    r["interventionLabel"] = label
                    r["allInterventionConcepts"] = all_c
                    expanded_rows.append(r)
            else:
                base_row["allInterventionConcepts"] = all_c
                expanded_rows.append(base_row)

    else:
        expanded_rows = list(study_canonical.values())
        for r in expanded_rows:
            r["allInterventionConcepts"] = []

    if not axes.get("condition"):
        condition_map = enrichment["condition"]
        for row in expanded_rows:
            study = row.get("study")
            if study and study in condition_map:
                uri, label = condition_map[study]
                row["conditionUri"] = uri
                row["conditionLabel"] = label

    outcome_map = enrichment["outcome"]
    for row in expanded_rows:
        study = row.get("study")
        if study and study in outcome_map:
            row["matchedOutcomeConcepts"] = outcome_map[study]

    return _local_rows_to_results(expanded_rows)


def _run_who_ictrp_crosswalk_query(axes: dict[str, str]) -> list[dict]:
    query = build_who_ictrp_crosswalk_query(axes)
    if not query:
        return []
    rows = sparql_select(query)

    # Deduplicate to one canonical row per study.
    study_canonical: dict[str, dict] = {}
    for row in rows:
        study = row.get("study", "")
        if study and study not in study_canonical:
            study_canonical[study] = row

    unique_uris = list(study_canonical.keys())
    enrichment_calls = []
    enrichment_names = []

    if not axes.get("intervention"):
        enrichment_names.extend(("direct", "stamped"))
        enrichment_calls.extend(
            (
                lambda: lookup_all_direct_intervention_concepts(
                    unique_uris, WHO_ICTRP_GRAPH_URI, f"{ICTRP}intervention"
                ),
                lambda: lookup_all_intervention_concepts(
                    unique_uris, WHO_ICTRP_GRAPH_URI
                ),
            )
        )

    enrichment_names.append("outcome")
    enrichment_calls.append(
        lambda: lookup_matched_outcome_concepts(unique_uris, WHO_ICTRP_GRAPH_URI)
    )
    enrichment = dict(
        zip(enrichment_names, _run_concurrently_ordered(*enrichment_calls))
    )

    if not axes.get("intervention"):
        direct_all = enrichment["direct"]
        all_stamped = enrichment["stamped"]
        expanded_rows: list[dict] = []
        for study_uri, base_row in study_canonical.items():
            concepts = direct_all.get(study_uri, [])
            all_c = all_stamped.get(study_uri, [])
            if concepts:
                for uri, label in concepts:
                    r = dict(base_row)
                    r["interventionUri"] = uri
                    r["interventionLabel"] = label
                    r["allInterventionConcepts"] = all_c
                    expanded_rows.append(r)
            else:
                base_row["allInterventionConcepts"] = all_c
                expanded_rows.append(base_row)
    else:
        for r in study_canonical.values():
            r["allInterventionConcepts"] = []
        expanded_rows = list(study_canonical.values())

    outcome_map = enrichment["outcome"]
    for row in expanded_rows:
        study = row.get("study")
        if study and study in outcome_map:
            row["matchedOutcomeConcepts"] = outcome_map[study]
    return _local_rows_to_results(expanded_rows)


def _run_unified_local_query(axes: dict[str, str]) -> list[dict]:
    """Execute local named-graph queries and map rows to the result schema."""
    state = axes.get("state")
    if state:
        # A single property-path query with LIMIT 100 globally ranks/truncates
        # the condition-or-outcome union. That is not equivalent to the old
        # client behavior, which gave each axis its own top-100 window. Keep
        # both original local searches inside this one permit-bearing chain.
        shared_axes = {key: value for key, value in axes.items() if key != "state"}
        condition_results = _run_unified_local_query(
            {**shared_axes, "condition": state}
        )
        outcome_results = _run_unified_local_query(
            {**shared_axes, "outcome": state}
        )
        return condition_results + outcome_results

    aea_results, who_results = _run_concurrently_ordered(
        lambda: _run_aea_crosswalk_query(axes),
        lambda: _run_who_ictrp_crosswalk_query(axes),
    )
    return aea_results + who_results


async def _maybe_run_ctgov(axes: dict[str, str]) -> list[dict]:
    """Run CT.gov for any combination of condition, intervention, outcome, and region.

    Condition uses MeSH filter.advanced when available, falls back to query.cond.
    Intervention uses query.intr and outcome uses query.outc via taxonomy prefLabel.
    Skips if no substantive axis (condition/intervention/outcome) is present.
    """
    condition = axes.get("condition")
    intervention = axes.get("intervention")
    outcome = axes.get("outcome")
    state = axes.get("state")
    region = axes.get("region", "")

    if not any([condition, intervention, outcome, state]) and not region:
        _mark_source_status("ctgov", "not_applicable")
        logger.info("Skipping CT.gov: no condition, intervention, or outcome axis")
        return []

    params: dict[str, str] = {"pageSize": "100"}
    region_param = _first_api_param("ctgov", "region", "live_api_param") or "query.locn"
    iso = ""
    region_countries: list[dict[str, str]] = []

    if region:
        try:
            region_countries = resolve_region_countries(region)
            iso = region_countries[0]["iso"]
        except ValueError:
            logger.warning("CT.gov region filter skipped: %s", region)

    condition_filters: list[str] = []
    if condition:
        mesh_uris = extract_mesh_exact_matches(condition)
        condition_filters = ctgov_condition_mesh_filter(mesh_uris) if mesh_uris else []
        if not condition_filters:
            logger.info("Skipping CT.gov condition axis: no MeSH D-number for %s", condition)

    intr_label: Optional[str] = None
    intr_terms: list[str] = []
    if intervention:
        intr_terms = get_concept_search_terms(intervention)
        if intr_terms:
            intr_label = intr_terms[0]
            params["query.intr"] = " OR ".join(f'"{t}"' for t in intr_terms)
        else:
            logger.info("CT.gov: no label for intervention %s, skipping axis", intervention)

    if outcome:
        outc_terms = get_concept_search_terms(outcome)
        if outc_terms:
            params["query.outc"] = " OR ".join(f'"{t}"' for t in outc_terms)
        else:
            logger.info("CT.gov: no label for outcome %s, skipping axis", outcome)

    if state:
        # A combined query.term OR is not equivalent to the old two requests:
        # CT.gov ranks and truncates the combined result set globally, so records
        # from either independently ranked top-100 set can disappear. Preserve
        # both original parameter sets, but execute them under one live-API slot.
        outcome_params = dict(params)
        mesh_uris = extract_mesh_exact_matches(state)
        state_condition_filters = ctgov_condition_mesh_filter(mesh_uris) if mesh_uris else []
        state_terms = get_concept_search_terms(state)
        if state_terms:
            outcome_params["query.outc"] = " OR ".join(
                f'"{term}"' for term in state_terms
            )
        searches = []
        for condition_filter in state_condition_filters:
            condition_params = dict(params)
            condition_params["filter.advanced"] = condition_filter
            searches.append((condition_params, state))
        if state_terms:
            searches.append((outcome_params, None))
        if not searches:
            _mark_source_status("ctgov", "not_applicable")
            logger.info("CT.gov: no resolvable state axis for %s", state)
            return []
        async def run_chunk(search_params, stamped_condition):
            if not await _acquire_live_api_slot("CT.gov"):
                return []
            try:
                return await _run_ctgov_live(
                    search_params, iso, intr_label, intervention, stamped_condition, intr_terms
                )
            finally:
                _LIVE_API_SEMAPHORE.release()

        batches = await asyncio.gather(
            *(run_chunk(search_params, stamped_condition) for search_params, stamped_condition in searches)
        )
        results = [study for batch in batches for study in batch]
        return dedupe_and_sort_results(results)

    if not condition_filters and not any(k in params for k in ("query.intr", "query.outc")) and not region:
        _mark_source_status("ctgov", "not_applicable")
        logger.info("Skipping CT.gov: no resolvable axis")
        return []

    location_names = " OR ".join(
        country["name"] for country in (region_countries or [{"name": iso_to_country_name(iso)}])
    )
    country_params = [{**params, region_param: location_names}]
    search_params_list = (
        [{**base, "filter.advanced": cf} for base in country_params for cf in condition_filters]
        if condition_filters
        else country_params
    )

    async def run_chunk(search_params):
        if not await _acquire_live_api_slot("CT.gov"):
            return []
        try:
            return await _run_ctgov_live(search_params, iso, intr_label, intervention, condition, intr_terms)
        finally:
            _LIVE_API_SEMAPHORE.release()

    batches = await asyncio.gather(*(run_chunk(search_params) for search_params in search_params_list))
    results = [study for batch in batches for study in batch]
    return dedupe_and_sort_results(results) if len(search_params_list) > 1 else results


def _ctgov_study_matches_intervention_terms(
    intr_terms: list[str], intervention_names: list[str]
) -> bool:
    """True when a CT.gov study's own intervention names actually contain a search term.

    CT.gov's query.intr does word/stem-level matching, not phrase matching, so a
    short altLabel like "Wave energy" can match completely unrelated studies that
    merely share individual words (e.g. "shock wave therapy" studies with no
    energy content at all). Re-verify against the study's real intervention text
    before trusting the tag CT.gov's search relevance implied. Studies with no
    named interventions (nothing to verify against) are passed through unchanged.

    Matching is on whole words, not substrings: a naive substring check lets a
    short acronym like "AFT" match unrelated words that merely contain those
    letters (e.g. "grafting"), which is exactly the false-positive class this
    function exists to catch -- a substring check would let it right back in.
    """
    if not intervention_names:
        return True
    haystack = " | ".join(intervention_names).lower()
    return any(
        _re.search(r"\b" + _re.escape(term.lower()) + r"\b", haystack)
        for term in intr_terms
    )


async def _run_ctgov_live(
    params: dict[str, str],
    iso: str,
    intr_label: Optional[str],
    intervention: Optional[str],
    condition: Optional[str],
    intr_terms: Optional[list[str]] = None,
) -> list[dict]:
    """Fetch, parse, and map one bounded CT.gov request."""

    try:
        raw: list[dict] = []
        page_params = dict(params)
        max_pages = 1
        for _ in range(max_pages):
            resp = await asyncio.to_thread(
                requests.get,
                _source_endpoint("ctgov", CTGOV_API_BASE),
                params=page_params,
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json()
            raw.extend(payload.get("studies", []))
            next_token = payload.get("nextPageToken")
            if not next_token:
                break
            page_params = dict(params)
            page_params["pageToken"] = next_token
    except requests.RequestException as exc:
        _mark_source_status("ctgov", "error")
        logger.warning("CT.gov request failed: %s", exc)
        return []

    seen: dict[str, dict] = {}
    for study in raw:
        mapped = map_ctgov_study(study, iso)
        if mapped is not None:
            seen[mapped["nctId"]] = mapped

    expanded: list[dict] = []
    for mapped in seen.values():
        intervention_names = mapped.pop("_intervention_names", [])
        if intervention:
            if intr_terms and not _ctgov_study_matches_intervention_terms(intr_terms, intervention_names):
                continue
            row = dict(mapped)
            row["intervention_concept"] = intr_label
            row["intervention_concept_uri"] = intervention
            expanded.append(row)
        elif intervention_names:
            allow_mesh_fallback = len(intervention_names) == 1
            for name in intervention_names:
                row = dict(mapped)
                row["intervention"] = name
                row["_allow_intervention_mesh_fallback"] = allow_mesh_fallback
                expanded.append(row)
        else:
            expanded.append(dict(mapped))

    results = [
        {("study_id" if k == "nctId" else k): v for k, v in row.items()}
        for row in expanded
    ]

    # Preserve explicit intervention-axis classification, then classify otherwise
    # untagged results via crosswalk -> MeSH -> taxonomy-label fallback.
    untagged = [r for r in results if not r.get("intervention_concept_uri")]
    if untagged:
        try:
            intr_crosswalk = _load_ctgov_intervention_concept_map()
        except Exception:
            logger.warning(
                "CT.gov intervention crosswalk load failed; using existing fallbacks",
                exc_info=True,
            )
            intr_crosswalk = {}
        for r in untagged:
            _attach_ctgov_intervention_concept(r, intr_crosswalk)

        untagged = [r for r in untagged if not r.get("intervention_concept_uri")]
        mesh_eligible = [
            r for r in untagged if r.get("_allow_intervention_mesh_fallback")
        ]
        all_intr_mesh = list(
            {m for r in mesh_eligible for m in r.get("intervention_mesh_uris", [])}
        )
        mesh_to_intervention = (
            lookup_interventions_by_mesh(all_intr_mesh) if all_intr_mesh else {}
        )
        for r in mesh_eligible:
            for m in r.get("intervention_mesh_uris", []):
                if m in mesh_to_intervention:
                    r["intervention_concept_uri"], r["intervention_concept"] = mesh_to_intervention[m]
                    break
        still_untagged = [r for r in untagged if not r.get("intervention_concept_uri") and r.get("intervention")]
        if still_untagged:
            texts = list({r["intervention"] for r in still_untagged})[:100]
            concept_map = lookup_intervention_concepts_by_label(texts)
            for r in still_untagged:
                match = concept_map.get(r["intervention"])
                if match:
                    r["intervention_concept_uri"], r["intervention_concept"] = match

    for r in results:
        r.pop("_allow_intervention_mesh_fallback", None)

    # Resolve condition concepts from MeSH URIs via in-memory crosswalk cache (O(1) per study).
    # For condition-axis searches, every result already matches the search concept — assign directly.
    if condition:
        cond_labels = get_concept_search_terms(condition)
        cond_label = cond_labels[0] if cond_labels else None
        for r in results:
            r.pop("condition_mesh_uris", None)
            if not r.get("condition_concept_uri") and cond_label:
                r["condition_concept_uri"] = condition
                r["condition_concept"] = cond_label
    else:
        all_mesh = list({m for r in results for m in r.get("condition_mesh_uris", [])})
        mesh_to_state = lookup_states_by_mesh(all_mesh)
        for r in results:
            mesh_uris = r.pop("condition_mesh_uris", [])
            if not r.get("condition_concept_uri"):
                for m in mesh_uris:
                    if m in mesh_to_state:
                        r["condition_concept_uri"], r["condition_concept"] = mesh_to_state[m]
                        break

    return results


async def _maybe_run_isrctn(axes: dict[str, str]) -> list[dict]:
    """Run ISRCTN for condition and/or intervention axes.

    Condition resolves to a conditionCategory value via ISRCTN exactMatch/closeMatch.
    Intervention resolves to a q= free-text OR query from prefLabel + altLabels + narrower.
    When both are present, conditionCategory is omitted because ISRCTN ANDs it very
    strictly with q=, collapsing recall to near-zero. The q= term alone gives adequate
    precision for combined searches.
    """
    condition = axes.get("condition") or axes.get("state")
    intervention = axes.get("intervention")
    region = axes.get("region", "")

    if not condition and not intervention and not region:
        _mark_source_status("isrctn", "not_applicable")
        logger.info("Skipping ISRCTN: no condition or intervention axis")
        return []

    api_params: dict = {}
    region_countries: list[dict[str, str]] = []
    if region:
        try:
            region_countries = resolve_region_countries(region)
        except ValueError:
            logger.warning("ISRCTN region filter skipped: %s", region)

    # Build q= from condition and/or intervention terms combined with AND.
    # ISRCTN returns condition strings in <conditions><condition><description> which
    # are matched against the conditions crosswalk for tagging.
    # Intervention tagging: stamp from search + crosswalk fallback on drugNames.
    cond_label: Optional[str] = None
    intr_terms: list[str] = []

    if condition:
        cond_label = get_concept_label(condition)

    if intervention:
        intr_terms = get_concept_search_terms(intervention)

    # Build MarkLogic q= string(s) combining condition field constraint and
    # free-text intervention OR terms. condition: "label" restricts to the
    # condition field specifically, avoiding false positives from bare q=
    # search. Intervention terms are chunked (see isrctn_intervention_query_chunks)
    # because ISRCTN 414s once enough terms are OR'd into one q= value; each
    # chunk becomes its own request, queried concurrently and merged below.
    cond_part: Optional[str] = None
    if cond_label:
        quoted_cond = f'"{cond_label}"' if " " in cond_label else cond_label
        cond_part = f"condition: {quoted_cond}"

    intr_chunks = isrctn_intervention_query_chunks(intr_terms) if intr_terms else []
    if intr_chunks:
        q_variants = [
            " AND ".join(part for part in (cond_part, chunk) if part)
            for chunk in intr_chunks
        ]
    elif cond_part:
        q_variants = [cond_part]
    else:
        q_variants = []

    if not q_variants:
        if region:
            q_variants = [""]
        else:
            _mark_source_status("isrctn", "not_applicable")
            logger.info("Skipping ISRCTN: no resolvable axis")
            return []

    recruitment_countries = " OR ".join(
        country["name"] for country in (region_countries or [{"name": ""}])
    )
    request_params = [
        {"q": q, "limit": api_params.get("limit", 100), "recruitmentCountry": recruitment_countries}
        for q in q_variants
    ]
    async def run_country(params):
        if not await _acquire_live_api_slot("ISRCTN"):
            return []
        try:
            return await _run_isrctn_live(params, intr_terms, intervention, condition, cond_label)
        finally:
            _LIVE_API_SEMAPHORE.release()
    batches = await asyncio.gather(*(run_country(params) for params in request_params))
    return dedupe_and_sort_results([study for batch in batches for study in batch])


async def _run_isrctn_live(
    api_params: dict,
    intr_terms: list[str],
    intervention: Optional[str],
    condition: Optional[str],
    cond_label: Optional[str],
) -> list[dict]:
    """Fetch, parse, and map ISRCTN results while the caller holds a slot."""

    try:
        import fetch_isrctn
        results = await fetch_isrctn.fetch_isrctn(api_params)
    except Exception as exc:
        _mark_source_status("isrctn", "error")
        logger.warning("ISRCTN request failed: %s", exc)
        return []

    # Stamp intervention from search term
    if intr_terms and intervention:
        intr_label = get_concept_label(intervention)
        for r in results:
            r["intervention_concept"] = intr_label
            r["intervention_concept_uri"] = intervention

    # Crosswalk fallback: check drug_names_list then intervention_descriptions
    intr_xwalk = get_isrctn_interventions_xwalk()
    for r in results:
        if not r.get("intervention_concept_uri"):
            candidates = (r.get("drug_names_list") or []) + (r.get("intervention_descriptions") or [])
            for raw in candidates:
                concept_uri = intr_xwalk.get(_normalize_xwalk_key(raw))
                if concept_uri:
                    r["intervention_concept_uri"] = concept_uri
                    r["intervention_concept"] = get_concept_label(concept_uri)
                    break

    # Stamp condition via crosswalk on condition_descriptions returned by the API.
    cond_xwalk = get_isrctn_conditions_xwalk()
    for r in results:
        if not r.get("condition_concept_uri"):
            for desc in (r.get("condition_descriptions") or []):
                concept_uri = cond_xwalk.get(_normalize_xwalk_key(desc))
                if concept_uri:
                    r["condition_concept_uri"] = concept_uri
                    r["condition_concept"] = get_concept_label(concept_uri)
                    break
        # Fallback: stamp from search axis when crosswalk has no entry for this
        # condition string. Crosswalk coverage is incomplete; the search term is
        # a reasonable approximation until the crosswalk is expanded.
        if not r.get("condition_concept_uri") and cond_label and condition:
            r["condition_concept"] = cond_label
            r["condition_concept_uri"] = condition

    # Stamp outcome measures via states crosswalk.
    for r in results:
        for outcome in r.get("outcomes") or []:
            if outcome.get("state_concept_uri"):
                continue
            measure = (outcome.get("measure") or "").strip()
            if not measure:
                continue
            concept_uri = cond_xwalk.get(_normalize_xwalk_key(measure))
            if concept_uri and concept_uri in _load_state_concept_uris():
                outcome["state_concept_uri"] = concept_uri
                outcome["state_concept"] = get_concept_label(concept_uri)

    # Stamp region via crosswalk. Try admin1 first (location + country), then country.
    region_xwalk = get_isrctn_regions_xwalk()
    for r in results:
        if r.get("location_concept_uri"):
            continue
        countries_raw = r.get("countries") or []
        locations_raw = r.get("study_locations") or []
        # Admin1: try "{location}, {country}" keys against the crosswalk.
        if len(countries_raw) == 1:
            for loc in locations_raw:
                key = _normalize_xwalk_key(f"{loc}, {countries_raw[0]}")
                concept_uri = region_xwalk.get(key)
                if concept_uri:
                    r["location_concept_uri"] = concept_uri
                    r["location_concept"] = get_concept_label(concept_uri)
                    break
        # Country fallback.
        if not r.get("location_concept_uri"):
            for raw in countries_raw:
                concept_uri = region_xwalk.get(_normalize_xwalk_key(raw))
                if concept_uri:
                    r["location_concept_uri"] = concept_uri
                    r["location_concept"] = get_concept_label(concept_uri)
                    break

    for r in results:
        r.pop("intervention_mesh_uris", None)

    return results


async def async_query_axes(axes: dict[str, str]) -> list[dict]:
    """Return studies for any combination of query axes.

    Local graphs (AEA, WHO ICTRP): pure URI joins via unified SPARQL.
    Live API sources (CT.gov, ISRCTN): taxonomy-driven parameter resolution.
    If no structured mapping exists for a provided axis, those sources skip.
    """
    if not axes:
        raise ValueError("At least one query axis is required")

    normalized = dict(axes)

    # Normalize legacy country → canonical GeoNames region URI.
    if "country" in normalized and "region" not in normalized:
        normalized["region"] = normalize_country_to_region_uri(
            normalized.pop("country"),
            context="input",
        )
    elif "country" in normalized:
        normalized.pop("country")

    # Resolve text condition label → URI (legacy CLI path)
    if "condition" in normalized:
        cond = normalized["condition"]
        if not (cond.startswith("http://") or cond.startswith("https://")):
            normalized["condition"] = resolve_condition_node(cond)

    sparql_axes = {k: v for k, v in normalized.items() if k in ("condition", "intervention", "outcome", "state", "region")}

    local_task  = _run_local_query_limited(sparql_axes)
    ctgov_task  = _maybe_run_ctgov(normalized)
    isrctn_task = _maybe_run_isrctn(normalized)

    local_results, ctgov_results, isrctn_results = await asyncio.gather(
        local_task, ctgov_task, isrctn_task
    )
    return dedupe_and_sort_results(local_results + ctgov_results + isrctn_results)


async def async_query_axes_with_metadata(
    axes: dict[str, str],
    query_fn=None,
) -> QueryExecution:
    """Run an axes query and retain internal source completion metadata.

    ``query_fn`` exists for route-level tests and wrappers; production callers
    omit it and execute :func:`async_query_axes`. The public result payload is
    unchanged because only the cache layer consumes this companion API.
    """
    tracker = {
        "local": "pending",
        "ctgov": "pending",
        "isrctn": "pending",
    }
    token = _SOURCE_COMPLETION.set(tracker)
    try:
        results = await (query_fn or async_query_axes)(axes)
        for source, status in tuple(tracker.items()):
            if status == "pending":
                tracker[source] = "success"
        return QueryExecution(results=results, source_status=dict(tracker))
    finally:
        _SOURCE_COMPLETION.reset(token)


async def _run_local_query_limited(axes: dict[str, str]) -> list[dict]:
    """Run one complete local search chain under the shared process-local cap."""
    try:
        await asyncio.wait_for(
            _LOCAL_QUERY_SEMAPHORE.acquire(),
            timeout=LOCAL_QUERY_SEMAPHORE_TIMEOUT,
        )
    except asyncio.TimeoutError as exc:
        raise LocalQueryBusyError("Search is busy, try again shortly") from exc

    work = asyncio.create_task(asyncio.to_thread(_run_unified_local_query, axes))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        # Cancelling a to_thread await cannot stop its synchronous socket work.
        # Keep ownership of the permit until that work actually terminates.
        with suppress(Exception):
            await asyncio.shield(work)
        raise
    finally:
        _LOCAL_QUERY_SEMAPHORE.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Query evidence by any combination of axes."
    )
    parser.add_argument("--condition", help="Condition URI or label.")
    parser.add_argument("--intervention", help="Intervention URI.")
    parser.add_argument("--outcome", help="Outcome URI.")
    parser.add_argument("--region", help="Region URI (e.g. https://sws.geonames.org/192950/).")
    parser.add_argument("--country", help="Country name or ISO alpha-2 (legacy; implies region).")
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args()

    axes: dict[str, str] = {}
    if args.condition:
        axes["condition"] = args.condition
    if args.intervention:
        axes["intervention"] = args.intervention
    if args.outcome:
        axes["outcome"] = args.outcome
    if args.region:
        axes["region"] = args.region
    if args.country:
        axes["country"] = args.country

    if not axes:
        print("Error: provide at least one axis (--condition, --intervention, --outcome, --region, --country)", file=__import__("sys").stderr)
        return 1

    try:
        results = asyncio.run(async_query_axes(axes))
    except (ValueError, requests.RequestException) as exc:
        logger.error("%s", exc)
        return 1

    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
