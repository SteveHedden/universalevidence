import asyncio
from copy import deepcopy
import pytest
from rdflib import Graph, URIRef
from rdflib.namespace import DCTERMS
from scripts.iati_search import SearchCrosswalk, ProjectSearch
from scripts.iati_live import SourceError
from test_iati_live import doc as legacy_doc, raw, page, S, I

def doc(*args, **kwargs):
 kwargs.setdefault("text", "We provide oral nutritional supplements to prevent stunting in children.")
 return legacy_doc(*args, **kwargs)

def execute(docs):
 calls=[]
 async def fetch(q):calls.append(q);return page(docs)
 return asyncio.run(ProjectSearch(fetcher=fetch).lookup(S,I)),calls

def test_query_registry_and_same_activity():
 r,c=execute([doc()]);assert len(c)==1 and ' AND ' in c[0]
 assert 'title_narrative:' in c[0] and 'description_narrative:' in c[0]
 assert 'sector_code' not in c[0] and r['mode']=='project-mentions'
 assert len(r['projects'])==1 and not r['organizations']
 assert r['projects'][0]['participants'][0]['role']=='implementing'
 g=Graph().parse(data=r['rdf'],format='turtle');assert len(g)>15
 assert not list(g.triples((None,DCTERMS.subject,None)))
 r,c=execute([doc('a',text='Stunting'),doc('b',text='Oral nutritional supplements')])
 assert not r['projects'] and len(c)==1

@pytest.mark.parametrize('text',['Advice about oral nutritional supplements for stunting.','We research oral nutritional supplements for stunting.','We do not provide oral nutritional supplements for stunting.'])
def test_mentions_are_not_delivery_claims(text):
 r,_=execute([doc(text=text)])
 assert r['projects'] and not r['organizations']
 assert all(s['kind']=='mention' for m in r['projects'][0]['matches'].values() for s in m['support'])
 assert 'explicit' not in r['projects'][0]['matches']['state']

def test_source_scoped_names_roles_and_withheld():
 r,_=execute([doc(ref=''),doc('b',role='1'),doc('c',name='IP not published')])
 cards={p['id']:p for p in r['projects']}
 assert cards['activity-a']['participants'][0]['ref'] is None
 assert cards['b']['participants'][0]['role']=='funding'
 assert not cards['c']['participants']
 assert not r['organizations']

def test_isolation_caps_order_and_ended():
 docs=[doc(str(n)) for n in range(7)]
 r,_=execute(docs+[{'iati_json':{} }]);assert r['status']=='partial' and len(r['projects'])==5
 assert r['coverage']['project_limit_reached']
 assert [p['id'] for p in r['projects']]==[p['id'] for p in execute(list(reversed(docs)))[0]['projects']]
 d=doc();raw(d)['activity-status']=[{'@code':'3'}]
 assert not execute([d])[0]['projects']

def test_errors_mapping_and_cache():
 async def scenario():
  calls=[]
  async def fetch(q):calls.append(q);return page([doc()])
  service=ProjectSearch(fetcher=fetch)
  a=await service.lookup(S,I);b=await service.lookup(S,I)
  assert b['cache']=='hit' and len(calls)==1 and not a['coverage']['truncated']
  assert (await service.lookup(S,I+'Unknown'))['status']=='missing-mappings'
  assert len(calls)==1
  async def fail(q):raise SourceError('source-http-error')
  r=await ProjectSearch(fetcher=fail).lookup(S,I)
  assert r['status']=='error' and not r['projects']
 asyncio.run(scenario())

def test_crosswalk_validation_and_candidate_exclusion(tmp_path):
 rules=SearchCrosswalk();assert len(rules.rules)==2
 from scripts.iati_search import ROOT
 original=(ROOT/'vocabularies/crosswalks/iati-narrative-crosswalk.ttl').read_text()
 p=tmp_path/'crosswalk.ttl';p.write_text(original.replace('"approved"','"candidate"'))
 # AGROVOC State mapping remains, but unreviewed source phrases cannot execute.
 book=SearchCrosswalk(crosswalk=p)
 assert not book.get('intervention',I)
 assert all(t['kind']=='code' for t in book.get('state',S)['terms'])
 p.write_text(original.replace('skos:exactMatch','skos:broadMatch'))
 with pytest.raises(ValueError):SearchCrosswalk(crosswalk=p)
