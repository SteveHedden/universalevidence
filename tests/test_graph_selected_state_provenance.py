"""Selected-root search support, separate from independently mapped States."""
import asyncio
import base64
import hashlib
import hmac
import json

import pytest
from fastapi import HTTPException
from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.namespace import SKOS

from api.routes import graph_v2 as g
import query_v2 as q

A = 'https://universalevidence.com/vocab/states/TestRoot'
B = 'https://universalevidence.com/vocab/states/TestChild'
C = 'https://universalevidence.com/vocab/states/TestOther'
I = 'https://universalevidence.com/vocab/interventions/TestIntervention'
J = 'https://universalevidence.com/vocab/interventions/TestSecond'


def query(**values):
    logic = values.pop('logic', 'or')
    return q.canonical_query(values, {'state': logic})


def meta(count=0):
    return dict(status='included', coverage='full', returned_unique_studies=count,
                truncated=False, approximate=False, reason=None)


def row(study='S', source='CT.gov', mappings=(), intervention=I):
    return dict(source=source, study_id=study, intervention_concept_uri=intervention,
                direct_state_mappings=list(mappings), outcomes=[])


def matched(value, *roots):
    return {**value, 'query_matches': [dict(selected_root=root, retrieval_basis='search',
                                         retrieval_role=None) for root in roots]}


def forest(uris):
    return dict(nodes=[dict(id=uri, label=uri.rsplit('/', 1)[-1], selected=True,
                           studyCount=0) for uri in uris], pairs=[], omitted=0)


@pytest.fixture(autouse=True)
def taxonomy(monkeypatch):
    states, interventions = Graph(), Graph()
    for uri in (A, B, C):
        states.add((URIRef(uri), SKOS.prefLabel, Literal(uri.rsplit('/', 1)[-1])))
    states.add((URIRef(B), SKOS.broader, URIRef(A)))
    for uri in (I, J):
        interventions.add((URIRef(uri), SKOS.prefLabel, Literal(uri.rsplit('/', 1)[-1])))
    monkeypatch.setattr(g, '_state_graph', lambda: states)
    monkeypatch.setattr(g, '_intervention_graph', lambda: interventions)
    monkeypatch.setattr(g, '_state_ancestor_uris', lambda uri: {A, B} if uri == B else {uri})
    monkeypatch.setattr(q, '_local_taxonomy_graph', lambda name: states if name == 'states.ttl' else interventions)
    monkeypatch.setattr(g, 'state_forest', lambda roots: forest(roots or (A, B, C)))
    monkeypatch.setattr(g, 'intervention_forest', lambda roots: forest(roots or (I, J)))


def aggregate(request, live=None, stored=None):
    live, stored = live or {}, stored or {}
    return g.build_graph_v2_payload(
        query=request, condition_taxonomy=forest(request.values['state'] or (A, B, C)),
        intervention_taxonomy=forest((I, J)), local_rows=stored, live_rows=live,
        source_meta={source: meta(len({r['study_id'] for r in live.get(source, [])}))
                     for source in q.SOURCE_IDS},
    )


def edges(payload):
    return [edge for edge in payload['edges'] if edge['kind'] == 'evidence']


@pytest.mark.parametrize('source', ['ctgov', 'isrctn'])
@pytest.mark.parametrize('logic,expected', [('or', {'S', 'A-only', 'B-only'}), ('and', {'S'})])
def test_branch_membership_survives_union_intersection_and_blank_mapping(source, logic, expected):
    class Adapter:
        source_id = source

        async def execute(self, spec, budget, regions):
            unique = 'A-only' if spec['state'] == A else 'B-only'
            return q.BranchResult(rows=[row('S', source), row(unique, source)])

    request = query(state=[A, B], logic=logic)
    rows, result_meta = asyncio.run(q.execute_graph_source(request, Adapter(), q.RegionIndex(Graph())))
    assert {r['study_id'] for r in rows} == expected
    by_id = {r['study_id']: {m['selected_root'] for m in r['query_matches']} for r in rows}
    assert by_id['S'] == {A, B}
    if logic == 'or':
        assert by_id['A-only'] == {A}
        assert by_id['B-only'] == {B}
    assert not any(r['state_concept_uri'] for r in rows)
    payload = aggregate(request, {source: rows})
    assert {e['target'] for e in edges(payload)} == {A, B}
    assert all(e['weight'] == (2 if logic == 'or' else 1) for e in edges(payload))
    assert payload['meta']['returned_unique_studies'] == len(expected)
    assert result_meta['returned_unique_studies'] == len(expected)


def test_selected_root_only_with_exact_descendant_outside_and_missing_mappings():
    rows = [matched(row(name, mappings=mappings), A) for name, mappings in (
        ('exact', [{'uri': A, 'role': 'condition'}]),
        ('descendant', [{'uri': B, 'role': 'outcome'}]),
        ('outside', [{'uri': C, 'role': 'condition'}]),
        ('missing', []),
    )]
    rows += [rows[1], matched(row('unmapped-intervention', intervention=None), A)]
    payload = aggregate(query(state=[A]), {'ctgov': rows})
    edge, = edges(payload)
    assert edge['target'] == A
    assert edge['weight'] == 4
    assert edge['support_counts'] == dict(search_only=2, mapping_only=0, both=2)
    assert next(n for n in payload['nodes'] if n['id'] == A)['studyCount'] == 4
    assert payload['meta']['sources']['ctgov']['omitted_unattributed_unique_studies'] == 1
    support = g._live_support(rows[1], A)
    assert support['direct_state_mappings'] == [{'uri': B, 'role': 'outcome', 'root_relationship': 'descendant'}]
    assert support['query_matches'][0]['retrieval_role'] is None


def test_root_membership_is_not_inferred_from_mapping_or_another_branch():
    request = query(state=[A, B])
    rows = [matched(row(mappings=[{'uri': B, 'role': 'condition'}]), A)]
    assert {e['target'] for e in edges(aggregate(request, {'ctgov': rows}))} == {A}
    rows = [matched(row(mappings=[{'uri': B, 'role': 'condition'}]), B)]
    assert {e['target'] for e in edges(aggregate(request, {'ctgov': rows}))} == {B}
    assert not edges(aggregate(request, {'ctgov': [row()]}))


@pytest.mark.parametrize('role', ['condition', 'outcome'])
@pytest.mark.parametrize('source', ['ctgov', 'isrctn'])
def test_mixed_filters_use_planner_population_and_only_canonical_coordinate(role, source):
    calls = []

    class Adapter:
        source_id = source

        async def execute(self, spec, budget, regions):
            calls.append(spec)
            # A source's strict-role result excludes the wrong-role negative.
            assert spec == {'state': A, role: C, 'intervention': I, 'region': q.WORLD_URI}
            return q.BranchResult(rows=[row('positive', source, [{'uri': C, 'role': role}])])

    request = query(state=[A], intervention=[I], region=[q.WORLD_URI], **{role: [C]})
    rows, _ = asyncio.run(q.execute_graph_source(request, Adapter(), q.RegionIndex(Graph())))
    edge, = edges(aggregate(request, {source: rows}))
    assert edge['target'] == A
    assert edge['weight'] == 1
    assert rows[0]['direct_state_mappings'] == [{'uri': C, 'role': role}]
    assert calls
    assert g._graph_state_roots(request) == (A,)


@pytest.mark.parametrize('source', ['aea', 'who-ictrp'])
@pytest.mark.parametrize('logic', ['or', 'and'])
def test_stored_selection_projection_and_details_share_actual_membership(monkeypatch, source, logic):
    dataset = Dataset()
    native = dataset.graph(URIRef(g._source_graph(source)))
    study = URIRef('https://example.org/study/S')
    other = URIRef('https://example.org/study/Other')
    study_type = URIRef(g.v1.AEA + 'RCTStudy' if source == 'aea' else g.v1.UE + 'Evidence')
    from rdflib.namespace import RDF
    for subject in (study, other):
        native.add((subject, RDF.type, study_type))
        native.add((subject, URIRef(g.v1.UE + 'matchesOutcome'), URIRef(A)))
        native.add((subject, URIRef(g.v1.DCTERMS + 'identifier'), Literal(str(subject).rsplit('/', 1)[-1])))
        for role, text in [('outcome', 'mapped descendant'), ('intervention', 'mapped intervention')]:
            predicate = g._DIRECT_CROSSWALKS[source][role][0]
            if predicate.startswith('('):
                predicate = predicate.split('<', 1)[1].split('>', 1)[0]
            native.add((subject, URIRef(predicate), Literal(text)))
    native.add((study, URIRef(g.v1.UE + 'matchesCondition'), URIRef(B)))
    captured = []

    async def select(sparql, budget):
        captured.append(sparql)
        return [{str(k): str(v) for k, v in r.asdict().items()} for r in dataset.query(sparql)]

    async def maps(source_id, roles):
        return {'condition': {}, 'outcome': {'mapped descendant': (B,)}}, {'mapped intervention': (I, J)}

    monkeypatch.setattr(g, '_sparql_select', select)
    monkeypatch.setattr(g, '_stored_direct_maps', maps)
    request = query(state=[A, B], logic=logic)
    rows, metadata, _ = asyncio.run(g.load_local_groups(source, request, [A, B], []))
    assert metadata['returned_unique_studies'] == (2 if logic == 'or' else 1)
    assert {(r['state'], r['intervention']) for r in rows} == {(a, i) for a in (A, B) for i in (I, J)}
    a_row = next(r for r in rows if r['state'] == A and r['intervention'] == I)
    assert int(a_row['weight']) == (2 if logic == 'or' else 1)
    b_row = next(r for r in rows if r['state'] == B and r['intervention'] == I)
    assert b_row['studyUris'] == str(study)
    payload = aggregate(request, stored={source: rows})
    edge = next(e for e in edges(payload) if e['target'] == A and e['source'] == I)
    assert edge['support_counts'] == dict(search_only=0, mapping_only=int(a_row['weight']), both=0)
    details, _ = asyncio.run(g._local_detail_rows(source, request, A, I, 25, [A, B], []))
    assert len(details) == edge['weight']
    assert all(r['support_provenance']['category'] == 'mapping_only' for r in details)
    assert all(r['support_provenance']['direct_state_mappings'][0]['uri'] == B for r in details)
    assert all(r['state_concept_uri'] is None for r in details)
    assert 'LIMIT 101' in captured[0]


def test_mixed_categories_source_qualified_dedup_and_multiple_interventions():
    direct = matched(row('same', mappings=[{'uri': B, 'role': 'outcome'}]), A)
    search = matched(row('same', 'ISRCTN'), A)
    stored_support = g._support_record(A, [dict(selected_root=A, retrieval_basis='mapping', retrieval_role='outcome')], [{'uri': B, 'role': 'outcome'}])
    local = {'state': A, 'intervention': I, 'studyUris': 'same', 'support_provenance': {'same': stored_support}}
    payload = aggregate(query(state=[A]), {'ctgov': [direct, direct, {**direct, 'intervention_concept_uri': J}], 'isrctn': [search]}, {'aea': [local], 'who-ictrp': [local]})
    edge = next(e for e in edges(payload) if e['source'] == I)
    assert edge['weight'] == 4
    assert edge['source_counts'] == {'ctgov': 1, 'isrctn': 1, 'aea': 1, 'who-ictrp': 1}
    assert edge['support_counts'] == dict(search_only=1, mapping_only=2, both=1)
    assert payload['meta']['returned_unique_studies'] == 4


def test_provenance_digest_reconciles_pages_and_rejects_same_identity_changed_basis(monkeypatch):
    request = query(state=[A])
    rows = [matched(row('S1'), A), matched(row('S2', mappings=[{'uri': B, 'role': 'outcome'}]), A)]
    edge, = edges(aggregate(request, {'ctgov': rows}))

    async def live(*args):
        return rows, meta(2)

    async def regions(*args):
        return q.RegionIndex(Graph())

    monkeypatch.setattr(g, 'execute_graph_source', live)
    monkeypatch.setattr(g, 'region_index_for_query', regions)
    monkeypatch.setattr(g, 'default_adapters', lambda: {'ctgov': object()})
    kwargs = dict(expected_source_counts=edge['source_counts'], expected_source_membership_digests=edge[g._EDGE_MEMBERSHIP_DIGESTS], cursor_context='test')
    for offset in (0, 1):
        page = asyncio.run(g.execute_edge_details(request, A, I, offset, 1, **kwargs))
        assert page['meta']['sound']
        assert page['meta']['support_counts'] == edge['support_counts']
        assert len(page['results']) == 1
        assert page['results'][0]['support_provenance']['category'] == ('search_only' if offset == 0 else 'both')
    rows[0]['direct_state_mappings'] = [{'uri': A, 'role': 'condition'}]
    with pytest.raises(HTTPException) as error:
        asyncio.run(g.execute_edge_details(request, A, I, 0, 1, **kwargs))
    assert error.value.status_code == 409


def test_old_semantic_tokens_rejected_and_cache_identity_versioned(monkeypatch):
    monkeypatch.setenv('GRAPH_EDGE_TOKEN_SECRET', 'selected-root-test')
    request = query(state=[A])
    edge, = edges(aggregate(request, {'ctgov': [matched(row(), A)]}))
    token = g._encode_edge_detail_token(request, edge, 'a'*64, 'b'*64)
    encoded = token.split('.')[0]
    payload = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
    payload.pop('projectionVersion')
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    signature = hmac.new(g._edge_detail_token_key(None, None), encoded.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(HTTPException):
        g._decode_edge_detail_token(encoded + '.' + signature, query=request, edge_id=edge['id'], condition_uri=A, intervention_uri=I, taxonomy_version='a'*64, dataset_version='b'*64)
    identity = g.graph_cache_identity(request, taxonomy_version='a'*64, dataset_version='b'*64)
    assert identity['projectionVersion'] == g.PROJECTION_VERSION


@pytest.mark.parametrize('source', ['ctgov', 'isrctn'])
@pytest.mark.parametrize('role', ['condition', 'outcome'])
def test_real_live_adapters_send_strict_mixed_role_queries(monkeypatch, source, role):
    """Mock registry transport, retaining the actual planner and adapter path."""
    import httpx
    other_role = 'outcome' if role == 'condition' else 'condition'
    registry = [
        {'id': 'positive', role: ['Other'], other_role: ['Root']},
        {'id': 'wrong-role', role: [], other_role: ['Root', 'Other']},
    ]
    calls = []

    async def resolve(uri):
        label = {A: 'Root', C: 'Other'}[uri]
        return q.ConceptResolution(label, (label,), ())

    async def taxonomy(function, *args):
        if function in (q._source_direct_intervention_crosswalk, q._isrctn_direct_intervention_crosswalk):
            return {'therapy': (I,)}
        if function is q._local_intervention_labels:
            return {I: 'Therapy'}
        return {}  # No independent State mapping is required.

    monkeypatch.setattr(q, '_taxonomy_call', taxonomy)

    if source == 'ctgov':
        def transport(request):
            calls.append(request)
            assert request.url.params['query.term'] == '(AREA[ConditionSearch]("Root") OR AREA[OutcomeSearch]("Root"))'
            assert request.url.params['query.cond' if role == 'condition' else 'query.outc'] == '"Other"'
            selected = [r for r in registry if 'Other' in r[role]]
            return httpx.Response(200, json={'studies': selected})

        def map_study(raw, *args, **kwargs):
            return dict(source='CT.gov', nctId=raw['id'], _intervention_names=['therapy'],
                        condition_mesh_uris=[], intervention_mesh_uris=[], outcomes=[])

        monkeypatch.setattr(q.v1, 'map_ctgov_study', map_study)
        adapter = q.CtgovAdapter(lambda: httpx.AsyncClient(transport=httpx.MockTransport(transport)), concept_resolver=resolve)
    else:
        async def fetch(client, endpoint, expression, timeout):
            calls.append(expression)
            field = 'condition' if role == 'condition' else 'outcomeMeasures'
            assert expression == f'(condition: Root OR outcomeMeasures: Root) AND {field}: Other'
            return [dict(source='ISRCTN', study_id=r['id'], intervention_descriptions=['therapy'],
                         condition_descriptions=r['condition'],
                         outcomes=[{'measure': text} for text in r['outcome']])
                    for r in registry if 'Other' in r[role]]

        adapter = q.IsrctnAdapter(
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
            fetch_batch=fetch, concept_resolver=resolve,
        )
    request = query(state=[A], **{role: [C]})
    rows, metadata = asyncio.run(q.execute_graph_source(request, adapter, q.RegionIndex(Graph()), budget_factory=lambda: q.SourceBudget(seconds=60)))
    assert calls
    assert {r['study_id'] for r in rows} == {'positive'}
    assert metadata['returned_unique_studies'] == 1
    edge, = edges(aggregate(request, {source: rows}))
    assert edge['target'] == A
    assert edge['support_counts'] == dict(search_only=1, mapping_only=0, both=0)
