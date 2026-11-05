"""Bounded query-time result integration. No persistent source documents or writes.

Explicit local review configuration supplies reviewed mapping assets. Registry
search uses query-v2; scoped mappings supplement lexical intervention discovery.
"""
from __future__ import annotations
import asyncio
import inspect
from copy import deepcopy
import html
import json
import time
from threading import Lock
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote
import httpx
from rdflib import Graph, URIRef, RDF
from rdflib.namespace import SKOS, DCTERMS
from ctgov_results import UE, LiveClient, query_studies, digest
from ctgov_result_rules import load_rules
from ctgov_intervention_identity import parse_key, normalize
import evidence_enrichment as enrichment
from api.vocabulary_compat import canonicalize_indicator_graph, canonicalize_payload

QUERY = '''PREFIX ue: <https://universalevidence.com/ontology/>
SELECT ?measurement ?study ?indicator ?value ?unit ?comparison ?timing ?uncertainty ?source WHERE {
 ?measurement a ue:OutcomeMeasurement; ue:measurementStudy ?study; ue:ofState ?state.
 OPTIONAL {?measurement ue:ofIndicator ?indicator}
 OPTIONAL {?measurement ue:value ?value} OPTIONAL {?measurement ue:unit ?unit}
 OPTIONAL {?measurement ue:comparison ?comparison} OPTIONAL {?measurement ue:timing ?timing}
 OPTIONAL {?measurement ue:uncertainty ?uncertainty} OPTIONAL {?measurement ue:sourceLocator ?source}
} ORDER BY ?study ?measurement'''


def descendants(graph, root):
    seen=set(); pending=[URIRef(root)]
    while pending:
        node=pending.pop()
        if node in seen: continue
        seen.add(node); pending.extend(graph.subjects(SKOS.broader,node)); pending.extend(graph.objects(node,SKOS.narrower))
    return seen


def scoped_candidates(crosswalk, taxonomy, intervention):
    targets=descendants(taxonomy,intervention); result={}
    for entry,key in crosswalk.subject_objects(UE.rawText):
        if not targets.intersection(set(crosswalk.objects(entry,SKOS.exactMatch))|set(crosswalk.objects(entry,SKOS.closeMatch))): continue
        parsed=parse_key(str(key))
        if parsed: result.setdefault(parsed[0],set()).add(parsed[1])
    return result


async def registry_search(state, intervention, config):
    if config.get('search_url'):
        async with httpx.AsyncClient(timeout=12) as client:
            r=await client.get(config['search_url'],params={'state':state,'intervention':intervention}); r.raise_for_status(); return r.json()
    from query_v2 import canonical_query, execute_query_v2
    return (await execute_query_v2(canonical_query({'state':[state],'intervention':[intervention]},{}))).payload


async def scoped_search(state, candidates, config):
    """At most four concurrent requests; state restriction precedes each cap."""
    from query_v2 import _resolve_local_concept, _ctgov_state_expression
    resolution=await asyncio.to_thread(_resolve_local_concept,state)
    if resolution is None: raise ValueError('Cannot resolve State for scoped discovery')
    expression, approximate=_ctgov_state_expression(resolution)
    ids=sorted(candidates)[:400]
    meta={'candidate_count':len(candidates),'requested_ids':ids,'request_cap':4,
          'truncated':len(candidates)>400,'approximate':approximate,'state_expression':expression}
    if not ids: return [],meta
    async with httpx.AsyncClient(timeout=config.get('source_timeout',10)) as client:
        async def page(chunk):
            async with asyncio.timeout(config.get('source_timeout',10)):
                r=await client.get('https://clinicaltrials.gov/api/v2/studies',params={
                'format':'json','pageSize':100,'filter.ids':','.join(chunk),'query.term':expression,
                'fields':'NCTId,BriefTitle,OverallStatus,InterventionName'})
            r.raise_for_status(); return r.json()
        pages=await asyncio.gather(*(page(ids[i:i+100]) for i in range(0,len(ids),100)),return_exceptions=True)
    body={'studies':[]}; meta['failures']=[]
    for page in pages:
        if isinstance(page,Exception): meta['failures'].append(str(page)); meta['truncated']=True
        else:
            body['studies'].extend(page.get('studies',[])); meta['truncated'] |= bool(page.get('nextPageToken'))
    rows=[]
    for study in body.get('studies',[]):
        p=study['protocolSection']; sid=p['identificationModule']['nctId']
        names={normalize(i['name']) for i in p.get('armsInterventionsModule',{}).get('interventions',[])}
        # A stale label cannot silently continue to qualify a study.
        if sid not in candidates or not names.intersection(candidates[sid]): continue
        rows.append({'source':'CT.gov','study_id':sid,'status':p['statusModule']['overallStatus'].lower(),
                     'title':p['identificationModule'].get('briefTitle'),'retrieval_basis':'reviewed_scoped_intervention_mapping'})
    meta['returned_ids']=[r['study_id'] for r in rows]; meta['truncated'] |= bool(body.get('nextPageToken'))
    return rows,meta


def _parse_assets(config):
    taxonomy=Graph()
    for p in config['taxonomy']: taxonomy.parse(p)
    return (canonicalize_indicator_graph(Graph().parse(config['crosswalk'])),canonicalize_indicator_graph(taxonomy),canonicalize_payload(load_rules(config['rules'])),
            Graph().parse(config['intervention_crosswalk']),Graph().parse(config['interventions']))


_ASSET_CACHE = OrderedDict()
_ASSET_LOCK = Lock()

def load_assets(config):
    """Reuse only unchanged local mapping assets; never cache API responses here."""
    paths=[config['crosswalk'], *config['taxonomy'], config['rules'],
           config['intervention_crosswalk'], config['interventions']]
    def signature():
        result=[]
        for path in paths:
            p=Path(path).resolve(); st=p.stat()
            result.append((str(p),st.st_mtime_ns,st.st_ctime_ns,st.st_size))
        return tuple(result)
    with _ASSET_LOCK:
        key=signature()
        if key in _ASSET_CACHE:
            _ASSET_CACHE.move_to_end(key)
            return _ASSET_CACHE[key]
        assets=_parse_assets(config)
        if signature()!=key:
            raise ValueError('Mapping assets changed while loading; retry search')
        _ASSET_CACHE[key]=assets
        while len(_ASSET_CACHE)>2: _ASSET_CACHE.popitem(last=False)
        return assets


# Only verified immutable release assets use this cache. Authoring readers retain
# their transaction and freshness checks. Parsing occurs once at release setup,
# outside the per-request evidence selection budget.
_EVIDENCE_LOCK = Lock()
_EVIDENCE_CACHE = OrderedDict()

def evidence_signature(path):
    p=Path(path).resolve();st=p.stat()
    if p.with_name('.'+p.stem+'.transaction.json').exists():
        raise ValueError('Interrupted RDF publication')
    return (str(p),st.st_mtime_ns,st.st_ctime_ns,st.st_size)

def prime_release_evidence(config):
    key=evidence_signature(config['evidence'])
    with _EVIDENCE_LOCK:
        if key not in _EVIDENCE_CACHE:
            with enrichment.rdf_store.snapshot(config['evidence'],enrichment.SCHEMA,enrichment.export_rdf) as store:
                data=(store.data,store.meta,store.revision)
            if evidence_signature(config['evidence'])!=key:
                raise ValueError('Release evidence changed during loading')
            _EVIDENCE_CACHE[key]=data
            while len(_EVIDENCE_CACHE)>2:_EVIDENCE_CACHE.popitem(last=False)
    return key

from contextlib import contextmanager
@contextmanager
def result_evidence_reader(config):
    key=config.get('_release_evidence_signature')
    if key is None:
        with enrichment.reader(config['evidence']) as store:yield store
        return
    if evidence_signature(config['evidence'])!=key:
        raise ValueError('Verified release evidence changed')
    started=time.monotonic()
    with _EVIDENCE_LOCK:
        if key not in _EVIDENCE_CACHE:raise ValueError('Release evidence cache unavailable; reload release')
        data,meta,revision=deepcopy(_EVIDENCE_CACHE[key])
    yield SimpleNamespace(data=data,meta=meta,revision=revision,read_deadline=started+2.0)


def local_results(ids, state, config, taxonomy):
    """Canonical eligibility gate and atomic, per-study graph construction."""
    coverage=[]; graph=Graph()
    with result_evidence_reader(config) as store:
        states=descendants(taxonomy,state)
        for record in ids:
            source,sid = record if isinstance(record,tuple) else ('aea',record)
            try:
                # Immutable release parsing is already complete. Bound each study's
                # selection/export, so earlier studies cannot exhaust later ones.
                if config.get('_release_evidence_signature') is not None:
                    store.read_deadline=time.monotonic()+2.0
                identity=enrichment.resolve(store,source,sid)
                findings=canonicalize_payload(enrichment.public_study(store,identity)['reported_findings']) if identity else []
                matched=[f for f in findings if URIRef(f['state_uri']) in states or any(x in states for x in taxonomy.objects(URIRef(f.get('indicator_uri') or ''),UE.measures))]
                selected={f['id']:deepcopy(store.data['findings'][f['id']]) for f in matched}
                docs={s['document_id'] for f in selected.values() for s in f['sources']}
                subset=SimpleNamespace(read_deadline=store.read_deadline,data={
                    'studies':{identity:store.data['studies'][identity]} if identity else {},
                    'documents':{k:store.data['documents'][k] for k in docs},'findings':selected})
                study_graph=enrichment.export_rdf(subset)
                graph+=canonicalize_indicator_graph(study_graph)
                coverage.append({'study':sid,'source':source,'status':('available' if any(f.get('value') is not None for f in matched) else 'reviewed_text_only' if matched else 'no_reviewed_local_results'),
                                 'records':len(matched),'missing_mappings':sum(not f.get('indicator_uri') for f in matched)})
            except (ValueError,KeyError,TypeError,OSError) as error:
                coverage.append({'study':sid,'source':source,'status':'retrieval_failure','reason':str(error)})
        return graph,coverage,enrichment.snapshot_version(store)


def verified_origins(records,config):
    """Resolve WHO records only through reviewed canonical study identities."""
    who=[r for r in records if r.get('source') in ('WHO ICTRP','WHO-ICTRP','who-ictrp')]
    if not who:return [],[]
    origins=set();coverage=[]
    try:
        with result_evidence_reader(config) as store:
            for row in who:
                identity=enrichment.resolve(store,'who-ictrp',row['study_id'])
                registrations=store.data['studies'][identity]['records'] if identity else []
                routed=[r['record_id'] for r in registrations if r['source']=='ctgov']
                origins.update(routed)
                coverage.append({'study':row['study_id'],'source':row['source'],
                    'originating_ctgov_ids':routed,'status':'verified_origin' if routed else 'verified_report_extraction_or_identity_required'})
    except (OSError,ValueError,KeyError,TypeError) as error:
        coverage=[{'study':r['study_id'],'source':r['source'],'status':'origin_resolution_failure','reason':str(error)} for r in who]
    return sorted(origins),coverage


_RDF_LOCK = Lock()  # RDFLib/pyparsing initial query compilation is not thread safe.

def fetch_results(sid,crosswalk,taxonomy,rules,timeout):
    client=LiveClient(max_requests=1,timeout=timeout)
    client.get(sid)  # Network overlaps; mapping and query compilation are serialized.
    with _RDF_LOCK:
        return query_studies([sid],crosswalk,taxonomy,rules,client)


def coverage_status(result, allowed_states=None):
    if result['failures']: return 'retrieval_failure'
    response=result['responses'][0].payload
    outcomes=response.get('resultsSection',{}).get('outcomeMeasuresModule',{}).get('outcomeMeasures',[])
    if not response.get('hasResults') or not any(o.get('reportingStatus')=='POSTED' for o in outcomes): return 'no_posted_structured_results'
    eligible=[r for r in result['records'] if r.get('mapping') and (allowed_states is None or URIRef(r['mapping']['state']) in allowed_states)]
    if any(r.get('numerically_usable') for r in eligible): return 'available'
    if allowed_states is not None and not eligible and any(r.get('mapping') for r in result['records']): return 'no_mapped_results_for_selected_state'
    if any(c.get('mapping_supported') for c in result['coverage']): return 'unsupported_results'
    return 'missing_mapping'


async def search_results(state, intervention, config, *, search=registry_search, supplement=scoped_search, fetch=fetch_results):
    started=time.perf_counter(); timings={}; failures=[]
    stamp=time.perf_counter(); cw,tax,rules,icw,itax=await asyncio.to_thread(load_assets,config)
    timings['asset_loading_seconds']=time.perf_counter()-stamp
    candidates=scoped_candidates(icw,itax,intervention)
    stamp=time.perf_counter()
    discoveries=await asyncio.gather(search(state,intervention,config),supplement(state,candidates,config),return_exceptions=True)
    discovery,extra=discoveries
    if isinstance(discovery,Exception):
        failures.append({'stage':'registry_search','reason':str(discovery)}); discovery={'results':[],'meta':{'execution_status':'partial','truncated':True}}
    if isinstance(extra,Exception):
        failures.append({'stage':'scoped_search','reason':str(extra)}); extra=([],{'status':'retrieval_failure','truncated':True})
    timings['search_seconds']=time.perf_counter()-stamp
    studies={ (r.get('source'),r.get('study_id')):r for r in discovery['results'] }
    for r in extra[0]: studies.setdefault((r['source'],r['study_id']),r)
    if config.get('discover_reviewed_results',False) and config.get('include_all_statuses') and config.get('report_semantics'):
        from report_result_semantics import discovery_candidates
        try:
            reviewed=await asyncio.to_thread(discovery_candidates,config,state,intervention,tax,itax)
            for r in reviewed:studies.setdefault((r['source'],r['study_id']),r)
        except (ValueError,KeyError,TypeError,OSError) as error:
            failures.append({'stage':'reviewed_result_discovery','reason':str(error)})
    completed=[r for r in studies.values() if not config.get('include_all_statuses') or r.get('study_id')]
    if not config.get('include_all_statuses'):
        completed=[r for r in completed if str(r.get('status','')).lower()=='completed']
    ctids=sorted({r['study_id'] for r in completed if r['source']=='CT.gov'})
    routing=[]; unmapped_ids=[]
    if config.get('include_all_statuses'):
        origins,routing=await asyncio.to_thread(verified_origins,completed,config)
        ctids=sorted(set(ctids).union(origins))
        # Spend the bounded upstream budget on maintained result mappings first.
        # Intervention-only discovery candidates may have no posted result rules.
        mapped_ids=set()
        for rule in rules.get('outcomes',[]):
            key=rule.get('key','')
            if not key.startswith('ctgov-indicator:v1|'): continue
            try:
                context=json.loads(unquote(key.split('|',1)[1]))
                if isinstance(context,dict): mapped_ids.add(context.get('record_id'))
            except (ValueError,TypeError): pass
        ctids.sort(key=lambda sid:(sid not in mapped_ids,sid not in candidates,sid))
        if config.get('fetch_mapped_only'):
            unmapped_ids=[sid for sid in ctids if sid not in mapped_ids]
            ctids=[sid for sid in ctids if sid in mapped_ids]
    cap=max(1,min(20,int(config.get('max_result_requests',10)))); selected=ctids[:cap]
    all_aeaids=sorted({r['study_id'] for r in completed if r['source']=='AEA'}); aeaids=all_aeaids[:100]
    if config.get('include_all_statuses'):
        all_aeaids=sorted({({'CT.gov':'ctgov','AEA':'aea','ISRCTN':'isrctn','WHO ICTRP':'who-ictrp'}.get(r['source'],r['source']),r['study_id']) for r in completed})
        aeaids=all_aeaids[:100]
    sem=asyncio.Semaphore(max(1,min(4,int(config.get('workers',4)))))
    async def one(sid):
        async with sem:
            args=(sid,cw,tax,rules,min(15,config.get('source_timeout',10)))
            return await fetch(*args) if inspect.iscoroutinefunction(fetch) else await asyncio.to_thread(fetch,*args)
    stamp=time.perf_counter()
    all_results=await asyncio.gather(*(one(sid) for sid in selected),asyncio.to_thread(local_results,aeaids,state,config,tax),return_exceptions=True)
    timings['retrieval_mapping_graph_wall_seconds']=time.perf_counter()-stamp
    graph=Graph(); coverage=[]; adapter_timings=[]; manifest=[]
    for sid,result in zip(selected,all_results[:-1]):
        if isinstance(result,Exception):
            failures.append({'study':sid,'stage':'study','reason':str(result)}); coverage.append({'study':sid,'source':'CT.gov','status':'retrieval_failure','reason':str(result)}); continue
        graph+=result['graph']; failures.extend(result['failures']); adapter_timings.extend(result['timings'])
        coverage.append({'study':sid,'source':'CT.gov','status':coverage_status(result,descendants(tax,state)),'outcomes':result['coverage']})
        manifest.extend({'url':r.url,'sha256':r.sha256,'retrieved_at':r.retrieved_at,'payload_sha256':digest(r.payload)} for r in result['responses'])
    local=all_results[-1]; version=None
    if isinstance(local,Exception):
        failures.append({'stage':'local_evidence','reason':str(local)})
        coverage.extend({'study':sid[1] if isinstance(sid,tuple) else sid,'source':sid[0] if isinstance(sid,tuple) else 'AEA','status':'retrieval_failure','reason':str(local)} for sid in aeaids)
    else:
        graph+=local[0]; coverage.extend(local[1]); version=local[2]
        failures.extend(c for c in local[1] if c['status']=='retrieval_failure')
    coverage.extend({'study':sid[1] if isinstance(sid,tuple) else sid,'source':sid[0] if isinstance(sid,tuple) else 'AEA','status':'local_study_cap_not_retrieved'} for sid in all_aeaids[100:])
    coverage.extend({'study':sid,'source':'CT.gov','status':'request_cap_not_retrieved'} for sid in ctids[cap:])
    coverage.extend({'study':sid,'source':'CT.gov','status':'missing_mapping_not_retrieved','reason':'No maintained result rule; posted-result availability was not checked'} for sid in unmapped_ids)
    coverage.extend({'study':r['study_id'],'source':r['source'],'status':'source_results_not_supported'} for r in completed if r['source'] not in ('CT.gov','AEA') and not config.get('include_all_statuses'))
    # Keep only the selected State subtree; no unrelated outcomes enter the response.
    allowed=descendants(tax,state)
    excluded={m for m in graph.subjects(RDF.type,UE.OutcomeMeasurement) if graph.value(m,UE.ofState) not in allowed and not any(x in allowed for x in tax.objects(graph.value(m,UE.ofIndicator),UE.measures))}
    for m in excluded: graph.remove((m,None,None))
    stamp=time.perf_counter(); rows=[]; seen=set()
    with _RDF_LOCK:
        queried=list(graph.query(QUERY))
    for row in queried:
        d={str(k):str(v) for k,v in row.asdict().items()}; key=d['measurement']
        if key in seen: continue
        seen.add(key); d['provenance']=[str(x) for x in graph.objects(URIRef(key),UE.sourceLocator)]
        for k in ('comparison','timing','uncertainty'):
            try:d[k]=json.loads(d.get(k,'null'))
            except ValueError:pass
        comp=d.get('comparison') or {}; classification=comp.get('classification',{})
        d['direction']=classification.get('direction') or str(graph.value(URIRef(key),UE.direction) or 'unknown')
        d['indicator_label']=str(tax.value(URIRef(d['indicator']),SKOS.prefLabel) or d['indicator']) if d.get('indicator') else 'Missing reviewed indicator mapping'
        d['source_details']=[{str(p):str(o) for p,o in graph.predicate_objects(URIRef(locator))} for locator in d['provenance']]
        for field in ('measure','subgroup','derivation','significance','indicator_review','result_text'):
            raw=graph.value(URIRef(key),UE[field])
            if raw is not None:
                try: d[field]=json.loads(str(raw))
                except ValueError: d[field]=str(raw)
        d['direction']={'exactly-zero-difference':'reported zero','no-change':'reported zero','unknown/unsupported':'unknown','uncertain':'unknown','mixed':'unknown'}.get(d['direction'],d['direction']) if config.get('include_all_statuses') else d['direction']
        d['interpretation']=str(graph.value(URIRef(key),UE.mapping) or '')
        d['citations']=[{'document':str(doc),'url':str(graph.value(doc,DCTERMS.source) or doc),'citation':str(graph.value(doc,DCTERMS.bibliographicCitation) or '')} for locator in d['provenance'] for doc in graph.objects(URIRef(locator),DCTERMS.source)]
        d['intervention_scope']='study context; this comparison does not map to the selected intervention' if comp.get('intervention_concepts') is not None and not descendants(itax,intervention).intersection(URIRef(x) for x in comp.get('intervention_concepts',[])) else 'See intervention and comparator components; no isolated component effect implied'
        rows.append(d)
    if config.get('api_semantics'):
        from api_result_semantics import annotate as annotate_api
        failures.extend(annotate_api(rows,all_results[:-1],config['api_semantics'],tax+itax))
    if config.get('report_semantics'):
        from report_result_semantics import annotate
        failures.extend(annotate(rows,graph,config,tax))
    timings['combined_sparql_seconds']=time.perf_counter()-stamp
    if config.get('comparison_components'):
        # Optional reviewed arm-component profile; no additional upstream calls.
        from result_comparison_model import load_components, project
        stamp=time.perf_counter()
        component_mappings=load_components(config['comparison_components'])
        # Only copy bearer links needed by these results; never invent subjects.
        for row in rows:
            measurement=URIRef(row['measurement'])
            for predicate in (UE.ofIndicator, UE.ofState):
                concept=graph.value(measurement,predicate)
                if concept:
                    for subject in tax.objects(concept,UE.bearer):
                        graph.add((concept,UE.bearer,subject))
        project(rows,graph,component_mappings,descendants(itax,intervention))
        timings['comparison_projection_seconds']=time.perf_counter()-stamp
    rows = canonicalize_payload(rows)
    canonicalize_indicator_graph(graph)
    stamp=time.perf_counter(); turtle=graph.serialize(format='turtle'); timings['rdf_serialization_seconds']=time.perf_counter()-stamp
    timings['total_seconds']=time.perf_counter()-started
    partial=bool(failures or discovery['meta'].get('truncated') or discovery['meta'].get('execution_status') in ('partial','timeout') or extra[1].get('truncated') or len(ctids)>cap or len(all_aeaids)>100)
    return {'completion':'partial' if partial else 'complete_within_declared_search_scope','query':{'state':state,'intervention':intervention,'completion_filter':'all_statuses' if config.get('include_all_statuses') else 'completed'},'search':discovery,
       'origin_routing':routing,'scoped_discovery':extra[1],'selected_ctgov_ids':selected,'coverage':coverage,'study_statuses':[{'study':r['study_id'],'source':r['source'],'status':r.get('status','unknown')} for r in completed],'failures':failures,'rows':rows,
       'rdf':turtle,'timings':timings,'adapter_timings':adapter_timings,'source_manifest':manifest,'local_snapshot_version':version,
       'limits':{'scoped_discovery_request_cap':4,'scoped_discovery_id_cap':400,'ctgov_result_request_cap':cap,'concurrency':max(1,min(4,int(config.get('workers',4)))),'aea_study_cap':100},
       'interpretation':'Multiple models and shared controls within a study are not independent studies. Group levels and within-group changes are not comparative effects. Components shared between arms are not isolated. No pooling, directional voting, or overall confidence is assigned.'}


def render(result):
    esc=lambda x:html.escape(x if isinstance(x,str) else json.dumps(x,ensure_ascii=False,indent=2))
    cards=''
    for r in result['rows']:
        comp=r.get('comparison') or {}; context=comp.get('context') or {}
        group=context.get('group') or {}
        label=comp.get('comparison') or (comp.get('intervention','')+' versus '+comp.get('reference','') if comp.get('intervention') else group.get('title') or ' versus '.join(g.get('title','Unnamed group') for g in (context.get('groups') or []) if isinstance(g,dict)) or comp.get('kind','Reported result'))
        fields=[('Study',r['study']),('Indicator',r['indicator_label']),('Result',r.get('value','No usable numerical estimate')+' '+r.get('unit','')),('Direction',r['direction']),('Statistic / model',comp.get('quantity') or comp.get('kind')),('Timing',r.get('timing')),('Uncertainty',r.get('uncertainty') or 'Not reported for this estimate; none inferred'),('Mapping interpretation',r.get('interpretation','')),('Source',r.get('citations',[]))]
        cards+='<article><h2>'+esc(label)+'</h2><p>'+esc(r.get('intervention_scope',''))+'</p>'+''.join('<h3>'+label+'</h3><pre>'+esc(value)+'</pre>' for label,value in fields)+'<details><summary>Full comparison, model and provenance</summary><pre>'+esc({'comparison':comp,'source_locators':r['source_details']})+'</pre></details></article>'
    coverage='<table><tr><th>Study</th><th>Source</th><th>Result availability</th></tr>'+''.join('<tr><td>'+esc(c['study'])+'</td><td>'+esc(c['source'])+'</td><td>'+esc(c['status'].replace('_',' '))+'</td></tr>' for c in result['coverage'])+'</table><details><summary>Outcome coverage details</summary><pre>'+esc(result['coverage'])+'</pre></details>'
    return '<!doctype html><meta charset="utf-8"><title>Local UE search results</title><style>body{font:16px system-ui;max-width:1100px;margin:30px auto;padding:20px;color:#193549;background:#f5f8fa}article{border:1px solid #ccd;background:white;padding:18px;margin:18px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}h3{font-size:1em}table{border-collapse:collapse;width:100%;background:white}td,th{text-align:left;padding:9px;border-bottom:1px solid #ccd}</style><h1>Local search results</h1><p>'+esc(result.get('completion','See search coverage'))+'</p><p>'+esc(result['interpretation'])+'</p><p>Study-level matching does not imply every contrast isolates the selected intervention. Read both arms and the model specification.</p><h2>Coverage and omissions</h2>'+coverage+'<details><summary>Search completeness and failures</summary><pre>'+esc({'search':result['search']['meta'],'scoped':result['scoped_discovery'],'failures':result['failures']})+'</pre></details><h2>Measurements</h2>'+cards+'<h2>Timing (seconds)</h2><pre>'+esc(result['timings'])+'</pre>'
