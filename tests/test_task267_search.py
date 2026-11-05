"""Intervention discovery preserves all State roles and scopes stored evidence."""
import copy
import json
import sys
from pathlib import Path
from rdflib import Dataset, Namespace, URIRef, Literal, RDF
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import query_v2 as q
from compact_results import select_query
from live_results import assemble
P='https://universalevidence.com/'
I=P+'vocab/interventions/Example'
A=P+'vocab/states/ExampleA'
B=P+'vocab/states/ExampleB'


def test_ctgov_preserves_multiple_states_and_distinct_roles():
    studies=[{'nctId':'NCT12345678','source':'CT.gov','condition_mesh_uris':['mesh1'],
              '_intervention_names':['Treatment'],'outcomes':[{'measure':'Score'}]}]
    rows=q._ctgov_presentation(studies,{'intervention':I},{'intervention':'Treatment'},
        {'mesh1':[(A,'A'),(B,'B')]},{'Treatment':(I,'Treatment')},{},{'Score':[(A,'A'),(B,'B')]})
    actual=q.dedupe_presentation(rows)[0]['state_mappings']
    assert {(m['uri'],m['role']) for m in actual}=={(s,r) for s in (A,B) for r in ('condition','outcome')}


def test_local_rows_union_states_without_duplicate_studies():
    rows=q._local_rows('aea',[{'study_id':'AEARCTR-0014624','study':'urn:one','matchedInterventionUri':I,
        'mappedState':state,'mappedStateLabel':state[-1],'stateRole':role}
        for state,role in [(A,'condition'),(B,'outcome')]],{'intervention':I})
    merged=q.dedupe_presentation(rows)
    assert len(merged)==1
    assert {(m['uri'],m['role']) for m in merged[0]['state_mappings']}=={(A,'condition'),(B,'outcome')}


def test_generic_compact_state_and_intervention_filters_execute():
    ds=Dataset();c=Namespace(P+'compact-results/');graph=P+'graph/test'
    g=ds.graph(URIRef(graph));study=URIRef(P+'studies/aea/one')
    g.add((study,c.metadata,Literal('{}')))
    for name,states in [('one',[A,B]),('two',[A])]:
        r=URIRef(P+'results/'+name)
        for triple in [(r,RDF.type,c.Result),(r,c.study,study),(r,c.row,Literal(name)),(r,c.matchesIntervention,URIRef(I))]:g.add(triple)
        for state in states:g.add((r,c.state,URIRef(state)))
    def found(values,logic={}):
        return {str(r.row) for r in ds.query(select_query(q.canonical_query(values,logic),graph))}
    assert found({'intervention':[I]})=={'one','two'}
    assert found({'state':[A,B]}, {'state':'and'})=={'one'}
    assert found({'state':[A,B]}, {'state':'or'})=={'one','two'}
    assert found({'intervention':[I+'Other']})==set()


def test_saved_stunting_results_join_intervention_search_without_adding_studies(monkeypatch):
    repo=ROOT.parents[3]
    payload=json.loads((repo/'ops/task266/intervention-search.json').read_text())
    selected=json.loads((repo/'ops/task266/stunting-search.json').read_text())
    rows=selected['reviewed_results']['rows']
    state=P+'vocab/states/Stunting'
    for r in rows:r['states']=[state]
    identities={r['study'] for r in rows}
    metadata={s['study_identity']:s for s in selected['results'] if s.get('study_identity') in identities}
    # Metadata identities/aliases are from the accepted State-selected response.
    local={'status':'available','graph':P+'graph/test','rows':rows,'studies':list(metadata.values())}
    monkeypatch.setattr(q,'_local_state_labels',lambda _: {state:'Stunting'})
    before={(s['source'],s['study_id']) for s in payload['results']}
    out=assemble(payload,q.canonical_query({'intervention':[P+'vocab/interventions/OralNutritionalSupplementation']},{}),local)
    assert len(out['reviewed_results']['rows'])==4
    assert {(s['source'],s['study_id']) for s in out['results']}==before
    assert any(m=={'uri':state,'label':'Stunting','role':'result'} for s in out['results'] for m in s['state_mappings'])
    # Excluding the study from discovery must also exclude its stored measurements.
    empty=assemble({'results':[],'meta':{}},q.canonical_query({'intervention':[I]},{}),local)
    assert empty['results']==[] and empty['reviewed_results']['rows']==[]



def test_local_detail_query_returns_all_state_bindings():
    ds=Dataset();study=URIRef(P+'studies/aea/example')
    graph=ds.graph(URIRef(q.v1._source_graph_uri('aea',q.v1.AEA_GRAPH_URI)))
    graph.add((study,URIRef(q.v1.DCTERMS+'identifier'),Literal('AEARCTR-1234567')))
    graph.add((study,URIRef(q.v1.DCTERMS+'title'),Literal('Example')))
    for role,state in [('condition',A),('outcome',B)]:
        graph.add((study,URIRef(q.v1._AXIS_MATCH_PRED[role]),URIRef(state)))
    bindings=list(ds.query(q._local_detail_query('aea',[str(study)],{'intervention':I})))
    assert {(str(r.mappedState),str(r.stateRole)) for r in bindings}=={(A,'condition'),(B,'outcome')}


def test_isrctn_preserves_multi_target_condition_and_outcome_crosswalks():
    rows=q._stamp_isrctn([{'source':'ISRCTN','study_id':'ISRCTN1',
        'condition_descriptions':['Condition text'],'outcomes':[{'measure':'Outcome text'}]}],
        {'intervention':I},{},condition_crosswalk={'Condition text':[A,B]},
        condition_labels={A:'A',B:'B'},outcome_crosswalk={'Outcome text':[B]},state_labels={A:'A',B:'B'})
    assert {(m['uri'],m['role']) for m in rows[0]['state_mappings']}=={(A,'condition'),(B,'condition'),(B,'outcome')}
