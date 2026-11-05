import copy,json
from pathlib import Path
import pytest
from rdflib import Graph,Literal,URIRef,RDF
from rdflib.namespace import SKOS
from scripts import ctgov_results as cr
from scripts.ctgov_indicator_context import indicator_key,parent_indicator_key,outcome_context
from scripts.ctgov_result_rules import save_rules,load_rules

SOURCE=Path(__file__).parent/'fixtures/ctgov_results/NCT03070600-classes.json'
def setup(indices=(1,)):
 s=json.loads(SOURCE.read_text());o=s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'][0]
 cw=Graph();tax=Graph();rules={'version':'class-test','outcomes':[]}
 for i in indices:
  key=indicator_key('NCT03070600',o,i);indicator=URIRef('urn:indicator:'+str(i));state=URIRef('urn:state:'+str(i));entry=URIRef('urn:crosswalk:'+str(i))
  cw.add((entry,cr.UE.rawText,Literal(key)));cw.add((entry,SKOS.closeMatch,indicator))
  tax.add((indicator,RDF.type,cr.UE.Indicator));tax.add((state,RDF.type,cr.UE.State));tax.add((indicator,cr.UE.measures,state))
  rules['outcomes'].append({'key':key,'indicator':str(indicator),'state':str(state),'context_sha256':cr.digest(cr.semantic_context(s,o)),'review_reason':'Reviewed exact class identity','arm_rule':{'kind':'arm_level','quantity':o['classes'][i]['title']}})
 return s,cw,tax,rules

def response(s):return cr.Response(s,cr.digest(s),'2026-10-06T00:00:00+00:00',0,cr.BASE+'NCT03070600')
def mapped(s,cw,tax,rules):return cr.map_response(response(s),cw,tax,rules)

def test_haz_only_preserves_all_rows_and_denominator_scopes():
 s,cw,tax,rules=setup();rows,coverage=mapped(s,cw,tax,rules)
 assert len(rows)==6
 hits=[r for r in rows if r['mapping']]
 assert len(hits)==2 and {r['value'] for r in hits}=={'-0.35','-0.47'}
 assert all('/classes/1/' in r['locator'] for r in hits)
 assert hits[0]['context']['class_denoms'][0]['counts'][0]['value']=='1777'
 assert hits[0]['context']['outcome_denoms'][0]['counts'][0]['value']=='1847'
 assert all(r['classification']['direction']=='unknown/unsupported' for r in rows)
 assert coverage[0]['usable_numeric_records']==2 and not coverage[0]['mapping_complete']
 assert [c['supported'] for c in coverage[0]['sections']]==[False,True,False]
 graph=cr.build_graph(response(s),rows,rules,tax)
 nodes=list(graph.subjects(RDF.type,cr.UE.OutcomeMeasurement));assert len(nodes)==2
 assert all(list(graph.objects(n,cr.UE.ofIndicator))==[URIRef('urn:indicator:1')] for n in nodes)
 assert json.loads(str(graph.value(URIRef(response(s).url+'#sha256-'+response(s).sha256),cr.PROV.value)))==s

def test_three_indicators_do_not_duplicate_or_cross_assign_values():
 s,cw,tax,rules=setup((0,1,2));rows,cov=mapped(s,cw,tax,rules)
 assert len(rows)==len({r['locator'] for r in rows})==6 and cov[0]['mapping_complete']
 for i in range(3):assert all(r['mapping']['indicator']=='urn:indicator:'+str(i) for r in rows if f'/classes/{i}/' in r['locator'])

@pytest.mark.parametrize('mutation',['rename','reorder','unit','group'])
def test_context_drift_invalidates_section_mapping(mutation):
 s,cw,tax,rules=setup();o=s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'][0]
 if mutation=='rename':o['classes'][1]['title']='Other measure'
 if mutation=='reorder':o['classes'].reverse()
 if mutation=='unit':o['unitOfMeasure']='cm'
 if mutation=='group':o['groups'][0]['title']='New group'
 rows,_=mapped(s,cw,tax,rules);assert not any(r['mapping'] for r in rows)

def test_numeric_updates_flow_through_and_keys_ignore_values():
 s,cw,tax,rules=setup();o=s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'][0];key=indicator_key('NCT03070600',o,1)
 o['classes'][1]['categories'][0]['measurements'][0]['value']='-0.25';o['classes'][1]['denoms'][0]['counts'][0]['value']='1700'
 assert indicator_key('NCT03070600',o,1)==key
 rows,_=mapped(s,cw,tax,rules);assert next(r for r in rows if r['mapping'])['value']=='-0.25'

def test_analysis_never_inherits_last_class_and_parent_overlap_fails_closed():
 s,cw,tax,rules=setup();o=s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'][0]
 o['analyses']=[{'groupIds':['OG000','OG001'],'paramType':'Odds Ratio','paramValue':'0.5'}]
 rules['outcomes'][0]['context_sha256']=cr.digest(cr.semantic_context(s,o))
 rows,_=mapped(s,cw,tax,rules);assert rows[-1]['mapping'] is None and rows[-1]['kind']=='source_comparison'
 cw.add((URIRef('urn:parent'),cr.UE.rawText,Literal(indicator_key('NCT03070600',o))))
 rows,cov=mapped(s,cw,tax,rules);assert not any(r['mapping'] for r in rows)
 assert 'Overlapping' in cov[0]['sections'][1]['reason']

def test_selector_roundtrip_and_parent_identity(tmp_path):
 s,cw,tax,rules=setup();o=s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'][0]
 key=rules['outcomes'][0]['key'];assert parent_indicator_key(key)==indicator_key('NCT03070600',o)
 assert outcome_context('NCT03070600',o,1)['class_selector']['title']=='Infant HAZ at 9mo PP'
 p=tmp_path/'rules.ttl';save_rules(rules,p);assert load_rules(p)==rules
 for invalid in (True,-1,3,'1'):
  with pytest.raises(ValueError):indicator_key('NCT03070600',o,invalid)

def test_class_comparison_configuration_is_explicitly_unsupported():
 s,cw,tax,rules=setup();rules['outcomes'][0]['derived']=[{'formula':'left - right'}]
 rows,cov=mapped(s,cw,tax,rules)
 assert not any(r['mapping'] for r in rows)
 assert 'arm measurements only' in cov[0]['sections'][1]['reason']
