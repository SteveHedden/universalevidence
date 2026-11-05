import asyncio
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import time
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import Graph
from scripts.iati_live import Organizations, Quota, SourceError, fetch_live, collect
from scripts.iati_live_rules import RuleBook, activity, freshness, assess, query

BASE='https://universalevidence.com/vocab/'
S=BASE+'states/Stunting'; I=BASE+'interventions/OralNutritionalSupplementation'; C=BASE+'interventions/UnconditionalCashTransfer'
NOW=datetime.now(timezone.utc).isoformat()

def rules():return RuleBook(Path(__file__).parent/'fixtures/iati-live-candidate.json',candidate_review=True)
def entry(text,**attrs):return {**attrs,'narrative':[{'text()':text}]}
def doc(identity='activity-a',text='We provide nutritional supplements to prevent stunting in children.',ref='GB-COH-123',name='Example implementer',role='4'):
 return {'iati_json':{'iati-activity':[{'@last-updated-datetime':NOW,'iati-identifier':[{'text()':identity}], 'title':[entry('Nutrition project')],'description':[entry(text)], 'reporting-org':[entry('Publisher',**{'@ref':'GB-PUB-1'})], 'participating-org':[entry(name,**{'@ref':ref,'@role':role})], 'activity-status':[{'@code':'2'}], 'activity-date':[{'@type':'2','@iso-date':'2025-01-01'},{'@type':'3','@iso-date':'2099-01-01'}], 'recipient-country':[entry('Kenya',**{'@code':'KE'})]}]}}
def raw(d):return d['iati_json']['iati-activity'][0]
def page(docs,total=None):return {'response':{'docs':docs,'numFound':len(docs) if total is None else total},'responseHeader':{'status':0}}
def run(coro):return asyncio.run(coro)
def execute(docs,state=S,intervention=I,second=None):
 calls=[]
 async def fetch(q):
  calls.append(q);return page(docs if len(calls)==1 or second is None else second)
 service=Organizations(rules(),fetch)
 return run(service.lookup(state,intervention)),calls

def test_positive_same_activity_transient_graph_and_no_fallback():
 r,calls=execute([doc()]);assert len(calls)==1 and r['status']=='ok'
 card=r['organizations'][0];assert card['name']=='Example implementer' and card['relevance']=='explicit'
 assert card['representative']['countries']==[{'name':'Kenya','code':'KE'}]
 assert card['representative']['freshness']['status']=='consistent-current'
 g=Graph().parse(data=r['rdf'],format='turtle');assert len(g)>15
 assert not any('Study'==str(o).split('/')[-1] for _,_,o in g)
 assert r['candidate_mapping_review']

def test_zero_confirmed_fallback_but_not_padding_and_no_repeat_single_axis():
 for n in [1,2,4,6]:
  r,calls=execute([doc(str(i),ref=f'GB-{i}') for i in range(n)])
  assert len(calls)==1 and len(r['organizations'])==min(n,5)
 r,calls=execute([],second=[doc(text='We provide nutritional supplements. Stunting affects many children.')])
 assert len(calls)==2 and r['organizations'][0]['relevance']=='candidate'
 _,calls=execute([doc()],state='');assert len(calls)==1
 r,calls=execute([doc()],intervention='');assert len(calls)==1 and r['missing_dimensions']==['intervention']

def test_no_cross_activity_join():
 a=doc('a',text='We prevent stunting.');b=doc('b',text='We provide nutritional supplements.')
 r,_=execute([a,b]);assert not r['organizations']

@pytest.mark.parametrize('text',[
 'We provide nutritional supplements for general nutrition.',
 'We do not provide nutritional supplements to prevent stunting.',
 'We research nutritional supplements to prevent stunting.',
 'We provide livestock nutritional supplements to prevent stunting.',
 'We prevent economic stunting and provide nutritional supplements.',
 'Stunting prevalence is high and nutritional supplements are discussed.',
 'We prevent stunting through food fortification.',
])
def test_false_positives_are_not_confirmed(text):
 r,_=execute([doc(text=text)])
 assert not any(c['relevance']=='explicit' for c in r['organizations'])

@pytest.mark.parametrize('text,positive',[
 ('We provide unconditional cash transfers to prevent stunting.',True),
 ('We provide cash transfers to prevent stunting.',False),
 ('We provide conditional cash transfers to prevent stunting.',False),
 ('We study unconditional cash transfers to prevent stunting.',False),
])
def test_cash_conditionality(text,positive):
 r,_=execute([doc(text=text)],intervention=C);assert bool(r['organizations'])==positive

def test_combined_project_does_not_claim_partner_component_delivery():
 r,_=execute([doc(text='We provide nutritional supplements and nutrition education to prevent stunting.')])
 assert r['organizations'] and 'component_delivery' not in r['organizations'][0]

@pytest.mark.parametrize('role',['1','2','3',''])
def test_only_explicit_implementers(role):
 r,_=execute([doc(role=role)]);assert not r['organizations']

def test_identity_deduplication_unresolved_withheld_and_no_names():
 a=doc();b=doc('b');c=doc('c',ref='GB-OTHER')
 r,_=execute([a,deepcopy(a),b,c,doc('d',ref=''),doc('e',name='IP not published'),doc('f',name=''),doc('g',name='Unknown')])
 assert len(r['organizations'])==2 and len(r['organizations'][0]['activities']) in (1,2)
 assert len(r['unresolved_participants'])==1
 r,calls=execute([doc(ref='')]);assert len(calls)==2 and not r['organizations']

@pytest.mark.parametrize('mutation,expected',[
 (lambda x:x.update({'activity-status':[{'@code':'3'}]}),'ended'),
 (lambda x:x['activity-date'].append({'@type':'4','@iso-date':'2026-01-01'}),'ended'),
 (lambda x:x['activity-date'].__setitem__(1,{'@type':'3','@iso-date':'2025-12-31'}),'unverified'),
 (lambda x:x.update({'activity-date':[]}),'unverified'),
 (lambda x:x['activity-status'].append({'@code':'3'}),'unverified'),
 (lambda x:x.update({'@last-updated-datetime':'2010-01-01T00:00:00Z'}),'unverified'),
 (lambda x:x['activity-date'].append({'@type':'2','@iso-date':'2024-01-01'}),'unverified'),
])
def test_dates_not_status_alone(mutation,expected):
 d=doc();mutation(raw(d));item=activity(d,NOW,rules().get('state',S),rules().get('intervention',I))
 assert item['freshness']['status']==expected
 if expected=='ended':assert not execute([d])[0]['organizations']

def test_code_vocabulary_publisher_version_scope_and_retired_not_ended():
 rule={'concept':I,'phrases':[],'codes':[{'code':'001','vocabulary':'99','vocabulary_uri':'https://example.org/codes','publisher_ref':'GB-PUB-1','version':'1','relation':'exact','lifecycle':'retired'}]}
 d=doc();raw(d)['sector']=[{'@code':'001','@vocabulary':'99','@vocabulary-uri':'https://example.org/codes','@version':'1'}]
 item=activity(d,NOW,None,None);assert assess(item,rule)['explicit'] and item['freshness']['status']!='ended'
 for field,value in [('vocabulary','98'),('version','2'),('vocabulary_uri','https://wrong')]:
  bad=deepcopy(item);bad['source']['sectors'][0][field]=value;assert not assess(bad,rule)['explicit']
 bad=deepcopy(item);bad['source']['sectors'][0]['publisher']=[{'@ref':'another'}];assert not assess(bad,rule)['explicit']
 rule['codes'][0]['relation']='broad';assert not assess(item,rule)['explicit']

def test_missing_mappings_never_queries_and_candidates_not_served():
 async def fetch(q):raise AssertionError('Must not call source')
 r=run(Organizations(RuleBook(Path(__file__).parent/'fixtures/iati-live-candidate.json'),fetch).lookup(S,I));assert r['status']=='missing-mappings'
 assert not RuleBook(Path(__file__).parent/'fixtures/iati-live-candidate.json').rules

def test_malformed_activity_isolated_and_no_error_fallback():
 r,calls=execute([doc(),{'iati_json':{'iati-activity':[{}]}}]);assert r['status']=='partial' and len(r['organizations'])==1 and len(calls)==1
 async def fetch(q):raise SourceError('source-http-error')
 r=run(Organizations(rules(),fetch).lookup(S,I));assert r['status']=='error' and len(r['coverage']['queries'])==1


def test_empty_vs_insufficient_truncation_and_deterministic_order():
 assert execute([])[0]['status']=='empty'
 assert execute([doc(text='Nutrition program')])[0]['status']=='insufficient-matches'
 async def fetch(q):return page([doc()],1000)
 r=run(Organizations(rules(),fetch).lookup(S,I));assert r['coverage']['truncated']
 a=execute([doc('a',ref='A'),doc('b',ref='B')])[0]
 b=execute([doc('b',ref='B'),doc('a',ref='A')])[0]
 assert [c['id'] for c in a['organizations']]==[c['id'] for c in b['organizations']]

def test_cache_singleflight_context_rules_ttl_lru_and_failure_not_cached():
 async def scenario():
  calls=[];clock=[0]
  async def fetch(q):calls.append(q);await asyncio.sleep(.01);return page([doc()])
  service=Organizations(rules(),fetch,clock=lambda:clock[0],cache_size=2)
  await asyncio.gather(*[service.lookup(S,I) for _ in range(6)]);assert len(calls)==1
  assert (await service.lookup(S,I))['cache']=='hit'
  await service.lookup(S,I,'new filter');assert len(calls)==2
  service.rules.version='new';await service.lookup(S,I);assert len(calls)==3
  clock[0]=601;await service.lookup(S,I);assert len(calls)==4
  assert len(service.cache)<=2
  async def bad(q):calls.append(q);raise SourceError('failed')
  service.fetcher=bad
  await service.lookup(S,C);await service.lookup(S,C);assert len(calls)==6
 run(scenario())

def test_deadline_preserves_primary_candidates_when_fallback_hangs():
 async def scenario():
  calls=[]
  async def fetch(q):
   calls.append(q)
   if len(calls)>1:await asyncio.sleep(1)
   return page([doc(text='We provide nutritional supplements. Stunting affects children.')])
  t=time.monotonic();r=await Organizations(rules(),fetch,deadline=.3).lookup(S,I)
  assert time.monotonic()-t<.5 and r['status']=='partial' and len(r['organizations'])==1
 run(scenario())

def test_atomic_shared_quota_concurrency_rate_daily_and_cooldown(tmp_path):
 a=Quota(tmp_path/'quota',interval=0,daily=3);b=Quota(tmp_path/'quota',interval=0,daily=3)
 first=a.reserve();second=b.reserve();assert first and second and a.reserve() is None
 a.release(first);third=b.reserve();assert third
 b.release(second);b.release(third)
 with pytest.raises(SourceError):a.reserve()
 c=Quota(tmp_path/'rate',interval=1);t=c.reserve();c.release(t);assert c.reserve() is None
 c.pause(10);assert c.reserve() is None

def test_endpoint_registration_and_validation(monkeypatch):
 from api.routes import organizations as route
 service=Organizations(RuleBook(Path(__file__).parent/'fixtures/iati-live-candidate.json'));monkeypatch.setattr(route,'service',lambda:service)
 app=FastAPI();app.include_router(route.router)
 with TestClient(app) as client:
  assert client.get('/query/organizations',params={'state':'not-a-uri'}).status_code==422
  response=client.get('/query/organizations',params={'state':S,'intervention':I})
  assert response.json()['status']=='missing-mappings' and response.headers['cache-control']=='no-store'

def test_research_title_and_conflicting_negation_do_not_confirm():
 d=doc();raw(d)['title']=[entry('Randomized trial of nutritional supplements')]
 assert not execute([d])[0]['organizations']
 d=doc(text='We provide nutritional supplements to prevent stunting. We do not provide nutritional supplements.')
 assert not execute([d])[0]['organizations']


def test_empty_ttl_is_short_and_inflight_cancellation_does_not_duplicate():
 async def scenario():
  calls=[];clock=[0]
  async def fetch(q):calls.append(q);await asyncio.sleep(.02);return page([])
  service=Organizations(rules(),fetch,clock=lambda:clock[0])
  a=asyncio.create_task(service.lookup('',I));await asyncio.sleep(.001);a.cancel()
  try:await a
  except asyncio.CancelledError:pass
  await service.lookup('',I);assert len(calls)==1
  clock[0]=59;await service.lookup('',I);assert len(calls)==1
  clock[0]=61;await service.lookup('',I);assert len(calls)==2
 run(scenario())


def test_http_stream_size_error_no_retry_and_slots_released(tmp_path,monkeypatch):
 import scripts.iati_live as live
 from contextlib import asynccontextmanager
 monkeypatch.setenv('IATI_API_KEY','synthetic-test-key')
 class Stream:
  status_code=200;headers={}
  async def aiter_bytes(self):
   yield b'x'*(live.MAX_BYTES+1)
 class Client:
  async def __aenter__(self):return self
  async def __aexit__(self,*a):pass
  @asynccontextmanager
  async def stream(self,*args,**kwargs):yield Stream()
 monkeypatch.setattr(live.httpx,'AsyncClient',lambda **kw:Client())
 quota=Quota(tmp_path/'quota',interval=0)
 with pytest.raises(SourceError,match='source-response-too-large'):run(fetch_live('query',quota))
 with sqlite3.connect(quota.path) as db:
  assert db.execute('select count(*),sum(lease) from calls').fetchone()==(1,0)


def test_real_fetch_gate_concurrent_endpoints_share_two_slots(tmp_path,monkeypatch):
 import scripts.iati_live as live
 from contextlib import asynccontextmanager
 monkeypatch.setenv('IATI_API_KEY','synthetic-test-key')
 active=0;peak=0;calls=0
 class Stream:
  status_code=200;headers={}
  async def aiter_bytes(self):
   nonlocal active,peak,calls
   active+=1;peak=max(peak,active);calls+=1
   try:await asyncio.sleep(.03);yield json.dumps(page([])).encode()
   finally:active-=1
 class Client:
  async def __aenter__(self):return self
  async def __aexit__(self,*a):pass
  @asynccontextmanager
  async def stream(self,*args,**kwargs):yield Stream()
 monkeypatch.setattr(live.httpx,'AsyncClient',lambda **kw:Client())
 async def scenario():
  service=Organizations(rules(),quota=Quota(tmp_path/'quota',interval=0))
  results=await asyncio.gather(*[service.lookup(S,I,str(n)) for n in range(5)])
  assert all(len(r['coverage']['queries'])==2 for r in results)
 run(scenario());assert peak==2 and calls==10


def test_quota_wait_is_inside_endpoint_deadline(tmp_path):
 quota=Quota(tmp_path/'quota',interval=60);quota.reserve()
 async def fetch(q):await quota.acquire();raise AssertionError('No quota available')
 start=time.monotonic();r=run(Organizations(rules(),fetch,deadline=.3).lookup(S,I))
 assert time.monotonic()-start<.5 and r['errors']==['endpoint-deadline']

def test_conflicting_duplicate_reports_cannot_resurrect_ended_activity():
 a=doc();b=deepcopy(a);raw(b)['activity-status']=[{'@code':'3'}]
 r,_=execute([a,b]);assert not r['organizations']
 b=deepcopy(a);raw(b)['title']=[entry('Another reported title')]
 r,_=execute([a,b]);assert r['organizations'][0]['representative']['freshness']['status']=='unverified'

def test_unseen_approved_concept_is_generic_and_query_terms_are_escaped(tmp_path):
 payload=deepcopy(rules().payload)
 new=deepcopy(payload['mappings'][1]);new['concept']=BASE+'interventions/NutritionEducation';new['phrases']=['nutrition education'];new['review']['status']='approved'
 payload['mappings']=[new];path=tmp_path/'rules.json';path.write_text(json.dumps(payload))
 book=RuleBook(path)
 async def fetch(q):return page([doc(identity='never-seen-before',text='We provide nutrition education.')])
 r=run(Organizations(book,fetch).lookup('',new['concept']));assert r['organizations']
 q=query({'phrases':['x" OR *:*'],'codes':[]});assert '\\"' in q
