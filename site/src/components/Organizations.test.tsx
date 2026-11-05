import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { Organizations, type OrganizationResponse } from './Organizations';
import { InterventionResults } from './InterventionResults';
const props={state:'https://universalevidence.com/vocab/states/Stunting',intervention:'https://universalevidence.com/vocab/interventions/OralNutritionalSupplementation',context:'{}',active:true};
const project={id:'a',title:'Nutrition project',source_url:'https://d-portal.org/q.html?aid=a',project_url:null,countries:[{code:'KE',name:'Kenya'}],retrieved_at:'2026-10-09T12:00:00Z',freshness:{status:'unverified',issues:['planned-end-passed'],dates:[{'@type':'3','@iso-date':'2025-12-31'}],updated_at:'2026-01-01'},matches:{state:{support:[{text:'We prevent stunting.',kind:'objective'}]}}};
const data:OrganizationResponse={status:'ok',organizations:[{id:'org',name:'Example implementer',relevance:'explicit',representative:project,activities:[project]}],unresolved_participants:[],missing_dimensions:[],errors:[],coverage:{truncated:false},retrieved_at:project.retrieved_at,candidate_mapping_review:false};
function respond(value: OrganizationResponse){return {ok:true,json:async()=>value} as Response;}
beforeEach(()=>{vi.stubGlobal('fetch',vi.fn().mockResolvedValue(respond(data)));});
afterEach(()=>{vi.unstubAllGlobals();vi.useRealTimers();});

test('initial evidence and Results make zero Organizations calls; keyboard opens on demand',async()=>{
 render(<InterventionResults {...props} reviewed={{status:'available',graph:'',rows:[]}}><p>Existing studies</p></InterventionResults>);
 expect(fetch).not.toHaveBeenCalled();
 fireEvent.click(screen.getByRole('tab',{name:'Results'}));expect(fetch).not.toHaveBeenCalled();
 fireEvent.keyDown(screen.getByRole('tab',{name:'Results'}),{key:'ArrowRight'});
 expect(screen.getByRole('tab',{name:'Organizations'})).toHaveFocus();
 expect(await screen.findByText('Example implementer')).toBeVisible();
 expect(fetch).toHaveBeenCalledTimes(1);
 fireEvent.keyDown(screen.getByRole('tab',{name:'Organizations'}),{key:'ArrowRight'});
 expect(screen.getByRole('tab',{name:'Studies'})).toHaveFocus();
 expect(screen.getByText('Existing studies')).toBeVisible();
 fireEvent.keyDown(screen.getByRole('tab',{name:'Studies'}),{key:'End'});
 expect(screen.getByRole('tab',{name:'Organizations'})).toHaveFocus();
 fireEvent.keyDown(screen.getByRole('tab',{name:'Organizations'}),{key:'Home'});
 expect(screen.getByRole('tab',{name:'Studies'})).toHaveFocus();
});

test('context change aborts and hides stale response even if fetch ignores abort',async()=>{
 let finish!:(r:Response)=>void;
 vi.mocked(fetch).mockImplementationOnce(()=>new Promise(r=>{finish=r}));
 const view=render(<Organizations {...props}/>);
 expect(screen.getByRole('status')).toHaveTextContent('Finding');
 view.rerender(<Organizations {...props} context='{"revision":2}'/>);
 expect(await screen.findByText('Example implementer')).toBeVisible();
 await act(async()=>finish(respond({...data,organizations:[{...data.organizations[0],name:'Stale organization'}]})));
 expect(screen.queryByText('Stale organization')).toBeNull();
 expect((vi.mocked(fetch).mock.calls[0][1]?.signal as AbortSignal).aborted).toBe(true);
});

test.each([
 ['missing-mappings','Approved IATI mappings'],['empty','No matching activities'],
 ['insufficient-matches','Returned activities did not'],['error','IATI retrieval is unavailable'],
 ['partial','Some activities or source requests'],
])('explicit %s state',async(status,text)=>{
 vi.mocked(fetch).mockResolvedValue(respond({...data,status,organizations:[]}));
 render(<Organizations {...props}/>);expect(await screen.findByText(new RegExp(text))).toBeVisible();
});

test('candidates, date uncertainty, missing dimension and unresolved identities stay explicit',async()=>{
 vi.mocked(fetch).mockResolvedValue(respond({...data,organizations:[{...data.organizations[0],relevance:'candidate'}],unresolved_participants:[{}],missing_dimensions:['condition'],coverage:{truncated:true}}));
 render(<Organizations {...props}/>);
 expect(await screen.findByText('Relevant candidates — condition relevance unconfirmed')).toBeVisible();
 expect(screen.getByText('Current operation is unverified.')).toBeVisible();
 expect(screen.getByText(/lack resolved identifiers/)).toBeVisible();
 expect(screen.getByText(/condition not specified/)).toBeVisible();
 expect(screen.getByText(/retrieval was partial or capped/)).toBeVisible();
});

test('network failure has no automatic retry and Studies stay available',async()=>{
 vi.mocked(fetch).mockRejectedValue(new Error('network'));
 render(<InterventionResults {...props} reviewed={{status:'available',graph:'',rows:[]}}><p>Existing studies</p></InterventionResults>);
 fireEvent.click(screen.getByRole('tab',{name:'Organizations'}));
 expect(await screen.findByRole('alert')).toHaveTextContent('could not be retrieved');
 expect(fetch).toHaveBeenCalledTimes(1);
 fireEvent.click(screen.getByRole('tab',{name:'Studies'}));expect(screen.getByText('Existing studies')).toBeVisible();
});

test('multiple conditions never silently use only the first condition',()=>{
 render(<Organizations {...props} context={JSON.stringify({values:{state:['a','b']}})}/>);
 expect(screen.getByText(/Organization search is currently in development/)).toBeVisible();expect(fetch).not.toHaveBeenCalled();
});

test('source URLs cannot create javascript links',async()=>{
 vi.mocked(fetch).mockResolvedValue(respond({...data,organizations:[{...data.organizations[0],representative:{...project,project_url:'javascript:alert(1)'}}]}));
 render(<Organizations {...props}/>);await screen.findByText('Example implementer');
 expect(screen.queryByText(/Publisher’s project website/)).toBeNull();
});


test('a sentence supporting both concepts appears only once on the card',async()=>{
 const p={...project,matches:{state:{support:[{text:'We provide nutritional supplements to prevent stunting.',kind:'objective'}]},intervention:{support:[{text:'We provide nutritional supplements to prevent stunting.',kind:'objective'}]}}};
 vi.mocked(fetch).mockResolvedValue(respond({...data,organizations:[{...data.organizations[0],representative:p}]}));
 render(<Organizations {...props}/>);
 await screen.findByText('Example implementer');
 expect(screen.getAllByText('We provide nutritional supplements to prevent stunting.')).toHaveLength(1);
});


test('project search displays reported roles and source-scoped names without delivery claims',async()=>{
 const p={...project,participants:[{names:['Named partner'],ref:null,role:'implementing',identity_status:'unresolved'},{names:['Funding partner'],ref:'GB-123',role:'funding',identity_status:'identified'}],reporting_organizations:[{names:['Publisher'],ref:'GB-PUB'}]};
 vi.mocked(fetch).mockResolvedValue(respond({...data,mode:'project-mentions',projects:[p],organizations:[]}));
 render(<Organizations {...props}/>);
 expect(await screen.findByText('Projects and reported organizations')).toBeVisible();
 expect(screen.getByText(/Implementer: Named partner.*identifier unavailable/)).toBeVisible();
 expect(screen.getByText(/Funder: Funding partner/)).toBeVisible();
 expect(screen.getByText(/Matches do not establish delivery/)).toBeVisible();
 expect(screen.queryByText(/Listed implementer of a project that includes/)).toBeNull();
 expect(screen.getByText('We prevent stunting.')).toBeVisible();
});

test.each(['missing-mappings','empty','insufficient-matches','error'])('project search exposes %s without fabricated cards',async(status)=>{
 vi.mocked(fetch).mockResolvedValue(respond({...data,mode:'project-mentions',status,projects:[],organizations:[]}));
 render(<Organizations {...props}/>);
 await screen.findByText('Projects and reported organizations');
 expect(screen.queryByText('Nutrition project')).toBeNull();
});


test('unsupported reviewed code querying remains distinct from missing or empty',async()=>{
 vi.mocked(fetch).mockResolvedValue(respond({...data,mode:'project-mentions',status:'unsupported',projects:[],organizations:[]}));
 render(<Organizations {...props}/>);
 expect(await screen.findByText(/does not yet support querying/)).toBeVisible();
 expect(screen.queryByText(/No projects were returned/)).toBeNull();
});

const reviewedProject={id:'https://example.org/study',title:'Reviewed trial project',source:'aea',study_id:'AEARCTR-0000001',source_url:'https://example.org/registry',source_date:'2025-01-01',reviewed_at:'2026-10-15T00:00:00Z',status:'Completed (source reported)',geography:['Zambia'],aliases:[],dates:[{label:'End',value:'2023-12-31'}],match_reasons:['Source explicitly aims to prevent stunting through supplements'],contexts:[{id:'context',components:[{uri:'supplement',component_id:'component',label:'Supplement arm'}]}],implementers:[],other_roles:[{name:'Research funder',organization_id:null,identity_status:'unresolved',role:'funder',scope:{kind:'study',id:'study'},evidence:[]}],missing_role:'Implementing organization not yet verified',gaps:['Delivery organization evidence not available']};
const reviewed={status:'ok',projects:[reviewedProject],count:1,scope:'Frozen reviewed Stunting cohort; both State and Intervention required. Other study filters are not applied.',freshness:'Retained reviewed metadata; current operation is not freshly verified.',errors:[]};
test.each(['missing-mappings','error','partial','empty'])('reviewed project survives IATI %s with truthful roles and dates',async(status)=>{
 vi.mocked(fetch).mockResolvedValue(respond({...data,mode:'project-mentions',status,projects:[],organizations:[],study_projects:reviewed}));
 render(<Organizations {...props}/>);
 const dropdown=await screen.findByText('Organization not yet identified');
 fireEvent.click(dropdown);
 dropdown.closest('details')!.open=true;
 expect(screen.getByText('Reviewed trial project')).toBeVisible();
 expect(screen.getByText('Implementing organization not yet verified')).toBeVisible();
 expect(screen.getByText(/funder: Research funder/)).toBeVisible();
 const details=screen.getByText('Evidence and details');
 fireEvent.click(details);details.closest('details')!.open=true;
 expect(screen.getByText(/Record reviewed: 15 October 2026/)).toBeVisible();
 expect(screen.getByText(/Completed \(source reported\)/i)).toBeVisible();
 expect(screen.queryByText('Verified implementing organizations')).toBeNull();
});
test('bundle scope displayed without component delivery claim',async()=>{
 const implementer={name:'Programme delivery organization',organization_id:'org',identity_status:'verified',role:'implementer',scope:{kind:'context',id:'context'},evidence:[]};
 vi.mocked(fetch).mockResolvedValue(respond({...data,study_projects:{...reviewed,projects:[{...reviewedProject,implementers:[implementer],missing_role:null}]}}));
 render(<Organizations {...props}/>);
 const dropdown=await screen.findByText('Programme delivery organization');
 fireEvent.click(dropdown);dropdown.closest('details')!.open=true;
 expect(screen.getByText('Verified implementing organizations')).toBeVisible();
 expect(screen.getByText(/Programme-wide role/)).toBeVisible();
});

 test('verified organization dropdown groups projects by identity and keeps roles scoped',async()=>{
 const org={name:'Verified university',organization_id:'org:one',identity_status:'verified',role:'sponsor',scope:{kind:'study',id:'study'},evidence:[]};
 const projects=[{...reviewedProject,other_roles:[org,{...org,role:'funder'}]},{...reviewedProject,id:'second',title:'Second project',other_roles:[org]}];
 vi.mocked(fetch).mockResolvedValue(respond({...data,mode:'project-mentions',projects:[],study_projects:{...reviewed,projects,count:2}}));
 render(<Organizations {...props}/>);
 const name=await screen.findByText('Verified university');const dropdown=name.closest('details')!;
 expect(dropdown.open).toBe(false);expect(dropdown.querySelector('summary')).toHaveTextContent('2 projects');
 expect(screen.getByText('Second project')).not.toBeVisible();
 fireEvent.click(name);dropdown.open=true;
 expect(screen.getByText('Second project')).toBeVisible();
 expect(screen.getAllByText('sponsor: Verified university')).toHaveLength(2);
 expect(screen.getByText('funder: Verified university')).toBeVisible();
 expect(screen.queryByText('Organization not yet identified')).toBeNull();
 });

test('broader nutrition projects are labeled separately from direct matches',async()=>{
 vi.mocked(fetch).mockResolvedValue(respond({...data,study_projects:{...reviewed,projects:[],count:0,related_projects:[{...reviewedProject,title:'Broader nutrition project',match_reasons:['Nutrition relevance only']}],related_count:1}}));
 render(<Organizations {...props}/>);
 expect(await screen.findByRole('heading',{name:'Related nutrition projects'})).toBeVisible();
 expect(screen.getByText(/A specific stunting objective or stunting outcome has not been established/)).toBeVisible();
 expect(screen.getByText('Broader nutrition project')).toBeInTheDocument();
});
