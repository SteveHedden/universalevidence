import {test,expect} from 'vitest';
import {render,screen} from '@testing-library/react';
import {InterventionResults,type ResultRow} from './InterventionResults';
import {summarizeFindings} from './resultFindings';
import {summarizeResults} from './resultSummaries';
const I='urn:intervention';
function row(study='urn:study'):ResultRow{return {study,measurement:'urn:measurement',measurement_conflict:true,
 indicator:'urn:indicator',indicator_label:'Example score',unit:'z-score',value:'0.07',direction:'increase',result_interventions:[I],
 uncertainty:{kind:'confidence-interval',level:95,lower:.01,upper:.12},
 summary_mapping:{study,measurement:'urn:measurement',indicator:'urn:indicator',unit:'z-score',tested:[I],comparator:[],background:[]}};}
test('a flagged single row remains unresolved after State/intervention filtering and cannot rank',()=>{
 const rows=[row()];expect(summarizeResults(rows,I)).toEqual([]);
 const findings=summarizeFindings(rows,I);expect(findings).toHaveLength(1);expect(findings[0].code).toBe('uninterpretable');expect(findings[0].rows[0].value).toBe('0.07');
 render(<InterventionResults state="https://universalevidence.com/vocab/states/Stunting" intervention={I} context="{}" active resultsOnly reviewed={{status:'available',graph:'test',rows}}>{null}</InterventionResults>);
 expect(screen.getAllByText('Unable to interpret').length).toBeGreaterThan(0);
 expect(screen.queryByText('Observed increase')).toBeNull();
 expect(screen.getByText(/Conflicting measurement payload or study identity/)).toBeInTheDocument();
});
test('conflicting numerical payloads and different study identities are all retained without votes',()=>{
 const rows=[row(),{...row(),value:'99'},row('urn:other-study')];
 expect(summarizeResults(rows,I)).toEqual([]);
 const findings=summarizeFindings(rows,I);expect(findings).toHaveLength(2);
 expect(new Set(findings.map(f=>f.study)).size).toBe(2);expect(findings.every(f=>f.code==='uninterpretable')).toBe(true);
 expect(findings.flatMap(f=>f.rows).map(r=>r.value)).toEqual(['0.07','99','0.07']);
});
