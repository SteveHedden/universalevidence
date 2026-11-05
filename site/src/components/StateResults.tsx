import {useId,useState,type ReactNode} from 'react';
import {InterventionResults,type ResultContext,type ResultRow} from './InterventionResults';
import type {StudyResult} from '../api/client';

export function stateRows(context:ResultContext|undefined,state:string) {
  return (context?.reviewed?.rows??[]).filter(r=>r.states?.includes(state));
}

function interventions(row:ResultRow):string[] {
  const tested=row.summary_mapping?.tested;
  return [...new Set([...(row.discovery_interventions??[]),...(row.result_interventions??[]),
    ...(Array.isArray(tested)?tested.filter((v):v is string=>typeof v==='string'):[])])];
}

export function StateResults({state,context,studies,children}:{state:string;context?:ResultContext;studies:StudyResult[];children:ReactNode}) {
  const [tab,setTab]=useState<'studies'|'results'>('studies');const id=useId();
  const rows=stateRows(context,state);
  const ids=new Set(rows.map(r=>r.study));
  const uris=[...new Set(rows.flatMap(interventions))];
  const labels=Object.assign({},...rows.map(r=>r.preferred_labels??{}),...studies.filter(s=>s.intervention_concept_uri&&s.intervention_concept).map(s=>({[s.intervention_concept_uri!]:s.intervention_concept!})));
  return <section className="intervention-results" aria-label="State evidence">
    <div role="tablist" aria-label="State evidence" onKeyDown={e=>{
      if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)) {e.preventDefault();const next=e.key==='Home'?'studies':e.key==='End'?'results':tab==='studies'?'results':'studies';setTab(next);document.getElementById(`${id}-${next}`)?.focus();}
    }}>
      {(['studies','results'] as const).map(t=><button key={t} id={`${id}-${t}`} role="tab" aria-controls={`${id}-${t}-panel`} aria-selected={tab===t} tabIndex={tab===t?0:-1} onClick={()=>setTab(t)}>{t==='studies'?'Studies':'Results'}</button>)}
    </div>
    <div role="tabpanel" id={`${id}-studies-panel`} aria-labelledby={`${id}-studies`} hidden={tab!=='studies'}>{children}</div>
    <div role="tabpanel" id={`${id}-results-panel`} aria-labelledby={`${id}-results`} hidden={tab!=='results'}>
      <p className="result-note">Results for this State, grouped by mapped intervention. Conditions studied and outcomes measured are separate relationships.</p>
      {context?.reviewed?.partial && <p className="result-note">Search or results coverage is partial. These findings cover returned studies only.</p>}
      {context?.reviewed?.status!=='available' ? <p>Results are unavailable for this search.</p> : !rows.length ? <p>No results available for this State in the returned studies.</p> : <>
        {uris.map(uri=><div key={uri}>
          <h3>{labels[uri]??'Intervention label unavailable'}</h3>
          <InterventionResults state={state} intervention={uri} context={context.context} active={tab==='results'} resultsOnly reviewed={{...context.reviewed!,rows:rows.filter(r=>interventions(r).includes(uri)),coverage:context.reviewed?.coverage?.filter(c=>[...ids].some(s=>s===c.study||s.endsWith('/'+c.study)))}}>{null}</InterventionResults>
        </div>)}
        {rows.some(r=>!interventions(r).length) && <p>{rows.filter(r=>!interventions(r).length).length} reported results have unresolved intervention attribution.</p>}
      </>}
    </div>
  </section>;
}
