import asyncio
from pathlib import Path
from rdflib import Graph
from test_search_results import assets, fixture_fetch, discovery, no_extra, STATE, INTERVENTION
import search_results as sr


def test_runtime_projection_uses_actual_ctgov_adapter(assets):
    mapping=Path(__file__).parent/'fixtures/ctgov_results/comparison-components.ttl'
    result=asyncio.run(sr.search_results(STATE,INTERVENTION,{'comparison_components':str(mapping)},
        search=discovery(['NCT00659061']),supplement=no_extra,fetch=fixture_fetch))
    assert len(result['rows'])==7
    difference=next(r for r in result['rows'] if r['comparison']['kind']=='derived_comparison')
    assert difference['result_model']['selected_intervention_role']=='present in both groups; effect not isolated'
    assert difference['value']=='1'
    assert len(difference['result_model']['groups'])==2
    assert 'comparison_projection_seconds' in result['timings']
    assert list(Graph().parse(data=result['rdf'],format='turtle'))
