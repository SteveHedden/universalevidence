from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import DCAT, DCTERMS, RDF, SKOS
from starlette.requests import Request

import api.main as api_main
import api.routes.vocab as vocab_routes
from api.routes.vocab import HTML, JSON, JSON_LD, TURTLE, negotiate_content_type


client = TestClient(api_main.app)


def _request_with_accept(accept: str | None) -> Request:
    headers = []
    if accept is not None:
        headers.append((b"accept", accept.encode("ascii")))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def test_negotiate_content_type_defaults_to_turtle_for_missing_or_generic_accept():
    assert negotiate_content_type(_request_with_accept(None)) == TURTLE
    assert negotiate_content_type(_request_with_accept("*/*")) == TURTLE


def test_negotiate_content_type_supports_rdf_and_simple_json():
    assert negotiate_content_type(_request_with_accept("text/turtle")) == TURTLE
    assert negotiate_content_type(_request_with_accept("application/ld+json")) == JSON_LD
    assert negotiate_content_type(_request_with_accept("application/json")) == JSON


def test_negotiate_content_type_rejects_html_and_unsupported_types():
    assert negotiate_content_type(_request_with_accept("text/html")) is None
    assert negotiate_content_type(_request_with_accept("application/xml")) is None


def test_vocabulary_endpoint_returns_parseable_turtle_by_default():
    response = client.get("/vocab/states")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/turtle")

    graph = Graph()
    graph.parse(data=response.text, format="turtle")
    assert len(graph) > 0


def test_vocabulary_endpoint_returns_parseable_json_ld():
    response = client.get("/vocab/states", headers={"accept": "application/ld+json"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/ld+json")

    graph = Graph()
    graph.parse(data=response.text, format="json-ld")
    assert len(graph) > 0


def test_vocabulary_endpoint_returns_simple_json_records():
    response = client.get("/vocab/states", headers={"accept": "application/json"})

    assert response.status_code == 200
    body = response.json()
    assert body["vocabulary"] == "states"
    assert any(
        term["uri"] == "https://universalevidence.com/vocab/states/ChildMalnutrition"
        and term["label"] == "Child malnutrition"
        for term in body["terms"]
    )


def test_sources_json_uses_flexible_non_skos_shape():
    response = client.get("/vocab/sources/ctgov", headers={"accept": "application/json"})

    assert response.status_code == 200
    body = response.json()
    assert body["uri"] == "https://universalevidence.com/vocab/sources/ctgov"
    assert body["title"] == "ClinicalTrials.gov"
    assert body["description"].startswith("US NLM registry")
    assert body["homepage"] == "https://clinicaltrials.gov"
    assert "http://www.w3.org/ns/dcat#Dataset" in body["type"]


def test_term_route_returns_local_name_resource_only_as_turtle():
    response = client.get(
        "/vocab/states/UnderFiveMortality",
        headers={"accept": "text/turtle"},
    )

    assert response.status_code == 200
    graph = Graph()
    graph.parse(data=response.text, format="turtle")

    subjects = {str(subject) for subject in graph.subjects()}
    assert subjects == {"https://universalevidence.com/vocab/states/UnderFiveMortality"}


def test_source_term_turtle_includes_blank_node_details():
    response = client.get(
        "/vocab/sources/ctgov",
        headers={"accept": "text/turtle"},
    )

    assert response.status_code == 200
    graph = Graph()
    graph.parse(data=response.text, format="turtle")

    source = URIRef("https://universalevidence.com/vocab/sources/ctgov")
    query_binding = URIRef("https://universalevidence.com/ontology/axisMapping")
    bindings = list(graph.objects(source, query_binding))

    assert bindings
    assert all(isinstance(binding, BNode) for binding in bindings)
    assert all(list(graph.triples((binding, None, None))) for binding in bindings)


def test_term_route_returns_local_name_resource_as_json():
    response = client.get(
        "/vocab/states/ChildMalnutrition",
        headers={"accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.json()["uri"] == "https://universalevidence.com/vocab/states/ChildMalnutrition"
    assert response.json()["label"] == "Child malnutrition"


def test_regions_vocabulary_returns_parseable_turtle():
    response = client.get("/vocab/regions", headers={"accept": "text/turtle"})

    assert response.status_code == 200
    graph = Graph()
    graph.parse(data=response.text, format="turtle")
    subjects = set(graph.subjects())
    assert URIRef("https://universalevidence.com/vocab/regions/SubSaharanAfrica") in subjects
    assert URIRef("https://universalevidence.com/vocab/regions/KE") not in subjects


def test_region_term_route_no_longer_returns_retired_country_json():
    response = client.get("/vocab/regions/KE", headers={"accept": "application/json"})

    assert response.status_code == 404


def test_missing_term_returns_404_independent_of_content_type():
    for accept in ("text/turtle", "application/ld+json", "application/json"):
        response = client.get("/vocab/states/NoSuchTerm", headers={"accept": accept})

        assert response.status_code == 404
        assert response.json() == {
            "error": "Term not found",
            "vocabulary": "states",
            "term": "NoSuchTerm",
        }


def test_html_accept_returns_406_not_acceptable_for_vocabulary_listing():
    # Only the term route (/vocab/{vocabulary}/{term}) has an HTML stub --
    # the listing route has no equivalent and should keep 406-ing.
    response = client.get("/vocab/states", headers={"accept": "text/html"})

    assert response.status_code == 406
    assert response.json() == {"error": "Not acceptable"}


def test_negotiate_content_type_allows_html_only_when_flagged():
    assert negotiate_content_type(_request_with_accept("text/html")) is None
    assert negotiate_content_type(_request_with_accept("text/html"), allow_html=True) == HTML
    assert (
        negotiate_content_type(
            _request_with_accept("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            allow_html=True,
        )
        == HTML
    )


def test_term_route_returns_branded_html_for_browser_accept():
    response = client.get(
        "/vocab/states/ChildMalnutrition",
        headers={"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "public, max-age=3600"
    assert response.headers["vary"] == "Accept"

    body = response.text
    assert "<title>Child malnutrition — Conditions &amp; outcomes — Universal Evidence</title>" in body
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in body
    assert '<meta name="description" content="' in body
    assert (
        '<link rel="canonical" '
        'href="https://universalevidence.com/vocab/states/ChildMalnutrition">'
    ) in body
    assert '<link rel="icon" type="image/svg+xml" href="/favicon.svg">' in body
    assert '<a class="brand-link" href="/" aria-label="Universal Evidence home">' in body
    assert '<main class="page-shell">' in body
    assert '<article class="term-card">' in body
    assert '<span class="vocabulary-badge">Conditions &amp; outcomes</span>' in body
    assert "<h1>Child malnutrition</h1>" in body
    assert '<h2 id="definition-title">Definition</h2>' in body
    assert '<h2 id="broader-title">Broader concepts</h2>' in body
    assert "list-style: none" in body
    assert "a:focus-visible" in body
    assert "overflow-wrap: anywhere" in body
    assert "@media (max-width: 600px)" in body
    assert '<button class="copy-uri-button" id="copy-uri" type="button"' in body
    assert 'data-uri="https://universalevidence.com/vocab/states/ChildMalnutrition"' in body
    assert '<code id="canonical-uri">https://universalevidence.com/vocab/states/ChildMalnutrition</code>' in body
    assert 'id="copy-uri-status" role="status" aria-live="polite"' in body
    assert 'navigator.clipboard.writeText(button.dataset.uri)' in body
    assert 'status.textContent = "URI copied."' in body
    assert 'status.textContent = "Could not copy URI. Select and copy it manually."' in body
    # Broader term should render as a real, crawlable link to another
    # /vocab stub page, not into the SPA.
    assert 'href="/vocab/states/Undernutrition"' in body


def test_source_html_uses_source_metadata_without_hierarchy_sections():
    response = client.get("/vocab/sources/ctgov", headers={"accept": "text/html"})

    assert response.status_code == 200
    body = response.text
    assert '<span class="vocabulary-badge">Evidence sources</span>' in body
    assert "<h1>ClinicalTrials.gov</h1>" in body
    assert '<h2 id="description-title">About this source</h2>' in body
    assert '<h2 id="source-details-title">Source details</h2>' in body
    assert 'href="https://clinicaltrials.gov"' in body
    assert 'href="https://clinicaltrials.gov/about-site/terms-conditions"' in body
    assert "Broader concepts" not in body
    assert "Narrower concepts" not in body
    assert "No broader or narrower concepts are recorded." not in body
    assert '<button class="copy-uri-button"' not in body
    assert 'id="copy-uri"' not in body


def test_intervention_term_copy_control_uses_the_exact_visible_canonical_uri():
    canonical_uri = "https://universalevidence.com/vocab/interventions/NutritionEducation"
    response = client.get(
        "/vocab/interventions/NutritionEducation",
        headers={"accept": "text/html"},
    )

    assert response.status_code == 200
    body = response.text
    assert f'<code id="canonical-uri">{canonical_uri}</code>' in body
    assert f'data-uri="{canonical_uri}"' in body
    assert 'aria-describedby="copy-uri-status">Copy URI</button>' in body
    assert 'navigator.clipboard.writeText(button.dataset.uri)' in body
    assert 'role="status" aria-live="polite" aria-atomic="true"' in body


@pytest.mark.parametrize(
    "term",
    [
        "NaturalSystemState",
        "PopulationHealthState",
        "DevelopmentHumanSystemState",
    ],
)
def test_state_collections_never_render_copy_targets(term: str):
    response = client.get(f"/vocab/states/{term}", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert '<button class="copy-uri-button"' not in response.text
    assert 'id="copy-uri"' not in response.text
    assert "navigator.clipboard" not in response.text


def test_state_namespace_indicator_never_renders_a_copy_target():
    response = client.get(
        "/vocab/states/AbnormalInvoluntaryMovementScale",
        headers={"accept": "text/html"},
    )

    assert response.status_code == 200
    assert '<button class="copy-uri-button"' not in response.text
    assert 'id="copy-uri"' not in response.text
    assert "navigator.clipboard" not in response.text


@pytest.mark.parametrize(
    ("vocabulary", "term"),
    [
        ("regions", "SubSaharanAfrica"),
        ("sources", "ctgov"),
    ],
)
def test_non_state_and_non_intervention_terms_never_render_copy_targets(
    vocabulary: str,
    term: str,
):
    response = client.get(
        f"/vocab/{vocabulary}/{term}",
        headers={"accept": "text/html"},
    )

    assert response.status_code == 200
    assert '<button class="copy-uri-button"' not in response.text
    assert 'id="copy-uri"' not in response.text
    assert "navigator.clipboard" not in response.text


def test_source_html_renders_unsafe_homepage_and_license_metadata_inert(monkeypatch):
    graph = Graph()
    subject = URIRef("https://universalevidence.com/vocab/sources/hostile")
    graph.add((subject, DCTERMS.title, Literal("Hostile <source>")))
    graph.add((subject, DCTERMS.description, Literal("A hostile <script> source.")))
    graph.add((subject, DCAT.landingPage, URIRef("javascript:alert(1)")))
    graph.add((subject, DCTERMS.license, URIRef("data:text/html,unsafe")))
    monkeypatch.setattr(vocab_routes, "_load_graph", lambda _config: graph)

    response = client.get("/vocab/sources/hostile", headers={"accept": "text/html"})

    assert response.status_code == 200
    body = response.text
    assert 'href="javascript:' not in body
    assert 'href="data:' not in body
    assert (
        '<span class="source-link-static">'
        "Visit Hostile &lt;source&gt; homepage</span>"
    ) in body
    assert '<span class="source-link-static">View license terms</span>' in body
    assert "A hostile &lt;script&gt; source." in body
    assert "Unsafe <script>" not in body


def test_packaged_geonames_labels_cover_every_tracked_region_child():
    region_graph = Graph().parse(vocab_routes.VOCABULARIES["regions"].path, format="turtle")
    expected_uris = {
        str(value)
        for value in region_graph.objects(None, SKOS.narrower)
        if str(value).startswith("https://sws.geonames.org/")
    }

    assert len(expected_uris) == 216
    assert set(vocab_routes.GEONAMES_COUNTRY_LABELS) == expected_uris
    assert (
        vocab_routes.GEONAMES_COUNTRY_LABELS["https://sws.geonames.org/1036973/"]
        == "Mozambique"
    )


def test_region_html_includes_explicit_geonames_narrower_links():
    response = client.get("/vocab/regions/SubSaharanAfrica", headers={"accept": "text/html"})

    assert response.status_code == 200
    body = response.text
    assert '<span class="vocabulary-badge">Regions</span>' in body
    assert '<section class="term-section definition"' not in body
    assert "Definition</h2>" not in body
    assert "Broader concepts" in body
    assert "Narrower concepts" in body
    assert (
        '<a class="concept-link" href="https://sws.geonames.org/1036973/" rel="external">'
        '<span>Mozambique<span class="sr-only"> (external)</span></span>'
    ) in body
    assert "GeoNames place" not in body

    region_graph = Graph().parse(vocab_routes.VOCABULARIES["regions"].path, format="turtle")
    subject = URIRef("https://universalevidence.com/vocab/regions/SubSaharanAfrica")
    expected_hrefs = {str(value) for value in region_graph.objects(subject, SKOS.narrower)}
    rendered_hrefs = re.findall(
        r'<a class="concept-link" href="(https://sws\.geonames\.org/[0-9]+/)" rel="external">',
        body,
    )

    assert len(expected_hrefs) == 48
    assert len(rendered_hrefs) == 48
    assert len(set(rendered_hrefs)) == 48
    assert set(rendered_hrefs) == expected_hrefs


def test_narrower_values_include_explicit_only_relations():
    graph = Graph()
    parent = URIRef("https://universalevidence.com/vocab/states/ExplicitParent")
    child = URIRef("https://universalevidence.com/vocab/states/ExplicitChild")
    graph.add((parent, SKOS.narrower, child))

    assert vocab_routes._narrower_values(graph, parent) == {str(child)}


def test_narrower_values_include_inverse_only_relations():
    graph = Graph()
    parent = URIRef("https://universalevidence.com/vocab/states/InverseParent")
    child = URIRef("https://universalevidence.com/vocab/states/InverseChild")
    graph.add((child, SKOS.broader, parent))

    assert vocab_routes._narrower_values(graph, parent) == {str(child)}


def test_html_deduplicates_overlapping_explicit_and_inverse_narrower_relations(monkeypatch):
    graph = Graph()
    subject = URIRef("https://universalevidence.com/vocab/states/DeduplicatedParent")
    child = URIRef("https://universalevidence.com/vocab/states/SingleChild")
    graph.add((subject, RDF.type, SKOS.Concept))
    graph.add((subject, SKOS.prefLabel, Literal("Deduplicated parent")))
    graph.add((subject, SKOS.definition, Literal("A test parent.")))
    graph.add((subject, SKOS.narrower, child))
    graph.add((child, SKOS.prefLabel, Literal("Single child")))
    graph.add((child, SKOS.broader, subject))
    monkeypatch.setattr(vocab_routes, "_load_graph", lambda _config: graph)

    response = client.get(
        "/vocab/states/DeduplicatedParent",
        headers={"accept": "text/html"},
    )

    assert response.status_code == 200
    assert "Narrower concepts" in response.text
    assert response.text.count('href="/vocab/states/SingleChild"') == 1


def test_html_does_not_link_unsafe_external_relation_schemes(monkeypatch):
    graph = Graph()
    subject = URIRef("https://universalevidence.com/vocab/states/SafeParent")
    unsafe = URIRef("javascript:alert(1)")
    graph.add((subject, RDF.type, SKOS.Concept))
    graph.add((subject, SKOS.prefLabel, Literal("Safe parent")))
    graph.add((subject, SKOS.definition, Literal("A test parent.")))
    graph.add((subject, SKOS.narrower, unsafe))
    graph.add((unsafe, SKOS.prefLabel, Literal("Unsafe destination")))
    monkeypatch.setattr(vocab_routes, "_load_graph", lambda _config: graph)

    response = client.get("/vocab/states/SafeParent", headers={"accept": "text/html"})

    assert response.status_code == 200
    assert 'href="javascript:' not in response.text
    assert (
        '<span class="concept-link concept-link-static">'
        "<span>Unsafe destination</span></span>"
    ) in response.text


def test_hierarchical_term_with_no_relations_uses_single_empty_state():
    response = client.get(
        "/vocab/states/AbnormalInvoluntaryMovementScale",
        headers={"accept": "text/html"},
    )

    assert response.status_code == 200
    body = response.text
    assert "No broader or narrower concepts are recorded." in body
    assert "Broader concepts</h2>" not in body
    assert "Narrower concepts</h2>" not in body


def test_html_escapes_dynamic_text_and_relation_labels(monkeypatch):
    graph = Graph()
    subject = URIRef("https://universalevidence.com/vocab/states/Escaping")
    parent = URIRef("https://universalevidence.com/vocab/states/Parent")
    graph.add((subject, RDF.type, SKOS.Concept))
    graph.add((subject, SKOS.prefLabel, Literal('Unsafe <script> & "quoted"')))
    graph.add((subject, SKOS.definition, Literal('Definition <img src=x onerror="bad()"> & more')))
    graph.add((subject, SKOS.broader, parent))
    graph.add((parent, SKOS.prefLabel, Literal('Parent <b> & "quoted"')))
    monkeypatch.setattr(vocab_routes, "_load_graph", lambda _config: graph)

    response = client.get("/vocab/states/Escaping", headers={"accept": "text/html"})

    assert response.status_code == 200
    body = response.text
    assert "Unsafe &lt;script&gt; &amp; &quot;quoted&quot;" in body
    assert "Definition &lt;img src=x onerror=&quot;bad()&quot;&gt; &amp; more" in body
    assert "Parent &lt;b&gt; &amp; &quot;quoted&quot;" in body
    assert "Unsafe <script>" not in body
    assert "<img src=x" not in body


def test_successful_term_representations_vary_on_accept():
    for accept in ("text/html", "text/turtle", "application/ld+json", "application/json"):
        response = client.get("/vocab/states/Malnutrition", headers={"accept": accept})

        assert response.status_code == 200
        assert response.headers["vary"] == "Accept"


def test_missing_term_still_returns_real_404_for_html_accept():
    response = client.get("/vocab/states/NoSuchTerm", headers={"accept": "text/html"})

    assert response.status_code == 404
    assert response.json() == {
        "error": "Term not found",
        "vocabulary": "states",
        "term": "NoSuchTerm",
    }


def test_ontology_endpoint_and_term_resolution():
    response = client.get("/ontology", headers={"accept": "application/json"})

    assert response.status_code == 200
    body = response.json()
    assert body["uri"] == "https://universalevidence.com/ontology/"
    assert "https://universalevidence.com/ontology/State" in body["classes"]

    redirect = client.get("/ontology/State", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/ontology/"
    resolved = client.get("/ontology/State", headers={"accept": "text/turtle"})
    assert resolved.status_code == 200
    assert (URIRef("https://universalevidence.com/ontology/State"), None, None) in Graph().parse(data=resolved.text, format="turtle")
    assert client.get("/ontology/NoSuchTerm").status_code == 404


def test_ontology_term_opens_in_a_normal_browser():
    response = client.get("/ontology/State", headers={"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<section id="State">' in response.text
    assert "Version 0.5.0" in response.text
    assert client.get("/ontology/NoSuchTerm", headers={"accept": "text/html"}).status_code == 404
