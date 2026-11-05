import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from live_results import local_only

def test_isrctn_local_results_only_for_discovered_study_and_no_api_alias():
    studies=[{'source':s,'study_id':s,'study_identity':'https://example.org/'+s,'registry_records':[{'source':s,'record_id':s}]} for s in ['ctgov','isrctn','aea','who-ictrp']]
    studies[-1]['registry_records'].append({'source':'ctgov','record_id':'NCTalias'})
    compact={'studies':studies,'rows':[{'study':s['study_identity']} for s in studies]}
    payload={'results':[{'source':'ISRCTN','study_id':'isrctn'}]}
    out=local_only(compact,payload)
    assert [s['source'] for s in out['studies']]==['isrctn']
    assert out['rows']==[{'study':'https://example.org/isrctn'}]
    assert len(compact['studies'])==4


def test_live_verified_api_alias_overrides_stale_local_metadata():
    study={'source':'aea','study_id':'AEA1','study_identity':'https://example.org/study','registry_records':[{'source':'aea','record_id':'AEA1'}]}
    compact={'studies':[study],'rows':[{'study':study['study_identity']}]}
    live={'results':[{'source':'CT.gov','study_id':'NCT1','study_identity':study['study_identity'],'registry_records':[{'source':'aea','record_id':'AEA1'},{'source':'ctgov','record_id':'NCT1'}]}]}
    assert local_only(compact,live)['rows']==[]

def test_ctgov_publication_fallback_requires_live_explicit_absence():
    study={'source':'ctgov','study_id':'NCT1','study_identity':'https://example.org/study','registry_records':[{'source':'ctgov','record_id':'NCT1'}]}
    compact={'studies':[study],'rows':[{'study':study['study_identity']}]}
    row={'source':'CT.gov','study_id':'NCT1','api_results':{'publication_fallback_eligible':True}}
    assert local_only(compact,{'results':[row]})['rows']==compact['rows']
    for result in ({}, {'publication_fallback_eligible':False}):
        assert local_only(compact,{'results':[{**row,'api_results':result}]})['rows']==[]
    assert local_only(compact,{'results':[]})['rows']==[]
