"""Read-only, version-pinned reviewed results joined to registry discovery.

Read State identities from the graph. Numerical evidence is joined only to returned
non-API studies by live_results; unsupported query contexts remain explicit.
"""
import asyncio
import copy
import json
import os
import re
import httpx
from api.vocabulary_compat import canonicalize_payload

PREFIX='https://universalevidence.com/'


def enabled(query):
    return bool(os.getenv('UE_COMPACT_RESULTS_GRAPH'))


def supported(query):
    return not any(v for k,v in query.values.items() if k not in ('state','intervention'))


def iri(value):
    if not re.fullmatch(r'https://universalevidence\.com/[A-Za-z0-9_./:#-]+',value):
        raise ValueError('Invalid compact results IRI')
    return '<'+value+'>'


def select_query(query,graph):
    filters=[]
    states=query.values.get('state',())
    if states:
        # State filtering is applied to the same result, with explicit OR/AND.
        clauses=['EXISTS { ?result c:state '+iri(v)+' }' for v in states]
        filters.append('FILTER ('+(' && ' if query.logic.get('state')=='and' else ' || ').join(clauses)+')')
    selected=query.values.get('intervention',())
    if selected:
        clauses=['EXISTS { ?result c:matchesIntervention '+iri(v)+' }' for v in selected]
        filters.append('FILTER ('+(' && ' if query.logic.get('intervention')=='and' else ' || ').join(clauses)+')')
    return '''PREFIX c: <https://universalevidence.com/compact-results/>
SELECT DISTINCT ?result ?row ?studyData WHERE { GRAPH '''+iri(graph)+''' {
 ?result a c:Result; c:row ?row; c:study ?study .
 ?study c:metadata ?studyData .
 '''+'\n'.join(filters)+''' } } LIMIT 10001'''


async def read_compact(query):
    graph=os.environ['UE_COMPACT_RESULTS_GRAPH']
    if not supported(query):
        return {'status':'unsupported_filters','rows':[],'studies':[], 'graph':graph}
    try:
        endpoint=os.getenv('FUSEKI_URL','http://127.0.0.1:3030/ue/query')
        async with httpx.AsyncClient(timeout=2.5) as client:
            response=await client.post(endpoint,data={'query':select_query(query,graph),'timeout':'2000'},headers={'Accept':'application/sparql-results+json'})
            response.raise_for_status();bindings=response.json()['results']['bindings']
            if not bindings:
                # A filter with no matches says nothing about release availability.
                health_query = ('PREFIX c: <https://universalevidence.com/compact-results/> '
                    'ASK WHERE { GRAPH '+iri(graph)+' { '
                    '?result a c:Result; c:row ?row; c:study ?study . '
                    '?study c:metadata ?metadata . } }')
                health = await client.post(endpoint,data={'query':health_query,'timeout':'2000'},headers={'Accept':'application/sparql-results+json'})
                health.raise_for_status()
                if health.json()['boolean'] is not True:
                    raise ValueError('Compact release graph is missing or has no readable results')
        if len(bindings)>10000:raise ValueError('Compact query exceeds pilot capacity')
        rows=[];studies={}
        for b in bindings:
            row=canonicalize_payload(json.loads(b['row']['value']));study=canonicalize_payload(json.loads(b['studyData']['value']))
            if row['study']!=study['study_identity']:raise ValueError('Inconsistent result identity')
            # Query states are indexes; preserve the actual measured states in the row.
            row['states']=sorted(set(row.get('states',[])))
            rows.append(row);studies[study['study_identity']]=study
        return {'status':'available','rows':rows,'studies':list(studies.values()),'graph':graph}
    except (httpx.HTTPError,ValueError,KeyError,TypeError):
        return {'status':'unavailable','rows':[],'studies':[], 'graph':graph}


def record_key(source,sid):
    normalized={'CT.gov':'ctgov','ClinicalTrials.gov':'ctgov','ISRCTN':'isrctn','AEA':'aea','WHO ICTRP':'who-ictrp'}.get(source,source)
    return (normalized,str(sid or ''))


def merge(payload,compact):
    out=copy.deepcopy(payload)
    out['reviewed_results']={k:v for k,v in compact.items() if k!='studies'}
    out.setdefault('meta',{})['reviewed_results']={'status':compact['status'],'graph':compact['graph']}
    if compact['status']!='available':return out
    metadata={s['study_identity']:s for s in compact['studies']}
    aliases={record_key(r['source'],r['record_id']):s['study_identity'] for s in compact['studies'] for r in s.get('registry_records',[])}
    def identity(s):
        return aliases.get(record_key(s.get('source'),s.get('study_id'))) or s.get('study_identity') or '\x00'.join(record_key(s.get('source'),s.get('study_id') or s.get('url') or s.get('title')))
    merged={}
    for row in out.get('results',[]):
        sid=identity(row);row['study_identity']=sid
        merged.setdefault((sid,row.get('intervention_concept_uri')),row)
    for result in compact['rows']:
        sid=result['study'];study=metadata[sid]
        for intervention in (result.get('finding_mapping') or result.get('summary_mapping') or {}).get('tested',[]):
            key=(sid,intervention)
            if key not in merged:
                # Prefer current live metadata when that study is already present.
                current=next((r for (s,_),r in merged.items() if s==sid),study)
                row={**study,**current,'study_identity':sid,'intervention_concept_uri':intervention,
                     'intervention_concept':result['intervention_labels'][intervention],
                     'match_provenance':[{'basis':'reviewed-result'}]}
                merged[key]=row
    out['results']=list(merged.values())
    out['meta']['returned_unique_studies']=len({r['study_identity'] for r in out['results']})
    out['meta']['reviewed_results']['studies_with_results']=len(metadata)
    # Registry truncation/status is deliberately preserved: local evidence is not exhaustive.
    return out


async def combine(live,query):
    async def safe_live():
        try:return await live
        except (TimeoutError,httpx.HTTPError,OSError,ValueError):
            from query_v2 import ExecutionProgress
            payload=ExecutionProgress().snapshot()
            payload['meta']['execution_status']='partial'
            payload['meta']['truncated']=True
            return payload
    payload,compact=await asyncio.gather(safe_live(),read_compact(query))
    return merge(payload,compact)
