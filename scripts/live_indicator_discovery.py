"""Live discovery through reviewed measure identities, never stored numeric results."""
import asyncio
import json
import re
from urllib.parse import unquote


def candidate_ids(query):
    from live_result_identity import identities
    from live_results import relevant
    # Additional axes need their own source-side conjunction; never ignore them.
    if not query.values.get('state') or any(query.values.get(k) for k in ('condition','outcome','intervention','region')):
        return []
    if getattr(query,'logic',{}).get('state','or') != 'or':
        return []
    mappings, definitions = identities()
    selected=set()
    for key, targets in mappings.items():
        if not key.startswith('ctgov-indicator:v1|') or len(targets)!=1:
            continue
        definition=definitions.get(str(next(iter(targets))))
        if not definition or not relevant({'states':definition.get('states',[])},query.values['state']):
            continue
        try:
            sid=json.loads(unquote(key.split('|',1)[1]))['record_id']
            if re.fullmatch(r'NCT[0-9]{8}',sid):selected.add(sid)
        except (ValueError,KeyError,TypeError):continue
    return sorted(selected)


async def discover(query,client=None):
    import httpx,time
    from live_result_rules import interpret_safely
    from live_results import relevant
    from query_runtime import REQUEST_DEADLINE,observe
    ids=await asyncio.to_thread(candidate_ids,query)
    if not ids:return [],[]
    if client is None:
        async with httpx.AsyncClient(timeout=4) as session:
            return await _fetch(query,ids,session)
    return await _fetch(query,ids,client)


async def _fetch(query,ids,client):
    import httpx,time
    from live_result_rules import interpret_safely
    from live_results import relevant
    from query_runtime import REQUEST_DEADLINE,observe
    failures=[];cap=400
    if len(ids)>cap:failures.append({'status':'indicator_candidate_cap','omitted':len(ids)-cap})
    ids=ids[:cap];sem=asyncio.Semaphore(3)
    completed_rows=[];finished=set()
    async def batch(selected):
        try:
            async with sem:
                response=await client.get('https://clinicaltrials.gov/api/v2/studies',params={'filter.ids':','.join(selected),'pageSize':len(selected),'format':'json'},timeout=4)
            response.raise_for_status();body=response.json()
            if not isinstance(body,dict) or not isinstance(body.get('studies'),list):raise ValueError('Invalid registry response')
            found={};duplicates=set()
            for raw in body['studies']:
                sid=raw.get('protocolSection',{}).get('identificationModule',{}).get('nctId')
                if sid not in selected:continue
                if sid in found:duplicates.add(sid)
                found[sid]=raw
            for sid in selected:
                if sid not in found or sid in duplicates:
                    failures.append({'study':sid,'status':'indicator_identity_missing_or_duplicate'});finished.add(sid);continue
                raw=found[sid];result=await asyncio.to_thread(interpret_safely,raw,{})
                finished.add(sid)
                if not any(relevant({'states':row.get('states',[])},query.values['state']) and row.get('value') is not None and row.get('comparison',{}).get('numerical_validation',{}).get('estimate',{}).get('status') == 'valid' for row in result.get('rows',[])):continue
                p=raw['protocolSection'];ident=p['identificationModule']
                completed_rows.append({'source':'CT.gov','study_id':sid,'study_identity':'https://universalevidence.com/studies/ctgov/'+sid,'title':ident.get('briefTitle') or ident.get('officialTitle') or sid,'url':'https://clinicaltrials.gov/study/'+sid,'status':p.get('statusModule',{}).get('overallStatus'),'api_results':result,'match_provenance':[{'basis':'reviewed-measured-outcome','registry_check':'current-exact-context'}]})
            return None
        except (httpx.HTTPError,ValueError,KeyError,TypeError,AttributeError) as exc:
            unfinished=set(selected)-finished
            failures.append({'status':'indicator_candidate_unavailable','error':type(exc).__name__,'omitted':len(unfinished)})
            finished.update(selected);return []
    deadline=REQUEST_DEADLINE.get();budget=min(6,max(0,deadline-time.monotonic()-.5)) if deadline else 6
    if budget<=0:return [],[{'status':'indicator_candidate_deadline','omitted':len(ids)}]
    tasks={asyncio.create_task(batch(ids[i:i+10])):ids[i:i+10] for i in range(0,len(ids),10)}
    try:
        done,pending=await asyncio.wait(tasks,timeout=budget)
        for task in done:
            task.result()  # Preserve unexpected failure propagation.
        if pending:failures.append({'status':'indicator_candidate_deadline','omitted':len(set(ids)-finished)})
        # Completed live interpretations survive a later sibling's timeout.
        # Task cancellation below cannot add rows after this returned snapshot.
        order={sid:i for i,sid in enumerate(ids)}
        return sorted(completed_rows,key=lambda r:order[r['study_id']]),failures
    finally:
        for task in tasks:
            if not task.done():observe(task,cancel=True)
