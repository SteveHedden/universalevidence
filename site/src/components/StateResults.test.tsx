import {render,screen,fireEvent,within} from '@testing-library/react';
import {describe,it,expect} from 'vitest';
import {SearchResults} from './SearchResults';
import {StateResults} from './StateResults';
import type {ResultContext} from './InterventionResults';
const P='https://universalevidence.com/';
const a=P+'vocab/states/Alpha',b=P+'vocab/states/Beta',i=P+'vocab/interventions/Example';
describe('symmetric State results',()=>{
 it('groups one study under all mapped States without inflating the search count or mixing roles',()=>{
  render(<SearchResults groupBy="condition" results={[{study_id:'one',source:'AEA',title:'One study',state_mappings:[{uri:a,label:'Alpha',role:'condition'},{uri:b,label:'Beta',role:'result'}]}]}/>);
  expect(screen.getByText(/1 study · 2 States/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:/Alpha.*1 study/}));
  expect(screen.getByText('Condition studied')).toBeVisible();
  fireEvent.click(screen.getByRole('button',{name:/Beta.*1 study/}));
  expect(screen.getByText('Reported result')).toBeVisible();
 });
 it('does not leak another State’s results and discloses unresolved attribution',()=>{
  const context:ResultContext={state:'',context:'{}',reviewed:{status:'available',graph:'fixture',rows:[
   {measurement:'m',study:'s',states:[b],indicator_label:'Wrong State',direction:'unknown',value:'7',result_interventions:[i]},
   {measurement:'n',study:'s',states:[a],indicator_label:'Unattributed',direction:'unknown',value:'3'}]}};
  render(<StateResults state={a} context={context} studies={[]}><p>Studies</p></StateResults>);
  fireEvent.click(screen.getByRole('tab',{name:'Results'}));
  expect(screen.getByText('1 reported results have unresolved intervention attribution.')).toBeVisible();
  expect(screen.queryByText('Wrong State')).not.toBeInTheDocument();
 });
});
