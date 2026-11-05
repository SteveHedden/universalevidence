import json
from copy import deepcopy
from pathlib import Path
import pytest
from rdflib import URIRef
from rdflib.namespace import RDF
from scripts import evidence_enrichment as e
from scripts.query_v2 import canonical_query

FIXTURE=Path(__file__).parent/'fixtures/enrichment/dual-registry.json'
@pytest.fixture
def batch(): return json.loads(FIXTURE.read_text())
@pytest.fixture
def db(tmp_path,monkeypatch,batch):
    path=tmp_path/'enrichment.ttl';monkeypatch.setenv('UE_ENRICHMENT_TTL',str(path))
    conn=e.open_store(path)
    with conn: e.apply_batch(conn,batch)
    yield conn
    conn.close()
def query(batch,**kwargs):
    return canonical_query({'state':[batch['findings'][0]['state_uri']],**kwargs},{})

def reviewed(db, proposed):
    """Test fixture for an explicit review against a fresh snapshot (not worker behavior)."""
    from datetime import timedelta
    result=deepcopy(proposed)
    prepared=e.prepare_batch(db,result)
    tokens={'studies':{},'documents':{},'findings':{}}
    for table, values in [('studies',[prepared['study']]),('documents',result.get('documents',[])),('findings',result.get('findings',[]))]:
        for value in values:
            old=e._get(db,table,value['id'])
            if old and old!=value:
                tokens[table][old['id']]=e.digest(old)
                if table=='documents' and old['status']==value['status'] and e._date(value['status_check']['checked_at'])<e._date(old['status_check']['checked_at']):
                    value['status_check']=deepcopy(old['status_check'])
                if table in {'documents','findings'} and old['status']!=value['status']:
                    at='2026-11-01T00:00:00Z'
                    if table=='documents':
                        at=(e._date(old['status_check']['checked_at'])+timedelta(days=1)).isoformat()
                        value['status_check']['checked_at']=at
                    result.setdefault('eligibility_reviews',{}).setdefault(table,{})[old['id']]={'from':old['status'],'to':value['status'],'reviewed_by':'test reviewer','reviewed_at':at,'rationale':'Explicit synthetic fixture transition'}
            if table=='findings' and value.get('replaces'):
                prior=e._get(db,'findings',value['replaces'])
                if prior:tokens[table][prior['id']]=e.digest(prior)
    if any(tokens.values()):result['preconditions']=tokens
    return result

def test_identity_reports_and_idempotence(db,batch):
    with db: e.apply_batch(db,reviewed(db,batch))
    assert len(db.data['events'])==1
    assert len(db.data['studies'])==1
    public=e.public_study(db,batch['study']['id'])
    assert len(public['registry_records'])==2
    assert len(public['reported_findings'])==3
    assert public['reported_findings'][0]['value']==-1.2
    assert public['reported_findings'][0]['significance']=='not statistically significant'
    assert public['reported_findings'][2]['value']==0

def test_retrieval_and_role(db,batch):
    rows,meta=e.retrieve(db,query(batch));assert len(rows)==2
    assert meta['returned_unique_studies']==1
    assert rows[0]['query_matches'][0]['retrieval_basis']=='reported_outcome'
    rows,_=e.retrieve(db,canonical_query({'condition':[batch['findings'][0]['state_uri']]},{}))
    assert rows==[]

def test_unsupported_and_ambiguous(db,batch):
    bad=deepcopy(batch);bad['study']['records'][0]['verification']['basis']='title-similarity';bad['preconditions']=e.revision_preconditions(db,batch['study']['id']);bad['study_replacement_review']={'reviewed_by':'reviewer','reviewed_at':'2026-11-01T00:00:00Z','rationale':'Test invalid evidence'}
    with pytest.raises(ValueError,match='Title similarity'): e.validate_batch(db,bad)
    bad=deepcopy(batch);bad['findings'][0]['sources'][0]['fields'].remove('value')
    with pytest.raises(ValueError,match='Unsupported finding'): e.validate_batch(db,bad)
    bad=deepcopy(batch);bad['findings'][0]['indicator_uri']='https://example.org/growth'
    with pytest.raises(ValueError,match='measures relationship'): e.validate_batch(db,bad)

def test_retraction_preserves_independent_and_history(db,batch):
    old=e.version();change=deepcopy(batch);change['findings']=[]
    change['documents'][0]['status']='retracted';change['documents'][0]['status_check']['result']='retracted'
    with db:e.apply_batch(db,reviewed(db,change))
    public=e.public_study(db,batch['study']['id'])
    assert len(public['reported_findings'])==2 and len(public['finding_history'])==1
    assert e.version()!=old
    change['documents'][1]['status']='retracted';change['documents'][1]['status_check']['result']='retracted'
    with db:e.apply_batch(db,reviewed(db,change))
    assert e.retrieve(db,query(batch))[0]==[]
    assert len(e.public_study(db,batch['study']['id'])['finding_history'])==3

def test_correction_and_rollback(db,batch):
    changed=deepcopy(batch);f=changed['findings'][0];f['replaces']=f['id'];f['id']+='-corrected';f['value']=-0.5
    changed['findings']=[f]
    with db:e.apply_batch(db,reviewed(db,changed))
    assert len(e.public_study(db,batch['study']['id'])['finding_history'])==1
    before=e.version();changed['study']['title']='Rollback'
    with pytest.raises(RuntimeError):
        with db:
            e.apply_batch(db,reviewed(db,changed))
            raise RuntimeError('crash before receipt')
    assert e.version()==before

def test_export_and_backup(db,batch,tmp_path):
    graph=e.export_rdf(db)
    assert (URIRef(batch['study']['id']),RDF.type,e.UE.Study) in graph
    assert len(list(graph.subjects(RDF.type,e.UE.OutcomeMeasurement)))==3
    path=tmp_path/'backup.ttl';e.backup(db.path,path)
    with e.reader(path) as copy: assert len(copy.data['findings'])==3

def test_search_dual_registry_and_enrichment_only(db,batch):
    sources={key:{'status':'included','returned_unique_studies':0} for key in e.SOURCES}
    payload={'results':[],'meta':{'sources':sources,'truncated':False,'approximate':False}}
    result=e.enrich_search(payload,query(batch))
    assert len(result['results'])==1 and result['meta']['returned_unique_studies']==1
    assert result['meta']['sources']['ctgov']['returned_unique_studies']==1
    assert result['meta']['sources']['who-ictrp']['returned_unique_studies']==1
    assert len(result['results'][0]['registry_records'])==2
    payload['results']=[{'source':'CT.gov','study_id':'NCT-FIXTURE','intervention_concept_uri':batch['study']['axes'][0]['uri']}]
    assert len(e.enrich_search(payload,query(batch))['results'])==1

def test_graph_counts_details_and_cache(db,batch,monkeypatch):
    import asyncio
    from api.routes import graph_v2 as g
    from api.query_v2_cache import QueryV2Cache
    q=query(batch);state=batch['findings'][0]['state_uri'];intervention=batch['study']['axes'][0]['uri']
    def forest(values):return {'nodes':[{'id':v,'label':v.rsplit('/',1)[-1]} for v in values],'pairs':[],'omitted':0}
    monkeypatch.setattr(g,'state_forest',lambda _:forest([state]))
    monkeypatch.setattr(g,'intervention_forest',lambda _:forest([intervention]))
    meta={'status':'included','coverage':'full','returned_unique_studies':0,'truncated':False,'approximate':False,'reason':None}
    aggregate=g.build_graph_v2_payload(query=q,condition_taxonomy=forest([state]),intervention_taxonomy=forest([intervention]),local_rows={},live_rows={},source_meta={k:dict(meta) for k in e.SOURCES})
    edge=next(x for x in aggregate['edges'] if x['kind']=='evidence')
    assert aggregate['meta']['returned_unique_studies']==1
    assert edge['weight']==1 and edge['source_counts']=={'ctgov':1,'who-ictrp':1}
    assert edge['support_counts']['mapping_only']==1
    assert all(n['studyCount']==1 for n in aggregate['nodes'])
    async def empty(*args,**kwargs):return [],dict(meta)
    async def regions(*args):return object()
    monkeypatch.setattr(g,'region_index_for_query',regions)
    monkeypatch.setattr(g,'_effective_stored_query',lambda q,r:(q,False))
    monkeypatch.setattr(g,'execute_graph_source',empty)
    monkeypatch.setattr(g,'_local_detail_rows',empty)
    monkeypatch.setattr(g,'default_adapters',lambda:{'ctgov':object()})
    details=asyncio.run(g.execute_edge_details(q,state,intervention,0,1,expected_source_counts=edge['source_counts'],expected_source_membership_digests=edge[g._EDGE_MEMBERSHIP_DIGESTS],cursor_context='fixture'))
    assert len(details['results'])==1 and details['meta']['nextCursor'] is None
    assert details['meta']['source_counts_reconciled'] and details['meta']['source_membership_reconciled']
    assert len(details['results'][0]['registry_records'])==2
    before=QueryV2Cache.canonical_key(q);graph_before=g.graph_cache_identity(q,taxonomy_version='t',dataset_version='d')
    changed=deepcopy(batch);changed['documents'][0]['status']='retracted';changed['documents'][0]['status_check']['result']='retracted';changed['findings']=[]
    with db:e.apply_batch(db,reviewed(db,changed))
    assert QueryV2Cache.canonical_key(q)!=before
    assert g.graph_cache_identity(q,taxonomy_version='t',dataset_version='d')!=graph_before

def test_shacl_and_namespace(db,batch):
    from rdflib import Graph, Namespace
    from rdflib.namespace import RDFS, OWL
    from pyshacl import validate
    ontology=Graph().parse(e.ROOT/'ontology/ue.ttl')
    assert (e.UE.OutcomeMeasurement,RDFS.subClassOf,e.PICO.Outcome) not in ontology
    assert (e.UE.OutcomeSpecification,RDFS.subClassOf,e.PICO.Outcome) in ontology
    old=Namespace('https://data.cochrane.org/ontologies/pico/')
    assert (old.Intervention,OWL.equivalentClass,e.PICO.Intervention) in ontology
    data=e.export_rdf(db)
    # Validate enrichment nodes against the actual referenced taxonomy union.
    focus=list(set(data.subjects()))
    for name in ['states','indicators','subjects','interventions']:
        data.parse(e.VOCABULARY_ROOT/(name+'.ttl'))
    ok,_,message=validate(data,shacl_graph=ontology,allow_warnings=True,focus_nodes=focus)
    assert ok,message
    data.remove((URIRef(batch['findings'][0]['id']),e.UE.comparison,None))
    assert not validate(data,shacl_graph=ontology,allow_warnings=True,focus_nodes=focus)[0]

def test_manifest_version_does_not_reset_store(db,batch):
    from scripts.load_fuseki import build_version_manifest
    before=e.version()
    manifest=build_version_manifest(taxonomy_sources=[],dataset_sources=[])
    assert manifest['enrichmentVersion']==before
    assert e.version()==before and len(e.public_study(db,batch['study']['id'])['reported_findings'])==3

def test_one_paper_two_studies_and_no_doi_duplication(db,batch):
    other=deepcopy(batch);other['study']['id']+='-2';other['study']['records']=deepcopy(other['study']['records'][:1]);other['study']['records'][0]['record_id']='NCT-SECOND'
    for doc in other['documents']:doc['study_links'].append({'study_id':other['study']['id'],'verification':deepcopy(doc['study_links'][0]['verification'])})
    other['findings']=[]
    with db:e.apply_batch(db,reviewed(db,other))
    assert len(db.data['documents'])==2
    assert len(db.data['studies'])==2
    assert len(e._get(db,'documents',batch['documents'][0]['id'])['study_links'])==2
    bad=deepcopy(other);bad['documents'][0]['id']+='-duplicate'
    with pytest.raises(ValueError,match='already exists'):e.validate_batch(db,bad)

def test_cap_filter_and_boolean_membership(db,batch,monkeypatch):
    other=deepcopy(batch);other['study']['id']+='-2'
    for record in other['study']['records']:record['record_id']+='-2';record.pop('record_uri',None)
    for doc in other['documents']:
        doc['study_links'].append({'study_id':other['study']['id'],'verification':deepcopy(doc['study_links'][0]['verification'])})
    for finding in other['findings']:finding['study_id']=other['study']['id'];finding['id']+='-2'
    with db:e.apply_batch(db,reviewed(db,other))
    monkeypatch.setenv('UE_ENRICHMENT_LIMIT','1')
    rows,meta=e.retrieve(db,query(batch));assert meta['truncated'] and meta['returned_unique_studies']==1
    assert {row['study_identity'] for row in rows}=={batch['study']['id']}
    q=canonical_query({'state':[batch['findings'][0]['state_uri'],'https://universalevidence.com/vocab/states/Malaria']},{'state':'and'})
    assert e.retrieve(db,q)[0]==[]
    q=canonical_query(q.values,{'state':'or'});assert e.retrieve(db,q)[0]
    q=canonical_query({'outcome':[batch['findings'][0]['state_uri']],'intervention':['https://example.org/not-mapped']},{})
    assert e.retrieve(db,q)[0]==[]
    q=canonical_query({'state':[batch['findings'][0]['state_uri']],'region':['https://universalevidence.com/vocab/regions/World']},{})
    assert e.retrieve(db,q)[0]

def test_registry_results_and_plans_are_different(db,batch):
    candidate=deepcopy(batch);candidate['documents'][0]['id']+='-posted';candidate['documents'][0]['identifier']='registry-results:NCT-FIXTURE';candidate['documents'][0]['kind']='registry-results';candidate['documents']=candidate['documents'][:1]
    candidate['findings']=candidate['findings'][:1];candidate['findings'][0]['id']+='-posted';candidate['findings'][0]['sources'][0]['document_id']=candidate['documents'][0]['id']
    with db:e.apply_batch(db,reviewed(db,candidate))
    candidate['documents'][0]['id']+='-plan';candidate['documents'][0]['identifier']='protocol:NCT-FIXTURE';candidate['documents'][0]['kind']='protocol'
    candidate['findings'][0]['id']+='-plan';candidate['findings'][0]['sources'][0]['document_id']=candidate['documents'][0]['id']
    with pytest.raises(ValueError,match='cannot supply reported'):e.validate_batch(db,candidate)

def test_withdrawn_multi_source_requires_reviewed_lineage(db,batch):
    multi=deepcopy(batch);f=multi['findings'][0];f['id']+='-multi';f['sources'].append({**f['sources'][0],'document_id':multi['documents'][1]['id']});multi['findings']=[f]
    with db:e.apply_batch(db,reviewed(db,multi))
    changed=deepcopy(multi);changed['documents'][0]['status']='retracted';changed['documents'][0]['status_check']['result']='retracted'
    with db:e.apply_batch(db,reviewed(db,changed))
    assert not e.active_finding(db,f)
    f['lineage_review']={'approved':True,'reviewed_by':'reviewer','reviewed_at':'2026-10-04','rationale':'All fields independently reported by follow-up table','active_document_ids':[multi['documents'][1]['id']],'supported_fields':f['sources'][1]['fields']}
    changed['findings']=[f]
    with db:e.apply_batch(db,reviewed(db,changed))
    assert e.active_finding(db,f)
    assert f['id'] in {v['id'] for v in e.public_study(db,batch['study']['id'])['reported_findings']}

def test_history_endpoint_reachable_after_all_reports_withdrawn(db,batch):
    from api.routes.query_v2 import study_enrichment_history
    withdrawn=deepcopy(batch);withdrawn['findings']=[]
    for document in withdrawn['documents']:document['status']='retracted';document['status_check']['result']='retracted'
    with db:e.apply_batch(db,reviewed(db,withdrawn))
    assert e.retrieve(db,query(batch))[0]==[]
    response=study_enrichment_history(batch['study']['id'])
    assert len(response['finding_history'])==3
    assert response['finding_history'][0]['eligibility']=='withdrawn-source-support'

def test_registry_refresh_preserves_store(db,batch,tmp_path,monkeypatch):
    from scripts import load_fuseki as loader
    raw=tmp_path/'raw.ttl';raw.write_text('@prefix ex: <https://example.org/> . ex:s ex:p ex:o .')
    monkeypatch.setattr(loader,'AEA_SOURCE',raw);monkeypatch.setattr(loader,'WHO_ICTRP_SOURCE',raw)
    monkeypatch.setattr(loader,'DEFAULT_TAXONOMY_SOURCES',(raw,))
    uploaded=[]
    monkeypatch.setattr(loader,'put_named_graph',lambda *args:uploaded.append(args[1]))
    monkeypatch.setattr(loader,'put_named_graph_payload',lambda *args:uploaded.append(args[1]))
    monkeypatch.setattr(loader,'stamp_concept_matches',lambda *args:None)
    store=Path(db.path);before=store.read_bytes()
    loader.load_default_graphs(version_manifest_path=tmp_path/'versions.json')
    assert len(uploaded)==4 and store.read_bytes()==before
    assert 'https://universalevidence.com/graph/outcomes' in uploaded
    assert json.loads((tmp_path/'versions.json').read_text())['enrichmentVersion']==e.version()

def test_plans_survive_identity_union(db,batch):
    payload={'results':[{'source':'CT.gov','study_id':'NCT-FIXTURE','condition_concept_uri':batch['findings'][0]['state_uri'],'intervention_concept_uri':batch['study']['axes'][0]['uri'],'outcomes':[{'type':'pre-specified-measure','measure':'Registry planned head circumference'}]}],'meta':{'sources':{key:{'status':'included','returned_unique_studies':0} for key in e.SOURCES},'truncated':False,'approximate':False}}
    result=e.enrich_search(payload,query(batch))
    assert all('Registry planned head circumference' in [p['measure'] for p in row['planned_outcomes']] for row in result['results'])
