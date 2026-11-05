"""Reproductions for independent Task 255 review R1, R2 and R4."""
from copy import deepcopy
import pytest
from scripts import evidence_enrichment as e
from test_evidence_enrichment import batch, db, query, reviewed


def test_stale_report_reinstatement_is_atomic_and_reviewed_reinstatement_allowed(db,batch):
    stale=deepcopy(batch);stale['preconditions']=e.revision_preconditions(db,batch['study']['id'])
    withdrawn=deepcopy(batch);withdrawn['findings']=[]
    for doc in withdrawn['documents']:
        doc['status']='retracted';doc['status_check']['result']='retracted'
    with db:e.apply_batch(db,reviewed(db,withdrawn))
    before=e.version();events=len(db.data['events'])
    stale['study']['title']='Older worker title'
    with pytest.raises(ValueError,match='precondition'):
        with db:e.apply_batch(db,stale)
    assert e.version()==before
    assert len(db.data['events'])==events
    assert e._get(db,'studies',batch['study']['id'])==batch['study']
    assert e.retrieve(db,query(batch))[0]==[]
    assert not e.graph_population(query(batch))[0]
    stale['preconditions']=e.revision_preconditions(db,batch['study']['id'])
    with pytest.raises(ValueError,match='Stale report status check'):
        with db:e.apply_batch(db,stale)
    for doc in stale['documents']:doc['status_check']['checked_at']='2026-11-01T00:00:00Z'
    with pytest.raises(ValueError,match='Explicit change review'):
        with db:e.apply_batch(db,stale)
    with db:e.apply_batch(db,reviewed(db,batch))
    assert len(e.public_study(db,batch['study']['id'])['reported_findings'])==3
    assert e.retrieve(db,query(batch))[0]


def test_stale_finding_and_historical_retry_do_not_reactivate(db,batch):
    original_tokens=e.revision_preconditions(db,batch['study']['id'])
    changed=deepcopy(batch)
    changed['findings'][0]['status']='retracted'
    with db:e.apply_batch(db,reviewed(db,changed))
    stale=deepcopy(batch);stale['study']['title']='stale title';stale['preconditions']=original_tokens
    with pytest.raises(ValueError,match='precondition: findings/'):
        with db:e.apply_batch(db,stale)
    before=e.version()
    with db:e.apply_batch(db,batch)  # Exact retry of original committed submission.
    assert e.version()==before
    assert e._get(db,'findings',batch['findings'][0]['id'])['status']=='retracted'


def test_partial_study_preserves_metadata_and_stale_snapshots_fail(db,batch):
    identity=batch['study']['id'];original=e._get(db,'studies',identity)
    old=e.revision_preconditions(db,identity)
    indexed=e.axis_index(db)
    partial={'schema':batch['schema'],'study':{k:deepcopy(batch['study'][k]) for k in ('id','records')},'documents':[],'findings':[]}
    with db:e.apply_batch(db,partial)
    assert e._get(db,'studies',identity)==original
    assert e.axis_index(db)==indexed
    assert e.retrieve(db,query(batch))[0]
    assert e.graph_population(query(batch))[0]
    newer=deepcopy(partial);newer['study']['title']='New reviewed title';newer['preconditions']=e.revision_preconditions(db,identity)
    with db:e.apply_batch(db,newer)
    stale=deepcopy(batch);stale['study']['title']='Old worker title';stale['preconditions']=old
    with pytest.raises(ValueError,match='precondition'):
        with db:e.apply_batch(db,stale)
    preserved=e._get(db,'studies',identity)
    assert preserved=={**original,'title':'New reviewed title'}
    removal=deepcopy(partial);removal['study']['axes']=[];removal['preconditions']=e.revision_preconditions(db,identity)
    with pytest.raises(ValueError,match='Explicit change review'):
        with db:e.apply_batch(db,removal)
    removal['study']=deepcopy(preserved);removal['study']['axes']=[]
    removal['study_replacement_review']={'reviewed_by':'reviewer','reviewed_at':'2026-11-01T00:00:00Z','rationale':'Incorrect intervention mapping intentionally removed'}
    with db:e.apply_batch(db,removal)
    assert e._get(db,'studies',identity)['axes']==[]
    assert e._get(db,'studies',identity)['context']==original['context']


@pytest.mark.parametrize('linked',[True,False])
def test_extractor_outcomes_preserve_source_results_separately(db,batch,linked):
    from scripts.query_interventions import prespecified_outcome,reported_outcome,adverse_event_outcome
    outcomes=[prespecified_outcome('Planned height'),reported_outcome('Posted height',value='-1.2',unit='cm'),adverse_event_outcome('Nausea',value='5'),{'measure':'Unspecified legacy outcome'}]
    row={'source':'CT.gov','study_id':'NCT-FIXTURE' if linked else 'NCT-UNLINKED','outcomes':outcomes}
    result=e.decorate(db,[row])[0]
    assert outcomes[0] in result['planned_outcomes']
    assert all(x not in result['planned_outcomes'] for x in outcomes[1:])
    assert result['source_reported_outcomes']==outcomes[1:3]
    assert result['source_unspecified_outcomes']==outcomes[3:]
    assert result['outcomes']==outcomes
