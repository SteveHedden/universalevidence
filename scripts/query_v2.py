#!/usr/bin/env python3
"""Isolated query-v2 planner and source adapters.

This module deliberately does not participate in the legacy ``/query`` path.
It evaluates the complete Boolean request per source, limits *study IDs* before
presentation expansion, and retains source-specific coverage/failure metadata.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import product
import logging
import math
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

import httpx
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, SKOS

if "query_interventions" in sys.modules:
    # The API imports v1 first. Reuse that exact module so v1 and v2 share the
    # same concurrency semaphores and warmed crosswalk caches.
    v1 = sys.modules["query_interventions"]
else:
    try:  # direct invocation from scripts/ or route-prepared sys.path
        import query_interventions as v1
    except ModuleNotFoundError:  # package-style test import fallback
        from scripts import query_interventions as v1


try:
    from query_runtime import REQUEST_DEADLINE, bounded, observe, stage, timed_thread
except ModuleNotFoundError:
    from scripts.query_runtime import REQUEST_DEADLINE, bounded, observe, stage, timed_thread

logger = logging.getLogger(__name__)

AXES = ("state", "condition", "intervention", "outcome", "region")
SOURCE_IDS = ("ctgov", "aea", "isrctn", "who-ictrp")
SOURCE_LABELS = {
    "ctgov": "CT.gov",
    "aea": "AEA",
    "isrctn": "ISRCTN",
    "who-ictrp": "WHO ICTRP",
}
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

UNIQUE_STUDY_LIMIT = 100
MAX_REQUESTS_PER_SOURCE = v1._positive_int_env(
    "REGION_QUERY_MAX_REQUESTS_PER_SOURCE", 8
)
MAX_PAGES_PER_BRANCH = v1._positive_int_env(
    "REGION_QUERY_MAX_PAGES_PER_BRANCH", 3
)


def _positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        if not math.isfinite(value) or value <= 0:
            raise ValueError
        return value
    except ValueError:
        logger.warning("Invalid %s; using %s", name, default)
        return default


RESPONSE_TIMEOUT_SECONDS = _positive_float_env("QUERY_V2_RESPONSE_TIMEOUT_SECONDS", 10.0)

TIME_BUDGET_SECONDS = _positive_float_env("REGION_QUERY_TIME_BUDGET_SECONDS", 5.0)

CTGOV_V2_FIELDS = "|".join(
    (
        "NCTId",
        "BriefTitle",
        "OverallStatus",
        "StartDate",
        "InterventionName",
        "ConditionMeshId",
        "InterventionMeshId",
        "LocationFacility",
        "LocationCity",
        "LocationState",
        "LocationZip",
        "LocationCountry",
        "LocationGeoPoint",
        "PrimaryOutcomeMeasure",
        "SecondaryOutcomeMeasure",
        "OtherOutcomeMeasure",
    )
)

REPO_ROOT = Path(__file__).resolve().parents[1]
VOCABULARIES_DIR = REPO_ROOT / "vocabularies"
WORLD_URI = "https://universalevidence.com/vocab/regions/World"

UE = Namespace(v1.UE)
GN = Namespace("http://www.geonames.org/ontology#")

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
    "state_concept",
    "state_concept_uri",
    "country",
    "status",
    "year",
    "outcomes",
)


class SourceUnavailable(RuntimeError):
    """A source or required read-only asset is not currently available."""


class SourceBudgetExhausted(RuntimeError):
    """The source cannot start another upstream request inside its budget."""


@dataclass(frozen=True)
class ConceptResolution:
    label: str | None
    terms: tuple[str, ...]
    mesh_uris: tuple[str, ...]


_CONCEPT_CACHE: dict[str, ConceptResolution] = {}
_CONCEPT_INFLIGHT: dict[str, asyncio.Task[ConceptResolution]] = {}


@lru_cache(maxsize=2)
def _local_taxonomy_graph(filename: str) -> Graph:
    """Load a tracked taxonomy asset once for network-independent planning."""
    graph = Graph()
    graph.parse(VOCABULARIES_DIR / filename, format="turtle")
    return graph


def _resolve_local_concept(uri: str) -> ConceptResolution | None:
    """Resolve labels, direct search terms, and descendant MeSH locally.

    The API image contains the same tracked taxonomy TTL files loaded into
    Fuseki. Planning from those files prevents a slow taxonomy lookup from
    consuming every source's independent five-second execution budget.
    """
    if "/vocab/interventions/" in uri:
        filenames = ("interventions.ttl",)
    elif "/vocab/states/" in uri:
        filenames = ("states.ttl",)
    else:
        return None

    subject = URIRef(uri)
    for filename in filenames:
        path = VOCABULARIES_DIR / filename
        if not path.exists():
            continue
        graph = _local_taxonomy_graph(filename)
        if not any(graph.triples((subject, None, None))):
            continue

        label_value = next(graph.objects(subject, SKOS.prefLabel), None)
        if label_value is None:
            label_value = next(graph.objects(subject, RDFS.label), None)
        label = str(label_value) if label_value is not None else None

        terms: list[str] = []
        seen_terms: set[str] = set()

        def add_terms(concept: URIRef, predicates: Sequence[URIRef]) -> None:
            for predicate in predicates:
                for value in graph.objects(concept, predicate):
                    text = str(value).strip()
                    key = text.casefold()
                    if text and key not in seen_terms:
                        seen_terms.add(key)
                        terms.append(text)

        add_terms(subject, (SKOS.prefLabel, SKOS.altLabel, RDFS.label))
        direct_children = sorted(
            {URIRef(value) for value in graph.subjects(SKOS.broader, subject)},
            key=str,
        )
        for child in direct_children:
            add_terms(child, (SKOS.prefLabel, SKOS.altLabel))

        descendants: set[URIRef] = {subject}
        frontier = [subject]
        while frontier:
            parent = frontier.pop()
            for child_value in graph.subjects(SKOS.broader, parent):
                child = URIRef(child_value)
                if child not in descendants:
                    descendants.add(child)
                    frontier.append(child)

        meshes = sorted(
            {
                str(value)
                for concept in descendants
                for value in graph.objects(concept, SKOS.exactMatch)
                if str(value).startswith(v1.MESH_PREFIXES)
            }
        )
        return ConceptResolution(
            label=label,
            terms=tuple(terms),
            mesh_uris=tuple(meshes),
        )
    return None


async def _resolve_concept_uncached(uri: str) -> ConceptResolution:
    local = await timed_thread(_resolve_local_concept, uri)
    if local is not None:
        return local
    query = f"""
PREFIX skos: <{v1.SKOS}>
PREFIX rdfs: <{v1.RDFS}>
SELECT DISTINCT ?kind ?value WHERE {{
  GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{
    {{
      {v1.sparql_iri(uri)} skos:prefLabel|rdfs:label ?value .
      BIND("label" AS ?kind)
    }} UNION {{
      {v1.sparql_iri(uri)} skos:prefLabel|skos:altLabel|rdfs:label ?value .
      BIND("term" AS ?kind)
    }} UNION {{
      ?child skos:broader {v1.sparql_iri(uri)} ;
             skos:prefLabel|skos:altLabel ?value .
      BIND("term" AS ?kind)
    }} UNION {{
      {v1.sparql_iri(uri)} skos:narrowerTransitive? ?concept .
      ?concept skos:exactMatch ?value .
      FILTER(STRSTARTS(STR(?value), "http://id.nlm.nih.gov/mesh/") ||
             STRSTARTS(STR(?value), "https://id.nlm.nih.gov/mesh/"))
      BIND("mesh" AS ?kind)
    }}
  }}
}}
"""
    budget = SourceBudget(max_requests=1, seconds=TIME_BUDGET_SECONDS)
    async with _bounded_slot(v1._LOCAL_QUERY_SEMAPHORE, budget):
        rows = await _sparql_select(query, budget)
    label = next((row.get("value") for row in rows if row.get("kind") == "label"), None)
    terms: list[str] = []
    meshes: list[str] = []
    seen_terms: set[str] = set()
    for row in rows:
        value = str(row.get("value") or "").strip()
        kind = row.get("kind")
        if kind in {"label", "term"} and value and value.casefold() not in seen_terms:
            seen_terms.add(value.casefold())
            terms.append(value)
        elif kind == "mesh" and value and value not in meshes:
            meshes.append(value)
    return ConceptResolution(label=label, terms=tuple(terms), mesh_uris=tuple(meshes))


async def resolve_concept(uri: str) -> ConceptResolution:
    cached = _CONCEPT_CACHE.get(uri)
    if cached is not None:
        return cached
    task = _CONCEPT_INFLIGHT.get(uri)
    if task is None:
        task = asyncio.create_task(_resolve_concept_uncached(uri))
        _CONCEPT_INFLIGHT[uri] = task

        def retire(completed):
            if _CONCEPT_INFLIGHT.get(uri) is completed:
                _CONCEPT_INFLIGHT.pop(uri, None)
            if not completed.cancelled() and completed.exception() is None:
                _CONCEPT_CACHE[uri] = completed.result()

        task.add_done_callback(retire)
        observe(task)
    try:
        result = await asyncio.shield(task)
        _CONCEPT_CACHE[uri] = result
        return result
    finally:
        if task.done() and _CONCEPT_INFLIGHT.get(uri) is task:
            _CONCEPT_INFLIGHT.pop(uri, None)


@dataclass(frozen=True)
class CanonicalQuery:
    """Sorted, de-duplicated query input used for planning and cache identity."""

    values: Mapping[str, tuple[str, ...]]
    logic: Mapping[str, str]

    def identity(self) -> dict[str, Any]:
        return {
            "api_version": "query-v2",
            "values": {axis: list(self.values.get(axis, ())) for axis in AXES},
            "logic": {axis: self.logic[axis] for axis in AXES},
        }


def canonical_query(
    values: Mapping[str, Sequence[str]], logic: Mapping[str, str]
) -> CanonicalQuery:
    normalized_values = {
        axis: tuple(sorted({str(value).strip() for value in values.get(axis, ()) if str(value).strip()}))
        for axis in AXES
    }
    normalized_logic = {axis: str(logic.get(axis, "or")).casefold() for axis in AXES}
    invalid = [axis for axis, operator in normalized_logic.items() if operator not in {"or", "and"}]
    if invalid:
        raise ValueError(f"invalid Boolean logic for: {', '.join(invalid)}")
    if not any(normalized_values.values()):
        raise ValueError("at least one query axis is required")
    return CanonicalQuery(values=normalized_values, logic=normalized_logic)


@dataclass(frozen=True)
class ClausePlan:
    """One disjunct whose atomic branches must all match the same study."""

    specs: tuple[Mapping[str, str], ...]


def plan_query(query: CanonicalQuery) -> tuple[ClausePlan, ...]:
    """Convert per-axis Boolean logic to bounded DNF-ready atomic branches.

    OR axes choose one value per disjunct. AND axes choose one value per atomic
    branch within that disjunct; those study sets are intersected later. Every
    branch still contains one value from every populated axis, so cross-axis
    predicates reach the source before its study limit.
    """
    populated = [axis for axis in AXES if query.values.get(axis)]
    or_axes = [axis for axis in populated if query.logic[axis] == "or"]
    and_axes = [axis for axis in populated if query.logic[axis] == "and"]
    or_choices = product(*(query.values[axis] for axis in or_axes)) if or_axes else [()]
    clauses: list[ClausePlan] = []
    for or_choice in or_choices:
        fixed = dict(zip(or_axes, or_choice))
        and_choices = (
            product(*(query.values[axis] for axis in and_axes))
            if and_axes
            else [()]
        )
        specs = tuple(
            {**fixed, **dict(zip(and_axes, and_choice))}
            for and_choice in and_choices
        )
        clauses.append(ClausePlan(specs=specs))
    return tuple(clauses)


def canonical_source(value: object) -> str:
    text = str(value or "").strip().casefold()
    if "/sources/" in text:
        text = text.rstrip("/").rsplit("/", 1)[-1]
    return SOURCE_ALIASES.get(text, text)


def study_key(row: Mapping[str, Any], source_id: str | None = None) -> tuple[str, str]:
    source = source_id or canonical_source(row.get("source"))
    return source, str(row.get("study_id") or "").strip()


def rows_by_study(
    rows: Sequence[Mapping[str, Any]], source_id: str
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for raw in rows:
        row = dict(raw)
        key = study_key(row, source_id)
        if not key[1]:
            continue
        grouped.setdefault(key, []).append(row)
    return grouped


def _row_state_coordinate_uris(row: Mapping[str, Any]) -> tuple[str, ...]:
    coordinates: list[str] = []
    for value in (
        row.get("state_concept_uri"),
        row.get("condition_concept_uri"),
    ):
        uri = str(value or "")
        if uri and uri not in coordinates:
            coordinates.append(uri)
    for outcome in row.get("outcomes") or ():
        if not isinstance(outcome, Mapping):
            continue
        uri = str(outcome.get("state_concept_uri") or "")
        if uri and uri not in coordinates:
            coordinates.append(uri)
    return tuple(coordinates)


def _qualified_graph_state_and_rows(
    rows: Sequence[Mapping[str, Any]],
    spec: Mapping[str, str],
    query: CanonicalQuery,
) -> list[Mapping[str, Any]]:
    """Require direct support in every branch of a multi-State Graph AND."""
    selected_root = spec.get("state")
    if not (
        _GRAPH_ATTRIBUTION.get()
        and selected_root
        and query.logic["state"] == "and"
        and len(query.values["state"]) > 1
    ):
        return list(rows)

    coordinates_by_row = [
        (row, _row_state_coordinate_uris(row))
        for row in rows
    ]
    in_scope_uris = {
        uri
        for uri, _label in _direct_taxonomy_matches(
            [
                (uri, uri)
                for _row, coordinates in coordinates_by_row
                for uri in coordinates
            ],
            selected_root,
            "states.ttl",
        )
    }
    return [
        row
        for row, coordinates in coordinates_by_row
        if any(uri in in_scope_uris for uri in coordinates)
    ]


def _outcome_identity(value: object) -> str:
    if isinstance(value, dict):
        return "|".join(str(value.get(key) or "") for key in (
            "type", "measure", "state_concept_uri", "description"
        ))
    return str(value)


_GRAPH_ATTRIBUTION: ContextVar[bool] = ContextVar(
    "query_v2_graph_attribution", default=False
)


def dedupe_presentation(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe Search rows, preserving condition coordinates only for Graph."""
    if not _GRAPH_ATTRIBUTION.get():
        selected: dict[tuple[str, str, str], dict[str, Any]] = {}
        for raw in rows:
            source = canonical_source(raw.get("source"))
            study_id = str(raw.get("study_id") or "").strip()
            if not source or not study_id:
                continue
            intervention_key = str(
                raw.get("intervention_concept_uri")
                or f"raw:{raw.get('intervention') or ''}"
            )
            key = (source, study_id, intervention_key)
            public = {name: raw.get(name) for name in PUBLIC_RESULT_KEYS}
            public["outcomes"] = list(raw.get("outcomes") or [])
            existing = selected.get(key)
            if existing is None:
                selected[key] = public
                continue
            seen_outcomes = {
                _outcome_identity(value) for value in existing["outcomes"]
            }
            for outcome in public["outcomes"]:
                identity = _outcome_identity(outcome)
                if identity not in seen_outcomes:
                    existing["outcomes"].append(outcome)
                    seen_outcomes.add(identity)
            if not existing.get("condition_concept_uri") and public.get(
                "condition_concept_uri"
            ):
                existing["condition_concept_uri"] = public[
                    "condition_concept_uri"
                ]
                existing["condition_concept"] = public.get("condition_concept")
        return sorted(
            selected.values(),
            key=lambda row: (
                -(
                    int(row.get("year") or 0)
                    if str(row.get("year") or "").isdigit()
                    else 0
                ),
                canonical_source(row.get("source")),
                str(row.get("study_id")),
            ),
        )

    selected: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for raw in rows:
        source = canonical_source(raw.get("source"))
        study_id = str(raw.get("study_id") or "").strip()
        if not source or not study_id:
            continue
        intervention_key = str(
            raw.get("intervention_concept_uri")
            or f"raw:{raw.get('intervention') or ''}"
        )
        state_key = str(
            raw.get("state_concept_uri")
            or raw.get("condition_concept_uri")
            or ""
        )
        key = (source, study_id, state_key, intervention_key)
        public = {name: raw.get(name) for name in PUBLIC_RESULT_KEYS}
        public["outcomes"] = list(raw.get("outcomes") or [])
        existing = selected.get(key)
        if existing is None:
            selected[key] = public
            continue
        seen_outcomes = {_outcome_identity(value) for value in existing["outcomes"]}
        for outcome in public["outcomes"]:
            identity = _outcome_identity(outcome)
            if identity not in seen_outcomes:
                existing["outcomes"].append(outcome)
                seen_outcomes.add(identity)
        if not existing.get("condition_concept_uri") and public.get("condition_concept_uri"):
            existing["condition_concept_uri"] = public["condition_concept_uri"]
            existing["condition_concept"] = public.get("condition_concept")
    attributed = {
        (source, study_id, intervention_key)
        for source, study_id, condition_key, intervention_key in selected
        if condition_key
    }
    retained = [
        value
        for (source, study_id, condition_key, intervention_key), value in selected.items()
        if condition_key
        or (source, study_id, intervention_key) not in attributed
    ]
    return sorted(
        retained,
        key=lambda row: (-(int(row.get("year") or 0) if str(row.get("year") or "").isdigit() else 0), canonical_source(row.get("source")), str(row.get("study_id"))),
    )


@dataclass
class SourceBudget:
    max_requests: int = MAX_REQUESTS_PER_SOURCE
    max_pages_per_branch: int = MAX_PAGES_PER_BRANCH
    seconds: float = TIME_BUDGET_SECONDS
    clock: Callable[[], float] = time.monotonic
    started: float = field(init=False)
    requests: int = 0

    def __post_init__(self) -> None:
        self.started = self.clock()

    @property
    def remaining(self) -> float:
        return max(0.0, self.seconds - (self.clock() - self.started))

    def claim_request(self) -> float:
        if self.requests >= self.max_requests or self.remaining <= 0:
            raise SourceBudgetExhausted("per-source request/time budget exhausted")
        self.requests += 1
        return self.remaining


@asynccontextmanager
async def _bounded_slot(semaphore: asyncio.Semaphore, budget: SourceBudget):
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=budget.remaining)
    except asyncio.TimeoutError as exc:
        raise SourceBudgetExhausted("source concurrency capacity unavailable") from exc
    try:
        yield
    finally:
        semaphore.release()


@dataclass
class BranchResult:
    rows: list[dict[str, Any]]
    truncated: bool = False
    approximate: bool = False
    budget_exhausted: bool = False


class SourceAdapter(Protocol):
    source_id: str

    async def execute(
        self,
        spec: Mapping[str, str],
        budget: SourceBudget,
        regions: "RegionIndex",
    ) -> BranchResult: ...


@dataclass(frozen=True)
class RegionDescriptor:
    uri: str
    kind: str
    label: str
    country_uris: tuple[str, ...]
    country_isos: tuple[str, ...]
    country_names: tuple[str, ...]


class RegionIndex:
    """Read-only hierarchy assembled only from approved checked-in assets."""

    def __init__(self, graph: Graph):
        self.graph = graph
        self._country_label_isos: dict[str, str] | None = None

    @classmethod
    def load_default(cls) -> "RegionIndex":
        required = VOCABULARIES_DIR / "regions.ttl"
        optional = (
            VOCABULARIES_DIR / "mirrors" / "geonames-countries-mirror.ttl",
            VOCABULARIES_DIR / "mirrors" / "geonames-adm1-mirror.ttl",
        )
        if not required.exists():
            raise SourceUnavailable(
                f"region asset unavailable: {required.name}"
            )
        graph = Graph()
        graph.parse(required, format="turtle")
        for path in optional:
            if not path.exists():
                continue
            graph.parse(path, format="turtle")
        return cls(graph)

    def _label(self, node: URIRef) -> str:
        return str(
            self.graph.value(node, SKOS.prefLabel)
            or self.graph.value(node, RDFS.label)
            or str(node).rstrip("/").rsplit("/", 1)[-1]
        )

    def kind(self, uri: str) -> str:
        if uri.rstrip("/") == WORLD_URI.rstrip("/"):
            return "world"
        node = URIRef(uri)
        if (node, GN.featureCode, GN["A.ADM1"]) in self.graph:
            return "adm1"
        if self.graph.value(node, UE.iso3166Alpha2) or (
            self.graph.value(node, GN.featureCode) == GN["A.PCLI"]
        ):
            return "country"
        if any(True for _ in self.graph.objects(node, SKOS.narrower)):
            return "group"
        return "unknown"

    def _country_nodes(self, uri: str) -> tuple[URIRef, ...]:
        kind = self.kind(uri)
        node = URIRef(uri)
        if kind == "country":
            return (node,)
        if kind == "adm1":
            parent = self.graph.value(node, GN.parentCountry) or self.graph.value(node, SKOS.broader)
            return (parent,) if isinstance(parent, URIRef) else ()
        if kind == "world" or kind == "group":
            found: set[URIRef] = set()
            pending = [node]
            visited: set[URIRef] = set()
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                if self.kind(str(current)) == "country":
                    found.add(current)
                    continue
                pending.extend(
                    child
                    for child in self.graph.objects(current, SKOS.narrower)
                    if isinstance(child, URIRef)
                )
            return tuple(sorted(found, key=str))
        return ()

    def describe(self, uri: str) -> RegionDescriptor:
        node = URIRef(uri)
        kind = self.kind(uri)
        countries = self._country_nodes(uri)
        isos: list[str] = []
        names: list[str] = []
        for country in countries:
            iso = self.graph.value(country, UE.iso3166Alpha2) or self.graph.value(country, GN.countryCode)
            if iso:
                isos.append(str(iso))
                names.append(self._label(country))
        return RegionDescriptor(
            uri=uri,
            kind=kind,
            label=self._label(node),
            country_uris=tuple(str(country) for country in countries),
            country_isos=tuple(isos),
            country_names=tuple(names),
        )

    @staticmethod
    def _normalized_label(value: object) -> str:
        return " ".join(
            str(value)
            .strip()
            .casefold()
            .replace("’", "'")
            .replace("‘", "'")
            .split()
        )

    def country_isos_for_labels(self, labels: Sequence[str]) -> set[str]:
        """Normalize raw source country labels through approved region assets."""
        if self._country_label_isos is None:
            mapping: dict[str, str] = {}
            country_nodes = set(self.graph.subjects(UE.iso3166Alpha2, None))
            country_nodes.update(self.graph.subjects(GN.countryCode, None))
            for country in country_nodes:
                iso_value = self.graph.value(country, UE.iso3166Alpha2) or self.graph.value(
                    country, GN.countryCode
                )
                if not iso_value:
                    continue
                iso = str(iso_value)
                values = [iso]
                for predicate in (SKOS.prefLabel, SKOS.altLabel, RDFS.label):
                    values.extend(str(value) for value in self.graph.objects(country, predicate))
                for value in values:
                    normalized = self._normalized_label(value)
                    if normalized:
                        mapping[normalized] = iso
            self._country_label_isos = mapping
        return {
            iso
            for label in labels
            if (iso := self._country_label_isos.get(self._normalized_label(label)))
        }

    def verified_match(self, verified_uris: Sequence[str], requested_uri: str) -> bool:
        descriptor = self.describe(requested_uri)
        verified = set(verified_uris)
        if descriptor.kind == "world":
            return True
        if descriptor.kind == "adm1":
            return requested_uri in verified
        if descriptor.kind == "country":
            if requested_uri in verified:
                return True
            return any(
                self.describe(uri).kind == "adm1"
                and requested_uri in self.describe(uri).country_uris
                for uri in verified
            )
        if descriptor.kind == "group":
            allowed = set(descriptor.country_uris)
            for uri in verified:
                if uri in allowed:
                    return True
                evidence = self.describe(uri)
                if evidence.kind == "adm1" and allowed.intersection(evidence.country_uris):
                    return True
        return False


@lru_cache(maxsize=1)
def default_region_index() -> RegionIndex:
    return RegionIndex.load_default()


_REGION_HYDRATION_LOCK = asyncio.Lock()


async def _hydrate_region_index(
    index: RegionIndex, region_uris: Sequence[str]
) -> None:
    """Fill ignored production mirror facts from the loaded Fuseki taxonomy."""
    wanted = tuple(
        uri
        for uri in region_uris
        if (
            index.kind(uri) == "unknown"
            or (
                index.kind(uri) == "group"
                and not index.describe(uri).country_isos
            )
        )
    )
    if not wanted:
        return
    values = " ".join(v1.sparql_iri(uri) for uri in wanted)
    query = f"""
PREFIX ue: <{v1.UE}>
PREFIX gn: <http://www.geonames.org/ontology#>
PREFIX rdfs: <{v1.RDFS}>
PREFIX skos: <{v1.SKOS}>
SELECT DISTINCT ?region ?label ?featureCode ?directIso
                ?parent ?parentIso ?parentName
                ?country ?countryIso ?countryName WHERE {{
  VALUES ?region {{ {values} }}
  GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{
    OPTIONAL {{ ?region skos:prefLabel|rdfs:label ?label . }}
    OPTIONAL {{ ?region gn:featureCode ?featureCode . }}
    OPTIONAL {{ ?region (ue:iso3166Alpha2|gn:countryCode) ?directIso . }}
    OPTIONAL {{
      ?region (gn:parentCountry|skos:broader) ?parent .
      ?parent ue:iso3166Alpha2 ?parentIso ; skos:prefLabel ?parentName .
    }}
    OPTIONAL {{
      ?region (skos:narrower|skos:narrowerTransitive) ?country .
      ?country ue:iso3166Alpha2 ?countryIso ; skos:prefLabel ?countryName .
    }}
  }}
}}
"""
    async with _REGION_HYDRATION_LOCK:
        still_wanted = tuple(
            uri
            for uri in wanted
            if index.kind(uri) == "unknown"
            or (index.kind(uri) == "group" and not index.describe(uri).country_isos)
        )
        if not still_wanted:
            return
        try:
            async with httpx.AsyncClient(timeout=TIME_BUDGET_SECONDS) as client:
                response = await asyncio.wait_for(
                    client.post(
                        v1.get_sparql_endpoint(),
                        data={"query": query, "timeout": "5000,5000"},
                        headers={"Accept": "application/sparql-results+json"},
                    ),
                    timeout=TIME_BUDGET_SECONDS,
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            raise SourceUnavailable("Fuseki region taxonomy unavailable") from exc

        for binding in payload.get("results", {}).get("bindings", []):
            row = {
                key: value.get("value")
                for key, value in binding.items()
                if value.get("value") is not None
            }
            region = URIRef(row.get("region", ""))
            if not region:
                continue
            if row.get("label"):
                index.graph.add((region, SKOS.prefLabel, Literal(row["label"])))
            if row.get("featureCode"):
                index.graph.add((region, GN.featureCode, URIRef(row["featureCode"])))
            if row.get("directIso"):
                index.graph.add((region, UE.iso3166Alpha2, Literal(row["directIso"])))
            parent_value = row.get("parent")
            if parent_value:
                parent = URIRef(parent_value)
                index.graph.add((region, GN.parentCountry, parent))
                index.graph.add((region, SKOS.broader, parent))
                if row.get("parentIso"):
                    index.graph.add((parent, UE.iso3166Alpha2, Literal(row["parentIso"])))
                if row.get("parentName"):
                    index.graph.add((parent, SKOS.prefLabel, Literal(row["parentName"])))
            country_value = row.get("country")
            if country_value:
                country = URIRef(country_value)
                index.graph.add((region, SKOS.narrower, country))
                if row.get("countryIso"):
                    index.graph.add((country, UE.iso3166Alpha2, Literal(row["countryIso"])))
                if row.get("countryName"):
                    index.graph.add((country, SKOS.prefLabel, Literal(row["countryName"])))
        # Hydration mutates the graph after this derived lookup may already
        # have been built by an earlier request. Force the next source adapter
        # to rebuild it from all region facts now present.
        index._country_label_isos = None


async def region_index_for_query(query: CanonicalQuery) -> RegionIndex:
    index = await timed_thread(default_region_index)
    await _hydrate_region_index(index, query.values.get("region", ()))
    return index


def _coverage(source_id: str, query: CanonicalQuery, regions: RegionIndex) -> str:
    descriptors = [regions.describe(uri) for uri in query.values.get("region", ())]
    kinds = {descriptor.kind for descriptor in descriptors}
    if not kinds or kinds == {"world"}:
        return "full"
    if "adm1" in kinds:
        return "country_only" if source_id in {"aea", "who-ictrp"} else "partial"
    if "group" in kinds and source_id in {"ctgov", "isrctn"}:
        return "partial"
    return "full"


def _source_meta(
    *,
    status: str,
    coverage: str,
    count: int = 0,
    truncated: bool = False,
    approximate: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "coverage": coverage,
        "returned_unique_studies": count,
        "truncated": truncated,
        "approximate": approximate,
        "reason": reason,
    }


async def execute_source(
    query: CanonicalQuery,
    adapter: SourceAdapter,
    regions: RegionIndex,
    *,
    budget_factory: Callable[[], SourceBudget] = SourceBudget,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source_id = adapter.source_id
    coverage = _coverage(source_id, query, regions)
    region_values = query.values.get("region", ())
    region_kinds = {regions.kind(uri) for uri in region_values}
    if "unknown" in region_kinds:
        return [], _source_meta(
            status="unavailable",
            coverage="none",
            truncated=True,
            approximate=True,
            reason="upstream_unavailable",
        )
    planning_query = query
    capability_partial = False
    if "adm1" in region_kinds and source_id in {"aea", "who-ictrp"}:
        supported_regions = tuple(
            uri for uri in region_values if regions.kind(uri) != "adm1"
        )
        if query.logic["region"] == "or" and supported_regions:
            effective_values = dict(query.values)
            effective_values["region"] = supported_regions
            planning_query = CanonicalQuery(
                values=effective_values,
                logic=query.logic,
            )
            capability_partial = True
        else:
            return [], _source_meta(
                status="excluded",
                coverage="country_only",
                reason="unsupported_admin_level",
            )

    budget = budget_factory()
    completed_clause_rows: list[dict[str, Any]] = []
    any_truncated = False
    any_approximate = False
    budget_exhausted = False
    terminal_status = "included"
    terminal_reason: str | None = None
    branches_started = 0

    for clause in plan_query(planning_query):
        branch_groups: list[dict[tuple[str, str], list[dict[str, Any]]]] = []
        clause_complete = True
        clause_truncated = False
        for spec in clause.specs:
            if (
                branches_started >= budget.max_requests
                or budget.requests >= budget.max_requests
                or budget.remaining <= 0
            ):
                budget_exhausted = True
                clause_complete = False
                break
            branches_started += 1
            try:
                branch = await bounded(
                    adapter.execute(spec, budget, regions),
                    seconds=budget.remaining,
                )
            except SourceBudgetExhausted:
                budget_exhausted = True
                clause_complete = False
                break
            except (httpx.TimeoutException, asyncio.TimeoutError):
                budget_exhausted = True
                clause_complete = False
                break
            except SourceUnavailable:
                terminal_status = "unavailable"
                terminal_reason = "upstream_unavailable"
                any_truncated = True
                clause_complete = False
                break
            except Exception:
                logger.warning("query-v2 %s adapter failed", source_id, exc_info=True)
                terminal_status = "error"
                terminal_reason = "upstream_error"
                any_truncated = True
                clause_complete = False
                break
            branch_groups.append(
                rows_by_study(
                    _qualified_graph_state_and_rows(
                        branch.rows,
                        spec,
                        planning_query,
                    ),
                    source_id,
                )
            )
            clause_truncated = clause_truncated or branch.truncated
            any_truncated = any_truncated or branch.truncated
            any_approximate = any_approximate or branch.approximate
            budget_exhausted = budget_exhausted or branch.budget_exhausted
            if branch.budget_exhausted:
                # A partially paged single branch is already a sound subset.
                # An unfinished multi-branch intersection is not.
                clause_complete = len(clause.specs) == 1
                break

        if not clause_complete or len(branch_groups) != len(clause.specs):
            any_approximate = True
            break
        if not branch_groups:
            continue
        keys = set(branch_groups[0])
        for group in branch_groups[1:]:
            keys.intersection_update(group)
        if len(branch_groups) > 1 and clause_truncated:
            # Intersecting independently capped branches can miss valid overlap.
            any_approximate = True
        for key in sorted(keys):
            for group in branch_groups:
                completed_clause_rows.extend(group.get(key, ()))
        progress = ACTIVE_EXECUTION.get()
        if progress is not None:
            safe_rows = await asyncio.to_thread(dedupe_presentation, completed_clause_rows)
            progress.sources[source_id] = (safe_rows, _source_meta(
                status="included", coverage=coverage,
                count=len({study_key(row, source_id) for row in safe_rows}),
                truncated=True, approximate=True, reason="budget_exhausted",
            ))

    if budget_exhausted:
        any_truncated = True
        any_approximate = True
        terminal_reason = "budget_exhausted"
    if capability_partial:
        any_truncated = True
        any_approximate = True
        if terminal_reason is None:
            terminal_reason = "unsupported_admin_level"

    rows = await asyncio.to_thread(dedupe_presentation, completed_clause_rows)
    unique_count = len({study_key(row, source_id) for row in rows})
    return rows, _source_meta(
        status=terminal_status,
        coverage=coverage if terminal_status == "included" else ("none" if terminal_status == "unavailable" else coverage),
        count=unique_count,
        truncated=any_truncated,
        approximate=any_approximate,
        reason=terminal_reason,
    )


async def execute_graph_source(
    query: CanonicalQuery,
    adapter: SourceAdapter,
    regions: RegionIndex,
    *,
    budget_factory: Callable[[], SourceBudget] = SourceBudget,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run one source with Graph-only specific-axis presentation enrichment."""
    token = _GRAPH_ATTRIBUTION.set(True)
    try:
        rows, meta = await execute_source(
            query,
            adapter,
            regions,
            budget_factory=budget_factory,
        )
        retained_keys: list[tuple[str, str]] = []
        retained_set: set[tuple[str, str]] = set()
        for row in rows:
            key = study_key(row, adapter.source_id)
            if key in retained_set:
                continue
            if len(retained_keys) >= UNIQUE_STUDY_LIMIT:
                break
            retained_keys.append(key)
            retained_set.add(key)
        unique_count = len({study_key(row, adapter.source_id) for row in rows})
        post_union_excess = unique_count > UNIQUE_STUDY_LIMIT
        exact_source_cap = bool(
            unique_count == UNIQUE_STUDY_LIMIT
            and meta.get("truncated")
            and not meta.get("reason")
        )
        source_limit_reached = post_union_excess or exact_source_cap
        if post_union_excess:
            rows = [
                row
                for row in rows
                if study_key(row, adapter.source_id) in retained_set
            ]
        if source_limit_reached:
            meta = {
                **meta,
                "returned_unique_studies": len(retained_set),
                "truncated": True,
                "approximate": True,
                "reason": meta.get("reason") or "source_limit",
                "source_limit_reached": True,
                # A source that returns exactly the cap may know only that the
                # window was filled, not that a 101st study exists. Preserve
                # zero as the honest lower bound unless the post-union set
                # itself proves excess.
                "source_limit_omitted_lower_bound": max(
                    0, unique_count - UNIQUE_STUDY_LIMIT
                ),
            }
        else:
            meta = {
                **meta,
                "source_limit_reached": False,
                "source_limit_omitted_lower_bound": 0,
            }
        return rows, meta
    finally:
        _GRAPH_ATTRIBUTION.reset(token)


@dataclass(frozen=True)
class QueryV2Execution:
    payload: dict[str, Any]

    @property
    def cacheable(self) -> bool:
        meta = self.payload["meta"]
        # "Approximate" can describe a deliberate, stable bounded result (for
        # example a sound capped intersection). Cache that envelope exactly as
        # returned so an immediate repeat does not re-hit every upstream. Only
        # transient execution states must be retried.
        return all(
            value["status"] in {"included", "excluded"}
            and value.get("reason") in {None, "unsupported_admin_level"}
            for value in meta["sources"].values()
        )


@dataclass
class ExecutionProgress:
    sources: dict = field(default_factory=dict)
    payload: dict | None = None

    def snapshot(self) -> dict:
        if self.payload is not None:
            return self.payload
        sources = {
            source: self.sources.get(source, ([], _source_meta(
                status="included", coverage="none", truncated=True,
                approximate=True, reason="budget_exhausted",
            ))) for source in SOURCE_IDS
        }
        # Each source already published deduplicated, public rows; source IDs
        # are part of presentation identity, so cross-source concatenation is safe.
        rows = [row for source in SOURCE_IDS for row in sources[source][0]]
        return annotate_completion({"results": rows, "meta": {
            "api_version": "query-v2",
            "returned_unique_studies": len({study_key(row) for row in rows}),
            "limit_per_source_branch": UNIQUE_STUDY_LIMIT,
            "truncated": any(value[1]["truncated"] for value in sources.values()),
            "approximate": any(value[1]["approximate"] for value in sources.values()),
            "sources": {source: value[1] for source, value in sources.items()},
        }})


ACTIVE_EXECUTION: ContextVar[ExecutionProgress | None] = ContextVar("query_execution", default=None)


def annotate_completion(payload: dict) -> dict:
    meta = payload["meta"]
    timed_out = [source for source, value in meta["sources"].items()
                 if value.get("reason") == "budget_exhausted"]
    completed = [source for source, value in meta["sources"].items()
                 if value.get("status") == "included" and value.get("reason") is None]
    failed = any(value.get("status") in {"error", "unavailable"} for value in meta["sources"].values())
    meta["timed_out_sources"] = timed_out
    meta["completed_sources"] = completed
    meta["execution_status"] = (
        "timeout" if timed_out and not completed and not payload["results"]
        else "partial" if timed_out or failed else "complete"
    )
    return payload


async def execute_query_v2(query: CanonicalQuery, **kwargs) -> QueryV2Execution:
    progress = ACTIVE_EXECUTION.get() or ExecutionProgress()
    token = ACTIVE_EXECUTION.set(progress)
    deadline = REQUEST_DEADLINE.get()
    if deadline is None:
        deadline = time.monotonic() + RESPONSE_TIMEOUT_SECONDS
    task = asyncio.create_task(_execute_query_v2(query, **kwargs), name="query-v2-execution")
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0, deadline - time.monotonic()))
        if done:
            execution = task.result()
            progress.payload = annotate_completion(execution.payload)
        else:
            stage("execution_deadline")
        return QueryV2Execution(progress.snapshot())
    finally:
        if not task.done():
            observe(task, cancel=True)
        ACTIVE_EXECUTION.reset(token)


async def _execute_query_v2(
    query: CanonicalQuery,
    *,
    adapters: Mapping[str, SourceAdapter] | None = None,
    regions: RegionIndex | None = None,
    budget_factory: Callable[[], SourceBudget] = SourceBudget,
    source_stage_timeout: float | None = None,
) -> QueryV2Execution:
    stage("planning_start")
    try:
        region_index = regions or await region_index_for_query(query)
    except SourceUnavailable:
        source_meta = {
            source: _source_meta(
                status="unavailable",
                coverage="none",
                truncated=True,
                approximate=True,
                reason="upstream_unavailable",
            )
            for source in SOURCE_IDS
        }
        return QueryV2Execution(
            payload={
                "results": [],
                "meta": {
                    "api_version": "query-v2",
                    "returned_unique_studies": 0,
                    "limit_per_source_branch": UNIQUE_STUDY_LIMIT,
                    "truncated": True,
                    "approximate": True,
                    "sources": source_meta,
                },
            }
        )
    except Exception:
        logger.warning("query-v2 region index failed", exc_info=True)
        source_meta = {
            source: _source_meta(
                status="error",
                coverage="none",
                truncated=True,
                approximate=True,
                reason="upstream_error",
            )
            for source in SOURCE_IDS
        }
        return QueryV2Execution(
            payload={
                "results": [],
                "meta": {
                    "api_version": "query-v2",
                    "returned_unique_studies": 0,
                    "limit_per_source_branch": UNIQUE_STUDY_LIMIT,
                    "truncated": True,
                    "approximate": True,
                    "sources": source_meta,
                },
            }
        )

    source_adapters = adapters or default_adapters()
    if adapters is None:
        concept_uris = sorted(
            {
                uri
                for axis in ("state", "condition", "intervention", "outcome")
                for uri in query.values.get(axis, ())
            }
        )
        if concept_uris:
            await asyncio.gather(
                *(resolve_concept(uri) for uri in concept_uris),
                return_exceptions=True,
            )
    stage("planning_end")
    progress = ACTIVE_EXECUTION.get()

    async def run_source(source):
        stage("source_start", source=source)
        try:
            value = await execute_source(query, source_adapters[source], region_index,
                                         budget_factory=budget_factory)
            if progress is not None:
                progress.sources[source] = value
            return value
        finally:
            stage("source_end", source=source)

    tasks = [asyncio.create_task(run_source(source), name=f"query-source-{source}") for source in SOURCE_IDS]
    # asyncio.wait_for() waits for a cancelled coroutine to finish cleaning up.
    # A slow HTTP client or parser can therefore outlive its nominal source
    # budget. Do not let that cancellation tail reach the proxy timeout: retain
    # every source that completed and represent remaining work as a sound empty
    # subset with explicit budget metadata.
    deadline = (
        TIME_BUDGET_SECONDS + 0.25
        if source_stage_timeout is None
        else source_stage_timeout
    )
    request_deadline = REQUEST_DEADLINE.get()
    if request_deadline is not None:
        deadline = min(deadline, max(0, request_deadline - time.monotonic()))
    try:
        done, pending = await asyncio.wait(tasks, timeout=deadline)
    finally:
        for task in tasks:
            if not task.done():
                observe(task, cancel=True)
    stage("aggregation_start")

    source_results: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
    for source, task in zip(SOURCE_IDS, tasks):
        if task in pending:
            source_results[source] = (progress.sources[source] if progress is not None and source in progress.sources else (
                [],
                _source_meta(
                    status="included",
                    coverage=_coverage(source, query, region_index),
                    truncated=True,
                    approximate=True,
                    reason="budget_exhausted",
                ),
            ))
            continue
        try:
            value = task.result()
        except BaseException as exc:
            value = exc
        if isinstance(value, BaseException):
            logger.warning(
                "query-v2 isolated unexpected %s failure",
                source,
                exc_info=(type(value), value, value.__traceback__),
            )
            source_results[source] = (
                [],
                _source_meta(
                    status="error",
                    coverage="none",
                    truncated=True,
                    approximate=True,
                    reason="upstream_error",
                ),
            )
        else:
            source_results[source] = value
    rows = await asyncio.to_thread(dedupe_presentation,
        [row for source in SOURCE_IDS for row in source_results[source][0]]
    )
    source_meta = {source: source_results[source][1] for source in SOURCE_IDS}
    unique_count = len({study_key(row) for row in rows})
    stage("aggregation_end", studies=unique_count)
    return QueryV2Execution(
        payload={
            "results": rows,
            "meta": {
                "api_version": "query-v2",
                "returned_unique_studies": unique_count,
                "limit_per_source_branch": UNIQUE_STUDY_LIMIT,
                "truncated": any(meta["truncated"] for meta in source_meta.values()),
                "approximate": any(meta["approximate"] for meta in source_meta.values()),
                "sources": source_meta,
            },
        }
    )


async def _sparql_select(query: str, budget: SourceBudget) -> list[dict[str, str]]:
    timeout = budget.claim_request()
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            v1.get_sparql_endpoint(),
            data={"query": query, "timeout": "5000,5000"},
            headers={"Accept": "application/sparql-results+json"},
        )
        response.raise_for_status()
        payload = response.json()
    return [
        {
            key: value["value"]
            for key, value in binding.items()
            if value.get("value") is not None
        }
        for binding in payload.get("results", {}).get("bindings", [])
    ]


def _limit_selected_studies(
    rows: Sequence[Mapping[str, Any]], study_field: str = "study"
) -> tuple[list[dict[str, Any]], bool]:
    order: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in rows:
        study = str(raw.get(study_field) or "")
        if not study:
            continue
        if study not in grouped:
            order.append(study)
            grouped[study] = []
        grouped[study].append(dict(raw))
    truncated = len(order) > UNIQUE_STUDY_LIMIT
    selected = set(order[:UNIQUE_STUDY_LIMIT])
    return [row for study in order if study in selected for row in grouped[study]], truncated


def _region_filter_parts(
    source_id: str, spec: Mapping[str, str], regions: RegionIndex
) -> tuple[str, RegionDescriptor | None]:
    region_uri = spec.get("region")
    if not region_uri:
        return "", None
    descriptor = regions.describe(region_uri)
    if descriptor.kind == "world":
        return "", descriptor
    if descriptor.kind == "unknown":
        raise SourceUnavailable(f"unknown region asset: {region_uri}")
    if source_id == "aea":
        if not descriptor.country_uris:
            return "FILTER(false)", descriptor
        field = v1._first_source_field("aea", "region", "region_literal")
        values = " ".join(v1.sparql_iri(uri) for uri in descriptor.country_uris)
        return f"""
    ?study {v1.sparql_iri(field)} ?region_raw .
  }}
  GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{
    ?region_entry ue:rawText ?region_raw ;
                  (skos:exactMatch|skos:closeMatch) ?selected_region .
    VALUES ?selected_region {{ {values} }}
  }}
  GRAPH <{v1._source_graph_uri('aea', v1.AEA_GRAPH_URI)}> {{
""", descriptor
    if source_id == "who-ictrp":
        if not descriptor.country_isos:
            return "FILTER(false)", descriptor
        values = " ".join(v1.sparql_string(iso) for iso in descriptor.country_isos)
        return f"VALUES ?selected_iso {{ {values} }}\n    ?study <{v1.ICTRP}countryIsoAlpha2> ?selected_iso .", descriptor
    return "", descriptor


def _local_axis_requirement(axis: str, value: str) -> str:
    """Return one source-graph predicate for a canonical query axis."""
    if axis == "state":
        return (
            f"    ?study (<{v1._AXIS_MATCH_PRED['condition']}>|"
            f"<{v1._AXIS_MATCH_PRED['outcome']}>) {v1.sparql_iri(value)} ."
        )
    return (
        f"    ?study <{v1._AXIS_MATCH_PRED[axis]}> "
        f"{v1.sparql_iri(value)} ."
    )


def _local_query(source_id: str, spec: Mapping[str, str], regions: RegionIndex) -> str:
    graph_uri = (
        v1._source_graph_uri("aea", v1.AEA_GRAPH_URI)
        if source_id == "aea"
        else v1.WHO_ICTRP_GRAPH_URI
    )
    anchor = (
        f"    ?study a <{v1.AEA}RCTStudy> ."
        if source_id == "aea"
        else f"    ?study a <{v1.UE}Evidence> ."
    )
    axis_requirements = "\n".join(
        _local_axis_requirement(axis, spec[axis])
        for axis in ("state", "condition", "intervention", "outcome")
        if spec.get(axis)
    )
    required = anchor + ("\n" + axis_requirements if axis_requirements else "")
    region_filter, _ = _region_filter_parts(source_id, spec, regions)
    region_filter = region_filter or ""

    if source_id == "aea":
        intervention_field = v1._first_source_field("aea", "intervention", "raw_text")
        outcome_field = v1._first_source_field("aea", "outcome", "raw_text")
        detail = f"""
  BIND("AEA" AS ?source)
  GRAPH <{graph_uri}> {{
    ?study <{v1.DCTERMS}identifier> ?study_id ;
           <{v1.DCTERMS}title> ?title .
    OPTIONAL {{ ?study <{v1.UE}studyStatus> ?status . }}
    OPTIONAL {{ ?study <{v1.AEA}studyStatus> ?status . }}
    OPTIONAL {{ ?study <{v1.DCTERMS}date> ?date . }}
    OPTIONAL {{ ?study <{v1.AEA}country> ?country . }}
    OPTIONAL {{ ?study <{intervention_field}> ?intervention . }}
    OPTIONAL {{ ?study <{outcome_field}> ?outcomeText . }}
  }}
  OPTIONAL {{
    GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{
      ?interventionEntry ue:rawText ?intervention ;
        (skos:exactMatch|skos:closeMatch) ?interventionUri .
      ?interventionUri a ue:Intervention ; skos:prefLabel ?interventionLabel .
    }}
  }}
"""
    else:
        detail = f"""
  BIND("WHO ICTRP" AS ?source)
  GRAPH <{graph_uri}> {{
    ?study <{v1.DCTERMS}identifier> ?study_id ;
           <{v1.DCTERMS}title> ?title .
    OPTIONAL {{ ?study <{v1.ICTRP}country> ?country . }}
    OPTIONAL {{ ?study <{v1.ICTRP}dateOfRegistration> ?date . }}
    OPTIONAL {{ ?study <{v1.ICTRP}intervention> ?intervention . }}
    OPTIONAL {{ ?study <{v1.ICTRP}primaryOutcome>|<{v1.ICTRP}secondaryOutcome> ?outcomeText . }}
  }}
  OPTIONAL {{
    GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{
      ?interventionEntry ue:rawText ?intervention ;
        (skos:exactMatch|skos:closeMatch) ?interventionUri .
      ?interventionUri a ue:Intervention ; skos:prefLabel ?interventionLabel .
    }}
  }}
"""

    condition_bind = ""
    if spec.get("condition"):
        condition_bind = f"""
  BIND({v1.sparql_iri(spec['condition'])} AS ?conditionUri)
  OPTIONAL {{ GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{ ?conditionUri skos:prefLabel ?conditionLabel . }} }}
"""
    intervention_bind = ""
    if spec.get("intervention"):
        intervention_bind = f"""
  BIND({v1.sparql_iri(spec['intervention'])} AS ?matchedInterventionUri)
  OPTIONAL {{ GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{ ?matchedInterventionUri skos:prefLabel ?matchedInterventionLabel . }} }}
"""
    outcome_bind = ""
    if spec.get("outcome"):
        outcome_bind = f"""
  BIND({v1.sparql_iri(spec['outcome'])} AS ?outcomeUri)
  OPTIONAL {{ GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{ ?outcomeUri skos:prefLabel ?outcomeLabel . }} }}
"""

    return f"""
PREFIX ue: <{v1.UE}>
PREFIX skos: <{v1.SKOS}>
SELECT DISTINCT ?study ?source ?study_id ?title ?status ?date ?country
       ?intervention ?interventionUri ?interventionLabel
       ?matchedInterventionUri ?matchedInterventionLabel
       ?conditionUri ?conditionLabel ?outcomeText ?outcomeUri ?outcomeLabel WHERE {{
  {{
    SELECT DISTINCT ?study WHERE {{
      GRAPH <{graph_uri}> {{
{required}
{region_filter}
      }}
    }}
    LIMIT {UNIQUE_STUDY_LIMIT + 1}
  }}
{detail}
{condition_bind}
{intervention_bind}
{outcome_bind}
}}
ORDER BY DESC(?date) ?study
"""


def _local_selector_query(
    source_id: str, spec: Mapping[str, str], regions: RegionIndex
) -> str:
    graph_uri = (
        v1._source_graph_uri("aea", v1.AEA_GRAPH_URI)
        if source_id == "aea"
        else v1.WHO_ICTRP_GRAPH_URI
    )
    anchor = (
        f"?study a <{v1.AEA}RCTStudy> ."
        if source_id == "aea"
        else f"?study a <{v1.UE}Evidence> ."
    )
    requirements = [anchor]
    requirements.extend(
        _local_axis_requirement(axis, spec[axis]).strip()
        for axis in ("state", "condition", "intervention", "outcome")
        if spec.get(axis)
    )
    region_filter, _ = _region_filter_parts(source_id, spec, regions)
    return f"""
PREFIX ue: <{v1.UE}>
PREFIX skos: <{v1.SKOS}>
SELECT DISTINCT ?study WHERE {{
  GRAPH <{graph_uri}> {{
    {chr(10).join(requirements)}
    {region_filter}
  }}
}}
LIMIT {UNIQUE_STUDY_LIMIT + 1}
"""


def _local_detail_query(
    source_id: str,
    study_uris: Sequence[str],
    spec: Mapping[str, str],
    *,
    hydrate_direct_interventions: bool = True,
) -> str:
    graph_uri = (
        v1._source_graph_uri("aea", v1.AEA_GRAPH_URI)
        if source_id == "aea"
        else v1.WHO_ICTRP_GRAPH_URI
    )
    values = " ".join(v1.sparql_iri(uri) for uri in study_uris)
    if source_id == "aea":
        details = f"""
  BIND("AEA" AS ?source)
  GRAPH <{graph_uri}> {{
    ?study <{v1.DCTERMS}identifier> ?study_id ;
           <{v1.DCTERMS}title> ?title .
    OPTIONAL {{ ?study <{v1.UE}studyStatus> ?status . }}
    OPTIONAL {{ ?study <{v1.AEA}studyStatus> ?status . }}
    OPTIONAL {{ ?study <{v1.DCTERMS}date> ?date . }}
    OPTIONAL {{ ?study <{v1.AEA}country> ?country . }}
  }}
"""
    else:
        details = f"""
  BIND("WHO ICTRP" AS ?source)
  GRAPH <{graph_uri}> {{
    ?study <{v1.DCTERMS}identifier> ?study_id ;
           <{v1.DCTERMS}title> ?title .
  }}
"""
    selected_bindings = ""
    if spec.get("condition"):
        selected_bindings += f"""
  BIND({v1.sparql_iri(spec['condition'])} AS ?conditionUri)
  OPTIONAL {{ GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{ ?conditionUri skos:prefLabel ?conditionLabel . }} }}
"""
    if not hydrate_direct_interventions and spec.get("intervention"):
        selected_bindings += f"""
  BIND({v1.sparql_iri(spec['intervention'])} AS ?matchedInterventionUri)
  OPTIONAL {{ GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{ ?matchedInterventionUri skos:prefLabel ?matchedInterventionLabel . }} }}
"""
    if spec.get("outcome"):
        selected_bindings += f"""
  BIND({v1.sparql_iri(spec['outcome'])} AS ?outcomeUri)
  OPTIONAL {{ GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{ ?outcomeUri skos:prefLabel ?outcomeLabel . }} }}
"""
    if spec.get("state"):
        state = v1.sparql_iri(spec["state"])
        selected_bindings += f"""
  OPTIONAL {{
    GRAPH <{graph_uri}> {{ ?study <{v1._AXIS_MATCH_PRED['condition']}> {state} . }}
    BIND({state} AS ?conditionUri)
    OPTIONAL {{ GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{ ?conditionUri skos:prefLabel ?conditionLabel . }} }}
  }}
  OPTIONAL {{
    GRAPH <{graph_uri}> {{ ?study <{v1._AXIS_MATCH_PRED['outcome']}> {state} . }}
    BIND({state} AS ?outcomeUri)
    OPTIONAL {{ GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{ ?outcomeUri skos:prefLabel ?outcomeLabel . }} }}
  }}
"""
    if hydrate_direct_interventions:
        intervention_text_predicate = (
            v1._first_source_field("aea", "intervention", "raw_text")
            if source_id == "aea"
            else f"{v1.ICTRP}intervention"
        )
        selected_bindings += f"""
  OPTIONAL {{
    GRAPH <{graph_uri}> {{
      ?study {v1.sparql_iri(intervention_text_predicate)} ?intervention .
    }}
  }}
"""
    return f"""
PREFIX skos: <{v1.SKOS}>
SELECT DISTINCT ?study ?source ?study_id ?title ?status ?date ?country
       ?intervention
       ?matchedInterventionUri ?matchedInterventionLabel
       ?conditionUri ?conditionLabel ?outcomeText ?outcomeUri ?outcomeLabel WHERE {{
  VALUES ?study {{ {values} }}
{details}
{selected_bindings}
}}
"""


def _minimal_local_rows(source_id: str, study_uris: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for uri in study_uris:
        study_id = uri.rsplit("_", 1)[-1] if source_id == "aea" else uri.rstrip("/").rsplit("/", 1)[-1]
        rows.append(
            {
                "source": SOURCE_LABELS[source_id],
                "study_id": study_id,
                "title": None,
                "url": (
                    v1._aea_external_url(study_id, uri)
                    if source_id == "aea"
                    else v1._who_ictrp_external_url(study_id)
                ),
                "intervention": None,
                "intervention_concept": None,
                "intervention_concept_uri": None,
                "condition_concept": None,
                "condition_concept_uri": None,
                "country": None,
                "status": None,
                "year": None,
                "outcomes": [],
            }
        )
    return rows


def _local_rows(
    source_id: str,
    rows: Sequence[Mapping[str, str]],
    spec: Mapping[str, str],
    direct_intervention_crosswalk: Mapping[str, str | Sequence[str]] | None = None,
    intervention_labels: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    direct_intervention_crosswalk = direct_intervention_crosswalk or {}
    intervention_labels = intervention_labels or {}
    results: list[dict[str, Any]] = []
    for row in rows:
        raw_study_id = row.get("study_id")
        study_uri = row.get("study", "")
        study_id = raw_study_id or study_uri.rsplit("_", 1)[-1]
        if source_id == "aea":
            for candidate in (study_uri, raw_study_id):
                match = v1._AEA_STUDY_ID_PATTERN.search(str(candidate or ""))
                if match:
                    study_id = match.group(0)
                    break
        selected_uri = row.get("matchedInterventionUri") or row.get("interventionUri")
        selected_label = row.get("matchedInterventionLabel") or row.get("interventionLabel")
        direct_matches: dict[str, str | None] = {}
        raw_by_uri: dict[str, str] = {}
        if selected_uri:
            direct_matches[selected_uri] = selected_label or intervention_labels.get(
                selected_uri
            )
        else:
            raw_value = row.get("intervention")
            if raw_value:
                for concept_uri in _mapped_source_intervention_uris(
                    source_id, direct_intervention_crosswalk, raw_value
                ):
                    direct_matches.setdefault(
                        concept_uri, intervention_labels.get(concept_uri)
                    )
                    raw_by_uri.setdefault(concept_uri, raw_value)
        outcome: list[dict[str, Any]] = []
        if row.get("outcomeText"):
            item = v1.prespecified_outcome(row["outcomeText"])
            if row.get("outcomeUri"):
                item["state_concept_uri"] = row["outcomeUri"]
                item["state_concept"] = row.get("outcomeLabel")
            outcome.append(item)
        presented_matches: Sequence[tuple[str | None, str | None]] = (
            tuple(direct_matches.items()) if direct_matches else ((None, None),)
        )
        for intervention_uri, intervention_label in presented_matches:
            results.append(
                {
                    "source": SOURCE_LABELS[source_id],
                    "study_id": study_id,
                    "title": row.get("title"),
                    "url": (
                        v1._aea_external_url(raw_study_id, study_uri)
                        if source_id == "aea"
                        else v1._who_ictrp_external_url(str(study_id))
                    ),
                    "intervention": (
                        raw_by_uri.get(str(intervention_uri))
                        or row.get("intervention")
                        or intervention_label
                    ),
                    "intervention_concept": intervention_label,
                    "intervention_concept_uri": intervention_uri,
                    "condition_concept": row.get("conditionLabel"),
                    "condition_concept_uri": row.get("conditionUri"),
                    "country": row.get("country"),
                    "status": row.get("status"),
                    "year": v1.extract_study_year(row.get("date")),
                    "outcomes": outcome,
                }
            )
    return results


class LocalSourceAdapter:
    def __init__(
        self,
        source_id: str,
        select: Callable[[str, SourceBudget], Any] = _sparql_select,
    ):
        if source_id not in {"aea", "who-ictrp"}:
            raise ValueError(source_id)
        self.source_id = source_id
        self._select = select

    async def execute(
        self,
        spec: Mapping[str, str],
        budget: SourceBudget,
        regions: RegionIndex,
    ) -> BranchResult:
        hydrate_direct_interventions = not _GRAPH_ATTRIBUTION.get()
        async with _bounded_slot(v1._LOCAL_QUERY_SEMAPHORE, budget):
            selector_rows = await self._select(
                _local_selector_query(self.source_id, spec, regions), budget
            )
            selected_rows, truncated = _limit_selected_studies(selector_rows)
            study_uris = list(
                dict.fromkeys(
                    str(row.get("study") or "")
                    for row in selected_rows
                    if row.get("study")
                )
            )
            if not study_uris:
                return BranchResult(rows=[], truncated=truncated)
            try:
                detail_rows = await self._select(
                    _local_detail_query(
                        self.source_id,
                        study_uris,
                        spec,
                        hydrate_direct_interventions=hydrate_direct_interventions,
                    ),
                    budget,
                )
            except (SourceBudgetExhausted, httpx.TimeoutException, asyncio.TimeoutError):
                return BranchResult(
                    rows=_minimal_local_rows(self.source_id, study_uris),
                    truncated=True,
                    approximate=True,
                    budget_exhausted=True,
                )
        direct_intervention_crosswalk: Mapping[
            str, str | Sequence[str]
        ] = {}
        intervention_labels: Mapping[str, str] = {}
        if hydrate_direct_interventions and any(
            row.get("intervention") for row in detail_rows
        ):
            try:
                direct_intervention_crosswalk = await _taxonomy_call(
                    _source_direct_intervention_crosswalk, self.source_id
                )
                matched_intervention_uris = sorted(
                    {
                        concept_uri
                        for row in detail_rows
                        if (raw_value := row.get("intervention"))
                        for concept_uri in _mapped_source_intervention_uris(
                            self.source_id,
                            direct_intervention_crosswalk,
                            raw_value,
                        )
                    }
                )
                intervention_labels = await _taxonomy_call(
                    _local_intervention_labels, matched_intervention_uris
                )
            except Exception as exc:
                raise SourceUnavailable(
                    f"{self.source_id} direct intervention map unavailable"
                ) from exc
        return BranchResult(
            rows=_local_rows(
                self.source_id,
                detail_rows,
                spec,
                direct_intervention_crosswalk,
                intervention_labels,
            ),
            truncated=truncated,
        )


def _quoted_or(terms: Sequence[str]) -> str:
    return " OR ".join(f'"{term}"' for term in terms if term)


def _ctgov_state_expression(
    resolution: ConceptResolution,
) -> tuple[str, bool]:
    """Build one CT.gov query.term expression for condition OR outcome.

    Keeping both roles inside one ranked upstream query is what makes the
    source's unique-study limit apply after the union instead of once per role.
    """
    available_terms = list(resolution.terms)
    chosen_terms = available_terms[:20] or (
        [str(resolution.label)] if resolution.label else []
    )
    role_expressions: list[str] = []
    condition_filters = v1.ctgov_condition_mesh_filter(
        list(resolution.mesh_uris)
    )
    if condition_filters:
        role_expressions.append(
            "(" + " OR ".join(condition_filters) + ")"
        )
    elif chosen_terms:
        role_expressions.append(
            f"AREA[ConditionSearch]({_quoted_or(chosen_terms)})"
        )
    if chosen_terms:
        role_expressions.append(
            f"AREA[OutcomeSearch]({_quoted_or(chosen_terms)})"
        )
    if not role_expressions:
        raise SourceUnavailable("CT.gov cannot resolve state URI")
    return (
        "(" + " OR ".join(role_expressions) + ")",
        len(available_terms) > len(chosen_terms),
    )


async def _taxonomy_call(function: Callable[..., Any], *args: Any) -> Any:
    return await timed_thread(function, *args)


def _raw_ctgov_country_isos(
    study: Mapping[str, Any],
    regions: RegionIndex,
) -> set[str]:
    labels = [
        location.country
        for location in v1.study_locations(dict(study))
        if location.country
    ]
    resolved = regions.country_isos_for_labels(labels)
    if resolved:
        return resolved
    # An anywhere query does not hydrate every ignored country mirror into the
    # process-local region index. Preserve the legacy normalizer as a display
    # hint there; predicate-bearing country/group queries use the hydrated
    # approved GeoNames facts above.
    return {
        v1.normalize_country_to_iso(label, context="ctgov-v2")
        for label in labels
    }


def _local_taxonomy_matches_by_mesh(
    mesh_uris: Sequence[str],
    filename: str,
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Map a bounded MeSH set to every direct local taxonomy target."""
    if not mesh_uris:
        return {}
    graph = _local_taxonomy_graph(filename)
    result: dict[str, tuple[tuple[str, str], ...]] = {}
    for mesh_uri in mesh_uris:
        mesh = URIRef(mesh_uri)
        candidates = sorted(
            {
                URIRef(subject)
                for predicate in (SKOS.exactMatch, SKOS.closeMatch)
                for subject in graph.subjects(predicate, mesh)
            },
            key=str,
        )
        matches: list[tuple[str, str]] = []
        for subject in candidates:
            label = next(graph.objects(subject, SKOS.prefLabel), None)
            if label is None:
                label = next(graph.objects(subject, RDFS.label), None)
            if label is not None:
                matches.append((str(subject), str(label)))
        if matches:
            result[mesh_uri] = tuple(matches)
    return result


def _local_states_by_mesh_multi(
    mesh_uris: Sequence[str],
) -> dict[str, tuple[tuple[str, str], ...]]:
    return _local_taxonomy_matches_by_mesh(mesh_uris, "states.ttl")


def _local_states_by_mesh(
    mesh_uris: Sequence[str],
) -> dict[str, tuple[str, str]]:
    """Compatibility view used by ordinary Search's single-coordinate UI."""
    return {
        mesh_uri: matches[0]
        for mesh_uri, matches in _local_states_by_mesh_multi(mesh_uris).items()
    }


def _local_interventions_by_mesh_multi(
    mesh_uris: Sequence[str],
) -> dict[str, tuple[tuple[str, str], ...]]:
    return _local_taxonomy_matches_by_mesh(mesh_uris, "interventions.ttl")


def _local_interventions_by_label_multi(
    texts: Sequence[str],
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Map exact raw labels to every direct tracked Intervention target."""
    if not texts:
        return {}
    graph = _local_taxonomy_graph("interventions.ttl")
    result: dict[str, tuple[tuple[str, str], ...]] = {}
    for text in dict.fromkeys(str(value) for value in texts if value):
        literal = Literal(text)
        candidates = sorted(
            {
                subject
                for predicate in (SKOS.prefLabel, SKOS.altLabel, RDFS.label)
                for subject in graph.subjects(predicate, literal)
                if isinstance(subject, URIRef)
                and (subject, RDF.type, UE.Intervention) in graph
            },
            key=str,
        )
        matches: list[tuple[str, str]] = []
        for subject in candidates:
            label = next(graph.objects(subject, SKOS.prefLabel), None)
            if label is None:
                label = next(graph.objects(subject, RDFS.label), None)
            if label is not None:
                matches.append((str(subject), str(label)))
        if matches:
            result[text] = tuple(matches)
    return result


def _local_intervention_labels(concept_uris: Sequence[str]) -> dict[str, str]:
    """Resolve intervention labels from the tracked taxonomy without per-row I/O."""
    if not concept_uris:
        return {}
    graph = _local_taxonomy_graph("interventions.ttl")
    labels: dict[str, str] = {}
    for uri in concept_uris:
        subject = URIRef(uri)
        value = next(graph.objects(subject, SKOS.prefLabel), None)
        if value is None:
            value = next(graph.objects(subject, RDFS.label), None)
        if value is not None:
            labels[uri] = str(value)
    return labels


ISRCTN_INTERVENTION_CROSSWALK_PREFIX = (
    "https://universalevidence.com/crosswalk/isrctn-interventions/"
)

_DIRECT_INTERVENTION_CROSSWALK_PREFIXES = {
    "ctgov": (
        "https://universalevidence.com/crosswalk/ctgov-interventions/",
    ),
    # AEA's registry keyword is also used for condition tags. Search presents
    # intervention mappings derived from the actual intervention description,
    # while the broader keyword stamps remain available for filtering/Graph.
    "aea": (
        "https://universalevidence.com/crosswalk/aea-interventions-freetext/",
    ),
    "who-ictrp": (
        "https://universalevidence.com/crosswalk/who-ictrp-interventions/",
    ),
    "isrctn": (
        ISRCTN_INTERVENTION_CROSSWALK_PREFIX,
    ),
}

CTGOV_OUTCOME_CROSSWALK_PREFIX = (
    "https://universalevidence.com/crosswalk/ctgov-outcomes/"
)

_DIRECT_STATE_CROSSWALK_PREFIXES = {
    "ctgov": {
        "outcome": (CTGOV_OUTCOME_CROSSWALK_PREFIX,),
    },
    "aea": {
        "condition": (
            "https://universalevidence.com/crosswalk/aea-conditions/",
        ),
        "outcome": (
            "https://universalevidence.com/crosswalk/aea-outcomes/",
        ),
    },
    "isrctn": {
        "condition": (
            "https://universalevidence.com/crosswalk/isrctn-states/",
        ),
        "outcome": (
            "https://universalevidence.com/crosswalk/isrctn-outcomes/",
        ),
    },
    "who-ictrp": {
        "condition": (
            "https://universalevidence.com/crosswalk/who-ictrp-conditions/",
        ),
        "outcome": (
            "https://universalevidence.com/crosswalk/who-ictrp-outcomes/",
        ),
    },
}

_PREWARMED_GRAPH_DIRECT_STATE_CROSSWALKS: dict[
    tuple[str, str], dict[str, tuple[str, ...]]
] = {}


def _load_source_direct_intervention_crosswalk(
    source_id: str,
) -> dict[str, tuple[str, ...]]:
    """Load source-scoped direct targets from the live Fuseki taxonomy.

    Crosswalk TTLs are R2-managed loader inputs and are intentionally absent
    from the production API image. Reading Fuseki here keeps Search on the
    exact snapshot that produced the source-graph stamps.
    """
    if source_id not in _DIRECT_INTERVENTION_CROSSWALK_PREFIXES:
        raise ValueError(source_id)
    entry_prefixes = _DIRECT_INTERVENTION_CROSSWALK_PREFIXES[source_id]
    targets: dict[str, set[str]] = {}
    prefix_filter = " || ".join(
        f'STRSTARTS(STR(?entry), "{prefix}")' for prefix in entry_prefixes
    )
    query = f"""
PREFIX ue: <{v1.UE}>
PREFIX skos: <{v1.SKOS}>
SELECT DISTINCT ?rawText ?concept WHERE {{
  GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{
    ?entry a ue:CrosswalkEntry ;
           ue:rawText ?rawText ;
           (skos:exactMatch|skos:closeMatch) ?concept .
    ?concept a ue:Intervention .
    FILTER({prefix_filter})
  }}
}}
"""
    for row in v1.sparql_select(query):
        raw_value = row.get("rawText")
        concept = row.get("concept")
        if raw_value and concept:
            key = _source_direct_intervention_key(source_id, raw_value)
            targets.setdefault(key, set()).add(str(concept))
    result = {
        key: tuple(sorted(concepts))
        for key, concepts in targets.items()
        if concepts
    }
    if not result:
        raise SourceUnavailable(
            f"{source_id} direct intervention crosswalk is empty"
        )
    return result


@lru_cache(maxsize=4)
def _source_direct_intervention_crosswalk(
    source_id: str,
) -> dict[str, tuple[str, ...]]:
    return _load_source_direct_intervention_crosswalk(source_id)


@lru_cache(maxsize=1)
def _isrctn_direct_intervention_crosswalk() -> dict[str, tuple[str, ...]]:
    """Return every direct ISRCTN raw-text target without ancestor expansion."""
    return _source_direct_intervention_crosswalk("isrctn")


def _load_source_direct_state_crosswalk(
    source_id: str,
    role: str,
) -> dict[str, tuple[str, ...]]:
    """Return source-scoped direct condition/outcome State targets."""
    try:
        entry_prefixes = _DIRECT_STATE_CROSSWALK_PREFIXES[source_id][role]
    except KeyError as exc:
        raise ValueError((source_id, role)) from exc
    prefix_filter = " || ".join(
        f'STRSTARTS(STR(?entry), "{prefix}")' for prefix in entry_prefixes
    )
    query = f"""
PREFIX ue: <{v1.UE}>
PREFIX skos: <{v1.SKOS}>
SELECT DISTINCT ?rawText ?concept WHERE {{
  GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{
    ?entry a ue:CrosswalkEntry ;
           ue:rawText ?rawText ;
           (skos:exactMatch|skos:closeMatch) ?concept .
    ?concept a ue:State .
    FILTER({prefix_filter})
  }}
}}
"""
    targets: dict[str, set[str]] = {}
    for row in v1.sparql_select(query):
        raw_value = row.get("rawText")
        concept = row.get("concept")
        if raw_value and concept:
            key = _source_direct_state_key(source_id, raw_value)
            targets.setdefault(key, set()).add(str(concept))
    result = {
        key: tuple(sorted(concepts))
        for key, concepts in targets.items()
        if concepts
    }
    if not result:
        raise SourceUnavailable(
            f"{source_id} direct {role} State crosswalk is empty"
        )
    return result


def _load_graph_direct_state_crosswalks() -> dict[
    tuple[str, str], dict[str, tuple[str, ...]]
]:
    """Load and validate every Graph direct-State map in one Fuseki request."""
    expected_pairs = tuple(
        (source_id, role)
        for source_id, roles in _DIRECT_STATE_CROSSWALK_PREFIXES.items()
        for role in roles
    )
    prefix_routes = tuple(
        (prefix, (source_id, role))
        for source_id, role in expected_pairs
        for prefix in _DIRECT_STATE_CROSSWALK_PREFIXES[source_id][role]
    )
    query = f"""
PREFIX ue: <{v1.UE}>
PREFIX skos: <{v1.SKOS}>
SELECT ?entry ?rawText ?concept WHERE {{
  GRAPH <{v1.TAXONOMY_GRAPH_URI}> {{
    {{ ?entry skos:exactMatch ?concept . }}
    UNION
    {{ ?entry skos:closeMatch ?concept . }}
    ?concept a ue:State .
    ?entry a ue:CrosswalkEntry ;
           ue:rawText ?rawText .
  }}
}}
"""
    targets: dict[tuple[str, str], dict[str, set[str]]] = {
        pair: {} for pair in expected_pairs
    }
    for row in v1.sparql_select(query):
        entry = str(row.get("entry") or "")
        matching_pairs = {
            pair
            for prefix, pair in prefix_routes
            if entry.startswith(prefix)
        }
        raw_value = row.get("rawText")
        concept = row.get("concept")
        if not matching_pairs:
            continue
        if len(matching_pairs) != 1:
            raise SourceUnavailable(
                "Graph direct State crosswalk row has ambiguous source/role: "
                f"{entry}"
            )
        pair = next(iter(matching_pairs))
        if not raw_value or not concept:
            continue
        key = _source_direct_state_key(pair[0], raw_value)
        targets[pair].setdefault(key, set()).add(str(concept))

    result = {
        pair: {
            key: tuple(sorted(concepts))
            for key, concepts in raw_targets.items()
            if concepts
        }
        for pair, raw_targets in targets.items()
    }
    missing = [pair for pair in expected_pairs if not result[pair]]
    if missing:
        labels = ", ".join(f"{source_id}/{role}" for source_id, role in missing)
        raise SourceUnavailable(
            f"Graph direct State crosswalk prewarm is missing: {labels}"
        )
    return result


def _bounded_ctgov_direct_outcome_crosswalk(
    raw_measures: Sequence[str],
) -> dict[str, tuple[str, ...]]:
    """Compatibility view over the prewarmed CT.gov outcome map.

    Graph attribution used to issue one synchronous Fuseki lookup per batch.
    Filtering the process-local source map keeps identical direct mappings
    without leaving an uncancellable query behind when the Graph deadline wins.
    """
    crosswalk = _source_direct_state_crosswalk("ctgov", "outcome")
    return {
        key: crosswalk[key]
        for raw_value in raw_measures
        if raw_value
        and (key := _source_direct_state_key("ctgov", raw_value)) in crosswalk
    }


@lru_cache(maxsize=8)
def _source_direct_state_crosswalk(
    source_id: str,
    role: str,
) -> dict[str, tuple[str, ...]]:
    prewarmed = _PREWARMED_GRAPH_DIRECT_STATE_CROSSWALKS.get(
        (source_id, role)
    )
    if prewarmed is not None:
        return prewarmed
    return _load_source_direct_state_crosswalk(source_id, role)


@lru_cache(maxsize=2)
def _isrctn_direct_state_crosswalk(
    role: str,
) -> dict[str, tuple[str, ...]]:
    """Compatibility wrapper for ISRCTN's two direct State role maps."""
    return _source_direct_state_crosswalk("isrctn", role)


def prewarm_graph_direct_state_crosswalks() -> None:
    """Populate every direct State map with one validated Fuseki snapshot."""
    global _PREWARMED_GRAPH_DIRECT_STATE_CROSSWALKS

    _local_taxonomy_graph("states.ttl")
    loaded = _load_graph_direct_state_crosswalks()
    _PREWARMED_GRAPH_DIRECT_STATE_CROSSWALKS = loaded
    # Replace any maps lazily loaded before startup completed with the single
    # validated snapshot, then populate the public LRU without further I/O.
    _source_direct_state_crosswalk.cache_clear()
    _isrctn_direct_state_crosswalk.cache_clear()
    for source_id, roles in _DIRECT_STATE_CROSSWALK_PREFIXES.items():
        for role in roles:
            crosswalk = _source_direct_state_crosswalk(source_id, role)
            logger.info(
                "Pre-warmed %s direct %s State map (%d raw terms)",
                source_id,
                role,
                len(crosswalk),
            )


def prewarm_direct_intervention_crosswalks() -> None:
    """Populate direct-map caches before FastAPI starts serving requests."""
    _local_taxonomy_graph("interventions.ttl")
    for source_id in _DIRECT_INTERVENTION_CROSSWALK_PREFIXES:
        crosswalk = _source_direct_intervention_crosswalk(source_id)
        logger.info(
            "Pre-warmed %s direct intervention map (%d raw terms)",
            source_id,
            len(crosswalk),
        )


def _source_direct_intervention_key(source_id: str, raw_value: object) -> str:
    """Mirror each registry's approved raw-text matching semantics."""
    value = str(raw_value)
    if source_id == "who-ictrp":
        return value
    return v1._normalize_xwalk_key(value)


def _source_direct_state_key(source_id: str, raw_value: object) -> str:
    """Mirror each registry's approved direct-State literal semantics."""
    value = str(raw_value)
    if source_id in {"ctgov", "who-ictrp"}:
        return value
    return v1._normalize_xwalk_key(value)


def _mapped_source_intervention_uris(
    source_id: str,
    crosswalk: Mapping[str, str | Sequence[str]],
    raw_value: object,
) -> tuple[str, ...]:
    mapped = crosswalk.get(_source_direct_intervention_key(source_id, raw_value))
    if isinstance(mapped, str):
        values: Sequence[str] = (mapped,)
    else:
        values = mapped or ()
    return tuple(dict.fromkeys(str(uri) for uri in values if uri))


def _mapped_source_state_uris(
    source_id: str,
    crosswalk: Mapping[str, str | Sequence[str]],
    raw_value: object,
) -> tuple[str, ...]:
    mapped = crosswalk.get(_source_direct_state_key(source_id, raw_value))
    if isinstance(mapped, str):
        values: Sequence[str] = (mapped,)
    else:
        values = mapped or ()
    return tuple(dict.fromkeys(str(uri) for uri in values if uri))


def _mapped_intervention_uris(
    crosswalk: Mapping[str, str | Sequence[str]], raw_value: object
) -> tuple[str, ...]:
    mapped = crosswalk.get(v1._normalize_xwalk_key(str(raw_value)))
    if isinstance(mapped, str):
        values: Sequence[str] = (mapped,)
    else:
        values = mapped or ()
    return tuple(dict.fromkeys(str(uri) for uri in values if uri))


def _mapped_state_uris(
    crosswalk: Mapping[str, str | Sequence[str]], raw_value: object
) -> tuple[str, ...]:
    mapped = crosswalk.get(v1._normalize_xwalk_key(str(raw_value)))
    if isinstance(mapped, str):
        values: Sequence[str] = (mapped,)
    else:
        values = mapped or ()
    return tuple(dict.fromkeys(str(uri) for uri in values if uri))


def _direct_taxonomy_matches(
    candidates: Sequence[tuple[str, str]],
    selected_root: str | None,
    taxonomy_name: str,
) -> list[tuple[str, str]]:
    """Retain every distinct direct mapping within an optional selected root."""
    unique = {uri: label for uri, label in candidates if uri}
    if selected_root is None:
        return sorted(unique.items())
    graph = _local_taxonomy_graph(taxonomy_name)
    retained: list[tuple[str, str]] = []
    for uri, label in unique.items():
        node = URIRef(uri)
        frontier = [node]
        seen: set[URIRef] = set()
        while frontier:
            current = frontier.pop(0)
            if current in seen:
                continue
            seen.add(current)
            frontier.extend(
                URIRef(parent)
                for parent in graph.objects(current, SKOS.broader)
            )
        if uri == selected_root or URIRef(selected_root) in seen:
            retained.append((uri, label))
    return sorted(retained)


def _direct_state_role_matches(
    condition_candidates: Sequence[tuple[str, str]],
    outcome_candidates: Sequence[tuple[str, str]],
    spec: Mapping[str, str],
) -> tuple[
    list[tuple[str, str]],
    list[tuple[str, str]],
    list[tuple[str, str]],
]:
    """Filter direct State coordinates without conflating registry roles.

    Explicit condition and outcome axes contribute roots only to their
    corresponding raw fields.  A canonical State root contributes to both
    roles, so mixed requests preserve the union of State and explicit roots.
    """
    condition_roots = tuple(
        dict.fromkeys(
            root
            for root in (spec.get("state"), spec.get("condition"))
            if root
        )
    )
    outcome_roots = tuple(
        dict.fromkeys(
            root
            for root in (spec.get("state"), spec.get("outcome"))
            if root
        )
    )
    dynamic_state_projection = not condition_roots and not outcome_roots
    include_condition = bool(condition_roots) or dynamic_state_projection
    include_outcome = bool(outcome_roots) or dynamic_state_projection

    def matches_any_root(
        candidates: Sequence[tuple[str, str]],
        roots: Sequence[str],
    ) -> list[tuple[str, str]]:
        if not roots:
            return _direct_taxonomy_matches(candidates, None, "states.ttl")
        return sorted(
            {
                uri: label
                for root in roots
                for uri, label in _direct_taxonomy_matches(
                    candidates,
                    root,
                    "states.ttl",
                )
            }.items()
        )

    condition_matches = (
        matches_any_root(
            condition_candidates,
            condition_roots,
        )
        if include_condition
        else []
    )
    outcome_matches = (
        matches_any_root(
            outcome_candidates,
            outcome_roots,
        )
        if include_outcome
        else []
    )
    state_matches = sorted(
        {
            uri: label
            for uri, label in condition_matches + outcome_matches
            if uri
        }.items()
    )
    return condition_matches, outcome_matches, state_matches


def _specific_taxonomy_matches(
    candidates: Sequence[tuple[str, str]],
    selected_root: str | None,
    taxonomy_name: str,
) -> list[tuple[str, str]]:
    """Retain mapped concepts in scope, dropping ancestors of other matches."""
    if not candidates:
        return []
    graph = _local_taxonomy_graph(taxonomy_name)
    unique = {uri: label for uri, label in candidates if uri}
    in_scope: dict[str, str] = {}
    ancestors_by_uri: dict[str, set[str]] = {}
    for uri, label in unique.items():
        node = URIRef(uri)
        frontier = [node]
        seen: set[URIRef] = set()
        while frontier:
            current = frontier.pop(0)
            if current in seen:
                continue
            seen.add(current)
            frontier.extend(
                URIRef(parent)
                for parent in graph.objects(current, SKOS.broader)
            )
        ancestors = {str(value) for value in seen if value != node}
        ancestors_by_uri[uri] = ancestors
        if selected_root is None or uri == selected_root or selected_root in ancestors:
            in_scope[uri] = label
    retained = [
        (uri, label)
        for uri, label in in_scope.items()
        if not any(
            uri in ancestors_by_uri.get(other_uri, set())
            for other_uri in in_scope
            if other_uri != uri
        )
    ]
    return sorted(retained)


def _concept_match_sequence(
    value: tuple[str, str]
    | Sequence[tuple[str, str]]
    | None,
) -> tuple[tuple[str, str], ...]:
    """Normalize legacy single and direct-multimap concept values."""
    if value is None:
        return ()
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and all(isinstance(part, str) for part in value)
    ):
        return (value,)
    return tuple(
        (str(match[0]), str(match[1]))
        for match in value
        if len(match) == 2 and match[0]
    )


def _ctgov_presentation(
    studies: Sequence[dict[str, Any]],
    spec: Mapping[str, str],
    labels: Mapping[str, str | None],
    condition_map: Mapping[
        str, tuple[str, str] | Sequence[tuple[str, str]]
    ],
    intervention_map: Mapping[
        str, tuple[str, str] | Sequence[tuple[str, str]]
    ],
    intervention_mesh_map: Mapping[
        str, tuple[str, str] | Sequence[tuple[str, str]]
    ],
    outcome_map: Mapping[
        str, tuple[str, str] | Sequence[tuple[str, str]]
    ],
    *,
    specific_attribution: bool = False,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for original in studies:
        mapped = dict(original)
        names = list(mapped.pop("_intervention_names", ()) or ())
        study_id = mapped.pop("nctId")
        condition_mesh_uris = list(mapped.pop("condition_mesh_uris", ()) or ())
        intervention_mesh_uris = list(
            mapped.pop("intervention_mesh_uris", ()) or ()
        )
        base = {"study_id": study_id, **mapped}
        base.pop("_location_region_uris", None)
        base.pop("_locations", None)
        if not specific_attribution:
            if spec.get("condition"):
                base["condition_concept_uri"] = spec["condition"]
                base["condition_concept"] = labels.get("condition")
            else:
                for mesh_uri in condition_mesh_uris:
                    match = condition_map.get(str(mesh_uri))
                    if match:
                        base["condition_concept_uri"], base["condition_concept"] = match
                        break
            base["outcomes"] = []
            for original_outcome in mapped.get("outcomes") or []:
                outcome = dict(original_outcome)
                match = outcome_map.get(str(outcome.get("measure") or ""))
                if match:
                    outcome["state_concept_uri"], outcome["state_concept"] = match
                base["outcomes"].append(outcome)
            if spec.get("intervention"):
                # A verified study-specific mapping identifies the active source
                # label; never attach the selected concept to a comparator merely
                # because it is the first name returned by the registry.
                scoped = []
                for name in names:
                    value = v1.ctgov_intervention_lookup(intervention_map, study_id, name)
                    for uri, label in _concept_match_sequence(value):
                        if uri == spec["intervention"]:
                            scoped.append((name, uri, label))
                if scoped:
                    for name, uri, label in scoped:
                        results.append({**base, "intervention": name,
                                        "intervention_concept_uri": uri,
                                        "intervention_concept": label})
                    continue
                row = dict(base)
                row["intervention"] = names[0] if names else base.get("intervention")
                row["intervention_concept_uri"] = spec["intervention"]
                row["intervention_concept"] = labels.get("intervention")
                results.append(row)
            elif names:
                for name in names:
                    row = dict(base)
                    row["intervention"] = name
                    match = v1.ctgov_intervention_lookup(intervention_map, study_id, name)
                    if not match and len(names) == 1:
                        for mesh_uri in intervention_mesh_uris:
                            match = intervention_mesh_map.get(str(mesh_uri))
                            if match:
                                break
                    if match:
                        row["intervention_concept_uri"], row["intervention_concept"] = match
                    results.append(row)
            else:
                results.append(base)
            continue

        for key in (
            "state_concept_uri",
            "state_concept",
            "condition_concept_uri",
            "condition_concept",
            "intervention_concept_uri",
            "intervention_concept",
        ):
            base.pop(key, None)
        condition_candidates = [
            match
            for mesh_uri in condition_mesh_uris
            for match in _concept_match_sequence(
                condition_map.get(str(mesh_uri))
            )
        ]
        outcome_candidates: list[tuple[str, str]] = []
        base["outcomes"] = []
        for original_outcome in mapped.get("outcomes") or []:
            outcome = dict(original_outcome)
            measure = str(outcome.get("measure") or "")
            mapped_value = outcome_map.get(measure)
            direct_matches = _concept_match_sequence(mapped_value)
            if not direct_matches and outcome.get("state_concept_uri"):
                direct_matches = (
                    (
                        str(outcome["state_concept_uri"]),
                        str(outcome.get("state_concept") or measure),
                    ),
                )
            outcome.pop("state_concept_uri", None)
            outcome.pop("state_concept", None)
            if len(direct_matches) == 1:
                (
                    outcome["state_concept_uri"],
                    outcome["state_concept"],
                ) = direct_matches[0]
            outcome_candidates.extend(direct_matches)
            base["outcomes"].append(outcome)
        condition_matches, _outcome_matches, state_matches = (
            _direct_state_role_matches(
                condition_candidates,
                outcome_candidates,
                spec,
            )
        )

        candidates: list[tuple[str, str]] = []
        raw_by_uri: dict[str, str] = {}
        for name in names:
            mapped_value = v1.ctgov_intervention_lookup(intervention_map, study_id, name)
            for match in _concept_match_sequence(mapped_value):
                candidates.append(match)
                raw_by_uri.setdefault(match[0], name)
        if not candidates and len(names) == 1:
            for mesh_uri in intervention_mesh_uris:
                for match in _concept_match_sequence(
                    intervention_mesh_map.get(str(mesh_uri))
                ):
                    candidates.append(match)
                    raw_by_uri.setdefault(match[0], names[0])
        direct_intervention_matches = _direct_taxonomy_matches(
            candidates,
            spec.get("intervention"),
            "interventions.ttl",
        )

        presented_states: Sequence[tuple[str | None, str | None]] = (
            state_matches if state_matches else ((None, None),)
        )
        presented_interventions: Sequence[
            tuple[str | None, str | None]
        ] = (
            direct_intervention_matches
            if direct_intervention_matches
            else ((None, None),)
        )
        for state_match in presented_states:
            for intervention_match in presented_interventions:
                row = dict(base)
                if state_match[0]:
                    row["state_concept_uri"], row["state_concept"] = state_match
                    if state_match in condition_matches:
                        row["condition_concept_uri"], row["condition_concept"] = (
                            state_match
                        )
                if intervention_match[0]:
                    row["intervention"] = raw_by_uri.get(
                        intervention_match[0], names[0] if names else None
                    )
                    (
                        row["intervention_concept_uri"],
                        row["intervention_concept"],
                    ) = intervention_match
                results.append(row)
    return results


class CtgovAdapter:
    source_id = "ctgov"

    def __init__(
        self,
        client_factory: Callable[[], httpx.AsyncClient] = httpx.AsyncClient,
        concept_resolver: Callable[[str], Any] = resolve_concept,
    ) -> None:
        self._client_factory = client_factory
        self._concept_resolver = concept_resolver

    async def execute(
        self,
        spec: Mapping[str, str],
        budget: SourceBudget,
        regions: RegionIndex,
    ) -> BranchResult:
        specific_attribution = _GRAPH_ATTRIBUTION.get()
        labels: dict[str, str | None] = {}
        terms: dict[str, list[str]] = {}
        resolutions: dict[str, ConceptResolution] = {}
        for axis in ("state", "condition", "intervention", "outcome"):
            if spec.get(axis):
                resolution = await self._concept_resolver(spec[axis])
                resolutions[axis] = resolution
                labels[axis] = resolution.label
                terms[axis] = list(resolution.terms)
                if not labels[axis] and not terms[axis]:
                    raise SourceUnavailable(f"CT.gov cannot resolve {axis} URI")

        base: dict[str, str] = {
            "format": "json",
            "pageSize": str(UNIQUE_STUDY_LIMIT),
            "fields": CTGOV_V2_FIELDS,
        }
        approximate = False
        if spec.get("state"):
            state_expression, state_approximate = _ctgov_state_expression(
                resolutions["state"]
            )
            base["query.term"] = state_expression
            approximate = approximate or state_approximate
        if spec.get("intervention"):
            available = terms["intervention"]
            chosen = available[:20] or [str(labels["intervention"])]
            terms["intervention"] = chosen
            base["query.intr"] = _quoted_or(chosen)
            approximate = approximate or len(available) > len(chosen)
        if spec.get("outcome"):
            chosen = terms["outcome"][:20] or [str(labels["outcome"])]
            base["query.outc"] = _quoted_or(chosen)
            approximate = approximate or len(terms["outcome"]) > len(chosen)

        condition_filters: list[str | None] = [None]
        if spec.get("condition"):
            meshes = list(resolutions["condition"].mesh_uris)
            filters = v1.ctgov_condition_mesh_filter(meshes)
            if filters:
                condition_filters = list(filters)
            else:
                chosen = terms["condition"][:20] or [str(labels["condition"])]
                base["query.cond"] = _quoted_or(chosen)
                approximate = approximate or len(terms["condition"]) > len(chosen)

        descriptor: RegionDescriptor | None = None
        if spec.get("region"):
            descriptor = regions.describe(spec["region"])
            if descriptor.kind == "unknown":
                raise SourceUnavailable("CT.gov region asset is unavailable")
            if descriptor.kind != "world":
                if not descriptor.country_names:
                    return BranchResult(rows=[])
                region_param = v1._first_api_param("ctgov", "region", "live_api_param") or "query.locn"
                base[region_param] = _quoted_or(descriptor.country_names)

        # ADM1 verification consumes the Contextual crosswalk. Warm
        # it off the event loop; country/group queries use direct source data.
        if descriptor and descriptor.kind == "adm1":
            try:
                await _taxonomy_call(v1.get_ctgov_regions_xwalk)
            except Exception as exc:
                raise SourceUnavailable(
                    "CT.gov contextual region crosswalk unavailable"
                ) from exc

        mapped_by_id: dict[str, dict[str, Any]] = {}
        truncated = approximate
        budget_exhausted = False
        endpoint = v1._source_endpoint("ctgov", v1.CTGOV_API_BASE)
        async with _bounded_slot(v1._LIVE_API_SEMAPHORE, budget):
            async with self._client_factory() as client:
                for filter_index, filter_value in enumerate(condition_filters):
                    page_params = dict(base)
                    if filter_value:
                        page_params["filter.advanced"] = filter_value
                    next_token: str | None = None
                    for page in range(budget.max_pages_per_branch):
                        if next_token:
                            page_params["pageToken"] = next_token
                        try:
                            timeout = budget.claim_request()
                        except SourceBudgetExhausted:
                            budget_exhausted = True
                            break
                        try:
                            response = await client.get(endpoint, params=page_params, timeout=timeout)
                        except httpx.TimeoutException:
                            budget_exhausted = True
                            break
                        if response.status_code == 429 or response.status_code >= 500:
                            raise SourceUnavailable(f"CT.gov returned {response.status_code}")
                        response.raise_for_status()
                        payload = response.json()
                        for raw in payload.get("studies", ()):
                            country_isos = _raw_ctgov_country_isos(raw, regions)
                            iso_hint = next(iter(country_isos)) if len(country_isos) == 1 else ""
                            mapped = v1.map_ctgov_study(
                                raw,
                                iso_hint,
                                include_region_evidence=bool(
                                    descriptor and descriptor.kind == "adm1"
                                ),
                                # Query v2 hydrates Graph outcomes through its
                                # source-scoped direct map below.  Calling the
                                # legacy mapper here would synchronously load
                                # the entire outcome map on the event loop.
                                include_outcome_concepts=False,
                            )
                            if mapped is None:
                                continue
                            mapped["_locations"] = [
                                context.as_evidence()
                                for context in v1.study_locations(raw)
                            ]
                            if descriptor and descriptor.kind != "world":
                                if descriptor.kind == "adm1":
                                    if not regions.verified_match(mapped.get("_location_region_uris", ()), descriptor.uri):
                                        continue
                                elif not country_isos.intersection(descriptor.country_isos):
                                    continue
                            if spec.get("intervention") and not v1._ctgov_study_matches_intervention_terms(
                                terms.get("intervention", ()), mapped.get("_intervention_names", ())
                            ):
                                continue
                            mapped_by_id[mapped["nctId"]] = mapped
                        next_token = payload.get("nextPageToken")
                        if len(mapped_by_id) >= UNIQUE_STUDY_LIMIT:
                            truncated = bool(next_token) or (
                                filter_index + 1 < len(condition_filters)
                            )
                            if filter_index + 1 < len(condition_filters):
                                approximate = True
                            break
                        if not next_token:
                            break
                        if page + 1 >= budget.max_pages_per_branch:
                            budget_exhausted = True
                            break
                    if budget_exhausted or len(mapped_by_id) >= UNIQUE_STUDY_LIMIT:
                        break
            if len(condition_filters) > 1 and len(mapped_by_id) > UNIQUE_STUDY_LIMIT:
                approximate = True

        ordered = list(mapped_by_id.values())
        if len(ordered) > UNIQUE_STUDY_LIMIT:
            ordered = ordered[:UNIQUE_STUDY_LIMIT]
            truncated = True
        if budget_exhausted:
            truncated = True
            approximate = True
        condition_map: Mapping[
            str, tuple[str, str] | Sequence[tuple[str, str]]
        ] = {}
        if ordered and (specific_attribution or not spec.get("condition")):
            condition_mesh_uris = sorted({
                str(mesh_uri)
                for study in ordered
                for mesh_uri in study.get("condition_mesh_uris", ())
            })
            if condition_mesh_uris:
                try:
                    condition_map = await _taxonomy_call(
                        (
                            _local_states_by_mesh_multi
                            if specific_attribution
                            else _local_states_by_mesh
                        ),
                        condition_mesh_uris,
                    )
                except Exception:
                    if specific_attribution:
                        raise SourceUnavailable(
                            "CT.gov direct condition mapping unavailable"
                        )
                    logger.warning(
                        "CT.gov condition presentation enrichment failed",
                        exc_info=True,
                    )
        outcome_map: dict[
            str, tuple[str, str] | Sequence[tuple[str, str]]
        ] = {}
        outcome_measures = sorted(
            {
                str(outcome.get("measure") or "")
                for study in ordered
                for outcome in study.get("outcomes") or ()
                if isinstance(outcome, Mapping) and outcome.get("measure")
            }
        )
        if specific_attribution and outcome_measures:
            try:
                direct_outcomes = await _taxonomy_call(
                    _source_direct_state_crosswalk,
                    "ctgov",
                    "outcome",
                )
                selected_direct_outcomes = {
                    _source_direct_state_key("ctgov", measure): uris
                    for measure in outcome_measures
                    if (
                        uris := _mapped_source_state_uris(
                            "ctgov",
                            direct_outcomes,
                            measure,
                        )
                    )
                }
                direct_outcome_uris = sorted(
                    {
                        uri
                        for uris in selected_direct_outcomes.values()
                        for uri in uris
                    }
                )
                direct_outcome_labels = await _taxonomy_call(
                    _local_state_labels,
                    direct_outcome_uris,
                )
                outcome_map.update(
                    {
                        key: tuple(
                            (
                                uri,
                                direct_outcome_labels.get(uri)
                                or uri.rsplit("/", 1)[-1],
                            )
                            for uri in uris
                        )
                        for key, uris in selected_direct_outcomes.items()
                    }
                )
            except Exception as exc:
                raise SourceUnavailable(
                    "CT.gov direct outcome State map unavailable"
                ) from exc
        intervention_map: dict[
            str, tuple[str, str] | Sequence[tuple[str, str]]
        ] = {}
        intervention_mesh_map: Mapping[
            str, tuple[str, str] | Sequence[tuple[str, str]]
        ] = {}
        selected_intervention_names = sorted(
            {
                str(name)
                for study in ordered
                for name in study.get("_intervention_names", ())
                if name
            }
        )
        if ordered and (
            (specific_attribution and selected_intervention_names)
            or not specific_attribution
        ):
            try:
                # Restore the same purpose-built crosswalk used by the legacy
                # adapter. The earlier implementation passed an empty map here, which
                # erased otherwise valid RUTF and other classifications from
                # condition-only query and graph responses.
                if specific_attribution:
                    direct_map = await _taxonomy_call(
                        _source_direct_intervention_crosswalk, "ctgov"
                    )
                    direct_uris = sorted(
                        {
                            uri
                            for values in direct_map.values()
                            for uri in values
                        }
                    )
                    direct_labels = await _taxonomy_call(
                        _local_intervention_labels, direct_uris
                    )
                    intervention_map.update(
                        {
                            raw: tuple(
                                (
                                    uri,
                                    direct_labels.get(uri)
                                    or uri.rsplit("/", 1)[-1],
                                )
                                for uri in uris
                            )
                            for raw, uris in direct_map.items()
                        }
                    )
                else:
                    intervention_map.update(
                        await _taxonomy_call(
                            v1._load_ctgov_intervention_concept_map
                        )
                    )
            except Exception as exc:
                if specific_attribution:
                    raise SourceUnavailable(
                        "CT.gov direct intervention map unavailable"
                    ) from exc
                logger.warning(
                    "CT.gov intervention crosswalk enrichment failed",
                    exc_info=True,
                )

            unmatched_names = sorted({
                name
                for name in selected_intervention_names
                if name not in intervention_map
                and v1._normalize_xwalk_key(name) not in intervention_map
            })
            if unmatched_names:
                try:
                    for name, match in (
                        await _taxonomy_call(
                            (
                                _local_interventions_by_label_multi
                                if specific_attribution
                                else v1.lookup_intervention_concepts_by_label
                            ),
                            (
                                unmatched_names
                                if specific_attribution
                                else unmatched_names[:UNIQUE_STUDY_LIMIT]
                            ),
                        )
                    ).items():
                        intervention_map.setdefault(name, match)
                except Exception as exc:
                    if specific_attribution:
                        raise SourceUnavailable(
                            "CT.gov direct intervention label mapping unavailable"
                        ) from exc
                    logger.warning(
                        "CT.gov intervention label enrichment failed",
                        exc_info=True,
                    )

            mesh_uris = sorted({
                str(mesh_uri)
                for study in ordered
                if len(study.get("_intervention_names", ())) == 1
                and str(study["_intervention_names"][0]) not in intervention_map
                and v1._normalize_xwalk_key(
                    str(study["_intervention_names"][0])
                ) not in intervention_map
                for mesh_uri in study.get("intervention_mesh_uris", ())
            })
            if mesh_uris:
                try:
                    intervention_mesh_map = await _taxonomy_call(
                        (
                            _local_interventions_by_mesh_multi
                            if specific_attribution
                            else v1.lookup_interventions_by_mesh
                        ),
                        mesh_uris,
                    )
                except Exception as exc:
                    if specific_attribution:
                        raise SourceUnavailable(
                            "CT.gov direct intervention MeSH mapping unavailable"
                        ) from exc
                    logger.warning(
                        "CT.gov intervention MeSH enrichment failed",
                        exc_info=True,
                    )
        return BranchResult(
            rows=_ctgov_presentation(
                ordered,
                spec,
                labels,
                condition_map,
                intervention_map,
                intervention_mesh_map,
                outcome_map,
                specific_attribution=specific_attribution,
            ),
            truncated=truncated,
            approximate=approximate,
            budget_exhausted=budget_exhausted,
        )


def _isrctn_phrase(field: str, value: str) -> str:
    escaped = value.replace('"', '\\"')
    return f'{field}: "{escaped}"' if " " in escaped else f"{field}: {escaped}"


def _isrctn_region_uris(row: Mapping[str, Any]) -> list[str]:
    crosswalk = v1.get_isrctn_regions_xwalk()
    countries = list(row.get("countries") or ())
    locations = list(row.get("study_locations") or ())
    verified: list[str] = []
    if len(countries) == 1:
        for location in locations:
            key = v1._normalize_xwalk_key(f"{location}, {countries[0]}")
            uri = crosswalk.get(key)
            if uri and uri not in verified:
                verified.append(uri)
    for country in countries:
        uri = crosswalk.get(v1._normalize_xwalk_key(country))
        if uri and uri not in verified:
            verified.append(uri)
    return verified


def _stamp_isrctn(
    rows: Sequence[dict[str, Any]],
    spec: Mapping[str, str],
    labels: Mapping[str, str | None],
    condition_crosswalk: Mapping[str, str | Sequence[str]] | None = None,
    condition_labels: Mapping[str, str] | None = None,
    intervention_crosswalk: Mapping[str, str | Sequence[str]] | None = None,
    intervention_labels: Mapping[str, str] | None = None,
    outcome_crosswalk: Mapping[str, str | Sequence[str]] | None = None,
    state_labels: Mapping[str, str] | None = None,
    *,
    specific_attribution: bool = False,
) -> list[dict[str, Any]]:
    condition_crosswalk = condition_crosswalk or {}
    condition_labels = condition_labels or {}
    intervention_crosswalk = intervention_crosswalk or {}
    intervention_labels = intervention_labels or {}
    outcome_crosswalk = outcome_crosswalk or {}
    state_labels = state_labels or condition_labels
    results: list[dict[str, Any]] = []
    for original in rows:
        row = dict(original)
        row["outcomes"] = [
            dict(outcome) if isinstance(outcome, Mapping) else outcome
            for outcome in row.get("outcomes") or ()
        ]
        if not specific_attribution:
            if spec.get("condition"):
                row["condition_concept_uri"] = spec["condition"]
                row["condition_concept"] = labels.get("condition")
            else:
                for description in row.get("condition_descriptions") or ():
                    concept_uris = _mapped_state_uris(
                        condition_crosswalk, description
                    )
                    if concept_uris:
                        concept_uri = concept_uris[0]
                        row["condition_concept_uri"] = concept_uri
                        row["condition_concept"] = (
                            condition_labels.get(concept_uri) or str(description)
                        )
                        break
            row.pop("intervention_concept_uri", None)
            row.pop("intervention_concept", None)
            candidates = list(row.get("drug_names_list") or ())
            candidates.extend(row.get("intervention_descriptions") or ())
            direct_matches: dict[str, str] = {}
            raw_by_uri: dict[str, str] = {}
            for raw in candidates:
                for concept_uri in _mapped_intervention_uris(
                    intervention_crosswalk, raw
                ):
                    direct_matches.setdefault(
                        concept_uri,
                        intervention_labels.get(concept_uri) or str(raw),
                    )
                    raw_by_uri.setdefault(concept_uri, str(raw))
            for outcome in row.get("outcomes") or []:
                if spec.get("outcome"):
                    outcome["state_concept_uri"] = spec["outcome"]
                    outcome["state_concept"] = labels.get("outcome")
            if direct_matches:
                for concept_uri, concept_label in direct_matches.items():
                    result = dict(row)
                    result["intervention_concept_uri"] = concept_uri
                    result["intervention_concept"] = concept_label
                    result["intervention"] = raw_by_uri[concept_uri]
                    results.append(result)
            else:
                results.append(row)
            continue

        for key in (
            "state_concept_uri",
            "state_concept",
            "condition_concept_uri",
            "condition_concept",
            "intervention_concept_uri",
            "intervention_concept",
        ):
            row.pop(key, None)
        for outcome in row.get("outcomes") or ():
            if isinstance(outcome, dict):
                outcome.pop("state_concept_uri", None)
                outcome.pop("state_concept", None)

        condition_candidates: list[tuple[str, str]] = []
        for description in row.get("condition_descriptions") or ():
            for concept_uri in _mapped_state_uris(
                condition_crosswalk, description
            ):
                condition_candidates.append(
                    (
                        concept_uri,
                        condition_labels.get(concept_uri) or str(description),
                    )
                )
        outcome_candidates: list[tuple[str, str]] = []
        for outcome in row.get("outcomes") or ():
            raw_measure = outcome.get("measure") if isinstance(outcome, dict) else outcome
            for concept_uri in _mapped_state_uris(
                outcome_crosswalk, raw_measure
            ):
                outcome_candidates.append(
                    (
                        concept_uri,
                        state_labels.get(concept_uri) or str(raw_measure),
                    )
                )
        condition_matches, _outcome_matches, state_matches = (
            _direct_state_role_matches(
                condition_candidates,
                outcome_candidates,
                spec,
            )
        )

        raw_interventions = list(row.get("drug_names_list") or ())
        raw_interventions.extend(row.get("intervention_descriptions") or ())
        intervention_candidates: list[tuple[str, str]] = []
        raw_by_uri: dict[str, str] = {}
        for raw in raw_interventions:
            for concept_uri in _mapped_intervention_uris(
                intervention_crosswalk, raw
            ):
                intervention_candidates.append(
                    (
                        concept_uri,
                        intervention_labels.get(concept_uri) or str(raw),
                    )
                )
                raw_by_uri.setdefault(concept_uri, str(raw))
        intervention_matches = _direct_taxonomy_matches(
            intervention_candidates,
            spec.get("intervention"),
            "interventions.ttl",
        )
        presented_states = state_matches or [("", "")]
        presented_interventions = intervention_matches or [("", "")]
        for state_uri, state_label in presented_states:
            for intervention_uri, intervention_label in presented_interventions:
                result = dict(row)
                if state_uri:
                    result["state_concept_uri"] = state_uri
                    result["state_concept"] = state_label
                    if any(uri == state_uri for uri, _label in condition_matches):
                        result["condition_concept_uri"] = state_uri
                        result["condition_concept"] = state_label
                if intervention_uri:
                    result["intervention_concept_uri"] = intervention_uri
                    result["intervention_concept"] = intervention_label
                    result["intervention"] = raw_by_uri.get(
                        intervention_uri, result.get("intervention")
                    )
                results.append(result)
    return results


def _local_state_labels(concept_uris: Sequence[str]) -> dict[str, str]:
    """Resolve a bounded set of state labels from the tracked taxonomy asset."""
    if not concept_uris:
        return {}
    graph = _local_taxonomy_graph("states.ttl")
    labels: dict[str, str] = {}
    for uri in concept_uris:
        subject = URIRef(uri)
        value = next(graph.objects(subject, SKOS.prefLabel), None)
        if value is None:
            value = next(graph.objects(subject, RDFS.label), None)
        if value is not None:
            labels[uri] = str(value)
    return labels


@lru_cache(maxsize=1)
def _isrctn_country_norm() -> dict[str, str]:
    import fetch_isrctn

    return fetch_isrctn.load_country_norm()


async def _fetch_isrctn_batch(
    client: httpx.AsyncClient,
    endpoint: str,
    query: str,
    timeout: float,
) -> list[dict[str, Any]]:
    import fetch_isrctn

    response = await client.get(
        endpoint,
        params={"q": query, "limit": str(UNIQUE_STUDY_LIMIT)},
        timeout=timeout,
    )
    if response.status_code == 429 or response.status_code >= 500:
        raise SourceUnavailable(f"ISRCTN returned {response.status_code}")
    response.raise_for_status()
    return fetch_isrctn.parse_isrctn_xml(response.content, _isrctn_country_norm())


class IsrctnAdapter:
    source_id = "isrctn"

    def __init__(
        self,
        client_factory: Callable[[], httpx.AsyncClient] = httpx.AsyncClient,
        fetch_batch: Callable[
            [httpx.AsyncClient, str, str, float], Any
        ] = _fetch_isrctn_batch,
        concept_resolver: Callable[[str], Any] = resolve_concept,
    ) -> None:
        self._client_factory = client_factory
        self._fetch_batch = fetch_batch
        self._concept_resolver = concept_resolver

    async def execute(
        self,
        spec: Mapping[str, str],
        budget: SourceBudget,
        regions: RegionIndex,
    ) -> BranchResult:
        specific_attribution = _GRAPH_ATTRIBUTION.get()
        labels: dict[str, str | None] = {}
        clauses: list[str] = []
        approximate = False
        if spec.get("state"):
            resolution = await self._concept_resolver(spec["state"])
            labels["state"] = resolution.label
            if not labels["state"]:
                raise SourceUnavailable("ISRCTN cannot resolve state URI")
            label = str(labels["state"])
            clauses.append(
                "("
                + _isrctn_phrase("condition", label)
                + " OR "
                + _isrctn_phrase("outcomeMeasures", label)
                + ")"
            )
        for axis, field_name in (
            ("condition", "condition"),
            ("outcome", "outcomeMeasures"),
        ):
            if spec.get(axis):
                resolution = await self._concept_resolver(spec[axis])
                labels[axis] = resolution.label
                if not labels[axis]:
                    raise SourceUnavailable(f"ISRCTN cannot resolve {axis} URI")
                clauses.append(_isrctn_phrase(field_name, str(labels[axis])))

        intervention_chunks: list[str | None] = [None]
        if spec.get("intervention"):
            resolution = await self._concept_resolver(spec["intervention"])
            labels["intervention"] = resolution.label
            terms = list(resolution.terms)
            if not terms and labels["intervention"]:
                terms = [str(labels["intervention"])]
            if not terms:
                raise SourceUnavailable("ISRCTN cannot resolve intervention URI")
            intervention_chunks = v1.isrctn_intervention_query_chunks(terms, max_chunks=None)

        descriptor: RegionDescriptor | None = None
        if spec.get("region"):
            descriptor = regions.describe(spec["region"])
            if descriptor.kind == "unknown":
                raise SourceUnavailable("ISRCTN region asset is unavailable")
            if descriptor.kind != "world":
                if not descriptor.country_names:
                    return BranchResult(rows=[])
                clauses.append(
                    "(" + " OR ".join(
                        _isrctn_phrase("recruitmentCountry", name)
                        for name in descriptor.country_names
                    ) + ")"
                )

        if descriptor and descriptor.kind == "adm1":
            try:
                await _taxonomy_call(v1.get_isrctn_regions_xwalk)
            except Exception as exc:
                raise SourceUnavailable(
                    "ISRCTN contextual region crosswalk unavailable"
                ) from exc

        import fetch_isrctn

        results_by_id: dict[str, dict[str, Any]] = {}
        budget_exhausted = False
        chunks_completed = 0
        async with _bounded_slot(v1._LIVE_API_SEMAPHORE, budget):
            async with self._client_factory() as client:
                for intervention_clause in intervention_chunks:
                    query_parts = list(clauses)
                    if intervention_clause:
                        query_parts.append(f"({intervention_clause})")
                    try:
                        timeout = budget.claim_request()
                    except SourceBudgetExhausted:
                        budget_exhausted = True
                        break
                    try:
                        # execute_source owns the wall-clock bound. Await directly
                        # so cancellation cannot orphan HTTP cleanup outside its permit.
                        batch = await self._fetch_batch(
                            client,
                            v1._source_endpoint("isrctn", fetch_isrctn.DEFAULT_ENDPOINT),
                            " AND ".join(query_parts),
                            timeout,
                        )
                    except asyncio.TimeoutError:
                        budget_exhausted = True
                        break
                    for row in batch:
                        if descriptor and descriptor.kind != "world":
                            country_isos = set(row.get("country_iso_alpha2") or ())
                            country_isos.update(
                                regions.country_isos_for_labels(
                                    row.get("countries") or ()
                                )
                            )
                            if descriptor.kind == "adm1":
                                if not regions.verified_match(_isrctn_region_uris(row), descriptor.uri):
                                    continue
                            elif not country_isos.intersection(descriptor.country_isos):
                                continue
                        study_id = str(row.get("study_id") or "")
                        if study_id:
                            results_by_id[study_id] = row
                    chunks_completed += 1
                    if len(results_by_id) >= UNIQUE_STUDY_LIMIT:
                        break

        truncated = len(results_by_id) >= UNIQUE_STUDY_LIMIT
        if len(intervention_chunks) > chunks_completed and not budget_exhausted:
            approximate = True
        if budget_exhausted:
            truncated = True
            approximate = True
        selected = list(results_by_id.values())[:UNIQUE_STUDY_LIMIT]
        condition_crosswalk: Mapping[str, str | Sequence[str]] = {}
        condition_labels: Mapping[str, str] = {}
        outcome_crosswalk: Mapping[str, str | Sequence[str]] = {}
        state_labels: Mapping[str, str] = {}
        selected_condition_values = [
            description
            for row in selected
            for description in row.get("condition_descriptions") or ()
        ]
        selected_outcome_values = [
            outcome.get("measure")
            if isinstance(outcome, Mapping)
            else outcome
            for row in selected
            for outcome in row.get("outcomes") or ()
        ]
        if selected and (specific_attribution or not spec.get("condition")):
            try:
                if specific_attribution:
                    if selected_condition_values:
                        condition_crosswalk = await _taxonomy_call(
                            _isrctn_direct_state_crosswalk, "condition"
                        )
                    if any(selected_outcome_values):
                        outcome_crosswalk = await _taxonomy_call(
                            _isrctn_direct_state_crosswalk, "outcome"
                        )
                else:
                    condition_crosswalk = await _taxonomy_call(
                        v1.get_isrctn_conditions_xwalk
                    )
                matched_condition_uris = sorted({
                    concept_uri
                    for row in selected
                    for description in row.get("condition_descriptions") or ()
                    for concept_uri in _mapped_state_uris(
                        condition_crosswalk, description
                    )
                })
                matched_outcome_uris = sorted({
                    concept_uri
                    for row in selected
                    for outcome in row.get("outcomes") or ()
                    for concept_uri in _mapped_state_uris(
                        outcome_crosswalk,
                        outcome.get("measure")
                        if isinstance(outcome, dict)
                        else outcome,
                    )
                })
                matched_state_uris = sorted(
                    set(matched_condition_uris).union(matched_outcome_uris)
                )
                condition_labels = await _taxonomy_call(
                    _local_state_labels, matched_condition_uris
                )
                state_labels = await _taxonomy_call(
                    _local_state_labels, matched_state_uris
                )
            except Exception as exc:
                if specific_attribution:
                    raise SourceUnavailable(
                        "ISRCTN direct State maps unavailable"
                    ) from exc
                logger.warning(
                    "ISRCTN condition presentation enrichment failed",
                    exc_info=True,
                )
                condition_crosswalk = {}
                condition_labels = {}
        intervention_crosswalk: Mapping[str, str | Sequence[str]] = {}
        intervention_labels: Mapping[str, str] = {}
        selected_intervention_values = [
            raw
            for row in selected
            for raw in (
                list(row.get("drug_names_list") or ())
                + list(row.get("intervention_descriptions") or ())
            )
        ]
        if selected_intervention_values:
            try:
                intervention_crosswalk = await _taxonomy_call(
                    _isrctn_direct_intervention_crosswalk
                )
                matched_intervention_uris = sorted({
                    concept_uri
                    for raw in selected_intervention_values
                    for concept_uri in _mapped_intervention_uris(
                        intervention_crosswalk, raw
                    )
                })
                intervention_labels = await _taxonomy_call(
                    _local_intervention_labels, matched_intervention_uris
                )
            except Exception as exc:
                raise SourceUnavailable(
                    "ISRCTN intervention map unavailable"
                ) from exc
        return BranchResult(
            rows=_stamp_isrctn(
                selected,
                spec,
                labels,
                condition_crosswalk,
                condition_labels,
                intervention_crosswalk,
                intervention_labels,
                outcome_crosswalk,
                state_labels,
                specific_attribution=specific_attribution,
            ),
            truncated=truncated,
            approximate=approximate,
            budget_exhausted=budget_exhausted,
        )


@lru_cache(maxsize=1)
def default_adapters() -> Mapping[str, SourceAdapter]:
    return {
        "ctgov": CtgovAdapter(),
        "aea": LocalSourceAdapter("aea"),
        "isrctn": IsrctnAdapter(),
        "who-ictrp": LocalSourceAdapter("who-ictrp"),
    }
