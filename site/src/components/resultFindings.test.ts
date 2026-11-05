import {test,expect} from 'vitest';
import {summarizeFindings,aggregateFindings,summaryTerms} from './resultFindings';
import {summarizeResults} from './resultSummaries';
import type {ResultRow} from './InterventionResults';
import writing from './task266WritingFixture.json';
const intervention='https://universalevidence.com/vocab/interventions/ExpressiveWritingIntervention';
const real=writing as unknown as ResultRow[];
function arm(time:string,value:string,id=time):ResultRow {return {study:'urn:other-study',measurement:`urn:other-outcome/classes/${id}`,indicator:'urn:other-indicator',indicator_label:'Other scale',unit:'points',value,direction:'unknown',result_interventions:['urn:therapy'],comparison:{kind:'arm_level',context:{class:time,population:'Enrolled population',statistic:'MEAN',group:{id:'G',title:'Different therapy'},components:['urn:therapy']}}};}
test('real writing baseline to three months is visible as descriptive only, control and gap retained',()=>{
 const fs=summarizeFindings(real,intervention),ew=fs.find(f=>f.basis.includes('Expressive Writing')&&f.code==='decrease')!;
 expect(ew.rows.map(r=>r.value)).toEqual(['25.17','18.45']);expect(ew.time).toBe('Baseline → 3 Months');
 expect(ew.alternatives).toHaveLength(1);expect(ew.alternatives![0].time).toBe('Baseline → Post-Intervention');expect(ew.detail).toContain('Denominators differ');
 expect(fs.some(f=>f.basis==='Within Control'&&f.type==='descriptive')).toBe(true);
 expect(fs.some(f=>f.type==='comparison'&&f.code==='uninterpretable')).toBe(true);
 expect(summarizeResults(real,intervention)).toEqual([]);
});
test.each([['8','increase'],['2','decrease'],['4','no_clear']])('generic study %s classifies as %s',(value,code)=>{
 const rows=[arm('Baseline','4'),arm('3 Months',value)];const f=summarizeFindings(rows,'urn:therapy')[0];
 expect(f.code).toBe(code);expect(f.rows).toEqual(rows);expect(f.detail).toContain('Same individual participants are not established');
});
test('missing population, incompatible units/statistics, duplicate time cells and unknown chronology fail closed',()=>{
 const rows=[arm('Baseline','4'),arm('3 Months','2')];
 for(const mutate of [(r:ResultRow)=>{(r.comparison!.context as any).population=null;},(r:ResultRow)=>{r.unit='percent';},(r:ResultRow)=>{(r.comparison!.context as any).statistic='MEDIAN';},(r:ResultRow)=>{(r.comparison!.context as any).class='After';}]){
  const copy=structuredClone(rows);mutate(copy[1]);expect(summarizeFindings(copy,'urn:therapy').every(f=>f.code==='uninterpretable')).toBe(true);
 }
 expect(summarizeFindings([...rows,arm('3 Months','7','duplicate')],'urn:therapy').every(f=>f.code==='uninterpretable')).toBe(true);
});
test('deduplicates exact rows but preserves conflicting identities as unresolved',()=>{
 const rows=[arm('Baseline','4'),arm('3 Months','2')];expect(summarizeFindings([...rows,...rows],'urn:therapy')).toEqual(summarizeFindings(rows,'urn:therapy'));
 expect(summarizeFindings([...rows,{...rows[1],value:'9'}],'urn:therapy').some(f=>f.detail.includes('Conflicting duplicate'))).toBe(true);
});
test('latest policy cannot cherry-pick favorable value and retains earlier alternatives',()=>{
 const fs=summarizeFindings([arm('Baseline','4'),arm('1 Month','2'),arm('3 Months','7')],'urn:therapy');
 expect(fs[0].code).toBe('increase');expect(fs[0].alternatives![0].code).toBe('decrease');
 const mixed=summarizeFindings([arm('Baseline','4'),arm('2 Weeks','2'),arm('3 Months','7')],'urn:therapy');expect(mixed[0].code).toBe('uninterpretable');expect(mixed[0].alternatives).toHaveLength(2);
});
function comparison(study:string,id:string,value:string,uncertainty:any={}):ResultRow{return {study,measurement:id,indicator:'urn:haz',indicator_label:'HAZ',unit:'z-score',value,direction:'unknown',subgroup:'Same defined population',uncertainty,summary_mapping:{study,measurement:id,indicator:'urn:haz',unit:'z-score',tested:['urn:therapy'],comparator:[],background:[],summary_timepoint:'Month 3',group_labels:['Therapy','Control']}};}
test('supported uncertainty, null, missing uncertainty and unresolved semantics remain distinct',()=>{
 let r=comparison('s','m','1',{kind:'confidence-interval',level:95,lower:-1,upper:2});
 expect(summarizeFindings([r],'urn:therapy')[0].code).toBe('no_clear');
 r.uncertainty={};const f=summarizeFindings([r],'urn:therapy')[0];expect(f.code).toBe('increase');expect(f.uncertain).toBe(true);
 r.comparison={kind:'source_comparison',semantics:'unsupported',reason:'Unknown orientation'};
 expect(summarizeFindings([r],'urn:therapy')[0].code).toBe('uninterpretable');
});
test('one study with conflicting findings is counted once, incompatible timeframes stay separate',()=>{
 const rows=[comparison('s','a','1'),comparison('s','b','-1'),comparison('t','c','1')];
 let gs=aggregateFindings(summarizeFindings(rows,'urn:therapy'));expect(gs).toHaveLength(1);expect(gs[0].studies).toBe(2);expect(gs[0].code).toBe('mixed');
 rows[2].summary_mapping!.summary_timepoint='Month 6';gs=aggregateFindings(summarizeFindings(rows,'urn:therapy'));expect(gs).toHaveLength(2);
});
test('stable vocabulary and empty results',()=>{expect(Object.values(summaryTerms)).toEqual(['Observed increase','Observed decrease','No clear change/difference','Mixed findings','Unable to interpret','No results available','Source-qualified finding']);expect(summarizeFindings([],'urn:therapy')).toEqual([]);});

import stunting from './task266StuntingFixture.json';
test('current live Stunting result retains HAZ and prevalence, direct/package and one distinct study',()=>{
 const rows=stunting as unknown as ResultRow[];
 const findings=summarizeFindings(rows,'https://universalevidence.com/vocab/interventions/OralNutritionalSupplementation');
 expect(findings).toHaveLength(4);expect(new Set(findings.map(f=>f.study)).size).toBe(1);
 expect(findings.filter(f=>f.package)).toHaveLength(2);expect(new Set(findings.map(f=>f.indicator)).size).toBe(2);
 const directPrevalence=findings.find(f=>!f.package&&f.indicator.endsWith('Prevalence'))!;
 expect(directPrevalence.code).toBe('decrease');expect(directPrevalence.detail).toContain('17% lower');expect(directPrevalence.uncertain).toBe(false);
 expect(findings.find(f=>f.package&&f.indicator.endsWith('Prevalence'))!.code).toBe('no_clear');
 expect(aggregateFindings(findings).every(s=>s.studies===1)).toBe(true);
});

test.each(['odds ratio','risk ratio','hazard ratio'])('ratio %s null is one, reverse orientation preserves operator',unit=>{
 const r=comparison('s','m','2',{kind:'confidence-interval',level:95,lower:1.1,upper:3});r.unit=unit;r.summary_mapping!.unit=unit;r.summary_mapping!.effect_multiplier=-1;
 const f=summarizeFindings([r],'urn:therapy')[0];expect(f.code).toBe('decrease');expect(f.direction).toBe(-1);
 r.value='1';r.uncertainty={kind:'confidence-interval',level:95,lower:.8,upper:1.2};expect(summarizeFindings([r],'urn:therapy')[0].code).toBe('no_clear');
});

test('source-reported changes are separate from endpoint differences and never treatment votes',()=>{
 const rows=[arm('Baseline','4'),arm('3 Months','2'),{...arm('Reported change','-1','reported-change'),comparison:{...arm('Reported change','-1').comparison,kind:'within_arm_change'}}];
 const fs=summarizeFindings(rows,'urn:therapy');expect(fs).toHaveLength(2);expect(fs.some(f=>f.detail.includes('Source-reported change: -1'))).toBe(true);expect(fs.some(f=>f.detail.includes('means: -2'))).toBe(true);expect(summarizeResults(rows,'urn:therapy')).toEqual([]);
});
test('conflicting duplicate descriptive cells cannot become comparison votes',()=>{
 const rows=[arm('Baseline','4'),arm('3 Months','2')];const fs=summarizeFindings([...rows,{...rows[1],value:'9'}],'urn:therapy');
 expect(fs.every(f=>f.type==='descriptive')).toBe(true);expect(fs.every(f=>f.direction===null)).toBe(true);
});
test('missing population metadata prevents cross-study aggregation',()=>{
 const a=comparison('a','a','1'),b=comparison('b','b','1');delete a.subgroup;delete b.subgroup;
 expect(aggregateFindings(summarizeFindings([a,b],'urn:therapy'))).toHaveLength(2);
});
test('structured source timeframe is human readable',()=>{
 const r=comparison('s','m','1');delete r.summary_mapping!.summary_timepoint;r.timing={reported:'18 months',reference_point:'Start of intervention'};
 expect(summarizeFindings([r],'urn:therapy')[0].time).toBe('18 months · Start of intervention');
});

import {groupFindingsByIndicator, type Finding} from './resultFindings';
test('indicator direction counts deduplicate studies and retain conflicting findings in mixed',()=>{
 const f=(study:string,code:Finding['code'],key:string)=>({study,code,key,indicator:'haz',label:'HAZ'} as Finding);
 const [g]=groupFindingsByIndicator([f('a','increase','1'),f('a','increase','2'),f('b','increase','3'),f('b','decrease','4'),f('c','no_clear','5')]);
 expect(g.studies).toBe(3);
 expect(g.groups.map(x=>[x.code,x.studies.length])).toEqual([['increase',1],['no_clear',1],['mixed',1]]);
 expect(g.groups.find(x=>x.code==='mixed')!.studies[0].findings).toHaveLength(2);
});
