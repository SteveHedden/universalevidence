import asyncio, json, sys, time
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
from contextlib import contextmanager
import pytest
from rdflib import Graph,URIRef,Literal
from rdflib.namespace import SKOS
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import search_results as sr
from ctgov_results import Response,LiveClient,query_studies,digest,BASE,UE
from ctgov_intervention_identity import contextual_key
HERE=Path(__file__).parent/'fixtures/ctgov_results'
STATE='https://universalevidence.com/vocab/states/Stunting'
INTERVENTION='https://universalevidence.com/vocab/interventions/NutritionEducation'
@pytest.fixture
def assets(monkeypatch):
 rules=json.loads((HERE/'rules.json').read_text());cw=Graph();tax=Graph().parse(HERE/'taxonomy.ttl')
 for i,r in enumerate(rules['outcomes']):
  e=URIRef('urn:map:'+str(i));cw.add((e,UE.rawText,Literal(r['key'])));cw.add((e,SKOS.closeMatch,URIRef(r['indicator'])))
 monkeypatch.setattr(sr,'load_assets',lambda config:(cw,tax,rules,Graph(),Graph()))
 monkeypatch.setattr(sr,'local_results',lambda *args:(Graph(),[],'test'))
 return cw,tax,rules

def fixture_fetch(sid,cw,tax,rules,timeout):
 payload=json.loads((HERE/f'{sid}.json').read_text());client=LiveClient()
 client.cache[sid]=Response(payload,digest(payload),'2026-10-06T00:00:00Z',0,BASE+sid)
 with sr._RDF_LOCK:return query_studies([sid],cw,tax,rules,client)
async def no_extra(*args):return [],{'truncated':False}
def discovery(ids):
 async def search(*args):return {'results':[{'source':'CT.gov','study_id':sid,'status':'completed'} for sid in ids],'meta':{}}
 return search

def test_scoped_mapping_descendants_and_legacy():
 cw=Graph();tax=Graph();child=URIRef(INTERVENTION+'/child');tax.add((child,SKOS.broader,URIRef(INTERVENTION)))
 for key in [contextual_key('NCT12345678','Nutrition'),'Nutrition']:
  e=URIRef('urn:entry:'+str(len(cw)));cw.add((e,UE.rawText,Literal(key)));cw.add((e,SKOS.closeMatch,child))
 assert sr.scoped_candidates(cw,tax,INTERVENTION)=={'NCT12345678':{'Nutrition'}}

def test_adapter_semantics(assets):
 r=asyncio.run(sr.search_results(STATE,INTERVENTION,{},search=discovery(['NCT00659061','NCT02472262']),supplement=no_extra,fetch=fixture_fetch))
 assert len(r['rows'])==10
 levels=[x for x in r['rows'] if x['comparison']['kind']=='arm_level']
 assert len(levels)==4 and all(x['direction']=='unknown/unsupported' for x in levels)
 changes=[x for x in r['rows'] if x['comparison']['kind']=='within_arm_change']
 assert len(changes)==3 and all(x['direction']=='decrease' for x in changes)
 contrast=next(x for x in r['rows'] if x['comparison']['kind']=='derived_comparison')
 assert contrast['value']=='1' and 'Advice Only' in contrast['comparison']['comparison']
 assert r['rdf'] and r['timings']['total_seconds']>0

def test_dedup_cap_failure_and_completed_filter(assets):
 async def search(*args):return {'results':[{'source':'CT.gov','study_id':sid,'status':status} for sid,status in [('NCT00659061','completed'),('NCT00659061','completed'),('NCT02472262','completed'),('NCT04002908','completed'),('NCT00000000','on_going')]],'meta':{}}
 calls=[]
 def fetch(sid,*args):
  calls.append(sid)
  if sid=='NCT02472262':raise OSError('synthetic failure')
  return fixture_fetch(sid,*args)
 r=asyncio.run(sr.search_results(STATE,INTERVENTION,{'max_result_requests':2},search=search,supplement=no_extra,fetch=fetch))
 assert sorted(calls)==['NCT00659061','NCT02472262'] and len(r['rows'])==7
 assert {x['status'] for x in r['coverage']}=={'available','retrieval_failure','request_cap_not_retrieved'}

def test_production_budget_prioritizes_maintained_result_mapping(assets):
 from urllib.parse import quote
 cw,tax,rules=assets
 rules['outcomes'].append({'key':'ctgov-indicator:v1|'+quote(json.dumps({'record_id':'NCT00659061'}))})
 calls=[]
 def fetch(sid,*args):
  calls.append(sid)
  raise OSError('Expected fixture failure')
 r=asyncio.run(sr.search_results(STATE,INTERVENTION,{'include_all_statuses':True,'max_result_requests':1},search=discovery(['NCT00000001','NCT00659061']),supplement=no_extra,fetch=fetch))
 assert calls==['NCT00659061']
 assert any(c['study']=='NCT00000001' and c['status']=='request_cap_not_retrieved' for c in r['coverage'])

def test_production_does_not_spend_live_requests_on_unmapped_studies(assets):
 from urllib.parse import quote
 cw,tax,rules=assets
 rules['outcomes'].append({'key':'ctgov-indicator:v1|'+quote(json.dumps({'record_id':'NCT00659061'}))})
 calls=[]
 def fetch(sid,*args):
  calls.append(sid)
  raise OSError('Expected fixture failure')
 r=asyncio.run(sr.search_results(STATE,INTERVENTION,{'include_all_statuses':True,'fetch_mapped_only':True},search=discovery(['NCT00000001','NCT00659061']),supplement=no_extra,fetch=fetch))
 assert calls==['NCT00659061']
 missing=next(c for c in r['coverage'] if c['study']=='NCT00000001')
 assert missing['status']=='missing_mapping_not_retrieved'
 assert 'availability was not checked' in missing['reason']

def test_discovery_failure_keeps_supplement(assets):
 async def search(*args):raise OSError('unavailable')
 async def extra(*args):return [{'source':'CT.gov','study_id':'NCT00659061','status':'completed'}],{}
 r=asyncio.run(sr.search_results(STATE,INTERVENTION,{},search=search,supplement=extra,fetch=fixture_fetch))
 assert len(r['rows'])==7 and r['failures'][0]['stage']=='registry_search'

def test_coverage_statuses(assets):
 cw,tax,rules=assets;r=fixture_fetch('NCT00659061',cw,tax,rules,1)
 assert sr.coverage_status(r)=='available'
 bad=deepcopy(r);bad['failures']=[{}];assert sr.coverage_status(bad)=='retrieval_failure'
 bad=deepcopy(r);bad['records']=[];bad['coverage']=[{'mapping_supported':False}];assert sr.coverage_status(bad)=='missing_mapping'
 bad['coverage']=[{'mapping_supported':True}];assert sr.coverage_status(bad)=='unsupported_results'
 bad['responses'][0].payload['hasResults']=False;assert sr.coverage_status(bad)=='no_posted_structured_results'

def test_local_withdrawn_support(monkeypatch):
 import evidence_enrichment as e
 sid='https://universalevidence.com/studies/aea/AEARCTR-9999999'
 f={'id':sid+'/f','study_id':sid,'status':'active','state_uri':STATE,'sources':[{'document_id':'urn:doc','locator':'Table1','fields':['value']}]}
 store=SimpleNamespace(data={'findings':{f['id']:f},'documents':{'urn:doc':{'id':'urn:doc','status':'retracted','study_links':[{'study_id':sid}]}},'studies':{sid:{'id':sid,'records':[{'source':'aea','record_id':'AEARCTR-9999999'}]}}},read_deadline=float('inf'),meta={'version':'x'},revision='x')
 @contextmanager
 def reader(*a,**k):yield store
 monkeypatch.setattr(e,'reader',reader);monkeypatch.setattr(e,'export_rdf',lambda subset:Graph())
 _,coverage,_=sr.local_results(['AEARCTR-9999999'],STATE,{'evidence':'ignored'},Graph())
 assert coverage[0]['records']==0

def test_concurrency_bound(assets):
 from threading import Lock
 active=0;peak=0;lock=Lock()
 def fetch(*args):
  nonlocal active,peak
  with lock:active+=1;peak=max(peak,active)
  time.sleep(.02)
  with lock:active-=1
  raise OSError('test')
 r=asyncio.run(sr.search_results(STATE,INTERVENTION,{'workers':2,'max_result_requests':20},search=discovery([f'NCT{i:08}' for i in range(7)]),supplement=no_extra,fetch=fetch))
 assert peak==2 and len(r['coverage'])==7

def test_scoped_batches_bound_stale_names_and_partial_failure(monkeypatch):
 import httpx,query_v2
 monkeypatch.setattr(query_v2,'_resolve_local_concept',lambda state:object())
 monkeypatch.setattr(query_v2,'_ctgov_state_expression',lambda r:('AREA[OutcomeSearch]("stunting")',False))
 candidates={f'NCT{i:08}':{'Nutrition'} for i in range(401)};requests=[]
 def handle(request):
  ids=request.url.params['filter.ids'].split(',');requests.append(ids)
  if ids[0]=='NCT00000100':return httpx.Response(503)
  return httpx.Response(200,json={'studies':[{'protocolSection':{'identificationModule':{'nctId':sid},'statusModule':{'overallStatus':'COMPLETED'},'armsInterventionsModule':{'interventions':[{'name':'Renamed stale intervention' if sid==ids[0] else 'Nutrition'}]}}} for sid in ids[:2]]})
 factory=httpx.AsyncClient
 monkeypatch.setattr(sr.httpx,'AsyncClient',lambda **kw:factory(transport=httpx.MockTransport(handle),**kw))
 rows,meta=asyncio.run(sr.scoped_search(STATE,candidates,{}))
 assert len(requests)==4 and all(len(ids)==100 for ids in requests)
 assert meta['truncated'] and len(meta['failures'])==1 and len(rows)==3
 assert all(r['study_id'].endswith('01') for r in rows)

def test_local_route_opt_in_and_three_formats(tmp_path,monkeypatch):
 from fastapi.testclient import TestClient
 from api import local_results
 client=TestClient(local_results.app)
 params={'state':STATE,'intervention':INTERVENTION}
 monkeypatch.delenv('UE_RESULTS_CONFIG',raising=False)
 assert client.get('/query/results',params=params).status_code==503
 cfg=tmp_path/'config.json';cfg.write_text('{"review_only":true}')
 monkeypatch.setenv('UE_RESULTS_CONFIG',str(cfg))
 async def search(*args):return {'timings':{},'rdf':'@prefix ue: <https://universalevidence.com/ontology/> .','rows':[],'coverage':[],'interpretation':'No pooling','search':{'meta':{}},'scoped_discovery':{},'failures':[]}
 monkeypatch.setattr(local_results,'search_results',search)
 for fmt in ('json','html','turtle'):
  response=client.get('/query/results',params={**params,'format':fmt})
  assert response.status_code==200 and response.headers['cache-control']=='no-store' and 'total;dur=' in response.headers['server-timing']
 assert client.get('/query/results',params={**params,'state':'not-a-uri'}).status_code==422

def test_local_bad_study_keeps_good_study(monkeypatch):
 import evidence_enrichment as e
 from rdflib import RDF
 data={'studies':{'good':{'id':'good'},'bad':{'id':'bad'}},'findings':{'urn:f':{'id':'urn:f','state_uri':STATE,'sources':[]}},'documents':{}}
 store=SimpleNamespace(data=data,read_deadline=float('inf'),meta={'version':'v'},revision='r')
 @contextmanager
 def reader(*a,**k):yield store
 def public(conn,identity):
  if identity=='bad':raise ValueError('malformed study')
  return {'reported_findings':[data['findings']['urn:f']]}
 def export(subset):
  g=Graph();g.add((URIRef('urn:f'),RDF.type,UE.OutcomeMeasurement));return g
 monkeypatch.setattr(e,'reader',reader);monkeypatch.setattr(e,'resolve',lambda c,s,r:r)
 monkeypatch.setattr(e,'public_study',public);monkeypatch.setattr(e,'export_rdf',export)
 g,cov,_=sr.local_results(['bad','good'],STATE,{'evidence':'ignored'},Graph())
 assert len(g)==1 and [r['status'] for r in cov]==['retrieval_failure','reviewed_text_only']


def test_coverage_is_scoped_to_requested_state(assets):
 cw,tax,rules=assets
 result=fixture_fetch('NCT00659061',cw,tax,rules,1)
 assert sr.coverage_status(result,{URIRef(STATE)})=='available'
 assert sr.coverage_status(result,{URIRef('urn:other-state')})=='no_mapped_results_for_selected_state'


def test_asset_cache_reloads_changed_local_rules(tmp_path,monkeypatch):
 paths=[]
 for i in range(6):
  p=tmp_path/str(i);p.write_text('initial');paths.append(str(p))
 cfg=dict(crosswalk=paths[0],taxonomy=[paths[1],paths[2]],rules=paths[3],intervention_crosswalk=paths[4],interventions=paths[5])
 calls=[]
 def parse(config):
  result=object();calls.append(result);return result
 monkeypatch.setattr(sr,'_parse_assets',parse)
 first=sr.load_assets(cfg)
 assert sr.load_assets(cfg) is first
 Path(cfg['rules']).write_text('changed rules')
 assert sr.load_assets(cfg) is not first
 assert len(calls)==2

def test_reviewed_discovery_is_opt_in_for_bounded_worker_replays(assets,monkeypatch):
 import report_result_semantics
 calls=[]
 monkeypatch.setattr(report_result_semantics,'annotate',lambda *args:[])
 monkeypatch.setattr(report_result_semantics,'discovery_candidates',lambda *args:[{'source':'AEA','study_id':'AEARCTR-0099999'}])
 monkeypatch.setattr(sr,'verified_origins',lambda *args:([],[]))
 def local(ids,*args):
  calls.append(ids)
  return Graph(),[],'test'
 monkeypatch.setattr(sr,'local_results',local)
 cfg={'include_all_statuses':True,'report_semantics':['unused']}
 asyncio.run(sr.search_results(STATE,INTERVENTION,cfg,search=discovery([]),supplement=no_extra,fetch=fixture_fetch))
 assert calls[-1]==[]
 asyncio.run(sr.search_results(STATE,INTERVENTION,dict(cfg,discover_reviewed_results=True),search=discovery([]),supplement=no_extra,fetch=fixture_fetch))
 assert calls[-1]==[('aea','AEARCTR-0099999')]
