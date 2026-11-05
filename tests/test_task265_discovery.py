"""Crosswalk-backed retrieval must retain study scope and existing mappings."""
import asyncio
import copy
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import query_v2 as engine
from ctgov_intervention_identity import contextual_key
from live_result_identity import intervention_discovery_terms, scoped_intervention_match

I = 'https://universalevidence.com/vocab/interventions/'


def test_contextual_hints_are_not_global_aliases():
    mapping = {contextual_key('NCT12345678','A source-only name'): [(I+'Target','Target')]}
    assert intervention_discovery_terms(I+'Target',mapping)==['A source-only name']
    assert scoped_intervention_match('NCT12345678',['A source-only name'],I+'Target',mapping)==(True,[])
    assert scoped_intervention_match('NCT87654321',['A source-only name'],I+'Target',mapping)==(False,['A source-only name'])
    mapping[contextual_key('NCT12345678','A source-only name')].append((I+'Other','Other'))
    assert intervention_discovery_terms(I+'Target',mapping)==[]
    assert scoped_intervention_match('NCT12345678',['A source-only name','Unmapped'],I+'Target',mapping)==(False,['Unmapped'])


@pytest.mark.parametrize('selected',[True,False])
@pytest.mark.parametrize('with_state',[True,False])
def test_adapter_uses_crosswalk_without_mocking_intervention_map(monkeypatch,selected,with_state):
    study=json.loads((ROOT/'tests/fixtures/task265/writing-study.json').read_text())
    unrelated=copy.deepcopy(study)
    unrelated['protocolSection']['identificationModule']['nctId']='NCT87654321'
    calls=[]
    def response(req):
        calls.append(req)
        return httpx.Response(200,json={'studies':[study,unrelated]},request=req)
    async def resolve(uri):
        return engine.ConceptResolution(label='Expressive writing intervention',terms=('Expressive writing intervention',),mesh_uris=())
    monkeypatch.setattr(engine.v1,'_load_ctgov_intervention_concept_map',lambda:{'An existing intervention':(I+'Existing','Existing')})
    monkeypatch.setattr(engine.v1,'_load_ctgov_outcome_concept_map',lambda:{})
    monkeypatch.setattr(engine.v1,'lookup_intervention_concepts_by_label',lambda _: {})
    monkeypatch.setattr(engine,'_local_states_by_mesh',lambda _: {})
    monkeypatch.setattr(engine,'_local_states_by_mesh_multi',lambda _: {})
    monkeypatch.setattr(engine,'_source_direct_state_crosswalk',lambda *args: {})
    adapter=engine.CtgovAdapter(lambda:httpx.AsyncClient(transport=httpx.MockTransport(response)),resolve)
    spec={'intervention':I+'ExpressiveWritingIntervention'} if selected else {}
    if with_state: spec['state']='https://universalevidence.com/vocab/states/Stunting'
    branch=asyncio.run(adapter.execute(spec,engine.SourceBudget(),engine.default_region_index()))
    mapped=[r for r in branch.rows if r.get('intervention_concept_uri')==I+'ExpressiveWritingIntervention']
    assert mapped and {r['study_id'] for r in mapped}=={'NCT03751020'}
    assert all(r['intervention']=='Expressive Writing (EW) Intervention' for r in mapped)
    if selected:
        if with_state: assert ') AND (AREA[InterventionSearch]' in calls[0].url.params['query.term']
        assert 'AREA[NCTId]("NCT03751020")' in calls[0].url.params['query.term']
        assert 'Expressive Writing (EW) Intervention' not in calls[0].url.params['query.term']
        assert {r['study_id'] for r in branch.rows}=={'NCT03751020'}
    else:
        assert any(r['study_id']=='NCT87654321' and not r.get('intervention_concept_uri') for r in branch.rows)
    assert any(len(r['api_results']['rows'])==264 for r in mapped)
