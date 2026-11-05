import copy
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from live_result_rules import interpret,PREFIX
from live_results import assemble
from query_v2 import canonical_query
I=PREFIX+'vocab/interventions/NutritionEducation'

def study(title='Height-for-age z-score',unit='z-score'):
 return {'hasResults':True,'protocolSection':{'identificationModule':{'nctId':'NCT99999999','briefTitle':'An unseen trial'},'armsInterventionsModule':{'armGroups':[{'label':'Education','type':'EXPERIMENTAL'},{'label':'Control','type':'NO_INTERVENTION'}],'interventions':[{'name':'education','type':'BEHAVIORAL','armGroupLabels':['Education']}]}},'resultsSection':{'outcomeMeasuresModule':{'outcomeMeasures':[{'title':title,'unitOfMeasure':unit,'paramType':'MEAN','reportingStatus':'POSTED','timeFrame':'6 months','groups':[{'id':'G1','title':'Education'},{'id':'G2','title':'Control'}],'classes':[{'categories':[{'measurements':[{'groupId':'G1','value':'0.3'},{'groupId':'G2','value':'0.1'}]}]}],'analyses':[{'groupIds':['G1','G2'],'groupDescription':'Education minus Control','paramType':'Mean Difference','paramValue':'0.2','ciPctValue':'95','ciNumSides':'TWO_SIDED','ciLowerLimit':'0.1','ciUpperLimit':'0.3'}]}]}}}

def run(s):return interpret(s,{'education':(I,'Education')})
def outcome(s):return s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'][0]

@pytest.mark.parametrize('title,unit,state',[('Height-for-age z-score','z-score','Stunting'),('Systolic blood pressure','mmHg','CardiovascularDiseases'),('HbA1c','%','NonCommunicableDiseases')])
def test_unseen_studies_three_states(title,unit,state):
 r=run(study(title,unit)); assert r['status']=='available';assert len(r['rows'])==3
 assert PREFIX+'vocab/states/'+state in r['rows'][0]['states']
 assert r['rows'][0]['direction']=='unknown/unsupported'
 assert r['rows'][2]['direction']=='increase'
 assert r['rows'][2]['summary_mapping']['tested']==[I]

@pytest.mark.parametrize('stat,operator,value,direction',[('Mean Difference','minus','-0.2','decrease'),('Risk Ratio','divided by','0.7','decrease'),('Odds Ratio','divided by','1.5','increase')])
def test_explicit_orientation_and_statistic(stat,operator,value,direction):
 s=study();a=outcome(s)['analyses'][0];a.update(paramType=stat,paramValue=value,groupDescription='Education '+operator+' Control')
 r=run(s)['rows'][-1];assert r['direction']==direction
 a['groupDescription']='Control '+operator+' Education'
 assert run(s)['rows'][-1]['direction']!=direction

def test_ambiguous_orientation_and_components_fail_closed():
 s=study();a=outcome(s)['analyses'][0];a['groupDescription']='Education versus Control'
 r=run(s)['rows'][-1];assert r['direction']=='unknown/unsupported';assert 'summary_mapping' not in r
 s=study();s['protocolSection']['armsInterventionsModule']['interventions'][0]['name']='unmapped drug'
 assert 'summary_mapping' not in run(s)['rows'][-1]

def test_changed_semantics_values_and_uncertainty():
 s=study();a=outcome(s)['analyses'][0];a.update(paramValue='-0.2',ciLowerLimit='-0.5',ciUpperLimit='0.3')
 r=run(s)['rows'][-1];assert r['direction']=='decrease'
 a.update(ciLowerLimit='0.9',ciUpperLimit='0.1');r=run(s)['rows'][-1]
 assert r['direction']=='decrease';assert r['comparison']['numerical_validation']['confidence_interval']['status']=='invalid'
 outcome(s)['title']='Unknown transformed score';assert not run(s)['rows']

def test_invalid_percent_and_counts_not_effects():
 s=study('HbA1c','%');outcome(s)['classes'][0]['categories'][0]['measurements'][0]['value']='162'
 assert run(s)['rows'][0]['value'] is None
 outcome(s)['paramType']='COUNT_OF_PARTICIPANTS'
 r=run(s)['rows'][0];assert r['value']=='162';assert r['unit']=='participants';assert 'summary_mapping' not in r

def test_duplicate_findings_and_distinct_timepoints():
 s=study();o=outcome(s);o['analyses'].append(copy.deepcopy(o['analyses'][0]));r=run(s)
 assert len(r['rows'])==3
 o['analyses'][1]['paramValue']='0.25';assert len(run(s)['rows'])==4
 other=copy.deepcopy(o);other['timeFrame']='12 months';s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'].append(other)
 assert len(run(s)['rows'])==8

def test_api_snapshot_cannot_add_study_or_results():
 s=study();payload={'results':[{'source':'CT.gov','study_id':'NCT99999999','title':'Live title','api_results':run(s)}], 'meta':{'truncated':True}}
 local={'status':'available','graph':'urn:old','studies':[{'source':'CT.gov','study_identity':'urn:old','study_id':'NCT00000001'}], 'rows':[{'study':'urn:old','measurement':'urn:paper'}]}
 out=assemble(payload,canonical_query({'state':[PREFIX+'vocab/states/Stunting']},{}),local)
 assert len(out['results'])==1;assert out['results'][0]['title']=='Live title'
 assert len(out['reviewed_results']['rows'])==3;assert out['reviewed_results']['partial']
 assert all(r['study']!= 'urn:old' for r in out['reviewed_results']['rows'])
 assert 'api_results' in payload['results'][0]

def test_isrctn_links_are_not_results_and_failures_are_explicit():
 payload={'results':[{'source':'ISRCTN','study_id':'ISRCTN123','title':'Live ISRCTN','report_metadata':{'url':'https://example.org/paper.pdf'}}], 'meta':{}}
 out=assemble(payload,canonical_query({'state':[PREFIX+'vocab/states/Stunting']},{}))
 assert out['reviewed_results']['rows']==[]
 assert out['reviewed_results']['coverage'][0]['status']=='no_supported_api_numerical_results'
 assert interpret({'broken':True})['status']=='invalid_source'

def test_shared_components_are_not_credited_and_packages_stay_combined():
 s=study();mod=s['protocolSection']['armsInterventionsModule']
 mod['interventions'] += [{'name':'shared','type':'BEHAVIORAL','armGroupLabels':['Education','Control']},{'name':'food','type':'DIETARY_SUPPLEMENT','armGroupLabels':['Education']}]
 mapping={'Education':(I,'Education'),'shared':('urn:shared','Shared'),'food':('urn:food','Food')}
 row=interpret(s,mapping)['rows'][-1];m=row['summary_mapping']
 assert set(m['tested'])=={I,'urn:food'};assert m['background']==['urn:shared'];assert m['package']

def test_dispersion_and_within_arm_change_keep_their_roles():
 s=study('Change in HbA1c','%');o=outcome(s);o['dispersionType']='STANDARD_ERROR_OF_MEAN';o['classes'][0]['categories'][0]['measurements'][0].update(value='-1.2',spread='0.1')
 row=run(s)['rows'][0];assert row['value']=='-1.2';assert row['comparison']['kind']=='within_arm_change'
 assert row['uncertainty']['dispersionType']=='STANDARD_ERROR_OF_MEAN'
 assert row['comparison']['numerical_validation']['dispersion']['status']=='valid'
 assert 'summary_mapping' not in row

def test_different_source_versions_not_silently_overwritten():
 from query_v2 import dedupe_presentation
 s=study();left=run(s);outcome(s)['analyses'][0]['paramValue']='0.4';right=run(s)
 base={'source':'CT.gov','study_id':'NCT99999999','intervention_concept_uri':I}
 rows=dedupe_presentation([{**base,'api_results':left},{**base,'api_results':right}])
 assert rows[0]['api_results']['source_versions_differ']
 assert {'0.2','0.4'}.issubset({r['value'] for r in rows[0]['api_results']['rows']})

def test_live_adapter_reuses_returned_records_and_obeys_cap(monkeypatch):
 import asyncio,httpx,query_v2 as engine
 samples=[]
 for i in range(201):
  s=study();s['protocolSection']['identificationModule']['nctId']=f'NCT{i:08d}';samples.append(s)
 def mapped(raw,*args,**kwargs):
  sid=raw['protocolSection']['identificationModule']['nctId']
  return {'source':'CT.gov','nctId':sid,'title':'Live title','_intervention_names':['Education'], 'condition_mesh_uris':[], 'intervention_mesh_uris':[],'outcomes':[]}
 monkeypatch.setattr(engine.v1,'map_ctgov_study',mapped)
 monkeypatch.setattr(engine.v1,'_load_ctgov_intervention_concept_map',lambda:{'Education':(I,'Education')})
 monkeypatch.setattr(engine.v1,'_load_ctgov_outcome_concept_map',lambda:{})
 calls=[]
 def response(request):
  calls.append(request);return httpx.Response(200,json={'studies':samples},request=request)
 adapter=engine.CtgovAdapter(lambda:httpx.AsyncClient(transport=httpx.MockTransport(response)))
 branch=asyncio.run(adapter.execute({'region':engine.WORLD_URI},engine.SourceBudget(),engine.default_region_index()))
 assert len(calls)==1;assert branch.truncated
 assert len({r['study_id'] for r in branch.rows})==200
 assert all(r['api_results']['status']=='available' for r in branch.rows)
 assert 'ResultsSection' in calls[0].url.params['fields']
 assert all('NCT00000200' not in str(r) for r in branch.rows)

def test_rules_use_existing_indicator_identities():
 from rdflib import Graph,URIRef,RDF,Namespace
 from live_result_rules import MEASURES
 root=Path(__file__).resolve().parents[1]
 g=Graph().parse(root/'vocabularies/indicators.ttl');ue=Namespace(PREFIX+'ontology/')
 for name,_,states,*_ in MEASURES:
  uri=URIRef(PREFIX+'vocab/indicators/'+name)
  assert (uri,RDF.type,ue.Indicator) in g
  for state in states:assert (uri,ue.measures,URIRef(PREFIX+'vocab/states/'+state)) in g

def test_local_aea_payload_is_preserved_exactly():
 row={'study':'urn:aea','measurement':'urn:m','value':'0.4','summary_mapping':{'tested':[I]},'intervention_labels':{I:'Education'}}
 local={'status':'available','graph':'urn:local','rows':[row], 'studies':[{'source':'AEA','study_id':'AEARCTR-1','study_identity':'urn:aea','title':'Paper study','registry_records':[]}]}
 payload={'results':[{'source':'AEA','study_id':'AEARCTR-1','study_identity':'urn:aea','title':'Paper study'}],'meta':{}}
 out=assemble(payload,canonical_query({'state':[PREFIX+'vocab/states/Stunting']},{}),local)
 assert [{k:v for k,v in r.items() if k!='preferred_labels'} for r in out['reviewed_results']['rows']]==[row]
 assert 'preferred_labels' not in row  # Projection must not mutate stored evidence.
 assert out['reviewed_results']['rows'][0]['preferred_labels'][I]=='Nutrition education'
 assert out['results'][0]['title']=='Paper study'

def test_projection_deadline_preserves_studies(monkeypatch):
 import asyncio,time,live_results
 from query_runtime import REQUEST_DEADLINE
 async def run():
  token=REQUEST_DEADLINE.set(time.monotonic()+.01)
  try:
   async def live():return {'results':[{'source':'CT.gov','study_id':'NCT99999999','title':'Real title','api_results':{'rows':[]}}],'meta':{}}
   result=await live_results.combine(live(),canonical_query({'state':[PREFIX+'vocab/states/Stunting']},{}))
   assert result['results']==[{'source':'CT.gov','study_id':'NCT99999999','title':'Real title'}]
   assert result['reviewed_results']['status']=='unavailable'
  finally:REQUEST_DEADLINE.reset(token)
 monkeypatch.delenv('UE_COMPACT_RESULTS_GRAPH',raising=False)
 asyncio.run(run())

def test_existing_verified_identity_stays_coherent():
 identity=PREFIX+'studies/aea/verified-alias'
 payload={'results':[{'source':'CT.gov','study_id':'NCT99999999','study_identity':identity,'api_results':run(study())}], 'meta':{}}
 out=assemble(payload,canonical_query({'state':[PREFIX+'vocab/states/Stunting']},{}))
 assert out['results'][0]['study_identity']==identity
 assert all(r['study']==identity for r in out['reviewed_results']['rows'])
 assert out['reviewed_results']['rows'][-1]['summary_mapping']['study']==identity

@pytest.mark.parametrize('text,estimand',[
 ('Treatment by sex interaction coefficient','interaction'),
 ('Association between exposure and outcome','association'),
 ('Log transformed regression coefficient','transformed'),
 ('Model-specific quantity with unclear meaning','unsupported')])
def test_incompatible_estimands_keep_values_but_never_main_effects(text,estimand):
 s=study();outcome(s)['analyses'][0]['statisticalComment']=text
 r=run(s)['rows'][-1]
 assert r['value']=='0.2';assert 'summary_mapping' not in r
 assert r['comparison']['estimand']==estimand
 assert r['direction']=='unknown/unsupported'
 assert run(s)['coverage'][-1]['reason']

@pytest.mark.parametrize('title,unit,included,excluded',[
 ('Systolic blood pressure','mmHg',['Hypertension','IsolatedSystolicHypertension'],['Stroke','DiabetesMellitus']),
 ('HbA1c','%',['Diabetes','DiabetesMellitus','Type2DiabetesMellitus'],['Hypertension','DiabetesInsipidusNephrogenic'])])
def test_specific_state_relevance_does_not_include_siblings(title,unit,included,excluded):
 payload={'results':[{'source':'CT.gov','study_id':'NCT99999999','api_results':run(study(title,unit))}],'meta':{}}
 for name in included+excluded:
  query=canonical_query({'state':[PREFIX+'vocab/states/'+name]}, {})
  rows=assemble(payload,query)['reviewed_results']['rows']
  assert len(rows)==(3 if name in included else 0),name

@pytest.mark.parametrize('field,value',[('armGroupLabels',None),('armGroupLabels','Education'),('armGroupLabels',[{}]),('name',None)])
def test_nested_protocol_validation(field,value):
 s=study();s['protocolSection']['armsInterventionsModule']['interventions'][0][field]=value
 r=run(s);assert r['status']=='invalid_source';assert field in r['reason']

def test_mixed_healthy_and_malformed_records_through_actual_adapter(monkeypatch):
 import asyncio,httpx,query_v2 as engine
 good=study('Systolic blood pressure','mmHg');bad=copy.deepcopy(good)
 bad['protocolSection']['identificationModule']['nctId']='NCT88888888'
 bad['protocolSection']['armsInterventionsModule']['interventions'][0]['armGroupLabels']=None
 worse=copy.deepcopy(bad);worse['protocolSection']['identificationModule']['nctId']='NCT77777777'
 worse['protocolSection']['armsInterventionsModule']['interventions']=None
 monkeypatch.setattr(engine.v1,'_load_ctgov_intervention_concept_map',lambda:{'education':(I,'Education')})
 monkeypatch.setattr(engine.v1,'lookup_intervention_concepts_by_label',lambda _: {})
 client=lambda:httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'studies':[bad,good,worse]},request=req)))
 branch=asyncio.run(engine.CtgovAdapter(client).execute({'state':PREFIX+'vocab/states/Hypertension'},engine.SourceBudget(),engine.default_region_index()))
 found={r['study_id']:r['api_results'] for r in branch.rows}
 assert found['NCT99999999']['status']=='available'
 assert found['NCT88888888']['status']==found['NCT77777777']['status']=='invalid_source'
 output=assemble({'results':branch.rows,'meta':{}},canonical_query({'state':[PREFIX+'vocab/states/Hypertension']},{}))
 assert len(output['reviewed_results']['rows'])==3
 assert sum(c['status']=='invalid_source' for c in output['reviewed_results']['coverage'])==2

@pytest.mark.parametrize('field,value',[('estimateComment','interaction coefficient'),('pValueComment','association test'),('testedNonInferiority',True)])
def test_additional_source_estimand_fields_cannot_bypass_context_gate(field,value):
 s=study();outcome(s)['analyses'][0][field]=value
 r=run(s)['rows'][-1]
 assert r['value']=='0.2';assert r['comparison']['semantics']=='unsupported'
 assert 'summary_mapping' not in r
