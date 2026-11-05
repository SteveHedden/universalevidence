import type {ResultRow} from './InterventionResults';
import {summarizeResults} from './resultSummaries';
export const summaryTerms = {increase:'Observed increase', decrease:'Observed decrease', no_clear:'No clear change/difference', mixed:'Mixed findings', uninterpretable:'Unable to interpret', no_results:'No results available', reported:'Source-qualified finding'} as const;
export type SummaryCode = keyof typeof summaryTerms;
const obj=(v:unknown):Record<string,any>=>v && typeof v==='object' && !Array.isArray(v)?v as Record<string,any>:{};
const num=(v:unknown):number|null=>(typeof v==='number'||typeof v==='string') && String(v).trim()!=='' && Number.isFinite(Number(v))?Number(v):null;
const fmt=(v:number)=>Number(v.toFixed(3)).toString();
const signCode=(v:number):SummaryCode=>v>0?'increase':v<0?'decrease':'no_clear';
export type Finding={key:string;scope:string;indicator:string;label:string;study:string;type:'descriptive'|'comparison'|'reported';code:SummaryCode;direction:number|null;uncertain:boolean;basis:string;time:string;detail:string;rows:ResultRow[];alternatives?:Finding[];package:boolean};
const ctx=(r:ResultRow)=>obj(obj(r.comparison).context);
export function resultText(v:unknown):string {
 if(v===null||v===undefined||v==='')return 'Timeframe unavailable';
 if(typeof v==='string'||typeof v==='number')return String(v);
 const x=obj(v);if(x.reported)return String(x.reported)+(x.reference_point?' · '+x.reference_point:'');
 return Object.entries(x).map(([k,value])=>`${k.replaceAll('_',' ')}: ${typeof value==='object'?JSON.stringify(value):String(value)}`).join('; ')||'Timeframe unavailable';
}
function assessment(v:unknown):{n:number;unit:string}|null {
 const s=String(v??'').trim().toLowerCase();
 if(s==='baseline')return {n:0,unit:'baseline'};
 if(/^post[- ]intervention$/.test(s))return {n:1,unit:'post'};
 const m=s.match(/^(\d+(?:\.\d+)?)\s*(days?|weeks?|months?|years?)\.?$/) || s.match(/\b(day|week|month|year)s?\s+(\d+(?:\.\d+)?)(?:\)|\.|$)/);
 if(!m)return null;
 return /^\d/.test(m[1])?{n:Number(m[1]),unit:m[2].replace(/s$/,'')}:{n:Number(m[2]),unit:m[1]};
}
export function denominator(r:ResultRow):string {
 const c=ctx(r), show=(v:unknown)=>(Array.isArray(v)?v:[]).flatMap(d=>(obj(d).counts??[]).filter((x:any)=>x.groupId===obj(c.group).id).map((x:any)=>`${x.value} ${obj(d).units}`)).join(', ');
 return `assessment ${show(c.class_denoms)||'unavailable'}; outcome ${show(c.outcome_denoms)||'unavailable'}`;
}
function base(r:ResultRow,type:Finding['type'],suffix=''):Finding {
 const c=ctx(r);
 return {key:r.measurement+suffix,scope:r.measurement+suffix,indicator:r.indicator??String(r.measure??r.indicator_label),label:r.indicator_label||String(r.measure??'Reported measure'),study:r.study,type,code:'uninterpretable',direction:null,uncertain:true,basis:type==='descriptive'?`Within ${obj(c.group).title||'unresolved group'}`:'Reported comparison',time:resultText(c.class??r.timing),detail:'Supported interpretation unavailable.',rows:[r],package:type==='descriptive'?(c.components??[]).length>1:obj(obj(r.comparison).attribution).package===true};
}
/** Exact duplicates are one observation. Conflicting identities must never select the first value. */
export function uniqueResultRows(rows:ResultRow[]):{rows:ResultRow[];conflicts:ResultRow[]} {
 const map=new Map<string,ResultRow[]>();for(const r of rows){const k=JSON.stringify([r.study,r.measurement]);map.set(k,[...(map.get(k)??[]),r]);}
 const output:ResultRow[]=[],conflicts:ResultRow[]=[];
 for(const rs of map.values()){if(!rs.some(r=>r.measurement_conflict) && new Set(rs.map(r=>JSON.stringify(r))).size===1)output.push(rs[0]);else conflicts.push(...rs);}
 return {rows:output,conflicts};
}
export function summarizeFindings(input:ResultRow[],intervention:string):Finding[] {
 const relevantStudies=new Set(input.filter(r=>r.discovery_interventions?.includes(intervention)||r.result_interventions?.includes(intervention)||obj(r.summary_mapping).tested?.includes(intervention)).map(r=>r.study));
 const {rows,conflicts}=uniqueResultRows(input);
 const findings:Finding[]=[],handled=new Set<string>();
 // Preserve existing reviewed comparison scales, orientations, selection and provenance.
 for(const s of summarizeResults(rows,intervention)){
  const source=rows.filter(r=>s.evidence.some(e=>e.study===r.study&&e.measurement===r.measurement));
  for(const r of source){const one=summarizeResults([r,...rows.filter(x=>x!==r && !x.summary_mapping)],intervention).find(x=>x.evidence.some(e=>e.measurement===r.measurement))??s;
   const e=one.evidence.find(x=>x.measurement===r.measurement)!;
   const uncertainNull=one.uncertaintyStatus==='null_spanning';
   const m=obj(r.summary_mapping),c=obj(r.comparison);
   findings.push({...base(r,'comparison'),scope:JSON.stringify([r.indicator,r.unit,m.tested,m.comparator,m.background,m.summary_timepoint??r.timing,r.subgroup,m.estimand,m.model_context,m.group_labels,c.statistic,c.quantity,c.model_context,ctx(r).statistic,ctx(r).groups,ctx(r).adjustment,s.package,(!r.subgroup||!(m.summary_timepoint??r.timing))?r.study:null]),code:uncertainNull?'no_clear':signCode(e.direction),direction:e.direction,uncertain:!e.clear,basis:one.context,time:resultText(m.summary_timepoint??r.timing),detail:`${one.magnitude}. ${one.interval}. ${uncertainNull?'An interval spanning the null or nonsignificant test does not establish no effect.':!e.clear?'Point-estimate direction only; uncertainty does not establish a clear difference.':''} ${c.statistic==='ratio'?'Ratio null: 1.':''}`,package:s.package});
   handled.add(r.measurement);
  }
 }
 const armGroups=new Map<string,ResultRow[]>();
 for(const r of rows){
  const retained=obj(r.finding_mapping);
  if(retained.effect_eligible===false && retained.selected===false && ['qualified-text','descriptive'].includes(retained.presentation) && retained.tested?.includes(intervention)){
   const f=base(r,'reported'),comparison=obj(r.comparison);
   findings.push({...f,code:'reported',basis:retained.group_labels?.join(' versus ')||'Source-reported context',detail:[typeof r.result_text==='string'?r.result_text:'',r.value!==undefined?`Source estimate: ${r.value} ${r.unit??''}.`:'',`Comparison: ${resultText(comparison)}.`,`Population: ${resultText(r.subgroup)}.`,`Reported uncertainty: ${JSON.stringify(r.uncertainty??{})}.`,String(retained.reason??''),r.indicator_review?`Indicator qualification: ${resultText(r.indicator_review)}`:''].filter(Boolean).join(' '),package:retained.package===true});
   handled.add(r.measurement);continue;
  }
  if(!relevantStudies.has(r.study))continue;
  const c=ctx(r),comparison=obj(r.comparison),kind=comparison.kind;
  if(kind==='arm_level'){
   const outcome=r.measurement.split('/classes/')[0];
   const k=JSON.stringify([r.study,outcome,r.indicator,r.unit,c.statistic,obj(c.group).id,c.category,r.subgroup,c.population]);
   armGroups.set(k,[...(armGroups.get(k)??[]),r]);
  }else if(kind==='within_arm_change'){
   const f=base(r,'descriptive'),v=num(r.value);
   const valid=v!==null&&obj(obj(comparison.numerical_validation).estimate).status!=='invalid';
   findings.push({...f,code:valid?signCode(v!):'uninterpretable',direction:valid?Math.sign(v!):null,detail:`Source-reported change: ${r.value??'unavailable'} ${r.unit??''}. ${denominator(r)}. Uncertainty of change is not inferred.`,basis:f.basis+' · source-reported change'});
  }else if(!handled.has(r.measurement) && (kind==='source_comparison'||kind==='reported_arm_measurement'||r.summary_mapping)){
   const f=base(r,kind==='reported_arm_measurement'?'descriptive':'comparison');
   findings.push({...f,detail:`${comparison.reason??'No supported selected comparison interpretation.'} Source estimate: ${r.value??'unavailable'} ${r.unit??''}. Reported uncertainty: ${JSON.stringify(r.uncertainty??{})}.`,basis:comparison.estimand==='interaction'?'Reported treatment-by-time interaction · excluded from effect summaries':f.basis});
  }
 }
 for(const [scope,rs] of armGroups){
  const first=rs[0],c=ctx(first),f=base(first,'descriptive'),baseline=rs.filter(r=>assessment(ctx(r).class)?.unit==='baseline');
  const endpoints=rs.filter(r=>{const a=assessment(ctx(r).class);return a && a.n>0&&a.unit!=='baseline';});
  const common=!!c.population && !!c.statistic && !!obj(c.group).id && !!first.indicator && !!first.unit;
  const pairs:Finding[]=[];
  if(common&&baseline.length===1){for(const end of endpoints){const a=assessment(ctx(end).class)!;
   const repeated=endpoints.filter(r=>JSON.stringify(assessment(ctx(r).class))===JSON.stringify(a)).length>1;
   const start=baseline[0],v0=num(start.value),v1=num(end.value);
   if(repeated||v0===null||v1===null||[start,end].some(r=>obj(obj(obj(r.comparison).numerical_validation).estimate).status==='invalid'))continue;
   const delta=v1-v0;
   pairs.push({...f,key:start.measurement+'→'+end.measurement,scope:scope+JSON.stringify(a),code:signCode(delta),direction:Math.sign(delta),time:`${ctx(start).class} → ${ctx(end).class}`,rows:[start,end],detail:`${start.value} → ${end.value} ${first.unit}; difference of reported ${String(c.statistic).toLowerCase()}s: ${fmt(delta)}. Denominators: baseline ${denominator(start)}; follow-up ${denominator(end)}. ${denominator(start)!==denominator(end)?'Denominators differ; attrition or composition may affect this descriptive difference. ':''}Population: ${c.population}. Same individual participants are not established. No uncertainty of change or treatment effect inferred.`});
  }}
  const units=new Set(endpoints.map(r=>assessment(ctx(r).class)!.unit).filter(u=>u!=='post'));
  pairs.sort((a,b)=>{const x=assessment(ctx(a.rows[1]).class)!,y=assessment(ctx(b.rows[1]).class)!;return Number(x.unit==='post')-Number(y.unit==='post')||y.n-x.n;});
  const unsupported=rs.filter(r=>!baseline.includes(r)&&!pairs.some(p=>p.rows.includes(r)));
  if(pairs.length && units.size<=1){const selected=pairs[0];selected.alternatives=pairs.slice(1);selected.detail+=' Selection: baseline to latest numeric assessment in one time unit, or sole post-intervention assessment. An undated post-intervention assessment is retained separately without assuming its order relative to numeric assessments.';findings.push(selected);}
  else findings.push({...f,scope,rows:rs,alternatives:pairs,detail:!common?'Population or pairing metadata is missing; comparison of these group summaries is unsupported.':units.size>1?'Mixed time units prevent selecting a latest assessment; supported baseline contrasts are retained separately.':'A unique baseline and supported ordered endpoint are required; no change inferred.'});
  if(pairs.length&&unsupported.length)findings.push({...f,key:f.key+'-unresolved-assessments',scope:scope+'-unresolved-assessments',rows:unsupported,time:unsupported.map(r=>resultText(ctx(r).class)).join(' / '),detail:'Some assessments have unknown chronology, conflicting cells or invalid values; they are retained without a computed change.'});
 }
 for(const r of conflicts.filter(r=>relevantStudies.has(r.study))){const conflictKey=JSON.stringify([r.study,r.measurement,'conflict']);if(findings.some(f=>f.key===conflictKey))continue;findings.push({...base(r,['arm_level','within_arm_change','reported_arm_measurement'].includes(String(obj(r.comparison).kind))?'descriptive':'comparison','-conflict'),key:conflictKey,scope:conflictKey,rows:conflicts.filter(x=>x.study===r.study&&x.measurement===r.measurement),detail:'Conflicting duplicate measurement identity; no value selected.'});}
 const priority=(f:Finding)=>f.type==='descriptive'&&f.direction!==null?(f.rows.some(r=>r.result_interventions?.includes(intervention))?0:2):f.type==='comparison'?1:3;
 return findings.sort((a,b)=>priority(a)-priority(b));
}
export function aggregateFindings(findings:Finding[]) {
 const scopes=new Map<string,Finding[]>();for(const f of findings){const k=JSON.stringify([f.indicator,f.type,f.scope]);scopes.set(k,[...(scopes.get(k)??[]),f]);}
 return [...scopes].map(([key,items])=>{
  const studies=new Set(items.map(f=>f.study)),unresolved=new Set(items.filter(f=>f.code==='uninterpretable').map(f=>f.study)),uncertain=new Set(items.filter(f=>f.uncertain).map(f=>f.study));
  const signs=new Set(items.filter(f=>f.direction!==null).map(f=>f.direction));
  const code:SummaryCode=signs.size>1?'mixed':items.every(f=>f.code==='uninterpretable')?'uninterpretable':items.some(f=>f.code==='no_clear')?'no_clear':items.find(f=>f.direction!==null)?.code??(items.some(f=>f.code==='reported')?'reported':'uninterpretable');
  const counts={increase:0,decrease:0,mixed:0,no_clear:0,reported:0,uninterpretable:0};
  for(const study of studies){const records=items.filter(f=>f.study===study),directions=new Set(records.filter(f=>f.direction!==null).map(f=>f.direction));
   if(directions.size>1)counts.mixed++;else if(records.some(f=>f.code==='no_clear'))counts.no_clear++;else if(directions.has(1))counts.increase++;else if(directions.has(-1))counts.decrease++;else if(records.some(f=>f.code==='reported'))counts.reported++;else counts.uninterpretable++;
  }
  return {key,items,code,counts,studies:studies.size,unresolved:unresolved.size,uncertain:uncertain.size,label:items[0].label,type:items[0].type};
 });
}

/** Navigation counts: one study per indicator bucket; preserve every underlying scope. */
export function groupFindingsByIndicator(findings:Finding[]) {
 const indicators=new Map<string,Finding[]>();
 for(const f of findings)indicators.set(f.indicator,[...(indicators.get(f.indicator)??[]),f]);
 const order:SummaryCode[]=['increase','decrease','no_clear','mixed','reported','uninterpretable'];
 return [...indicators].map(([id,items])=>{
  const studies=new Map<string,Finding[]>();
  for(const f of items)studies.set(f.study,[...(studies.get(f.study)??[]),f]);
  const grouped=[...studies].map(([study,records])=>{
   const codes=new Set(records.filter(f=>!['uninterpretable','reported'].includes(f.code)).map(f=>f.code));
   return {id:study,findings:records,code:codes.size>1?'mixed' as SummaryCode:[...codes][0]??(records.some(f=>f.code==='reported')?'reported':'uninterpretable') as SummaryCode};
  });
  return {id,label:items[0].label,studies:studies.size,groups:order.map(code=>({code,studies:grouped.filter(s=>s.code===code)})).filter(g=>g.studies.length)};
 });
}
