"""Combine transient API results with unchanged non-API compact evidence.

Reviewed non-API attribution can discover studies in supported State/intervention
queries. Only live-discovered API studies contribute. The former compact API-study catalogue
is never queried for API evidence and never adds API study cards.
"""
import copy
import json
from live_result_rules import VERSION, PREFIX
from compact_results import read_compact, enabled, merge, record_key, supported
from result_routing import local_eligible


def local_only(compact, payload=None, *, reviewed_discovery=False):
    studies=[]
    api_checks = {}
    if payload is not None:
        for row in payload.get('results', []):
            source, sid = record_key(row.get('source'), row.get('study_id'))
            if source == 'ctgov':
                allowed = row.get('api_results', {}).get('publication_fallback_eligible') is True
                api_checks[sid] = api_checks.get(sid, True) and allowed
    for study in compact.get('studies', []):
        if not local_eligible({**study, 'api_result_checks': api_checks} if payload is not None else study):
            continue
        if payload is not None:
            keys={record_key(r['source'],r['record_id']) for r in study.get('registry_records', [])}
            keys.add(record_key(study.get('source'), study.get('study_id')))
            matched=[s for s in payload.get('results', []) if
                     s.get('study_identity') == study.get('study_identity') or
                     record_key(s.get('source'),s.get('study_id')) in keys or
                     any(record_key(r['source'],r['record_id']) in keys for r in s.get('registry_records', []))]
            if not matched:
                # Reviewed result attribution is a discovery route for non-API
                # registries: their literal intervention searches are incomplete.
                # CT.gov (including aliases) still requires a current API check.
                records = [study, *study.get('registry_records', []), *study.get('records', [])]
                has_api_alias = any(record_key(r.get('source'), r.get('study_id', r.get('record_id')))[0] == 'ctgov' for r in records)
                if not reviewed_discovery or has_api_alias:
                    continue
            # Live identity evidence can add an API alias absent from the release.
            merged_records=list(study.get('registry_records', []))
            for row in matched:
                merged_records.extend(row.get('registry_records', []))
                merged_records.extend(row.get('records', []))
                merged_records.append({'source':row.get('source'), 'record_id':row.get('study_id')})
            if not local_eligible({**study,'registry_records':merged_records, 'api_result_checks': api_checks}):
                continue
        studies.append(study)
    identities={s['study_identity'] for s in studies}
    return {**compact,'studies':studies,'rows':[r for r in compact.get('rows',[]) if r['study'] in identities]}


def coalesce_measurements(rows):
    """Merge projection-only repeats; quarantine conflicting globally named results.

    Study is part of the key. States are the sole permitted projection difference.
    Conflicts carry their complete original payloads to every affected State view,
    so filtering cannot hide one side and manufacture a supported summary.
    """
    identities = {}
    measurement_studies = {}
    for row in rows:
        key = (row['study'], row['measurement'])
        payload = json.dumps({k: v for k, v in row.items() if k != 'states'}, sort_keys=True)
        variants = identities.setdefault(key, {})
        if payload in variants:
            variants[payload]['states'] = sorted(set(variants[payload].get('states', [])) | set(row.get('states', [])))
        else:
            variants[payload] = copy.deepcopy(row)
        measurement_studies.setdefault(row['measurement'], set()).add(row['study'])
    output, conflicts = [], []
    for (study, measurement), variants in identities.items():
        if len(variants) > 1 or len(measurement_studies[measurement]) > 1 or any(r.get('measurement_conflict') for r in variants.values()):
            states = sorted({state for row in variants.values() for state in row.get('states', [])})
            for row in variants.values():
                row['states'] = states
                row['measurement_conflict'] = True
            conflicts.append({'study': study, 'source': 'results', 'measurement': measurement,
                              'status': 'conflicting_measurement',
                              'reason': 'Conflicting measurement payload or study identity; no directional interpretation is supported.'})
        output.extend(variants.values())
    return output, conflicts


def assemble(payload, query, local=None):
    # Only the supported, graph-filtered State/intervention scope may discover
    # historical studies. A geography/status/other filter must never be ignored.
    discover = bool(local and local.get('status') == 'available' and supported(query)
                    and query.values.get('state'))
    scoped=local_only(local,payload,reviewed_discovery=discover) if local else None
    if discover and not query.values.get('intervention'):
        # A measured outcome can discover a non-API study without claiming an
        # intervention effect. Eligibility above still checks every API alias.
        payload=copy.deepcopy(payload)
        present={s.get('study_identity') for s in payload.get('results',[])}
        present_keys={record_key(s.get('source'),s.get('study_id')) for s in payload.get('results',[])}
        for study in scoped.get('studies',[]):
            if study['study_identity'] not in present and record_key(study.get('source'),study.get('study_id')) not in present_keys:
                payload.setdefault('results',[]).append({**study,'match_provenance':[{'basis':'reviewed-measured-outcome'}]})
    out=merge(payload,scoped) if local else copy.deepcopy(payload)
    local_status=local.get('status') if local else 'not_configured'
    rows=copy.deepcopy(out.get('reviewed_results',{}).get('rows',[]))
    # Compact evidence has already been scoped by its query; retain that contract.
    local_measurements = {(row['study'], row['measurement']) for row in rows}
    coverage=[];seen=set()
    discovery_interventions={}
    for study in out.get('results',[]):
        key=record_key(study.get('source'),study.get('study_id'))
        if study.get('intervention_concept_uri'):
            discovery_interventions.setdefault(key,set()).add(study['intervention_concept_uri'])
    for study in out.get('results',[]):
        source,sid=record_key(study.get('source'),study.get('study_id'))
        if source not in ('ctgov','isrctn'):
            study.pop('api_results',None)
            continue
        identity=study.get('study_identity')
        if not isinstance(identity,str) or not identity.startswith(PREFIX+'studies/'):
            identity=PREFIX+'studies/'+source+'/'+sid
        study['study_identity']=identity
        result=study.pop('api_results',None)
        duplicate=(source,sid) in seen
        seen.add((source,sid))
        if source=='isrctn':
            coverage.append({'study':sid,'source':source,'status':'no_supported_api_numerical_results',
                             'reason':'Returned planned outcomes and report links are not numerical results'})
            continue
        if result is None:
            coverage.append({'study':sid,'source':source,'status':'unavailable','reason':'Result fields were not interpreted'})
            continue
        if not duplicate:coverage.append({'study':sid,'source':source,'title':study.get('title'), 'discovery_interventions':sorted(discovery_interventions.get((source,sid),())), **{k:v for k,v in result.items() if k!='rows'}})
        # Collect every returned branch before any relevance filtering.
        for row in result.get('rows', []):
            projected={**row,'study':identity,'discovery_interventions':sorted(discovery_interventions.get((source,sid),()))}
            if row.get('summary_mapping'):projected['summary_mapping']={**row['summary_mapping'],'study':identity}
            rows.append(projected)
    # Detect conflicts globally before State/intervention views can hide a variant.
    rows, conflicts = coalesce_measurements(rows)
    coverage.extend(conflicts)
    targets=query.values.get('state',()) or query.values.get('outcome',()) or query.values.get('condition',())
    if targets:
        rows = [row for row in rows
                if (row['study'], row['measurement']) in local_measurements or relevant(row, targets)]
    from live_result_identity import preferred_labels
    for row in rows:
        mapping = row.get('finding_mapping') or row.get('summary_mapping') or {}
        concepts = set(row.get('result_interventions', []))
        for field in ('tested', 'comparator', 'background'):
            concepts.update(mapping.get(field, []))
        row['preferred_labels'] = preferred_labels(concepts)
    # Grouping reflects all supported roles; a measured State is never relabeled
    # as an enrollment condition. No additional studies are discovered here.
    from query_v2 import _local_state_labels
    state_uris={s for row in rows for s in row.get('states',[])}
    state_labels=_local_state_labels(sorted(state_uris)) if state_uris else {}
    rows_by_study={}
    for row in rows:
        rows_by_study.setdefault(row['study'],[]).append(row)
    for study in out.get('results',[]):
        mappings=list(study.get('state_mappings') or [])
        if study.get('condition_concept_uri'):
            mappings.append({'uri':study['condition_concept_uri'],'label':study.get('condition_concept') or study['condition_concept_uri'],'role':'condition'})
        for outcome in study.get('outcomes') or []:
            if isinstance(outcome,dict) and outcome.get('state_concept_uri'):
                mappings.append({'uri':outcome['state_concept_uri'],'label':outcome.get('state_concept') or outcome['state_concept_uri'],'role':'outcome'})
        for row in rows_by_study.get(study.get('study_identity'),[]):
            for state in row.get('states',[]):
                mappings.append({'uri':state,'label':state_labels.get(state) or state,'role':'result'})
        study['state_mappings']=list({(m['uri'],m['role']):m for m in mappings}.values())
    partial=bool(conflicts) or local_status in ('unavailable','unsupported_filters') or out.get('meta',{}).get('truncated',False) or out.get('meta',{}).get('execution_status') in ('partial','timeout')
    out['reviewed_results']={'status':'available','graph':VERSION,'mode':'api-first',
                             'rows':rows,'coverage':coverage,'partial':bool(partial),
                             'local_evidence_status':local_status,
                             'scope':'Results cover live registry matches and reviewed result attribution in supported State/intervention searches. CT.gov publication fallback requires current API absence checks.'}
    out.setdefault('meta',{})['reviewed_results']={'status':'available','mode':'api-first','version':VERSION,
        'studies_with_results':len({r['study'] for r in rows if r.get('value') is not None}),
        'coverage':coverage,'partial':bool(partial)}
    return out


def relevant(row,targets):
    if set(row.get("states",[])).intersection(targets):return True
    from query_v2 import _local_taxonomy_graph
    from rdflib import URIRef
    from rdflib.namespace import SKOS
    graph=_local_taxonomy_graph('states.ttl')
    # Reviewed indicator applicability is narrower than its broad taxonomy family.
    # Only a study already admitted by this query may use this relevance bridge.
    specific={
        PREFIX+'vocab/indicators/BloodPressureMeasurement': ('Hypertension','IsolatedSystolicHypertension'),
        PREFIX+'vocab/indicators/GlycemicControl': ('Diabetes','DiabetesMellitus','DiabetesGestational'),
    }.get(row.get('indicator'),())
    roots={URIRef(PREFIX+'vocab/states/'+s) for s in specific}
    for target in targets:
        pending=[URIRef(target)];visited=set()
        while pending:
            node=pending.pop()
            if node in roots:return True
            if node in visited:continue
            visited.add(node);pending.extend(graph.objects(node,SKOS.broader));pending.extend(graph.subjects(SKOS.narrower,node))
    pending=[URIRef(s) for s in row.get('states',[])];seen=set()
    while pending:
        node=pending.pop()
        if str(node) in targets:return True
        if node in seen:continue
        seen.add(node);pending.extend(graph.objects(node,SKOS.broader));pending.extend(graph.subjects(SKOS.narrower,node))
    return False


async def reviewed_api_candidates(query, local, client=None):
    """Check graph-scoped report candidates against current CT.gov before admission.

    Only explicit absence admits a publication fallback. API results, errors,
    wrong identities and unsupported filters never enable historical paper rows.
    """
    import asyncio, re, httpx
    from datetime import datetime, timezone
    from result_routing import no_posted_results
    if not local or local.get('status')!='available' or not supported(query) or not query.values.get('state'):
        return [], []
    candidates={}; ambiguous=set()
    for study in local.get('studies',[]):
        for record in [study,*study.get('registry_records',[])]:
            source,sid=record_key(record.get('source'),record.get('study_id',record.get('record_id')))
            if source=='ctgov' and re.fullmatch(r'NCT[0-9]{8}',sid):
                if sid in candidates and candidates[sid].get('study_identity')!=study.get('study_identity'):
                    ambiguous.add(sid)
                else:candidates[sid]=study
    # Prefer reviewed intervention comparisons over unassigned raw measurements.
    # Check every candidate in bounded concurrent exact-ID batches.
    attributed={r['study'] for r in local.get('rows',[]) if (r.get('finding_mapping') or r.get('summary_mapping') or {}).get('tested')}
    ids=sorted(set(candidates)-ambiguous,key=lambda sid:(candidates[sid].get('study_identity') not in attributed,sid))
    failures=[{'study':sid,'status':'ambiguous_reviewed_identity'} for sid in sorted(ambiguous)]
    batches=[ids[i:i+50] for i in range(0,len(ids),50)]
    if client is None:
        async with httpx.AsyncClient() as owned:
            return await reviewed_api_candidates(query,local,owned)
    if not ids:return [],failures
    semaphore=asyncio.Semaphore(3)
    async def fetch_batch(selected):
        try:
            # Exact-ID batch avoids twenty competing registry requests. Each
            # returned identity still needs its own explicit no-results check.
            async with semaphore:
                response=await client.get('https://clinicaltrials.gov/api/v2/studies',
                    params={'filter.ids':','.join(selected),'pageSize':len(selected),'format':'json'},timeout=4)
            response.raise_for_status();body=response.json()
            if not isinstance(body,dict) or not isinstance(body.get('studies'),list):raise ValueError('Invalid registry batch')
            by_id={};duplicates=set()
            for raw in body['studies']:
                if not isinstance(raw,dict):continue
                sid=raw.get('protocolSection',{}).get('identificationModule',{}).get('nctId')
                if sid not in selected:continue
                if sid in by_id:duplicates.add(sid)
                by_id[sid]=raw
            rows=[]
            for sid in selected:
                if sid not in by_id or sid in duplicates:
                    failures.append({'study':sid,'status':'registry_identity_missing_or_duplicate'});continue
                raw=by_id[sid]
                if not no_posted_results(raw):continue
                protocol=raw['protocolSection'];identity=protocol['identificationModule']
                rows.append({**candidates[sid],'source':'CT.gov','study_id':sid,
                    'title':identity.get('briefTitle') or identity.get('officialTitle') or candidates[sid]['title'],
                    'url':'https://clinicaltrials.gov/study/'+sid,
                    'status':protocol.get('statusModule',{}).get('overallStatus'),
                    'api_results':{'publication_fallback_eligible':True,'rows':[],
                      'status':'no_api_numerical_results','retrieved_at':datetime.now(timezone.utc).isoformat()},
                    'match_provenance':[{'basis':'reviewed-result','registry_check':'no-posted-results'}]})
            return rows
        except (httpx.HTTPError,ValueError,KeyError,TypeError,AttributeError) as exc:
            failures.append({'status':'registry_check_unavailable','error':type(exc).__name__,
                'omitted':len(selected),'http_status':exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else None})
            return []
    import time
    from query_runtime import REQUEST_DEADLINE, observe
    deadline=REQUEST_DEADLINE.get()
    budget=min(4.5,max(0,deadline-time.monotonic()-.5)) if deadline else 4.5
    if budget<=0:return [],failures+[{'status':'candidate_deadline','omitted':len(ids)}]
    tasks={asyncio.create_task(fetch_batch(batch)):batch for batch in batches}
    try:
        done,pending=await asyncio.wait(tasks,timeout=budget)
        if pending:failures.append({'status':'candidate_deadline','omitted':sum(len(tasks[t]) for t in pending)})
        return [row for task in tasks if task in done for row in task.result()],failures
    finally:
        for task in tasks:
            if not task.done():observe(task,cancel=True)

async def compact_with_candidates(query):
    local=await read_compact(query)
    candidates,failures=await reviewed_api_candidates(query,local)
    return local,candidates,failures


async def combine(live,query):
    import asyncio
    if enabled(query):
        from live_indicator_discovery import discover
        payload,(local,candidates,failures),(indicator_candidates,indicator_failures)=await asyncio.gather(live,compact_with_candidates(query),discover(query))
        candidates=[*candidates,*indicator_candidates]
        failures=[*failures,*indicator_failures]
        # Existing live responses take precedence, including posted-results/error states.
        existing={record_key(s.get('source'),s.get('study_id')) for s in payload.get('results',[])}
        additional=[]
        for candidate in candidates:
            key=record_key(candidate['source'],candidate['study_id'])
            if key not in existing:
                additional.append(candidate)
                existing.add(key)
        payload={**payload,'results':[*payload.get('results',[]),*additional],'meta':dict(payload.get('meta',{}))}
        if failures:
            payload['meta']['reviewed_candidate_checks']=failures
            payload['meta']['truncated']=True
    else:
        payload,local=await live,None
    # Result projection must not consume the response-encoding reserve or erase
    # healthy study rows when a different source consumed the search deadline.
    import time
    from query_runtime import REQUEST_DEADLINE
    deadline=REQUEST_DEADLINE.get()
    remaining=deadline-time.monotonic()-0.04 if deadline else 5.0
    if remaining>0:
        try:
            return await asyncio.wait_for(asyncio.to_thread(assemble,payload,query,local),remaining)
        except asyncio.TimeoutError:
            pass
    out={**payload,'results':[{k:v for k,v in r.items() if k!='api_results'} for r in payload.get('results',[])],
         'meta':dict(payload.get('meta',{}))}
    out['reviewed_results']={'status':'unavailable','mode':'api-first','graph':VERSION,'rows':[],
                             'coverage':[],'partial':True,'reason':'Result projection deadline reached; study results preserved'}
    out['meta']['reviewed_results']={'status':'unavailable','reason':'result_projection_deadline'}
    return out


def rows_to_rdf(rows):
    import json
    from rdflib import Graph,URIRef,Literal,RDF,Namespace,XSD
    from rdflib.namespace import DCTERMS
    from decimal import Decimal
    ue=Namespace('https://universalevidence.com/ontology/')
    graph=Graph()
    for row in rows:
        m=URIRef(row['measurement']);graph.add((m,RDF.type,ue.OutcomeMeasurement))
        graph.add((m,ue.measurementStudy,URIRef(row['study'])))
        if row.get('indicator'):graph.add((m,ue.ofIndicator,URIRef(row['indicator'])))
        if row.get('value') is not None:graph.add((m,ue.value,Literal(Decimal(row['value']),datatype=XSD.decimal)))
        if row.get('unit'):graph.add((m,ue.unit,Literal(row['unit'])))
        for state_uri in row.get('states',[]):graph.add((m,ue.ofState,URIRef(state_uri)))
        for citation in row.get('citations',[]):graph.add((m,DCTERMS.source,URIRef(citation['url'])))
        # Preserve comparison scope, uncertainty and source fingerprint in the existing profile.
        comparison = dict(row.get('comparison') or {})
        if row.get('measurement_conflict'):
            comparison['measurement_conflict'] = True
        graph.add((m,ue.comparison,Literal(json.dumps(comparison))))
        graph.add((m,ue.uncertainty,Literal(json.dumps(row.get('uncertainty',{})))))
        graph.add((m,ue.timing,Literal(json.dumps(row.get('timing')))))
        for detail in row.get('source_details',[]):
            locator=URIRef(detail.get('url','https://clinicaltrials.gov')+'#'+detail.get('sha256','')+detail.get('locator',''))
            graph.add((m,ue.sourceLocator,locator))
            if detail.get('url'):graph.add((locator,DCTERMS.source,URIRef(detail['url'])))
    return graph.serialize(format='turtle')
