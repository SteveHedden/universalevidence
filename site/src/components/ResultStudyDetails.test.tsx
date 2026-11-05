import {fireEvent, render, screen} from '@testing-library/react';
import {expect, test, vi} from 'vitest';
import {InterventionResults, type ResultRow} from './InterventionResults';
import {ResultStudyDetails, type ResultCoverage} from './ResultStudyDetails';
import {summarizeResults, aggregateIndicators} from './resultSummaries';
import data from './task265Fixture.json';
type Fixture = {status:string;graph:string;mode:string;rows:ResultRow[];coverage:ResultCoverage[]};
const fixture = data as unknown as {fresh:Fixture;synthetic:Fixture};
vi.mock('../api/client',()=>({apiGet:vi.fn()}));
const intervention='https://universalevidence.com/vocab/interventions/ExpressiveWritingIntervention';
const props={state:'https://universalevidence.com/vocab/states/Stunting',intervention,context:'{}',active:true};

test('real endpoint-shaped records retain arms, assessment, SD, denominator, and comparison blockers',()=>{
 render(<InterventionResults {...props} reviewed={fixture.fresh}><p>Studies</p></InterventionResults>);
 fireEvent.click(screen.getByRole('tab',{name:'Results'}));
 fireEvent.click(screen.getByText('All source measurements'));
 expect(screen.getByText(/reported indicator/)).toBeVisible();
 expect(screen.getAllByText('Descriptive arm measurement')).toHaveLength(9);
 expect(screen.getAllByText('Expressive Writing (EW)').length).toBeGreaterThan(0);
 expect(screen.getAllByText('Self-Affirmation (SA)').length).toBeGreaterThan(0);
 expect(screen.getAllByText(/Assessment: 36 Participants/).length).toBeGreaterThan(0);
 expect(screen.getByText('Standard Deviation: 11.83')).toBeInTheDocument();
 expect(screen.getAllByText(/Source must state ordered group subtraction/).length).toBeGreaterThan(0);
 expect(screen.getByText(/Unresolved reported comparison/)).toBeInTheDocument();
 expect(summarizeResults(fixture.fresh.rows,intervention)).toEqual([]);
});

test('positive synthetic interaction reaches Results UI but never summary/ranking',()=>{
 render(<InterventionResults {...props} reviewed={fixture.synthetic}><p>Studies</p></InterventionResults>);
 fireEvent.click(screen.getByRole('tab',{name:'Results'}));
 expect(screen.getByText('Reported treatment-by-time interaction')).toBeInTheDocument();
 expect(screen.getByText('Expressive Writing (EW) minus Control')).toBeInTheDocument();
 expect(screen.getByText('baseline to 3 months')).toBeInTheDocument();
 const summaries=summarizeResults(fixture.synthetic.rows,intervention);
 expect(summaries).toEqual([]);expect(aggregateIndicators(summaries)).toEqual([]);
});

test('accidental summary mapping cannot turn arm observations into votes',()=>{
 const row=fixture.fresh.rows[0];
 const mapped={...row,unit:'z-score',summary_mapping:{selected:true,study:row.study,measurement:row.measurement,indicator:row.indicator,unit:'z-score',tested:[intervention],comparator:[],background:[]}};
 expect(summarizeResults([mapped],intervention)).toEqual([]);
});

test('source arm stays visible when component identity is missing',()=>{
 const row=fixture.fresh.rows[0];
 const changed={...row,result_interventions:[],discovery_interventions:[intervention],comparison:{...row.comparison,context:{...(row.comparison?.context as Record<string, unknown>),components:[],mapping_status:'unresolved',mapping_reason:'Awaiting reviewed identity'}}};
 render(<ResultStudyDetails rows={[changed]} coverage={[]} intervention={intervention}/>);
 expect(screen.getByText('Control')).toBeInTheDocument();
 expect(screen.getByText('Unavailable · Intervention mapping unresolved')).toBeInTheDocument();
 expect(screen.getByText('Awaiting reviewed identity')).toBeInTheDocument();
});

test('an entirely unsupported study retains its source coverage gap',()=>{
 const coverage:ResultCoverage[]=[{study:'NCT99999999',title:'An unsupported study',source:'ctgov',status:'unsupported',discovery_interventions:[intervention],coverage:[{locator:'/outcomes/0',title:'Unknown score',status:'unsupported',reason:'No reviewed identity'}]}];
 render(<ResultStudyDetails rows={[] as ResultRow[]} coverage={coverage} intervention={intervention}/>);
 expect(screen.getByText('An unsupported study · 0 reported indicators')).toBeVisible();
 expect(screen.getByText('Unknown score: unsupported · No reviewed identity')).toBeInTheDocument();
});

for (const [statistic, subtype, operator] of [
 ['difference','Mean Difference','minus'],
 ['ratio','Risk Ratio','divided by'],
 ['ratio','Relative Risk','divided by'],
 ['ratio','Odds Ratio','divided by'],
 ['ratio','Hazard Ratio','divided by'],
] as const) {
 test.each([false,true])(`${subtype} preserves its operator and estimate with reversed source order=%s`, reversed => {
  const row:ResultRow={study:'urn:study',measurement:'urn:comparison',indicator:'urn:indicator',indicator_label:'Reported measure',value:'0.5',unit:subtype.toLowerCase(),direction:'unknown',result_interventions:[intervention],comparison:{kind:'source_comparison',statistic,semantics:'explicit',ordered_groups:['Treatment','Control'],orientation:reversed?'reverse':'forward',context:{statistic:subtype,groups:(reversed?['Control','Treatment']:['Treatment','Control']).map(title=>({title}))}}};
  render(<ResultStudyDetails rows={[row]} coverage={[]} intervention={intervention}/>);
  expect(screen.getByText('Ordered contrast').nextElementSibling).toHaveTextContent(`Treatment ${operator} Control`);
  expect(screen.getByText('Source groups').nextElementSibling).toHaveTextContent(reversed?'Control / Treatment':'Treatment / Control');
  expect(screen.getByText('Statistic and source estimate').nextElementSibling).toHaveTextContent(`${subtype}: 0.5 ${subtype.toLowerCase()}`);
 });
}

test.each([undefined,'unsupported'])('unknown comparison operator %s remains unresolved', statistic=>{
 const row:ResultRow={study:'urn:study',measurement:'urn:comparison',indicator_label:'Unknown statistic',direction:'unknown',result_interventions:[intervention],comparison:{kind:'source_comparison',statistic,ordered_groups:['Treatment','Control']}};
 render(<ResultStudyDetails rows={[row]} coverage={[]} intervention={intervention}/>);
 expect(screen.getByText('Ordered contrast').nextElementSibling).toHaveTextContent(/^Unresolved$/);
});
