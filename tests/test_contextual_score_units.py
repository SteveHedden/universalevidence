import copy
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import live_result_identity as identity
from live_result_rules import interpret, arm_kind
from ctgov_indicator_context import indicator_key
from test_live_result_rules import study, outcome

TARGET='urn:reviewed-score'
def mapped(monkeypatch,unit='Z-score',statistic='MEAN',scoped=False):
 s=study(title='Reviewed length measure',unit=unit);o=outcome(s)
 o.update(description='Specified reference: Fenton gestational-age growth reference.',paramType=statistic)
 o['analyses']=[]
 if scoped:
  o['classes']=[{'title':'WAZ','categories':[{'measurements':[{'groupId':'G1','value':'9'}]}]},
                {'title':'HAZ','denoms':[{'units':'Participants','counts':[{'groupId':'G1','value':'1777'}]}],'categories':[{'measurements':[{'groupId':'G1','value':'-.35','lowerLimit':'-1.25','upperLimit':'.50'}]}]}]
  o['dispersionType']='Inter-Quartile Range'
 key=indicator_key('NCT99999999',o,1 if scoped else None)
 mappings={key:{TARGET}}
 definitions={TARGET:{'indicator':TARGET,'label':'Reviewed score','states':['urn:growth'],'definition':'Reference-generic score, not prevalence','scope_notes':[]}}
 monkeypatch.setattr(identity,'identities',lambda:(mappings,definitions))
 return s,mappings

@pytest.mark.parametrize('unit',['Z-score','z-score','z score','score on a scale'])
def test_exact_identity_allows_source_unit_without_conversion(monkeypatch,unit):
 s,_=mapped(monkeypatch,unit);rows=interpret(s)['rows']
 assert len(rows)==2
 assert {r['unit'] for r in rows}=={unit}
 assert rows[0]['value']=='0.3'
 assert rows[0]['source_details'][0]['outcome']['description']==outcome(s)['description']
 assert all('summary_mapping' not in r for r in rows)

@pytest.mark.parametrize('change',['no_mapping','conflict','reference_changed','unit_changed','invalid_statistic'])
def test_unsupported_or_stale_identity_fails_closed(monkeypatch,change):
 s,mappings=mapped(monkeypatch)
 if change=='no_mapping':mappings.clear()
 elif change=='conflict':next(iter(mappings.values())).add('urn:other-score')
 elif change=='reference_changed':outcome(s)['description']='Different WHO age reference'
 elif change=='unit_changed':outcome(s)['unitOfMeasure']='centimeters'
 else:outcome(s)['paramType']='GEOMETRIC_MEAN'
 assert not interpret(s)['rows']

def test_scoped_median_retains_iqr_denominator_and_excludes_other_classes(monkeypatch):
 s,_=mapped(monkeypatch,statistic='MEDIAN',scoped=True);rows=interpret(s)['rows']
 assert len(rows)==1
 r=rows[0];assert r['value']=='-.35'
 assert r['comparison']['context']['class']=='HAZ'
 assert r['comparison']['context']['class_denoms']==outcome(s)['classes'][1]['denoms']
 assert r['comparison']['context']['dispersionType']=='Inter-Quartile Range'
 assert r['source_details'][0]['raw']['lowerLimit']=='-1.25'
 assert 'summary_mapping' not in r

def test_explicit_change_remains_change_but_assessment_levels_do_not(monkeypatch):
 s,mappings=mapped(monkeypatch,unit='score on a scale');o=outcome(s)
 o['title']='Change in Somatic Growth (Length)';o['description']='Evaluated by the change in Z-score (standard score).'
 mappings.clear();mappings[indicator_key('NCT99999999',o)]={TARGET}
 rows=interpret(s)['rows'];assert all(r['comparison']['kind']=='within_arm_change' for r in rows)
 rule=identity.contextual_measure('NCT99999999',o)
 assert arm_kind(rule,{'title':'Baseline'})=='arm_level'
 assert arm_kind(rule,{'title':'3 months'})=='arm_level'
 assert arm_kind(rule,{'title':'Ambiguous subgroup'})=='reported_arm_measurement'
 o['classes'][0]['categories'][0]['title']='Baseline'
 mappings.clear();mappings[indicator_key('NCT99999999',o)]={TARGET}
 assert not identity.contextual_measure('NCT99999999',o)['change']
