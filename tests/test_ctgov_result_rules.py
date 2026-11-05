import json,sys
from pathlib import Path
import pytest
from rdflib import Graph,Literal,RDF
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from ctgov_result_rules import load_rules,save_rules,RULE
FIXTURE=Path(__file__).parent/'fixtures/ctgov_results/rules.json'
def test_exact_rule_behavior_preserved(tmp_path):
 original=json.loads(FIXTURE.read_text());p=tmp_path/'rules.ttl';save_rules(original,p)
 assert load_rules(p)==original
 assert not any(v.datatype==RDF.JSON for v in Graph().parse(p).objects() if isinstance(v,Literal))
def test_nullable_operands_and_analysis_rules(tmp_path):
 original={'version':'test','outcomes':[{'analysis_rules':{'0':{'orientation':'reverse'}},'derived':[{'operands':[{'category':None,'groupId':'OG000'},{'category':'Endline','groupId':'OG001'}]}]}]}
 p=tmp_path/'rules.ttl';save_rules(original,p);assert load_rules(p)==original
def test_duplicate_values_rejected(tmp_path):
 p=tmp_path/'rules.ttl';save_rules({'version':'test','outcomes':[]},p);g=Graph().parse(p)
 root=next(g.subjects(RDF.type,RULE.RuleSet));g.add((root,RULE['field/version'],Literal('other')));g.serialize(p,format='turtle')
 with pytest.raises(ValueError,match='Multiple values'):load_rules(p)
def test_cyclic_list_rejected(tmp_path):
 p=tmp_path/'rules.ttl';save_rules({'version':'test','outcomes':[{}]},p);g=Graph().parse(p)
 root=next(g.subjects(RDF.type,RULE.RuleSet));head=g.value(root,RULE['field/outcomes']);g.set((head,RDF.rest,head));g.serialize(p,format='turtle')
 with pytest.raises(ValueError,match='Cyclic'):load_rules(p)
