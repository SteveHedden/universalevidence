import asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from api.routes import organizations as route

LOCAL={'status':'ok','count':1,'projects':[{'id':'reviewed','title':'Retained reviewed metadata'}]}
S='https://universalevidence.com/vocab/states/Stunting'
I='https://universalevidence.com/vocab/interventions/OralNutritionalSupplementation'

class Fake:
 def __init__(self,mode):self.mode=mode;self.projects=self
 def envelope(self,state,intervention,status,**kw):return {'status':status,'mode':'project-mentions','projects':[],**kw}
 async def lookup(self,*args):
  if self.mode=='failure':raise RuntimeError('IATI failed')
  if self.mode=='timeout':raise TimeoutError()
  return self.envelope(*args[:2],self.mode)

def test_sources_independent_and_no_live_registry_call(monkeypatch):
 for mode in ('empty','missing-mappings','partial','failure','timeout'):
  monkeypatch.setattr(route,'service',lambda:Fake(mode))
  monkeypatch.setattr(route,'reviewed_projects',lambda *args:LOCAL)
  app=FastAPI();app.include_router(route.router)
  with TestClient(app) as client:
   response=client.get('/query/organizations',params={'state':S,'intervention':I})
  assert response.status_code==200
  assert response.json()['study_projects']==LOCAL
  assert response.json()['projects']==[]
  assert response.headers['cache-control']=='no-store'

def test_local_failure_preserves_iati(monkeypatch):
 monkeypatch.setattr(route,'service',lambda:Fake('ok'))
 monkeypatch.setattr(route,'reviewed_projects',lambda *a:{'status':'unavailable','projects':[]})
 app=FastAPI();app.include_router(route.router)
 with TestClient(app) as client:r=client.get('/query/organizations',params={'state':S,'intervention':I})
 assert r.json()['status']=='ok' and r.json()['study_projects']['status']=='unavailable'
