"""Migration invariants: every public identity resolves; evidence values are unchanged."""
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient
from rdflib import Graph, URIRef, Namespace, Literal
from rdflib.namespace import RDF, SKOS, DCTERMS
from api.main import app
from api.routes.vocab import VOCABULARIES, _load_graph
from api.vocabulary_compat import indicator_migrations, canonicalize_payload, canonicalize_indicator_graph
from scripts.refresh_sitemap import refresh, NS

ROOT = Path(__file__).resolve().parents[1]
U = Namespace('https://universalevidence.com/ontology/')
I = Namespace('https://universalevidence.com/vocab/indicators/')
S = Namespace('https://universalevidence.com/vocab/states/')
client = TestClient(app)


def test_all_indicators_have_own_identity_scheme_and_no_state_type():
    g = _load_graph(VOCABULARIES['indicators'])
    mapping = indicator_migrations()
    terms = set(g.subjects(RDF.type, U.Indicator))
    assert len(mapping) == len(terms) == 879
    assert terms == {URIRef(v) for v in mapping.values()}
    assert (URIRef(I), RDF.type, SKOS.ConceptScheme) in g
    for term in terms:
        assert set(g.objects(term, SKOS.inScheme)) == {URIRef(I)}
        assert (term, RDF.type, U.State) not in g
    assert not any(isinstance(x, URIRef) and str(x) in mapping for triple in g for x in triple)
    states = _load_graph(VOCABULARIES['states'])
    assert not set(states.subjects(RDF.type, U.Indicator))
    # Existing release defects are preserved, not relabeled as newly migrated indicators.
    unresolved = {str(i) for _, i in states.subject_objects(U.measuredBy) if i not in terms}
    assert unresolved == {str(S.ScreenPositivePrevalence), str(S.EducationalStatus)}


def test_every_old_url_redirects_and_every_new_url_returns_its_own_rdf():
    for old, new in indicator_migrations().items():
        response = client.get(old, headers={'accept': 'text/html'}, follow_redirects=False)
        assert response.status_code == 308, old
        assert response.headers['location'] == new.removeprefix('https://universalevidence.com')
        result = client.get(response.headers['location'], headers={'accept': 'text/html'})
        assert result.status_code == 200, new
        assert result.headers['content-type'].startswith('text/turtle')
        graph = Graph().parse(data=result.text, format='turtle')
        assert (URIRef(new), RDF.type, U.Indicator) in graph
        assert '<!doctype html' not in result.text.lower()


def test_machine_representations_and_mapping_download():
    for accept in ('text/turtle', 'application/ld+json', 'application/json'):
        response = client.get('/vocab/states/AgriculturalYield', headers={'accept': accept})
        assert response.status_code == 200
        assert response.headers['content-type'].startswith(accept)
    response = client.get('/vocab/indicator-migrations')
    mapping = Graph().parse(data=response.text, format='turtle')
    assert len(mapping) == 879
    assert (S.AgriculturalYield, DCTERMS.isReplacedBy, I.AgriculturalYield) in mapping
    assert client.get('/vocab/indicators/MissingIndicator').status_code == 404
    assert client.get('/vocab/states/MissingIndicator').status_code == 404


def test_browse_sitemap_and_state_pages_exclude_indicators():
    assert '/browse/indicators' not in client.get('/browse').text
    for page in range(1, 10):
        response = client.get(f'/browse/indicators?page={page}', follow_redirects=False)
        assert response.status_code == 308
        assert response.headers['location'] == '/browse'
    old = str(S.AgriculturalYield)
    xml = f'<urlset xmlns="{NS}"><url><loc>{old}</loc></url><url><loc>https://universalevidence.com/about.html</loc></url></urlset>'
    urls = {x.text for x in ET.fromstring(refresh(xml)).iter(f'{{{NS}}}loc')}
    assert old not in urls
    assert not any('/browse/indicators' in u for u in urls)
    assert not any('/vocab/indicators/' in u for u in urls)
    assert 'https://universalevidence.com/about.html' in urls
    page = client.get('/vocab/states/AgriculturalProductivity', headers={'accept':'text/html'})
    assert page.status_code == 200 and 'indicators-title' not in page.text
    assert '<h2>Measures</h2>' not in page.text


def test_historical_graph_adapter_preserves_measurements_and_states():
    old = Graph()
    old.add((URIRef('urn:result'), U.ofIndicator, S.AgriculturalYield))
    old.add((URIRef('urn:result'), U.ofState, S.AgriculturalProductivity))
    old.add((URIRef('urn:result'), U.value, Literal('12.34')))
    canonicalize_indicator_graph(old)
    assert len(old) == 3
    assert (URIRef('urn:result'), U.ofIndicator, I.AgriculturalYield) in old
    assert (URIRef('urn:result'), U.ofState, S.AgriculturalProductivity) in old
    assert (URIRef('urn:result'), U.value, Literal('12.34')) in old
    payload = {'indicator': str(S.AgriculturalYield), 'state': str(S.AgriculturalProductivity),
               'value': 12.34, 'indicator_labels': {str(S.AgriculturalYield): 'Yield'}}
    result = canonicalize_payload(payload)
    assert result['indicator'] == str(I.AgriculturalYield)
    assert result['state'] == payload['state'] and result['value'] == payload['value']
    assert result['indicator_labels'] == {str(I.AgriculturalYield): 'Yield'}
    assert payload['indicator'] == str(S.AgriculturalYield)
