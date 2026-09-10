from pathlib import Path

from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, SKOS

REPO_ROOT = Path(__file__).resolve().parents[1]
REGIONS_PATH = REPO_ROOT / "vocabularies" / "regions.ttl"
COUNTRIES_PATH = (
    REPO_ROOT / "vocabularies" / "mirrors" / "geonames-countries-mirror.ttl"
)

UE = Namespace("https://universalevidence.com/ontology/")
UER = Namespace("https://universalevidence.com/vocab/regions/")


def _graph() -> Graph:
    graph = Graph()
    graph.parse(REGIONS_PATH, format="turtle")
    graph.parse(COUNTRIES_PATH, format="turtle")
    return graph


def test_regions_ttl_parses_and_contains_country_concepts():
    graph = _graph()
    country_concepts = [
        subject
        for subject in graph.subjects(RDF.type, UE.Region)
        if graph.value(subject, UE.iso3166Alpha2) is not None
    ]

    assert len(country_concepts) >= 180
    assert all(graph.value(country, UE.iso3166Alpha2) for country in country_concepts)
    assert all(str(country).startswith("https://sws.geonames.org/") for country in country_concepts)
    assert all(graph.value(country, SKOS.inScheme) == URIRef(str(UER)) for country in country_concepts)


def test_regions_ttl_contains_mvp_groupings_with_reciprocal_links():
    graph = _graph()
    groupings = [
        UER.SubSaharanAfrica,
        UER.SouthAsia,
        UER.LatinAmericaCaribbean,
        UER.MiddleEastNorthAfrica,
        UER.EastAsiaPacific,
        UER.EuropeCentralAsia,
    ]

    for grouping in groupings:
        assert (grouping, RDF.type, UE.Region) in graph
        assert graph.value(grouping, RDFS.label) is not None
        narrower = list(graph.objects(grouping, SKOS.narrower))
        assert narrower
        assert all((country, SKOS.broader, grouping) in graph for country in narrower)


def test_known_countries_are_under_sub_saharan_africa():
    graph = _graph()

    countries_by_code = {
        str(graph.value(country, UE.iso3166Alpha2)): country
        for country in graph.subjects(RDF.type, UE.Region)
        if graph.value(country, UE.iso3166Alpha2) is not None
    }
    for code in ["KE", "NG", "TZ"]:
        country = countries_by_code[code]
        assert (country, SKOS.broader, UER.SubSaharanAfrica) in graph
        assert (UER.SubSaharanAfrica, SKOS.narrower, country) in graph


def test_regions_include_generation_metadata_and_alt_labels():
    graph = _graph()

    assert (URIRef(str(UER)), RDF.type, SKOS.ConceptScheme) in graph
    kenya = next(
        country
        for country in graph.subjects(UE.iso3166Alpha2, None)
        if str(graph.value(country, UE.iso3166Alpha2)) == "KE"
    )
    assert graph.value(kenya, SKOS.prefLabel) == graph.value(kenya, RDFS.label)
    assert "KEN" in {str(value) for value in graph.objects(kenya, SKOS.altLabel)}
