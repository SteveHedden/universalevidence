import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { vi, test, expect } from 'vitest';
import { InterventionResults } from './InterventionResults';
import { apiGet } from '../api/client';
vi.mock('../api/client',()=>({apiGet:vi.fn()}));
const props={state:'https://universalevidence.com/vocab/states/Stunting',intervention:'urn:intervention',context:'{}',active:true};
const response={completion:'partial',coverage:[{source:'CT.gov',study:'NCT1',status:'available'}],rows:[{measurement:'urn:m',study:'NCT1',indicator_label:'Stunting prevalence',indicator:'urn:i',value:'62',unit:'%',direction:'unknown',result_text:{raw:'62',numerical_validation:'valid'},comparison:{kind:'arm_level',context:{group:{title:'Advice'}}}}],timings:{},interpretation:'No pooling'};
test('results appear only in Results tab; studies remain separate; request reused',async()=>{
 let resolve!:(v:unknown)=>void;vi.mocked(apiGet).mockClear().mockImplementation(()=>new Promise(r=>{resolve=r;}));
 const view=render(<InterventionResults {...props}><p>Existing study</p></InterventionResults>);
 expect(screen.getByText('Existing study')).toBeVisible();expect(screen.queryByRole('status')).toBeNull();
 expect(screen.getByRole('tab',{name:'Studies'})).toHaveAttribute('aria-selected','true');
 resolve(response);expect(await screen.findByText('No results available for a supported summary in the returned search scope.')).not.toBeVisible();
 fireEvent.click(screen.getByRole('tab',{name:'Results'}));expect(screen.getByText('No results available for a supported summary in the returned search scope.')).toBeVisible();
 expect(screen.getByText('Existing study')).not.toBeVisible();
 fireEvent.keyDown(screen.getByRole('tab',{name:'Results'}),{key:'ArrowLeft'});
 expect(screen.getByRole('tab',{name:'Studies'})).toHaveFocus();
 view.rerender(<InterventionResults {...props} active={false}><p>Existing study</p></InterventionResults>);
 view.rerender(<InterventionResults {...props}><p>Existing study</p></InterventionResults>);
 await waitFor(()=>expect(apiGet).toHaveBeenCalledTimes(1));
});
test('failure is inline and retry is explicit',async()=>{
 vi.mocked(apiGet).mockRejectedValue(new Error('Source timeout'));
 render(<InterventionResults {...props}><p>Existing study</p></InterventionResults>);
 expect(screen.getByText('Existing study')).toBeVisible();
 fireEvent.click(screen.getByRole('tab',{name:'Results'}));
 expect(await screen.findByRole('alert')).toHaveTextContent('Source timeout');
 expect(screen.getByText('Existing study')).not.toBeVisible();expect(screen.getByRole('button',{name:'Retry results'})).toBeVisible();
});
test('late responses from old query cannot replace current context',async()=>{
 let old!:(v:unknown)=>void;vi.mocked(apiGet).mockImplementationOnce(()=>new Promise(r=>{old=r;})).mockResolvedValue(response);
 const view=render(<InterventionResults {...props}><p>Studies</p></InterventionResults>);
 view.rerender(<InterventionResults {...props} context='{"revision":2}'><p>Studies</p></InterventionResults>);
 await screen.findByText('No results available for a supported summary in the returned search scope.');old({...response,rows:[]});
 await waitFor(()=>expect(screen.getByText('No results available for a supported summary in the returned search scope.')).not.toBeVisible());
});

test('unsupported structured result text cannot crash the view',async()=>{
 vi.mocked(apiGet).mockResolvedValue({...response,rows:[{...response.rows[0],value:undefined}]});
 render(<InterventionResults {...props}><p>Existing study</p></InterventionResults>);
 await screen.findByText('No results available for a supported summary in the returned search scope.');
 expect(screen.getByText('Existing study')).toBeVisible();
});

test('same indicator renders one overview with collapsed comparisons',async()=>{
 const row=(study:string,measurement:string,value:string)=>({study,measurement,value,unit:'z-score',subgroup:'Same population',timing:'Month 3',indicator:'urn:haz',indicator_label:'Height-for-age z-score',direction:'unknown',summary_mapping:{study,measurement,indicator:'urn:haz',unit:'z-score',tested:[props.intervention],comparator:[],background:[],contrast_id:measurement}});
 vi.mocked(apiGet).mockResolvedValue({...response,completion:'complete',rows:[row('s1','m1','1'),row('s1','m2','2'),row('s2','m3','-1')]});
 render(<InterventionResults {...props}><p>Studies content</p></InterventionResults>);
 fireEvent.click(screen.getByRole('tab',{name:'Results'}));
 await screen.findAllByText('Height-for-age z-score');
 const indicator=document.querySelector('.result-indicator')!;
 expect(indicator).not.toHaveAttribute('open');
 expect(indicator.querySelector('summary')).toHaveTextContent('2 studies');
 expect(indicator.querySelector('summary')).toHaveTextContent('Observed increase · 1');
 expect(indicator.querySelector('summary')).toHaveTextContent('Observed decrease · 1');
 expect(document.querySelectorAll('.result-indicator')).toHaveLength(1);
 expect(document.querySelectorAll('details[open]')).toHaveLength(0);
 fireEvent.click(indicator.querySelector('summary')!);
 const direction=indicator.querySelector('.result-direction')!;
 fireEvent.click(direction.querySelector('summary')!);
 expect(direction.querySelectorAll('.result-supporting-study')).toHaveLength(1);
 expect(direction.querySelector('.result-supporting-study summary')).toHaveTextContent('s1 · 2 findings');

});

test('initial-search snapshot serves Results without a second request',async()=>{
 vi.mocked(apiGet).mockClear();
 render(<InterventionResults {...props} reviewed={{status:'available',graph:'urn:release',rows:[]}}><p>Matched study</p></InterventionResults>);
 fireEvent.click(screen.getByRole('tab',{name:'Results'}));
 expect(screen.getByText('Reviewed result snapshot · included in this search.')).toBeVisible();
 expect(apiGet).not.toHaveBeenCalled();
});

import writingRows from './task266WritingFixture.json';
import type {ResultRow} from './InterventionResults';
test('writing descriptive results and unresolved comparisons remain inside collapsed indicators',()=>{
 render(<InterventionResults {...props} intervention="https://universalevidence.com/vocab/interventions/ExpressiveWritingIntervention" reviewed={{status:'available',graph:'test',rows:writingRows as unknown as ResultRow[]}}><p>Studies</p></InterventionResults>);
 fireEvent.click(screen.getByRole('tab',{name:'Results'}));
 expect(screen.getAllByText(/25.17 → 18.45/).every(e=>!!e.closest('.result-indicator'))).toBe(true);
 expect(document.querySelectorAll('details[open]')).toHaveLength(0);
 expect(screen.getAllByText('Unable to interpret').every(e=>!!e.closest('details'))).toBe(true);
 const study=document.querySelector('.result-study-details > details')!;fireEvent.click(study.querySelector('summary')!);
 const indicator=study.querySelector('details')!;fireEvent.click(indicator.querySelector('summary')!);
 expect(screen.getAllByText('Neutral writing task').length).toBeGreaterThan(0);
 expect(screen.queryByText('Neutral Writing Comparator Intervention')).toBeNull();
});
