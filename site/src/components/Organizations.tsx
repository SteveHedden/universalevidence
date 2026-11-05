import { reviewedEvidenceAvailable } from './reviewedAvailability';
import { useEffect, useState } from 'react';
import { resolveApiUrl } from '../api/client';
import { studyDates, studyStatus } from './organizationMetadata';

export type Project = {
  participants?: {names:string[];ref:string|null;role:string;identity_status:string}[];
  reporting_organizations?: {names:string[];ref?:string}[];
  id: string; title: string; source_url: string; project_url: string | null;
  countries: {code: string; name: string}[]; retrieved_at: string;
  freshness: {status: string; issues: string[]; dates: {'@type': string; '@iso-date': string}[]; updated_at: string | null};
  matches: Record<string, {support: {text?: string; kind: string}[]}>;
};
type StudyRole = {name:string; organization_id:string|null; identity_status:string; role:string;
  scope:{kind:string;id:string}; evidence:{url:string;locator:string;quote?:string}[]};
type StudyProject = {id:string; title:string; source:string; study_id:string; source_url:string;
  source_date:string|null; reviewed_at:string; status:string; geography:string[];
  aliases:{source:string;study_id:string;url?:string}[]; dates:{label:string;value:string}[];
  match_reasons:string[]; contexts:{id:string;components:{uri:string;component_id:string;label:string}[]}[];
  implementers:StudyRole[]; other_roles:StudyRole[]; missing_role:string|null; gaps:string[]};
export type StudyProjectsResponse = {status:string; projects:StudyProject[]; count:number; scope:string; related_projects?:StudyProject[]; related_count?:number;
  freshness:string; version?:string; stale_source_count?:number; reviewed_at?:string; errors:string[]};
export type OrganizationResponse = {
  study_projects?: StudyProjectsResponse;
  mode?: string; projects?: Project[]; returned_project_count?: number; matched_project_count?: number;
  status: string; organizations: {id: string; name: string; relevance: string; representative: Project; activities: Project[]}[];
  unresolved_participants: unknown[]; missing_dimensions: string[]; errors: string[];
  coverage: {truncated: boolean; card_limit_reached?: boolean; project_limit_reached?: boolean}; retrieved_at: string;
  candidate_mapping_review: boolean;
};
const dateLabels: Record<string,string> = {'1':'Planned start','2':'Actual start','3':'Planned end','4':'Actual end'};
function safeLink(url: string | null) { return url && /^https?:\/\//i.test(url) ? url : undefined; }

type OrganizationProps = {state: string; intervention: string; context: string; active: boolean};
export function Organizations(props: OrganizationProps) {
  if (!reviewedEvidenceAvailable(props.state, props.context)) {
    return <p>Organization search is currently in development.</p>;
  }
  return <AvailableOrganizations {...props}/>;
}
function AvailableOrganizations({state, intervention, context, active}: OrganizationProps) {
  const key=JSON.stringify([state,intervention,context]);
  const [response,setResponse]=useState<{key:string;value:OrganizationResponse}|null>(null);
  const [failure,setFailure]=useState<{key:string;message:string}|null>(null);
  const [attempt,setAttempt]=useState(0);
  let multipleStates=false;
  try { multipleStates=(JSON.parse(context).values?.state?.length ?? 0)>1; } catch { /* Opaque search context is still included in cache identity. */ }
  useEffect(()=>{
    if (!active || multipleStates) return;
    let current=true;
    const controller=new AbortController();
    const started=performance.now();
    setResponse(null);setFailure(null);
    const timer=setTimeout(()=>controller.abort(),9500);
    const params=new URLSearchParams({state,intervention,context});
    // No generic apiGet retry: one tab request must not silently double its budget.
    fetch(resolveApiUrl(`/query/organizations?${params}`),{signal:controller.signal,headers:{Accept:'application/json'}})
      .then(async r=>{if(!r.ok)throw new Error('Organizations could not be retrieved.');return await r.json() as OrganizationResponse;})
      .then(value=>{if(current){setResponse({key,value});performance.measure('organizations-tab-response',{start:started,end:performance.now()});}})
      .catch(()=>{if(current)setFailure({key,message:controller.signal.aborted?'Organization retrieval reached its time limit.':'Organizations could not be retrieved.'});})
      .finally(()=>clearTimeout(timer));
    return ()=>{current=false;clearTimeout(timer);controller.abort();};
  },[key,state,intervention,context,active,attempt,multipleStates]);
  const data=response?.key===key?response.value:null;
  const error=failure?.key===key?failure.message:null;
  if(multipleStates)return <p>Select a single condition to find organizations. Multiple-condition organization matching is not yet supported.</p>;
  if(!active)return null;
  if(data?.mode==='project-mentions')return <ProjectMatches data={data} retry={()=>setAttempt(n=>n+1)}/>;
  return <div className="organizations-overview" aria-live="polite">
    {data?.study_projects && <StudyProjects data={data.study_projects}/>}
    <h3>IATI organizations</h3>
    <p className="result-note">Organizations reporting matching projects in IATI. This is a limited discovery list, not a ranking or evidence of effectiveness. Other study filters are not applied.</p>
    {!data && !error && <p role="status">Finding projects and organizations…</p>}
    {error && <p role="alert">{error} <button onClick={()=>setAttempt(n=>n+1)}>Retry organizations</button></p>}
    {data && <>
      {data.candidate_mapping_review && <p role="status">Local demonstration using candidate mappings awaiting review.</p>}
      {data.missing_dimensions.length>0 && <p>Matching only the available context; {data.missing_dimensions.join(' and ')} not specified.</p>}
      {data.status==='missing-mappings' && <p>Approved IATI mappings are not available for this selection yet.</p>}
      {data.status==='missing-context' && <p>Select a condition or intervention to find organizations.</p>}
      {data.status==='empty' && <p>No matching activities were returned by this bounded IATI search.</p>}
      {data.status==='insufficient-matches' && <p>Returned activities did not identify sufficiently specific matches with named, identified implementers.</p>}
      {['error','busy','partial'].includes(data.status) && <p role="status">{data.status==='partial'?'Some activities or source requests could not be processed. Available matches are shown.':'IATI retrieval is unavailable or reached a limit.'} <button onClick={()=>setAttempt(n=>n+1)}>Retry organizations</button></p>}
      {data.coverage.truncated && <p>Source retrieval was partial or capped; additional organizations may be missing.</p>}
      {data.coverage.card_limit_reached && <p>Showing five organizations from the returned activities.</p>}
      {data.organizations.some(o=>o.relevance==='candidate') && <h4>Relevant candidates — condition relevance unconfirmed</h4>}
      {data.organizations.map(org=>{
        const p=org.representative;
        const evidence=Object.values(p.matches).flatMap(m=>m.support).filter(s=>s.text).filter((s,index,all)=>all.findIndex(other=>other.text===s.text)===index).slice(0,2);
        return <article className="organization-card" key={org.id}>
          <h4>{org.name}</h4>
          <p><strong>{p.title}</strong></p>
          <p>Project countries: {p.countries.map(c=>c.name).join(', ') || 'Not reported'}</p>
          <p>{org.relevance==='candidate'?'Listed implementer of a project with explicit intervention support; relevance to the condition is unconfirmed.':data.missing_dimensions.length?'Listed implementer of a project matching the available search context.':'Listed implementer of a project that includes the intervention and addresses the condition.'}</p>
          {evidence.map((s,i)=><blockquote key={i}>{s.text}</blockquote>)}
          <p className="result-note">{p.freshness.status==='consistent-current'?'Published dates and status are consistent with current work; operation is not independently verified.':'Current operation is unverified.'}</p>
          <details><summary>Project dates and source details{org.activities.length>1?` · ${org.activities.length} matching reports`:''}</summary>
            {org.activities.map((a,i)=><div key={a.id+String(i)}>
              <p>{a.title}</p>
              {a.freshness.dates.map((d,j)=><p key={j}>{dateLabels[d['@type']] ?? 'Date'}: {d['@iso-date'] || 'Not reported'}</p>)}
              <p>Source updated: {a.freshness.updated_at || 'Not reported'}</p>
              <p>Retrieved: {a.retrieved_at}</p>
              {a.freshness.issues.length>0 && <p>Uncertainty: {a.freshness.issues.map(x=>x.replaceAll('-',' ')).join('; ')}</p>}
              <a href={safeLink(a.source_url)} target="_blank" rel="noreferrer">View IATI activity in d-portal ↗</a>
            </div>)}
          </details>
          {safeLink(p.project_url) && <a href={safeLink(p.project_url)} target="_blank" rel="noreferrer">Publisher’s project website ↗ </a>}
          <a href={safeLink(p.source_url)} target="_blank" rel="noreferrer">View IATI activity in d-portal ↗</a>
        </article>;
      })}
      {data.unresolved_participants.length>0 && <p className="result-note">Additional named activity participants lack resolved identifiers and are excluded from organization cards and counts. Names alone cannot establish distinct organizations.</p>}
      <p className="result-note">Retrieved {data.retrieved_at}. Project participation does not establish which partner delivers each component.</p>
    </>}
  </div>;
}


function ProjectMatches({data,retry}:{data:OrganizationResponse;retry:()=>void}) {
  return <div className="organizations-overview" aria-live="polite">
    {data.study_projects && <StudyProjects data={data.study_projects}/>}
    <h3>Projects and reported organizations</h3>
    <p>IATI projects</p>
    <p className="result-note">Projects matching the selected source terms or reviewed codes. Matches do not establish delivery of an intervention, effectiveness, or which partner delivers it. Other study filters are not applied.</p>
    {data.missing_dimensions.length>0 && <p>Matching the selected {data.missing_dimensions.includes('condition')?'intervention':'condition'} only.</p>}
    {data.status==='missing-mappings' && <p>Approved IATI search mappings are not available for this selection yet.</p>}
    {data.status==='missing-context' && <p>Select a condition or intervention to find projects.</p>}
    {data.status==='unsupported' && <p>IATI does not yet support querying the reviewed mappings for this selection.</p>}
    {data.status==='empty' && <p>No projects were returned by this bounded IATI search.</p>}
    {data.status==='insufficient-matches' && <p>No returned projects passed the source-match and project-status checks.</p>}
    {['error','busy','partial'].includes(data.status) && <p role="status">{data.status==='partial'?'Some records could not be processed. Available projects are shown.':'IATI retrieval is unavailable or reached a limit.'} <button onClick={retry}>Retry organizations</button></p>}
    {data.coverage.truncated && <p>Source retrieval was partial or capped; additional projects may be missing.</p>}
    {data.coverage.project_limit_reached && <p>Showing five projects from the matching returned records.</p>}
    {(data.projects??[]).map(p=><article className="organization-card" key={p.id}>
      <h4>{p.title}</h4>
      <p>Project countries: {p.countries.map(c=>c.name).join(', ') || 'Not reported'}</p>
      <p><strong>Reported organizations</strong></p>
      <ul>
        {(p.reporting_organizations??[]).map((o,i)=><li key={'publisher'+i}>Reporting organization: {o.names.join('; ') || o.ref || 'Not reported'}{o.ref?` (${o.ref})`:' — identifier not reported'}</li>)}
        {(p.participants??[]).map((o,i)=><li key={i}>{({implementing:'Implementer',funding:'Funder',accountable:'Accountable organization',extending:'Extending organization'} as Record<string,string>)[o.role] ?? 'Participant (role not specified)'}: {o.names.join('; ') || o.ref || 'Name not reported'}{o.ref?` (${o.ref})`:' — identifier unavailable; name as reported'}</li>)}
      </ul>
      {!(p.participants??[]).some(o=>o.role==='implementing') && <p>Implementer not reported.</p>}
      {Object.entries(p.matches).map(([axis,m])=><div key={axis}>
        <p><strong>{axis==='state'?'Condition':'Intervention'} source match</strong></p>
        {m.support.filter(s=>s.text).filter((s,i,all)=>all.findIndex(other=>other.text===s.text)===i).slice(0,2).map((s,i)=><blockquote key={i}>{s.text}</blockquote>)}
      </div>)}
      <p className="result-note">{p.freshness.status==='consistent-current'?'Published dates and status are consistent with current work; operation is not independently verified.':'Current operation is unverified.'}</p>
      <details><summary>Project dates and source details</summary>
        {p.freshness.dates.map((d,i)=><p key={i}>{dateLabels[d['@type']]??'Date'}: {d['@iso-date']||'Not reported'}</p>)}
        <p>Source updated: {p.freshness.updated_at || 'Not reported'}</p>
        <p>Activity ID: {p.id}</p><p>Retrieved: {p.retrieved_at}</p>
      </details>
      {safeLink(p.project_url) && <a href={safeLink(p.project_url)} target="_blank" rel="noreferrer">Publisher’s project website ↗ </a>}
      <a href={safeLink(p.source_url)} target="_blank" rel="noreferrer">View IATI activity in d-portal ↗</a>
    </article>)}
    <p className="result-note">Retrieved {data.retrieved_at}. Organization names without identifiers are displayed only within their source project; they are not merged or counted as distinct organizations.</p>
  </div>;
}


function StudyProjects({data,broader=false}:{data:StudyProjectsResponse;broader?:boolean}) {
  const roleLabel=(r:StudyRole)=>r.role==='implementer' && r.identity_status!=='verified'
    ? 'Delivery reported; organization identity unresolved' : r.role.replaceAll('_',' ');
  const role=(r:StudyRole,i:number)=><li key={r.scope.id+r.name+r.role+i}>
    <strong>{roleLabel(r)}: {r.name}</strong>
    {r.identity_status!=='verified' && <span> — identity unresolved; source-reported name</span>}
    <span> · {r.scope.kind==='context'?'Programme-wide role':r.scope.kind==='component'?'Intervention-specific role':'Study role'}</span>
    {r.evidence.length>0 && <details><summary>Role evidence</summary>{r.evidence.map((e,j)=><p key={j}>
      {e.quote && <span>{e.quote} </span>}<a href={safeLink(e.url)} target="_blank" rel="noreferrer">Source ↗</a> · {e.locator}
    </p>)}</details>}
  </li>;
  const projectCard=(p:StudyProject)=><article className="organization-card" key={p.id}>
      <h4>{p.title}</h4>
      <p className="result-note">{p.source==='ctgov'?'ClinicalTrials.gov':p.source.toUpperCase()} · {p.study_id}</p>
      <p>{p.source==='isrctn'?'Registry-listed recruitment countries':'Source-reported study locations'}: {p.geography.join(', ') || 'Not reported'}</p>
      
      {p.implementers.length>0 && <><h5>Verified implementing organizations</h5><ul>{p.implementers.map(role)}</ul></>}
      {p.missing_role && <p><strong>{p.missing_role}</strong></p>}
      {p.other_roles.length>0 && <><h5>Reported roles</h5><ul>{p.other_roles.map(role)}</ul></>}
      <p>Status: {studyStatus(p.status)}</p>
      {studyDates(p.dates).filter(d=>d.primary).map(d=><p key={d.label+d.value}>{d.label}: {d.value}</p>)}
      <details><summary>Evidence and details</summary>
        {p.match_reasons.map((r,i)=><p key={'match'+i}><strong>Why this project matches:</strong> {r}</p>)}
        <p>Record reviewed: {studyDates([{label:'reviewed_at',value:p.reviewed_at}])[0]?.value || 'Not reported'}</p>
        {p.source_date && <p>Source updated: {studyDates([{label:'source_date',value:p.source_date}])[0]?.value || 'Not reported'}</p>}
        {studyDates(p.dates).filter(d=>!d.primary).map(d=><p key={d.label+d.value}>{d.label}: {d.value}</p>)}
        {p.aliases.filter(a=>a.source!==p.source || a.study_id!==p.study_id).map((a,i)=><p key={i}>Also registered as: {a.source.toUpperCase()} {a.study_id}{a.url && <> · <a href={safeLink(a.url)} target="_blank" rel="noreferrer">Registry ↗</a></>}</p>)}
        <p>Interventions: {[...new Set(p.contexts.flatMap(c=>c.components.map(i=>i.label)))].join('; ')}</p>
        {p.gaps.filter(g=>!g.startsWith('Current operational status') && !g.startsWith('Implementing organization not yet verified')).map((gap,i)=><p key={i}>Uncertainty: {gap}</p>)}
      </details>
      <a href={safeLink(p.source_url)} target="_blank" rel="noreferrer">View study source ↗</a>
    </article>;
  const groups=new Map<string,{name:string;projects:Map<string,StudyProject>}>();
  const unresolved:StudyProject[]=[];
  for(const project of data.projects) {
    const roles=[...project.implementers,...project.other_roles];
    const verified=roles.filter(r=>r.identity_status==='verified' && r.organization_id);
    for(const r of verified) {
      const id=r.organization_id!;
      if(!groups.has(id))groups.set(id,{name:r.name,projects:new Map()});
      groups.get(id)!.projects.set(project.id,{...project,
        implementers:project.implementers.filter(x=>x.organization_id===id && x.identity_status==='verified'),
        other_roles:project.other_roles.filter(x=>x.organization_id===id && x.identity_status==='verified')});
    }
    const unknown=roles.filter(r=>r.identity_status!=='verified' || !r.organization_id);
    if(unknown.length || !roles.length)unresolved.push({...project,implementers:[],other_roles:unknown});
  }
  return <section aria-label="Reviewed study-backed projects">
    <h3>{broader?"Related nutrition projects":"Organizations from studies"}</h3>
    <p className="result-note">Expand an organization to see its projects and reported roles.</p>
    <p className="result-note">{broader?"These projects address broader nutrition needs. A specific stunting objective or stunting outcome has not been established.":"Organizations linked to reviewed studies of this condition and intervention. Study roles do not establish effectiveness or current activity. Other search filters are not applied."}</p>
    {data.status==='missing-context' && <p>Select both a condition and an intervention to see reviewed study-backed projects.</p>}
    {data.status==='empty' && <p>No reviewed study-backed projects match both selections in this bounded cohort.</p>}
    {data.status==='unavailable' && <p role="status">Reviewed study-backed projects are temporarily unavailable. IATI results remain separate.</p>}
    {['invalid-context','unsupported-context'].includes(data.status) && <p>Select one condition and one intervention for reviewed study-backed projects.</p>}
    {data.status==='partial' && <p role="status">Some source records have changed and require attribution review. Available reviewed projects are shown.</p>}
    {data.projects.length>0 && <p>{data.count} reviewed {data.count===1?'project':'projects'}</p>}
    {[...groups.entries()].sort((a,b)=>a[1].name.localeCompare(b[1].name)).map(([id,group])=>
      <details className="organization-card organization-dropdown" key={id}>
        <summary><strong>{group.name}</strong> · {group.projects.size} {group.projects.size===1?'project':'projects'}</summary>
        {[...group.projects.values()].map(projectCard)}
      </details>)}
    {unresolved.length>0 && <details className="organization-card organization-dropdown">
      <summary><strong>Organization not yet identified</strong> · {unresolved.length} {unresolved.length===1?'project':'projects'}</summary>
      <p className="result-note">Source-reported names below have not been linked to a verified organization. They are kept within their own projects.</p>
      {unresolved.map(projectCard)}
    </details>}
    {!broader && !!data.related_projects?.length && <StudyProjects broader data={{...data,projects:data.related_projects,count:data.related_projects.length,related_projects:[]}}/>}
  </section>;
}
