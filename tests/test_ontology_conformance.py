"""Public ontology contracts: exercise real bundled SHACL, not application mocks."""
from pathlib import Path
import pytest
from rdflib import Graph
from rdflib.namespace import RDF, SH
from pyshacl import validate

ONTOLOGY = Path(__file__).resolve().parents[1] / 'ontology' / 'ue.ttl'
PREFIX = '@prefix ue: <https://universalevidence.com/ontology/> . @prefix ex: <https://example.org/> . @prefix skos: <http://www.w3.org/2004/02/skos/core#> . '
STATE = 'ex:s a ue:State; skos:prefLabel "State"; skos:inScheme ex:scheme; skos:definition "Example"; ue:bearer ex:person. ex:person a ue:Subject; skos:prefLabel "Person"; skos:inScheme ex:subjects; skos:definition "Example". '
INDICATOR = 'ex:i a ue:Indicator; skos:prefLabel "Indicator"; skos:inScheme ex:scheme; ue:bearer ex:person. '
OTHER = 'ex:j a ue:State; skos:prefLabel "Other"; skos:inScheme ex:scheme; skos:definition "Example"; ue:bearer ex:person. '

def check(body):
    ok, report, _ = validate(Graph().parse(data=PREFIX+body, format='turtle'), shacl_graph=Graph().parse(ONTOLOGY), meta_shacl=True, allow_warnings=True)
    return ok, [str(report.value(n, SH.resultSeverity)).split('#')[-1] for n in report.subjects(RDF.type, SH.ValidationResult)]

@pytest.mark.parametrize('body', [
    STATE, STATE+INDICATOR+'ex:i ue:measures ex:s.',
    STATE+INDICATOR+'ex:s ue:measuredBy ex:i.',
    STATE+INDICATOR+'ex:i ue:measures ex:s. ex:s ue:measuredBy ex:i.',
    STATE+'ex:o a ue:OutcomeMeasurement; ue:ofState ex:s; ue:outcomeStatus "pre-specified".',
    STATE+'ex:j a ue:Intervention; skos:prefLabel "State"; skos:inScheme ex:different; skos:definition "Example".',
])
def test_valid_contracts(body): assert check(body)[0]

@pytest.mark.parametrize('body', [
    STATE+INDICATOR,
    STATE+INDICATOR+'ex:i ue:measures ex:person.',
    STATE+INDICATOR+'ex:i ue:measures ex:s. ex:s skos:broader ex:i.',
    STATE+INDICATOR+'ex:i ue:measures ex:s. ex:s skos:narrower ex:i.',
    'ex:o a ue:OutcomeMeasurement.',
    STATE+'ex:o a ue:OutcomeMeasurement; ue:ofState ex:s; ue:outcomeStatus "INVALID".',
    STATE+'ex:o a ue:OutcomeMeasurement; ue:ofState ex:s; ue:intended "not boolean".',
    STATE+'ex:o a ue:OutcomeMeasurement; ue:ofState ex:person.',
    STATE+'ex:o a ue:OutcomeMeasurement; ue:ofState ex:s; ue:ofIndicator ex:person.',
    STATE+'ex:j a ue:State; skos:prefLabel "State"; skos:inScheme ex:scheme; skos:definition "Example"; ue:bearer ex:person.',
])
def test_invalid_contracts(body): assert not check(body)[0]

@pytest.mark.parametrize('links', [
    'ex:i ue:measures ex:s, ex:j.',
    'ex:i ue:measures ex:s. ex:j ue:measuredBy ex:i.',
    'ex:s ue:measuredBy ex:i. ex:j ue:measuredBy ex:i.',
])
def test_multiple_state_measurement_is_structurally_valid(links):
    assert check(STATE+OTHER+INDICATOR+links)[0]

@pytest.mark.parametrize('links', [
    'ex:i ue:measures ex:s, ex:missing.',
    'ex:i ue:measures ex:s. ex:person ue:measuredBy ex:i.',
    'ex:i ue:measures ex:s, ex:i.',
])
def test_multiple_targets_do_not_bypass_state_type_requirement(links):
    assert not check(STATE+INDICATOR+links)[0]

def test_synonym_collision_is_only_a_warning():
    ok, severities = check(STATE+OTHER+'ex:j skos:altLabel "State".')
    assert ok and severities == ['Warning']

def test_separate_scheme_synonym_is_allowed():
    assert check(STATE+'ex:j a ue:Intervention; skos:prefLabel "Action"; skos:altLabel "State"; skos:inScheme ex:different; skos:definition "Example".') == (True, [])
