import json
from copy import deepcopy
from pathlib import Path
from rdflib import Graph,Literal,URIRef
from rdflib.namespace import RDF
import pytest
from scripts import evidence_enrichment as e
from scripts import rdf_store as rs
from scripts.cids_outcomes import CIDS,I72,OBO,UE,add_cids

FIXTURE=Path(__file__).parent/'fixtures/enrichment/dual-registry.json'

def test_legacy_read_new_write_roundtrip(tmp_path):
    data=json.loads(FIXTURE.read_text());p=tmp_path/'store.ttl'
    with e.writer(p) as s:e.apply_batch(s,data)
    before=deepcopy(s.data);meta=deepcopy(s.meta);workflow=deepcopy(s.workflow)
    # Construct a genuine legacy serialization, then read through compatibility.
    g=Graph().parse(p)
    for new,old in [(UE.OutcomeMeasurement,UE.ReportedFinding)]:
        for n in list(g.subjects(RDF.type,new)):g.remove((n,RDF.type,new));g.add((n,RDF.type,old))
    for key,old in rs.LEGACY_FINDING_FIELDS.items():
        new=rs.predicate('findings',key)
        for a,b in list(g.subject_objects(new)):g.remove((a,new,b));g.add((a,old,b))
    import hashlib
    raw=g.serialize(format='turtle');p.write_text(raw)
    side=p.with_name('.store.workflow.json');body=json.loads(side.read_text());body['knowledge_sha256']=hashlib.sha256(raw.encode()).hexdigest();side.write_text(json.dumps(body))
    with e.writer(p) as s:assert s.data==before
    after=e.open_store(p)
    assert (after.data,after.meta,after.workflow)==(before,meta,workflow)
    g=Graph().parse(p)
    assert len(list(g.subjects(RDF.type,UE.OutcomeMeasurement)))==3
    assert not list(g.subjects(RDF.type,UE.ReportedFinding))
    assert len(list(g.subjects(RDF.type,CIDS.Outcome)))>=1
    assert not list(g.subjects(RDF.type,CIDS.IndicatorReport))

def test_effect_measure_not_prevalence_and_shared_context():
    f={'study_id':'https://x/study','state_uri':'https://x/stunting','indicator_uri':'https://x/prevalence','value':0.63,'unit':'odds ratio','measure':'adjusted odds ratio'}
    g=Graph();add_cids(g,{'https://x/one':f,'https://x/two':dict(f,value=0.8)})
    assert len(list(g.subjects(RDF.type,CIDS.Outcome)))==1
    assert len(list(g.subjects(RDF.type,CIDS.Indicator)))==1
    assert len(list(g.subjects(RDF.type,OBO.STATO_0000182)))==2
    assert not list(g.triples((None,I72.value,None)))
    assert (URIRef('https://x/one/estimate'),I72.numerical_value,Literal(0.63)) in g

def test_text_result_does_not_create_number():
    f={'study_id':'https://x/study','state_uri':'https://x/state','result_text':'No estimate available'}
    g=Graph();add_cids(g,{'https://x/text':f})
    assert not list(g.subjects(RDF.type,I72.Measure))

def test_conflicting_legacy_and_canonical_links_fail(tmp_path):
    data=json.loads(FIXTURE.read_text());p=tmp_path/'store.ttl'
    with e.writer(p) as s:e.apply_batch(s,data)
    g=Graph().parse(p);n=next(g.subjects(RDF.type,UE.OutcomeMeasurement))
    g.add((n,UE.findingStudy,URIRef('https://x/wrong')));g.serialize(p,format='turtle')
    with pytest.raises(rs.StoreError,match='Ambiguous'):
        rs.Store(p,e.SCHEMA,readonly=True)
