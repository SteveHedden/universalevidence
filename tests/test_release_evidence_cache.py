from contextlib import contextmanager
from types import SimpleNamespace
import pytest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import search_results as sr

def test_release_parsed_once_request_copies_isolated_and_stale_rejected(tmp_path,monkeypatch):
 p=tmp_path/'evidence.ttl';p.write_text('snapshot');config={'evidence':str(p)};calls=[]
 @contextmanager
 def snapshot(*args,**kwargs):
  calls.append(1);yield SimpleNamespace(data={'findings':{'one':{'value':1}}},meta={'version':'v1'},revision='r1')
 monkeypatch.setattr(sr.enrichment.rdf_store,'snapshot',snapshot)
 config['_release_evidence_signature']=sr.prime_release_evidence(config)
 assert sr.prime_release_evidence(config)==config['_release_evidence_signature']
 with sr.result_evidence_reader(config) as first:first.data['findings']['one']['value']=99
 with sr.result_evidence_reader(config) as second:
  assert second.data['findings']['one']['value']==1
  assert second.read_deadline>sr.time.monotonic()
 assert len(calls)==1
 p.write_text('different snapshot')
 with pytest.raises(ValueError,match='changed'):
  with sr.result_evidence_reader(config):pass

def test_interrupted_or_mid_parse_changed_evidence_rejected(tmp_path,monkeypatch):
 p=tmp_path/'evidence.ttl';p.write_text('before');config={'evidence':str(p)}
 @contextmanager
 def snapshot(*args,**kwargs):
  p.write_text('after');yield SimpleNamespace(data={},meta={},revision='r')
 monkeypatch.setattr(sr.enrichment.rdf_store,'snapshot',snapshot)
 with pytest.raises(ValueError,match='changed during'):sr.prime_release_evidence(config)
 p.with_name('.evidence.transaction.json').write_text('{}')
 with pytest.raises(ValueError,match='Interrupted'):sr.prime_release_evidence(config)

def test_authoring_keeps_normal_reader(tmp_path,monkeypatch):
 called=[]
 @contextmanager
 def reader(path):called.append(path);yield 'normal'
 monkeypatch.setattr(sr.enrichment,'reader',reader)
 with sr.result_evidence_reader({'evidence':str(tmp_path/'authoring.ttl')}) as value:assert value=='normal'
 assert len(called)==1

def test_release_budget_is_per_study(monkeypatch):
 from rdflib import Graph
 clock=[0.0]
 @contextmanager
 def reader(config):yield SimpleNamespace(data={'studies':{},'findings':{},'documents':{}},read_deadline=2.0)
 def resolve(store,source,sid):
  clock[0]+=1.5
  assert clock[0]<store.read_deadline
  return None
 monkeypatch.setattr(sr,'result_evidence_reader',reader)
 monkeypatch.setattr(sr.time,'monotonic',lambda:clock[0])
 monkeypatch.setattr(sr.enrichment,'resolve',resolve)
 monkeypatch.setattr(sr.enrichment,'export_rdf',lambda store:Graph())
 monkeypatch.setattr(sr.enrichment,'snapshot_version',lambda store:'v')
 _,coverage,_=sr.local_results(['one','two','three'],'urn:state',{'_release_evidence_signature':('verified',)},Graph())
 assert len(coverage)==3
 assert all(c['status']=='no_reviewed_local_results' for c in coverage)
