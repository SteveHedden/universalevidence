import copy
import io
import json
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
import pytest
from rdflib import Graph, Literal, RDF, URIRef
from rdflib.namespace import SKOS
from scripts import ctgov_results as cr

ROOT=Path(__file__).resolve().parents[1]
HERE=ROOT/'tests/fixtures/ctgov_results'
@pytest.fixture(scope='module')
def assets():
 rules=json.loads((HERE/'rules.json').read_text())
 cw=Graph()
 for i,r in enumerate(rules['outcomes']):
  e=URIRef('urn:test:crosswalk:'+str(i));cw.add((e,cr.UE.rawText,Literal(r['key'])));cw.add((e,SKOS.closeMatch,URIRef(r['indicator'])))
 root=ROOT
 tax=Graph().parse(HERE/'taxonomy.ttl')
 payloads={sid:json.loads((HERE/f'{sid}.json').read_text()) for sid in ['NCT02472262','NCT00659061','NCT04002908']}
 return rules,cw,tax,payloads

def response(s):
 sid=s['protocolSection']['identificationModule']['nctId']
 return cr.Response(s,cr.digest(s),datetime.now(timezone.utc).isoformat(),0,cr.BASE+sid)

def rule(**changes):
 r=dict(kind='source_comparison',statistic='difference',orientation='forward',semantics='explicit',quantity='stunting prevalence',scale='identity')
 r.update(changes);return r

@pytest.mark.parametrize('stat,value,orientation,expected',[
 ('difference','-2','forward','decrease'),('difference','-2','reverse','increase'),
 ('difference','2','forward','increase'),('difference','0.00','forward','exactly-zero-difference'),
 ('ratio','0.8','forward','decrease'),('ratio','0.8','reverse','increase'),
 ('ratio','1.00','reverse','exactly-zero-difference'),('ratio','1.2','forward','increase'),
 ('log_ratio','-0.2','forward','decrease'),('log_ratio','-0.2','reverse','increase'),
 ('log_ratio','0','forward','exactly-zero-difference')])
def test_null_and_orientation(stat,value,orientation,expected):
 r=rule(statistic=stat,orientation=orientation,scale='explicit_log_ratio' if stat=='log_ratio' else 'identity')
 assert cr.classify(value,r)['direction']==expected

@pytest.mark.parametrize('value',[None,'','<0.1','> 2','NA','NaN','Infinity','1,234',True])
def test_missing_and_nonscalar(value):
 assert cr.classify(value,rule())['direction']=='unknown/unsupported'

@pytest.mark.parametrize('changes',[{'orientation':None},{'semantics':None},{'scale':'log unspecified'},
 {'kind':'arm_level'},{'statistic':'hazard coefficient'},{'statistic':'log_ratio'},{'quantity':None}])
def test_fail_closed(changes):
 assert cr.classify('-1',rule(**changes))['direction']=='unknown/unsupported'

@pytest.mark.parametrize('value',['0','-1'])
def test_invalid_ratio(value):
 assert cr.classify(value,rule(statistic='ratio'))['direction']=='unknown/unsupported'

def test_transformed_quantity_stays_scoped():
 for scale,quantity in [('measured_reverse_coded','reverse-coded functioning score'),('measured_transformed','log concentration'),('identity','non-stunted prevalence')]:
  result=cr.classify('2',rule(scale=scale,quantity=quantity))
  assert result['direction']=='increase' and result['quantity']==quantity
 assert cr.classify('0.0',rule())['zero_interpretation'].startswith('Reported null')

def test_three_live_contexts_and_raw_recovery(assets):
 rules,cw,tax,payloads=assets
 graphs=Graph();records=[]
 for s in payloads.values():
  rows,coverage=cr.map_response(response(s),cw,tax,rules)
  graph=cr.build_graph(response(s),rows,rules,tax);graphs+=graph;records+=rows
  assert json.loads(str(graph.value(URIRef(response(s).url+'#sha256-'+response(s).sha256),cr.PROV.value)))==s
  assert len(coverage)==len(s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'])
 mapped=[r for r in records if r['mapping']]
 assert len(mapped)==11 # 3 changes + 4 levels + 2 tests + 1 difference + 1 level
 assert len(set(r['study'] for r in mapped))==3
 assert len(list(graphs.subjects(RDF.type,cr.CIDS.IndicatorReport)))==5
 assert all(r['classification']['direction']=='decrease' for r in mapped if r['kind']=='within_arm_change')
 diff=next(r for r in mapped if r['kind']=='derived_comparison')
 assert diff['value']=='1' and diff['unit']=='percentage points'
 assert diff['context']['operand_values']==['62','61']
 assert diff['classification']['direction']=='increase'
 assert 'Advice Only' in diff['comparison']['comparison']
 assert not diff['raw'] and 'no effect CI' in diff['context']['limitations']
 assert all(r['classification']['direction']=='unknown/unsupported' for r in mapped if r['kind']=='arm_level')
 assert len(list(graphs.subjects(RDF.type,cr.OBO.STATO_0000196)))==3
 assert len(list(graphs.subjects(RDF.type,cr.OBO.STATO_0000237)))==1
 # Existing source CIs and SD remain distinct; no derived comparison is a level report.
 for r in mapped:
  if r['kind']=='derived_comparison':
   n=URIRef(response(payloads[r['study']]).url+'#sha256-'+response(payloads[r['study']]).sha256+r['locator'])
   assert (n,RDF.type,cr.CIDS.IndicatorReport) not in graphs

def test_changed_values_flow_without_rule_change(assets):
 rules,cw,tax,payloads=assets;s=copy.deepcopy(payloads['NCT00659061'])
 old=cr.digest(rules);o=s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'][6]
 o['classes'][1]['categories'][0]['measurements'][0]['value']='59'
 rows,_=cr.map_response(response(s),cw,tax,rules);r=next(r for r in rows if r['kind']=='derived_comparison')
 assert r['value']=='-2' and r['classification']['direction']=='decrease' and cr.digest(rules)==old

@pytest.mark.parametrize('change',['group','class','category','units','population','method','eligibility','not_posted'])
def test_semantic_changes_invalidate(assets,change):
 rules,cw,tax,payloads=assets;s=copy.deepcopy(payloads['NCT00659061']);o=s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'][6]
 if change=='group':o['groups'][0]['description']='Advice only now'
 if change=='class':o['classes'][1]['title']='Different follow-up'
 if change=='category':o['classes'][0]['categories'][0]['title']='New subgroup'
 if change=='units':o['unitOfMeasure']='count'
 if change=='population':o['populationDescription']='Adults'
 if change=='method':o['analyses'][0]['statisticalMethod']='Different model'
 if change=='eligibility':s['protocolSection']['eligibilityModule']['minimumAge']='10 Years'
 if change=='not_posted':o['reportingStatus']='NOT_POSTED'
 rows,cov=cr.map_response(response(s),cw,tax,rules)
 assert not any(r['mapping'] for r in rows)
 assert all(r['classification']['direction']=='unknown/unsupported' for r in rows)
 assert not cov[6]['supported'] and cov[6]['reason']

@pytest.mark.parametrize('mode',['missing','ambiguous'])
def test_invalid_crosswalks(assets,mode):
 rules,cw,tax,payloads=assets;new=Graph()
 if mode=='ambiguous':
  new+=cw
  entry=next(new.subjects(cr.UE.rawText,Literal(rules['outcomes'][0]['key'])))
  new.add((entry,SKOS.exactMatch,URIRef(rules['outcomes'][1]['indicator'])))
 rows,_=cr.map_response(response(payloads['NCT02472262']),new,tax,rules)
 assert not any(r['mapping'] for r in rows)

def test_analysis_ci_crossing_null_and_threshold_p(assets):
 rules,cw,tax,payloads=assets;rules=copy.deepcopy(rules);s=copy.deepcopy(payloads['NCT00659061'])
 o=s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'][6]
 o['analyses']=[{'groupIds':['OG000','OG001'],'paramType':'Risk Ratio','paramValue':'0.8','ciLowerLimit':'0.4','ciUpperLimit':'1.4','ciPctValue':'95','pValue':'>0.05','groupDescription':'Sprinkle-Advice over Advice Only','numParticipants':573}]
 cfg=rules['outcomes'][1];cfg['context_sha256']=cr.digest(cr.semantic_context(s,o));cfg['analysis_rules']={'0':rule(statistic='ratio',source_param_type='Risk Ratio',quantity='stunting risk',unit='dimensionless')}
 rows,_=cr.map_response(response(s),cw,tax,rules);a=next(r for r in rows if r['kind']=='source_comparison' and r['outcome'].endswith('/6'))
 assert a['classification']['direction']=='decrease' and a['raw']['pValue']=='>0.05'
 assert a['raw']['numParticipants']==573 and 'outcome_denoms' not in a['context']
 graph=cr.build_graph(response(s),rows,rules,tax)
 assert list(graph.subjects(RDF.type,cr.OBO.STATO_0000196))
 p=next(graph.subjects(RDF.type,cr.OBO.STATO_0000700))
 assert graph.value(p,cr.OBO.STATO_0000129) is None

def test_missing_denominator_bounds_preserves_result(assets):
 rules,cw,tax,payloads=assets;rules=copy.deepcopy(rules);s=copy.deepcopy(payloads['NCT02472262'])
 o=s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'][0];o.pop('denoms')
 o['classes'][0]['categories'][0]['measurements'][0].pop('upperLimit')
 rules['outcomes'][0]['context_sha256']=cr.digest(cr.semantic_context(s,o))
 rows,_=cr.map_response(response(s),cw,tax,rules)
 r=rows[0];assert r['classification']['direction']=='decrease' and r['context']['outcome_denoms'] is None
 graph=cr.build_graph(response(s),rows,rules,tax)
 assert len(list(graph.subjects(RDF.type,cr.OBO.STATO_0000314)))==2

def test_client_reuse_caps_failures_and_duplicate_studies(assets):
 rules,cw,tax,payloads=assets;calls=[]
 def opener(url,timeout):
  calls.append((url,timeout));return io.BytesIO(json.dumps(payloads['NCT02472262']).encode())
 client=cr.LiveClient(max_requests=1,opener=opener)
 result=cr.query_studies(['NCT02472262','NCT02472262','NCT00659061'],cw,tax,rules,client)
 assert len(calls)==1 and result['study_count']==1 and len(result['failures'])==1
 assert client.get('NCT02472262') is result['responses'][0]
 def fail(url,timeout):raise TimeoutError('source timed out')
 result=cr.query_studies(['NCT02472262'],cw,tax,rules,cr.LiveClient(opener=fail))
 assert result['study_count']==0 and result['rows']==[] and len(result['failures'])==1

def test_full_unchanged_local_shapes(assets):
 import pyshacl
 rules,cw,tax,payloads=assets;g=Graph()
 for s in payloads.values():
  rows,_=cr.map_response(response(s),cw,tax,rules);g+=cr.build_graph(response(s),rows,rules,tax)
 shapes=Graph().parse(ROOT/'ontology/ue.ttl')
 ok,_,report=pyshacl.validate(g,shacl_graph=shapes,advanced=True)
 assert ok, report
 # Ensure we did not quietly remove an applicable shape: invalid direction fails.
 node=next(g.subjects(RDF.type,cr.UE.OutcomeMeasurement));g.set((node,cr.UE.observedDirection,Literal('beneficial')))
 assert not pyshacl.validate(g,shacl_graph=shapes,advanced=True)[0]


@pytest.mark.parametrize('value', ['162', '-1'])
def test_invalid_percent_not_promoted_but_source_and_mapping_retained(assets, value):
 rules,cw,tax,payloads=assets;s=copy.deepcopy(payloads['NCT00659061'])
 s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'][6]['classes'][1]['categories'][0]['measurements'][0]['value']=value
 rows,coverage=cr.map_response(response(s),cw,tax,rules)
 invalid=next(r for r in rows if r['source_value']==value)
 assert invalid['mapping'] and invalid['value'] is None and not invalid['numerically_usable']
 assert invalid['raw']['value']==value and invalid['numerical_validation']['estimate']['status']=='invalid'
 assert coverage[6]['mapping_supported'] and coverage[6]['usable_numeric_records']==3
 assert coverage[6]['numerical_issues']
 derived=next(r for r in rows if r['kind']=='derived_comparison')
 assert derived['value'] is None and derived['classification']['direction']=='unknown/unsupported'
 graph=cr.build_graph(response(s),rows,rules,tax)
 node=URIRef(response(s).url+'#sha256-'+response(s).sha256+invalid['locator'])
 assert graph.value(node,cr.UE.value) is None and graph.value(node,cr.UE.estimate) is None
 assert (node,RDF.type,cr.CIDS.IndicatorReport) not in graph
 assert str(graph.value(node,cr.UE.reportedValue))==value
 assert 'Percentage must be' in str(graph.value(node,cr.UE.result_text))
 assert all(row.get('value')!=value for row in [{str(k):str(v) for k,v in r.asdict().items()} for r in graph.query(cr.QUERY)])


def test_reversed_ci_isolated_from_valid_estimate(assets):
 rules,cw,tax,payloads=assets;s=copy.deepcopy(payloads['NCT02472262'])
 m=s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'][0]['classes'][0]['categories'][0]['measurements'][0]
 m.update(lowerLimit='5',upperLimit='-5')
 rows,coverage=cr.map_response(response(s),cw,tax,rules);r=rows[0]
 assert r['numerical_validation']['confidence_interval']['status']=='invalid'
 assert r['numerically_usable'] and r['classification']['direction']=='decrease'
 assert coverage[0]['mapping_supported'] and coverage[0]['usable_numeric_records']==3
 assert coverage[0]['numerical_issues'][0]['component']=='confidence_interval'
 graph=cr.build_graph(response(s),rows,rules,tax)
 node=URIRef(response(s).url+'#sha256-'+response(s).sha256+r['locator'])
 assert graph.value(node,cr.UE.uncertaintyInterval) is None
 assert graph.value(node,cr.UE.value)==Literal(Decimal('-0.14'))
 assert len(list(graph.subjects(RDF.type,cr.OBO.STATO_0000196)))==2
 assert json.loads(str(graph.value(node,cr.UE.uncertainty)))['lowerLimit']=='5'


@pytest.mark.parametrize('bounds', [('0.2','0.3'), (None,'0.3'), ('-0.3',None)])
def test_ci_not_required_to_contain_estimate_and_one_sided_retained(assets,bounds):
 rules,cw,tax,payloads=assets;s=copy.deepcopy(payloads['NCT02472262'])
 m=s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'][0]['classes'][0]['categories'][0]['measurements'][0]
 for key,value in zip(['lowerLimit','upperLimit'],bounds):
  if value is None:m.pop(key)
  else:m[key]=value
 rows,_=cr.map_response(response(s),cw,tax,rules)
 assert rows[0]['numerical_validation']['confidence_interval']['status']=='valid'
 graph=cr.build_graph(response(s),rows,rules,tax)
 assert len(list(graph.subjects(RDF.type,cr.OBO.STATO_0000196)))==3


@pytest.mark.parametrize('dispersion', ['Standard Deviation','Standard Error'])
def test_negative_dispersion_suppressed_independently(assets,dispersion):
 rules,cw,tax,payloads=assets;s=copy.deepcopy(payloads['NCT04002908']);rules=copy.deepcopy(rules)
 o=s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'][0];o['dispersionType']=dispersion
 rules['outcomes'][2]['context_sha256']=cr.digest(cr.semantic_context(s,o))
 o['classes'][0]['categories'][0]['measurements'][0]['spread']='-1.28'
 rows,_=cr.map_response(response(s),cw,tax,rules)
 assert rows[0]['numerical_validation']['dispersion']['status']=='invalid'
 assert rows[0]['numerically_usable']
 graph=cr.build_graph(response(s),rows,rules,tax)
 assert len(list(graph.subjects(RDF.type,cr.CIDS.IndicatorReport)))==1
 assert not list(graph.subjects(RDF.type,cr.DISPERSION_TYPES[dispersion]))


def test_ratio_domain_blocks_structured_estimate(assets):
 rules,cw,tax,payloads=assets;rules=copy.deepcopy(rules);s=copy.deepcopy(payloads['NCT00659061'])
 o=s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'][6]
 o['analyses']=[{'groupIds':['OG000','OG001'],'paramType':'Risk Ratio','paramValue':'-0.8','ciLowerLimit':'0.4','ciUpperLimit':'1.4'}]
 rules['outcomes'][1]['context_sha256']=cr.digest(cr.semantic_context(s,o))
 rules['outcomes'][1]['analysis_rules']={'0':rule(statistic='ratio',source_param_type='Risk Ratio',unit='dimensionless')}
 rows,_=cr.map_response(response(s),cw,tax,rules);r=next(r for r in rows if r['kind']=='source_comparison')
 assert r['value'] is None and r['source_value']=='-0.8'
 assert r['numerical_validation']['estimate']['status']=='invalid'
 graph=cr.build_graph(response(s),rows,rules,tax)
 node=URIRef(response(s).url+'#sha256-'+response(s).sha256+r['locator'])
 assert graph.value(node,cr.UE.estimate) is None


@pytest.mark.parametrize('mutation', ['classes_null','group_null','group_id_array','measurement_array','denoms_null','analyses_object','group_ids_object','title_array','value_object'])
def test_middle_study_shape_failure_is_atomic_and_later_study_continues(assets,mutation):
 rules,cw,tax,payloads=assets;calls=[]
 class Client:
  def get(self,sid):
   calls.append(sid);s=copy.deepcopy(payloads[sid])
   if sid=='NCT00659061':
    o=s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'][6]
    if mutation=='classes_null':o['classes']=None
    if mutation=='group_null':o['groups'][0]=None
    if mutation=='group_id_array':o['groups'][0]['id']=[]
    if mutation=='measurement_array':o['classes'][0]['categories'][0]['measurements'][0]=[]
    if mutation=='denoms_null':o['denoms']=None
    if mutation=='analyses_object':o['analyses']={}
    if mutation=='group_ids_object':o['analyses'][0]['groupIds']={}
    if mutation=='title_array':o['title']=[]
    if mutation=='value_object':o['classes'][0]['categories'][0]['measurements'][0]['value']={}
   return response(s)
 ids=list(payloads);r=cr.query_studies(ids,cw,tax,rules,Client())
 assert calls==ids and r['study_count']==2 and len(r['failures'])==1
 assert r['failures'][0]['study']=='NCT00659061' and r['failures'][0]['stage']=='source_data'
 assert 'expected' in r['failures'][0]['reason']
 assert any(c['study']=='NCT00659061' and not c['supported'] for c in r['coverage'])
 assert all(x['study']!='NCT00659061' for x in r['records'])
 assert len(r['rows'])==4
 assert not any('NCT00659061' in str(term) for triple in r['graph'] for term in triple)


@pytest.mark.parametrize('payload', [None, [], {'protocolSection':None}])
def test_live_client_reports_malformed_top_level_as_source_data(payload):
 client=cr.LiveClient(opener=lambda *args,**kwargs:io.BytesIO(json.dumps(payload).encode()))
 with pytest.raises(cr.SourceDataError):client.get('NCT02472262')


def test_graph_failure_isolated_but_programming_errors_not_silenced(assets,monkeypatch):
 rules,cw,tax,payloads=assets
 class Client:
  def get(self,sid):return response(payloads[sid])
 original=cr.build_graph
 def broken(resp,rows,rules,taxonomy):
  graph=original(resp,rows,rules,taxonomy)
  if resp.payload['protocolSection']['identificationModule']['nctId']=='NCT00659061':
   raise cr.SourceDataError('Injected source-data construction failure after local graph built')
  return graph
 monkeypatch.setattr(cr,'build_graph',broken)
 r=cr.query_studies(list(payloads),cw,tax,rules,Client())
 assert r['study_count']==2 and len(r['rows'])==4 and len(r['failures'])==1
 assert not any('NCT00659061' in str(term) for triple in r['graph'] for term in triple)
 def bug(*args):raise TypeError('Programming defect')
 monkeypatch.setattr(cr,'map_response',bug)
 with pytest.raises(TypeError,match='Programming defect'):
  cr.query_studies(list(payloads),cw,tax,rules,Client())
