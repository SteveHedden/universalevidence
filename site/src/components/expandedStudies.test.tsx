import {render,screen,fireEvent,waitFor,cleanup} from '@testing-library/react';
import {afterEach,describe,it,expect,vi} from 'vitest';
import * as client from '../api/client';
import {SearchResults} from './SearchResults';
import {groupQuery,mergeMeasurements} from './expandedStudies';
import type {ResultRow} from './InterventionResults';
const I='https://universalevidence.com/vocab/interventions/OralNutritionalSupplementation';
const S='https://universalevidence.com/vocab/states/Stunting';
const context=JSON.stringify({values:{state:[S]},logic:{},country:''});
const study=(id:string,source='ISRCTN'):client.StudyResult=>({study_id:id,source,title:id,intervention_concept:'Oral nutritional supplementation',intervention_concept_uri:I});
const row=(id:string):ResultRow=>({study:id,measurement:id,indicator_label:'Height-for-age z-score',value:'1',direction:'',discovery_interventions:[I]});
const original=[1,2,3,4,5].map(i=>study('LOCAL'+i));
const reviewed={status:'available',graph:'g',mode:'api-first',rows:[1,2,3,4].map(i=>row('LOCAL'+i))};
const response:client.QueryV2Response={results:[...Array.from({length:7},(_,i)=>study('NCT'+i,'CT.gov')),original[0]],reviewed_results:{...reviewed,rows:[]},meta:{api_version:'query-v2',returned_unique_studies:8,limit_per_source_branch:100,truncated:false,approximate:false,execution_status:'complete',sources:{}}};
afterEach(()=>{cleanup();vi.restoreAllMocks();});
describe('expanded intervention groups',()=>{
 it('loads on expansion, preserves local studies/results, updates totals, and avoids repeated loads',async()=>{
  const fetch=vi.spyOn(client,'queryStudiesV2').mockResolvedValue(response);
  render(<SearchResults results={original} resultContext={{state:S,context,reviewed}}/>);
  expect(fetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:/Oral nutritional supplementation.*5 studies/}));
  await screen.findByText('NCT6',{selector:'.study-id'});
  expect(fetch).toHaveBeenCalledWith({state:[S],intervention:[I]},{intervention:'or'});
  const group=screen.getByRole('button',{name:/Oral nutritional supplementation.*12 studies.*4 with results/});
  expect(screen.getByText('LOCAL5',{selector:'.study-id'})).toBeVisible();
  expect(screen.getByText(/12 studies/, {selector:'.results-summary'})).toBeVisible();
  fireEvent.click(group);fireEvent.click(group);expect(fetch).toHaveBeenCalledTimes(1);
 });
 it('retains existing cards on failure and supports retry',async()=>{
  const fetch=vi.spyOn(client,'queryStudiesV2').mockRejectedValueOnce(new Error('offline')).mockResolvedValue(response);
  render(<SearchResults results={original} resultContext={{state:S,context,reviewed}}/>);
  fireEvent.click(screen.getByRole('button',{name:/Oral nutritional supplementation.*5 studies/}));
  await screen.findByRole('button',{name:'Retry'});expect(screen.getByText('LOCAL5',{selector:'.study-id'})).toBeVisible();
  fireEvent.click(screen.getByRole('button',{name:'Retry'}));await screen.findByText('NCT6',{selector:'.study-id'});expect(fetch).toHaveBeenCalledTimes(2);
 });
 it('preserves all submitted State/Region predicates and logic, and skips already filtered or legacy country searches',()=>{
  const values={state:[S,'other-state'],region:['region-a','region-b']};const logic={state:'and',region:'or'};
  expect(groupQuery(JSON.stringify({values,logic}),I)).toEqual({values:{...values,intervention:[I]},logic:{...logic,intervention:'or'}});
  expect(groupQuery(JSON.stringify({values:{...values,intervention:['other']}}),I)).toBeNull();
  expect(groupQuery(JSON.stringify({values,country:'Kenya'}),I)).toBeNull();
 });
 it('deduplicates identical observations while preserving genuinely conflicting values',()=>{
  const a=row('same');const b={...a,discovery_interventions:['other',I]};
  expect(mergeMeasurements([a],[b])).toHaveLength(1);
  expect(mergeMeasurements([a],[b])[0].discovery_interventions).toEqual([I,'other']);
  expect(mergeMeasurements([a],[{...b,value:'2'}])).toHaveLength(2);
 });
});
