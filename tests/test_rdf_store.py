"""Behavioral safeguards for authoritative RDF and separate worker state."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from rdflib import Graph,URIRef
from scripts import evidence_enrichment as e, rdf_store as r

FIXTURE=Path(__file__).parent/'fixtures/enrichment/dual-registry.json'

def test_rdf_roundtrip_keeps_native_indicator_links_and_private_workflow(tmp_path):
    batch=json.loads(FIXTURE.read_text());path=tmp_path/'outcomes.ttl'
    with e.writer(path) as store:
        e.apply_batch(store,batch);store.workflow['secret_claim']='private-worker-token'
    graph=Graph().parse(path)
    f=batch['findings'][0]
    assert (URIRef(f['id']),e.UE.findingIndicator,URIRef(f['indicator_uri'])) in graph
    assert 'private-worker-token' not in path.read_text()
    with e.reader(path,budget=None) as store:
        assert store.data['findings'][f['id']]==f
        assert not store.workflow
    with e.writer(path) as store:assert store.workflow['secret_claim']=='private-worker-token'

def test_partial_publication_fails_closed_then_recovers_without_losing_receipt(tmp_path,monkeypatch):
    path=tmp_path/'outcomes.ttl';batch=json.loads(FIXTURE.read_text())
    with e.writer(path):pass
    real=r.atomic_write
    def crash(target,data):
        if Path(target)==path:raise OSError('simulated crash after workflow publication')
        real(target,data)
    with monkeypatch.context() as patch:
        patch.setattr(r,'atomic_write',crash)
        with pytest.raises(OSError):
            with e.writer(path) as store:
                e.apply_batch(store,batch);store.workflow['receipt']='committed-attempt'
    with pytest.raises(r.StoreError):
        with e.reader(path):pass
    with e.writer(path) as store:
        assert len(store.data['findings'])==3
        assert store.workflow['receipt']=='committed-attempt'
        before=len(store.data['events']);e.apply_batch(store,batch)
        assert len(store.data['events'])==before
    with e.reader(path) as store:assert len(store.data['findings'])==3

def test_failed_validation_does_not_publish_partial_knowledge(tmp_path):
    path=tmp_path/'outcomes.ttl'
    with e.writer(path):pass
    before=path.read_bytes();batch=json.loads(FIXTURE.read_text());batch['findings'][0]['value']='wrong'
    with pytest.raises(ValueError):
        with e.writer(path) as store:e.apply_batch(store,batch)
    assert path.read_bytes()==before

def test_new_findings_need_indicator_or_explicit_gap(tmp_path):
    path=tmp_path/'outcomes.ttl';batch=json.loads(FIXTURE.read_text())
    f=batch['findings'][0];f.pop('indicator_uri');f['mapping']['basis']='explicit-state'
    with e.writer(path) as store:
        with pytest.raises(ValueError,match='indicator_uri'):e.validate_batch(store,batch)
        f['indicator_review']={'status':'unresolved','rationale':'No definition-equivalent metric available.','next_step':'Review a new Indicator in core states TTL.'}
        e.apply_batch(store,batch)
    with e.reader(path) as store:assert store.data['findings'][f['id']]['indicator_review']==f['indicator_review']

def test_state_cannot_be_used_as_indicator(tmp_path):
    batch=json.loads(FIXTURE.read_text());batch['findings'][0]['indicator_uri']=batch['findings'][0]['state_uri']
    with e.writer(tmp_path/'outcomes.ttl') as store:
        with pytest.raises(ValueError):e.validate_batch(store,batch)

def test_stale_operational_sidecar_refuses_writer(tmp_path):
    path=tmp_path/'outcomes.ttl'
    with e.writer(path):pass
    workflow=path.with_name('.outcomes.workflow.json');body=json.loads(workflow.read_text());body['knowledge_sha256']='invalid';workflow.write_text(json.dumps(body))
    with pytest.raises(r.StoreError):
        with e.writer(path):pass
