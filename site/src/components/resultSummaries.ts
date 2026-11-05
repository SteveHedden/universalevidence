import policy from '../config/result-summary-policy.json';
import { scaleFor, transformValue, displayMagnitude, displayInterval } from './resultScales';
import type {ResultRow} from './InterventionResults';
type Details = Record<string, unknown>;
const object=(v:unknown):Details=>v && typeof v==='object' && !Array.isArray(v)?v as Details:{};
const number=(v:unknown):number|null=> (typeof v==='number'||typeof v==='string'&&v.trim()!=='') && Number.isFinite(Number(v))?Number(v):null;
const fmt=(v:number)=>Number(v.toFixed(2)).toString();
const sourceUncertaintyFor=(row:ResultRow)=>object(row.uncertainty);
export const conceptLabel=(uri:string)=>uri.split('/').pop()?.replace(/([a-z])([A-Z])/g,'$1 $2') ?? uri;
export type Summary={uncertaintyStatus:'clear'|'null_spanning'|'missing';indicatorId:string;evidence:{study:string;measurement:string;direction:number;clear:boolean}[];key:string;indicator:string;headline:string;magnitude:string;interval:string;studies:number;package:boolean;context:string;citations:{url:string;citation:string}[]};
export function summarizeResults(rows:ResultRow[],intervention:string):Summary[]{
 const buckets=new Map<string,{row:ResultRow;entry:typeof policy.entries[number];value:number;low:number|null;high:number|null;p:number|null}[]>();
 const dynamic=rows.map(r=>r.summary_mapping).filter(Boolean) as unknown as typeof policy.entries;
 const entries=[...policy.entries.filter(e=>!e.study.includes('/studies/ctgov/') && !e.study.includes('/studies/isrctn/') && !dynamic.some(d=>d.measurement===e.measurement && d.study===e.study)),...dynamic];
 for(const entry of entries){
  if((entry as any).selected===false || !entry.tested.includes(intervention))continue;
  if(['interaction','association'].includes((entry as any).estimand))continue;
  const matches=rows.filter(r=>r.study===entry.study && r.indicator===entry.indicator && r.unit===entry.unit &&
   (entry.measurement?r.measurement===entry.measurement:entry.selector && r.measurement.endsWith(entry.selector)));
  if(matches.length!==1)continue; // Missing or ambiguous source identities require review.
  const row=matches[0],raw=number(row.value);if(raw===null || row.measurement_conflict)continue;
  if(['arm_level','within_arm_change','reported_arm_measurement'].includes(String(object(row.comparison).kind)))continue;
  const comparison=object(row.comparison);
  if(comparison.semantics==='unsupported' || ['interaction','association','transformed','unsupported'].includes(String(comparison.estimand)))continue;
  const validation=object(object(row.comparison).numerical_validation);
  if(object(validation.estimate).status==='invalid')continue;
  const scale=scaleFor(entry.unit);if(!scale)continue;
  const multiplier=(entry as any).effect_multiplier ?? 1;
  if(multiplier!==1 && multiplier!==-1)continue;
  const value=transformValue(raw,scale,multiplier);if(value===null)continue;
  const sourceUncertainty=object(row.uncertainty);
  const u=(entry as any).effect_multiplier!==undefined && sourceUncertainty.ciNumSides==='TWO_SIDED' && object(validation.confidence_interval).status==='valid'
   ? {kind:'confidence-interval',level:sourceUncertainty.ciPctValue,lower:sourceUncertainty.ciLowerLimit,upper:sourceUncertainty.ciUpperLimit}
   :sourceUncertainty;let low:number|null=null,high:number|null=null;
  // Report extraction stores probability (0.95); registry APIs store percent (95).
  const confidence=number(u.level);
  if(u.kind==='confidence-interval' && (confidence===95 || confidence===0.95)){
   const l=number(u.lower),h=number(u.upper);
   if(l!==null && h!==null && l<=raw && raw<=h && (!scale.ratio||l>0)){
    const ends=[transformValue(l,scale,multiplier),transformValue(h,scale,multiplier)];
    if(ends.every(x=>x!==null)){low=Math.min(...ends as number[]);high=Math.max(...ends as number[]);}
   }
  }
  let p:number|null=null;
  if((entry as any).effect_multiplier!==undefined && object(validation.p_value).status==='valid')p=number(sourceUncertainty.pValue);
  if(entry.pValueSelector){
   const sourceVersion=row.measurement.split('/resultsSection/')[0];
   const analyses=rows.filter(r=>r.study===row.study && r.indicator===row.indicator && r.measurement===sourceVersion+entry.pValueSelector);
   if(analyses.length===1){const n=number(object(analyses[0].uncertainty).pValue);if(n!==null&&n>=0&&n<=1)p=n;}
  }
  // Keep exact contrast/time/population strata; never silently pool incompatible studies.
  const key=JSON.stringify([entry.tested,entry.comparator,entry.background,entry.indicator,entry.unit,(entry as any).summary_timepoint ?? row.timing,row.subgroup,(entry as any).contrast_id,(entry as any).estimand,(entry as any).model_context]);
  buckets.set(key,[...(buckets.get(key)??[]),{row,entry,value,low,high,p}]);
 }
 return [...buckets].map(([key,estimates])=>{
  const {row,entry}=estimates[0];const values=estimates.map(x=>x.value),lo=Math.min(...values),hi=Math.max(...values);
  const isPackage=(entry as any).package ?? entry.tested.length>1;
  const uncertain=estimates.some(x=>x.low!==null && x.high!==null ? x.low<=0&&x.high>=0 : x.p!==null&&x.p>=policy.alpha);
  const missing=estimates.some(x=>x.low===null&&x.p===null);
  const mixed=lo<0&&hi>0;
  const scale=scaleFor(entry.unit)!;
  const magnitude=displayMagnitude(lo,hi,scale);
  const headline=uncertain?'No clear difference detected':missing?'Direction uncertain':mixed?'Mixed directions':lo===0&&hi===0?'No observed difference':magnitude;
  const e=estimates[0];
  let interval='Uncertainty unavailable';
  if(estimates.length===1&&e.low!==null&&e.high!==null){
   interval=displayInterval(e.low,e.high,scale);
  }else if(e.p!==null)interval=`Reported comparison p = ${fmt(e.p)}; no effect interval`;
  else if(estimates.length===1 && object(e.row.uncertainty).kind==='standard-error' && number(object(e.row.uncertainty).value)!==null)interval=`Reported standard error: ${fmt(Number(object(e.row.uncertainty).value)*scale.factor)}; confidence interval not reported`;
  if(estimates.length>1)interval='Separate study estimates; no pooled confidence interval';
  if(estimates.length===1 && interval==='Uncertainty unavailable' && sourceUncertaintyFor(e.row).dispersionType==='STANDARD_ERROR_OF_MEAN' && object(object(object(e.row.comparison).numerical_validation).dispersion).status==='valid')interval=`Reported standard error: ${fmt(Number(sourceUncertaintyFor(e.row).dispersionValue)*scale.factor)}; confidence interval not reported`;
  if(scale.ratio && (entry as any).effect_multiplier===-1 && interval.startsWith('Reported standard error:'))interval+=' (original source orientation; not transformed)';
  const groupLabels=(entry as any).group_labels as string[]|undefined;
  const label=(uri:string)=>row.preferred_labels?.[uri] || 'Label unavailable';
  const contextBase=groupLabels?.length===2?`${groupLabels[0]} versus ${groupLabels[1]}`:isPackage?'Package: '+entry.tested.map(uri=>row.preferred_labels?.[uri] || 'Label unavailable').join(' + '):entry.background.length?'Added to '+entry.background.map(uri=>row.preferred_labels?.[uri] || 'Label unavailable').join(' + '):'Compared with '+(entry.comparator.length?entry.comparator.map(uri=>row.preferred_labels?.[uri] || 'Label unavailable').join(' + '):'no assigned intervention');
  const context=((entry as any).estimand && (entry as any).estimand!=='pairwise' ? `${(entry as any).estimand}: `:'')+contextBase+((entry as any).model_context ? ` · ${(entry as any).model_context}`:'')+((entry as any).summary_timepoint ? ` · ${(entry as any).summary_timepoint}`:'');
  const citations=estimates.flatMap(x=>x.row.citations??[]).filter(c=>/^https?:\/\//.test(c.url));
  return {uncertaintyStatus:uncertain?'null_spanning' as const:missing?'missing' as const:'clear' as const,indicatorId:row.indicator!,evidence:estimates.map(x=>({study:x.row.study,measurement:x.row.measurement,direction:Math.sign(x.value),clear:x.low!==null&&x.high!==null ? x.low>0||x.high<0 : x.p!==null&&x.p<policy.alpha})),key,indicator:row.indicator_label.replace('SDG 2.2.1 under-five stunting prevalence','Under-five stunting prevalence'),headline,magnitude,interval,studies:new Set(estimates.map(x=>x.row.study)).size,package:isPackage,context,citations:[...new Map(citations.map(c=>[c.url,c])).values()]};
 }).sort((a,b)=>Number(a.package)-Number(b.package));
}

export type DirectionCounts={increase:number;decrease:number;mixed:number;unchanged:number};
export function aggregateIndicators(summaries:Summary[]){
 const groups=new Map<string,Summary[]>();
 for(const s of summaries)groups.set(s.indicatorId,[...(groups.get(s.indicatorId)??[]),s]);
 const count=(items:Summary[])=>{
  const studies=new Map<string,Set<number>>();const uncertain=new Set<string>();
  for(const s of items)for(const e of s.evidence){
   const signs=studies.get(e.study)??new Set<number>();signs.add(e.direction);studies.set(e.study,signs);
   if(!e.clear)uncertain.add(e.study);
  }
  const counts:DirectionCounts={increase:0,decrease:0,mixed:0,unchanged:0};
  for(const signs of studies.values()){
   if(signs.size>1)counts.mixed++;else if(signs.has(1))counts.increase++;else if(signs.has(-1))counts.decrease++;else counts.unchanged++;
  }
  return {studies:studies.size,counts,uncertain:uncertain.size};
 };
 return [...groups].map(([key,items])=>{
  const overall=count(items),direct=count(items.filter(s=>!s.package)),combined=count(items.filter(s=>s.package));
  const c=overall.counts;
  const headline=c.mixed||[c.increase,c.decrease,c.unchanged].filter(Boolean).length>1?'Mixed observed directions':c.increase?'Observed increases':c.decrease?'Observed decreases':'No observed change';
  return {key,indicator:items[0].indicator,headline,...overall,direct,combined,items};
 });
}
export function directionCountText(counts:DirectionCounts){
 return (['decrease','increase','mixed','unchanged'] as const).filter(k=>counts[k]).map(k=>`${counts[k]} ${k==='mixed'?'mixed':k==='unchanged'?'unchanged':k==='decrease'?'decreased':'increased'}`).join(' · ');
}
