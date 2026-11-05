import asyncio
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
sys.path.insert(0, str(ROOT))
from live_result_rules import interpret, components, PREFIX
from live_result_identity import identities
from ctgov_indicator_context import indicator_key
from live_results import assemble, rows_to_rdf

I = PREFIX+'vocab/interventions/'
S = PREFIX+'vocab/states/'

def source():
    return json.loads((ROOT/'tests/fixtures/task265/writing-study.json').read_text())

def outcomes(s):
    return s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures']

def synthetic():
    s = source()
    s['protocolSection']['identificationModule'] = {'nctId':'NCT12345678','briefTitle':'Synthetic explicit net interaction — test evidence only'}
    o = copy.deepcopy(outcomes(s)[0]);o['title'] = 'Synthetic reviewed score'
    o['description'] = 'A reviewed 20-item score; observed assessments, not computed changes.'
    o['classes'] = [o['classes'][0], o['classes'][2]]
    o['analyses'][0].update(groupIds=['OG001','OG000'], groupDescription='Expressive Writing (EW) minus Control',
                           statisticalComment='Treatment-by-time interaction: change from Baseline to 3 Months.')
    ids={'OG000':'TEST_CONTROL_41','OG001':'TEST_WRITING_17','OG002':'TEST_AFFIRMATION_9'}
    def rename(x):
        if isinstance(x,dict):
            for key,value in x.items():
                if key in ('id','groupId') and isinstance(value,str):x[key]=ids.get(value,value)
                elif key=='groupIds':x[key]=[ids.get(v,v) for v in value]
                else:rename(value)
        elif isinstance(x,list):
            for value in x:rename(value)
    rename(o)
    s['resultsSection']['outcomeMeasuresModule']['outcomeMeasures'] = [o]
    return s


def mapped_synthetic(monkeypatch):
    import live_result_identity as identity
    mapping, definitions = identities()
    mapping = copy.deepcopy(mapping)
    s = synthetic();mapping[indicator_key('NCT12345678', outcomes(s)[0])] = {__import__('rdflib').URIRef(S+'CESD20TotalScore')}
    monkeypatch.setattr(identity, 'identities', lambda: (mapping, definitions))
    import live_result_rules
    from ctgov_intervention_identity import contextual_key
    intervention_map = copy.deepcopy(identity.reviewed_interventions())
    intervention_map[contextual_key('NCT12345678','Expressive Writing (EW) Intervention')] = [(I+'ExpressiveWritingIntervention','Expressive writing')]
    intervention_map[contextual_key('NCT12345678','Control')] = [(I+'NeutralWritingComparatorIntervention','Neutral writing')]
    monkeypatch.setattr(live_result_rules, 'reviewed_interventions', lambda: intervention_map)
    return s


def test_all_source_cells_and_no_fabricated_effects():
    s = source();r = interpret(s)
    expected = {f'/resultsSection/outcomeMeasuresModule/outcomeMeasures/{oi}/classes/{ci}/categories/{ki}/measurements/{mi}': m
                for oi,o in enumerate(outcomes(s)) for ci,c in enumerate(o.get('classes',[]))
                for ki,k in enumerate(c.get('categories',[])) for mi,m in enumerate(k.get('measurements',[]))}
    descriptive = [row for row in r['rows'] if row['comparison']['kind']=='arm_level']
    actual = {d['locator']:row for row in descriptive for d in row['source_details']}
    assert actual.keys() == expected.keys()
    assert len(actual)==258
    assert all(row['value']==expected[loc]['value'] for loc,row in actual.items())
    assert all(row['result_interventions'] and not row.get('summary_mapping') for row in descriptive)
    assert all(row['direction']=='unknown/unsupported' for row in r['rows'])
    comparisons=[row for row in r['rows'] if row['comparison']['kind']=='source_comparison']
    assert len(comparisons)==6
    assert all(row['comparison']['estimand']=='interaction' and row['comparison']['semantics']=='unsupported' for row in comparisons)
    assert all('subtraction' in row['comparison']['reason'] and 'assessment' in row['comparison']['reason'] for row in comparisons)
    assert all(not row.get('summary_mapping') for row in r['rows'])
    assert [c['status'] for c in r['coverage']].count('not_collected')==1
    assert len({row['indicator'] for row in descriptive})==29
    cortisol=[row for row in descriptive if row['indicator']==S+'CortisolLevel']
    assert all(row['states']==[S+'CortisolPhysiologicalStatus'] for row in cortisol)
    assert all(row['comparison']['context']['class_denoms'] for row in descriptive)


@pytest.mark.parametrize('field,value', [('description','A different scale version or recall window'),('unitOfMeasure','events per year'),('paramType','MEDIAN'),('timeFrame','12 months'),('populationDescription','Different population'),('title','Different instrument')])
def test_semantic_changes_invalidate_identity(field,value):
    s=source();outcomes(s)[0][field]=value
    r=interpret(s)
    assert not any(row['indicator']==S+'CESD20TotalScore' for row in r['rows'])
    assert len([c for c in r['coverage'] if '/0/classes/' in c['locator']])==9


def test_numeric_change_keeps_identity_and_reads_new_values():
    s=source();outcomes(s)[0]['classes'][0]['categories'][0]['measurements'][0]['value']='17.25'
    assert interpret(s)['rows'][0]['value']=='17.25'


def test_participant_counts_and_event_means_do_not_become_rates():
    rows=interpret(source())['rows']
    counts=[r for r in rows if r['indicator']==S+'SelfReportedSuicideAttemptParticipantCount']
    events=[r for r in rows if r['indicator']==S+'SelfReportedNonsuicidalSelfInjuryFrequency']
    assert all(r['unit']=='participants' and r['comparison']['context']['statistic']=='COUNT_OF_PARTICIPANTS' for r in counts)
    assert all(r['unit']=='self-harm behaviors' and r['comparison']['context']['statistic']=='MEAN' for r in events)
    assert any('week' in note and 'month' in note for note in events[0]['scope_notes'])
    s=source();outcomes(s)[7]['classes'][0]['categories'][0]['measurements'][0]['value']='1.5'
    invalid=[r for r in interpret(s)['rows'] if r['indicator']==S+'SelfReportedSuicideAttemptParticipantCount'][0]
    assert invalid['value'] is None


def test_scoped_mapping_precedes_generic_and_does_not_leak():
    from ctgov_intervention_identity import contextual_key
    s=source();group=outcomes(s)[0]['groups'][0]
    mapping={'control':[('urn:wrong','Wrong')],contextual_key('NCT03751020','Control'):[(I+'NeutralWritingComparatorIntervention','Neutral writing')]}
    assert components(s,group,mapping)==[I+'NeutralWritingComparatorIntervention']
    s['protocolSection']['identificationModule']['nctId']='NCT00000001'
    assert components(s,group,mapping)==['urn:wrong']
    mapping[contextual_key('NCT00000001','Control')]=[]
    assert components(s,group,mapping) is None


def test_missing_attribution_retains_all_descriptive_source_arms():
    s=source();s['protocolSection']['armsInterventionsModule']['interventions'][0]['name']='Unreviewed intervention'
    rows=interpret(s)['rows'];ew=[r for r in rows if r['comparison']['context'].get('group',{}).get('title')=='Expressive Writing (EW)']
    assert ew and all(r['value'] is not None and r['comparison']['context']['mapping_status']=='unresolved' for r in ew)


def test_positive_interaction_and_reversed_source_order(monkeypatch):
    s=mapped_synthetic(monkeypatch)
    a=interpret(s)['rows'][-1]
    assert a['comparison']['displayable'] and a['comparison']['semantics']=='explicit'
    assert a['comparison']['ordered_groups']==['Expressive Writing (EW)','Control']
    assert not a.get('summary_mapping') and a['direction']=='unknown/unsupported'
    outcomes(s)[0]['analyses'][0]['groupIds'].reverse()
    b=interpret(s)['rows'][-1]
    assert b['comparison']['ordered_groups']==a['comparison']['ordered_groups']
    assert b['value']==a['value']
    assert b['comparison']['orientation']=='reverse'


@pytest.mark.parametrize('field,value',[('groupDescription','Expressive Writing (EW) versus Control'),('statisticalComment','Treatment-by-time interaction without defined time contrast'),('estimateComment','Log transformed coefficient')])
def test_ambiguous_interaction_remains_gap(monkeypatch,field,value):
    s=mapped_synthetic(monkeypatch);outcomes(s)[0]['analyses'][0][field]=value
    row=interpret(s)['rows'][-1]
    assert row['comparison']['semantics']=='unsupported' and not row['comparison'].get('displayable')
    assert not row.get('summary_mapping')


def test_distinct_study_count_and_state_relevance():
    r=interpret(source());payload={'results':[{'source':'ctgov','study_id':'NCT03751020','api_results':r}]*2,'meta':{}}
    out=assemble(payload,SimpleNamespace(values={}))
    assert len(out['reviewed_results']['rows'])==264 and out['meta']['reviewed_results']['studies_with_results']==1
    mental=assemble(payload,SimpleNamespace(values={'state':[S+'MentalHealth']}))
    assert all(x['indicator']!=S+'CortisolLevel' for x in mental['reviewed_results']['rows'])
    from rdflib import Graph
    assert len(Graph().parse(data=rows_to_rdf(out['reviewed_results']['rows']),format='turtle'))>264


@pytest.mark.parametrize('use_synthetic',[False,True])
def test_maintained_source_adapter_to_results_endpoint(monkeypatch,use_synthetic):
    import httpx
    import query_v2 as engine
    from api.routes.results import results
    from api.query_v2_cache import query_v2_cache
    s=mapped_synthetic(monkeypatch) if use_synthetic else source()
    calls=[]
    def response(request):
        calls.append(str(request.url));return httpx.Response(200,json={'studies':[s]},request=request)
    monkeypatch.setattr(engine.v1,'_load_ctgov_intervention_concept_map',lambda:{})
    monkeypatch.setattr(engine.v1,'_load_ctgov_outcome_concept_map',lambda:{})
    monkeypatch.setattr(engine.v1,'lookup_intervention_concepts_by_label',lambda _: {})
    adapter=engine.CtgovAdapter(lambda:httpx.AsyncClient(transport=httpx.MockTransport(response)))
    async def search(query,execute):
        branch=await adapter.execute({'region':engine.WORLD_URI},engine.SourceBudget(),engine.default_region_index())
        assert branch.rows
        return {'results':branch.rows,'meta':{}}
    monkeypatch.setattr(query_v2_cache,'get_or_execute',search)
    result=asyncio.run(results(state=S+'PopulationHealthStatus',intervention=I+'ExpressiveWritingIntervention',context='{}',format='json'))
    payload=json.loads(result.body)
    assert len(calls)==1 and 'ResultsSection' in calls[0]
    assert payload['rows']
    if use_synthetic:assert any(r['comparison'].get('displayable') for r in payload['rows'])
    else:
        expected=assemble({'results':[{'source':'ctgov','study_id':'NCT03751020','api_results':interpret(s)}],'meta':{}},SimpleNamespace(values={'state':[S+'PopulationHealthStatus']}))
        assert len(payload['rows'])==len(expected['reviewed_results']['rows'])
        assert {r['indicator'] for r in payload['rows']}=={r['indicator'] for r in expected['reviewed_results']['rows']}


@pytest.mark.parametrize('old,new',[('0-60','0-100'),('20 items','10 items'),('past week','past year')])
def test_specific_scale_range_version_and_recall_changes(old,new):
    s=source();o=outcomes(s)[0]
    assert old in o['description']
    o['description']=o['description'].replace(old,new)
    assert not any(r['indicator']==S+'CESD20TotalScore' for r in interpret(s)['rows'])


def test_scoped_measurements_cannot_lend_identity_to_outcome_analysis(monkeypatch):
    import live_result_identity as identity
    mappings,definitions=identities();mappings=copy.deepcopy(mappings)
    s=source();o=outcomes(s)[0]
    target=mappings.pop(indicator_key('NCT03751020',o))
    mappings[indicator_key('NCT03751020',o,0)]=target
    monkeypatch.setattr(identity,'identities',lambda:(mappings,definitions))
    r=interpret(s);rows=[row for row in r['rows'] if row['indicator']==S+'CESD20TotalScore']
    assert len(rows)==3 and all(row['comparison']['context']['class']=='Baseline' for row in rows)
    assert any(c['status']=='unsupported_analysis' and c['locator'].endswith('/0/analyses/0') for c in r['coverage'])
    assert len([c for c in r['coverage'] if '/0/classes/' in c['locator']])==6


def test_conflicting_runtime_and_bundled_contextual_mapping_fails_closed():
    from ctgov_intervention_identity import contextual_key
    key=contextual_key('NCT03751020','Control')
    rows=interpret(source(),{key:('urn:conflicting-identity','Conflicting identity')})['rows']
    controls=[r for r in rows if r['comparison']['context'].get('group',{}).get('title')=='Control']
    assert controls and all(r['comparison']['context']['mapping_status']=='unresolved' for r in controls)


def mixed_scope_source(monkeypatch):
    import live_result_identity as identity
    mappings,definitions=identities();mappings=copy.deepcopy(mappings)
    s=source();o=outcomes(s)[0]
    target=mappings.pop(indicator_key('NCT03751020',o))
    mappings[indicator_key('NCT03751020',o,0)]=target
    mappings[indicator_key('NCT03751020',o,2,0)]=target
    monkeypatch.setattr(identity,'identities',lambda:(mappings,definitions))
    return s,o,mappings,target


def test_mixed_class_and_category_scopes_preserve_six_cells_and_unmapped_gap(monkeypatch):
    s,o,_,_=mixed_scope_source(monkeypatch)
    r=interpret(s);rows=[row for row in r['rows'] if row['indicator']==S+'CESD20TotalScore']
    expected={f'/resultsSection/outcomeMeasuresModule/outcomeMeasures/0/classes/{ci}/categories/0/measurements/{mi}':m['value']
              for ci in (0,2) for mi,m in enumerate(o['classes'][ci]['categories'][0]['measurements'])}
    assert {d['locator']:row['value'] for row in rows for d in row['source_details']}==expected
    gaps=[c for c in r['coverage'] if '/0/classes/' in c['locator']]
    assert len(gaps)==3 and all('/classes/1/' in c['locator'] for c in gaps)
    assert any(c['status']=='unsupported_analysis' and c['locator'].endswith('/0/analyses/0') for c in r['coverage'])
    assert all(not row.get('summary_mapping') for row in rows)


@pytest.mark.parametrize('different_target',[False,True])
def test_overlapping_class_category_identities_fail_only_overlapping_branch(monkeypatch,different_target):
    from rdflib import URIRef
    s,o,mappings,target=mixed_scope_source(monkeypatch)
    o['classes'][0]['categories'].append(copy.deepcopy(o['classes'][0]['categories'][0]))
    mappings[indicator_key('NCT03751020',o,0,0)]={URIRef(S+'BSI18ItemMeanScore')} if different_target else target
    r=interpret(s);rows=[row for row in r['rows'] if row['indicator']==S+'CESD20TotalScore']
    paths={d['locator'] for row in rows for d in row['source_details']}
    assert len(paths)==6
    assert all('/classes/0/categories/1/' in p or '/classes/2/categories/0/' in p for p in paths)
    assert len([c for c in r['coverage'] if '/0/classes/0/categories/0/' in c['locator']])==3


def test_parent_and_scoped_identity_overlap_remains_unsupported(monkeypatch):
    s,o,mappings,target=mixed_scope_source(monkeypatch)
    mappings[indicator_key('NCT03751020',o)]=target
    r=interpret(s)
    assert not any(row['indicator']==S+'CESD20TotalScore' for row in r['rows'])
