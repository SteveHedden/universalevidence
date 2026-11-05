import sys,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from compact_results import merge,select_query,supported
STATE="https://universalevidence.com/vocab/states/Stunting"
from query_v2 import canonical_query
I='https://universalevidence.com/vocab/interventions/UnconditionalCashTransfer'
S='https://universalevidence.com/studies/isrctn/ISRCTN1'
def fixture():
 return {'status':'available','graph':'https://universalevidence.com/graph/test','studies':[{'study_identity':S,'source':'ISRCTN','study_id':'ISRCTN1','title':'Trial','registry_records':[{'source':'isrctn','record_id':'ISRCTN1'},{'source':'ctgov','record_id':'NCT00000001'}]}], 'rows':[{'study':S,'summary_mapping':{'tested':[I]},'intervention_labels':{I:'Cash'}}]}
def test_stored_study_outside_live_cap_is_added_and_counted():
 p={'results':[],'meta':{'truncated':True}};original=copy.deepcopy(p)
 out=merge(p,fixture());assert len(out['results'])==1
 assert out['meta']['returned_unique_studies']==1
 assert out['meta']['truncated'];assert p==original

def test_cross_registry_aliases_and_multiple_groups_deduplicate():
 p={'results':[{'source':'CT.gov','study_id':'NCT00000001','title':'Fresh title','intervention_concept_uri':I},{'source':'ISRCTN','study_id':'ISRCTN1','intervention_concept_uri':I}], 'meta':{}}
 out=merge(p,fixture());assert len(out['results'])==1
 assert out['results'][0]['title']=='Fresh title';assert out['results'][0]['study_identity']==S

def test_failures_do_not_erase_live_results_or_claim_no_evidence():
 p={'results':[{'study_id':'one'}],'meta':{}}
 out=merge(p,{'status':'unavailable','graph':'g','rows':[],'studies':[]})
 assert out['results']==p['results'];assert out['reviewed_results']['status']=='unavailable'

def test_filters_not_silently_broadened_and_iris_validated():
 import pytest
 q=canonical_query({'state':[STATE],'region':['https://universalevidence.com/vocab/regions/Kenya']},{})
 assert not supported(q)
 q=canonical_query({'state':[STATE],'intervention':[I,I+'2']},{'intervention':'and'})
 assert ' && ' in select_query(q,'https://universalevidence.com/graph/test')
 with pytest.raises(ValueError):select_query(q,'https://universalevidence.com/graph/> } UNION {')

def test_live_failure_preserves_reviewed_evidence(monkeypatch):
 import asyncio,compact_results
 async def live():raise TimeoutError()
 async def stored(query):return fixture()
 monkeypatch.setattr(compact_results,'read_compact',stored)
 out=asyncio.run(compact_results.combine(live(),canonical_query({'state':[STATE]},{})))
 assert len(out['results'])==1
 assert out['meta']['execution_status']=='partial'
