"""Runtime source registry projected from vocabularies/sources.ttl."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import DCTERMS, SKOS

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES = REPO_ROOT / "vocabularies" / "sources.ttl"

UE = Namespace("https://universalevidence.com/ontology/")
DCAT = Namespace("http://www.w3.org/ns/dcat#")

AXIS_BY_URI = {
    str(UE.ConditionAxis): "condition",
    str(UE.InterventionAxis): "intervention",
    str(UE.OutcomeAxis): "outcome",
    str(UE.RegionAxis): "region",
}

ROLE_BY_URI = {
    str(UE.KeywordCrosswalk): "keyword_crosswalk",
    str(UE.RawText): "raw_text",
    str(UE.RegionLiteral): "region_literal",
    str(UE.StampedUri): "stamped_uri",
    str(UE.LiveApiParam): "live_api_param",
}


@dataclass(frozen=True)
class AxisMapping:
    """One runtime mapping from a source field to a UE query axis."""

    axis: str
    source_field: str
    query_role: str | None = None
    api_param: str | None = None


@dataclass(frozen=True)
class SourceConfig:
    """Runtime query config for one source dataset."""

    key: str
    uri: str
    title: str | None
    query_strategy: str | None
    graph_uri: str | None
    endpoint_url: str | None
    axis_mappings: tuple[AxisMapping, ...]

    def mappings(
        self,
        axis: str | None = None,
        role: str | None = None,
    ) -> tuple[AxisMapping, ...]:
        mappings = self.axis_mappings
        if axis is not None:
            mappings = tuple(mapping for mapping in mappings if mapping.axis == axis)
        if role is not None:
            mappings = tuple(mapping for mapping in mappings if mapping.query_role == role)
        return mappings

    def first_field(self, axis: str, role: str) -> str | None:
        mappings = self.mappings(axis, role)
        return mappings[0].source_field if mappings else None


def _local_name(uri: URIRef) -> str:
    return str(uri).rstrip("/#").rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def _first_endpoint(graph: Graph, source: URIRef) -> str | None:
    for distribution in graph.objects(source, DCAT.distribution):
        endpoint = graph.value(distribution, DCAT.endpointURL)
        if endpoint is not None:
            return str(endpoint)
    return None


def _axis_mappings(graph: Graph, source: URIRef) -> tuple[AxisMapping, ...]:
    mappings: list[AxisMapping] = []
    for mapping in graph.objects(source, UE.axisMapping):
        axis = graph.value(mapping, UE.axis)
        source_field = graph.value(mapping, UE.sourceField)
        if axis is None or source_field is None:
            continue
        mappings.append(
            AxisMapping(
                axis=AXIS_BY_URI.get(str(axis), str(axis)),
                source_field=str(source_field),
                query_role=ROLE_BY_URI.get(str(graph.value(mapping, UE.queryRole))),
                api_param=str(graph.value(mapping, UE.apiParam))
                if graph.value(mapping, UE.apiParam) is not None
                else None,
            )
        )
    return tuple(mappings)


@lru_cache(maxsize=1)
def load_source_configs(sources_path: str | Path = DEFAULT_SOURCES) -> dict[str, SourceConfig]:
    """Load runtime query configs keyed by source local name."""
    graph = Graph()
    graph.parse(Path(sources_path), format="turtle")

    configs: dict[str, SourceConfig] = {}
    for source in graph.subjects(predicate=UE.queryStrategy):
        key = _local_name(source)
        title = graph.value(source, DCTERMS.title)
        strategy = graph.value(source, UE.queryStrategy)
        graph_uri = graph.value(source, UE.graphUri)
        configs[key] = SourceConfig(
            key=key,
            uri=str(source),
            title=str(title) if title is not None else None,
            query_strategy=_local_name(strategy) if isinstance(strategy, URIRef) else None,
            graph_uri=str(graph_uri) if graph_uri is not None else None,
            endpoint_url=_first_endpoint(graph, source),
            axis_mappings=_axis_mappings(graph, source),
        )
    return configs


def get_source_config(key: str) -> SourceConfig:
    """Return one source config by registry key."""
    configs = load_source_configs()
    try:
        return configs[key]
    except KeyError as exc:
        raise KeyError(f"Source {key!r} is not registered in sources.ttl") from exc


@lru_cache(maxsize=4)
def load_region_label_mapping(regions_path: str | Path) -> dict[str, str]:
    """Return country/region labels and ISO aliases mapped to ISO alpha-2 codes.

    Countries now live in the GeoNames country mirror; regions.ttl only carries
    UE-owned grouping concepts. Keep the existing ISO-returning API for live
    source adapters by reading both files when the mirror is present.
    """
    graph = Graph()
    regions_path = Path(regions_path)
    graph.parse(regions_path, format="turtle")
    country_mirror = regions_path.parent / "mirrors" / "geonames-countries-mirror.ttl"
    if country_mirror.exists():
        graph.parse(country_mirror, format="turtle")

    mapping: dict[str, str] = {}
    for concept in graph.subjects(SKOS.prefLabel, None):
        iso = str(graph.value(concept, UE.iso3166Alpha2) or "")
        if not iso:
            continue
        labels = (
            list(graph.objects(concept, SKOS.prefLabel))
            + list(graph.objects(concept, SKOS.altLabel))
            + list(graph.objects(concept, UE.iso3166Alpha2))
            + list(graph.objects(concept, UE.iso3166Alpha3))
        )
        for label in labels:
            text = str(label).strip()
            if text:
                mapping[text] = iso
    return mapping


@lru_cache(maxsize=4)
def load_region_uri_mapping(regions_path: str | Path) -> dict[str, str]:
    """Return country labels and ISO aliases mapped to canonical region URIs.

    For countries this returns GeoNames URIs from the country mirror. It is
    intentionally separate from load_region_label_mapping, which preserves the
    ISO-returning contract used by live source adapters.
    """
    graph = Graph()
    regions_path = Path(regions_path)
    graph.parse(regions_path, format="turtle")
    country_mirror = regions_path.parent / "mirrors" / "geonames-countries-mirror.ttl"
    if country_mirror.exists():
        graph.parse(country_mirror, format="turtle")

    mapping: dict[str, str] = {}
    for concept in graph.subjects(SKOS.prefLabel, None):
        iso = str(graph.value(concept, UE.iso3166Alpha2) or "")
        if not iso:
            continue
        labels = (
            list(graph.objects(concept, SKOS.prefLabel))
            + list(graph.objects(concept, SKOS.altLabel))
            + list(graph.objects(concept, UE.iso3166Alpha2))
            + list(graph.objects(concept, UE.iso3166Alpha3))
        )
        for label in labels:
            text = str(label).strip()
            if text:
                mapping[text] = str(concept)
    return mapping
