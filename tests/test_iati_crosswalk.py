import asyncio
import json
from pathlib import Path
import pytest
from rdflib import Graph, URIRef, Literal
from rdflib.namespace import SKOS, RDF, DCTERMS
from scripts.iati_crosswalk import SearchCrosswalk, ROOT, IQ, UE, SOURCE
from scripts.iati_search import ProjectSearch
from test_iati_live import doc,raw,page,S,I

@pytest.fixture
def workspace(tmp_path):
 (tmp_path/'vocabularies/crosswalks').mkdir(parents=True)
 for rel in ['vocabularies/sources.ttl','vocabularies/crosswalks/iati-narrative-crosswalk.ttl']:
  (tmp_path/rel).write_bytes((ROOT/rel).read_bytes())
 # Minimal typed axis fixtures avoid repeatedly parsing unrelated taxonomy in unit tests.
 for name,uri,kind in [('states',S,UE.State),('interventions',I,UE.Intervention)]:
  g=Graph();g.add((URIRef(uri),RDF.type,kind));g.add((URIRef(uri),RDF.type,SKOS.Concept))
  if name=='states':g.add((URIRef(S),SKOS.exactMatch,URIRef('http://aims.fao.org/aos/agrovoc/c_98822596')))
  g.serialize(tmp_path/f'vocabularies/{name}.ttl',format='turtle')
 return tmp_path

def load(root):return SearchCrosswalk(root=root)
def registry(root,change):
 p=root/'vocabularies/sources.ttl';g=Graph().parse(p);change(g);g.serialize(p,format='turtle')

def test_forward_and_reverse_are_one_mapping(workspace):
 b=load(workspace)
 assert b.forward['oral nutritional supplementation']=={I}
 q=b.query(b.get('intervention',I))
 assert '"oral nutritional supplementation"' in q
 assert '"nutritional supplementation"' not in q
 assert 'tag_code:"98822596"' in b.query(b.get('state',S))

@pytest.mark.parametrize('text',[ 'nutritional supplements for stunting','lipid-based nutrient supplements for stunting','nutrition for stunting','information on supplements for stunting'])
def test_removed_broad_hints_do_not_assert_oral_equivalence(workspace,text):
 b=load(workspace)
 assert not b.support(raw(doc(text=text)),b.get('intervention',I))

@pytest.mark.parametrize('text',['Advice about oral nutritional supplementation for stunting.','We do not deliver oral nutritional supplements for stunting.'])
def test_mentions_still_are_not_delivery(workspace,text):
 b=load(workspace);support=b.support(raw(doc(text=text)),b.get('intervention',I))
 assert support and all(s['kind']=='mention' for s in support)

@pytest.mark.parametrize('entries,expected',[
 ([{'@code':'98822596','@vocabulary':'1'}],True),
 ([{'@code':'98822596','@vocabulary':'99'}],False),
 ([{'@code':'98822596'},{'@code':'other','@vocabulary':'1'}],False),
 ([{'@code':'2.2','@vocabulary':'3'}],False),
 ([{'@code':'12240','@vocabulary':'1'}],False),
 ([{'@code':'98822596','@vocabulary':'3'},{'@code':'2.2','@vocabulary':'1'}],False),
])
def test_same_nested_vocabulary_entry(workspace,entries,expected):
 b=load(workspace);a=raw(doc(text='No condition term here'));a['tag']=entries
 assert bool(b.support(a,b.get('state',S)))==expected

@pytest.mark.parametrize('mutate',[
 lambda a:a.update({'reporting-org':[{'@ref':'wrong'}]}),
 lambda a:a['tag'][0].update({'@vocabulary-uri':'https://wrong.example/codes'}),
 lambda a:a['tag'][0].update({'@vocabulary-version':'old'}),
 lambda a:a['tag'][0].pop('@vocabulary-uri'),
])
def test_publisher_uri_version_scope(workspace,mutate):
 def enable(g):
  g.set((IQ.agrovocTag,IQ.vocabularyCode,Literal('99')))
  for p,v in [(IQ.publisherRef,'GB-PUB-1'),(IQ.vocabularyURI,'https://example.org/reviewed-codes'),(IQ.vocabularyVersion,'2026')]:g.set((IQ.agrovocTag,p,Literal(v)))
 registry(workspace,enable);b=load(workspace)
 a=raw(doc(text='no state term'));a['tag']=[{'@code':'98822596','@vocabulary':'99','@vocabulary-uri':'https://example.org/reviewed-codes','@vocabulary-version':'2026'}]
 assert b.support(a,b.get('state',S));mutate(a)
 assert not b.support(a,b.get('state',S))

@pytest.mark.parametrize('change',[
 lambda g:g.set((IQ.title,IQ.queryField,Literal('unimplemented_field'))),
 lambda g:g.set((IQ.agrovocTag,IQ.queryField,Literal('title_narrative'))),
 lambda g:g.set((IQ.agrovocTag,IQ.reviewStatus,Literal('candidate'))),
 lambda g:g.set((IQ.agrovocTag,IQ.vocabularyCode,Literal('99'))),
 lambda g:g.set((IQ.agrovocTag,IQ.conceptPrefix,Literal('https://wrong.example/'))),
 lambda g:g.set((IQ.unicefSector,IQ.enabled,Literal(True))),
])
def test_declarations_cannot_enable_unsupported_or_unreviewed_codes(workspace,change):
 registry(workspace,change)
 with pytest.raises(ValueError):load(workspace)

def test_cache_versions_cover_thesaurus_and_registry(workspace):
 a=load(workspace).version
 p=workspace/'vocabularies/states.ttl';p.write_text(p.read_text()+'\n# reviewed taxonomy version\n')
 b=load(workspace).version;assert a!=b
 registry(workspace,lambda g:g.set((IQ.agrovocTag,IQ.enabled,Literal(False))))
 assert load(workspace).version!=b

def test_identity_collision_and_noncanonical_target(workspace):
 p=workspace/'vocabularies/crosswalks/iati-narrative-crosswalk.ttl';s=p.read_text()
 p.write_text(s.replace('"child stunting"','"stunting"'))
 with pytest.raises(ValueError,match='Duplicate'):load(workspace)
 p.write_text(s.replace('/OralNutritionalSupplementation','/Nonexistent'))
 with pytest.raises(ValueError,match='canonical'):load(workspace)

def test_disabled_reviewed_code_distinct_from_missing(workspace):
 p=workspace/'vocabularies/crosswalks/iati-narrative-crosswalk.ttl';p.write_text(p.read_text().replace('"approved"','"candidate"'))
 registry(workspace,lambda g:g.set((IQ.agrovocTag,IQ.enabled,Literal(False))))
 calls=[]
 async def fetch(q):calls.append(q);return page([])
 r=asyncio.run(ProjectSearch(load(workspace),fetcher=fetch).lookup(S,''))
 assert r['status']=='unsupported' and not calls

@pytest.mark.parametrize('entries',[None,{},[None,{'narrative':None}]])
def test_malformed_fields_do_not_create_matches(workspace,entries):
 b=load(workspace);a=raw(doc(text='nothing'));a['title']=entries;a['description']=[]
 assert not b.support(a,b.get('intervention',I))


def test_unreviewed_thesaurus_link_cannot_inherit_another_concepts_review(workspace):
 p=workspace/'vocabularies/states.ttl';g=Graph().parse(p);other=URIRef(S+'Other')
 g.add((other,RDF.type,UE.State));g.add((other,SKOS.exactMatch,URIRef('http://aims.fao.org/aos/agrovoc/c_98822596')));g.serialize(p,format='turtle')
 assert load(workspace).get('state',str(other)) is None


def test_full_response_cap_includes_large_project_metadata(workspace,monkeypatch):
 from scripts import iati_search
 monkeypatch.setattr(iati_search,'MAX_RESPONSE_BYTES',14000)
 d=doc(text='oral nutritional supplements for stunting');raw(d)['title']=[{'narrative':[{'text()':'x'*20000}]}]
 async def fetch(q):return page([d])
 result=asyncio.run(ProjectSearch(load(workspace),fetcher=fetch).lookup(S,I))
 assert len(json.dumps(result).encode())<=14000
 assert result['coverage']['truncated'] and 'response-size-limit' in result['errors']
 assert result['returned_project_count']==len(result['projects'])


def test_real_source_transport_enforces_four_second_wall_timeout(workspace,monkeypatch):
 import time,sqlite3
 from contextlib import asynccontextmanager
 from scripts import iati_live as live
 monkeypatch.setenv('IATI_API_KEY','synthetic-test-key')
 class Stream:
  status_code=200;headers={}
  async def aiter_bytes(self):
   await asyncio.sleep(30)
   yield b'{}'
 class Client:
  async def __aenter__(self):return self
  async def __aexit__(self,*args):pass
  @asynccontextmanager
  async def stream(self,*args,**kwargs):yield Stream()
 def client(**kwargs):
  assert kwargs['timeout']==4
  return Client()
 monkeypatch.setattr(live.httpx,'AsyncClient',client)
 quota=live.Quota(workspace/'quota.sqlite3',interval=0)
 start=time.monotonic()
 with pytest.raises(live.SourceError,match='source-timeout'):
  asyncio.run(live.fetch_live('query',quota))
 assert 3.8<=time.monotonic()-start<4.5
 with sqlite3.connect(quota.path) as db:assert db.execute('select sum(lease) from calls').fetchone()[0]==0
