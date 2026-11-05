import {describe,it,expect} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {summarizeFindings,groupFindingsByIndicator} from './resultFindings';
import {summarizeResults} from './resultSummaries';
import {InterventionResults,type ResultRow} from './InterventionResults';
const intervention='urn:nutrition';
const row:ResultRow={measurement:'urn:study/finding/1',study:'urn:study',indicator:'urn:stunting',indicator_label:'Stunting',direction:'unknown',result_text:'No statistically significant difference; not equivalence.',timing:{reported:'12 months'},comparison:{kind:'association',context:{model:'observational cohort'}},finding_mapping:{presentation:'qualified-text',selected:false,effect_eligible:false,tested:[intervention],group_labels:['Participants','Nonparticipants'],reason:'Observational; no causal effect inferred.'}};
describe('retained report finding independent checks',()=>{
 it('retains qualified source text without zero-effect or causal conversion',()=>{
  const output=summarizeFindings([row],intervention);expect(output).toHaveLength(1);expect(output[0].direction).toBeNull();expect(output[0].code).toBe('reported');expect(output[0].detail).toContain(row.result_text);expect(summarizeResults([row],intervention)).toEqual([]);
 });
 it('does not assign another intervention and does not multiply distinct studies',()=>{
  expect(summarizeFindings([row],'urn:other')).toEqual([]);
  const output=summarizeFindings([row,{...row,measurement:'urn:study/finding/2'}],intervention);
  expect(groupFindingsByIndicator(output)[0].studies).toBe(1);
 });
 it('preserves nested model qualifications in the actual card',()=>{
  const output=summarizeFindings([row],intervention);expect(output[0].detail).toContain('observational cohort');expect(output[0].detail).not.toContain('[object Object]');
  const html=renderToStaticMarkup(<InterventionResults active resultsOnly state="https://universalevidence.com/vocab/states/Stunting" intervention={intervention} context="{}" reviewed={{status:'available',graph:'',rows:[row]}}>{null}</InterventionResults>);
  expect(html).toContain('No statistically significant difference; not equivalence.');expect(html).not.toContain('Direct comparison');
 });
 it('retains numerical descriptive zero only as a source measurement',()=>{
  const r={...row,result_text:'Quartiles are not confidence intervals.',value:'0',comparison:{kind:'within-group'},finding_mapping:{...row.finding_mapping,presentation:'descriptive'}};
  const output=summarizeFindings([r],intervention);expect(output[0].code).toBe('reported');expect(output[0].direction).toBeNull();expect(summarizeResults([r],intervention)).toEqual([]);
 });
});
