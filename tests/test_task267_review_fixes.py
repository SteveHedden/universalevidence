"""Regression reproductions for the three Task 267 review findings."""
import asyncio
import copy
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs
import httpx
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT)]
import compact_results as compact
import query_v2 as q
from live_results import assemble
P=compact.PREFIX
A=P+'vocab/states/ExampleA'; B=P+'vocab/states/ExampleB'
I=P+'vocab/interventions/Example'; S=P+'studies/aea/example'
M=P+'results/shared'


def fixture():
    study={'source':'AEA','study_id':'example','study_identity':S,
           'registry_records':[{'source':'aea','record_id':'example'}]}
    row={'study':S,'measurement':M,'indicator':A,'indicator_label':'Example score',
         'states':[A],'value':'0.07','unit':'z-score','direction':'unknown',
         'intervention_labels':{I:'Example intervention'},
         'summary_mapping':{'study':S,'measurement':M,'indicator':A,'unit':'z-score',
                            'tested':[I],'comparator':[],'background':[]}}
    return study,row


def project(monkeypatch, rows, studies=None):
    study,_=fixture();studies=studies or [study]
    monkeypatch.setattr(q,'_local_state_labels',lambda _: {A:'A',B:'B'})
    return assemble({'results':copy.deepcopy(studies),'meta':{}},q.canonical_query({'intervention':[I]},{}),
                    {'status':'available','graph':P+'graph/test','studies':studies,'rows':rows})


def test_identical_payloads_merge_only_projection_states(monkeypatch):
    _,row=fixture();other=copy.deepcopy(row);other['states']=[B]
    original=copy.deepcopy([row,other]);out=project(monkeypatch,[row,other])
    assert len(out['reviewed_results']['rows'])==1
    assert out['reviewed_results']['rows'][0]['states']==[A,B]
    assert not out['reviewed_results']['partial']
    assert [row,other]==original


@pytest.mark.parametrize('field,value',[('value','99'),('unit','cm'),('comparison',{'statistic':'ratio','orientation':'reverse'})])
def test_conflicting_payloads_survive_all_state_scopes_and_are_flagged(monkeypatch,field,value):
    _,row=fixture();other=copy.deepcopy(row);other[field]=value;other['states']=[B]
    out=project(monkeypatch,[row,other]);rows=out['reviewed_results']['rows']
    assert len(rows)==2
    assert {json.dumps(r.get(field),sort_keys=True) for r in rows}=={json.dumps(row.get(field),sort_keys=True),json.dumps(value,sort_keys=True)}
    assert all(r.get('measurement_conflict') is True for r in rows)
    assert all(r['states']==[A,B] for r in rows)
    assert out['reviewed_results']['partial']
    assert any(c['status']=='conflicting_measurement' for c in out['reviewed_results']['coverage'])


def test_shared_measurement_with_different_study_identity_never_coalesces(monkeypatch):
    study,row=fixture();other=copy.deepcopy(row);other['study']=S+'2';other['summary_mapping']['study']=S+'2'
    second={**study,'study_identity':S+'2','study_id':'example2','registry_records':[{'source':'aea','record_id':'example2'}]}
    out=project(monkeypatch,[row,other],[study,second]);rows=out['reviewed_results']['rows']
    assert {r['study'] for r in rows}=={S,S+'2'}
    assert len(rows)==2 and all(r.get('measurement_conflict') for r in rows)


@pytest.mark.parametrize('values',[{'intervention':[I]},{'state':[A]}])
def test_transient_api_conflicts_also_reach_consumer(monkeypatch,values):
    _,row=fixture();row['study']=P+'studies/ctgov/NCT12345678';row.pop('summary_mapping')
    other={**copy.deepcopy(row),'value':'99','states':[B]}
    monkeypatch.setattr(q,'_local_state_labels',lambda _: {A:'A'})
    out=assemble({'results':[{'source':'CT.gov','study_id':'NCT12345678','intervention_concept_uri':I,
        'api_results':{'rows':[row,other],'status':'available'}}],'meta':{}},q.canonical_query(values,{}))
    assert [r['value'] for r in out['reviewed_results']['rows']]==['0.07','99']
    assert all(r.get('measurement_conflict') for r in out['reviewed_results']['rows'])


@pytest.mark.parametrize('values',[{'state':[A]},{'intervention':[I]},{'state':[A],'intervention':[I]}])
@pytest.mark.parametrize('healthy',[True,False])
def test_empty_filtered_results_use_independent_graph_health(monkeypatch,values,healthy):
    queries=[];client=httpx.AsyncClient
    def handler(req):
        query=parse_qs(req.content.decode())['query'][0];queries.append(query)
        return httpx.Response(200,json={'boolean':healthy} if 'ASK' in query else {'results':{'bindings':[]}},request=req)
    monkeypatch.setenv('UE_COMPACT_RESULTS_GRAPH',P+'graph/test')
    monkeypatch.setattr(compact.httpx,'AsyncClient',lambda **kw:client(transport=httpx.MockTransport(handler),**kw))
    out=asyncio.run(compact.read_compact(q.canonical_query(values,{})))
    assert out['status']==('available' if healthy else 'unavailable')
    assert len(queries)==2 and 'ASK' in queries[1]
    assert I not in queries[1] and A not in queries[1]
    assert out['rows']==[]


def test_compact_endpoint_failure_remains_unavailable(monkeypatch):
    client=httpx.AsyncClient
    monkeypatch.setenv('UE_COMPACT_RESULTS_GRAPH',P+'graph/test')
    monkeypatch.setattr(compact.httpx,'AsyncClient',lambda **kw:client(transport=httpx.MockTransport(lambda req:httpx.Response(503,request=req)),**kw))
    assert asyncio.run(compact.read_compact(q.canonical_query({'intervention':[I]},{})))['status']=='unavailable'


def adapter(monkeypatch):
    study=json.loads((ROOT/'tests/fixtures/task265/writing-study.json').read_text())
    async def resolve(uri):return q.ConceptResolution(label='Expressive writing intervention',terms=('Expressive writing intervention',),mesh_uris=())
    monkeypatch.setattr(q.v1,'_load_ctgov_intervention_concept_map',lambda:{})
    monkeypatch.setattr(q.v1,'_load_ctgov_outcome_concept_map',lambda:{})
    monkeypatch.setattr(q.v1,'lookup_intervention_concepts_by_label',lambda _: {})
    monkeypatch.setattr(q,'_local_states_by_mesh_multi',lambda _: {})
    def fail(*args):raise RuntimeError('Temporary taxonomy failure')
    monkeypatch.setattr(q,'_source_direct_state_crosswalk',fail)
    return q.CtgovAdapter(lambda:httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'studies':[study]},request=req))),resolve)


def test_optional_state_enrichment_keeps_discovery_and_marks_partial(monkeypatch):
    rows,meta=asyncio.run(q.execute_source(q.canonical_query({'intervention':[P+'vocab/interventions/ExpressiveWritingIntervention']},{}),adapter(monkeypatch),q.default_region_index()))
    assert {r['study_id'] for r in rows}=={'NCT03751020'}
    assert meta['status']=='included' and meta['coverage']=='partial'
    assert meta['reason']=='state_enrichment_unavailable'
    envelope=q.annotate_completion({'results':rows,'meta':{'sources':{'ctgov':meta}}})
    assert envelope['meta']['execution_status']=='partial'
    assert rows[0]['intervention_concept_uri'].endswith('/ExpressiveWritingIntervention')


def test_required_specific_mapping_still_fails_closed(monkeypatch):
    instance=adapter(monkeypatch)
    token=q._GRAPH_ATTRIBUTION.set(True)
    try:
        with pytest.raises(q.SourceUnavailable,match='outcome State map unavailable'):
            asyncio.run(instance.execute({'intervention':P+'vocab/interventions/ExpressiveWritingIntervention','state':A},q.SourceBudget(),q.default_region_index()))
    finally:q._GRAPH_ATTRIBUTION.reset(token)


def test_empty_healthy_result_scope_does_not_mark_live_search_partial(monkeypatch):
    study,_=fixture()
    out=assemble({'results':[study],'meta':{}},q.canonical_query({'state':[A]},{}),
                 {'status':'available','graph':P+'graph/test','rows':[],'studies':[]})
    assert out['results'] and not out['reviewed_results']['partial']
    assert out['reviewed_results']['local_evidence_status']=='available'


def test_optional_label_failure_retains_known_outcome_identities(monkeypatch):
    instance=adapter(monkeypatch)
    study=json.loads((ROOT/'tests/fixtures/task265/writing-study.json').read_text())
    # Match a source outcome title generically, without requiring its display label.
    measure=study['protocolSection']['outcomesModule']['primaryOutcomes'][0]['measure']
    key=q._source_direct_state_key('ctgov',measure)
    monkeypatch.setattr(q,'_source_direct_state_crosswalk',lambda *args:{key:[A]})
    def fail(*args):raise RuntimeError('Label service unavailable')
    monkeypatch.setattr(q,'_local_state_labels',fail)
    branch=asyncio.run(instance.execute({'intervention':P+'vocab/interventions/ExpressiveWritingIntervention'},q.SourceBudget(),q.default_region_index()))
    assert branch.rows and branch.state_enrichment_partial
    assert any(m['uri']==A and m['role']=='outcome' for row in branch.rows for m in row.get('state_mappings',[]))


def test_optional_enrichment_failure_cannot_bypass_intervention_eligibility(monkeypatch):
    instance=adapter(monkeypatch)
    branch=asyncio.run(instance.execute({'intervention':I},q.SourceBudget(),q.default_region_index()))
    assert branch.rows==[]  # Scoped writing mappings cannot be reattributed to Example.


def test_conflict_marker_and_both_values_survive_rdf_export(monkeypatch):
    from live_results import rows_to_rdf
    from rdflib import Graph,URIRef,Namespace
    _,row=fixture();other={**copy.deepcopy(row),'value':'99'}
    rows=project(monkeypatch,[row,other])['reviewed_results']['rows']
    graph=Graph().parse(data=rows_to_rdf(rows),format='turtle');ue=Namespace(P+'ontology/')
    from decimal import Decimal
    assert {v.toPython() for v in graph.objects(URIRef(M),ue.value)}=={Decimal('0.07'),Decimal('99')}
    assert all(json.loads(str(v))['measurement_conflict'] for v in graph.objects(URIRef(M),ue.comparison))
