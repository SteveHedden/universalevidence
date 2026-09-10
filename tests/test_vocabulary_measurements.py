"""Regression checks for the vocabulary and synthetic measurement examples."""
from pathlib import Path
import pytest
from rdflib import Graph,Namespace,URIRef,Literal
from rdflib.namespace import RDF,SH
from pyshacl import validate
R=Path(__file__).resolve().parents[1]
U=Namespace('https://universalevidence.com/ontology/');S=Namespace('https://universalevidence.com/vocab/states/')
@pytest.fixture(scope='module')
def core():
    g=Graph().parse(R/'vocabularies/states.ttl');g.parse(R/'vocabularies/subjects.ttl');return g

def test_fetal_weight_retains_both_applicable_states_in_rdf_queries(core):
    expected={S.FetalGrowthRestriction,S.FetalMacrosomia}
    assert set(core.objects(S.FetalWeight,U.measures))==expected
    found={row.state for row in core.query("SELECT DISTINCT ?state WHERE { <https://universalevidence.com/vocab/states/FetalWeight> (<https://universalevidence.com/ontology/measures>|^<https://universalevidence.com/ontology/measuredBy>) ?state }")}
    assert found==expected
    for state in expected:
        assert (state,U.measuredBy,S.FetalWeight) in core
    # The metric link does not add a subsumption edge between the conditions.
    from rdflib.namespace import SKOS
    assert not (S.FetalGrowthRestriction,SKOS.broader,S.FetalMacrosomia) in core

def test_indicator_targets_are_states_and_reviewed_pairs_are_reciprocal(core):
    for name in ['FetalWeight','LiteracyRate','EarlyGradeReadingAssessment','ContraceptivePrevalence','WDISPDYNCONMQ2ZS','SDG153NationalDisasterRiskStrategyAdoption','SDG1681DevelopingCountryVotingRightsShare','SDG841MaterialFootprint','AlertFatigueRate','MedicalOveruseRate']:
        i=S[name];forward=set(core.objects(i,U.measures));reverse=set(core.subjects(U.measuredBy,i))
        assert forward and forward==reverse,name
        assert all((s,RDF.type,U.State) in core for s in forward)
    assert (S.DisasterLoss,RDF.type,U.State) in core
    assert (S.ServiceSatisfaction,RDF.type,U.State) in core
    assert (S.ContraceptivePrevalence,RDF.type,U.Indicator) in core

@pytest.fixture(scope='module')
def record_shapes():
    shapes=Graph().parse(R/'ontology/ue.ttl')
    # Reference concepts carry types here; their full conformance is checked on the core union.
    for shape in list(shapes.subjects(RDF.type,SH.NodeShape)):
        if shape not in [U.EvidenceShape,U.ObservedOutcomeShape]:
            for pred in [SH.targetClass,SH.targetSubjectsOf,SH.targetObjectsOf]:shapes.remove((shape,pred,None))
    return shapes

def test_synthetic_normalized_records_and_missing_provenance(record_shapes):
    g=Graph().parse(R/'tests/fixtures/ontology_conformance/normalized-studies.ttl')
    assert len(set(g.subjects(RDF.type,U.Evidence)))==4
    assert validate(g,shacl_graph=record_shapes,meta_shacl=True,allow_warnings=True)[0]
    for prop in [U.source,U.studyId]:
        broken=Graph()
        for t in g:broken.add(t)
        record=next(broken.subjects(RDF.type,U.Evidence));broken.remove((record,prop,None))
        assert not validate(broken,shacl_graph=record_shapes,allow_warnings=True)[0]

def test_normalized_outcome_selects_specific_state_of_shared_metric(record_shapes,core):
    # Synthetic outcome fixture, not an assertion of findings in the representative registry studies.
    body='''@prefix ue: <https://universalevidence.com/ontology/> .
      @prefix state: <https://universalevidence.com/vocab/states/> .
      <https://example.org/outcome> a ue:OutcomeMeasurement;
        ue:ofState state:FetalGrowthRestriction; ue:ofIndicator state:FetalWeight;
        ue:outcomeStatus "pre-specified" .'''
    g=Graph().parse(data=body,format='turtle')
    for t in core.triples((None,RDF.type,None)):g.add(t)
    assert validate(g,shacl_graph=record_shapes,allow_warnings=True)[0]
    g.remove((None,U.ofState,None))
    assert not validate(g,shacl_graph=record_shapes,allow_warnings=True)[0]
