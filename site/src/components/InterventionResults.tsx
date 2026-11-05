import { reviewedEvidenceAvailable } from './reviewedAvailability';
import { ResultStudyDetails, type ResultCoverage } from './ResultStudyDetails';
import { Organizations } from './Organizations';
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { apiGet } from '../api/client';
import './InterventionResults.css';
import { summarizeFindings, groupFindingsByIndicator, summaryTerms, resultText } from './resultFindings';

type Details = Record<string, unknown>;
export type ResultRow = {
  measurement_conflict?: boolean;
  states?: string[];
  preferred_labels?: Record<string,string>; measure_definition?: string; scope_notes?: string[]; result_interventions?: string[];
  measurement: string; study: string; discovery_interventions?: string[]; indicator?: string; indicator_label: string;
  value?: string | null; unit?: string; direction: string; comparison?: Details;
  timing?: unknown; uncertainty?: unknown; subgroup?: unknown; derivation?: unknown;
  summary_mapping?: Details; finding_mapping?: Details; measure?: unknown; indicator_review?: unknown; interpretation?: string; intervention_scope?: string; result_text?: unknown;
  citations?: {url: string; citation: string}[]; source_details?: Details[];
};
export type ResultResponse = {
  completion: string; rows: ResultRow[];
  coverage: ResultCoverage[];
  study_statuses?: {study: string; status: string}[];
  timings: Record<string, number>; interpretation: string;
};
export type ResultContext = {state: string; context: string; reviewed?: {status: string; graph: string; rows: ResultRow[]; mode?: string; partial?: boolean; coverage?: ResultCoverage[]}};
export function InterventionResults({state, intervention, context, active, children, reviewed, resultsOnly=false}: ResultContext & {intervention: string; active: boolean; children: ReactNode; resultsOnly?: boolean}) {
  const id=useId(); const [tab,setTab]=useState<'studies'|'results'|'organizations'>('studies');
  const [data,setData]=useState<ResultResponse|null>(null); const [error,setError]=useState<string|null>(null);
  const [loading,setLoading]=useState(false); const [attempt,setAttempt]=useState(0);
  const request=useRef<{key:string; promise:Promise<ResultResponse>}|null>(null);
  const key=JSON.stringify([state,intervention,context,attempt]);
  useEffect(() => {
    if (!active || reviewed || !reviewedEvidenceAvailable(state, context)) return;
    let current=true; setLoading(true);setError(null);setData(null);
    if (request.current?.key!==key) {
      const params=new URLSearchParams({state,intervention,context});
      request.current={key,promise:apiGet<ResultResponse>(`/query/results?${params}`)};
    }
    request.current.promise.then(value => {if(current)setData(value);}).catch(e=>{if(current)setError(e instanceof Error?e.message:'Results unavailable');}).finally(()=>{if(current)setLoading(false);});
    return ()=>{current=false;};
  },[key,active,state,intervention,context,reviewed]);
  const summaries=summarizeFindings(reviewed?.rows ?? data?.rows ?? [],intervention);
  const indicators=groupFindingsByIndicator(summaries);
  const count=(n:number)=>`${n} ${n===1?'study':'studies'}`;
  const cards=<>{indicators.map(indicator=><details className="result-summary-card result-indicator" key={indicator.id}>
    <summary><strong>{indicator.label}</strong><span className="result-count">{count(indicator.studies)}</span>
      <span className="result-direction-preview">{indicator.groups.map(group=><span key={group.code}>{summaryTerms[group.code]} · {group.studies.length}</span>)}</span>
    </summary>
    {indicator.groups.map(group=><details className="result-direction" key={group.code}>
      <summary><strong>{summaryTerms[group.code]}</strong> · {count(group.studies.length)}</summary>
      {group.studies.map(study=><details className="result-supporting-study" key={study.id}>
        <summary>{study.id.split('/').pop()} · {study.findings.length} {study.findings.length===1?'finding':'findings'}</summary>
        {study.findings.map(f=><section className="result-estimate" key={f.key}>
          <p><strong>{summaryTerms[f.code]}</strong> · {f.type==='reported'?'Source-qualified finding; not a treatment-effect summary':f.type==='descriptive'?'Descriptive change; not a treatment effect':f.package?'Combined intervention comparison':'Direct comparison'}</p>
          <p className="result-context">{f.basis} · {f.time}</p>
          <p>{f.detail}</p>
          <details><summary>Values, assessments and sources</summary>
            {f.rows.map((r,index)=><p key={r.measurement+index}>{r.value ?? 'Unavailable'} {r.unit} · {resultText(r.comparison?.context && (r.comparison.context as Details).class || r.timing)} · <code>{r.measurement}</code></p>)}
            {f.alternatives?.map(a=><p key={a.key}>{summaryTerms[a.code]} · {a.time}: {a.detail}</p>)}
            {f.rows.flatMap(r=>r.citations??[]).filter((c,i,all)=>all.findIndex(x=>x.url===c.url)===i).map(c=><a key={c.url} href={c.url} target="_blank" rel="noreferrer">View source ↗ </a>)}
          </details>
        </section>)}
      </details>)}
    </details>)}
  </details>)}</>;
  return <section className="intervention-results" hidden={!active} aria-label="Intervention results">
    {!resultsOnly && <div role="tablist" aria-label="Intervention evidence" onKeyDown={e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const tabs=['studies','results','organizations'] as const;const next=e.key==='Home'?'studies':e.key==='End'?'organizations':tabs[(tabs.indexOf(tab)+(e.key==='ArrowRight'?1:2))%3];setTab(next);document.getElementById(`${id}-${next}`)?.focus();}}}>
      {(['studies','results','organizations'] as const).map(name=><button key={name} role="tab" id={`${id}-${name}`} aria-controls={`${id}-${name}-panel`} aria-selected={tab===name} tabIndex={tab===name?0:-1} onClick={()=>setTab(name)}>{name==='studies'?'Studies':name==='results'?'Results':'Organizations'}</button>)}
    </div>}
    {!resultsOnly && <div role="tabpanel" id={`${id}-studies-panel`} aria-labelledby={`${id}-studies`} hidden={tab!=='studies'}>{children}</div>}
    <div role={resultsOnly?undefined:'tabpanel'} id={`${id}-results-panel`} aria-labelledby={resultsOnly?undefined:`${id}-results`} hidden={!resultsOnly && tab!=='results'}>
    {!reviewedEvidenceAvailable(state, context) ? <p>Results are not available here yet.</p> : <div className="result-overview" aria-live="polite">
      <h3>Results</h3>
      {loading && <p role="status">Loading results…</p>}
      {error && <p role="alert">Results unavailable: {error} <button onClick={()=>setAttempt(n=>n+1)}>Retry results</button></p>}
      {reviewed && <p className="result-note">{reviewed.status==='available'?(reviewed.mode==='api-first'?'Results from returned source records · included in this search.':'Reviewed result snapshot · included in this search.'):reviewed.status==='unsupported_filters'?'Reviewed results are not yet indexed for these additional filters.':'Reviewed results are temporarily unavailable.'}</p>}
      {(data || reviewed?.status==='available') && <>
        {cards}
        {indicators.length>0 && <p className="result-note">Counts are distinct studies per indicator, not pooled effects. Mixed findings means a study has different directions or interpretations across its findings.</p>}
        {reviewed?.mode==='api-first' && <>
          {reviewed.partial && <p className="result-note">Search coverage is partial or capped. These results cover only the returned studies.</p>}
          <details><summary>Source results coverage</summary>
            {reviewed.coverage?.map(c=><p key={c.source+c.study}>{c.study}: {c.status.replaceAll('_',' ')}{c.reason ? ` · ${c.reason}` : ''}</p>)}
          </details>

        </>}
        <details className="result-all-measurements"><summary>All source measurements</summary><ResultStudyDetails rows={reviewed?.rows ?? data?.rows ?? []} coverage={reviewed?.coverage ?? data?.coverage ?? []} intervention={intervention}/></details>
        {!summaries.length && <p>{summaryTerms.no_results} for a supported summary in the returned search scope.</p>}
        {summaries.length>0 && summaries.every(s=>s.package) && <p className="result-note">Available results test combined interventions; the effect of this component alone is not established.</p>}
        {data?.completion==='partial' && <p className="result-note">Some sources are unavailable. Results shown cover the reviewed evidence returned.</p>}
      </>}
    </div>}
    </div>
    {!resultsOnly && <div role="tabpanel" id={`${id}-organizations-panel`} aria-labelledby={`${id}-organizations`} hidden={tab!=='organizations'}>
      <Organizations state={state} intervention={intervention} context={context} active={active && tab==='organizations'}/>
    </div>}
  </section>;
}
