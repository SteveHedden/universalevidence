"""Offline actor identity, mapping, refresh and API acceptance cases."""
from copy import deepcopy
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib.namespace import RDF
from scripts import iati_actors as a
from api.routes.actors import router

STATE='https://universalevidence.com/vocab/states/Stunting'
INTERVENTION='https://universalevidence.com/vocab/interventions/NutritionEducation'
AT='2026-10-04T12:00:00Z'

def person(ref=None,name=None,role='4'):
    p={'@role':role,'@type':'21'}
    if ref:p['@ref']=ref
    if name:p['narrative']=[{'text()':name}]
    return p

def document(identity='test-activity',participants=None):
    return {'iati_json':{'iati-activity':[{'iati-identifier':[{'text()':identity}],
      '@last-updated-datetime':AT,'reporting-org':[{'@ref':'PUB-1','narrative':[{'text()':'Publisher only'}]}],
      'title':[{'narrative':[{'text()':'Stunting prevention'}]}],
      'description':[{'narrative':[{'text()':'Nutrition education for stunting prevention.'}]}],
      'activity-status':[{'@code':'3'}],'activity-date':[{'@type':'4','@iso-date':'2020-12-31'}],
      'sector':[{'@code':'12240','@vocabulary':'1','narrative':[{'text()':'Basic nutrition'}]},{'@code':'A','@vocabulary':'99','@vocabulary-uri':'https://example.org/codes','narrative':[{'text()':'Bundled nutrition'}]}],
      'participating-org':participants if participants is not None else [person('ORG-1','Partner','4'),person('ORG-1','Alias','1'),person('ORG-1','Partner','4'),person('ORG-2'),person(name='Unknown'),person('OPAQUE-1','IP not published')]}]}}

def decision(source,axis='state',status='accepted'):
    field='titles' if axis=='state' else 'descriptions'
    return {'axis':axis,'concept':STATE if axis=='state' else INTERVENTION,'status':status,
      'method':'source-text-review','reviewer':'fixture reviewer','reviewed_at':AT,
      'rationale':'Synthetic explicit activity scope/description, not inferred effectiveness',
      'support':[{'path':[field,0,'narrative',0,'text()'],'value':source[field][0]['narrative'][0]['text()']}]}

@pytest.fixture
def db(tmp_path,monkeypatch):
    path=tmp_path/'actors.ttl';monkeypatch.setenv('UE_ACTORS_TTL',str(path))
    conn=a.open_store(path);a.initialize(conn);conn.commit()
    yield conn
    conn.close()

def ingest(conn,doc,accept=True):
    identity=doc['iati_json']['iati-activity'][0]['iati-identifier'][0]['text()']
    source=a.normalize(doc,AT)['source']
    with conn:
        a.update(conn,identity,None,document=doc,retrieved_at=AT,complete=True,decisions=[decision(source),decision(source,'intervention')] if accept else [])
        row=conn.data['populations'].get('fixture')
        import json
        ids=row['activity_ids'] if row else []
        a.save_population(conn,{'id':'fixture','state':STATE,'activity_ids':sorted(set(ids+[identity])),'activity_cap':25,'query_cap':10,'truncated':True,'errors':[],'updated_at':AT,'scope':'Bounded Stunting fixture'})
    return identity

def test_identity_roles_counts_pagination_and_api(db):
    identity=ingest(db,document())
    result=a.lookup(db,state=STATE,limit=1)
    assert result['meta']['identified_organization_count']==2
    assert result['meta']['unresolved_participant_count']==2
    pages=result['results']+a.lookup(db,state=STATE,limit=1,offset=1,snapshot=result['meta']['snapshot'])['results']
    named=next(x for x in pages if x['id']==a.org_uri('ORG-1'))
    assert named['names']==['Alias','Partner'] and len(named['activities'])==1
    assert named['activities'][0]['roles']=={'1':'funding','4':'implementing'}
    assert a.org_uri('PUB-1') not in [x['id'] for x in pages]
    assert any(x['name_unavailable'] for x in pages)
    assert a.lookup(db,state=STATE,role='1')['meta']['identified_organization_count']==1
    assert a.lookup(db,state=STATE,role='1')['meta']['unresolved_participant_count']==0
    assert len(a.lookup(db,state=STATE,unresolved=True)['results'])==2
    app=FastAPI();app.include_router(router);client=TestClient(app)
    assert client.get('/actors/organizations',params={'state':STATE}).json()['meta']['identified_organization_count']==2
    assert client.get('/actors/activities/detail',params={'id':identity,'history':True}).json()['history']
    assert client.get('/actors/organizations').status_code==422
    assert client.get('/actors/organizations',params={'state':STATE,'role':'reporter'}).status_code==422
    assert client.get('/actors/organizations',params={'state':STATE,'offset':1}).status_code==409
    assert client.get('/actors/activities/detail',params={'id':'missing'}).status_code==404
    graph=a.export_agents(db)
    assert len(set(graph.subjects(RDF.type,a.PROV.Organization)))==2
    assert not list(graph.subjects(RDF.type,a.UE.Evidence))

def test_combined_axes_same_activity_and_scoped_intervention(db):
    first=ingest(db,document('first'));second=ingest(db,document('second'),False)
    with db:
        one=a.get(db,first);a.update(db,first,one['revision'],decisions=[decision(one['source'],'intervention','rejected')])
        two=a.get(db,second);a.update(db,second,two['revision'],decisions=[decision(two['source'],'intervention')])
    assert a.lookup(db,state=STATE)['results']
    assert a.lookup(db,state=STATE,intervention=INTERVENTION)['results']==[]
    # Even intervention-only answers remain restricted to accepted Stunting activities.
    assert a.lookup(db,intervention=INTERVENTION)['results']==[]

def test_refresh_mapping_support_and_participation(db):
    doc=document();identity=ingest(db,doc);old=a.get(db,identity)
    revised=deepcopy(doc);raw=revised['iati_json']['iati-activity'][0]
    raw['participating-org']=[person('NEW-ORG','New implementer')]
    raw['@last-updated-datetime']='2026-10-05T00:00:00Z'
    with db:a.update(db,identity,old['revision'],document=revised,retrieved_at='2026-10-05T01:00:00Z',complete=True)
    result=a.lookup(db,state=STATE,intervention=INTERVENTION)
    assert [x['id'] for x in result['results']]==[a.org_uri('NEW-ORG')]
    assert len(a.get(db,identity)['mappings'])==2
    current=a.get(db,identity);raw['description'][0]['narrative'][0]['text()']='Unspecified nutrition activity'
    with db:a.update(db,identity,current['revision'],document=revised,retrieved_at='2026-10-05T02:00:00Z',complete=True)
    assert a.lookup(db,state=STATE)['results']
    assert not a.lookup(db,intervention=INTERVENTION)['results']
    assert next(m for m in a.get(db,identity)['mappings'] if m['axis']=='intervention')['status']=='needs-review'
    history=a.detail(db,identity,True)['history']
    assert history[0]['snapshot']['source']['participants']!=history[-1]['snapshot']['source']['participants']
    assert history[0]['snapshot']['mappings'][0]['status']=='accepted'

def test_stale_rollback_missing_refresh_and_source_revision(db):
    doc=document();identity=ingest(db,doc);old=a.get(db,identity)
    with db:a.update(db,identity,old['revision'],error='http-error',retrieved_at='2026-10-05T00:00:00Z')
    before=a.generation(db);count=len(db.data['events'])
    with pytest.raises(a.Conflict):
        with db:a.update(db,identity,old['revision'],decisions=[decision(old['source'],'state','rejected')])
    assert a.generation(db)==before and len(db.data['events'])==count
    assert a.lookup(db,state=STATE)['results'][0]['activities'][0]['freshness_errors']
    current=a.get(db,identity);empty=document(participants=[])
    with db:a.update(db,identity,current['revision'],document=empty,retrieved_at=AT,complete=False)
    assert a.lookup(db,state=STATE)['meta']['identified_organization_count']==2
    older=document();older['iati_json']['iati-activity'][0]['@last-updated-datetime']='2020-01-01T00:00:00Z'
    with pytest.raises(a.Conflict):
        with db:a.update(db,identity,a.get(db,identity)['revision'],document=older,retrieved_at=AT,complete=True)

def test_identity_resolution_and_withholding(db):
    identity=ingest(db,document());item=a.get(db,identity)
    unknown=next(p for p in item['source']['participants'] if p['identity_status']=='unresolved')
    resolution={'participant':unknown['id'],'ref':'ORG-1','reviewer':'reviewer','reviewed_at':AT,'rationale':'Explicit documented identity','source_url':'https://example.org/identity'}
    with db:a.update(db,identity,item['revision'],resolutions=[resolution])
    result=a.lookup(db,state=STATE)
    assert result['meta']['identified_organization_count']==2 and result['meta']['unresolved_participant_count']==1
    withheld=next(p for p in item['source']['participants'] if p['identity_status']=='withheld')
    with pytest.raises(ValueError,match='non-withheld'):
        with db:a.update(db,identity,a.get(db,identity)['revision'],resolutions=[{**resolution,'participant':withheld['id']}])
    assert a.detail(db,identity,True)['history'][-1]['snapshot']['resolutions'][unknown['id']]==resolution

def test_nested_sectors_no_auto_mappings_and_unresolved_scope(db):
    doc=document();raw=doc['iati_json']['iati-activity'][0]
    raw['transaction']=[{'@ref':'t','sector':[{'@code':'A','@vocabulary':'98','narrative':[{'text()':'Different scoped code'}]}]}]
    identity=ingest(db,doc,False)
    normalized=a.get(db,identity)
    assert normalized['source']['sectors'][1]['publisher_scoped']
    assert normalized['source']['sectors'][2]['transaction']['ref']=='t'
    assert not a.lookup(db,state=STATE)['results']  # Search hit and broad/bundled codes confer no eligibility.
    m=decision(normalized['source']);m['method']='sector-code-review';m['support']=[{'path':['sectors',0],'value':normalized['source']['sectors'][0]}]
    with pytest.raises(ValueError,match='crosswalk'):
        with db:a.update(db,identity,normalized['revision'],decisions=[m])
    ingest(db,document('second'))
    with db:a.update(db,identity,normalized['revision'],decisions=[decision(normalized['source'])])
    assert a.lookup(db,state=STATE)['meta']['unresolved_participant_count']==4
    assert a.lookup(db,state=STATE)['meta']['identified_organization_count']==2

def test_plan_scope_escaping_caps_and_metadata(db):
    plan=a.make_plan(STATE,[INTERVENTION],25,1,3)
    assert plan['activity_cap']==25 and plan['omitted_query_count']>0
    assert all(q['state_terms'] for q in a.make_plan(STATE,[INTERVENTION])['queries'])
    assert a.quoted('a" OR *:*')=='"a\\" OR \\*\\:\\*"'
    doc=document();doc['iati_json']['iati-activity'][0]['activity-status'].append({'@code':'2'})
    identity=ingest(db,doc)
    assert a.get(db,identity)['source']['metadata_issues']['conflicting_status']
    assert a.lookup(db,state=STATE)['results'][0]['activities'][0]['metadata_issues']['current_operation']=='not inferred'


def test_rdf_export_shapes_and_research_isolation(db):
    from rdflib import Graph
    from pyshacl import validate
    identity=ingest(db,document())
    graph=a.export_activities(db)
    assert (a.URIRef(a.ACTIVITIES+identity),RDF.type,a.PROV.Activity) in graph
    assert not list(graph.subjects(RDF.type,a.UE.Study))
    assert not list(graph.subjects(RDF.type,a.UE.Evidence))
    shapes=Graph().parse(a.ROOT/'ontology/actor-population.shacl.ttl')
    assert validate(graph,shacl_graph=shapes)[0]
    association=next(graph.subjects(RDF.type,a.PROV.Association))
    graph.remove((association,a.PROV.agent,None))
    assert not validate(graph,shacl_graph=shapes)[0]
    assert not db.data['studies'] and not db.data['findings']


def test_page_invalidation_withdrawal_and_reopen(db):
    identity=ingest(db,document());page=a.lookup(db,state=STATE,limit=1)
    item=a.get(db,identity)
    with db:a.update(db,identity,item['revision'],withdrawal={'reviewer':'reviewer','reviewed_at':AT,'rationale':'Explicit fixture withdrawal','source_url':'https://example.org/withdrawn'})
    with pytest.raises(a.Conflict):a.lookup(db,state=STATE,offset=1,snapshot=page['meta']['snapshot'])
    assert a.lookup(db,state=STATE)['meta']['identified_organization_count']==0
    path=db.path
    with a.reader(path) as reopened:
        assert a.detail(reopened,identity,True)['withdrawn']
        assert len(a.detail(reopened,identity,True)['history'])==2


def test_no_lexical_acceptance_or_cross_activity_name_merge(db):
    first=document('A',participants=[person(name='Unresolved same name')]);first['iati_json']['iati-activity'][0]['title']=[{'narrative':[{'text()':'Stunting of industrial process efficiency'}]}]
    identity=ingest(db,first,False)
    assert not a.lookup(db,state=STATE)['results']
    current=a.get(db,identity)
    with db:a.update(db,identity,current['revision'],decisions=[{**decision(current['source'],status='rejected'),'rationale':'Lexical false positive unrelated to child undernutrition'}])
    assert a.lookup(db,state=STATE)['meta']['matching_activity_count']==0
    ingest(db,document('B',participants=[person(name='Unresolved same name')]))
    ingest(db,document('C',participants=[person(name='Unresolved same name')]))
    result=a.lookup(db,state=STATE,unresolved=True)
    assert result['meta']['identified_organization_count']==0 and result['meta']['unresolved_participant_count']==2
    assert result['results'][0]['id']!=result['results'][1]['id']


def test_exact_upsert_retry_and_unknown_source_order(db):
    doc=document();source=a.normalize(doc,AT)['source']
    with db:revision=a.update(db,'test-activity',None,document=doc,retrieved_at=AT,complete=True,decisions=[decision(source)])
    before=a.generation(db)
    with db:assert a.update(db,'test-activity',None,document=doc,retrieved_at=AT,complete=True,decisions=[decision(source)])==revision
    assert a.generation(db)==before
    changed=deepcopy(doc);changed['iati_json']['iati-activity'][0]['activity-status']=[{'@code':'4'}]
    with db:a.update(db,'test-activity',revision,document=changed,retrieved_at='2026-10-06T00:00:00Z',complete=True)
    current=a.get(db,'test-activity')
    assert current['source_revision_relation']=='changed-without-publisher-ordering-evidence'
    assert current['mappings'][0]['status']=='accepted'


def test_production_api_registers_actor_routes_without_changing_health(db):
    from api.main import app
    ingest(db,document())
    client=TestClient(app)
    result=client.get('/actors/organizations',params={'state':STATE,'role':'4'})
    assert result.status_code==200 and result.json()['meta']['identified_organization_count']==2
    assert client.get('/health').json()=={'status':'ok'}


@pytest.mark.parametrize('marker',['IP not published','Name withheld','Not disclosed','REDACTED'])
def test_withholding_and_placeholder_refs_never_count_as_identified(db,marker):
    ingest(db,document(participants=[person('OPAQUE-7',marker),person('N/A'),person('unknown','Named but unresolved')]))
    result=a.lookup(db,state=STATE)
    assert result['meta']['identified_organization_count']==0
    assert result['meta']['unresolved_participant_count']==3
    unknown=a.lookup(db,state=STATE,unresolved=True)['results']
    assert sum(p['identity_status']=='withheld' for p in unknown)==1

@pytest.mark.parametrize('status',['pending','rejected','needs-review','withdrawn','outside-population'])
def test_default_exports_share_population_eligibility(db,status):
    from rdflib import Graph, URIRef
    identity=ingest(db,document(),accept=status!='pending')
    item=a.get(db,identity)
    with db:
        if status=='rejected':a.update(db,identity,item['revision'],decisions=[decision(item['source'],status='rejected')])
        elif status=='needs-review':
            changed=document();changed['iati_json']['iati-activity'][0]['title'][0]['narrative'][0]['text()']='Changed scope'
            a.update(db,identity,item['revision'],document=changed,retrieved_at=AT,complete=True)
        elif status=='withdrawn':a.update(db,identity,item['revision'],withdrawal={'reviewer':'reviewer','reviewed_at':AT,'rationale':'Withdrawn','source_url':'https://example.org/withdrawn'})
        elif status=='outside-population':db.data['populations'].clear()
    app=FastAPI();app.include_router(router);client=TestClient(app)
    def check(expected):
        result=client.get('/actors/organizations',params={'state':STATE})
        assert result.json()['meta']['identified_organization_count']==expected
        response=client.get('/actors/vocabulary');assert response.status_code==200
        graphs=[Graph().parse(data=response.text,format='turtle'),a.export_agents(db),a.export_activities(db)]
        for graph in graphs:
            assert len(set(graph.subjects(RDF.type,a.PROV.Organization)))==expected
            assert not list(graph.triples((URIRef(item['uri']),None,None)))
            assert not list(graph.triples((None,a.DCTERMS.source,URIRef(item['source_url']))))
    check(0)
    # Another accepted activity independently supports the same organization.
    ingest(db,document('survivor',participants=[person('ORG-1','Eligible alias')]))
    check(1)
    graph=a.export_agents(db)
    assert sorted(str(v) for v in graph.objects(URIRef(a.org_uri('ORG-1')),a.RDFS.label))==['Eligible alias']
    assert a.detail(db,identity,True)['history']
