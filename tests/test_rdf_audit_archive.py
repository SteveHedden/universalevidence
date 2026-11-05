import json
import pytest
from rdflib import Graph, Literal, RDF
from scripts import rdf_store as r

SCHEMA='audit-test'
BATCH={'findings':[{'id':'finding','value':42,'context':'unique full submission context'}]}

def create(path):
    with r.transaction(path,SCHEMA,None) as s:
        s.data['events']['event']={'id':'event','batch':BATCH}
        s.workflow['receipt']='receipt'

def test_archive_roundtrip_and_backup(tmp_path):
    p=tmp_path/'outcomes.ttl';create(p)
    assert 'unique full submission context' not in p.read_text()
    assert len(list((tmp_path/'outcomes.audit').glob('*.json')))==1
    with r.snapshot(p,SCHEMA,None) as s:assert s.data['events']['event']['batch']==BATCH
    dest=tmp_path/'backup'/'renamed.ttl';r.backup(p,dest,SCHEMA,None)
    with r.snapshot(dest,SCHEMA,None) as s:assert s.data['events']['event']['batch']==BATCH
    with r.transaction(dest,SCHEMA,None) as s:assert s.workflow['receipt']=='receipt'

@pytest.mark.parametrize('missing',[True,False])
def test_missing_or_corrupt_archive_fails_closed(tmp_path,missing):
    p=tmp_path/'outcomes.ttl';create(p)
    blob=next((tmp_path/'outcomes.audit').glob('*.json'))
    if missing:blob.unlink()
    else:blob.write_text('{}')
    with pytest.raises(r.StoreError):r.Store(p,SCHEMA,readonly=True)

def test_legacy_embedded_payload_migrates_without_data_change(tmp_path):
    p=tmp_path/'outcomes.ttl';create(p)
    g=Graph().parse(p);node=next(g.subjects(RDF.type,r.UE.RevisionEvent))
    fields=json.loads(str(g.value(node,r.UE.storedFields)));fields['batch']='json'
    g.set((node,r.UE.storedFields,Literal(r.canonical(fields))))
    g.set((node,r.UE.batch,Literal(r.canonical(BATCH))))
    raw=g.serialize(format='turtle').encode();p.write_bytes(raw)
    wf=p.with_name('.outcomes.workflow.json');body=json.loads(wf.read_text());body['knowledge_sha256']=r.hashlib.sha256(raw).hexdigest();wf.write_text(json.dumps(body))
    with r.transaction(p,SCHEMA,None) as s:assert s.data['events']['event']['batch']==BATCH
    assert 'unique full submission context' not in p.read_text()

def test_interrupted_publication_recovers_with_archive(tmp_path,monkeypatch):
    p=tmp_path/'outcomes.ttl';create(p);write=r.atomic_write
    def crash(path,data):
        if path==p:raise OSError('crash')
        write(path,data)
    with monkeypatch.context() as m:
        m.setattr(r,'atomic_write',crash)
        with pytest.raises(OSError):
            with r.transaction(p,SCHEMA,None) as s:s.data['events']['second']={'id':'second','batch':{'new':True}}
    with pytest.raises(r.StoreError):r.Store(p,SCHEMA,readonly=True)
    with r.transaction(p,SCHEMA,None) as s:assert s.data['events']['second']['batch']=={'new':True}
