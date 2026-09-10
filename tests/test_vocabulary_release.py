"""Release integrity and old-identifier compatibility regressions."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import OWL, RDF, SKOS

from api.main import app

ROOT = Path(__file__).resolve().parents[1]
U = Namespace('https://universalevidence.com/ontology/')
S = Namespace('https://universalevidence.com/vocab/states/')


@pytest.fixture(scope='module')
def core():
    graph = Graph()
    for name in ('states', 'subjects', 'interventions'):
        graph.parse(ROOT / 'vocabularies' / f'{name}.ttl')
    return graph


def test_core_has_no_deprecated_records_or_registry_alignments(core):
    assert not list(core.subjects(OWL.deprecated, None))
    for s, p, o in core:
        if p in (SKOS.exactMatch, SKOS.closeMatch):
            assert 'socialscienceregistry.org/schema' not in str(o)
            assert 'isrctn.com/schema' not in str(o)
        if p in (U.measures, U.measuredBy, U.bearer, SKOS.broader, SKOS.narrower, SKOS.related):
            assert (o, RDF.type, None) in core


def test_removed_uris_redirect_to_defined_replacements(core):
    replacements = json.loads((ROOT / 'api/resources/vocabulary-replacements.json').read_text())
    client = TestClient(app)
    for old, new in replacements.items():
        assert (URIRef(old), None, None) not in core
        assert (URIRef(new), RDF.type, SKOS.Concept) in core
        response = client.get(old.replace('https://universalevidence.com', ''), follow_redirects=False)
        assert response.status_code == 308
        assert response.headers['location'] == new.replace('https://universalevidence.com', '')
        assert client.get(response.headers['location']).status_code == 200
    assert client.get('/vocab/states/NoSuchReleaseConcept', follow_redirects=False).status_code == 404


def test_state_metric_and_procedure_distinctions(core):
    assert (S.Wasting, U.measuredBy, S.SDG222UnderFiveMalnutritionPrevalence) in core
    assert (S.SDG222UnderFiveMalnutritionPrevalence, U.measures, S.Wasting) in core
    for s in (S.Employment, S.LaborForceParticipation):
        assert (s, SKOS.exactMatch, URIRef('https://metadata.un.org/sdg/8.5.2')) not in core
    assert 'Psychiatric Rehabilitation' not in map(str, core.objects(S.MentalHealth, SKOS.altLabel))
    assert (S.EndocrineGlandNeoplasms, SKOS.exactMatch, URIRef('http://id.nlm.nih.gov/mesh/D010235')) not in core
    assert (S.Paraganglioma, SKOS.exactMatch, URIRef('http://id.nlm.nih.gov/mesh/D010235')) in core


def test_hospital_outcomes_do_not_migrate_to_educational_admissions():
    for filename in ('isrctn-outcomes-crosswalk.ttl', 'who-ictrp-outcomes-crosswalk.ttl'):
        graph = Graph().parse(ROOT / 'vocabularies/crosswalks' / filename)
        assert not list(graph.subject_predicates(S.AdmissionsAccess))
        entries = list(graph.subjects(SKOS.closeMatch, S.HospitalServiceUse))
        assert entries
        for entry in entries:
            assert graph.value(entry, U.rawText)
            assert (entry, SKOS.closeMatch, S.EducationalAdmissionsAccess) not in graph


def test_search_and_graph_request_builders_resolve_retired_identifiers():
    from api.routes.query import build_axes_dict
    from api.routes.query_v2 import build_query_v2_request
    from api.routes.graph_v2 import _graph_query

    old = str(S.AdmissionsAccess)
    new = str(S.EducationalAdmissionsAccess)
    assert build_axes_dict(condition=old)['condition'] == new
    options = dict(
        state=[old], condition=[], intervention=[], outcome=[], region=[],
        condition_logic='or', intervention_logic='or', outcome_logic='or', region_logic='or', state_logic='or',
    )
    for builder in (build_query_v2_request, _graph_query):
        legacy = builder(**options)
        current = builder(**dict(options, state=[new]))
        assert legacy == current
