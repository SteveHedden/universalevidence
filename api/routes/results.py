"""Bounded production result transport; immutable assets, transient source RDF."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from result_config import load_result_config
from search_results import search_results, scoped_search

router=APIRouter()
_slots=asyncio.Semaphore(1)  # One worker in the deployment; max four live result calls.
_running=set()
_verified=None
_ctgov_retry_after=0.0


def release_config():
    global _verified
    raw=os.getenv('UE_RESULT_RELEASE')
    if not raw: raise HTTPException(503,'Reviewed results release is not configured')
    root=Path(raw).resolve()
    try:
        manifest=json.loads((root/'manifest.json').read_text())
        signatures=tuple((name,(root/name).stat().st_mtime_ns,(root/name).stat().st_size) for name in manifest['files'])
        key=(str(root),hashlib.sha256((root/'manifest.json').read_bytes()).hexdigest(),signatures)
        if _verified and _verified[0]==key: return dict(_verified[1])
        for name,entry in manifest['files'].items():
            path=(root/name).resolve()
            if path.parent!=root or hashlib.sha256(path.read_bytes()).hexdigest()!=entry['sha256']:
                raise ValueError('Result release checksum mismatch')
        config=load_result_config(root/'config.json')
        asset_paths=[config[k] for k in ('crosswalk','rules','interventions','intervention_crosswalk','evidence')]+config['taxonomy']
        if config.get('comparison_components'): asset_paths.append(config['comparison_components'])
        asset_paths.extend(config.get('report_semantics',[]))
        asset_paths.extend(config[k] for k in ('source_registry','result_interpretation','aggregation_rules','api_semantics') if config.get(k))
        for path in asset_paths:
            if Path(path).parent!=root or Path(path).name not in manifest['files']: raise ValueError('Unmanifested runtime asset')
        if config.get('review_only') or config.get('search_url'): raise ValueError('Review configuration cannot serve production results')
        config.update(discover_reviewed_results=True,include_all_statuses=True,fetch_mapped_only=True,source_timeout=min(5,float(config.get('source_timeout',5))))
        from search_results import prime_release_evidence
        config['_release_evidence_signature']=prime_release_evidence(config)
        _verified=(key,config)
        return dict(config)
    except (OSError,ValueError,KeyError,TypeError) as error:
        raise HTTPException(503,'Results release unavailable or invalid') from error


async def production_fetch(sid,crosswalk,taxonomy,rules,timeout):
    """Hard network deadline, bounded bytes; mapping uses the reviewed adapter."""
    global _ctgov_retry_after
    if time.monotonic()<_ctgov_retry_after: raise RuntimeError('CT.gov rate limited; retry after source cooldown')
    import httpx
    from datetime import datetime,timezone
    from ctgov_results import LiveClient,Response as SourceResponse,BASE,validate_source,query_studies
    from search_results import _RDF_LOCK
    import re
    if not re.fullmatch(r'NCT\d{8}',sid): raise ValueError('Invalid registry identifier')
    start=time.perf_counter()
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream('GET',BASE+sid) as response:
                if response.status_code==429:
                    try: delay=min(300,max(30,int(response.headers.get('Retry-After','60'))))
                    except ValueError: delay=60
                    _ctgov_retry_after=time.monotonic()+delay
                response.raise_for_status();body=bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body)>8_000_000: raise ValueError('Source response exceeds 8 MB')
    payload=json.loads(body);validate_source(payload,sid)
    source=SourceResponse(payload,hashlib.sha256(body).hexdigest(),datetime.now(timezone.utc).isoformat(),time.perf_counter()-start,BASE+sid)
    client=LiveClient(max_requests=1);client.cache[sid]=source
    def mapped():
        with _RDF_LOCK:return query_studies([sid],crosswalk,taxonomy,rules,client)
    return await asyncio.to_thread(mapped)


@router.get('/query/results')
async def results(state:str=Query(max_length=300), intervention:str=Query(max_length=300),
                  context:str=Query('{}',max_length=6000), format:str=Query('json',pattern='^(json|turtle)$')):
    started=time.perf_counter()
    if not state.startswith('https://universalevidence.com/vocab/states/') or not intervention.startswith('https://universalevidence.com/vocab/interventions/'):
        raise HTTPException(422,'Canonical State and Intervention URIs required')
    try:
        query=json.loads(context); values=query.get('values',{}); logic=query.get('logic',{})
        if not isinstance(values,dict) or not isinstance(logic,dict): raise ValueError()
        if any(not isinstance(v,list) or len(v)>10 or any(not isinstance(x,str) or len(x)>300 for x in v) for v in values.values()): raise ValueError()
        # State is the displayed result group. Other predicates must remain active.
        if values.get('state') not in (None,[],[state]): raise ValueError()
        values={**values,'state':[state]}
        if not values.get('intervention'): values['intervention']=[intervention]
        from query_v2 import canonical_query,execute_query_v2,AXES
        if set(values)-set(AXES) or set(logic)-set(AXES): raise ValueError()
        canonical=canonical_query(values,logic)
    except (ValueError,TypeError,AttributeError): raise HTTPException(422,'Invalid result query context')
    from api.query_v2_cache import query_v2_cache
    from live_results import combine
    payload = await combine(query_v2_cache.get_or_execute(canonical,execute_query_v2),canonical)
    evidence=payload['reviewed_results']
    rows=evidence['rows']
    result={'completion':'partial' if evidence['partial'] else 'complete_within_declared_search_scope',
            'rows':rows,'coverage':evidence['coverage'],'search':payload,
            'timings':{'request_seconds_before_encoding':time.perf_counter()-started},
            'interpretation':evidence.get('scope',evidence.get('reason','Results unavailable'))}
    if format=='turtle':
        from live_results import rows_to_rdf
        body=rows_to_rdf(rows)
    else:body=json.dumps(result,allow_nan=False)
    return Response(body,media_type='text/turtle' if format=='turtle' else 'application/json',headers={'Cache-Control':'no-store'})
