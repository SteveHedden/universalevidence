"""File separation must preserve the existing RDF and HTTP identity contract."""
from pathlib import Path
from rdflib import Graph, Namespace, RDF, URIRef
from scripts import load_fuseki, query_v2
from api.routes import vocab
ROOT=Path(__file__).resolve().parents[1]
U=Namespace('https://universalevidence.com/ontology/')
S=Namespace('https://universalevidence.com/vocab/states/')

def test_indicators_are_separate_with_resolvable_state_links():
    states=Graph().parse(ROOT/'vocabularies/states.ttl')
    indicators=Graph().parse(ROOT/'vocabularies/indicators.ttl')
    assert not set(states.subjects(RDF.type,U.Indicator))
    assert len(set(indicators.subjects(RDF.type,U.Indicator))) >= 819
    assert not set(indicators.subjects(RDF.type,U.State))
    for indicator,state in indicators.subject_objects(U.measures):
        assert (state,RDF.type,U.State) in states
    assert (S.SDG221UnderFiveStuntingPrevalence,U.measures,S.Stunting) in indicators

def test_indicator_uri_resolves_through_existing_vocab_route():
    graph=vocab._load_graph(vocab.VOCABULARIES['states'])
    assert (S.SDG221UnderFiveStuntingPrevalence,RDF.type,U.Indicator) in graph
    result=query_v2._resolve_local_concept(str(S.SDG221UnderFiveStuntingPrevalence))
    assert result is not None

def test_loader_and_teacher_include_indicator_asset():
    assert ROOT/'vocabularies/indicators.ttl' in load_fuseki.DEFAULT_TAXONOMY_SOURCES
    sources=Graph().parse(ROOT/'vocabularies/sources.ttl')
    assert str(sources.value(U.IndicatorAxis,U.targetFile))=='vocabularies/indicators.ttl'
    assert str(sources.value(U.IndicatorAxis,U.targetNamespace))==str(S)


def test_enrichment_cache_tracks_indicator_file_changes(tmp_path, monkeypatch):
    from scripts import evidence_enrichment as enrichment
    monkeypatch.setattr(enrichment, 'VOCABULARY_ROOT', tmp_path)
    prefixes='@prefix ue: <https://universalevidence.com/ontology/> . @prefix s: <https://universalevidence.com/vocab/states/> . '
    (tmp_path/'states.ttl').write_text(prefixes+'s:Stunting a ue:State .')
    indicator_file=tmp_path/'indicators.ttl'
    indicator_file.write_text(prefixes+'s:First a ue:Indicator ; ue:measures s:Stunting .')
    first=enrichment.taxonomy('states.ttl')
    assert (S.First,U.measures,S.Stunting) in first
    indicator_file.write_text(prefixes+'s:Second a ue:Indicator ; ue:measures s:Stunting .')
    second=enrichment.taxonomy('states.ttl')
    assert (S.Second,U.measures,S.Stunting) in second
    assert (S.First,U.measures,S.Stunting) not in second
