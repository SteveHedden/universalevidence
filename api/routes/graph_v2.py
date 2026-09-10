"""Hierarchy-aware, lazy graph projection used by the D3 graph lab.

The route is additive and deliberately does not participate in the legacy
``/graph`` or ordinary Search execution paths. Stored sources are selected and
hydrated in Fuseki, then attributed and grouped in process; live sources reuse
the bounded query-v2 adapters.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections import defaultdict
from functools import lru_cache
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, Literal, Mapping, Sequence

from fastapi import APIRouter, HTTPException, Query, Request
import httpx
from rdflib import Graph, URIRef
from rdflib.namespace import RDFS, SKOS


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import query_interventions as v1  # noqa: E402
from query_interventions import (  # noqa: E402
    ClientDisconnectedError,
    await_query_or_disconnect,
)
from query_v2 import (  # noqa: E402
    CanonicalQuery,
    RegionIndex,
    SOURCE_IDS,
    SourceBudget,
    SourceBudgetExhausted,
    SourceUnavailable,
    UNIQUE_STUDY_LIMIT,
    _bounded_slot,
    _mapped_source_intervention_uris,
    _mapped_source_state_uris,
    _sparql_select,
    _source_direct_intervention_crosswalk,
    _source_direct_state_crosswalk,
    default_adapters,
    execute_graph_source,
    plan_query,
    region_index_for_query,
    study_key,
)

from api.graph_v2_cache import GraphV2Cache, graph_v2_cache  # noqa: E402
from api.routes.graph import confidence_label  # noqa: E402
from api.routes.query_v2 import build_query_v2_request  # noqa: E402


logger = logging.getLogger(__name__)
router = APIRouter()

SCHEMA_VERSION = "graph-v2"
MAX_STATE_NODES = 100
# One-window source compatibility for callers importing the old constant.
MAX_CONDITION_NODES = MAX_STATE_NODES
MAX_INTERVENTION_NODES = 100
MAX_TOTAL_NODES = 250
MAX_EVIDENCE_EDGES = 500
MAX_GROUPED_ROWS = 2_000
GRAPH_DEADLINE_SECONDS = 6.0
DETAIL_DEFAULT_LIMIT = 25
DETAIL_MAX_LIMIT = 50
DETAIL_MAX_OFFSET = 1_000
GROUP_SEPARATOR = "|||"
_STORED_SELECTION_ROW = "selection"
_STORED_PAIR_ROW = "pair"
_STORED_DETAIL_ROW = "detail"
_STORED_STATE_RAW_ROW = "state"
_STORED_INTERVENTION_RAW_ROW = "intervention"
_DETAIL_SUPPORT_ID = "_graph_support_id"
_EDGE_MEMBERSHIP_DIGESTS = "_source_membership_digests"
DEFAULT_VERSION_MANIFEST = REPO_ROOT / "data" / "manifests" / "graph-versions.json"

SOURCE_LABELS = {
    "aea": "AEA",
    "ctgov": "CT.gov",
    "isrctn": "ISRCTN",
    "who-ictrp": "WHO ICTRP",
}
SOURCE_DESIGN_WEIGHTS = {
    "aea": 2.0,
    "ctgov": 1.0,
    "isrctn": 1.0,
    "who-ictrp": 1.0,
}

graph_v2_detail_cache = GraphV2Cache(
    namespace="graph-v2-edge-details-1",
    max_entries=500,
    populated_ttl=900.0,
    empty_ttl=120.0,
    partial_ttl=30.0,
)


def _label(graph: Graph, node: URIRef) -> str:
    value = graph.value(node, SKOS.prefLabel) or graph.value(node, RDFS.label)
    return str(value or str(node).rstrip("/").rsplit("/", 1)[-1])


@lru_cache(maxsize=1)
def _state_graph() -> Graph:
    graph = Graph()
    graph.parse(REPO_ROOT / "vocabularies" / "states.ttl", format="turtle")
    return graph


@lru_cache(maxsize=1)
def _intervention_graph() -> Graph:
    graph = Graph()
    graph.parse(REPO_ROOT / "vocabularies" / "interventions.ttl", format="turtle")
    return graph


def _valid_version(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if len(text) == 64 and all(character in "0123456789abcdef" for character in text):
        return text
    return None


def graph_versions() -> tuple[str | None, str | None, str | None]:
    """Return loader-owned cache versions and a bypass reason, if any."""
    taxonomy_version = _valid_version(os.getenv("UE_TAXONOMY_VERSION"))
    dataset_version = _valid_version(os.getenv("UE_DATASET_VERSION"))
    manifest_path = Path(
        os.getenv("UE_GRAPH_VERSION_MANIFEST", str(DEFAULT_VERSION_MANIFEST))
    )
    if taxonomy_version is None or dataset_version is None:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        if manifest.get("schemaVersion") == "ue-loader-versions-v1":
            taxonomy_version = taxonomy_version or _valid_version(
                manifest.get("taxonomyVersion")
            )
            dataset_version = dataset_version or _valid_version(
                manifest.get("datasetVersion")
            )
    missing = []
    if taxonomy_version is None:
        missing.append("taxonomy_version_unavailable")
    if dataset_version is None:
        missing.append("dataset_version_unavailable")
    return taxonomy_version, dataset_version, "+".join(missing) or None


def _taxonomy_forest(
    root_uris: Sequence[str],
    *,
    graph: Graph,
    class_name: str,
    limit: int,
) -> dict[str, Any]:
    """Return a deterministic multi-root breadth-first taxonomy forest."""
    roots = sorted(
        {URIRef(uri) for uri in root_uris},
        key=lambda node: (_label(graph, node).casefold(), str(node)),
    )
    for root in roots:
        if not any(graph.triples((root, None, None))):
            raise ValueError(f"{class_name.casefold()} is not present in the tracked taxonomy")

    queue: list[tuple[URIRef, int]] = [(root, 0) for root in roots]
    depths: dict[URIRef, int] = {}
    while queue:
        node, depth = queue.pop(0)
        if node in depths and depths[node] <= depth:
            continue
        depths[node] = depth
        children = sorted(
            {URIRef(child) for child in graph.subjects(SKOS.broader, node)},
            key=lambda child: (_label(graph, child).casefold(), str(child)),
        )
        queue.extend((child, depth + 1) for child in children)

    ordered = sorted(
        depths.items(),
        key=lambda item: (item[1], _label(graph, item[0]).casefold(), str(item[0])),
    )

    retained = ordered[:limit]
    retained_ids = {str(node) for node, _depth in retained}
    nodes = [
        {
            "id": str(node),
            "label": _label(graph, node),
            "class": class_name,
            "studyCount": 0,
            "depth": depth,
            "selected": node in roots,
        }
        for node, depth in retained
    ]
    hierarchy_pairs: list[tuple[str, str]] = []
    for node, _depth in retained:
        if node in roots:
            continue
        parents = sorted(
            {
                str(parent)
                for parent in graph.objects(node, SKOS.broader)
                if str(parent) in retained_ids
            }
        )
        if not parents:
            # Breadth-first retention normally guarantees a parent. Keep the
            # graph connected if malformed multi-parent source data does not.
            parents = [str(roots[0])] if roots else []
        hierarchy_pairs.extend((parent, str(node)) for parent in parents)
    return {
        "nodes": nodes,
        "pairs": hierarchy_pairs,
        "total": len(ordered),
        "omitted": max(0, len(ordered) - len(retained)),
        "roots": [str(root) for root in roots],
    }


def state_forest(
    root_uris: Sequence[str], limit: int = MAX_STATE_NODES
) -> dict[str, Any]:
    return _taxonomy_forest(
        root_uris,
        graph=_state_graph(),
        class_name="State",
        limit=limit,
    )


def condition_forest(
    root_uris: Sequence[str], limit: int = MAX_STATE_NODES
) -> dict[str, Any]:
    """Deprecated helper alias; Graph wire nodes are always ``State``."""
    return state_forest(root_uris, limit)


def intervention_forest(
    root_uris: Sequence[str], limit: int = MAX_INTERVENTION_NODES
) -> dict[str, Any]:
    return _taxonomy_forest(
        root_uris,
        graph=_intervention_graph(),
        class_name="Intervention",
        limit=limit,
    )


def condition_subtree(root_uri: str, limit: int = MAX_STATE_NODES) -> dict[str, Any]:
    """Backward-compatible single-root condition subtree helper."""
    return condition_forest((root_uri,), limit)


def stable_hierarchy_edge_id(parent: str, child: str) -> str:
    digest = hashlib.sha256(
        f"{SCHEMA_VERSION}|hierarchy|{parent}|{child}".encode()
    ).hexdigest()
    return f"gh-{digest[:32]}"


def stable_evidence_edge_id(state: str, intervention: str) -> str:
    digest = hashlib.sha256(
        f"{SCHEMA_VERSION}|evidence|{state}|{intervention}".encode()
    ).hexdigest()
    return f"ge-{digest[:32]}"


def _study_membership_digest(
    source_id: str,
    study_ids: Sequence[str],
) -> str:
    """Hash one source's canonical support identities deterministically."""
    membership = {
        str(study_id).strip()
        for study_id in study_ids
        if str(study_id).strip()
    }
    encoded = json.dumps(
        {
            "v": 1,
            "source": source_id,
            "studies": sorted(membership),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _graph_state_roots(query: CanonicalQuery) -> tuple[str, ...]:
    """Canonical State roots, retaining strict role-filter roots for display."""
    return tuple(
        sorted(
            set(query.values["state"])
            .union(query.values["condition"])
            .union(query.values["outcome"])
        )
    )


def _source_graph(source_id: str) -> str:
    if source_id == "aea":
        return v1._source_graph_uri("aea", v1.AEA_GRAPH_URI)
    if source_id == "who-ictrp":
        return v1.WHO_ICTRP_GRAPH_URI
    raise ValueError(source_id)


def _study_axis_filter(
    roots: Sequence[str], *, predicate: str, logic: str, variable: str
) -> str:
    if not roots:
        return ""
    if logic == "and":
        return "\n".join(
            f"    ?study {predicate} {v1.sparql_iri(uri)} ." for uri in roots
        )
    values = " ".join(v1.sparql_iri(uri) for uri in roots)
    return f"    VALUES ?{variable} {{ {values} }}\n    ?study {predicate} ?{variable} ."


def _candidate_values(variable: str, uris: Sequence[str]) -> str:
    if not uris:
        return ""
    values = " ".join(v1.sparql_iri(uri) for uri in uris)
    return f"  VALUES ?{variable} {{ {values} }}"


def _most_specific_filter(
    *,
    graph_uri: str,
    predicate: str,
    concept: str,
    narrower_variable: str,
    retained_uris: Sequence[str],
) -> str:
    """Filter descendants using exactly the aggregate's retained population.

    With selected roots, breadth-first taxonomy expansion may be capped. Both
    aggregate and detail queries must ignore a narrower stamped concept only
    when that narrower concept is in that same retained capped population.
    Empty populations intentionally retain the legacy global-most-specific
    behavior used for an unselected/dynamic axis.
    """
    retained_values = _candidate_values(narrower_variable, retained_uris)
    return f"""  FILTER NOT EXISTS {{
{retained_values}
    GRAPH <{graph_uri}> {{ ?study {predicate} ?{narrower_variable} . }}
    GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{ ?{narrower_variable} skos:broader+ {concept} . }}
  }}"""


_DIRECT_CROSSWALKS = {
    "aea": {
        "condition": (
            f"{v1.AEA}keyword",
            "https://universalevidence.com/crosswalk/aea-conditions/",
        ),
        "outcome": (
            f"{v1.AEA}primaryOutcome",
            "https://universalevidence.com/crosswalk/aea-outcomes/",
        ),
        "intervention": (
            f"{v1.AEA}intervention",
            "https://universalevidence.com/crosswalk/aea-interventions-freetext/",
        ),
    },
    "who-ictrp": {
        "condition": (
            f"{v1.ICTRP}condition",
            "https://universalevidence.com/crosswalk/who-ictrp-conditions/",
        ),
        "outcome": (
            f"(<{v1.ICTRP}primaryOutcome>|<{v1.ICTRP}secondaryOutcome>)",
            "https://universalevidence.com/crosswalk/who-ictrp-outcomes/",
        ),
        "intervention": (
            f"{v1.ICTRP}intervention",
            "https://universalevidence.com/crosswalk/who-ictrp-interventions/",
        ),
    },
}


def _source_predicate(path: str) -> str:
    if path.startswith("("):
        return path
    return v1.sparql_iri(path)


def _direct_text_filter(source_id: str, raw: str, crosswalk_raw: str) -> str:
    if source_id == "who-ictrp":
        return f"sameTerm({raw}, {crosswalk_raw})"
    # AEA Search uses its normalized crosswalk key: trimmed and
    # whitespace-collapsed, with case preserved. Mirror that rule here.
    def normalized(value: str) -> str:
        return (
            f'REPLACE(REPLACE(STR({value}), "^[ \\t\\r\\n]+|[ \\t\\r\\n]+$", ""), '
            '"[ \\t\\r\\n]+", " ")'
        )

    return (
        f"sameTerm({raw}, {crosswalk_raw}) || "
        f"{normalized(raw)} = {normalized(crosswalk_raw)}"
    )


def _stored_axis_requirement(
    axis: str, uri: str, study_variable: str = "?study"
) -> str:
    iri = v1.sparql_iri(uri)
    if axis == "state":
        return (
            f"{study_variable} (ue:matchesCondition|ue:matchesOutcome) "
            + iri
            + " ."
        )
    return f"{study_variable} ue:matches{axis.title()} {iri} ."


def _stored_region_requirement(
    source_id: str,
    region_uri: str,
    regions: RegionIndex,
    study_variable: str = "?study",
) -> str:
    descriptor = regions.describe(region_uri)
    if descriptor.kind == "world":
        return ""
    if descriptor.kind == "unknown":
        raise SourceUnavailable(f"unknown region asset: {region_uri}")
    graph_uri = _source_graph(source_id)
    if source_id == "aea":
        if not descriptor.country_uris:
            return "FILTER(false)"
        countries = " ".join(
            v1.sparql_iri(uri) for uri in descriptor.country_uris
        )
        return f"""
FILTER EXISTS {{
  GRAPH <{graph_uri}> {{ {study_variable} <{v1.AEA}country> ?regionRaw . }}
  GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{
    ?regionEntry ue:rawText ?regionRaw ;
      (skos:exactMatch|skos:closeMatch) ?regionConcept .
    FILTER(STRSTARTS(STR(?regionEntry), "https://universalevidence.com/crosswalk/aea-regions/"))
    VALUES ?regionConcept {{ {countries} }}
  }}
}}"""
    if not descriptor.country_isos:
        return "FILTER(false)"
    isos = " ".join(v1.sparql_string(iso) for iso in descriptor.country_isos)
    return f"""
FILTER EXISTS {{
  GRAPH <{graph_uri}> {{
    VALUES ?regionIso {{ {isos} }}
    {study_variable} <{v1.ICTRP}countryIsoAlpha2> ?regionIso .
  }}
}}"""


def _stored_selector_body(
    source_id: str,
    query: CanonicalQuery,
    regions: RegionIndex | None,
    study_variable: str = "?study",
) -> str:
    graph_uri = _source_graph(source_id)
    anchor = (
        f"{study_variable} a <{v1.AEA}RCTStudy> ."
        if source_id == "aea"
        else f"{study_variable} a <{v1.UE}Evidence> ."
    )
    branches: list[str] = []
    for clause in plan_query(query):
        requirements: list[str] = []
        region_requirements: list[str] = []
        for spec in clause.specs:
            for axis in ("state", "condition", "intervention", "outcome"):
                if spec.get(axis):
                    value = _stored_axis_requirement(
                        axis, spec[axis], study_variable
                    )
                    if value not in requirements:
                        requirements.append(value)
            if spec.get("region"):
                if regions is None:
                    raise SourceUnavailable("region index unavailable")
                value = _stored_region_requirement(
                    source_id, spec["region"], regions, study_variable
                )
                if value and value not in region_requirements:
                    region_requirements.append(value)
        branches.append(
            "{\n  GRAPH <"
            + graph_uri
            + "> {\n    "
            + anchor
            + ("\n    " + "\n    ".join(requirements) if requirements else "")
            + "\n  }\n  "
            + "\n  ".join(region_requirements)
            + "\n}"
        )
    return "\nUNION\n".join(branches)


def stored_selection_query(
    source_id: str,
    query: CanonicalQuery,
    regions: RegionIndex | None = None,
) -> str:
    """Select one deterministic stored-source window before attribution.

    The 101st URI is a sentinel proving that the public 100-study source cap
    was reached.  Keeping this query independent from raw-text crosswalks
    prevents taxonomy cardinality from changing either selection or latency.
    """
    selector = _stored_selector_body(source_id, query, regions)
    return f"""
PREFIX ue: <{v1.UE}>
PREFIX skos: <{v1.SKOS}>
SELECT DISTINCT ?study WHERE {{
{selector}
}}
ORDER BY STR(?study)
LIMIT {UNIQUE_STUDY_LIMIT + 1}
"""


def stored_raw_attribution_query(
    source_id: str,
    study_uris: Sequence[str],
) -> str:
    """Fetch only native raw fields for an already bounded study window."""
    if not study_uris:
        raise ValueError("stored raw attribution requires at least one study")
    graph_uri = _source_graph(source_id)
    config = _DIRECT_CROSSWALKS[source_id]
    values = " ".join(v1.sparql_iri(uri) for uri in study_uris)
    state_branches: list[str] = []
    for role in ("condition", "outcome"):
        predicate, _prefix = config[role]
        state_branches.append(
            f"""{{
      ?study {_source_predicate(predicate)} ?rawText .
      BIND("{_STORED_STATE_RAW_ROW}" AS ?rowKind)
      BIND("{role}" AS ?role)
    }}"""
        )
    intervention_predicate, _prefix = config["intervention"]
    branches = "\n    UNION\n    ".join(
        state_branches
        + [
            f"""{{
      ?study {_source_predicate(intervention_predicate)} ?rawText .
      BIND("{_STORED_INTERVENTION_RAW_ROW}" AS ?rowKind)
      BIND("intervention" AS ?role)
    }}"""
        ]
    )
    return f"""
SELECT DISTINCT ?study ?rowKind ?role ?rawText WHERE {{
  VALUES ?study {{ {values} }}
  GRAPH <{graph_uri}> {{
    {branches}
  }}
}}
ORDER BY STR(?study) ?rowKind ?role STR(?rawText)
"""


def stored_study_detail_query(
    source_id: str,
    study_uris: Sequence[str],
) -> str:
    """Hydrate public detail fields for a previously attributed edge."""
    if not study_uris:
        raise ValueError("stored detail hydration requires at least one study")
    graph_uri = _source_graph(source_id)
    values = " ".join(v1.sparql_iri(uri) for uri in study_uris)
    if source_id == "aea":
        optional = f"""
    OPTIONAL {{ ?study <{v1.UE}studyStatus> ?status . }}
    OPTIONAL {{ ?study <{v1.AEA}studyStatus> ?status . }}
    OPTIONAL {{ ?study <{v1.DCTERMS}date> ?date . }}
    OPTIONAL {{ ?study <{v1.AEA}country> ?country . }}"""
    else:
        optional = f"""
    OPTIONAL {{ ?study <{v1.ICTRP}dateOfRegistration> ?date . }}
    OPTIONAL {{ ?study <{v1.ICTRP}country> ?country . }}"""
    return f"""
SELECT DISTINCT ?study ?study_id ?title ?status ?date ?country WHERE {{
  VALUES ?study {{ {values} }}
  GRAPH <{graph_uri}> {{
    OPTIONAL {{ ?study <{v1.DCTERMS}identifier> ?study_id . }}
    OPTIONAL {{ ?study <{v1.DCTERMS}title> ?title . }}
{optional}
  }}
}}
ORDER BY STR(?study)
"""


def _stored_projection_roles(query: CanonicalQuery) -> tuple[str, ...]:
    if query.values["state"]:
        return ("condition", "outcome")
    roles = tuple(
        role for role in ("condition", "outcome") if query.values[role]
    )
    return roles or ("condition", "outcome")


def _stored_direct_pair_pattern(
    source_id: str,
    query: CanonicalQuery,
    retained_state_uris: Sequence[str],
) -> str:
    graph_uri = _source_graph(source_id)
    config = _DIRECT_CROSSWALKS[source_id]
    state_branches: list[str] = []
    for role in _stored_projection_roles(query):
        predicate, prefix = config[role]
        retained_values = _candidate_values("state", retained_state_uris)
        roots = tuple(
            sorted(
                set(query.values["state"]).union(query.values[role])
            )
        )
        root_scope = ""
        if roots:
            root_values = " ".join(v1.sparql_iri(uri) for uri in roots)
            root_variable = f"{role}StateRoot"
            root_scope = f"""
    VALUES ?{root_variable} {{ {root_values} }}
    ?state skos:broader* ?{root_variable} ."""
        state_branches.append(f"""{{
{retained_values}
  GRAPH <{graph_uri}> {{
    ?study {_source_predicate(predicate)} ?stateRaw .
  }}
  GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{
    ?stateEntry ue:rawText ?stateCrosswalkRaw ;
      (skos:exactMatch|skos:closeMatch) ?state .
    ?state a ue:State ; skos:prefLabel ?stateLabel .
{root_scope}
  }}
  FILTER(STRSTARTS(STR(?stateEntry), "{prefix}"))
  FILTER({_direct_text_filter(source_id, '?stateRaw', '?stateCrosswalkRaw')})
}}""")
    intervention_predicate, intervention_prefix = config["intervention"]
    return f"""
{{ {(' UNION '.join(state_branches))} }}
GRAPH <{graph_uri}> {{
  ?study {_source_predicate(intervention_predicate)} ?interventionRaw .
}}
GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{
  ?interventionEntry ue:rawText ?interventionCrosswalkRaw ;
    (skos:exactMatch|skos:closeMatch) ?intervention .
  ?intervention a ue:Intervention ; skos:prefLabel ?interventionLabel .
}}
FILTER(STRSTARTS(STR(?interventionEntry), "{intervention_prefix}"))
FILTER({_direct_text_filter(source_id, '?interventionRaw', '?interventionCrosswalkRaw')})"""


def local_group_query(
    source_id: str,
    query: CanonicalQuery,
    condition_uris: Sequence[str],
    intervention_uris: Sequence[str],
    regions: RegionIndex | None = None,
) -> str:
    intervention_values = _candidate_values("intervention", intervention_uris)
    selection_selector = _stored_selector_body(
        source_id, query, regions, study_variable="?selectionStudy"
    )
    pair_selector = _stored_selector_body(source_id, query, regions)
    pairs = _stored_direct_pair_pattern(source_id, query, condition_uris)
    return f"""
PREFIX ue: <{v1.UE}>
PREFIX skos: <{v1.SKOS}>
SELECT ?rowKind ?rowOrder ?state ?stateLabel ?intervention ?interventionLabel
       (COUNT(DISTINCT ?supportStudy) AS ?weight)
       (GROUP_CONCAT(DISTINCT STR(?supportStudy); separator="{GROUP_SEPARATOR}") AS ?studyUris)
WHERE {{
  {{
    {{
      SELECT DISTINCT ?selectionStudy WHERE {{
{selection_selector}
      }}
      ORDER BY STR(?selectionStudy)
      LIMIT {UNIQUE_STUDY_LIMIT + 1}
    }}
    BIND("{_STORED_SELECTION_ROW}" AS ?rowKind)
    BIND(0 AS ?rowOrder)
    BIND(?selectionStudy AS ?supportStudy)
  }}
  UNION
  {{
    {{
      SELECT DISTINCT ?study WHERE {{
{pair_selector}
      }}
      ORDER BY STR(?study)
      LIMIT {UNIQUE_STUDY_LIMIT}
    }}
    BIND("{_STORED_PAIR_ROW}" AS ?rowKind)
    BIND(1 AS ?rowOrder)
    BIND(?study AS ?supportStudy)
{intervention_values}
{pairs}
  }}
}}
GROUP BY ?rowKind ?rowOrder ?state ?stateLabel ?intervention ?interventionLabel
ORDER BY ?rowOrder DESC(?weight) ?state ?intervention
LIMIT {MAX_GROUPED_ROWS + 2}
"""


@lru_cache(maxsize=None)
def _state_ancestor_uris(uri: str) -> frozenset[str]:
    graph = _state_graph()
    seen: set[str] = {uri}
    frontier = [URIRef(uri)]
    while frontier:
        child = frontier.pop()
        for parent in graph.objects(child, SKOS.broader):
            parent_uri = str(parent)
            if parent_uri not in seen:
                seen.add(parent_uri)
                frontier.append(URIRef(parent))
    return frozenset(seen)


def _allowed_stored_states_by_role(
    query: CanonicalQuery,
    retained_state_uris: Sequence[str],
) -> dict[str, set[str]]:
    retained = set(retained_state_uris)
    allowed: dict[str, set[str]] = {}
    for role in _stored_projection_roles(query):
        roots = set(query.values["state"]).union(query.values[role])
        allowed[role] = {
            uri
            for uri in retained
            if not roots or roots.intersection(_state_ancestor_uris(uri))
        }
    return allowed


async def _stored_direct_maps(
    source_id: str,
    roles: Sequence[str],
) -> tuple[dict[str, Mapping[str, Sequence[str]]], Mapping[str, Sequence[str]]]:
    state_maps, intervention_map = await asyncio.gather(
        asyncio.gather(
            *(
                asyncio.to_thread(
                    _source_direct_state_crosswalk, source_id, role
                )
                for role in roles
            )
        ),
        asyncio.to_thread(_source_direct_intervention_crosswalk, source_id),
    )
    return dict(zip(roles, state_maps)), intervention_map


async def _load_stored_support(
    source_id: str,
    query: CanonicalQuery,
    retained_state_uris: Sequence[str],
    retained_intervention_uris: Sequence[str],
    regions: RegionIndex | None = None,
    *,
    budget: SourceBudget | None = None,
) -> tuple[
    tuple[str, ...],
    dict[tuple[str, str], tuple[str, ...]],
    bool,
]:
    """Select, hydrate, and directly attribute one stored-source window."""
    source_budget = budget or SourceBudget(max_requests=2, seconds=5.0)
    async with _bounded_slot(v1._LOCAL_QUERY_SEMAPHORE, source_budget):
        selection_rows = await _sparql_select(
            stored_selection_query(source_id, query, regions), source_budget
        )
        selected_window = tuple(
            sorted(
                {
                    str(row.get("study") or "")
                    for row in selection_rows
                    if row.get("study")
                }
            )
        )
        selected_studies = selected_window[:UNIQUE_STUDY_LIMIT]
        raw_rows = (
            await _sparql_select(
                stored_raw_attribution_query(source_id, selected_studies),
                source_budget,
            )
            if selected_studies
            else []
        )

    roles = _stored_projection_roles(query)
    state_maps, intervention_map = await _stored_direct_maps(source_id, roles)
    allowed_states = _allowed_stored_states_by_role(
        query, retained_state_uris
    )
    restrict_states = bool(_graph_state_roots(query))
    allowed_interventions = set(retained_intervention_uris)
    restrict_interventions = bool(query.values["intervention"])
    selected_set = set(selected_studies)
    states_by_study: dict[str, set[str]] = defaultdict(set)
    interventions_by_study: dict[str, set[str]] = defaultdict(set)
    for row in raw_rows:
        study_uri = str(row.get("study") or "")
        if study_uri not in selected_set:
            continue
        row_kind = str(row.get("rowKind") or "")
        role = str(row.get("role") or "")
        raw_text = row.get("rawText")
        if raw_text is None:
            continue
        if row_kind == _STORED_STATE_RAW_ROW and role in state_maps:
            states_by_study[study_uri].update(
                uri
                for uri in _mapped_source_state_uris(
                    source_id, state_maps[role], raw_text
                )
                if not restrict_states or uri in allowed_states[role]
            )
        elif row_kind == _STORED_INTERVENTION_RAW_ROW:
            interventions_by_study[study_uri].update(
                uri
                for uri in _mapped_source_intervention_uris(
                    source_id, intervention_map, raw_text
                )
                if not restrict_interventions or uri in allowed_interventions
            )

    support: dict[tuple[str, str], set[str]] = defaultdict(set)
    for study_uri in selected_studies:
        for state_uri in states_by_study[study_uri]:
            for intervention_uri in interventions_by_study[study_uri]:
                support[(state_uri, intervention_uri)].add(study_uri)
    return (
        selected_studies,
        {
            coordinate: tuple(sorted(studies))
            for coordinate, studies in support.items()
        },
        len(selected_window) > UNIQUE_STUDY_LIMIT,
    )


async def load_local_groups(
    source_id: str,
    query: CanonicalQuery,
    condition_uris: Sequence[str],
    intervention_uris: Sequence[str],
    regions: RegionIndex | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any], float]:
    started = time.monotonic()
    selected_studies, support, source_limit_reached = (
        await _load_stored_support(
            source_id,
            query,
            condition_uris,
            intervention_uris,
            regions,
        )
    )
    state_graph = _state_graph()
    intervention_graph = _intervention_graph()
    ranked = sorted(
        support.items(),
        key=lambda item: (-len(item[1]), item[0][0], item[0][1]),
    )
    truncated = len(ranked) > MAX_GROUPED_ROWS
    normalized_rows = [
        {
            "state": state_uri,
            "stateLabel": _label(state_graph, URIRef(state_uri)),
            "intervention": intervention_uri,
            "interventionLabel": _label(
                intervention_graph, URIRef(intervention_uri)
            ),
            "weight": str(len(study_uris)),
            "studyUris": GROUP_SEPARATOR.join(study_uris),
        }
        for (state_uri, intervention_uri), study_uris in ranked[
            :MAX_GROUPED_ROWS
        ]
    ]
    omitted = max(0, len(ranked) - len(normalized_rows))
    meta = {
        "status": "included",
        "coverage": "full",
        "returned_unique_studies": len(selected_studies),
        "truncated": truncated or source_limit_reached,
        "approximate": truncated or source_limit_reached,
        "reason": (
            "budget_exhausted"
            if truncated
            else "source_limit" if source_limit_reached else None
        ),
        "grouped_rows": len(normalized_rows),
        "grouped_rows_omitted_lower_bound": omitted,
        "budget_exhausted": truncated,
        "source_limit_reached": source_limit_reached,
        "source_limit_omitted_lower_bound": 1 if source_limit_reached else 0,
    }
    return normalized_rows, meta, (time.monotonic() - started) * 1000


def cap_grouped_rows(
    local_rows: Mapping[str, Sequence[Mapping[str, str]]],
    source_meta: dict[str, Mapping[str, Any]],
) -> dict[str, list[Mapping[str, str]]]:
    """Apply the 2,000-row contract once across all stored sources."""
    ranked = sorted(
        (
            (source_id, row)
            for source_id, rows in local_rows.items()
            for row in rows
        ),
        key=lambda item: (
            -int(item[1].get("weight") or 0),
            item[0],
            str(item[1].get("state") or item[1].get("condition") or ""),
            str(item[1].get("intervention") or ""),
        ),
    )
    retained = ranked[:MAX_GROUPED_ROWS]
    kept: dict[str, list[Mapping[str, str]]] = {
        source_id: [] for source_id in local_rows
    }
    for source_id, row in retained:
        kept[source_id].append(row)

    for source_id, original_rows in local_rows.items():
        meta = dict(source_meta.get(source_id) or {})
        omitted = max(0, len(original_rows) - len(kept[source_id]))
        meta["grouped_rows"] = len(kept[source_id])
        meta["grouped_rows_omitted_lower_bound"] = int(
            meta.get("grouped_rows_omitted_lower_bound") or 0
        ) + omitted
        if omitted:
            meta["truncated"] = True
            meta["approximate"] = True
            meta["reason"] = "budget_exhausted"
            meta["budget_exhausted"] = True
        source_meta[source_id] = meta
    return kept


def _failed_source_meta(reason: str, *, status: str = "error") -> dict[str, Any]:
    return {
        "status": status,
        "coverage": "none",
        "returned_unique_studies": 0,
        "truncated": True,
        "approximate": True,
        "reason": reason,
        "grouped_rows": 0,
        "grouped_rows_omitted_lower_bound": 0,
        "budget_exhausted": reason == "budget_exhausted",
        "source_limit_reached": False,
        "source_limit_omitted_lower_bound": 0,
    }


def _effective_stored_query(
    query: CanonicalQuery,
    regions: RegionIndex,
) -> tuple[CanonicalQuery | None, bool]:
    """Apply the stored-source ADM1 capability contract once."""
    region_values = query.values["region"]
    region_kinds = {regions.kind(uri) for uri in region_values}
    if "unknown" in region_kinds:
        raise SourceUnavailable("region index unavailable")
    if "adm1" not in region_kinds:
        return query, False
    supported = tuple(
        uri for uri in region_values if regions.kind(uri) != "adm1"
    )
    if query.logic["region"] == "or" and supported:
        values = dict(query.values)
        values["region"] = supported
        return CanonicalQuery(values=values, logic=query.logic), True
    return None, False


def _edge_record(state: str, intervention: str, intervention_label: str) -> dict[str, Any]:
    return {
        "id": stable_evidence_edge_id(state, intervention),
        "kind": "evidence",
        "source": intervention,
        "target": state,
        "label": intervention_label,
        "study_keys": set(),
        "source_studies": defaultdict(set),
        "design_counts": defaultdict(int),
    }


def _live_edge_coordinates(
    row: Mapping[str, Any],
    query: CanonicalQuery,
    state_ids: set[str],
    intervention_ids: set[str],
) -> tuple[tuple[str, str], ...]:
    """Project only row-carried live-source coordinates onto graph edges."""
    intervention = str(row.get("intervention_concept_uri") or "")
    if not intervention:
        return ()
    if query.values["intervention"] and intervention not in intervention_ids:
        return ()
    states: list[str] = []

    def append_state(value: object) -> None:
        state = str(value or "")
        if state and state not in states:
            states.append(state)

    condition_state = str(row.get("condition_concept_uri") or "")
    projected_state = str(row.get("state_concept_uri") or "")
    outcome_states: list[str] = []
    for outcome in row.get("outcomes") or ():
        if isinstance(outcome, Mapping):
            state = str(outcome.get("state_concept_uri") or "")
            if state and state not in outcome_states:
                outcome_states.append(state)

    strict_condition = bool(query.values["condition"]) and not (
        query.values["state"] or query.values["outcome"]
    )
    strict_outcome = bool(query.values["outcome"]) and not (
        query.values["state"] or query.values["condition"]
    )
    if strict_condition:
        append_state(condition_state)
    elif strict_outcome:
        for state in outcome_states:
            append_state(state)
        # Live adapters use the top-level State projection for direct outcome
        # multimaps that cannot be represented on one nested outcome object.
        if not condition_state:
            append_state(projected_state)
    else:
        append_state(projected_state)
        append_state(condition_state)
        for state in outcome_states:
            append_state(state)

    roots = _graph_state_roots(query)
    if roots:
        states = [state for state in states if state in state_ids]
    return tuple((state, intervention) for state in states)


def build_graph_v2_payload(
    *,
    query: CanonicalQuery,
    condition_taxonomy: Mapping[str, Any],
    intervention_taxonomy: Mapping[str, Any],
    local_rows: Mapping[str, Sequence[Mapping[str, str]]],
    live_rows: Mapping[str, Sequence[Mapping[str, Any]]],
    source_meta: Mapping[str, Mapping[str, Any]],
    source_timings_ms: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    state_nodes = [
        {**dict(node), "class": "State"}
        for node in condition_taxonomy["nodes"]
    ]
    intervention_nodes = [dict(node) for node in intervention_taxonomy["nodes"]]
    state_ids = {node["id"] for node in state_nodes}
    intervention_ids = {node["id"] for node in intervention_nodes}
    restrict_states = bool(_graph_state_roots(query))
    restrict_interventions = bool(query.values["intervention"])
    state_studies: dict[str, set[tuple[str, str]]] = defaultdict(set)
    state_labels = {node["id"]: node["label"] for node in state_nodes}
    intervention_labels: dict[str, str] = {}
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    omitted_missing_intervention = 0
    dynamic_state_omitted = 0
    dynamic_intervention_omitted = 0
    attributed_by_source: dict[str, set[tuple[str, str]]] = defaultdict(set)

    for source_id, rows in local_rows.items():
        for row in rows:
            state = str(row.get("state") or row.get("condition") or "")
            intervention = str(row.get("intervention") or "")
            if not state or not intervention:
                continue
            if restrict_states and state not in state_ids:
                continue
            if restrict_interventions and intervention not in intervention_ids:
                continue
            state_labels[state] = _label(_state_graph(), URIRef(state))
            label = _label(_intervention_graph(), URIRef(intervention))
            record = edges.setdefault(
                (state, intervention),
                _edge_record(state, intervention, label),
            )
            intervention_labels[intervention] = label
            studies = {
                (source_id, study_uri)
                for study_uri in str(row.get("studyUris") or "").split(GROUP_SEPARATOR)
                if study_uri
            }
            record["study_keys"].update(studies)
            record["source_studies"][source_id].update(studies)
            attributed_by_source[source_id].update(studies)
            design = "rct" if source_id == "aea" else "unknown"
            record["design_counts"][design] += len(studies)
            state_studies[state].update(studies)

    for source_id, rows in live_rows.items():
        seen_pairs: set[tuple[tuple[str, str], str, str]] = set()
        for row in rows:
            key = study_key(row, source_id)
            if not key[1]:
                continue
            if not row.get("intervention_concept_uri"):
                omitted_missing_intervention += 1
            coordinates = _live_edge_coordinates(
                row, query, state_ids, intervention_ids
            )
            if not coordinates:
                continue
            for state, intervention in coordinates:
                dedupe_key = (key, state, intervention)
                if dedupe_key in seen_pairs:
                    continue
                seen_pairs.add(dedupe_key)
                # A row can contribute several condition/outcome coordinates.
                # Its singular display label does not identify each coordinate.
                label = _label(_intervention_graph(), URIRef(intervention))
                state_labels[state] = _label(_state_graph(), URIRef(state))
                record = edges.setdefault(
                    (state, intervention),
                    _edge_record(state, intervention, label),
                )
                intervention_labels[intervention] = label
                record["study_keys"].add(key)
                record["source_studies"][source_id].add(key)
                attributed_by_source[source_id].add(key)
                record["design_counts"]["unknown"] += 1
                state_studies[state].add(key)

    if not restrict_states:
        dynamic_state_omitted = max(
            0, len(state_studies) - MAX_STATE_NODES
        )
        state_nodes = [
            {
                "id": uri,
                "label": state_labels.get(uri) or uri.rsplit("/", 1)[-1],
                "class": "State",
                "studyCount": 0,
                "selected": False,
            }
            for uri in sorted(
                state_studies,
                key=lambda value: (state_labels.get(value, value).casefold(), value),
            )[:MAX_STATE_NODES]
        ]
        state_ids = {node["id"] for node in state_nodes}

    evidence_edges: list[dict[str, Any]] = []
    evidence_edge_studies: dict[str, dict[str, set[tuple[str, str]]]] = {}
    for record in edges.values():
        source_counts = {
            source_id: len(studies)
            for source_id, studies in sorted(record["source_studies"].items())
        }
        source_membership_digests = {
            source_id: _study_membership_digest(
                source_id,
                [study_id for _source_id, study_id in studies],
            )
            for source_id, studies in sorted(record["source_studies"].items())
        }
        weighted_score = sum(
            count * SOURCE_DESIGN_WEIGHTS.get(source_id, 1.0)
            for source_id, count in source_counts.items()
        )
        evidence_edges.append(
            {
                "id": record["id"],
                "kind": "evidence",
                "source": record["source"],
                "target": record["target"],
                "weight": len(record["study_keys"]),
                "weighted_score": round(weighted_score, 3),
                "confidence": confidence_label(weighted_score),
                "study_design_counts": dict(sorted(record["design_counts"].items())),
                "source_counts": source_counts,
                _EDGE_MEMBERSHIP_DIGESTS: source_membership_digests,
            }
        )
        evidence_edge_studies[record["id"]] = {
            source_id: set(studies)
            for source_id, studies in record["source_studies"].items()
        }
    evidence_edges.sort(key=lambda edge: (-edge["weight"], edge["id"]))

    kept_edges: list[dict[str, Any]] = []
    kept_interventions: set[str] = set(intervention_ids if restrict_interventions else ())
    intervention_capacity = min(
        MAX_INTERVENTION_NODES,
        max(0, MAX_TOTAL_NODES - len(state_nodes)),
    )
    for edge in evidence_edges:
        intervention = edge["source"]
        if edge["target"] not in state_ids:
            continue
        if intervention not in kept_interventions and len(kept_interventions) >= intervention_capacity:
            continue
        if len(kept_edges) >= MAX_EVIDENCE_EDGES:
            break
        kept_interventions.add(intervention)
        kept_edges.append(edge)

    if not restrict_states:
        kept_state_ids = {edge["target"] for edge in kept_edges}
        dynamic_state_omitted = max(0, len(state_studies) - len(kept_state_ids))
        state_nodes = [
            node for node in state_nodes if node["id"] in kept_state_ids
        ]
        state_ids = kept_state_ids

    retained_state_studies: dict[str, set[tuple[str, str]]] = defaultdict(set)
    retained_intervention_studies: dict[
        str, set[tuple[str, str]]
    ] = defaultdict(set)
    for edge in kept_edges:
        edge_studies = evidence_edge_studies.get(edge["id"], {})
        for studies in edge_studies.values():
            retained_state_studies[edge["target"]].update(studies)
            retained_intervention_studies[edge["source"]].update(studies)

    for node in state_nodes:
        node["studyCount"] = len(retained_state_studies[node["id"]])
        node.pop("depth", None)

    if not restrict_interventions:
        candidate_interventions = {edge["source"] for edge in evidence_edges}
        dynamic_intervention_omitted = max(
            0, len(candidate_interventions) - len(kept_interventions)
        )
        intervention_nodes = [
            {
                "id": uri,
                "label": intervention_labels.get(uri) or uri.rsplit("/", 1)[-1],
                "class": "Intervention",
                "studyCount": len(retained_intervention_studies[uri]),
                "selected": False,
            }
            for uri in sorted(
                kept_interventions,
                key=lambda value: (
                    intervention_labels.get(value, value).casefold(),
                    value,
                ),
            )
        ]
    for node in intervention_nodes:
        node["studyCount"] = len(retained_intervention_studies[node["id"]])
        node.pop("depth", None)

    hierarchy_pairs = list(condition_taxonomy["pairs"])
    retained_intervention_ids = {node["id"] for node in intervention_nodes}
    hierarchy_pairs.extend(
        (parent, child)
        for parent, child in intervention_taxonomy["pairs"]
        if parent in retained_intervention_ids and child in retained_intervention_ids
    )
    hierarchy_edges = [
        {
            "id": stable_hierarchy_edge_id(parent, child),
            "kind": "hierarchy",
            "source": parent,
            "target": child,
        }
        for parent, child in hierarchy_pairs
    ]

    displayed_by_source: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for edge in kept_edges:
        for source_id, studies in evidence_edge_studies.get(edge["id"], {}).items():
            displayed_by_source[source_id].update(studies)
    sources: dict[str, dict[str, Any]] = {}
    for source_id in SOURCE_IDS:
        source = dict(
            source_meta.get(source_id)
            or _failed_source_meta("upstream_unavailable", status="unavailable")
        )
        selected_count = int(source.get("returned_unique_studies") or 0)
        attributed_count = len(attributed_by_source[source_id])
        displayed_count = len(displayed_by_source[source_id])
        source.update(
            {
                "selected_unique_studies": selected_count,
                "attributed_unique_studies": attributed_count,
                "returned_unique_studies": displayed_count,
                "omitted_unattributed_unique_studies": max(
                    0, selected_count - attributed_count
                ),
                "omitted_by_graph_caps_unique_studies": max(
                    0, attributed_count - displayed_count
                ),
                "grouped_rows": int(source.get("grouped_rows") or 0),
                "grouped_rows_omitted_lower_bound": int(
                    source.get("grouped_rows_omitted_lower_bound") or 0
                ),
                "budget_exhausted": bool(source.get("budget_exhausted")),
                "source_limit_reached": bool(
                    source.get("source_limit_reached")
                ),
                "source_limit_omitted_lower_bound": int(
                    source.get("source_limit_omitted_lower_bound") or 0
                ),
            }
        )
        sources[source_id] = source
    unique_studies = {
        key for studies in displayed_by_source.values() for key in studies
    }
    intervention_nodes_omitted = int(
        intervention_taxonomy["omitted"] + dynamic_intervention_omitted
    )
    evidence_edges_omitted = max(0, len(evidence_edges) - len(kept_edges))
    grouped_rows = sum(source["grouped_rows"] for source in sources.values())
    grouped_rows_omitted = sum(
        source["grouped_rows_omitted_lower_bound"] for source in sources.values()
    )
    graph_truncated = bool(
        condition_taxonomy["omitted"]
        or dynamic_state_omitted
        or intervention_nodes_omitted
        or evidence_edges_omitted
        or grouped_rows_omitted
        or any(meta.get("truncated") for meta in sources.values())
    )
    graph_approximate = any(meta.get("approximate") for meta in sources.values())

    return {
        "nodes": state_nodes + intervention_nodes,
        "edges": hierarchy_edges + kept_edges,
        "metadata": {
            "stateNodes": len(state_nodes),
            "stateNodesOmitted": int(
                condition_taxonomy["omitted"] + dynamic_state_omitted
            ),
            # Deprecated for one compatibility window; exact aliases only.
            "conditionNodes": len(state_nodes),
            "conditionNodesOmitted": int(
                condition_taxonomy["omitted"] + dynamic_state_omitted
            ),
            "interventionNodes": len(intervention_nodes),
            "interventionNodesOmitted": intervention_nodes_omitted,
            "totalNodes": len(state_nodes) + len(intervention_nodes),
            "totalNodesOmitted": int(
                condition_taxonomy["omitted"]
                + dynamic_state_omitted
                + intervention_nodes_omitted
            ),
            "evidenceEdges": len(kept_edges),
            "evidenceEdgesOmitted": evidence_edges_omitted,
            "groupedRows": grouped_rows,
            "groupedRowsOmittedLowerBound": grouped_rows_omitted,
            "omittedMissingIntervention": omitted_missing_intervention,
            "caps": {
                "stateNodes": MAX_STATE_NODES,
                # Deprecated exact alias.
                "conditionNodes": MAX_STATE_NODES,
                "interventionNodes": MAX_INTERVENTION_NODES,
                "totalNodes": MAX_TOTAL_NODES,
                "evidenceEdges": MAX_EVIDENCE_EDGES,
                "groupedRows": MAX_GROUPED_ROWS,
            },
        },
        "meta": {
            "api_version": "query-v2",
            "schemaVersion": SCHEMA_VERSION,
            "returned_unique_studies": len(unique_studies),
            "limit_per_source_branch": 100,
            "truncated": graph_truncated,
            "approximate": graph_approximate,
            "sound": True,
            "budget_exhausted": any(
                source["budget_exhausted"] for source in sources.values()
            ),
            "sources": sources,
            "source_timings_ms": {
                key: round(value, 3)
                for key, value in sorted((source_timings_ms or {}).items())
            },
        },
    }


async def execute_graph_v2(query: CanonicalQuery) -> dict[str, Any]:
    condition_taxonomy, intervention_taxonomy = await asyncio.gather(
        asyncio.to_thread(state_forest, _graph_state_roots(query)),
        asyncio.to_thread(intervention_forest, query.values["intervention"]),
    )
    condition_uris = [node["id"] for node in condition_taxonomy["nodes"]]
    intervention_uris = [node["id"] for node in intervention_taxonomy["nodes"]]
    try:
        regions = await region_index_for_query(query)
    except SourceUnavailable:
        regions = None

    adapters = default_adapters()
    started_by_source: dict[str, float] = {}

    async def local_task(source_id: str):
        started_by_source[source_id] = time.monotonic()
        if regions is None:
            raise SourceUnavailable("region index unavailable")
        effective_query, capability_partial = _effective_stored_query(
            query, regions
        )
        if effective_query is None:
            duration = (time.monotonic() - started_by_source[source_id]) * 1000
            return [], {
                **_failed_source_meta(
                    "unsupported_admin_level", status="excluded"
                ),
                "coverage": "country_only",
                "truncated": False,
                "approximate": False,
            }, duration
        rows, meta, duration = await load_local_groups(
            source_id,
            effective_query,
            condition_uris,
            intervention_uris,
            regions,
        )
        if capability_partial:
            meta.update(
                {
                    "coverage": "country_only",
                    "truncated": True,
                    "approximate": True,
                    "reason": "unsupported_admin_level",
                }
            )
        return rows, meta, duration

    async def live_task(source_id: str):
        started_by_source[source_id] = time.monotonic()
        if regions is None:
            raise SourceUnavailable("region index unavailable")
        rows, meta = await execute_graph_source(
            query, adapters[source_id], regions
        )
        return rows, meta, (time.monotonic() - started_by_source[source_id]) * 1000

    tasks = {
        "aea": asyncio.create_task(local_task("aea")),
        "who-ictrp": asyncio.create_task(local_task("who-ictrp")),
        "ctgov": asyncio.create_task(live_task("ctgov")),
        "isrctn": asyncio.create_task(live_task("isrctn")),
    }
    done, pending = await asyncio.wait(tasks.values(), timeout=GRAPH_DEADLINE_SECONDS)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    local_rows: dict[str, Sequence[Mapping[str, str]]] = {}
    live_rows: dict[str, Sequence[Mapping[str, Any]]] = {}
    source_meta: dict[str, Mapping[str, Any]] = {}
    timings: dict[str, float] = {}
    for source_id, task in tasks.items():
        if task in pending:
            source_meta[source_id] = _failed_source_meta(
                "budget_exhausted", status="included"
            )
            timings[source_id] = GRAPH_DEADLINE_SECONDS * 1000
            continue
        try:
            rows, meta, duration_ms = task.result()
        except (
            SourceBudgetExhausted,
            asyncio.TimeoutError,
            httpx.TimeoutException,
        ):
            rows = []
            meta = _failed_source_meta(
                "budget_exhausted", status="included"
            )
            duration_ms = (
                time.monotonic()
                - started_by_source.get(source_id, time.monotonic())
            ) * 1000
        except SourceUnavailable:
            rows = []
            meta = _failed_source_meta("upstream_unavailable", status="unavailable")
            duration_ms = (time.monotonic() - started_by_source.get(source_id, time.monotonic())) * 1000
        except Exception:
            logger.warning("graph-v2 %s source failed", source_id, exc_info=True)
            rows = []
            meta = _failed_source_meta("upstream_error")
            duration_ms = (time.monotonic() - started_by_source.get(source_id, time.monotonic())) * 1000
        meta = dict(meta)
        meta.setdefault("grouped_rows", 0)
        meta.setdefault("grouped_rows_omitted_lower_bound", 0)
        meta.setdefault("budget_exhausted", meta.get("reason") == "budget_exhausted")
        meta.setdefault("source_limit_reached", meta.get("reason") == "source_limit")
        meta.setdefault(
            "source_limit_omitted_lower_bound",
            1 if meta.get("source_limit_reached") else 0,
        )
        if source_id in {"aea", "who-ictrp"}:
            local_rows[source_id] = rows
        else:
            live_rows[source_id] = rows
        source_meta[source_id] = meta
        timings[source_id] = duration_ms

    local_rows = cap_grouped_rows(local_rows, source_meta)

    payload = build_graph_v2_payload(
        query=query,
        condition_taxonomy=condition_taxonomy,
        intervention_taxonomy=intervention_taxonomy,
        local_rows=local_rows,
        live_rows=live_rows,
        source_meta=source_meta,
        source_timings_ms=timings,
    )
    return payload


def graph_cache_identity(
    query: CanonicalQuery,
    *,
    taxonomy_version: str | None = None,
    dataset_version: str | None = None,
) -> dict[str, Any]:
    if taxonomy_version is None or dataset_version is None:
        resolved_taxonomy, resolved_dataset, _reason = graph_versions()
        taxonomy_version = taxonomy_version or resolved_taxonomy
        dataset_version = dataset_version or resolved_dataset
    return {
        **query.identity(),
        "schemaVersion": SCHEMA_VERSION,
        "caps": {
            "states": MAX_STATE_NODES,
            "interventions": MAX_INTERVENTION_NODES,
            "nodes": MAX_TOTAL_NODES,
            "edges": MAX_EVIDENCE_EDGES,
            "groupedRows": MAX_GROUPED_ROWS,
        },
        "taxonomyVersion": taxonomy_version,
        "datasetVersion": dataset_version,
    }


def _positive_edge_source_counts(
    expected_source_counts: Mapping[str, int],
) -> dict[str, int]:
    try:
        positive = {
            str(source_id): int(count)
            for source_id, count in expected_source_counts.items()
            if int(count) > 0
        }
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail="graph evidence edge has invalid source counts",
        ) from exc
    if not positive or set(positive) - set(SOURCE_IDS):
        raise HTTPException(
            status_code=500,
            detail="graph evidence edge has invalid source counts",
        )
    return positive


def _edge_source_membership_digests(
    expected_digests: Mapping[str, Any],
    expected_source_counts: Mapping[str, int],
) -> dict[str, str]:
    """Validate the fixed-size membership manifest for an evidence edge."""
    try:
        normalized = {
            str(source_id): digest.lower()
            for source_id, digest in expected_digests.items()
            if isinstance(digest, str)
            and len(digest) == 64
            and all(character in "0123456789abcdefABCDEF" for character in digest)
        }
    except (AttributeError, TypeError) as exc:
        raise HTTPException(
            status_code=500,
            detail="graph evidence edge has invalid source membership digests",
        ) from exc
    if (
        len(normalized) != len(expected_digests)
        or set(normalized) != set(expected_source_counts)
        or set(normalized) - set(SOURCE_IDS)
    ):
        raise HTTPException(
            status_code=500,
            detail="graph evidence edge has invalid source membership digests",
        )
    return dict(sorted(normalized.items()))


def detail_cache_identity(
    query: CanonicalQuery,
    *,
    edge_id: str,
    condition_uri: str,
    intervention_uri: str,
    expected_source_counts: Mapping[str, int],
    expected_source_membership_digests: Mapping[str, str],
    offset: int,
    limit: int,
    taxonomy_version: str | None,
    dataset_version: str | None,
) -> dict[str, Any]:
    """Return a versioned identity for one stable page of edge evidence."""
    return {
        "kind": "edge-details",
        "query": query.identity(),
        "schemaVersion": SCHEMA_VERSION,
        "edgeId": edge_id,
        "edgeState": condition_uri,
        "edgeIntervention": intervention_uri,
        "sourceCounts": dict(sorted(expected_source_counts.items())),
        "sourceMembershipDigests": dict(
            sorted(expected_source_membership_digests.items())
        ),
        "offset": offset,
        "limit": limit,
        "caps": {
            "maxOffset": DETAIL_MAX_OFFSET,
            "maxLimit": DETAIL_MAX_LIMIT,
            "deadlineSeconds": GRAPH_DEADLINE_SECONDS,
        },
        "taxonomyVersion": taxonomy_version,
        "datasetVersion": dataset_version,
    }


def _log_graph_observability(
    query: CanonicalQuery,
    payload: Mapping[str, Any],
    *,
    duration_ms: float,
    cache_status: str,
    cache_bypass_reason: str | None,
) -> None:
    meta = payload.get("meta") or {}
    metadata = payload.get("metadata") or {}
    summary = {
        "schema_version": SCHEMA_VERSION,
        "states": list(query.values["state"]),
        "conditions": list(query.values["condition"]),
        "outcomes": list(query.values["outcome"]),
        "regions": list(query.values["region"]),
        "interventions": list(query.values["intervention"]),
        "duration_ms": round(duration_ms, 3),
        "cache_status": cache_status,
        "cache_bypass_reason": cache_bypass_reason,
        "grouped_rows": int(metadata.get("groupedRows") or 0),
        "returned_nodes": len(payload.get("nodes") or ()),
        "returned_edges": len(payload.get("edges") or ()),
        "truncated": bool(meta.get("truncated")),
        "approximate": bool(meta.get("approximate")),
        "budget_exhausted": bool(meta.get("budget_exhausted")),
    }
    logger.info("graph_request %s", json.dumps(summary, sort_keys=True))
    timings = meta.get("source_timings_ms") or {}
    for source_id, source in sorted((meta.get("sources") or {}).items()):
        source_log = {
            "schema_version": SCHEMA_VERSION,
            "source": source_id,
            "source_duration_ms": round(float(timings.get(source_id) or 0.0), 3),
            "cache_status": cache_status,
            "grouped_rows": int(source.get("grouped_rows") or 0),
            "returned_nodes": summary["returned_nodes"],
            "returned_edges": summary["returned_edges"],
            "truncated": bool(source.get("truncated")),
            "approximate": bool(source.get("approximate")),
            "budget_exhausted": bool(source.get("budget_exhausted")),
        }
        logger.info("graph_source %s", json.dumps(source_log, sort_keys=True))


def _graph_query(
    *,
    state: list[str] | None = None,
    condition: list[str],
    intervention: list[str],
    outcome: list[str],
    region: list[str],
    condition_logic: str,
    intervention_logic: str,
    outcome_logic: str,
    region_logic: str,
    state_logic: str = "or",
) -> CanonicalQuery:
    return build_query_v2_request(
        state=state or [],
        condition=condition,
        intervention=intervention,
        outcome=outcome,
        region=region,
        condition_logic=condition_logic,
        intervention_logic=intervention_logic,
        outcome_logic=outcome_logic,
        region_logic=region_logic,
        state_logic=state_logic,
    )


@router.get("/graph/v2")
async def graph_v2(
    request: Request,
    state: list[str] = Query(default=[]),
    condition: list[str] = Query(default=[]),
    intervention: list[str] = Query(default=[]),
    outcome: list[str] = Query(default=[]),
    region: list[str] = Query(default=[]),
    state_logic: Literal["or", "and"] = "or",
    condition_logic: Literal["or", "and"] = "or",
    intervention_logic: Literal["or", "and"] = "or",
    outcome_logic: Literal["or", "and"] = "or",
    region_logic: Literal["or", "and"] = "or",
) -> dict[str, Any]:
    started = time.monotonic()
    query = _graph_query(
        state=state,
        condition=condition,
        intervention=intervention,
        outcome=outcome,
        region=region,
        condition_logic=condition_logic,
        intervention_logic=intervention_logic,
        outcome_logic=outcome_logic,
        region_logic=region_logic,
        state_logic=state_logic,
    )
    taxonomy_version, dataset_version, bypass_reason = graph_versions()
    identity = graph_cache_identity(
        query,
        taxonomy_version=taxonomy_version,
        dataset_version=dataset_version,
    )
    try:
        payload, cache_status = await await_query_or_disconnect(
            graph_v2_cache.get_or_execute_with_status(
                identity,
                lambda: execute_graph_v2(query),
                enabled=bypass_reason is None,
            ),
            request,
        )
        response = dict(payload)
        token_enabled = _edge_detail_tokens_enabled(
            taxonomy_version,
            dataset_version,
            bypass_reason,
        )
        response_edges: list[dict[str, Any]] = []
        for edge in payload.get("edges", ()):
            public_edge = dict(edge)
            public_edge.pop(_EDGE_MEMBERSHIP_DIGESTS, None)
            if token_enabled and edge.get("kind") == "evidence":
                public_edge["detailToken"] = _encode_edge_detail_token(
                    query,
                    edge,
                    taxonomy_version,
                    dataset_version,
                )
            response_edges.append(public_edge)
        response["edges"] = response_edges
        response_meta = dict(payload.get("meta") or {})
        response_meta.update(
            {
                "cache_status": cache_status,
                "cache_bypass_reason": bypass_reason,
                "taxonomyVersion": taxonomy_version,
                "datasetVersion": dataset_version,
            }
        )
        response["meta"] = response_meta
        _log_graph_observability(
            query,
            response,
            duration_ms=(time.monotonic() - started) * 1000,
            cache_status=cache_status,
            cache_bypass_reason=bypass_reason,
        )
        return response
    except ClientDisconnectedError as exc:
        raise HTTPException(status_code=499, detail="Client disconnected") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _detail_cursor_context(
    query: CanonicalQuery,
    edge_id: str,
    taxonomy_version: str | None,
    dataset_version: str | None,
    source_membership_digests: Mapping[str, str],
) -> str:
    identity = {
        "schemaVersion": SCHEMA_VERSION,
        "edgeId": edge_id,
        "taxonomyVersion": taxonomy_version,
        "datasetVersion": dataset_version,
        "sourceMembershipDigests": dict(
            sorted(source_membership_digests.items())
        ),
        **query.identity(),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:24]


def _edge_detail_token_key(
    _taxonomy_version: str | None,
    _dataset_version: str | None,
) -> bytes:
    """Return a stable signing key for short-lived graph edge manifests."""
    configured = os.getenv("GRAPH_EDGE_TOKEN_SECRET")
    if not configured:
        raise ValueError("graph edge token secret is not configured")
    return hashlib.sha256(configured.encode()).digest()


def _edge_detail_tokens_enabled(
    taxonomy_version: str | None,
    dataset_version: str | None,
    bypass_reason: str | None,
) -> bool:
    return bool(
        os.getenv("GRAPH_EDGE_TOKEN_SECRET")
        and taxonomy_version
        and dataset_version
        and bypass_reason is None
    )


def _edge_detail_query_digest(query: CanonicalQuery) -> str:
    encoded = json.dumps(
        query.identity(),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _encode_edge_detail_token(
    query: CanonicalQuery,
    edge: Mapping[str, Any],
    taxonomy_version: str | None,
    dataset_version: str | None,
) -> str:
    source_counts = _positive_edge_source_counts(edge.get("source_counts") or {})
    source_membership_digests = _edge_source_membership_digests(
        edge.get(_EDGE_MEMBERSHIP_DIGESTS) or {},
        source_counts,
    )
    payload = {
        "v": 2,
        "schemaVersion": SCHEMA_VERSION,
        "edgeId": str(edge.get("id") or ""),
        "state": str(edge.get("target") or ""),
        "intervention": str(edge.get("source") or ""),
        "sourceCounts": source_counts,
        "sourceMembershipDigests": source_membership_digests,
        "queryDigest": _edge_detail_query_digest(query),
        "taxonomyVersion": taxonomy_version,
        "datasetVersion": dataset_version,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    signature = hmac.new(
        _edge_detail_token_key(taxonomy_version, dataset_version),
        encoded.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{signature}"


def _decode_edge_detail_token(
    token: str,
    *,
    query: CanonicalQuery,
    edge_id: str,
    condition_uri: str,
    intervention_uri: str,
    taxonomy_version: str | None,
    dataset_version: str | None,
) -> tuple[dict[str, int], dict[str, str]]:
    try:
        encoded, signature = token.split(".", 1)
        expected_signature = hmac.new(
            _edge_detail_token_key(taxonomy_version, dataset_version),
            encoded.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            raise ValueError("signature mismatch")
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(
            base64.b64decode(
                padded.encode(), altchars=b"-_", validate=True
            ).decode()
        )
        if (
            payload.get("v") != 2
            or payload.get("schemaVersion") != SCHEMA_VERSION
            or payload.get("edgeId") != edge_id
            or payload.get("state") != condition_uri
            or payload.get("intervention") != intervention_uri
            or payload.get("queryDigest") != _edge_detail_query_digest(query)
            or payload.get("taxonomyVersion") != taxonomy_version
            or payload.get("datasetVersion") != dataset_version
        ):
            raise ValueError("manifest context mismatch")
        source_counts = _positive_edge_source_counts(
            payload.get("sourceCounts") or {}
        )
        source_membership_digests = _edge_source_membership_digests(
            payload.get("sourceMembershipDigests") or {},
            source_counts,
        )
    except (
        binascii.Error,
        json.JSONDecodeError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        raise HTTPException(
            status_code=422,
            detail="invalid graph edge detail token",
        ) from exc
    except HTTPException as exc:
        raise HTTPException(
            status_code=422,
            detail="invalid graph edge detail token",
        ) from exc
    return source_counts, source_membership_digests


def _encode_cursor(offset: int, context: str) -> str:
    payload = json.dumps(
        {"v": 1, "offset": offset, "context": context},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str | None, context: str) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(
            padded.encode(), altchars=b"-_", validate=True
        ).decode()
        payload = json.loads(decoded)
        if (
            payload.get("v") != 1
            or payload.get("context") != context
            or isinstance(payload.get("offset"), bool)
        ):
            raise ValueError("cursor context does not match")
        offset = int(payload["offset"])
    except (
        binascii.Error,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise HTTPException(status_code=422, detail="invalid graph detail cursor") from exc
    if offset < 0 or offset > DETAIL_MAX_OFFSET:
        raise HTTPException(status_code=422, detail="graph detail cursor is out of range")
    return offset


def local_detail_query(
    source_id: str,
    query: CanonicalQuery,
    condition_uri: str,
    intervention_uri: str,
    row_limit: int,
    retained_condition_uris: Sequence[str],
    retained_intervention_uris: Sequence[str],
    regions: RegionIndex | None = None,
) -> str:
    graph_uri = _source_graph(source_id)
    if source_id == "aea":
        detail = f"""
    OPTIONAL {{ ?study <{v1.UE}studyStatus> ?candidateStatus . }}
    OPTIONAL {{ ?study <{v1.AEA}studyStatus> ?candidateStatus . }}
    OPTIONAL {{ ?study <{v1.DCTERMS}date> ?candidateDate . }}
    OPTIONAL {{ ?study <{v1.AEA}country> ?candidateCountry . }}
"""
    else:
        detail = f"""
    OPTIONAL {{ ?study <{v1.ICTRP}dateOfRegistration> ?candidateDate . }}
    OPTIONAL {{ ?study <{v1.ICTRP}country> ?candidateCountry . }}
"""
    selection_selector = _stored_selector_body(
        source_id, query, regions, study_variable="?selectionStudy"
    )
    detail_selector = _stored_selector_body(source_id, query, regions)
    pairs = _stored_direct_pair_pattern(source_id, query, (condition_uri,))
    return f"""
PREFIX ue: <{v1.UE}>
PREFIX skos: <{v1.SKOS}>
SELECT ?rowKind ?rowOrder ?study
       (SAMPLE(?candidateStudyId) AS ?study_id)
       (SAMPLE(?candidateTitle) AS ?title)
       (SAMPLE(?candidateStatus) AS ?status)
       (SAMPLE(?candidateDate) AS ?date)
       (SAMPLE(?candidateCountry) AS ?country)
       (GROUP_CONCAT(DISTINCT STR(?supportStudy); separator="{GROUP_SEPARATOR}") AS ?selectedStudyUris)
WHERE {{
  {{
    {{
      SELECT DISTINCT ?selectionStudy WHERE {{
{selection_selector}
      }}
      ORDER BY STR(?selectionStudy)
      LIMIT {UNIQUE_STUDY_LIMIT + 1}
    }}
    BIND("{_STORED_SELECTION_ROW}" AS ?rowKind)
    BIND(0 AS ?rowOrder)
    BIND(?selectionStudy AS ?supportStudy)
  }}
  UNION
  {{
    {{
      SELECT DISTINCT ?study WHERE {{
{detail_selector}
      }}
      ORDER BY STR(?study)
      LIMIT {UNIQUE_STUDY_LIMIT}
    }}
    BIND("{_STORED_DETAIL_ROW}" AS ?rowKind)
    BIND(1 AS ?rowOrder)
    BIND(?study AS ?supportStudy)
    VALUES ?intervention {{ {v1.sparql_iri(intervention_uri)} }}
{pairs}
    GRAPH <{graph_uri}> {{
      OPTIONAL {{ ?study <{v1.DCTERMS}identifier> ?candidateStudyId . }}
      OPTIONAL {{ ?study <{v1.DCTERMS}title> ?candidateTitle . }}
{detail}
    }}
  }}
}}
GROUP BY ?rowKind ?rowOrder ?study
ORDER BY ?rowOrder ?study_id ?study
LIMIT {row_limit + 1}
"""


async def _local_detail_rows(
    source_id: str,
    query: CanonicalQuery,
    condition_uri: str,
    intervention_uri: str,
    row_limit: int,
    retained_condition_uris: Sequence[str],
    retained_intervention_uris: Sequence[str],
    regions: RegionIndex | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    budget = SourceBudget(max_requests=3, seconds=5.0)
    _selected_studies, support, source_limit_reached = (
        await _load_stored_support(
            source_id,
            query,
            retained_condition_uris,
            retained_intervention_uris,
            regions,
            budget=budget,
        )
    )
    edge_studies = support.get((condition_uri, intervention_uri), ())
    if edge_studies:
        async with _bounded_slot(v1._LOCAL_QUERY_SEMAPHORE, budget):
            detail_rows = await _sparql_select(
                stored_study_detail_query(source_id, edge_studies),
                budget,
            )
    else:
        detail_rows = []
    retained_set = set(edge_studies)
    results_by_id: dict[str, dict[str, Any]] = {}
    for row in detail_rows:
        if str(row.get("study") or "") not in retained_set:
            continue
        study_id = str(row.get("study_id") or row.get("study") or "").rsplit("_", 1)[-1]
        url = (
            v1._aea_external_url(study_id, str(row.get("study") or ""))
            if source_id == "aea"
            else v1._who_ictrp_external_url(study_id)
        )
        if study_id and study_id not in results_by_id:
            results_by_id[study_id] = {
                _DETAIL_SUPPORT_ID: str(row.get("study") or ""),
                "source": SOURCE_LABELS[source_id],
                "study_id": study_id,
                "title": row.get("title"),
                "url": url,
                "intervention": None,
                "intervention_concept": None,
                "intervention_concept_uri": intervention_uri,
                "state_concept": None,
                "state_concept_uri": condition_uri,
                "country": row.get("country"),
                "status": row.get("status"),
                "year": v1.extract_study_year(row.get("date")),
                "outcomes": [],
            }
    results = list(results_by_id.values())
    return results, {
        "status": "included",
        "coverage": "full",
        "returned_unique_studies": len(results),
        "truncated": source_limit_reached,
        "approximate": source_limit_reached,
        "reason": "source_limit" if source_limit_reached else None,
        "source_limit_reached": source_limit_reached,
        "source_limit_omitted_lower_bound": 1 if source_limit_reached else 0,
    }


async def execute_edge_details(
    query: CanonicalQuery,
    condition_uri: str,
    intervention_uri: str,
    offset: int,
    limit: int,
    *,
    expected_source_counts: Mapping[str, int],
    expected_source_membership_digests: Mapping[str, str],
    cursor_context: str,
) -> dict[str, Any]:
    if limit < 1 or limit > DETAIL_MAX_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=f"graph detail limit must be between 1 and {DETAIL_MAX_LIMIT}",
        )
    positive_source_counts = _positive_edge_source_counts(expected_source_counts)
    expected_membership_digests = _edge_source_membership_digests(
        expected_source_membership_digests,
        positive_source_counts,
    )
    active_source_ids = tuple(
        source_id for source_id in SOURCE_IDS if source_id in positive_source_counts
    )
    condition_taxonomy, intervention_taxonomy = await asyncio.gather(
        asyncio.to_thread(state_forest, _graph_state_roots(query)),
        asyncio.to_thread(intervention_forest, query.values["intervention"]),
    )
    retained_condition_uris = [
        node["id"] for node in condition_taxonomy["nodes"]
    ]
    retained_intervention_uris = [
        node["id"] for node in intervention_taxonomy["nodes"]
    ]
    if _graph_state_roots(query) and condition_uri not in {
        node["id"] for node in condition_taxonomy["nodes"]
    }:
        raise HTTPException(status_code=404, detail="stale or invalid graph evidence edge")
    if not _graph_state_roots(query) and not any(
        _state_graph().triples((URIRef(condition_uri), None, None))
    ):
        raise HTTPException(status_code=404, detail="stale or invalid graph evidence edge")
    if query.values["intervention"] and intervention_uri not in {
        node["id"] for node in intervention_taxonomy["nodes"]
    }:
        raise HTTPException(status_code=404, detail="stale or invalid graph evidence edge")
    if not query.values["intervention"] and not any(
        _intervention_graph().triples((URIRef(intervention_uri), None, None))
    ):
        raise HTTPException(status_code=404, detail="stale or invalid graph evidence edge")
    row_limit = min(DETAIL_MAX_OFFSET + DETAIL_MAX_LIMIT + 1, offset + limit + 1)
    live_source_ids = {
        source_id for source_id in active_source_ids if source_id in {"ctgov", "isrctn"}
    }
    regions = (
        await region_index_for_query(query)
        if live_source_ids or query.values["region"]
        else None
    )
    adapters = default_adapters() if live_source_ids else {}

    async def live_details(source_id: str):
        assert regions is not None
        rows, meta = await execute_graph_source(
            query, adapters[source_id], regions
        )
        filtered = []
        for row in rows:
            coordinates = _live_edge_coordinates(
                row,
                query,
                {node["id"] for node in condition_taxonomy["nodes"]},
                {node["id"] for node in intervention_taxonomy["nodes"]},
            )
            if (condition_uri, intervention_uri) not in coordinates:
                continue
            filtered.append(dict(row))
        filtered_meta = dict(meta)
        filtered_meta["returned_unique_studies"] = len(
            {study_key(row, source_id) for row in filtered}
        )
        return filtered, filtered_meta

    tasks: dict[str, asyncio.Task] = {}
    for source_id in active_source_ids:
        if source_id in {"aea", "who-ictrp"}:
            effective_query = query
            capability_partial = False
            if regions is not None:
                effective_query, capability_partial = _effective_stored_query(
                    query, regions
                )
            if effective_query is None:
                async def excluded_details():
                    return [], {
                        **_failed_source_meta(
                            "unsupported_admin_level", status="excluded"
                        ),
                        "coverage": "country_only",
                        "truncated": False,
                        "approximate": False,
                    }

                tasks[source_id] = asyncio.create_task(excluded_details())
            else:
                async def stored_details(
                    selected_source: str = source_id,
                    selected_query: CanonicalQuery = effective_query,
                    partial: bool = capability_partial,
                ):
                    rows, meta = await _local_detail_rows(
                        selected_source,
                        selected_query,
                        condition_uri,
                        intervention_uri,
                        row_limit,
                        retained_condition_uris,
                        retained_intervention_uris,
                        regions,
                    )
                    if partial:
                        meta.update(
                            {
                                "coverage": "country_only",
                                "truncated": True,
                                "approximate": True,
                                "reason": "unsupported_admin_level",
                            }
                        )
                    return rows, meta

                tasks[source_id] = asyncio.create_task(stored_details())
        else:
            tasks[source_id] = asyncio.create_task(live_details(source_id))
    _done, pending = await asyncio.wait(
        tasks.values(), timeout=GRAPH_DEADLINE_SECONDS
    )
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    population_membership_ids: dict[str, set[str]] = defaultdict(set)
    sources: dict[str, Mapping[str, Any]] = {}
    for source_id, task in tasks.items():
        if task in pending:
            rows: Sequence[Mapping[str, Any]] = []
            meta = _failed_source_meta(
                "budget_exhausted", status="included"
            )
        else:
            try:
                rows, meta = task.result()
            except (
                SourceBudgetExhausted,
                asyncio.TimeoutError,
                httpx.TimeoutException,
            ):
                rows = []
                meta = _failed_source_meta(
                    "budget_exhausted", status="included"
                )
            except Exception:
                logger.warning("graph-v2 detail %s failed", source_id, exc_info=True)
                rows = []
                meta = _failed_source_meta("upstream_error")
        sources[source_id] = meta
        for row in rows:
            key = study_key(row, source_id)
            if key[1]:
                support_id = str(row.get(_DETAIL_SUPPORT_ID) or key[1]).strip()
                if support_id:
                    population_membership_ids[source_id].add(support_id)
                public_row = dict(row)
                public_row.pop(_DETAIL_SUPPORT_ID, None)
                merged[key] = public_row

    ordered = [merged[key] for key in sorted(merged)]
    page = ordered[offset : offset + limit]
    has_more = len(ordered) > offset + limit
    next_offset = offset + limit
    pagination_ceiling_reached = has_more and next_offset > DETAIL_MAX_OFFSET
    next_cursor = (
        _encode_cursor(next_offset, cursor_context)
        if has_more and not pagination_ceiling_reached
        else None
    )
    population_source_counts = {
        source_id: sum(1 for key in merged if key[0] == source_id)
        for source_id in active_source_ids
    }
    source_counts_reconciled = _source_counts_reconciled(
        positive_source_counts,
        population_source_counts,
        next_cursor=next_cursor,
    )
    population_membership_digests = {
        source_id: _study_membership_digest(
            source_id,
            sorted(population_membership_ids[source_id]),
        )
        for source_id in active_source_ids
    }
    source_membership_reconciled = (
        expected_membership_digests == population_membership_digests
    )
    if source_counts_reconciled and not source_membership_reconciled:
        raise HTTPException(
            status_code=409,
            detail="graph edge evidence changed; refresh the graph",
        )
    detail_truncated = pagination_ceiling_reached or any(
        meta.get("truncated") for meta in sources.values()
    )
    detail_approximate = any(meta.get("approximate") for meta in sources.values())
    if not source_counts_reconciled or not source_membership_reconciled:
        detail_truncated = True
        detail_approximate = True
    sources_sound = all(
        meta.get("status") in {"included", "excluded"}
        and meta.get("reason")
        in {
            None,
            "budget_exhausted",
            "source_limit",
            "unsupported_admin_level",
        }
        for meta in sources.values()
    )
    # Every active source population is materialized before global paging, so
    # a cursor cannot make a mismatched population sound: missing rows can sort
    # ahead of this page and change every subsequent cursor.
    detail_sound = (
        sources_sound
        and source_counts_reconciled
        and source_membership_reconciled
    )
    return {
        "results": page,
        "meta": {
            "api_version": "query-v2",
            "schemaVersion": SCHEMA_VERSION,
            "edgeId": stable_evidence_edge_id(condition_uri, intervention_uri),
            "limit": limit,
            "returned": len(page),
            "nextCursor": next_cursor,
            "source_counts": dict(sorted(positive_source_counts.items())),
            "detail_population_source_counts": dict(
                sorted(population_source_counts.items())
            ),
            "source_counts_reconciled": source_counts_reconciled,
            "source_membership_reconciled": source_membership_reconciled,
            "truncated": detail_truncated,
            "approximate": detail_approximate,
            "sound": detail_sound,
            "sources": {source_id: dict(meta) for source_id, meta in sources.items()},
        },
    }


def _source_counts_reconciled(
    expected: Mapping[str, int],
    population: Mapping[str, int],
    *,
    next_cursor: str | None,
) -> bool:
    """Require exact source/count equality independently of page position."""
    normalize = lambda values: {
        str(source): int(count)
        for source, count in values.items()
        if int(count) > 0
    }
    return normalize(expected) == normalize(population)


@router.get("/graph/v2/edges/{edge_id}/studies")
async def graph_v2_edge_studies(
    edge_id: str,
    request: Request,
    edge_state: str | None = Query(default=None),
    edge_condition: str | None = Query(default=None),
    edge_intervention: str = Query(...),
    state: list[str] = Query(default=[]),
    condition: list[str] = Query(default=[]),
    intervention: list[str] = Query(default=[]),
    outcome: list[str] = Query(default=[]),
    region: list[str] = Query(default=[]),
    state_logic: Literal["or", "and"] = "or",
    condition_logic: Literal["or", "and"] = "or",
    intervention_logic: Literal["or", "and"] = "or",
    outcome_logic: Literal["or", "and"] = "or",
    region_logic: Literal["or", "and"] = "or",
    edge_token: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=DETAIL_DEFAULT_LIMIT, ge=1, le=DETAIL_MAX_LIMIT),
) -> dict[str, Any]:
    if edge_state and edge_condition and edge_state != edge_condition:
        raise HTTPException(
            status_code=422,
            detail="edge_state conflicts with deprecated edge_condition",
        )
    edge_state_uri = edge_state or edge_condition
    if not edge_state_uri:
        raise HTTPException(status_code=422, detail="edge_state is required")
    query = _graph_query(
        state=state,
        condition=condition,
        intervention=intervention,
        outcome=outcome,
        region=region,
        condition_logic=condition_logic,
        intervention_logic=intervention_logic,
        outcome_logic=outcome_logic,
        region_logic=region_logic,
        state_logic=state_logic,
    )
    expected = stable_evidence_edge_id(edge_state_uri, edge_intervention)
    if edge_id != expected:
        raise HTTPException(status_code=404, detail="stale or invalid graph evidence edge")
    taxonomy_version, dataset_version, bypass_reason = graph_versions()
    if edge_token and not _edge_detail_tokens_enabled(
        taxonomy_version,
        dataset_version,
        bypass_reason,
    ):
        raise HTTPException(
            status_code=422,
            detail="graph edge detail tokens are unavailable",
        )
    token_manifest = (
        _decode_edge_detail_token(
            edge_token,
            query=query,
            edge_id=edge_id,
            condition_uri=edge_state_uri,
            intervention_uri=edge_intervention,
            taxonomy_version=taxonomy_version,
            dataset_version=dataset_version,
        )
        if edge_token
        else None
    )
    identity = graph_cache_identity(
        query,
        taxonomy_version=taxonomy_version,
        dataset_version=dataset_version,
    )

    async def validated_details() -> dict[str, Any]:
        if token_manifest is not None:
            source_counts, source_membership_digests = token_manifest
            cache_status = "edge-token"
        else:
            aggregate, cache_status = await graph_v2_cache.get_or_execute_with_status(
                identity,
                lambda: execute_graph_v2(query),
                enabled=bypass_reason is None,
            )
            edge = next(
                (
                    candidate
                    for candidate in aggregate.get("edges", ())
                    if candidate.get("kind") == "evidence"
                    and candidate.get("id") == edge_id
                    and candidate.get("target") == edge_state_uri
                    and candidate.get("source") == edge_intervention
                ),
                None,
            )
            if edge is None:
                raise HTTPException(
                    status_code=404,
                    detail="stale or invalid graph evidence edge",
                )
            source_counts = _positive_edge_source_counts(
                edge.get("source_counts") or {}
            )
            source_membership_digests = _edge_source_membership_digests(
                edge.get(_EDGE_MEMBERSHIP_DIGESTS) or {},
                source_counts,
            )
        cursor_context = _detail_cursor_context(
            query,
            edge_id,
            taxonomy_version,
            dataset_version,
            source_membership_digests,
        )
        offset = _decode_cursor(cursor, cursor_context)
        details_identity = detail_cache_identity(
            query,
            edge_id=edge_id,
            condition_uri=edge_state_uri,
            intervention_uri=edge_intervention,
            expected_source_counts=source_counts,
            expected_source_membership_digests=source_membership_digests,
            offset=offset,
            limit=limit,
            taxonomy_version=taxonomy_version,
            dataset_version=dataset_version,
        )
        details_payload, details_cache_status = (
            await graph_v2_detail_cache.get_or_execute_with_status(
                details_identity,
                lambda: execute_edge_details(
                    query,
                    edge_state_uri,
                    edge_intervention,
                    offset,
                    limit,
                    expected_source_counts=source_counts,
                    expected_source_membership_digests=source_membership_digests,
                    cursor_context=cursor_context,
                ),
                enabled=bypass_reason is None,
            )
        )
        details = dict(details_payload)
        details["meta"] = dict(details_payload.get("meta") or {})
        details["meta"]["aggregateCacheStatus"] = cache_status
        details["meta"]["detailCacheStatus"] = details_cache_status
        details["meta"]["cache_bypass_reason"] = bypass_reason
        return details

    try:
        return await await_query_or_disconnect(
            validated_details(),
            request,
        )
    except ClientDisconnectedError as exc:
        raise HTTPException(status_code=499, detail="Client disconnected") from exc


@router.get("/graph/v2/cache/stats")
def graph_v2_cache_stats() -> dict[str, Any]:
    return {
        **graph_v2_cache.stats(),
        "detail_cache": graph_v2_detail_cache.stats(),
    }
