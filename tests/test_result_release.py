import asyncio,json,sys
from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from test_search_results import assets, fixture_fetch, no_extra, discovery, STATE, INTERVENTION
import search_results as sr
from api.routes import results as route
from package_result_assets import package


def test_all_statuses_can_have_results(assets):
 async def search(*args):return {'results':[{'source':'CT.gov','study_id':'NCT00659061','status':'on_going'}],'meta':{}}
 result=asyncio.run(sr.search_results(STATE,INTERVENTION,{'include_all_statuses':True},search=search,supplement=no_extra,fetch=fixture_fetch))
 assert result['coverage'][0]['status']=='available'
 assert result['study_statuses'][0]['status']=='on_going'
 assert len(result['rows'])==7
 assert all(row['direction']=='unknown' for row in result['rows'] if row['comparison']['kind']=='arm_level')
 assert result['query']['completion_filter']=='all_statuses'


def test_local_sources_use_verified_identity(monkeypatch):
 from contextlib import contextmanager
 from types import SimpleNamespace
 from rdflib import Graph
 seen=[]
 @contextmanager
 def reader(*args):yield SimpleNamespace(data={},meta={},revision='v',read_deadline=float('inf'))
 monkeypatch.setattr(sr.enrichment,'reader',reader)
 monkeypatch.setattr(sr.enrichment,'resolve',lambda store,source,sid:seen.append((source,sid)))
 monkeypatch.setattr(sr.enrichment,'export_rdf',lambda subset:Graph())
 monkeypatch.setattr(sr.enrichment,'snapshot_version',lambda store:'v')
 # Missing identities must be explicit; no label-based registry joins.
 sr.local_results([('isrctn','ISRCTN12345678'),('who-ictrp','origin')],STATE,{'evidence':'x'},Graph())
 assert seen==[('isrctn','ISRCTN12345678'),('who-ictrp','origin')]


def test_release_manifest_and_tamper(tmp_path,monkeypatch):
 root=tmp_path/'maintained';root.mkdir()
 from rdflib import Graph,Literal
 store=sr.enrichment.rdf_store
 g=Graph();g.add((store.STORE,store.UE.storageSchema,Literal(sr.enrichment.SCHEMA)))
 g.add((store.STORE,store.UE.storageMetadata,Literal('{}')))
 g.serialize(root/'source.ttl',format='turtle')
 cfg={k:'source.ttl' for k in ('crosswalk','rules','interventions','intervention_crosswalk','evidence')}
 cfg.update(taxonomy=['source.ttl'],project_root='.',review_only=True,search_url='https://review.invalid')
 (root/'config.json').write_text(json.dumps(cfg))
 dest=tmp_path/'release';package(root/'config.json',dest)
 monkeypatch.setenv('UE_RESULT_RELEASE',str(dest));monkeypatch.setattr(route,'_verified',None)
 loaded=route.release_config();assert loaded['include_all_statuses'] and 'search_url' not in loaded
 (dest/'rules-0.ttl').write_text('tampered')
 with pytest.raises(Exception) as exc:route.release_config()
 assert exc.value.status_code==503


def test_transport_preserves_context_without_supplementation(monkeypatch):
 from types import SimpleNamespace
 import query_v2
 from api.query_v2_cache import query_v2_cache
 query_v2_cache.clear()
 app=FastAPI();app.include_router(route.router);client=TestClient(app)
 captured=[]
 async def execute(query):
  captured.append(query)
  return SimpleNamespace(cacheable=False,payload={'results':[{'study_id':'NCT00659061','source':'CT.gov','title':'Source title','intervention_concept_uri':INTERVENTION}],'meta':{}})
 def forbidden(*args,**kwargs):raise AssertionError('Legacy catalogue/paper path must not execute')
 monkeypatch.setattr(query_v2,'execute_query_v2',execute)
 monkeypatch.setattr(route,'release_config',forbidden)
 monkeypatch.setattr(route,'search_results',forbidden)
 params={'state':STATE,'intervention':INTERVENTION,'context':json.dumps({'values':{'region':['urn:region']}})}
 response=client.get('/query/results',params=params)
 assert response.status_code==200 and response.headers['cache-control']=='no-store'
 assert captured[0].values['region']==('urn:region',)
 assert response.json()['rows']==[]
 assert response.json()['coverage'][0]['status']=='unavailable'
 assert client.get('/query/results',params={**params,'format':'turtle'}).status_code==200
 assert client.get('/query/results',params={**params,'context':'invalid'}).status_code==422
 assert client.get('/query/results',params={**params,'state':'invalid'}).status_code==422


def test_production_timeout_and_rate_limit(monkeypatch):
 import httpx
 async def run():
  real=httpx.AsyncClient
  async def timeout(request):
   await asyncio.sleep(.1);return httpx.Response(200,json={})
  monkeypatch.setattr(route,'_ctgov_retry_after',0)
  monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:real(transport=httpx.MockTransport(timeout),**kw))
  with pytest.raises(TimeoutError):await route.production_fetch('NCT00659061',None,None,None,.01)
  calls=[]
  def limited(request):calls.append(request);return httpx.Response(429,headers={'Retry-After':'60'})
  monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:real(transport=httpx.MockTransport(limited),**kw))
  with pytest.raises(httpx.HTTPStatusError):await route.production_fetch('NCT00659061',None,None,None,1)
  with pytest.raises(RuntimeError,match='rate limited'):await route.production_fetch('NCT00659061',None,None,None,1)
  assert len(calls)==1
 asyncio.run(run())


def test_who_routes_only_verified_registry_identity(monkeypatch):
 from contextlib import contextmanager
 from types import SimpleNamespace
 @contextmanager
 def reader(*args):yield SimpleNamespace(data={'studies':{'known':{'records':[{'source':'ctgov','record_id':'NCT00659061'}]}}})
 monkeypatch.setattr(sr.enrichment,'reader',reader)
 monkeypatch.setattr(sr.enrichment,'resolve',lambda store,source,sid:'known' if sid=='verified' else None)
 ids,coverage=sr.verified_origins([{'source':'WHO ICTRP','study_id':'verified'},{'source':'WHO ICTRP','study_id':'unverified'}],{'evidence':'test'})
 assert ids==['NCT00659061'] and coverage[1]['originating_ctgov_ids']==[]
