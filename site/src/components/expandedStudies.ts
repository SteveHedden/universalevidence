import {queryStudiesV2, type QueryV2Values, type QueryV2Logic, type QueryV2Response, type StudyResult} from '../api/client';
import type {ResultRow} from './InterventionResults';

export function groupQuery(context:string, intervention:string):{values:QueryV2Values;logic:QueryV2Logic}|null {
  try {
    const {values,logic,country}=JSON.parse(context);
    if(country || !values?.state?.length || values.intervention?.length)return null;
    return {values:{...values,intervention:[intervention]},logic:{...logic,intervention:'or'}};
  } catch {return null;}
}
export function fetchGroup(context:string,intervention:string):Promise<QueryV2Response> {
  const query=groupQuery(context,intervention);
  if(!query)throw new Error('This search cannot be expanded.');
  return queryStudiesV2(query.values,query.logic);
}
export function mergeStudies(original:StudyResult[], extra:StudyResult[]):StudyResult[] {
  const key=(r:StudyResult)=>JSON.stringify([r.study_identity ?? [r.source,r.study_id??r.url??r.title],r.intervention_concept_uri,r.condition_concept_uri]);
  const rows=new Map(original.map(r=>[key(r),r]));
  for(const r of extra)if(!rows.has(key(r)))rows.set(key(r),r);
  return [...rows.values()];
}
// Discovery context may expand without changing the numerical observation. Only
// coalesce scientifically identical rows; differing values/provenance remain
// distinct so the existing conflict detector can refuse an interpretation.
export function mergeMeasurements(original:ResultRow[], extra:ResultRow[]):ResultRow[] {
  const signature=(r:ResultRow)=>{
    const {discovery_interventions,preferred_labels,...evidence}=r;
    const stable=(v:unknown):unknown=>Array.isArray(v)?v.map(stable):v&&typeof v==='object'?Object.fromEntries(Object.entries(v).sort(([a],[b])=>a.localeCompare(b)).map(([k,x])=>[k,stable(x)])):v;
    return JSON.stringify(stable(evidence));
  };
  const rows=new Map<string,ResultRow>();
  for(const r of [...original,...extra]){
    const key=signature(r),prior=rows.get(key);
    rows.set(key,prior?{...prior,discovery_interventions:[...new Set([...(prior.discovery_interventions??[]),...(r.discovery_interventions??[])])],preferred_labels:{...prior.preferred_labels,...r.preferred_labels}}:r);
  }
  return [...rows.values()];
}
