"""Structural tests for vocabularies/sources.ttl runtime query mappings."""

from pathlib import Path

import pytest
from rdflib import Graph, Namespace

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCES_PATH = REPO_ROOT / "vocabularies" / "sources.ttl"

UE = Namespace("https://universalevidence.com/ontology/")
UES = Namespace("https://universalevidence.com/vocab/sources/")
RDFS = Namespace("http://www.w3.org/2000/01/rdf-schema#")
AEA = Namespace("https://socialscienceregistry.org/schema#")
ICTRP = Namespace("https://universalevidence.com/source/who-ictrp/schema#")


@pytest.fixture(scope="module")
def sources_graph() -> Graph:
    graph = Graph()
    graph.parse(SOURCES_PATH, format="turtle")
    return graph


def axis_mappings(graph: Graph, source, axis=None, role=None):
    mappings = list(graph.objects(source, UE.axisMapping))
    if axis is not None:
        mappings = [m for m in mappings if graph.value(m, UE.axis) == axis]
    if role is not None:
        mappings = [m for m in mappings if graph.value(m, UE.queryRole) == role]
    return mappings


def test_sources_ttl_parses(sources_graph):
    assert len(sources_graph) > 0


def test_materialized_query_sources_have_graph_uri(sources_graph):
    for source in [UES.aea, UES.wholictrp]:
        assert sources_graph.value(source, UE.graphUri) is not None


def test_aea_runtime_mappings_distinguish_raw_text_and_keyword_crosswalk(sources_graph):
    assert str(sources_graph.value(UES.aea, UE.graphUri)) == "https://universalevidence.com/graph/aea"

    condition_keywords = axis_mappings(
        sources_graph, UES.aea, UE.ConditionAxis, UE.KeywordCrosswalk
    )
    assert len(condition_keywords) == 1
    assert sources_graph.value(condition_keywords[0], UE.sourceField) == AEA.keyword

    intervention_raw = axis_mappings(
        sources_graph, UES.aea, UE.InterventionAxis, UE.RawText
    )
    assert len(intervention_raw) == 1
    assert sources_graph.value(intervention_raw[0], UE.sourceField) == AEA.intervention

    intervention_keywords = axis_mappings(
        sources_graph, UES.aea, UE.InterventionAxis, UE.KeywordCrosswalk
    )
    assert len(intervention_keywords) == 1
    assert sources_graph.value(intervention_keywords[0], UE.sourceField) == AEA.keyword

    outcome_raw = axis_mappings(sources_graph, UES.aea, UE.OutcomeAxis, UE.RawText)
    assert len(outcome_raw) == 1
    assert sources_graph.value(outcome_raw[0], UE.sourceField) == AEA.primaryOutcome


def test_aea_region_mapping_uses_aea_country_literal(sources_graph):
    region_mappings = axis_mappings(sources_graph, UES.aea, UE.RegionAxis, UE.RegionLiteral)
    assert len(region_mappings) == 1
    assert sources_graph.value(region_mappings[0], UE.sourceField) == AEA.country


def test_ctgov_live_api_mappings_expose_endpoint_and_params(sources_graph):
    endpoint = None
    for distribution in sources_graph.objects(UES.ctgov, Namespace("http://www.w3.org/ns/dcat#").distribution):
        endpoint = sources_graph.value(distribution, Namespace("http://www.w3.org/ns/dcat#").endpointURL)
        if endpoint is not None:
            break

    assert str(endpoint) == "https://clinicaltrials.gov/api/v2/studies"

    region_mappings = axis_mappings(sources_graph, UES.ctgov, UE.RegionAxis, UE.LiveApiParam)
    assert len(region_mappings) == 1
    assert str(sources_graph.value(region_mappings[0], UE.apiParam)) == "query.locn"

    condition_mappings = axis_mappings(sources_graph, UES.ctgov, UE.ConditionAxis, UE.LiveApiParam)
    assert len(condition_mappings) == 1
    assert str(sources_graph.value(condition_mappings[0], UE.apiParam)) == "filter.advanced"








def test_who_ictrp_runtime_mappings_use_source_predicates(sources_graph):
    expected_fields = {
        UE.ConditionAxis: ICTRP.condition,
        UE.InterventionAxis: ICTRP.intervention,
        UE.OutcomeAxis: ICTRP.primaryOutcome,
        UE.RegionAxis: ICTRP.countryIsoAlpha2,
    }

    for axis, expected_field in expected_fields.items():
        mappings = axis_mappings(sources_graph, UES.wholictrp, axis)
        assert len(mappings) == 1
        assert sources_graph.value(mappings[0], UE.sourceField) == expected_field




def test_axis_and_query_role_instances_are_declared(sources_graph):
    for resource in [
        UE.ConditionAxis,
        UE.InterventionAxis,
        UE.OutcomeAxis,
        UE.RegionAxis,
        UE.KeywordCrosswalk,
        UE.RawText,
        UE.RegionLiteral,
        UE.StampedUri,
        UE.LiveApiParam,
    ]:
        assert sources_graph.value(resource, RDFS.label) is not None
