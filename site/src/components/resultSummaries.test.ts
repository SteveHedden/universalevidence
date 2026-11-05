import {test,expect} from 'vitest';
import {summarizeResults} from './resultSummaries';
import type {ResultRow} from './InterventionResults';
const base='https://universalevidence.com/';
const i=base+'vocab/interventions/';
const study=base+'studies/aea/AEARCTR-0014624';
function z(arm='sq-lns',value='0.83',lower=.73,upper=.94):ResultRow{return {study,measurement:study+'/findings/bmj2024-table2-'+arm+'-col6',indicator:base+'vocab/indicators/SDG221UnderFiveStuntingPrevalence',indicator_label:'SDG 2.2.1 under-five stunting prevalence',value,unit:'prevalence ratio',direction:'decrease',uncertainty:{kind:'confidence-interval',level:95,lower,upper}};}
test('one reviewed model becomes relative magnitude with interval',()=>{
 const row=z();const result=summarizeResults([row,{...row,measurement:row.measurement.replace('col6','col5')}],i+'OralNutritionalSupplementation');
 expect(result).toHaveLength(1);expect(result[0].headline).toBe('17% lower (relative)');expect(result[0].interval).toBe('95% interval: 6–27% lower (relative)');expect(result[0].studies).toBe(1);
});
test('nutrition education cannot inherit supplement-only comparison',()=>{expect(summarizeResults([z()],i+'NutritionEducation')).toEqual([]);});
test('package stays separate and uncertain interval avoids directional headline',()=>{
 const r=summarizeResults([z('combined','0.91',.81,1.03)],i+'OralNutritionalSupplementation')[0];expect(r.package).toBe(true);expect(r.headline).toBe('No clear difference detected');expect(r.magnitude).toBe('9% lower (relative)');
});
test('current numeric response determines magnitude, no hardcoded result',()=>{
 expect(summarizeResults([z('sq-lns','0.8',.7,.9)],i+'OralNutritionalSupplementation')[0].headline).toBe('20% lower (relative)');
});
test('missing or invalid intervals never imply confidence',()=>{
 const r=z();r.uncertainty={};expect(summarizeResults([r],i+'OralNutritionalSupplementation')[0].headline).toBe('Direction uncertain');
 r.uncertainty={kind:'confidence-interval',level:95,lower:3,upper:1};expect(summarizeResults([r],i+'OralNutritionalSupplementation')[0].headline).toBe('Direction uncertain');
});
test('ambiguous, invalid and unreviewed source results excluded',()=>{
 expect(summarizeResults([z(),z()],i+'OralNutritionalSupplementation')).toEqual([]);
 expect(summarizeResults([{...z(),value:'NaN'}],i+'OralNutritionalSupplementation')).toEqual([]);
 expect(summarizeResults([{...z(),unit:'odds ratio'}],i+'OralNutritionalSupplementation')).toEqual([]);
});
test('a named API fixture cannot inherit browser study-specific rules',()=>{
 const prefix='https://clinicaltrials.gov/api/v2/studies/NCT00659061#sha256-live';
 const r:ResultRow={...z(),study:base+'studies/ctgov/NCT00659061',measurement:prefix+'/resultsSection/outcomeMeasuresModule/outcomeMeasures/6/derived/0',value:'1',unit:'percentage points'};
 expect(summarizeResults([r],i+'MicronutrientSupplementation')).toEqual([]);
 expect(summarizeResults([r],i+'NutritionEducation')).toEqual([]);
});

test('uses published runtime comparisons without a study-specific browser entry',()=>{
 const make=(name:string,value:string,tested:string[],packageFlag:boolean)=>({
  measurement:`https://example.org/findings/${name}`,study:'https://example.org/studies/new',indicator:'https://example.org/HAZ',indicator_label:'Height-for-age z-score',value,unit:'z-score units',direction:'increase',
  uncertainty:{kind:'standard-error',value:0.181},
  summary_mapping:{study:'https://example.org/studies/new',measurement:`https://example.org/findings/${name}`,selector:null,indicator:'https://example.org/HAZ',unit:'z-score units',tested,comparator:[],background:[],pValueSelector:null,contrast_id:name,package:packageFlag,group_labels:[name,'Control']}
 });
 const intervention='https://example.org/NutritionEducation';
 const output=summarizeResults([make('Maternal', '0.014',[intervention],false),make('Maternal and paternal','-0.198',[intervention,intervention],true)],intervention);
 expect(output).toHaveLength(2);
 expect(output[0].studies).toBe(1);
 expect(output[0].headline).toBe('Direction uncertain');
 expect(output[0].interval).toContain('standard error: 0.18');
 expect(output[1].package).toBe(true);
 expect(output[1].context).toBe('Maternal and paternal versus Control');
 expect(summarizeResults([make('Maternal','0.014',[intervention],false)],'https://example.org/Other')).toHaveLength(0);
});

test('API attribution reverses estimate and interval, preserves shared exclusion and timepoints',()=>{
 const intervention=i+'ParentingProgramIntervention';
 const make=(time:string,value:string,low:string,high:string):ResultRow=>({
  ...z(),study:base+'studies/ctgov/NCT04036708',measurement:'https://source/'+time,
  value,unit:'z-score',indicator:base+'vocab/indicators/HeightForAgeZScore',indicator_label:'Height-for-age z-score',
  comparison:{numerical_validation:{estimate:{status:'valid'},confidence_interval:{status:'valid'},p_value:{status:'valid'}}},
  uncertainty:{ciPctValue:'95',ciNumSides:'TWO_SIDED',ciLowerLimit:low,ciUpperLimit:high,pValue:'.02'},
  summary_mapping:{study:base+'studies/ctgov/NCT04036708',measurement:'https://source/'+time,indicator:base+'vocab/indicators/HeightForAgeZScore',unit:'z-score',tested:[intervention],comparator:[],background:[i+'NutritionEducation'],effect_multiplier:-1,summary_timepoint:time,contrast_id:time,group_labels:['MISC plus WTM','WTM'],package:false}
 });
 const rows=[make('Month 6','-0.16','-0.48','0.16'),make('Month 12','-0.39','-0.72','-0.06')];
 const summaries=summarizeResults(rows,intervention);expect(summaries).toHaveLength(2);
 expect(summaries[0].headline).toBe('No clear difference detected');
 expect(summaries[1].headline).toBe('0.39 z-score higher');
 expect(summaries[1].interval).toBe('95% interval: 0.06–0.72 z-score higher');
 expect(summaries[1].context).toContain('Month 12');expect(summaries.every(s=>s.studies===1)).toBe(true);
 expect(summarizeResults(rows,i+'NutritionEducation')).toHaveLength(0);
});

test('API invalid uncertainty does not create a confident direction',()=>{
 const r:ResultRow={...z(),study:'new',measurement:'new',value:'0.2',unit:'z-score',comparison:{numerical_validation:{estimate:{status:'valid'},confidence_interval:{status:'invalid'},p_value:{status:'invalid'}}},uncertainty:{ciPctValue:'95',ciNumSides:'TWO_SIDED',ciLowerLimit:'.1',ciUpperLimit:'.3',pValue:'-1'},summary_mapping:{study:'new',measurement:'new',indicator:z().indicator,unit:'z-score',tested:[i+'ParentingProgramIntervention'],comparator:[],background:[],effect_multiplier:1}};
 expect(summarizeResults([r],i+'ParentingProgramIntervention')[0].headline).toBe('Direction uncertain');
});

import {aggregateIndicators} from './resultSummaries';
test('indicator overview deduplicates studies and separates package evidence',()=>{
 const intervention='urn:intervention';
 const make=(study:string,id:string,value:string,packageFlag=false,indicator='urn:haz'):ResultRow=>({study,measurement:id,indicator,indicator_label:indicator,value,unit:'z-score',direction:'unknown',uncertainty:{},summary_mapping:{study,measurement:id,indicator,unit:'z-score',tested:[intervention],comparator:[],background:[],package:packageFlag,contrast_id:id}});
 const summaries=summarizeResults([make('s1','a','1'),make('s1','b','-1'),make('s2','c','1',true),make('s2','d','2',true)],intervention);
 const result=aggregateIndicators(summaries);
 expect(result).toHaveLength(1);expect(result[0].studies).toBe(2);
 expect(result[0].counts).toEqual({increase:1,decrease:0,mixed:1,unchanged:0});
 expect(result[0].direct.studies).toBe(1);expect(result[0].combined.studies).toBe(1);
 expect(result[0].headline).toBe('Mixed observed directions');expect(result[0].uncertain).toBe(2);
 expect(aggregateIndicators([...summaries,...summarizeResults([make('s3','e','0',false,'urn:prevalence')],intervention)])).toHaveLength(2);
});

test('new scales preserve odds, nulls, probability units, rates and reverse orientation',()=>{
 const intervention=i+'NutritionEducation';
 function row(unit:string,value:number,lower:number,upper:number,multiplier=1):ResultRow{
  return {...z(),study:'urn:study',measurement:'urn:result',unit,value:String(value),uncertainty:{kind:'confidence-interval',level:95,lower,upper},summary_mapping:{study:'urn:study',measurement:'urn:result',indicator:z().indicator!,unit,tested:[intervention],comparator:[],background:[],effect_multiplier:multiplier}};
 }
 let s=summarizeResults([row('odds ratio',.5,.25,.8)],intervention)[0];
 expect(s.headline).toBe('0.5 odds ratio');expect(s.interval).toBe('95% interval: 0.25–0.8 (odds ratio)');expect(s.evidence[0].direction).toBe(-1);
 s=summarizeResults([row('hazard ratio',.8,.5,1.2)],intervention)[0];expect(s.headline).toBe('No clear difference detected');
 s=summarizeResults([row('odds ratio',2,1.25,4,-1)],intervention)[0];expect(s.headline).toBe('0.5 odds ratio');expect(s.interval).toContain('0.25–0.8');
 expect(summarizeResults([row('odds ratio',0,-1,1)],intervention)).toEqual([]);
 s=summarizeResults([row('probability difference',-.07,-.13,-.01)],intervention)[0];expect(s.headline).toBe('7 percentage points lower');expect(s.interval).toContain('1–13 percentage points lower');
 for(const unit of ['mm','z-score/month']){s=summarizeResults([row(unit,.2,.1,.3)],intervention)[0];expect(s.headline).toBe(`0.2 ${unit} higher`);}
 const se={...row('proportion difference',-.07,-.13,-.01),uncertainty:{kind:'standard-error',value:.02}};
 s=summarizeResults([se],intervention)[0];expect(s.interval).toContain('standard error: 2;');expect(s.headline).toBe('Direction uncertain');
 const alternate=row('odds ratio',.5,.25,.8);(alternate.summary_mapping as any).selected=false;expect(summarizeResults([alternate],intervention)).toEqual([]);
 (alternate.summary_mapping as any).selected=true;(alternate.summary_mapping as any).estimand='interaction';expect(summarizeResults([alternate],intervention)).toEqual([]);
});

test('generic backend fixtures flow through the actual summary consumer for three indicator families', async()=>{
 const {default:rows}=await import('./liveResultFixture.json');
 const summaries=summarizeResults(rows as ResultRow[],i+'NutritionEducation');
 expect(summaries).toHaveLength(3);
 expect(new Set(summaries.map(s=>s.indicatorId)).size).toBe(3);
 expect(summaries.every(s=>s.evidence.length===1 && s.evidence[0].clear)).toBe(true);
 expect(summaries.every(s=>s.evidence[0].direction===1)).toBe(true);
});

test('reviewed estimand exclusions survive the actual summary consumer',async()=>{
 const {default:cases}=await import('./reviewRegressionFixture.json');
 for(const sample of cases){
  const rows=sample.rows as ResultRow[];
  expect(summarizeResults(rows,i+'NutritionEducation')).toHaveLength(sample.context ? 0 : 1);
 }
 const good=cases[cases.length-1].rows.at(-1)! as ResultRow;
 const unsupported=cases[0].rows.at(-1)! as ResultRow;
 const bad={...unsupported,summary_mapping:{...good.summary_mapping,measurement:unsupported.measurement,study:unsupported.study}};
 expect(summarizeResults([bad] as ResultRow[],i+'NutritionEducation')).toEqual([]);
});

 test('fractional report confidence levels preserve effects and null-spanning intervals',()=>{
  for(const [arm,value,lo,hi] of [['sq-lns','0.83',.73,.94],['combined','0.91',.81,1.03]] as const){
   const percent=z(arm,value,lo,hi);
   const fraction={...percent,uncertainty:{kind:'confidence-interval',lower:lo,upper:hi,level:0.95}};
   expect(summarizeResults([fraction],i+'OralNutritionalSupplementation')).toEqual(summarizeResults([percent],i+'OralNutritionalSupplementation'));
   for(const level of [0.9,90,0.99,99,0,1]){
    expect(summarizeResults([{...fraction,uncertainty:{...fraction.uncertainty,level}}],i+'OralNutritionalSupplementation')[0].uncertaintyStatus).toBe('missing');
   }
  }
 });
