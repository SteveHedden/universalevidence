"""Derived graph API routes."""

from __future__ import annotations

import asyncio
from collections import defaultdict
import hashlib
import logging
from pathlib import Path
import re
import sys
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
import requests

from api.routes.query import _cached_query_axes, build_axes_dict

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from query_interventions import (  # noqa: E402
    ClientDisconnectedError,
    LocalQueryBusyError,
    async_query_axes,
    await_query_or_disconnect,
    sparql_select,
    TAXONOMY_GRAPH_URI,
)

logger = logging.getLogger(__name__)

router = APIRouter()

DESIGN_WEIGHTS = {
    "rct": 2.0,
    "quasi_experimental": 1.5,
    "observational": 1.0,
    "unknown": 1.0,
    "systematic_review": 4.0,
}
SOURCE_DESIGN_DEFAULTS = {
    "aea": "rct",
    "aea rct registry": "rct",
    "ct.gov": "unknown",
    "clinicaltrials.gov": "unknown",
    "isrctn": "unknown",
    "who ictrp": "unknown",
}
MAX_NODE_LABEL_LENGTH = 72

UE_NS = "https://universalevidence.com/ontology/"


def local_name(value: str) -> str:
    return value.rstrip("/#").rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def display_label(value: str) -> str:
    if value.startswith("http://") or value.startswith("https://"):
        text = local_name(value)
        return truncate_label(re.sub(r"(?<!^)(?=[A-Z])", " ", text).replace("_", " "))
    return truncate_label(value)


def truncate_label(value: str, limit: int = MAX_NODE_LABEL_LENGTH) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rsplit(" ", 1)[0].rstrip(".,;:") + "..."


def stable_text_node_id(text: str) -> str:
    digest = hashlib.sha1(text.strip().casefold().encode("utf-8")).hexdigest()[:12]
    return f"urn:ue:intervention-text:{digest}"


def load_taxonomy_labels() -> dict[str, str]:
    """Return {uri: label} for all State and Intervention concepts in the taxonomy graph."""
    query = f"""
PREFIX ue: <{UE_NS}>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
SELECT ?uri (COALESCE(?rdfsLabel, ?prefLabel) AS ?label) WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    {{ ?uri rdf:type ue:State }} UNION {{ ?uri rdf:type ue:Intervention }}
    OPTIONAL {{ ?uri rdfs:label ?rdfsLabel . }}
    OPTIONAL {{ ?uri skos:prefLabel ?prefLabel . }}
    FILTER(BOUND(?rdfsLabel) || BOUND(?prefLabel))
  }}
}}
"""
    rows = sparql_select(query)
    return {row["uri"]: row["label"] for row in rows if row.get("uri") and row.get("label")}


def load_intervention_taxonomy() -> list[dict]:
    """Return intervention terms with their keyword values from the taxonomy graph."""
    query = f"""
PREFIX ue: <{UE_NS}>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?term ?label ?keyword WHERE {{
  GRAPH <{TAXONOMY_GRAPH_URI}> {{
    ?term a ue:Intervention .
    OPTIONAL {{ ?term rdfs:label ?label . }}
    OPTIONAL {{ ?term skos:prefLabel ?label . }}
    OPTIONAL {{ ?term ue:aeaKeyword ?keyword . }}
  }}
}}
ORDER BY ?term ?keyword
"""
    rows = sparql_select(query)
    terms: dict[str, dict] = {}
    for row in rows:
        uri = row.get("term", "")
        if not uri:
            continue
        label = row.get("label") or ""
        if uri not in terms:
            terms[uri] = {"uri": uri, "label": label or uri, "keywords": []}
            if label:
                terms[uri]["keywords"].append(label.strip().lower())
        keyword = row.get("keyword")
        if keyword and keyword.strip().lower() not in terms[uri]["keywords"]:
            terms[uri]["keywords"].append(keyword.strip().lower())
    return list(terms.values())


def compile_taxonomy(taxonomy: list[dict]) -> list[dict]:
    """Pre-compile keyword regexes once so normalize_intervention avoids per-call compilation."""
    compiled = []
    for term in taxonomy:
        patterns = []
        for kw in term.get("keywords", []):
            try:
                patterns.append((len(kw), re.compile(r"(?<!\w)" + re.escape(kw) + r"(?!\w)")))
            except re.error:
                pass
        compiled.append({"uri": term["uri"], "label": term["label"], "patterns": patterns})
    return compiled


def normalize_intervention(text: str, taxonomy: list[dict]) -> tuple[str, str]:
    """Map free-text intervention to a taxonomy term URI and label via keyword matching."""
    lower = text.strip().lower()
    best_uri: Optional[str] = None
    best_label: Optional[str] = None
    best_length = 0

    for term in taxonomy:
        patterns = term.get("patterns")
        if patterns is not None:
            for kw_len, pattern in patterns:
                if kw_len <= best_length:
                    continue
                if pattern.search(lower):
                    best_uri = term["uri"]
                    best_label = term["label"]
                    best_length = kw_len
        else:
            for keyword in term.get("keywords", []):
                if len(keyword) <= best_length:
                    continue
                if re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", lower):
                    best_uri = term["uri"]
                    best_label = term["label"]
                    best_length = len(keyword)

    if best_uri:
        return best_uri, best_label
    return stable_text_node_id(text), truncate_label(text)


def normalize_design(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = value.strip().casefold().replace("-", "_").replace(" ", "_")
    if text in DESIGN_WEIGHTS:
        return text
    if "random" in text or text == "rct":
        return "rct"
    if "quasi" in text:
        return "quasi_experimental"
    if "observ" in text:
        return "observational"
    if "systematic" in text:
        return "systematic_review"
    return None


def infer_study_design(result: dict) -> str:
    explicit = normalize_design(result.get("study_design"))
    if explicit:
        return explicit
    source = str(result.get("source") or "").strip().casefold()
    return SOURCE_DESIGN_DEFAULTS.get(source, "unknown")


def design_weight(study_design: str) -> float:
    return DESIGN_WEIGHTS.get(study_design, DESIGN_WEIGHTS["unknown"])


def confidence_label(weighted_score: float) -> str:
    if weighted_score >= 8:
        return "high"
    if weighted_score >= 3:
        return "medium"
    return "low"


def parse_source_filter(source: Optional[str]) -> set[str]:
    if not source:
        return set()
    return {part.strip().casefold() for part in source.split(",") if part.strip()}


def source_allowed(result: dict, filters: set[str]) -> bool:
    if not filters:
        return True
    source = str(result.get("source") or "").casefold()
    return source in filters


def study_payload(result: dict, study_design: str) -> dict:
    return {
        "source": result.get("source"),
        "study_id": result.get("study_id"),
        "title": result.get("title"),
        "url": result.get("url"),
        "study_design": study_design,
    }


def build_graph_payload(
    results: list[dict],
    condition: str,
    source: Optional[str] = None,
    taxonomy: Optional[list[dict]] = None,
    labels: Optional[dict[str, str]] = None,
    min_weight: int = 2,
    max_nodes: int = 30,
) -> dict:
    """Aggregate flat study results into a Cytoscape-compatible star graph.

    Results that supply a pre-resolved intervention URI are used directly.
    Results with only free text fall back to keyword matching against taxonomy;
    if no match is found the text label is kept as-is (InterventionText node)
    so free-text sources are never silently dropped.
    """
    compiled_taxonomy = compile_taxonomy(taxonomy) if taxonomy else []

    source_filters = parse_source_filter(source)
    condition_id = condition if condition.startswith(("http://", "https://")) else f"urn:ue:condition-label:{stable_text_node_id(condition).rsplit(':', 1)[-1]}"
    condition_label = (labels or {}).get(condition_id) or display_label(condition)
    condition_node = {
        "id": condition_id,
        "label": condition_label,
        "class": "Condition",
        "studyCount": 0,
    }
    nodes: dict[str, dict] = {condition_id: condition_node}
    edges: dict[tuple[str, str], dict] = {}
    omitted_missing_intervention = 0
    omitted_unmatched_intervention = 0

    seen_study_interv: set[tuple[str, str]] = set()

    for result in results:
        if not source_allowed(result, source_filters):
            continue

        study_id = result.get("study_id") or ""

        # Build the list of (intervention_id, intervention_label) pairs for this study.
        # Prefer pre-stamped all_intervention_concepts when available; fall back to single URI or text.
        all_concepts: list[tuple[str, str]] = result.get("all_intervention_concepts") or []
        if all_concepts:
            intervention_pairs = [
                (uri, (labels or {}).get(uri) or label)
                for uri, label in all_concepts
            ]
        else:
            intervention_uri_field = result.get("intervention_uri") or result.get("intervention_concept_uri")
            if intervention_uri_field:
                iLabel = (labels or {}).get(intervention_uri_field) or display_label(intervention_uri_field)
                intervention_pairs = [(intervention_uri_field, iLabel)]
            else:
                intervention = (result.get("intervention") or "").strip()
                if not intervention:
                    omitted_missing_intervention += 1
                    continue
                if compiled_taxonomy:
                    iid, ilabel = normalize_intervention(intervention, compiled_taxonomy)
                else:
                    iid = stable_text_node_id(intervention)
                    ilabel = truncate_label(intervention)
                if iid.startswith("urn:"):
                    omitted_unmatched_intervention += 1
                    continue
                intervention_pairs = [(iid, ilabel)]

        study_design = infer_study_design(result)
        weight = design_weight(study_design)

        for intervention_id, intervention_label in intervention_pairs:
            dedup_key = (study_id, intervention_id)
            if dedup_key in seen_study_interv:
                continue
            seen_study_interv.add(dedup_key)

            intervention_node = nodes.setdefault(intervention_id, {
                "id": intervention_id,
                "label": intervention_label,
                "class": "Intervention",
                "studyCount": 0,
            })

            edge = edges.setdefault(
                (intervention_id, condition_id),
                {
                    "source": intervention_id,
                    "target": condition_id,
                    "weight": 0,
                    "weighted_score": 0.0,
                    "confidence": "low",
                    "studyIds": [],
                    "studies": [],
                    "_study_design_counts": defaultdict(int),
                },
            )

            if study_id:
                edge["studyIds"].append(study_id)
            edge["studies"].append(study_payload(result, study_design))
            edge["weight"] += 1
            edge["weighted_score"] += weight
            edge["_study_design_counts"][study_design] += 1
            intervention_node["studyCount"] += 1
            condition_node["studyCount"] += 1

    all_edges = []
    for edge in edges.values():
        edge["weighted_score"] = round(edge["weighted_score"], 3)
        edge["confidence"] = confidence_label(edge["weighted_score"])
        edge["study_design_counts"] = dict(sorted(edge["_study_design_counts"].items()))
        del edge["_study_design_counts"]
        all_edges.append(edge)

    output_edges = sorted(
        [e for e in all_edges if e["weight"] >= min_weight],
        key=lambda e: -e["weight"],
    )[:max_nodes]
    if not output_edges:
        output_edges = sorted(all_edges, key=lambda e: -e["weight"])[:max_nodes]

    kept_interv_ids = {e["source"] for e in output_edges}
    nodes = {
        nid: n for nid, n in nodes.items()
        if nid == condition_id or nid in kept_interv_ids
    }

    return {
        "nodes": sorted(nodes.values(), key=lambda node: (node["class"], node["label"])),
        "edges": sorted(output_edges, key=lambda edge: (edge["source"], edge["target"])),
        "metadata": {
            "omittedMissingIntervention": omitted_missing_intervention,
            "omittedUnmatchedIntervention": omitted_unmatched_intervention,
        },
    }


@router.get("/graph")
async def graph(
    request: Request,
    condition: Optional[str] = Query(None),
    region: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
) -> dict:
    """Return a star graph payload for the given condition (and optionally region/country).

    Returns empty payload when no condition is provided. Search inputs are
    truncated to the 100 most-recent local results per source.
    """
    if not condition:
        return {"nodes": [], "edges": [], "metadata": {}}

    axes = build_axes_dict(condition=condition, region=region, country=country)

    try:
        results, labels, intervention_taxonomy = await await_query_or_disconnect(
            asyncio.gather(
                _cached_query_axes(axes, query_fn=async_query_axes),
                asyncio.to_thread(load_taxonomy_labels),
                asyncio.to_thread(load_intervention_taxonomy),
            ),
            request,
        )
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
        logger.exception("Unhandled graph failure")
        raise HTTPException(status_code=500, detail="Internal server error") from exc

    return build_graph_payload(
        results,
        axes["condition"],
        source=source,
        taxonomy=intervention_taxonomy,
        labels=labels,
    )
