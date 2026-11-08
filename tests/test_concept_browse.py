from html.parser import HTMLParser
from urllib.parse import urlsplit, parse_qs
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from api.routes import vocab as v

class SnippetText(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag not in {'meta', 'link', 'img', 'br', 'input', 'hr'}:
            excluded = tag in {'script', 'style'} or (
                tag in {'div', 'span', 'section'} and 'data-nosnippet' in dict(attrs))
            self.stack.append((tag, excluded))

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_data(self, text):
        if not any(excluded for _, excluded in self.stack):
            self.text.append(text)

def test_concept_snippet_prefers_definition(client):
    html = client.get('/vocab/states/Root', headers={'accept': 'text/html'}).text
    eligible = ' '.join(SnippetText(html).text)
    assert 'Real definition' in eligible
    assert 'Narrower concepts' not in eligible
    assert 'Canonical URI' not in eligible
    assert 'Know what’s been done' not in eligible
    assert 'meta name="description" content="Real definition"' in html

@pytest.mark.parametrize('slug', ['malaria', 'stunting', 'bednets'])
def test_pilot_snippet_prefers_definition(slug):
    from pathlib import Path
    html = (Path(v.__file__).parents[1] / 'concept_pilots' / f'{slug}.html').read_text()
    eligible = ' '.join(SnippetText(html).text)
    assert 'Browse' not in eligible
    assert 'Narrower' not in eligible
    assert 'Selected evidence' not in eligible
    assert 'Search evidence' not in eligible
    import re
    definition = re.search(r'<p class="definition">(.*?)</p>', html).group(1)
    assert definition in eligible

class Links(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.links=[]; self.feed(text)
    def handle_starttag(self, tag, attrs):
        if tag=='a': self.links.append(dict(attrs).get('href',''))

@pytest.fixture
def client(tmp_path, monkeypatch):
    path=tmp_path/'states.ttl'
    path.write_text('''@prefix s: <https://universalevidence.com/vocab/states/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix ue: <https://universalevidence.com/ontology/> .
s:Root a skos:Concept, ue:State; skos:prefLabel "Root"; skos:definition "Real definition" .
s:Other a skos:Concept, ue:State; skos:prefLabel "Other" .
s:Child a skos:Concept, ue:State; skos:prefLabel "Child"; skos:broader s:Root, s:Other .
s:Orphan a skos:Concept, ue:State; skos:prefLabel "Orphan" .
''')
    config=v.ResourceConfig('states',path,'https://universalevidence.com/vocab/states/')
    monkeypatch.setitem(v.VOCABULARIES,'states',config)
    app=FastAPI();app.include_router(v.router)
    return TestClient(app)

def test_concept_content_relationships_and_search(client):
    r=client.get('/vocab/states/Root',headers={'accept':'text/html'})
    assert r.status_code==200
    assert 'Real definition' in r.text
    assert '/vocab/states/Child' in Links(r.text).links
    search=next(u for u in Links(r.text).links if u.startswith('/explore.html?'))
    assert parse_qs(urlsplit(search).query)=={'state':['https://universalevidence.com/vocab/states/Root'],'state_label':['Root']}
    assert 'technical-details' in r.text
    assert r.text.count('cloud.umami.is/script.js')==1
    assert 'href="https://universalevidence.com/vocab/states/Root"' in r.text

def test_multiple_parents_leaf_and_orphan(client):
    r=client.get('/vocab/states/Child',headers={'accept':'text/html'})
    assert 'Broader concepts' in r.text and 'Narrower concepts' not in r.text
    assert {'/vocab/states/Root','/vocab/states/Other'} <= set(Links(r.text).links)
    r=client.get('/vocab/states/Orphan',headers={'accept':'text/html'})
    assert 'No broader or narrower concepts are recorded.' in r.text

def test_directory_uses_actual_roots_and_all_terms(client):
    r=client.get('/browse/states')
    assert r.status_code==200
    links=Links(r.text).links
    assert '/vocab/states/Root' in links and '/vocab/states/Orphan' not in links
    assert '/vocab/states/Orphan' in Links(client.get('/browse/states?view=all').text).links
    assert '/vocab/states/Child' not in links
    assert '/vocab/states/Child' in Links(client.get('/browse/states?view=all').text).links
    assert client.get('/browse/states?page=999').status_code==404
    assert client.get('/browse/not-real').status_code==404
    assert client.get('/browse/states?view=not-real').status_code==404

def test_rdf_still_available(client):
    for mime in ['application/json','application/ld+json','text/turtle']:
        r=client.get('/vocab/states/Root',headers={'accept':mime})
        assert r.status_code==200 and r.headers['content-type'].startswith(mime)
