"""Backend/consumer contract: values remain source-backed; labels are vocabulary-owned."""
import sys
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT),str(ROOT/'tests')]
from test_task265_results import source
from live_result_rules import interpret
from live_result_identity import preferred_labels
from live_results import assemble
from rdflib import Graph, URIRef
from rdflib.namespace import SKOS


def test_preferred_component_labels_come_from_maintained_vocabulary():
    rows=interpret(source())['rows']
    graph=Graph().parse(ROOT/'vocabularies/interventions.ttl')
    for row in rows:
        for uri,label in row['preferred_labels'].items():
            assert label in {str(v) for v in graph.objects(URIRef(uri),SKOS.prefLabel)}
    control='https://universalevidence.com/vocab/interventions/NeutralWritingComparatorIntervention'
    assert 'comparator' not in preferred_labels([control])[control].lower()
    assert preferred_labels(['urn:unknown'])=={}


def test_transient_projection_preserves_pairing_values_and_provenance():
    result=interpret(source())
    payload={'results':[{'source':'CT.gov','study_id':'NCT03751020','api_results':result}], 'meta':{}}
    rows=assemble(payload,SimpleNamespace(values={}))['reviewed_results']['rows']
    depression=[r for r in rows if r['indicator'].endswith('/CESD20TotalScore')]
    assert len(depression)==10
    for row in depression:
        assert row['value']==str(row['source_details'][0]['raw']['value']) if row['comparison']['kind']=='arm_level' else True
        if row['comparison']['kind']=='arm_level':
            assert row['comparison']['context']['population']
            assert row['comparison']['context']['class_denoms']
            assert not row.get('summary_mapping')
    assert len([r for r in depression if r['comparison'].get('semantics')=='unsupported'])==1
