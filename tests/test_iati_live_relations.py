"""R1: lexical co-occurrence is not intervention provision; exercise real cards/RDF."""
import json
from copy import deepcopy
from pathlib import Path
import pytest
from rdflib import Graph, URIRef
from rdflib.namespace import DCTERMS
from test_iati_live import C, I, S, NOW, doc, raw, entry, execute, rules
from scripts.iati_live_rules import RuleBook, activity

PHRASES=[(I,'nutritional supplements'),(C,'unconditional cash transfers')]

@pytest.mark.parametrize('intervention,phrase',PHRASES)
@pytest.mark.parametrize('template',[
 'We provide information about {p} to prevent stunting.',
 'We distribute information about {p} to prevent stunting.',
 'We deliver advice about {p} to prevent stunting.',
 'We provide guidance about {p} to prevent stunting.',
 'We advocate for {p} to prevent stunting.',
 'We provide referrals for {p} to prevent stunting.',
 'We refer families to providers of {p} to prevent stunting.',
 'We discuss providing {p} to prevent stunting.',
 'We plan to provide {p} to prevent stunting.',
 'We procure {p} to prevent stunting.',
 'We prepare procurement plans to provide {p} to prevent stunting.',
 'We recommend that partners provide {p} to prevent stunting.',
 'We teach families to provide {p} to prevent stunting.',
 'We may provide {p} to prevent stunting.',
 'We will provide {p} to prevent stunting.',
 'We deliver training so that partners provide {p} to prevent stunting.',
 'We provide information; {p} are discussed as a way to prevent stunting.',
 'Our discussion explains how {p} are provided to prevent stunting.',
 'We research {p} to prevent stunting.',
 'Do we provide {p} to prevent stunting?',
 'We provide a briefing on {p} to prevent stunting.',
 'We provide support for {p} to prevent stunting.',
 'To provide {p} to prevent stunting.',
 'We offer resources to provide {p} to prevent stunting.',
 'We provide stunting services and mention {p}.',
])
def test_information_and_plans_never_become_organization_cards(intervention,phrase,template):
 text=template.format(p=phrase)
 result,calls=execute([doc(identity='counterexample-unseen',text=text)],intervention=intervention)
 assert not result['organizations'], text
 assert not result['unresolved_participants'], text
 assert result['status']=='insufficient-matches'
 assert len(calls)==2 # zero eligible primary may fallback, but cannot promote prose
 graph=Graph().parse(data=result['rdf'],format='turtle')
 assert not list(graph.triples((None,DCTERMS.subject,URIRef(intervention))))

@pytest.mark.parametrize('intervention,phrase',PHRASES)
@pytest.mark.parametrize('template',[
 'We provide {p} to prevent stunting.',
 'We distribute {p} to prevent stunting.',
 'We deliver {p} to prevent stunting.',
 'We supply {p} to prevent stunting.',
 'We provided {p} to prevent stunting.',
 'We provide monthly {p} to prevent stunting.',
 '{p} are provided to households to prevent stunting.',
 '{p} were distributed to households to prevent stunting.',
 'We provide {p} and nutrition education to prevent stunting.',
])
def test_actual_provision_still_produces_explicit_card_and_rdf(intervention,phrase,template):
 text=template.format(p=phrase)
 result,calls=execute([doc(identity='positive-unseen',text=text)],intervention=intervention)
 assert len(result['organizations'])==1, text
 assert result['organizations'][0]['relevance']=='explicit'
 assert len(calls)==1
 project=result['organizations'][0]['representative']
 support=project['matches']['intervention']['support']
 assert any(s['relation_contract']=='direct-provision-v1' and s['kind']=='objective' for s in support)
 graph=Graph().parse(data=result['rdf'],format='turtle')
 assert list(graph.triples((None,DCTERMS.subject,URIRef(intervention))))

@pytest.mark.parametrize('intervention,phrase',PHRASES)
def test_background_fallback_requires_actual_provision(intervention,phrase):
 for text,expected in [(f'We provide {phrase}. Stunting affects many children.',True),
                       (f'We provide information about {phrase}. Stunting affects many children.',False)]:
  result,_=execute([],intervention=intervention,second=[doc(text=text)])
  assert bool(result['organizations']) is expected
  if expected: assert result['organizations'][0]['relevance']=='candidate'


def test_relation_contract_is_required_not_silently_inferred(tmp_path):
 payload=deepcopy(rules().payload)
 payload['mappings'][1].pop('text_relation')
 path=tmp_path/'rules.json';path.write_text(json.dumps(payload))
 with pytest.raises(ValueError,match='relation contract'):RuleBook(path,candidate_review=True)


def test_candidates_are_not_approved_by_the_fix():
 fixture=Path(__file__).parent/'fixtures/iati-live-candidate.json'
 assert all(m['review']['status']=='candidate' for m in json.loads(fixture.read_text())['mappings'])
 assert not RuleBook(fixture).rules
